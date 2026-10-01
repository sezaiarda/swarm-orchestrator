"""Overseer passes: the record, the digest, the supervisor's side, the CLI.

The supervisor-side tests build a real :class:`Supervisor` in-process on the bare
driver with the session spawn stubbed, so every step of a pass (reserve, spawn,
end, timeout, finish hold) is driven by hand and nothing ``claude`` ever starts.
The end-to-end tests at the bottom run the real supervisor process against the
demo fixture with ``fake-master.sh`` playing the Overseer.
"""

from __future__ import annotations

import json
import os
import time

import pytest

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
    """Routine outcomes no longer ping, so the digest is how they reach the owner."""
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
    assert "## Operator jobs finished since" in md and "(2, 1 flagged)" in md
    assert "- **[needs the owner]** api-F26: NOT rolled; owed: roll api-F18 first" in md
    assert "- read-W97: already done: image on 2026-01-01.1" in md
    assert "old-job" not in md


def test_the_digest_writes_a_markdown_and_a_json_twin(cfg):
    data = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0)
    md = ovdigest.write(cfg, "20260923T100000Z", data)
    assert md.name == "digest-20260923T100000Z.md" and md.is_file()
    twin = json.loads(md.with_suffix(".json").read_text())
    assert twin["context"]["free_slots"] == [0, 1]


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


def test_three_bad_passes_in_a_row_ping_and_a_good_one_resets_the_streak(sup, cfg, tmp_path):
    def hang():
        pid = _start(sup)
        with state_mod.transaction(cfg) as st:
            st.overseer_deadline = time.time() - 1
        sup._overseer_tick()

    hang()
    hang()
    assert "limit and was stopped" not in _tg(tmp_path)
    hang()
    assert _tg(tmp_path).count("limit and was stopped") == 1
    sup._end_overseer_pass(_start(sup), ovrecord.DONE)
    hang()
    assert _tg(tmp_path).count("limit and was stopped") == 1


def test_asking_the_owner_stretches_the_deadline_and_answering_resets_it(sup, cfg, monkeypatch, tmp_path):
    pid = _start(sup)
    monkeypatch.setenv("SWARM_OVERSEER_PASS", pid)
    assert cli_main(["--project-dir", str(cfg.project_dir), "waiting", "overseer",
                     "drop", "the", "look", "campaign?"]) == 0
    assert state_mod.read(cfg).overseer_deadline > time.time() + 6 * 24 * 3600
    assert "the Overseer is waiting on you — drop the look campaign?" in _tg(tmp_path)
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


def test_e2e_every_three_finished_runs_a_pass_that_signs_off(swarm):
    swarm.env["SWARM_OVERSEER"] = "1"
    _toml(swarm, "\n[overseer]\nevery_finished = 3\nevery_s = 0\n")
    swarm.up()
    assert swarm.wait(lambda: "ACTION finish" in swarm.log_text(), timeout=60), swarm.log_text()

    log = swarm.log_text()
    assert "OVERSEER-TRIGGER finished" in log
    assert "OVERSEER-PASS-START" in log and "OVERSEER-PASS-END" in log
    [rec] = [p for p in _passes(swarm) if p["reasons"][0]["key"] == "finished"]
    assert rec["status"] == "done"
    assert rec["summary"].startswith("fake overseer pass: read the digest")
    digest = rec["digest"]
    assert os.path.isfile(digest) and "phase(s) finished since the last pass" in open(digest).read()
    md = open(rec["record"]).read()
    assert "nothing to do (fake overseer)" in md
    assert log.count("ACTION finish") == 1
    out = swarm.cli("overseer").stdout
    assert rec["id"] in out and "[done" in out


def test_e2e_a_hung_pass_is_killed_and_the_run_still_finishes(swarm):
    swarm.env["SWARM_OVERSEER"] = "1"
    swarm.env["FAKE_OVERSEER_HANG"] = "1"
    _toml(swarm, "\n[overseer]\nevery_finished = 1\nevery_s = 0\ntimeout_s = 2\nmin_gap_s = 0\n")
    swarm.up()
    assert swarm.wait(lambda: "ACTION finish" in swarm.log_text(), timeout=60), swarm.log_text()
    log = swarm.log_text()
    assert "OVERSEER-TIMEOUT" in log
    assert any(p["status"] == "timeout" for p in _passes(swarm))
    st = swarm.state()
    assert set(st["done"]) >= {"P0", "P1", "P2", "P3", "P4"}
    assert st.get("overseer_pass") is None


def test_e2e_overseer_now_runs_a_pass_mid_run(swarm):
    swarm.env["SWARM_OVERSEER"] = "1"
    swarm.env["FAKE_WORKER_SLEEP"] = "4"
    _toml(swarm, "\n[overseer]\nevery_finished = 0\nevery_s = 0\n")
    swarm.up()
    assert swarm.wait(lambda: "EVENT master-idle" in swarm.log_text(), timeout=20), swarm.log_text()
    assert swarm.cli("overseer", "--now").returncode == 0
    assert swarm.wait(lambda: "OVERSEER-PASS-END" in swarm.log_text(), timeout=30), swarm.log_text()
    [rec] = _passes(swarm)
    assert rec["reasons"][0]["key"] == "manual" and rec["status"] == "done"
    assert swarm.wait(lambda: "ACTION finish" in swarm.log_text(), timeout=60), swarm.log_text()
