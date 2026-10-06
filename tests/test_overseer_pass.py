"""Overseer passes: the record, the digest, the supervisor's side, the CLI.

The supervisor-side tests build a real :class:`Supervisor` in-process on the bare
driver with the session spawn stubbed, so every step of a pass (reserve, spawn,
end, timeout, finish hold) is driven by hand and nothing ``claude`` ever starts.
The end-to-end tests at the bottom run the real supervisor process against the
demo fixture with ``fake-master.sh`` playing the Overseer.
"""

from __future__ import annotations

import fcntl
import json
import os
import time

import pytest
from conftest import machine_toml

from swarm_orchestrator import ovdigest, ovrecord
from swarm_orchestrator import notes as notes_mod
from swarm_orchestrator import overseer as ov
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load
from swarm_orchestrator.supervisor import Supervisor


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "ledger.txt").write_text("P0\nP1 needs:P0\nP2 needs:P1\nP3 needs:P0\n")
    (project / ".swarm.toml").write_text(
        '[swarm]\nmax_workers = 2\ndriver = "bare"\n[tasks]\nledger = "ledger.txt"\n'
        "[worker]\npark_after = 120\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_OVERSEER", "1")
    monkeypatch.setenv("SWARM_WATCHDOG", "0")
    for leak in ("SWARM_DRIVER", "SWARM_GIT_ISOLATION", "SWARM_MASTER_CMD", "SWARM_OVERSEER_PASS"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


def _tg(tmp_path) -> str:
    p = tmp_path / "tg.log"
    return p.read_text() if p.is_file() else ""


def _ledger(cfg) -> list[dict]:
    p = cfg.state_dir / "notifications.jsonl"
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln] if p.is_file() else []


# -- records -------------------------------------------------------------------
def test_a_record_is_a_json_half_and_a_markdown_half_joined_by_the_loader(cfg):
    ovrecord.create(cfg, "20260923T100000Z", [{"key": "fail:P1", "text": "P1 finished fail"}],
                    cfg.state_dir / "overseer" / "digest-x.md", now=1000.0)
    md = ovrecord.md_path(cfg, "20260923T100000Z")
    text = md.read_text()
    assert "- P1 finished fail" in text and "## Saw" in text and "## Left for the owner" in text
    md.write_text(text.replace("## Saw\n", "## Saw\nP1 failed on a flaky test\n")
                  .replace("## Did\n", "## Did\nretried P1\n"))
    ovrecord.update(cfg, "20260923T100000Z", status=ovrecord.DONE, ended_at=1600.0, summary="retried P1")

    [rec] = ovrecord.load_passes(cfg)
    assert (rec.status, rec.summary, rec.duration_s) == ("done", "retried P1", 600.0)
    assert rec.saw == "P1 failed on a flaky test" and rec.did == "retried P1" and rec.left == ""
    assert rec.reasons[0]["key"] == "fail:P1"


def test_a_finished_pass_keeps_how_it_ended(cfg):
    ovrecord.create(cfg, "20260923T100000Z", [], None)
    ovrecord.update(cfg, "20260923T100000Z", status=ovrecord.DONE, ended_at=1.0)
    ovrecord.update(cfg, "20260923T100000Z", status=ovrecord.TIMEOUT, ended_at=2.0)
    rec = ovrecord.load_json(cfg, "20260923T100000Z")
    assert (rec.status, rec.ended_at) == ("done", 1.0)


def test_a_running_record_that_is_not_live_reads_as_interrupted(cfg):
    ovrecord.create(cfg, "20260923T100000Z", [], None)
    ovrecord.create(cfg, "20260923T110000Z", [], None)
    passes = ovrecord.load_passes(cfg, live="20260923T110000Z")
    assert [(p.id, p.status) for p in passes] == [
        ("20260923T110000Z", "running"), ("20260923T100000Z", "interrupted")
    ]
    assert ovrecord.mark_stale(cfg, live="20260923T110000Z") == ["20260923T100000Z"]
    assert ovrecord.load_json(cfg, "20260923T100000Z").status == "interrupted"


def test_ids_are_unique_within_a_second_and_mirrors_are_branch_safe(cfg):
    a = ovrecord.new_id(cfg, 0.0)
    ovrecord.create(cfg, a, [], None)
    b = ovrecord.new_id(cfg, 0.0)
    assert a == "19700101T000000Z" and b == "19700101T000000Z-2"
    assert ovrecord.mirror_name(a) == "ovs-19700101t000000z"


# -- the digest ------------------------------------------------------------------
def _done(cfg, phase, status, note, at):
    path = cfg.done_dir / f"{phase}.{status}"
    path.write_text(f"{phase} {status} {note}\n")
    os.utime(path, (at, at))
    with state_mod.transaction(cfg) as st:
        st.mark_done(phase, status)


def test_the_digest_lists_what_finished_since_the_last_pass_with_its_notes(cfg):
    now = time.time()
    _done(cfg, "P0", "ok", "laid the base", now - 7200)
    _done(cfg, "P1", "fail", "gate red on the flaky suite", now - 60)
    notes_mod.add(cfg, "P1", "the suite is flaky on a loaded box", "risk")
    with state_mod.transaction(cfg) as st:
        st.waiting["P3"] = now + 60  # asked a minute ago (park_after 120)
    reasons = [ov.Reason("fail:P1", "P1 finished fail", False, now)]

    data = ovdigest.build(cfg, state_mod.read(cfg), reasons, since=now - 3600)
    md = ovdigest.render(data)

    assert [f["phase"] for f in data["finished"]] == ["P1"]  # P0 is older than the pass
    assert data["finished"][0]["notes"] == [{"kind": "risk", "text": "the suite is flaky on a loaded box"}]
    assert data["failures"] == [{"phase": "P1", "note": "gate red on the flaky suite"}]
    assert data["owner"][0]["who"] == "P3" and 50 < data["owner"][0]["age_s"] < 70
    assert "- P1 finished fail" in md
    assert "risk: the suite is flaky on a loaded box" in md
    # P1 failed, so P2 waits behind it; P3 is on the owner.
    by_root = {b["phase"]: b["blocks"] for b in data["starvation"]["blockers"]}
    assert by_root == {"P1": 1}
    assert "P1 [failed] blocks 1: P2" in md


def test_the_digest_carries_every_operator_outcome_since_the_last_pass(cfg):
    """An outcome with nothing for the owner is not sent: the digest is how it reaches them."""
    from swarm_orchestrator import opqueue

    now = time.time()
    for job, outcome, attention, at in (
        ("read-W97", "already done: image on 2026-01-01.1", False, now - 600),
        ("api-F26", "NOT rolled; owed: roll api-F18 first", True, now - 300),
        ("old-job", "done long ago", False, now - 7200),
    ):
        opqueue._write(cfg, opqueue.Item(phase=job, state=opqueue.DONE, outcome=outcome,
                                         attention=attention, done_at=at, queued_at=at))
    opqueue._write(cfg, opqueue.Item(phase="still-open", note="roll it", queued_at=now))

    data = ovdigest.build(cfg, state_mod.read(cfg), [], since=now - 3600)
    md = ovdigest.render(data)

    assert [o["job"] for o in data["operator"]["finished"]] == ["api-F26", "read-W97"]
    assert "## Operator jobs finished since" in md and "(2, 1 asked the owner)" in md
    assert "- **[asked the owner]** api-F26: NOT rolled; owed: roll api-F18 first" in md
    assert "- read-W97: already done: image on 2026-01-01.1" in md
    assert "old-job" not in md


def test_the_digest_writes_a_markdown_and_a_json_twin(cfg):
    data = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0)
    md = ovdigest.write(cfg, "20260923T100000Z", data)
    assert md.name == "digest-20260923T100000Z.md" and md.is_file()
    twin = json.loads(md.with_suffix(".json").read_text())
    assert twin["context"]["free_slots"] == [0, 1]


def _hold_seat(cfg, seat: int, **rec):
    """A build alive on the machine's gate: its seat record, and the lock a
    build keeps on it. Returns the descriptor that holds the lock."""
    cfg.buildsem_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.buildsem_dir / f"seat{seat}"
    path.write_text(json.dumps({"v": 1, "seat": seat, "slot": seat, "ended": None,
                                "pid": os.getpid(), "start_ts": time.time() - 600, **rec}))
    fd = os.open(path, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_the_digest_says_whose_builds_hold_the_gate_and_who_else_is_on_the_box(cfg, tmp_path):
    """The box and its build gate are shared. The digest names the other swarms
    on the machine and says, for each build on the gate, whose it is."""
    machine_toml(build={"max_concurrent": 2})
    far = cfg.state_dir.parent / "glasheim-1a2b"  # another swarm's state dir, beside this one
    far.mkdir()
    (tmp_path / "glasheim").mkdir()
    (far / "config.json").write_text(json.dumps(
        {"name": "glasheim", "project_dir": str(tmp_path / "glasheim")}))
    held = [
        _hold_seat(cfg, 0, id="own", swarm=cfg.state_dir.name, swarm_name=cfg.name,
                   phase="P1", argv="cargo build"),
        _hold_seat(cfg, 1, id="far", swarm=far.name, swarm_name="glasheim", phase="W7",
                   argv="cargo nextest run"),
    ]
    try:
        data = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0)
    finally:
        for fd in held:
            os.close(fd)
    r = data["resources"]
    assert r["swarms"] == [{"slug": far.name, "name": "glasheim", "status": "stopped"}]
    assert r["gate"]["max_concurrent"] == 2
    assert {(h["phase"], h["swarm_name"], h["mine"]) for h in r["gate"]["holders"]} == {
        ("P1", cfg.name, True), ("W7", "glasheim", False)}
    md = ovdigest.render(data)
    section = md.split("## Resources")[1]
    assert ("- build gate (the machine's, shared by every swarm on it): 2 build(s) on it,"
            " 1 of them another swarm's; limit 2 at once; 0 waiting, 0 of them this swarm's"
            ) in section
    assert "  - this swarm, P1, 10m: cargo build" in section
    assert "  - another swarm [glasheim], W7, 10m: cargo nextest run" in section
    assert "theirs included): glasheim (stopped)" in section


def test_the_digest_counts_the_waiting_builds_that_are_this_swarms(cfg, monkeypatch):
    waiting = [{"id": i, "mine": mine} for i, mine in enumerate((True, False, False))]
    holder = {"swarm": "glas-1", "swarm_name": "glasheim", "mine": False, "phase": "W7",
              "state": "yielded", "frozen": True, "running_s": 7200.0, "argv": "bash wait.sh"}
    nameless = dict(holder, swarm=None, swarm_name=None, state="active", frozen=False)
    monkeypatch.setattr(ovdigest.buildstatus, "snapshot", lambda _cfg, n_recent=1: {
        "max_concurrent": 1, "builds": [holder, nameless], "queue": waiting})
    r = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0)["resources"]
    assert (r["gate"]["queued"], r["gate"]["queued_mine"]) == (3, 1)
    lines = ovdigest._gate_lines(r["gate"])
    assert "1 of them another swarm's" in lines[0]
    assert lines[0].endswith("3 waiting, 1 of them this swarm's")
    assert lines[1] == ("  - another swarm [glasheim], W7, 2.0h: bash wait.sh; set aside as idle,"
                        " does not count against the limit; frozen with its swarm")
    assert lines[2].startswith("  - a swarm its record does not name, W7")
    assert r["swarms"] == [] and "theirs included): none" in ovdigest.render(
        ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0))


def test_a_gate_that_cannot_be_read_never_fails_the_digest(cfg, monkeypatch):
    def broken(_cfg, n_recent=1):
        raise OSError("the gate's files are gone")
    monkeypatch.setattr(ovdigest.buildstatus, "snapshot", broken)
    data = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0)
    assert data["resources"]["gate"] is None
    assert ("- build gate (the machine's, shared by every swarm on it): could not be read"
            in ovdigest.render(data))
    assert data["resources"]["mem_total"]  # the rest of the section is still there


def test_a_gate_that_is_off_is_said_to_be_off(cfg):
    machine_toml(build={"max_concurrent": 0})
    md = ovdigest.render(ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0))
    assert "shared by every swarm on it): off, builds do not queue" in md


def test_resources_flag_what_is_dangerous(cfg, tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16000000 kB\nMemAvailable:    1000000 kB\n"
        "SwapTotal:       4000000 kB\nSwapFree:          200000 kB\n"
    )
    r = ovdigest.resources(cfg, meminfo)
    assert r["mem_available"] == 1000000 * 1024
    assert any(f.startswith("RAM low") for f in r["flags"])
    assert "swap 95% used" in r["flags"]
    assert r["state_fs"]["free"] > 0


# -- the supervisor's side -------------------------------------------------------
@pytest.fixture
def sup(cfg, monkeypatch):
    s = Supervisor(cfg)
    s._bootstrapped = True
    s.spawned = []
    s.lines = []

    def fake_spawn(kind, pane=None, *, cwd=None, env=None, line=None):
        s.spawned.append((kind, cwd, env))
        s.lines.append(line)
        return True

    monkeypatch.setattr(s.master, "spawn", fake_spawn)
    monkeypatch.setattr(s.master, "is_alive", lambda: False)
    monkeypatch.setattr(s.master, "kill", lambda: None)
    # The spawn thread is run inline by the test (`_start`), never in the background.
    s.spawn_args = []
    monkeypatch.setattr(s, "_start_overseer_spawn", lambda *a: s.spawn_args.append(a))
    s._doctor_probed = time.time()  # the probe has its own test
    s._box_probed = time.time()  # and so has the look at the box
    yield s
    s.log.close()


def _start(sup) -> str:
    sup.overseer.request(ov.MANUAL, "by hand", urgent=True)
    sup._overseer_tick()
    pid = sup._overseer_live
    assert pid is not None and sup.spawn_args[-1][0] == pid
    # the spawn thread reports through the FIFO; nobody reads it here, so run it inline
    sup._overseer_spawn(*sup.spawn_args[-1])
    sup._on_overseer_spawned(pid, "ok")
    return pid


def test_a_pass_gets_its_digest_record_env_and_state(sup, cfg):
    pid = _start(sup)
    [(kind, cwd, env)] = sup.spawned
    assert kind == "overseer" and cwd is None  # isolation none: the project itself
    assert env["SWARM_OVERSEER_PASS"] == pid and env["SWARM_MASTER_KIND"] == "overseer"
    assert os.path.isfile(env["SWARM_OVERSEER_DIGEST"])
    assert env["SWARM_OVERSEER_RECORD"] == str(ovrecord.md_path(cfg, pid))
    st = state_mod.read(cfg)
    assert st.overseer_pass == pid and st.master_alive
    assert st.overseer_deadline > time.time() + 2600
    log = cfg.supervisor_log.read_text()
    assert f"OVERSEER-PASS-START {pid} reasons=manual" in log


def test_the_pane_gets_one_short_line_and_the_brief_goes_to_a_file(sup, cfg):
    """An Overseer pass once died as "prompt would not submit".
    Its brief was a long block of state paths typed into the pane, and Claude
    Code folds pasted text that long into "[Pasted text #1]", so the submit check
    never saw it land — the failure operator hand-offs had before their brief
    moved to a file. The pane gets a short pointer; the brief is in the file."""
    pid = _start(sup)
    [line] = sup.lines
    brief = ovrecord.overseer_dir(cfg) / f"{pid}.brief.md"
    assert len(line) < 300 and str(brief) in line and "\n" not in line
    text = brief.read_text(encoding="utf-8")
    assert "overseer.md" in text and f"pass {pid}" in text and "overseer-done" in text
    assert str(ovrecord.md_path(cfg, pid)) in text
    assert [r.id for r in ovrecord.load_passes(cfg)] == [pid]  # a .md is never a record


def _held(cfg, phase: str, kind: str = "dirty", resolver: bool = False) -> None:
    with state_mod.transaction(cfg) as st:
        st.integ_blocked, st.integ_blocked_kind = phase, kind
        if resolver:
            st.windows[f"resolve:{phase}"] = "@7"


def test_no_second_pass_while_one_runs_and_the_pending_one_runs_after(sup, cfg):
    pid = _start(sup)
    _held(cfg, "P1")
    sup.overseer.request("hold:P1", "held", urgent=True)
    sup._overseer_tick()
    assert sup._overseer_live == pid and len(sup.spawned) == 1
    sup._end_overseer_pass(pid, ovrecord.DONE)
    assert state_mod.read(cfg).overseer_pass is None
    sup._overseer_tick()
    assert sup._overseer_live not in (None, pid)  # the coalesced reason got its pass
    rec = ovrecord.load_json(cfg, sup._overseer_live)
    assert [r["key"] for r in rec.reasons] == ["hold:P1"]


def test_nothing_starts_before_the_init_pass_or_while_it_has_the_pane(sup, monkeypatch):
    sup._bootstrapped = False
    sup.overseer.request(ov.MANUAL, "by hand", urgent=True)
    sup._overseer_tick()
    assert sup._overseer_live is None
    sup._bootstrapped = True
    monkeypatch.setattr(sup.master, "is_alive", lambda: True)  # the init master
    sup._overseer_tick()
    assert sup._overseer_live is None
    monkeypatch.setattr(sup.master, "is_alive", lambda: False)
    sup._overseer_tick()
    assert sup._overseer_live is not None


def test_a_hung_pass_is_killed_at_its_deadline(sup, cfg, tmp_path):
    pid = _start(sup)
    with state_mod.transaction(cfg) as st:
        st.overseer_deadline = time.time() - 1
    sup._overseer_tick()
    assert sup._overseer_live is None
    assert ovrecord.load_json(cfg, pid).status == ovrecord.TIMEOUT
    assert f"OVERSEER-TIMEOUT {pid}" in cfg.supervisor_log.read_text()
    # One long pass is not the owner's problem: logged, not sent.
    assert "limit and was stopped" not in _tg(tmp_path)
    [row] = [r for r in _ledger(cfg) if r["kind"] == "overseer"]
    assert "limit and was stopped" in row["text"] and row["suppressed"]
    assert state_mod.read(cfg).overseer_pass is None


def test_three_bad_passes_in_a_row_ask_and_a_good_one_resets_the_streak(sup, cfg, tmp_path):
    def hang():
        pid = _start(sup)
        with state_mod.transaction(cfg) as st:
            st.overseer_deadline = time.time() - 1
        sup._overseer_tick()

    ask = "Asks you: Check the overseer window: the Overseer has failed 3 times in a row"
    hang()
    hang()
    assert _tg(tmp_path) == ""
    assert [(r["class"], "limit and was stopped" in r["text"]) for r in _ledger(cfg)] == [
        ("logged", True)] * 2
    hang()
    assert _tg(tmp_path).count(ask) == 1
    sup._end_overseer_pass(_start(sup), ovrecord.DONE)
    hang()
    assert _tg(tmp_path).count("Asks you:") == 1


def test_asking_the_owner_stretches_the_deadline_and_answering_resets_it(sup, cfg, monkeypatch, tmp_path):
    pid = _start(sup)
    monkeypatch.setenv("SWARM_OVERSEER_PASS", pid)
    assert cli_main(["--project-dir", str(cfg.project_dir), "waiting", "overseer",
                     "drop", "the", "look", "campaign?"]) == 0
    assert state_mod.read(cfg).overseer_deadline > time.time() + 6 * 24 * 3600
    assert _tg(tmp_path) == "[project] Asks you: drop the look campaign?\n"
    assert state_mod.read(cfg).waiting == {}  # no FIFO reader here: the poke is lost
    sup._overseer_tick()
    assert sup._overseer_live == pid  # a pass waiting on a person is not hung
    assert cli_main(["--project-dir", str(cfg.project_dir), "resumed", "overseer", "keep", "it"]) == 0
    rec = ovrecord.load_json(cfg, pid)
    assert (rec.question, rec.answer) == ("drop the look campaign?", "keep it")
    [owner] = notes_mod.load(cfg, notes_mod.OVERSEER)
    assert owner.kind == notes_mod.OWNER_DECISION
    assert owner.text == "keep it (asked: drop the look campaign?)"
    assert state_mod.read(cfg).overseer_deadline < time.time() + 2800


def test_a_pass_left_waiting_is_parked_and_the_next_pass_may_run(sup, cfg, monkeypatch):
    """Parked like a worker: the pass leaves the master pane, alive, and keeps the
    run from finishing; a later pass can use the pane; its own end closes it."""
    pid = _start(sup)
    monkeypatch.setenv("SWARM_OVERSEER_PASS", pid)
    assert cli_main(["--project-dir", str(cfg.project_dir), "waiting", "overseer", "ship?"]) == 0
    key = f"overseer:{pid}"
    sup._on_waiting(key)
    with state_mod.transaction(cfg) as st:
        st.waiting[key] = time.time() - 1
    sup._check_park_deadlines()

    st = state_mod.read(cfg)
    assert st.parked == [key] and st.overseer_pass is None and not st.master_alive
    assert sup._overseer_live is None and st.pending()
    assert st.live_passes() == {pid}
    [rec] = ovrecord.load_passes(cfg, live=st.live_passes())
    assert rec.status == ovrecord.RUNNING and rec.question == "ship?"
    assert f"PARK {key} window=wait:overseer-{pid}" in cfg.supervisor_log.read_text()

    sup.overseer.request(ov.MANUAL, "again", urgent=True)
    sup._overseer_tick()
    assert sup._overseer_live not in (None, pid)  # the pane is free for the next pass

    assert cli_main(["--project-dir", str(cfg.project_dir), "resumed", "overseer", "yes"]) == 0
    assert cli_main(["--project-dir", str(cfg.project_dir), "overseer-done", "shipped"]) == 0
    sup._end_overseer_pass(pid, ovrecord.DONE)
    st = state_mod.read(cfg)
    assert st.parked == [] and ovrecord.load_json(cfg, pid).status == ovrecord.DONE
    assert f"UNPARK {key}" in cfg.supervisor_log.read_text()


def test_a_pass_that_will_not_start_gives_its_reasons_back(sup, cfg, tmp_path):
    _held(cfg, "P1")
    sup.overseer.request("hold:P1", "held", urgent=True)
    sup._overseer_tick()
    pid = sup._overseer_live
    sup._on_overseer_spawned(pid, "failed")
    assert sup._overseer_live is None
    assert ovrecord.load_json(cfg, pid).status == ovrecord.FAILED
    assert [(r.key, r.urgent) for r in sup.overseer.pending] == [("hold:P1", False)]
    assert "would not start" not in _tg(tmp_path)  # its reasons wait for the next one
    assert any("would not start" in r["text"] for r in _ledger(cfg))


def test_a_conflict_the_resolver_is_on_gets_a_pass_only_once_it_gives_up(sup, cfg):
    _held(cfg, "P1", "conflict", resolver=True)
    sup._overseer_tick()
    assert sup._overseer_live is None
    sup._handle("resolver-escalated P1")
    sup._overseer_tick()
    rec = ovrecord.load_json(cfg, sup._overseer_live)
    assert [r["key"] for r in rec.reasons] == ["hold:P1"]
    assert "RESOLVER-ESCALATED P1" in cfg.supervisor_log.read_text()


def test_a_hold_reason_whose_hold_cleared_starts_no_pass(sup, cfg):
    sup.overseer.request("hold:P1", "held", urgent=True)  # queued; then the resolver finished
    sup._overseer_tick()
    assert sup._overseer_live is None and sup.overseer.pending == []
    assert "OVERSEER-TRIGGER-DROPPED hold:P1" in cfg.supervisor_log.read_text()


def test_a_live_or_owed_pass_holds_the_finish(sup, cfg):
    with state_mod.transaction(cfg) as st:
        for p in ("P0", "P1", "P2", "P3"):
            st.mark_done(p, "ok")
    sup.overseer.observe(state_mod.read(cfg))  # baseline
    pid = _start(sup)
    sup._finish_if_settled()
    assert not state_mod.read(cfg).finished  # live pass
    sup.overseer.request("fail:P9", "P9 failed")  # owed, and inside the gap
    sup._end_overseer_pass(pid, ovrecord.DONE)
    assert not state_mod.read(cfg).finished
    assert "FINISH-HELD overseer pending=['fail:P9']" in cfg.supervisor_log.read_text()
    sup.overseer.mem.pending = []
    sup._finish_if_settled()
    assert state_mod.read(cfg).finished


def test_a_summary_that_is_due_does_not_hold_the_finish(sup, cfg):
    """The finish message says more than a summary would."""
    with state_mod.transaction(cfg) as st:
        for p in ("P0", "P1", "P2", "P3"):
            st.mark_done(p, "ok")
    sup.overseer.mem.anchor = time.time() - cfg.overseer_every_s - 1
    sup._finish_if_settled()
    assert [r.key for r in sup.overseer.pending] == ["summary"]
    assert state_mod.read(cfg).finished


def test_the_policy_is_told_what_the_box_is_short_of_once_a_minute(sup, cfg, monkeypatch):
    looks = []
    monkeypatch.setattr(ovdigest, "resources",
                        lambda _cfg: looks.append(1) or {"flags": ["swap 91% used"]})
    sup._box_probed = 0.0
    sup._overseer_tick()
    sup._overseer_tick()
    assert len(looks) == 1 and sup.overseer.mem.box_since
    assert sup.overseer.pending == []  # short for a moment is not a reason
    sup.overseer.mem.box_since -= ov.BOX_S
    sup._box_probed = 0.0
    sup._overseer_tick()
    assert "OVERSEER-TRIGGER box — the box is short: swap 91% used" in cfg.supervisor_log.read_text()


def test_the_last_failure_of_a_run_still_gets_its_pass(sup, cfg):
    """The event that settles the run is the trigger: it is seen before the finish."""
    with state_mod.transaction(cfg) as st:
        for p in ("P0", "P1", "P3"):
            st.mark_done(p, "ok")
    sup.overseer.observe(state_mod.read(cfg))  # baseline
    with state_mod.transaction(cfg) as st:
        st.mark_done("P2", "fail")
    sup._finish_if_settled()
    assert not state_mod.read(cfg).finished
    assert "fail:P2" in [r.key for r in sup.overseer.pending]


def test_a_starved_swarm_is_seen_as_such(sup, cfg):
    with state_mod.transaction(cfg) as st:
        st.mark_done("P0", "fail")  # everything else waits behind it
    assert sup._starving(state_mod.read(cfg)) is True
    with state_mod.transaction(cfg) as st:
        st.done.pop("P0")
    assert sup._starving(state_mod.read(cfg)) is False  # P0 is launchable


def test_the_doctor_probe_is_cheap_rate_limited_and_reports_stuck_state(sup, cfg):
    sup._doctor_probed = 0.0
    (cfg.project_dir / "ledger.txt").write_text("P0 needs:P1\nP1 needs:P0\n")
    fails = sup._doctor_probe(state_mod.read(cfg), time.time())
    assert set(fails) == {"ledger"}
    assert sup._doctor_probe(state_mod.read(cfg), time.time()) is None  # not due again


def test_the_doctor_probe_sees_a_lost_nudge_right_after_an_event(sup, cfg):
    """The probe runs when an event has just been handled. ``swarm doctor``
    waits out an event that fresh; the supervisor is past it and must not."""
    sup._doctor_probed = 0.0
    with state_mod.transaction(cfg) as st:
        st.bootstrapping = False  # the init pass is over
        st.last_event_at = time.time()
    sup.log.line("EVENT done PX ok freed_slot=0 parked=False")
    fails = sup._doctor_probe(state_mod.read(cfg), time.time())
    assert set(fails) == {"run.nudge"} and "ready ['P0']" in fails["run.nudge"]


def test_the_doctor_probe_leaves_out_a_phase_waiting_out_a_failed_launch(sup, cfg):
    with state_mod.transaction(cfg) as st:
        st.bootstrapping = False  # the init pass is over; only P0 is ready
    stale = state_mod.read(cfg)  # read before the launch failed, as a wake's is
    sup._launch_failed("P0", time.time() - 20)
    sup._doctor_probed = 0.0
    assert sup._doctor_probe(stale, time.time()) == {}


def test_the_doctor_probe_names_a_phase_given_up_on(sup, cfg):
    with state_mod.transaction(cfg) as st:
        st.bootstrapping = False
    with sup._launch_lock:
        sup._launch_fails["P0"] = (state_mod.LAUNCH_GIVE_UP, time.time() - 900)
    sup._doctor_probed = 0.0
    fails = sup._doctor_probe(state_mod.read(cfg), time.time())
    assert set(fails) == {"run.nudge"}
    assert "['P0'] given up on" in fails["run.nudge"] and "lost nudge" not in fails["run.nudge"]


def test_the_doctor_probe_leaves_out_rows_a_lane_holds_back(sup, cfg, monkeypatch):
    sup._doctor_probed = 0.0
    monkeypatch.setattr(cfg, "lanes_enabled", True)
    (cfg.project_dir / "frontend" / ".git").mkdir(parents=True)
    (cfg.project_dir / "ledger.txt").write_text(
        "- [ ] `ui-W1` · dir:`frontend` · needs:— · touches:`frontend/src/**` · **broad**\n"
        "- [ ] `ui-W2` · dir:`frontend` · needs:— · touches:`frontend/src/a.ts` · **inside**\n",
        encoding="utf-8",
    )
    with state_mod.transaction(cfg) as st:
        st.bootstrapping = False
        st.slots[0].busy, st.slots[0].phase = True, "ui-W1"
    assert sup._doctor_probe(state_mod.read(cfg), time.time()) == {}


def test_disabled_the_supervisor_never_starts_a_pass(sup, cfg):
    cfg.overseer_enabled = False
    sup.overseer.request(ov.MANUAL, "by hand", urgent=True)
    sup._overseer_tick()
    assert sup._overseer_live is None and sup.spawned == []


# -- the CLI ---------------------------------------------------------------------
def test_overseer_lists_passes_and_pending_reasons(cfg, capsys):
    ovrecord.create(cfg, "20260923T100000Z", [{"key": "every", "text": "no pass for 3.0h"}], None)
    ovrecord.update(cfg, "20260923T100000Z", status="done", ended_at=time.time(), summary="all quiet")
    ov.Policy(cfg).request("fail:P1", "P1 finished fail")
    assert cli_main(["--project-dir", str(cfg.project_dir), "overseer"]) == 0
    out = capsys.readouterr().out
    assert "pending: P1 finished fail" in out
    assert "20260923T100000Z [done" in out and "(every) — all quiet" in out
    assert cli_main(["--project-dir", str(cfg.project_dir), "overseer", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["passes"][0]["summary"] == "all quiet" and data["pending"][0]["key"] == "fail:P1"


def test_overseer_now_needs_it_enabled_and_a_supervisor(cfg, capsys, monkeypatch):
    assert cli_main(["--project-dir", str(cfg.project_dir), "overseer", "--now"]) == 1  # nobody reading
    monkeypatch.setenv("SWARM_OVERSEER", "0")
    assert cli_main(["--project-dir", str(cfg.project_dir), "overseer", "--now"]) == 2


def test_overseer_done_without_a_pass_is_refused(cfg, capsys):
    assert cli_main(["--project-dir", str(cfg.project_dir), "overseer-done", "nothing"]) == 1


def test_overseer_done_records_before_it_pokes(cfg, capsys):
    ovrecord.create(cfg, "20260923T100000Z", [], None)
    with state_mod.transaction(cfg) as st:
        st.overseer_pass = "20260923T100000Z"
    assert cli_main(["--project-dir", str(cfg.project_dir), "overseer-done", "retried", "P1"]) == 0
    rec = ovrecord.load(cfg, "20260923T100000Z", live="20260923T100000Z")
    assert (rec.status, rec.summary) == ("done", "retried P1")
    assert "## Summary\nretried P1" in ovrecord.md_path(cfg, "20260923T100000Z").read_text()


def test_a_timed_out_session_signing_off_late_is_only_recorded(cfg, monkeypatch, capsys):
    ovrecord.create(cfg, "20260923T100000Z", [], None)
    ovrecord.update(cfg, "20260923T100000Z", status="timeout", ended_at=1.0)
    monkeypatch.setenv("SWARM_OVERSEER_PASS", "20260923T100000Z")
    assert cli_main(["--project-dir", str(cfg.project_dir), "overseer-done", "late"]) == 1
    rec = ovrecord.load_json(cfg, "20260923T100000Z")
    assert (rec.status, rec.summary) == ("timeout", "late")


# -- end to end: the demo fixture, fake-master.sh as the Overseer -----------------
def _toml(swarm, extra: str) -> None:
    path = swarm.project / ".swarm.toml"
    path.write_text(path.read_text() + extra, encoding="utf-8")


def _passes(swarm) -> list[dict]:
    d = swarm.state_dir / "overseer"
    return [
        json.loads(p.read_text())
        for p in sorted(d.glob("2*.json"))
    ] if d.is_dir() else []


def _phone(swarm) -> list[str]:
    return swarm.tg_sink.read_text().splitlines() if swarm.tg_sink.is_file() else []


def test_e2e_the_summary_clock_runs_a_pass_that_signs_off(swarm):
    swarm.env["SWARM_OVERSEER"] = "1"
    swarm.env["FAKE_OVERSEER_SUMMARY"] = "Two phases landed and three are building. Nothing waits on you."
    _toml(swarm, "\n[overseer]\nevery_s = 1\n")
    swarm.up()
    assert swarm.wait(lambda: "ACTION finish" in swarm.log_text(), timeout=60), swarm.log_text()

    log = swarm.log_text()
    assert "OVERSEER-TRIGGER summary" in log
    assert "OVERSEER-PASS-START" in log and "OVERSEER-PASS-END" in log
    [rec] = [p for p in _passes(swarm) if p["reasons"][0]["key"] == "summary"]
    assert rec["status"] == "done"
    assert rec["summary"].startswith("fake overseer pass: read the digest")
    digest = rec["digest"]
    assert os.path.isfile(digest) and "the owner's summary is due" in open(digest).read()
    md = open(rec["record"]).read()
    assert "nothing to do (fake overseer)" in md
    assert log.count("ACTION finish") == 1
    out = swarm.cli("overseer").stdout
    assert rec["id"] in out and "[done" in out
    # The owner's phone: the summary the pass wrote, then the run's last one.
    name = swarm.project.name
    assert _phone(swarm) == [
        f"[{name}] Overseer: Two phases landed and three are building. Nothing waits on you.",
        f"[{name}] Overseer: The run has finished: 5 phase(s) landed. Nothing waits on you.",
    ]
    assert rec["owner_summary"].startswith("Two phases landed")


def test_e2e_a_hung_pass_is_killed_and_the_run_still_finishes(swarm):
    swarm.env["SWARM_OVERSEER"] = "1"
    swarm.env["FAKE_OVERSEER_HANG"] = "1"
    _toml(swarm, "\n[overseer]\nevery_s = 1\ntimeout_s = 2\n")
    swarm.up()
    assert swarm.wait(lambda: "ACTION finish" in swarm.log_text(), timeout=60), swarm.log_text()
    log = swarm.log_text()
    assert "OVERSEER-TIMEOUT" in log
    assert any(p["status"] == "timeout" for p in _passes(swarm))
    # The pass never wrote the summary it was started for: the swarm sent its own.
    assert "SUMMARY-OWN supervisor._summary_owed" in log
    own, last = _phone(swarm)
    assert own.startswith(f"[{swarm.project.name}] Overseer: Since ") and "building now" in own
    assert "The run has finished" in last
    st = swarm.state()
    assert set(st["done"]) >= {"P0", "P1", "P2", "P3", "P4"}
    assert st.get("overseer_pass") is None


def test_e2e_overseer_now_runs_a_pass_mid_run(swarm):
    swarm.env["SWARM_OVERSEER"] = "1"
    swarm.env["FAKE_WORKER_SLEEP"] = "4"
    _toml(swarm, "\n[overseer]\nevery_s = 0\n")
    swarm.up()
    assert swarm.wait(lambda: "EVENT master-idle" in swarm.log_text(), timeout=20), swarm.log_text()
    assert swarm.cli("overseer", "--now").returncode == 0
    assert swarm.wait(lambda: "OVERSEER-PASS-END" in swarm.log_text(), timeout=30), swarm.log_text()
    [rec] = _passes(swarm)
    assert rec["reasons"][0]["key"] == "manual" and rec["status"] == "done"
    assert swarm.wait(lambda: "ACTION finish" in swarm.log_text(), timeout=60), swarm.log_text()
