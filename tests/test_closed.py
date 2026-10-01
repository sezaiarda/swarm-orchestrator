"""A row and its record that disagree: one rule, and one answer everywhere.

A phase that failed and whose row was closed since (``swarm record <phase>
done``, or a tick by hand) is no failure any more: its failure record and its
sentinel are retired, so ``swarm status``, ``swarm doctor``, ``swarm why``, the
Overseer's digest, the dashboard, the web board and the forecast all read the
same state, its dependents are released, and the next ``swarm up`` has nothing
to bring the failure back from. The old failure stays in the row's history.

The order matters, and the row itself carries it: a ticked row that says
``failed`` was ticked first and failed since, and stays a failure. So does a
tick that is not committed on the target branch, a row a report is still queued
for, and a phase with a worker on it.

The reverse, a row still open in the ledger whose work the swarm landed, stays
done: the record wins, nothing rebuilds it, and doctor names it.

On real git, through the supervisor's own handlers.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import cli, doctor, gitq, ledgerw, ovdigest
from swarm_orchestrator import master as master_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import why as why_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.eta import engine as eta_engine
from swarm_orchestrator.supervisor import Supervisor
from swarm_orchestrator.tui import campaign, drawer
from swarm_orchestrator.tui.dash import Dash
from swarm_orchestrator.web.feed import Feed

LEDGER = """# Ledger

## Campaign A
- [x] `a-W1` · dir:`alpha` · needs:— · **the first wave**
- [ ] `a-W2` · dir:`alpha` · needs:`a-W1` · **the second wave**
- [ ] `a-W3` · dir:`alpha` · needs:`a-W2` · **the third wave**
"""

LEDGER_FILE = "docs/PHASE-LEDGER.md"

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
    monkeypatch.setenv("SWARM_SLUG", "closed")
    monkeypatch.setenv("SWARM_OVERSEER", "1")
    for leak in ("SWARM_SESSION_ID", "SWARM_PHASE", "SWARM_DRIVER", "SWARM_OVERSEER_PASS"):
        monkeypatch.delenv(leak, raising=False)
    monkeypatch.setattr(doctor, "_dir_size", lambda path: 0)
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)
    monkeypatch.setattr(sup, "_fill_slots", lambda *a, **k: [])
    yield cfg, project, sup
    sup.log.close()


def _attempt(cfg, sup, phase: str = "a-W2") -> None:
    """A worker on ``phase``: a claimed slot and a commit on its branch."""
    wt = gitq.worktree_add(cfg, phase, sup.log)
    (wt / "code.txt").write_text("half built\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"{phase} work")
    with state_mod.transaction(cfg) as st:
        st.claim_slot(phase)


def _report_fail(cfg, phase: str = "a-W2") -> None:
    """``swarm done <phase> fail`` as the worker leaves it: sentinel and report."""
    (cfg.done_dir / f"{phase}.fail").write_text(f"{phase} fail no saved titles to measure\n")
    ledgerw.queue(cfg, phase, {"kind": "outcome", "outcome": "fail",
                               "note": "no saved titles to measure", "after": ""})


def _fail(cfg, sup, phase: str = "a-W2") -> None:
    """An attempt that ends ``fail``, as the supervisor receives it: the branch
    is set aside and the row says failed."""
    _attempt(cfg, sup, phase)
    _report_fail(cfg, phase)
    sup._on_done(phase, "fail")


def _record_done(cfg, sup, phase: str = "a-W2") -> None:
    """``swarm record <phase> done`` from another session, and the supervisor
    reading the poke."""
    assert cli.cmd_record(cfg, phase, "done", "measured with seven saved titles", "") == 0
    sup._handle("ledger")


def _edit_row(project: Path, phase: str, *, status: str | None) -> None:
    """Tick the row in the working file; ``status`` replaces its short status."""
    ledger = project / LEDGER_FILE
    text = ledger.read_text().replace(f"- [ ] `{phase}`", f"- [x] `{phase}`")
    if status is not None:
        text = re.sub(rf"(`{phase}`.*status: )[^·\n]*", rf"\g<1>{status}", text)
    ledger.write_text(text)


def _tick_by_hand(project: Path, phase: str = "a-W2") -> None:
    """Someone closes the row in the ledger, as the swarm would have written it."""
    _edit_row(project, phase, status="done (by hand)")
    _git(project, "commit", "-qam", f"{phase}: closed by hand")


def _close(cfg, project, sup, how: str) -> None:
    _fail(cfg, sup)
    if how == "recorded":
        _record_done(cfg, sup)
    else:
        _tick_by_hand(project)
        assert state_mod.read(cfg).done == {"a-W2": "fail"}  # stale until something sweeps
        sup._watchdog_tick()


def _check(cfg, name: str) -> doctor.Check:
    return next(c for c in doctor.run_checks(cfg) if c.name == name)


def _still_failed(cfg) -> bool:
    return (state_mod.read(cfg).done.get("a-W2") == "fail"
            and (cfg.done_dir / "a-W2.fail").exists()
            and "a-W3" not in master_mod.build_context(cfg, state_mod.read(cfg))["ready"]
            and _check(cfg, "phases.failed").status == doctor.WARN)


HOW = pytest.mark.parametrize("how", ["recorded", "ticked"])


# -- the control: a failure that nobody closed is still one -------------------
def test_a_failed_phase_is_a_failure_until_its_row_is_closed(ws, capsys):
    cfg, project, sup = ws
    _fail(cfg, sup)
    sup._watchdog_tick()

    assert _still_failed(cfg)
    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert "failed: a-W2" in out and "1 failed" in out
    check = _check(cfg, "phases.failed")
    assert "['a-W2']" in check.detail and check.fix_hint.startswith("swarm retry a-W2")
    data = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0)
    assert [f["phase"] for f in data["failures"]] == ["a-W2"]
    assert ledgerw.release_closed(cfg, sup.log) == []


# -- the record-done path ------------------------------------------------------
def test_recording_a_failed_phase_done_retires_its_failure(ws):
    cfg, project, sup = ws
    _fail(cfg, sup)
    assert (cfg.done_dir / "a-W2.fail").exists()

    _record_done(cfg, sup)

    assert "- [x] `a-W2`" in (project / LEDGER_FILE).read_text()
    assert "a-W2" not in state_mod.read(cfg).done
    assert not (cfg.done_dir / "a-W2.fail").exists()
    assert "a-W2" not in gitq.sentinel_done(cfg)  # the next `swarm up` seeds no failure
    assert "FAIL-CLOSED a-W2" in cfg.supervisor_log.read_text()
    # The old failure is still in the row's history, above the entry that closed it.
    history = ledgerw.history_text(project, cfg.history_dir, "a-W2")
    assert history.index("no saved titles to measure") < history.index("seven saved titles")


def test_recording_it_done_releases_what_waited_behind_it(ws, monkeypatch):
    cfg, project, sup = ws
    _fail(cfg, sup)
    filled: list[str] = []
    monkeypatch.setattr(sup, "_fill_slots", lambda reason, **k: filled.append(reason) or [])

    _record_done(cfg, sup)

    assert filled == ["ledger updated"]
    assert master_mod.build_context(cfg, state_mod.read(cfg))["ready"] == ["a-W3"]


def test_a_sweep_that_cannot_run_does_not_hide_the_tick(ws, monkeypatch):
    cfg, project, sup = ws
    _fail(cfg, sup)

    def broken(*a, **k):
        raise gitq.GitError("git timed out")

    monkeypatch.setattr(gitq, "committed_text", broken)
    assert cli.cmd_record(cfg, "a-W2", "done", "measured", "") == 0
    assert sup._flush_ledger({}) is True  # the row was ticked: the launcher must run

    assert "- [x] `a-W2`" in (project / LEDGER_FILE).read_text()
    assert "FAIL-CLOSED-ERROR" in cfg.supervisor_log.read_text()
    monkeypatch.undo()
    assert ledgerw.release_closed(cfg, sup.log) == ["a-W2"]  # the next sweep does it


# -- every reader gives the same answer ----------------------------------------
@HOW
def test_status_does_not_call_a_closed_row_failed(ws, capsys, how):
    cfg, project, sup = ws
    _close(cfg, project, sup, how)
    capsys.readouterr()

    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert "failed" not in out and "fail=" not in out
    assert "phases: 2 of 3 done" in out and "1 ready" in out

    assert cli.cmd_status(cfg, as_json=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["failed"] == [] and "fail" not in data["done_counts"]
    assert data["phases"]["failed"] == 0 and data["phases"]["done"] == 2
    assert data["phases"]["held"] == 0


@HOW
def test_doctor_does_not_call_a_closed_row_failed(ws, how):
    cfg, project, sup = ws
    _close(cfg, project, sup, how)

    check = _check(cfg, "phases.failed")
    assert (check.status, check.detail) == (doctor.OK, "no failed phases")
    assert _check(cfg, "sentinels").status == doctor.OK
    assert _check(cfg, "phases.open").status == doctor.OK


@HOW
def test_the_digest_has_no_failed_phase_for_a_closed_row(ws, how):
    cfg, project, sup = ws
    _close(cfg, project, sup, how)

    data = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0)
    md = ovdigest.render(data)
    assert data["failures"] == [] and "## Failed phases (0)" in md
    assert "fail" not in data["context"]["done_counts"]
    assert data["context"]["ready"] == ["a-W3"]


@HOW
def test_why_and_the_launcher_read_a_closed_row_as_landed(ws, how):
    cfg, project, sup = ws
    _close(cfg, project, sup, how)

    assert master_mod.build_context(cfg, state_mod.read(cfg))["ready"] == ["a-W3"]
    assert why_mod.explain(cfg, "a-W3").reason != why_mod.BLOCKED
    text = why_mod.render(why_mod.explain(cfg, "a-W2"))
    assert "failed" not in text and "retry" not in text


@HOW
def test_the_tui_counts_a_closed_row_as_done(ws, how):
    cfg, project, sup = ws
    _close(cfg, project, sup, how)
    dash = Dash(cfg)
    dash.poll()

    snap = dash.snapshot
    total = campaign.overall(campaign.summarise(
        dash.graph, snap.landed, set(), set(cfg.exclude or []), dash.ticked, dash.deferred))
    assert (total.built, total.failed, total.ready, total.held) == (2, 0, ["a-W3"], 0)
    assert (snap.progress.failed, snap.progress.ready) == (0, 1)
    assert [a.title for a in drawer.alerts(dash) if a.phase == "a-W2"] == []


def test_an_open_dashboard_sees_the_row_closed(ws):
    """The dashboard was already up and showing the failure when the tick came."""
    cfg, project, sup = ws
    _fail(cfg, sup)
    dash = Dash(cfg)
    dash.poll()
    assert dash.snapshot.progress.failed == 1
    assert [a.phase for a in drawer.alerts(dash) if a.phase == "a-W2"] == ["a-W2"]

    _record_done(cfg, sup)
    dash.poll()

    assert (dash.snapshot.progress.failed, dash.snapshot.progress.ready) == (0, 1)
    assert [a.title for a in drawer.alerts(dash) if a.phase == "a-W2"] == []


@HOW
def test_the_web_board_places_a_closed_row_under_done(ws, how):
    cfg, project, sup = ws
    _close(cfg, project, sup, how)
    feed = Feed(cfg)
    feed.refresh(force=True)
    cards = {c["id"]: c for col in feed.board["columns"] for c in col["cards"]}

    assert cards["a-W2"]["col"] == "done"
    assert cards["a-W3"]["col"] == "ready"


@HOW
def test_the_forecast_does_not_list_a_closed_row_as_stuck(ws, how):
    cfg, project, sup = ws
    _close(cfg, project, sup, how)

    plan = eta_engine.plan_of(eta_engine.from_files(cfg, state_mod.read(cfg)))
    assert plan.stuck == () and set(plan.rows) == {"a-W3"}


# -- the stale record: the watchdog, and the next `swarm up` -------------------
def test_until_the_sweep_doctor_says_the_record_is_on_its_way_out(ws):
    """A supervisor on older code, or the minutes before its next tick."""
    cfg, project, sup = ws
    _fail(cfg, sup)
    _tick_by_hand(project)

    check = _check(cfg, "phases.failed")
    assert check.status == doctor.WARN
    assert "closed in the ledger since" in check.detail and "['a-W2']" in check.detail
    assert "swarm record" not in check.fix_hint and "swarm restart" in check.fix_hint
    assert ledgerw.closable(cfg, state_mod.read(cfg)) == {"a-W2"}

    sup._watchdog_tick()
    assert _check(cfg, "phases.failed").status == doctor.OK
    assert ledgerw.closable(cfg, state_mod.read(cfg)) == set()



def test_the_watchdog_retires_a_stale_failure_and_runs_the_launcher(ws, monkeypatch):
    cfg, project, sup = ws
    _fail(cfg, sup)
    _tick_by_hand(project)
    filled: list[str] = []
    monkeypatch.setattr(sup, "_fill_slots", lambda reason, **k: filled.append(reason) or [])

    assert sup._release_dated() is True

    assert "a-W2" not in state_mod.read(cfg).done
    assert not (cfg.done_dir / "a-W2.fail").exists()
    assert filled == ["a failed phase's row was closed"]
    assert "FAIL-CLOSED a-W2" in cfg.supervisor_log.read_text()
    assert sup._release_dated() is False and len(filled) == 1  # once


def test_swarm_up_does_not_bring_a_closed_failure_back(ws):
    """Only the sentinel is left (the done map was lost, or cleared by hand):
    the re-seed from ``done/`` used to restore the failure."""
    cfg, project, sup = ws
    _fail(cfg, sup)
    _tick_by_hand(project)
    with state_mod.transaction(cfg) as st:
        st.done.pop("a-W2")
    assert gitq.sentinel_done(cfg) == {"a-W2": "fail"}

    cli._reconcile_orphans(cfg)

    assert "a-W2" not in state_mod.read(cfg).done
    assert gitq.sentinel_done(cfg) == {}
    assert _check(cfg, "phases.failed").status == doctor.OK


def test_swarm_up_still_seeds_a_failure_whose_row_is_open(ws):
    cfg, project, sup = ws
    _fail(cfg, sup)
    with state_mod.transaction(cfg) as st:
        st.done.pop("a-W2")

    cli._reconcile_orphans(cfg)

    assert state_mod.read(cfg).done == {"a-W2": "fail"}
    assert (cfg.done_dir / "a-W2.fail").exists()


def test_a_record_left_without_its_sentinel_is_retired_by_the_next_sweep(ws):
    """The sweep stopped between the two (the sentinel goes first)."""
    cfg, project, sup = ws
    _fail(cfg, sup)
    _tick_by_hand(project)
    (cfg.done_dir / "a-W2.fail").unlink()

    assert ledgerw.release_closed(cfg, sup.log) == ["a-W2"]
    assert "a-W2" not in state_mod.read(cfg).done
    assert _check(cfg, "sentinels").status == doctor.OK


# -- what does not close a row -------------------------------------------------
def test_a_row_ticked_first_that_failed_since_is_still_a_failure(ws):
    """Recorded done while its worker was still on it; the worker then failed."""
    cfg, project, sup = ws
    _attempt(cfg, sup)
    _record_done(cfg, sup)
    assert "- [x] `a-W2`" in (project / LEDGER_FILE).read_text()

    _report_fail(cfg)
    sup._on_done("a-W2", "fail")
    sup._watchdog_tick()

    row = next(ln for ln in (project / LEDGER_FILE).read_text().splitlines() if "`a-W2`" in ln)
    assert row.startswith("- [x]") and "status: failed" in row
    assert _still_failed(cfg)
    check = _check(cfg, "phases.failed")
    assert "ticked in the ledger all the same: ['a-W2']" in check.detail
    assert check.fix_hint.startswith("swarm record a-W2 done")


def test_a_landed_phase_that_is_run_again_and_fails_is_a_failure(ws):
    cfg, project, sup = ws
    _edit_row(project, "a-W2", status=None)
    _git(project, "commit", "-qam", "a-W2 landed long ago")
    with state_mod.transaction(cfg) as st:
        st.mark_done("a-W2", "ok")
    assert cli.cmd_retry(cfg, ["a-W2"], False, False, False, False) == 0

    _fail(cfg, sup)
    sup._watchdog_tick()

    assert _still_failed(cfg)


def test_a_box_ticked_on_a_row_that_still_says_failed_closes_nothing(ws):
    cfg, project, sup = ws
    _fail(cfg, sup)
    _edit_row(project, "a-W2", status=None)  # the box alone
    _git(project, "commit", "-qam", "a-W2: box only")
    sup._watchdog_tick()

    assert _still_failed(cfg)
    assert _check(cfg, "phases.failed").fix_hint.startswith("swarm record a-W2 done")

    _record_done(cfg, sup)  # the hint's command closes it
    assert state_mod.read(cfg).done == {} and not (cfg.done_dir / "a-W2.fail").exists()


def test_a_tick_that_is_not_committed_closes_nothing(ws):
    cfg, project, sup = ws
    _fail(cfg, sup)
    _edit_row(project, "a-W2", status="done (by hand)")

    assert ledgerw.release_closed(cfg, sup.log) == []
    assert state_mod.read(cfg).done == {"a-W2": "fail"}

    _git(project, "checkout", "-q", "--", LEDGER_FILE)  # taken back: nothing was lost
    assert _still_failed(cfg)


def test_a_tick_on_another_branch_closes_nothing(ws):
    cfg, project, sup = ws
    _fail(cfg, sup)
    _git(project, "checkout", "-qb", "draft")
    _tick_by_hand(project)

    assert ledgerw.release_closed(cfg, sup.log) == []

    _git(project, "checkout", "-q", "master")
    assert _still_failed(cfg)
    _git(project, "merge", "-q", "--ff-only", "draft")
    assert ledgerw.release_closed(cfg, sup.log) == ["a-W2"]


def test_a_row_with_a_report_still_queued_is_left_alone(ws):
    cfg, project, sup = ws
    _fail(cfg, sup)
    _tick_by_hand(project)
    assert cli.cmd_record(cfg, "a-W2", "failed", "it is still broken", "") == 0  # not read yet

    assert ledgerw.reported(cfg) == {"a-W2"}
    assert ledgerw.release_closed(cfg, sup.log) == []

    sup._handle("ledger")  # the row now says failed
    sup._watchdog_tick()
    assert _still_failed(cfg)


@pytest.mark.parametrize("held", ["claimed", "merging"])
def test_a_phase_with_a_worker_on_it_keeps_its_record(ws, held):
    cfg, project, sup = ws
    _fail(cfg, sup)
    _tick_by_hand(project)
    with state_mod.transaction(cfg) as st:
        if held == "claimed":
            st.claim_slot("a-W2")
        else:
            st.integ_push("a-W2", "ok")

    assert ledgerw.release_closed(cfg, sup.log) == []
    assert state_mod.read(cfg).done == {"a-W2": "fail"}
    assert (cfg.done_dir / "a-W2.fail").exists()


def test_closed_rows_reads_the_box_and_the_status():
    text = "\n".join([
        "- [x] `a` · needs:— · **t** · status: done (2026-10-01)",
        "- [x] `b` · needs:— · **t**",
        "- [X] `c` · needs:— · **t** · status: done, operator follow-up (2026-10-01)",
        "- [x] `d` · needs:— · **t** · status: failed (2026-10-01)",
        "- [x] `e` · needs:— · **t** · status: blocked (2026-10-01)",
        "- [x] `f` · needs:— · **t** · after:`2999-01-01` · status: later, after 2999-01-01",
        "- [ ] `g` · needs:— · **t** · status: done (2026-10-01)",
        "- [ ] `h` · needs:— · **t**",
    ])
    assert ledgerw.closed_rows(text) == {"a", "b", "c"}


# -- the reverse: the swarm landed it, the ledger still shows it open ----------
def test_a_row_reopened_after_ok_stays_done_and_doctor_names_it(ws, capsys):
    cfg, project, sup = ws
    with state_mod.transaction(cfg) as st:
        st.mark_done("a-W2", "ok")

    ctx = master_mod.build_context(cfg, state_mod.read(cfg))
    assert ctx["ready"] == ["a-W3"]  # never rebuilt, and its dependents go on
    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert "phases: 2 of 3 done (1 of them done by the swarm, still open in the ledger)" in out
    check = _check(cfg, "phases.open")
    assert check.status == doctor.WARN and "['a-W2']" in check.detail
    assert "swarm record a-W2 done" in check.fix_hint and "swarm retry a-W2" in check.fix_hint


def test_doctor_does_not_name_a_row_whose_tick_is_on_its_way(ws):
    cfg, project, sup = ws
    with state_mod.transaction(cfg) as st:
        st.mark_done("a-W2", "ok")
    ledgerw.queue(cfg, "a-W2", {"kind": "outcome", "outcome": "ok", "note": "n", "after": ""})

    assert _check(cfg, "phases.open").status == doctor.OK

    sup._flush_ledger({"a-W2": "ok"})
    assert "- [x] `a-W2`" in (project / LEDGER_FILE).read_text()
    check = _check(cfg, "phases.open")
    assert (check.status, check.detail) == (doctor.OK, "every landed phase is ticked in the ledger")


def test_doctor_does_not_ask_for_a_record_that_is_already_queued(ws):
    cfg, project, sup = ws
    with state_mod.transaction(cfg) as st:
        st.mark_done("a-W2", "ok")
    assert _check(cfg, "phases.open").status == doctor.WARN

    assert cli.cmd_record(cfg, "a-W2", "done", "it stands", "") == 0  # queued, not read yet

    assert _check(cfg, "phases.open").status == doctor.OK


def test_a_skipped_row_left_open_is_not_a_disagreement(ws):
    cfg, project, sup = ws
    with state_mod.transaction(cfg) as st:
        st.mark_done("a-W2", "skip")

    assert _check(cfg, "phases.open").status == doctor.OK


def test_a_ledger_without_boxes_has_nothing_to_compare(ws):
    cfg, project, sup = ws
    (project / LEDGER_FILE).write_text("a-W1:\na-W2: a-W1\n")
    _git(project, "commit", "-qam", "a bare ledger")
    with state_mod.transaction(cfg) as st:
        st.mark_done("a-W1", "ok")
        st.mark_done("a-W2", "fail")

    check = _check(cfg, "phases.open")
    assert (check.status, check.detail) == (doctor.OK, "the ledger has no checkboxes to compare")
    assert ledgerw.release_closed(cfg, sup.log) == []
