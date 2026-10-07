"""Batch claims at run time: one session builds several rows, each reported,
recorded and attributed on its own, and the batch's branch lands once.

On real git, through the supervisor's own handlers (as ``test_later.py``): a
batch is claimed the way ``launch_outcome`` claims it, rows report the way
``swarm done`` reports, and a dead session is reaped the way the watchdog
reaps it. The rows of the batch are ``k-W1`` (the seed), ``k-W2`` and ``k-W3``.
The selection itself (which rows ride) is ``test_batcher.py``'s.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from swarm_orchestrator import batcher, cli, gitq
from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import master as master_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.supervisor import Supervisor

LEDGER = """# Ledger

- [x] `k-W0` · needs:— · **the base**
- [ ] `k-W1` · needs:`k-W0` · **one**
- [ ] `k-W2` · needs:`k-W0` · **two**
- [ ] `k-W3` · needs:`k-W2` · **three, after two**
- [ ] `z-W1` · needs:— · **unrelated**
"""
LEDGER_FILE = "docs/PHASE-LEDGER.md"
ROWS = ["k-W1", "k-W2", "k-W3"]
ATTIC = "refs/swarm-attic"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def ws(tmp_path, monkeypatch):
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", "-b", "master", str(project)], check=True)
    for k, v in (("user.email", "swarm@test"), ("user.name", "swarm"), ("commit.gpgsign", "false")):
        _git(project, "config", k, v)
    (project / "docs").mkdir()
    (project / LEDGER_FILE).write_text(LEDGER)
    (project / ".swarm.toml").write_text(
        f'[swarm]\ndriver = "bare"\n[tasks]\nledger = "{LEDGER_FILE}"\n')
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_SLUG", "batch")
    monkeypatch.setenv("SWARM_BATCHING", "1")
    monkeypatch.setenv("SWARM_WORKER_CMD", "true")
    for leak in ("SWARM_SESSION_ID", "SWARM_PHASE", "SWARM_DRIVER"):
        monkeypatch.delenv(leak, raising=False)
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)
    monkeypatch.setattr(sup, "_fill_slots", lambda *a, **k: [])
    yield cfg, project, sup
    sup.log.close()


def _claim(cfg, sup, rows=ROWS) -> Path:
    """The slot, the batch and the seed's mirror, as ``launch_outcome`` leaves them."""
    with state_mod.transaction(cfg) as st:
        st.claim_slot(rows[0])
        st.batches[rows[0]] = list(rows)
    return gitq.worktree_add(cfg, rows[0], sup.log)


def _build(wt: Path, row: str) -> None:
    """A builder's row, reviewed and committed by the lead: one commit per row."""
    (wt / f"{row}.txt").write_text(f"{row}\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"{row}: built")


def _report(cfg, sup, row: str, status: str = "ok") -> None:
    """``swarm done <row> <status>`` from the lead, and the supervisor reading it."""
    result = launch_mod.done(cfg, row, status, f"{row} recap: what was done and where")
    sup._on_done(row, result.status)  # the poke carries the canonical status


def _main_files(project: Path) -> set[str]:
    return set(_git(project, "ls-tree", "-r", "--name-only", "master").split())


# -- one landing, each row its own record ---------------------------------------
def test_rows_report_one_by_one_and_the_batch_lands_once(ws):
    cfg, project, sup = ws
    wt = _claim(cfg, sup)
    for row in ROWS[:2]:
        _build(wt, row)
        _report(cfg, sup, row)
        st = state_mod.read(cfg)
        # Nothing is recorded or merged yet, and the session goes on in its slot.
        assert row not in st.done and st.batch_done[row] == "ok"
        assert [s.phase for s in st.busy_slots()] == ["k-W1"] and wt.is_dir()
    assert "k-W1.txt" not in _main_files(project)

    _build(wt, "k-W3")
    _report(cfg, sup, "k-W3")
    st = state_mod.read(cfg)
    assert {r: st.done.get(r) for r in ROWS} == {r: "ok" for r in ROWS}
    assert not st.batches and not st.batch_done and not st.busy_slots()
    assert {f"{r}.txt" for r in ROWS} <= _main_files(project)
    # One commit per row on main, attributable to its row.
    subjects = _git(project, "log", "--format=%s", "master")
    assert all(f"{r}: built" in subjects for r in ROWS)
    log = cfg.supervisor_log.read_text()
    assert log.count("BATCH-CLOSED k-W1") == 1
    for row in ROWS:
        assert f"EVENT done {row} ok" in log


def test_a_failed_row_does_not_drag_the_others(ws):
    cfg, project, sup = ws
    wt = _claim(cfg, sup)
    _build(wt, "k-W1")
    _report(cfg, sup, "k-W1")
    (wt / "half.txt").write_text("k-W2 did not work out\n")
    _git(wt, "stash", "push", "-u", "-m", "swarm: k-W2")  # as the lead is told to
    _report(cfg, sup, "k-W2", "fail")
    _build(wt, "k-W3")
    _report(cfg, sup, "k-W3")

    st = state_mod.read(cfg)
    assert {r: st.done.get(r) for r in ROWS} == {"k-W1": "ok", "k-W2": "fail", "k-W3": "ok"}
    files = _main_files(project)
    assert {"k-W1.txt", "k-W3.txt"} <= files and "half.txt" not in files


def test_a_batch_with_nothing_to_land_records_every_row_and_lands_nothing(ws):
    cfg, project, sup = ws
    wt = _claim(cfg, sup, ["k-W1", "k-W2"])
    _build(wt, "k-W1")
    _report(cfg, sup, "k-W1", "blocked")
    _report(cfg, sup, "k-W2", "fail")
    st = state_mod.read(cfg)
    assert st.done == {"k-W1": "fail", "k-W2": "fail"} and not st.busy_slots()
    assert "k-W1.txt" not in _main_files(project)
    assert _git(project, "for-each-ref", "--format=%(refname)", f"{ATTIC}/k-W1").strip()


def test_a_lead_reports_a_row_of_its_batch_but_no_other(ws, monkeypatch):
    cfg, _project, sup = ws
    _claim(cfg, sup)
    monkeypatch.setenv(cfg.env_marker, "k-W1")
    st = state_mod.read(cfg)
    assert cli._done_refusal(cfg, "k-W3", st) is None
    refused = cli._done_refusal(cfg, "z-W1", st)
    assert refused and "k-W1, k-W2, k-W3" in refused


def test_done_tells_the_lead_what_is_left(ws):
    cfg, _project, sup = ws
    _claim(cfg, sup, ["k-W1", "k-W2"])
    first = launch_mod.done(cfg, "k-W1", "ok", "k-W1 recap: built and tested")
    assert "goes on" in first.batch and "k-W2" in first.batch
    sup._on_done("k-W1", "ok")
    last = launch_mod.done(cfg, "k-W2", "ok", "k-W2 recap: built and tested")
    assert "last row" in last.batch
    assert "k-W2" in last.render()


# -- a session that dies ---------------------------------------------------------
def test_a_dead_batch_lands_what_it_reported_and_frees_the_rest(ws):
    cfg, project, sup = ws
    wt = _claim(cfg, sup)
    _build(wt, "k-W1")
    _report(cfg, sup, "k-W1")
    (wt / "wip.txt").write_text("k-W2, half made\n")  # never committed
    sup._reap("k-W1", "pane-dead")

    st = state_mod.read(cfg)
    assert st.done.get("k-W1") == "ok" and "k-W2" not in st.done and "k-W3" not in st.done
    assert not st.batches and not st.batch_rows() and not st.busy_slots()
    files = _main_files(project)
    assert "k-W1.txt" in files and "wip.txt" not in files
    # The half-made row is in the attic, not lost.
    attic = _git(project, "for-each-ref", "--format=%(refname)", ATTIC).split()
    assert attic and any("wip.txt" in _git(project, "show", "--stat", ref) for ref in attic)
    # The rows it never reported are ready again.
    assert "k-W2" in master_mod.build_context(cfg, st)["ready"]
    log = cfg.supervisor_log.read_text()
    assert "BATCH-DIED k-W1 released=k-W2 k-W3" in log
    assert "RUN-ENDED k-W2 reason=released" in log


def test_a_dead_batch_with_nothing_reported_keeps_the_seed_branch(ws):
    cfg, project, sup = ws
    wt = _claim(cfg, sup)
    (wt / "wip.txt").write_text("k-W1, half made\n")
    sup._reap("k-W1", "pane-dead")

    st = state_mod.read(cfg)
    assert not st.done and not st.batches and not st.busy_slots()
    ready = master_mod.build_context(cfg, st)["ready"]
    assert {"k-W1", "k-W2"} <= set(ready)
    # The seed resumes on its own branch, as a lone phase that died does.
    assert "wip.txt" in _git(project, "ls-tree", "-r", "--name-only", "swarm/k-W1")


# -- handing a row back ------------------------------------------------------------
def test_unbatch_frees_a_rider_at_once_and_the_batch_ends_without_it(ws, monkeypatch, capsys):
    cfg, project, sup = ws
    wt = _claim(cfg, sup)
    monkeypatch.setenv(cfg.env_marker, "k-W1")
    assert cli.cmd_unbatch(cfg, "k-W3", "k-W2 failed, and k-W3 builds on it") == 0
    st = state_mod.read(cfg)
    assert st.batches["k-W1"] == ["k-W1", "k-W2"] and not st.in_flight("k-W3")
    assert batcher._failed_before(cfg, "k-W3")  # it runs alone next time
    # Refused: twice, unknown, too short a reason.
    assert cli.cmd_unbatch(cfg, "k-W3", "k-W2 failed, and k-W3 builds on it") == 2
    assert cli.cmd_unbatch(cfg, "k-W2", "no") == 2

    _build(wt, "k-W1")
    _report(cfg, sup, "k-W1")
    _build(wt, "k-W2")
    _report(cfg, sup, "k-W2")
    st = state_mod.read(cfg)
    assert st.done == {"k-W1": "ok", "k-W2": "ok"} and not st.batches


def test_unbatching_the_seed_records_nothing_for_it(ws, monkeypatch):
    cfg, project, sup = ws
    wt = _claim(cfg, sup, ["k-W1", "k-W2"])
    monkeypatch.setenv(cfg.env_marker, "k-W1")
    assert cli.cmd_unbatch(cfg, "k-W1", "the row is far larger than it says") == 0
    _build(wt, "k-W2")
    _report(cfg, sup, "k-W2")
    st = state_mod.read(cfg)
    assert st.done == {"k-W2": "ok"} and not st.busy_slots() and not st.batches
    assert "k-W2.txt" in _main_files(project)
    assert "k-W1" in master_mod.build_context(cfg, st)["ready"]


def test_escalate_is_refused_in_a_batch(ws, monkeypatch, capsys):
    cfg, _project, sup = ws
    _claim(cfg, sup)
    monkeypatch.setenv(cfg.env_marker, "k-W1")
    assert cli.cmd_escalate(cfg, "k-W1", "a reason long enough to be read by the next one") == 2
    assert "swarm unbatch" in capsys.readouterr().err


# -- the claim ----------------------------------------------------------------------
def test_launch_claims_the_batch_and_briefs_the_lead(ws):
    cfg, _project, sup = ws
    assert launch_mod.launch_outcome(cfg, "k-W1", sup.log, quiet=True,
                                     batch=ROWS) == launch_mod.LAUNCHED
    st = state_mod.read(cfg)
    assert st.batches == {"k-W1": ROWS}
    assert {"k-W2", "k-W3"} <= st.batch_rows() and st.in_flight("k-W3")
    ready = master_mod.build_context(cfg, st)["ready"]
    assert "k-W2" not in ready and "z-W1" in ready
    log = cfg.supervisor_log.read_text()
    assert "CLAIM k-W1 slot=0" in log and "CLAIM k-W2 slot=0 batch=k-W1" in log

    shell = launch_mod._worker_shell(cfg, "k-W1", cfg.wt_dir / "k-W1")
    brief = cfg.state_dir / "briefs" / "k-W1.lead.md"
    assert f"--append-system-prompt-file {brief}" in shell
    text = brief.read_text()
    assert "`k-W1`, `k-W2`, `k-W3`" in text and 'model: "sonnet"' in text
    assert launch_mod._worker_env(cfg, "k-W1")["SWARM_BATCH"] == "k-W1 k-W2 k-W3"


def test_launch_drops_a_rider_taken_meanwhile_and_the_rows_that_need_it(ws):
    cfg, _project, sup = ws
    with state_mod.transaction(cfg) as st:
        st.launching = ["k-W2"]  # another launch thread is starting it
    assert launch_mod.launch_outcome(cfg, "k-W1", sup.log, quiet=True,
                                     batch=ROWS) == launch_mod.LAUNCHED
    assert state_mod.read(cfg).batches == {}  # k-W3 needs k-W2: alone is what is left


def test_a_failed_launch_frees_its_riders(ws, monkeypatch):
    cfg, _project, sup = ws
    monkeypatch.setattr(launch_mod, "_launch_bare", lambda *a, **k: False)
    assert launch_mod.launch_outcome(cfg, "k-W1", sup.log, quiet=True,
                                     batch=ROWS) == launch_mod.FAILED
    st = state_mod.read(cfg)
    assert not st.batches and not st.busy_slots()
    assert "RUN-ENDED k-W2 reason=launch-failed" in cfg.supervisor_log.read_text()


def test_batching_off_briefs_no_lead(ws, monkeypatch):
    cfg, _project, _sup = ws
    cfg.batch_enabled = False
    shell = launch_mod._worker_shell(cfg, "k-W1", cfg.project_dir)
    assert "--append-system-prompt" not in shell


# -- the state ----------------------------------------------------------------------
def test_batches_survive_a_round_trip_and_a_carried_restart(ws):
    cfg, _project, sup = ws
    _claim(cfg, sup)
    with state_mod.transaction(cfg) as st:
        st.batch_done["k-W1"] = "ok"
    st = state_mod.read(cfg)
    assert state_mod.State.from_dict(st.to_dict()).batches == {"k-W1": ROWS}
    assert "batches" not in state_mod.State.fresh(1).to_dict()  # nothing new when unused

    state_mod.init_state(cfg, carried={"k-W1"})
    st = state_mod.read(cfg)
    assert st.batches == {"k-W1": ROWS} and st.batch_done == {"k-W1": "ok"}
    state_mod.init_state(cfg)
    st = state_mod.read(cfg)
    assert not st.batches and not st.batch_done


def test_the_watchdog_ends_a_batch_whose_last_word_was_lost(ws, monkeypatch):
    cfg, project, sup = ws
    wt = _claim(cfg, sup, ["k-W1", "k-W2"])
    _build(wt, "k-W1")
    _report(cfg, sup, "k-W1")
    with state_mod.transaction(cfg) as st:
        st.batch_done["k-W2"] = batcher.RELEASED  # handed back, its poke lost
        st.last_event_at = time.time()
    sup._last_sweep = 0.0
    sup._watchdog_tick()
    st = state_mod.read(cfg)
    assert st.done == {"k-W1": "ok"} and not st.batches
    assert "k-W1.txt" in _main_files(project)


# -- end to end: a real supervisor and the fake lead ---------------------------
def test_a_real_supervisor_runs_a_batch_in_one_slot(swarm):
    (swarm.project / "ledger.md").write_text(
        "# Ledger\n\n"
        "- [ ] `k-W1` · needs:— · **one**\n"
        "- [ ] `k-W2` · needs:— · **two**\n"
        "- [ ] `k-W3` · needs:`k-W2` · **three**\n",
        encoding="utf-8",
    )
    toml = swarm.project / ".swarm.toml"
    toml.write_text(toml.read_text(encoding="utf-8")
                    .replace("max_workers = 4", "max_workers = 1")
                    .replace('ledger = "ledger.txt"', 'ledger = "ledger.md"')
                    + '\n[batch]\nmodel = ""\n', encoding="utf-8")
    swarm.env.update(SWARM_BATCHING="1", FAKE_WORKER_SLEEP="0", FAKE_ROW_STATUS="k-W2=ok")
    swarm.up()
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        st = swarm.state() or {}
        if st.get("finished") and "ACTION finish" in swarm.log_text():
            break
        time.sleep(0.05)
    log = swarm.log_text()
    st = swarm.state() or {}
    assert st.get("done") == {"k-W1": "ok", "k-W2": "ok", "k-W3": "ok"}, log
    assert log.count("LAUNCH k-W") == 1, log  # one session for all three
    assert "CLAIM k-W3 slot=0 batch=k-W1" in log
