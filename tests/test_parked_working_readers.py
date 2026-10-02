"""Every reader of ``parked`` tells an answered session from one still asking.

A parked session stays in its own window until it finishes, so ``State.parked``
alone cannot say whether the owner still owes it an answer. ``State.answered``
does: the owner answered, and the session is working again. These tests put one
real state file in three shapes — answered, still asking, and a file from before
the marks existed — and read it through every reader outside the supervisor: the
forecast, ``swarm status``, ``swarm why``, ``swarm context``, ``swarm report``,
``swarm restart``, the dashboard's data and the web board.

The answered session is a worker at work wherever it is listed. The other two
shapes are the control: each still reads as waiting on the owner, as it always
did.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from dataclasses import replace
from datetime import datetime

import pytest

from swarm_orchestrator import cli, logutil, master, procs
from swarm_orchestrator import report as report_mod
from swarm_orchestrator import restart as restart_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import why as why_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.eta import engine
from swarm_orchestrator.eta import hazards as hazards_mod
from swarm_orchestrator.eta import holds as holds_mod
from swarm_orchestrator.eta import model as model_mod
from swarm_orchestrator.eta import plan as plan_mod
from swarm_orchestrator.eta import sim as sim_mod
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.state import State
from swarm_orchestrator.tui import books, data
from swarm_orchestrator.web.feed import Feed

H = 3600.0
PARK_AFTER = 120
NOW = float(int(time.time()))
LAUNCHED = NOW - 3 * H
ASKED = NOW - 2 * H
ANSWERED = NOW - H

#: P0 builds in a slot, P1 is the parked session, P2 can only start after P1.
LEDGER = (
    "- [ ] `P0` · needs:—\n"
    "- [ ] `P1` · needs:—\n"
    "- [ ] `P2` · needs:`P1`\n"
)

#: The three shapes a parked session's marks can be in.
WORKING = "answered"
ASKING = "asked"
UNMARKED = "a state file from before the marks"
STILL_ASKING = pytest.mark.parametrize("mark", [ASKING, UNMARKED])


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    (project / ".swarm.toml").write_text(
        '[swarm]\ndriver = "bare"\nmax_workers = 2\n[web]\nenabled = false\n'
        f"[worker]\npark_after = {PARK_AFTER}\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    for leak in ("SWARM_DRIVER", "SWARM_GIT_ISOLATION", "SWARM_PARK_AFTER"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    # Both workers were launched three hours ago, as the log says.
    c.supervisor_log.write_text(
        "".join(f"{datetime.fromtimestamp(LAUNCHED).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}"
                f" 1.000 LAUNCH {phase} slot={slot}\n" for slot, phase in enumerate(("P0", "P1"))),
        encoding="utf-8")
    return c


def run(cfg, mark: str) -> State:
    """P0 in a slot; P1 asked, was parked, and is now in the shape ``mark`` names."""
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(State.fresh(2).__dict__)
        st.claim_slot("P0")
        st.claim_slot("P1")
        st.waiting["P1"] = ASKED + PARK_AFTER
        st.park("P1", PARK_AFTER)
        if mark == WORKING:
            st.answer("P1", ANSWERED)
        elif mark == UNMARKED:
            st.asked.clear()
    st = state_mod.read(cfg)
    assert st.parked == ["P1"] and st.asking("P1") == (mark != WORKING)
    return st


def raw(cfg) -> dict:
    """``state.json`` as the dashboard and the board read it."""
    return json.loads(cfg.state_path.read_text(encoding="utf-8"))


# -- the forecast ------------------------------------------------------------------
def test_the_forecast_schedules_an_answered_parked_row_as_running(cfg):
    plan = engine.plan_of(engine.from_files(cfg, run(cfg, WORKING), now=NOW))
    assert plan.stuck == () and not plan.held
    assert plan.running == {"P0": 3 * H, "P1": 3 * H}  # from its launch, like a slot's
    assert set(plan.rows) == {"P0", "P1", "P2"}  # and the row behind it is timed again
    assert plan.books["P1"].running == ("P1",) and plan.books["P1"].behind == 0
    assert plan.outside == frozenset({"P1"})  # it runs in its own window, not in a seat


def test_the_forecast_gives_the_seat_an_answered_parked_row_left_to_the_next_row(cfg):
    """Two seats, one worker in a slot and one parked and at work: a seat is
    free now, and the forecast starts the next ready row in it."""
    ledger = LEDGER + "- [ ] `P3` · needs:—\n"
    (cfg.project_dir / "docs" / "PHASE-LEDGER.md").write_text(ledger, encoding="utf-8")
    inputs = engine.from_files(cfg, run(cfg, WORKING), now=NOW)
    plan = engine.plan_of(replace(inputs, started={}))  # both just started: an hour each
    hour = model_mod.Durations(mu=math.log(H), sigma=1e-9, shared=0.0)
    (future,) = sim_mod.simulate(plan, hour, hazards_mod.Hazards(follow_all=0.0),
                                 holds_mod.Holds(), sim_mod.Options(runs=1))
    hours = {row: round((t - NOW) / H, 3) for row, t in future.finish.items()}
    assert hours == {"P0": 1.0, "P1": 1.0, "P3": 1.0, "P2": 2.0}


@STILL_ASKING
def test_the_forecast_leaves_out_a_parked_row_that_still_asks(cfg, mark):
    plan = engine.plan_of(engine.from_files(cfg, run(cfg, mark), now=NOW))
    assert [(s.row, s.why, s.behind) for s in plan.stuck] == [("P1", plan_mod.ASKED, ("P2",))]
    assert plan.running == {"P0": 3 * H} and set(plan.rows) == {"P0"}
    assert plan.outside == frozenset()


def test_the_forecast_is_remade_when_the_owner_answers(cfg):
    asking = engine.from_files(cfg, run(cfg, ASKING), now=NOW)
    working = engine.from_files(cfg, run(cfg, WORKING), now=NOW)
    again = engine.from_files(cfg, run(cfg, WORKING), now=NOW + 5)
    fid = engine.fit_key(asking)
    assert engine.key(asking, fid) != engine.key(working, fid)
    assert engine.key(working, fid) == engine.key(again, fid)  # the clock alone is no change
    # With nothing in a slot the parked worker alone keeps the running cadence.
    with state_mod.transaction(cfg) as st:
        st.free_slot_for("P0")
    alone = engine.from_files(cfg, state_mod.read(cfg), now=NOW)
    assert engine.recompute_s(alone) == engine.RECOMPUTE_S


def test_swarm_status_says_a_parked_session_is_working_and_counts_nothing_on_the_owner(
        cfg, capsys):
    run(cfg, WORKING)
    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert "waiting=[] parked=['P1'] working=['P1']" in out
    assert "wait on you" not in out and "waiting on you" not in out
    assert "eta: 3 rows left" in out
    # The ledger's count has it running, as the dashboard's headline does, not
    # ready to launch.
    standing = cli._phase_standing(cfg, state_mod.read(cfg))
    assert (standing["running"], standing["ready"]) == (2, 0)


@STILL_ASKING
def test_swarm_status_keeps_its_line_and_its_count_for_a_session_that_asks(cfg, capsys, mark):
    run(cfg, mark)
    # The count has it waiting on the owner: not ready to launch, and not at work.
    standing = cli._phase_standing(cfg, state_mod.read(cfg))
    assert (standing["running"], standing["asking"], standing["ready"]) == (1, 1, 0)
    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert "waiting=[] parked=['P1']\n" in out and "working=" not in out
    assert "eta: 1 row left" in out and "2 more wait on you" in out
    assert "waiting on you: P1 (asked you a question) holds 1 row" in out


# -- swarm why -----------------------------------------------------------------------
def test_why_says_an_answered_parked_phase_is_working_in_its_window(cfg):
    run(cfg, WORKING)
    exp = why_mod.explain(cfg, "P1")
    assert exp.reason == why_mod.PARKED
    assert "working on your answer in tmux window wait:P1" in exp.detail
    assert "waiting on YOU" not in exp.detail and "not waiting on you" in exp.detail
    behind = why_mod.explain(cfg, "P2")
    assert behind.root_cause == "P1" and "working on your answer" in behind.root_detail
    assert why_mod._holder_state(state_mod.read(cfg), "P1") == "working in its own window"


@STILL_ASKING
def test_why_says_a_parked_phase_that_asks_waits_on_the_owner(cfg, mark):
    run(cfg, mark)
    exp = why_mod.explain(cfg, "P1")
    assert exp.reason == why_mod.PARKED
    assert exp.detail.startswith("waiting on YOU") and "answer it there" in exp.detail
    assert why_mod.explain(cfg, "P2").root_detail.startswith("waiting on YOU")
    assert why_mod._holder_state(state_mod.read(cfg), "P1") == "parked"


# -- swarm context, swarm report, swarm skip -----------------------------------------
def test_context_names_the_parked_sessions_that_are_working(cfg):
    ctx = master.build_context(cfg, run(cfg, WORKING))
    assert ctx["parked"] == ["P1"] and ctx["parked_working"] == ["P1"]
    # Still in flight: never offered again, and the row behind it still waits.
    assert ctx["ready"] == [] and ctx["launchable"] == []


@STILL_ASKING
def test_context_lists_no_parked_session_as_working_while_it_asks(cfg, mark):
    ctx = master.build_context(cfg, run(cfg, mark))
    assert ctx["parked"] == ["P1"] and ctx["parked_working"] == []
    assert ctx["ready"] == [] and ctx["launchable"] == []


def test_the_report_calls_an_answered_parked_phase_busy(cfg):
    run(cfg, WORKING)
    live = {p.phase: p.live for p in report_mod.build_report(cfg).phases}
    assert live["P0"] == "busy" and live["P1"] == "busy"


@STILL_ASKING
def test_the_report_calls_a_parked_phase_that_asks_parked(cfg, mark):
    run(cfg, mark)
    live = {p.phase: p.live for p in report_mod.build_report(cfg).phases}
    assert live["P0"] == "busy" and live["P1"] == "parked"


def test_skip_says_what_the_session_it_closed_was_doing(cfg, capsys):
    run(cfg, WORKING)
    assert cli.cmd_skip(cfg, "P1") == 0
    out = capsys.readouterr().out
    assert "working on your answer" in out and "waiting on you" not in out
    st = state_mod.read(cfg)
    assert st.parked == [] and st.answered == {} and st.done["P1"] == "skip"


@STILL_ASKING
def test_skip_says_a_session_that_asked_was_waiting_on_the_owner(cfg, capsys, mark):
    run(cfg, mark)
    assert cli.cmd_skip(cfg, "P1") == 0
    assert "it was parked waiting on you" in capsys.readouterr().out


# -- swarm restart ---------------------------------------------------------------------
def test_a_restart_counts_an_answered_parked_session_as_a_worker_and_no_question(cfg):
    st = run(cfg, WORKING)
    assert restart_mod.counts(st) == (2, 0)
    assert restart_mod.counts_of(raw(cfg)) == (2, 0)
    plan = restart_mod.new_plan(cfg, restart_mod.SUPERVISOR, NOW + H, "owner terminal", now=NOW)
    assert "2 workers and 0 questions carry on untouched" in restart_mod.line(
        plan, *restart_mod.counts(st), NOW)


@STILL_ASKING
def test_a_restart_counts_a_parked_session_that_asks_as_a_question(cfg, mark):
    st = run(cfg, mark)
    assert restart_mod.counts(st) == (1, 1)
    assert restart_mod.counts_of(raw(cfg)) == (1, 1)


def test_with_no_supervisor_to_wait_for_it_a_full_restart_refuses_for_a_working_session(
        cfg, capsys):
    """A drain waits for a session at work outside the slots, and the supervisor
    is what drains. With none running nothing would wait, so a full restart
    would close it mid-work: it is still named, as what it is."""
    st = run(cfg, WORKING)
    (q,) = restart_mod.questions(cfg, st)
    assert (q.key, q.who, q.parked, q.asking, q.text) == ("P1", "the worker on P1", True, False, "")
    assert restart_mod.question_lines([q]) == ["  - the worker on P1: working on your answer"]
    assert restart_mod.standing([q]) == "1 session(s) working on your answer"
    assert restart_mod.refusal({}, [q]) == (
        "1 session working on the owner's answer would be closed by a full restart:"
        " the worker on P1")

    assert cli.cmd_restart(cfg, full=True) == 1
    err = capsys.readouterr().err
    assert "refused — 1 session(s) are working on your answer, and a full restart closes" in err
    assert "the worker on P1: working on your answer" in err
    assert "waiting on you" not in err
    assert not restart_mod.load(cfg)  # nothing was planned


@STILL_ASKING
def test_a_full_restart_refuses_for_a_parked_session_that_asks_in_the_words_it_had(
        cfg, capsys, mark):
    st = run(cfg, mark)
    (q,) = restart_mod.questions(cfg, st)
    assert q.asking and q.parked
    assert restart_mod.standing([q]) == "1 session(s) waiting on you"
    assert restart_mod.standing([q], "are ") == "1 session(s) are waiting on you"
    assert restart_mod.refusal({}, [q]) == (
        "1 session waiting on the owner would be closed by a full restart: the worker on P1")
    assert cli.cmd_restart(cfg, full=True) == 1
    err = capsys.readouterr().err
    assert "refused — 1 session(s) are waiting on you, and a full restart closes them:" in err
    assert "working on your answer" not in err


def test_the_refusal_counts_the_asking_apart_from_the_working(cfg):
    asking = restart_mod.Question("P0", "the worker on P0", "", "which port?", False)
    working = restart_mod.Question("P1", "the worker on P1", "wait:P1", "", True, False)
    both = [asking, working]
    assert restart_mod.standing(both, "are ") == (
        "1 session(s) are waiting on you and 1 are working on your answer")
    assert restart_mod.question_lines(both) == [
        "  - the worker on P0: which port?",
        "  - the worker on P1 (tmux window wait:P1): working on your answer"]
    assert restart_mod.refusal({}, both) == (
        "1 session waiting on the owner and 1 session working on the owner's answer would be"
        " closed by a full restart: the worker on P0 and the worker on P1")


@pytest.fixture
def sessions(cfg):
    """A live process per kept session, carrying the markers a worker's does:
    on the bare driver that is how a restart tells a session is still there."""
    started = []

    def start(phase: str) -> None:
        env = {"PATH": os.environ.get("PATH", ""), "SWARM_STATE_DIR": str(cfg.state_dir),
               procs.SESSION_ENV: f"worker:{phase}", cfg.env_marker: phase}
        started.append(subprocess.Popen(["sleep", "300"], env=env))

    yield start
    for proc in started:
        proc.kill()
        proc.wait()


def carried(cfg) -> State:
    """The state after a kept restart's down and up: a fresh run, then the kept
    sessions put back."""
    log = Log(cfg.supervisor_log)
    try:
        state_mod.init_state(cfg, carried=restart_mod.kept_phases(cfg))
        assert state_mod.read(cfg).parked == []
        restart_mod.carry_in(cfg, log)
    finally:
        log.close()
    return state_mod.read(cfg)


def test_a_kept_restart_carries_who_is_working_and_who_asks_and_since_when(cfg, sessions):
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(State.fresh(2).__dict__)
        for phase in ("P0", "P1", "P2"):
            st.claim_slot(phase)
            st.waiting[phase] = ASKED + PARK_AFTER
        st.park("P0", PARK_AFTER)  # parked, and still asking
        st.park("P1", PARK_AFTER)
        st.answer("P1", ANSWERED)  # parked, and working on the answer
        st.waiting["P2"] = ASKED + 60 + PARK_AFTER  # asked a minute later: not parked yet
    for phase in ("P0", "P1", "P2"):
        sessions(phase)
    log = Log(cfg.supervisor_log)
    try:
        assert restart_mod.carry_out(cfg, "plan-1", log) == ["P0", "P1", "P2"]
    finally:
        log.close()
    kept = {e["key"]: (e["asked"], e["answered"]) for e in restart_mod.load_kept(cfg)["sessions"]}
    assert kept == {"P0": (ASKED, None), "P1": (None, ANSWERED), "P2": (ASKED + 60, None)}

    st = carried(cfg)
    assert st.parked == ["P0", "P1", "P2"] and st.waiting == {}
    assert st.working_parked() == ["P1"] and st.answered == {"P1": ANSWERED}
    assert st.on_owner() == ["P0", "P2"]
    assert st.asked_at("P0", PARK_AFTER) == ASKED and st.asked_at("P2", PARK_AFTER) == ASKED + 60
    assert not restart_mod.load_kept(cfg)


def test_a_kept_file_from_before_the_marks_comes_back_asking(cfg, sessions):
    run(cfg, WORKING)
    sessions("P1")
    log = Log(cfg.supervisor_log)
    try:
        assert restart_mod.carry_out(cfg, "plan-1", log) == ["P1"]
    finally:
        log.close()
    path = restart_mod.kept_path(cfg)
    kept = json.loads(path.read_text(encoding="utf-8"))
    for entry in kept["sessions"]:
        entry.pop("asked", None)
        entry.pop("answered", None)
    path.write_text(json.dumps(kept), encoding="utf-8")

    st = carried(cfg)
    assert st.parked == ["P1"] and st.asking("P1")
    assert st.asked == {} and st.answered == {} and st.asked_at("P1", PARK_AFTER) is None


def full_restart(cfg) -> dict:
    """A ``--keep-questions`` full restart from its drain's end, with the swarm's
    own down and up stood in for by what they do to the state; the plan after."""
    plan = restart_mod.save(cfg, restart_mod.new_plan(
        cfg, restart_mod.FULL, NOW, "owner terminal", questions=restart_mod.KEEP, now=NOW))
    if not cfg.fifo_path.exists():
        os.mkfifo(cfg.fifo_path)
    reader = os.open(cfg.fifo_path, os.O_RDONLY | os.O_NONBLOCK)  # a supervisor came up
    try:
        assert restart_mod.finish_full(cfg, plan["id"], lambda c: 0,
                                       lambda c: 0 if carried(c) else 1) == 0
    finally:
        os.close(reader)
    return restart_mod.load(cfg)


def test_a_kept_restart_says_it_carried_a_working_session_not_a_question(cfg, sessions):
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(State.fresh(2).__dict__)
        for phase in ("P0", "P1"):
            st.claim_slot(phase)
            st.waiting[phase] = ASKED + PARK_AFTER
            st.park(phase, PARK_AFTER)
        st.answer("P1", ANSWERED)
    sessions("P0")
    sessions("P1")
    plan = full_restart(cfg)
    assert plan["stage"] == restart_mod.DONE
    assert plan["detail"] == "1 question and 1 working session carried across"
    assert state_mod.read(cfg).working_parked() == ["P1"]


@STILL_ASKING
def test_a_kept_restart_says_it_carried_a_question_for_a_session_that_asks(cfg, sessions, mark):
    run(cfg, mark)
    with state_mod.transaction(cfg) as st:
        st.free_slot_for("P0")
    sessions("P1")
    plan = full_restart(cfg)
    assert plan["stage"] == restart_mod.DONE and plan["detail"] == "1 question carried across"
    assert state_mod.read(cfg).on_owner() == ["P1"]


# -- the dashboard's data ----------------------------------------------------------------
def snapshot(cfg):
    return data.build_snapshot(cfg, data.read_state(cfg), graph=data.load_graph(cfg))


def history(cfg) -> dict:
    events = data.parse_events(logutil.read_all(cfg.supervisor_log))
    return {r.phase: r for r in data.build_history(events, state=data.read_state(cfg))}


def test_the_dashboard_does_not_ask_the_owner_about_an_answered_parked_phase(cfg):
    run(cfg, WORKING)
    snap = snapshot(cfg)
    assert snap.blockers == []
    assert snap.progress.running == 2 and snap.progress.ready == 0  # still in flight
    p1 = history(cfg)["P1"]
    assert p1.running and p1.hold == ""  # a run at work, like the one in the slot


@STILL_ASKING
def test_the_dashboard_asks_the_owner_about_a_parked_phase_that_asks(cfg, mark):
    run(cfg, mark)
    snap = snapshot(cfg)
    assert [(b.phase, b.kind) for b in snap.blockers] == [("P1", "parked")]
    assert "wait:P1" in snap.blockers[0].detail
    # Aged from its question when state has the moment, else as before.
    assert snap.blockers[0].since == (ASKED if mark == ASKING else None)
    assert snap.progress.running == 2 and snap.progress.ready == 0
    p1 = history(cfg)["P1"]
    assert not p1.running and p1.hold == "parked"


# -- the web board ---------------------------------------------------------------------
def board(cfg) -> dict:
    feed = Feed(cfg)
    feed.refresh(force=True)
    return feed.board


def cards(b: dict) -> dict[str, dict]:
    return {c["id"]: c for col in b["columns"] for c in col["cards"]}


def test_the_board_shows_an_answered_parked_phase_building_in_its_window(cfg):
    run(cfg, WORKING)
    b = board(cfg)
    got = cards(b)
    assert got["P1"]["col"] == "building"
    assert got["P1"]["sub"] == "works on your answer in tmux window wait:P1"
    assert got["P1"]["since"] == LAUNCHED and "slot" not in got["P1"] and "q" not in got["P1"]
    assert b["header"]["needs_you"] == 0
    building = next(col for col in b["columns"] if col["key"] == "building")
    assert [c["id"] for c in building["cards"]] == ["P0", "P1"]  # the slot first
    # What waits behind it waits on work under way, not on the owner.
    assert got["P2"]["col"] == "blocked"
    assert (got["P2"]["root"], got["P2"]["root_kind"]) == ("P1", "building")


@STILL_ASKING
def test_the_board_shows_a_parked_phase_that_asks_under_needs_you(cfg, mark):
    run(cfg, mark)
    b = board(cfg)
    got = cards(b)
    assert got["P1"]["col"] == "needs_you"
    assert got["P1"]["sub"] == "asks you · answer in tmux window wait:P1"
    assert b["header"]["needs_you"] == 1
    assert (got["P2"]["root"], got["P2"]["root_kind"]) == ("P1", "parked")
