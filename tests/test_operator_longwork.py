"""Tests for an operator job that has to wait longer than one lease.

A lease is one hour for every job, and a session that sat past it was closed in
the middle of its work and its job queued again as a failed attempt. A wait is
not a failure, so a session now says which kind of wait it has:

* **the session is not needed meanwhile** (a run detached on the host, a time
  window): it puts the job back with ``operator-done --not-before`` and a note
  of where it stopped. The window is free, the attempt is given back, the job
  opens again at that time whatever its triage said, and the note reaches the
  next session even when a reopening in between failed;
* **the session has to stay** (a build of its own behind a busy gate):
  ``operator-hold`` moves the lease to a declared, bounded time. Only the
  session carrying the job out can do that, both halves of the lease move
  together, and a session that outlives what it declared is still reclaimed,
  without the attempt being counted, a bounded number of times.

Everything runs on the bare driver, like the other operator tests: a test must
never be able to start a real session.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from swarm_orchestrator import operator as operator_mod
from swarm_orchestrator import opqueue
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log

JOB = "op-L1"
OTHER = "op-L2"
NOTE = "roll the new image and read the measurement when its run ends"
WHY = "the image build waits behind another build"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".swarm.toml").write_text(
        '[operator]\nenabled = true\n[swarm]\ndriver = "bare"\n', encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_BIN", "true")  # nothing detached may reach a model
    for leak in ("SWARM_TRIAGE_CMD", "SWARM_RECAP_CMD", "SWARM_OPERATOR", "SWARM_DRIVER",
                 operator_mod.JOB_ENV, "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(leak, raising=False)
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def log(cfg):
    lg = Log(cfg.supervisor_log)
    yield lg
    lg.close()


def cli(cfg, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "swarm_orchestrator",
         "--project-dir", str(cfg.project_dir), *args],
        cwd=str(cfg.project_dir), capture_output=True, text=True, timeout=60,
    )


def running(cfg, log, job: str = JOB, note: str = NOTE) -> opqueue.Item:
    assert opqueue.add(cfg, job, status="operator", note=note) is not None
    assert operator_mod.dispatch(cfg, job, log) is True
    return opqueue.load(cfg, job)


def minutes(n: float) -> float:
    return n * 60.0


# -- the session stays: a declared, bounded lease -------------------------
def test_a_declared_ninety_minute_wait_is_not_reclaimed_at_the_hour(cfg, log):
    running(cfg, log)
    t0 = time.time()

    held = operator_mod.hold(cfg, JOB, t0 + minutes(90), WHY, log, now=t0)
    operator_mod.sweep(cfg, log, now=t0 + minutes(61))

    assert held is not None
    assert opqueue.load(cfg, JOB).state == opqueue.RUNNING
    assert state_mod.read(cfg).operator_phase == JOB
    assert "OPERATOR-LEASE-EXPIRED" not in cfg.supervisor_log.read_text()


def test_a_declared_wait_is_reclaimed_once_its_own_bound_has_passed(cfg, log):
    """A session that truly hangs must still let go: the bound moved, it did not go."""
    running(cfg, log)
    t0 = time.time()
    operator_mod.hold(cfg, JOB, t0 + minutes(90), WHY, log, now=t0)

    operator_mod.sweep(cfg, log, now=t0 + minutes(91))

    item = opqueue.load(cfg, JOB)
    assert item.state == opqueue.QUEUED
    assert state_mod.read(cfg).operator_phase is None
    assert "OPERATOR-LEASE-EXPIRED" in cfg.supervisor_log.read_text()
    assert WHY in item.last_error  # the next session reads what this one was waiting for


def test_a_session_that_declared_nothing_is_reclaimed_at_the_hour_as_before(cfg, log):
    running(cfg, log)

    operator_mod.sweep(cfg, log, now=time.time() + opqueue.LEASE_S + 1)

    item = opqueue.load(cfg, JOB)
    assert (item.state, item.attempts) == (opqueue.QUEUED, 1)  # the attempt is spent
    assert state_mod.read(cfg).operator_phase is None


def test_a_declared_wait_that_runs_out_does_not_add_an_attempt(cfg, log):
    running(cfg, log)
    t0 = time.time()
    operator_mod.hold(cfg, JOB, t0 + minutes(90), WHY, log, now=t0)

    operator_mod.sweep(cfg, log, now=t0 + minutes(91))

    item = opqueue.load(cfg, JOB)
    assert (item.state, item.attempts) == (opqueue.QUEUED, 0)
    assert (item.hold_until, item.hold_why) == (0.0, "")  # the next session declared nothing


def test_a_hold_shorter_than_the_hour_does_not_make_the_hour_free(cfg, log):
    """The refund is for a declared wait that ran out, not for having declared one."""
    running(cfg, log)
    t0 = time.time()
    operator_mod.hold(cfg, JOB, t0 + minutes(5), WHY, log, now=t0)

    operator_mod.sweep(cfg, log, now=t0 + opqueue.LEASE_S + 1)

    item = opqueue.load(cfg, JOB)
    assert (item.state, item.attempts, item.lapses) == (opqueue.QUEUED, 1, 0)
    assert item.last_error == "the operator session outlived its lease"


def test_a_hold_that_is_long_past_does_not_make_a_later_hang_free(cfg, log):
    """Asked the owner, answered after the hold was over: an ordinary lease again."""
    running(cfg, log)
    t0 = time.time()
    operator_mod.hold(cfg, JOB, t0 + minutes(90), WHY, log, now=t0)
    opqueue.wait_on_owner(cfg, JOB, "which host", now=t0 + minutes(80))
    item = opqueue.resume(cfg, JOB, "the staging one", now=t0 + minutes(100))
    operator_mod.hold_lease(cfg, JOB, item.lease_until)

    operator_mod.sweep(cfg, log, now=t0 + minutes(161))

    item = opqueue.load(cfg, JOB)
    assert (item.state, item.attempts, item.lapses) == (opqueue.QUEUED, 1, 0)


def test_opening_a_put_back_job_by_hand_still_overrides_its_later_triage(cfg, log):
    """A supervisor started before this still reads the triage, so the CLI must set it."""
    running(cfg, log)
    opqueue.set_triage(cfg, JOB, when=opqueue.LATER, why="keeps", group="", source="test")
    opqueue.later(cfg, JOB, time.time() + minutes(90), "run started; read its result")

    assert cli(cfg, "operator", JOB).returncode == 0

    assert opqueue.load(cfg, JOB).triage["when"] == opqueue.NOW


def test_declared_waits_that_keep_running_out_stop_being_free(cfg, log):
    """A job that only ever declares a wait and hangs must still reach the cap."""
    opqueue.add(cfg, JOB, status="operator", note=NOTE)
    now = time.time()
    rounds = 0
    while opqueue.load(cfg, JOB).state != opqueue.ABANDONED:
        rounds += 1
        assert rounds <= opqueue.MAX_LAPSES + opqueue.MAX_ATTEMPTS, "never abandoned"
        assert operator_mod.dispatch(cfg, JOB, log) is True
        operator_mod.hold(cfg, JOB, now + minutes(90), WHY, log, now=now)
        now += minutes(91)
        operator_mod.sweep(cfg, log, now=now)
        _skip_the_cool_off(cfg, JOB)

    assert rounds == opqueue.MAX_LAPSES + opqueue.MAX_ATTEMPTS
    assert opqueue.load(cfg, JOB).lapses == opqueue.MAX_LAPSES


def _skip_the_cool_off(cfg, job: str) -> None:
    """``dispatch`` reads the real clock, so a requeued item is made due by hand."""
    item = opqueue.load(cfg, job)
    if item.state == opqueue.QUEUED:
        item.run_after = 0.0
        with opqueue._locked(cfg):
            opqueue._write(cfg, item)


def test_the_item_lease_and_the_state_lease_move_together(cfg, log):
    running(cfg, log)
    t0 = time.time()

    item = operator_mod.hold(cfg, JOB, t0 + minutes(90), WHY, log, now=t0)

    st = state_mod.read(cfg)
    assert item.lease_until == pytest.approx(t0 + minutes(90))
    assert st.operator_lease_until == item.lease_until == opqueue.load(cfg, JOB).lease_until
    # ... and they come back together: neither half is left for the other to trip on.
    operator_mod.sweep(cfg, log, now=t0 + minutes(91))
    assert state_mod.read(cfg).operator_phase is None
    assert opqueue.load(cfg, JOB).lease_until == 0.0


def test_a_hold_never_shortens_the_lease_and_never_passes_the_bound(cfg, log):
    before = running(cfg, log).lease_until
    t0 = time.time()

    short = operator_mod.hold(cfg, JOB, t0 + minutes(5), WHY, log, now=t0)
    assert short.lease_until == before  # ten minutes of work must not cost the hour

    far = operator_mod.hold(cfg, JOB, t0 + 30 * 3600, WHY, log, now=t0)
    assert far.lease_until == pytest.approx(t0 + opqueue.HOLD_MAX_S)
    assert state_mod.read(cfg).operator_lease_until == far.lease_until


def test_a_job_that_is_not_running_cannot_be_held(cfg, log):
    opqueue.add(cfg, JOB, status="operator", note=NOTE)  # queued, never opened

    assert operator_mod.hold(cfg, JOB, time.time() + minutes(90), WHY, log) is None
    assert opqueue.load(cfg, JOB).lease_until == 0.0


def test_an_answer_from_the_owner_does_not_cut_a_declared_wait_short(cfg, log):
    running(cfg, log)
    t0 = time.time()
    operator_mod.hold(cfg, JOB, t0 + minutes(180), WHY, log, now=t0)
    key = state_mod.waiter_key(state_mod.OPERATOR, JOB)
    assert cli(cfg, "waiting", key, "which host").returncode == 0

    assert cli(cfg, "resumed", key, "the staging one").returncode == 0

    item = opqueue.load(cfg, JOB)
    assert item.state == opqueue.RUNNING
    assert item.lease_until == pytest.approx(t0 + minutes(180))
    assert state_mod.read(cfg).operator_lease_until == item.lease_until


# -- the command ----------------------------------------------------------
def test_operator_hold_is_the_sessions_own_to_run(cfg, log, monkeypatch):
    running(cfg, log)
    before = opqueue.load(cfg, JOB).lease_until

    outside = cli(cfg, "operator-hold", JOB, "90m", WHY)
    monkeypatch.setenv(operator_mod.JOB_ENV, OTHER)
    another = cli(cfg, "operator-hold", JOB, "90m", WHY)

    assert outside.returncode == 1 and another.returncode == 1
    assert "only the session" in outside.stderr
    assert opqueue.load(cfg, JOB).lease_until == before


def test_operator_hold_moves_the_lease_and_says_until_when(cfg, log, monkeypatch):
    running(cfg, log)
    monkeypatch.setenv(operator_mod.JOB_ENV, JOB)

    result = cli(cfg, "operator-hold", JOB, "90m", "the", "image", "build", "is", "queued")

    assert result.returncode == 0, result.stderr
    item = opqueue.load(cfg, JOB)
    assert item.lease_until == pytest.approx(time.time() + minutes(90), abs=30)
    assert item.hold_why == "the image build is queued"
    assert state_mod.read(cfg).operator_lease_until == item.lease_until
    assert "until" in result.stdout
    assert f"OPERATOR-HOLD {JOB}" in cfg.supervisor_log.read_text()


def test_operator_hold_refuses_what_it_cannot_grant(cfg, log, monkeypatch):
    running(cfg, log)
    monkeypatch.setenv(operator_mod.JOB_ENV, JOB)
    before = opqueue.load(cfg, JOB).lease_until

    too_long = cli(cfg, "operator-hold", JOB, "2d", WHY)
    nonsense = cli(cfg, "operator-hold", JOB, "a while", WHY)
    no_reason = cli(cfg, "operator-hold", JOB, "90m", " ")

    assert (too_long.returncode, nonsense.returncode, no_reason.returncode) == (2, 2, 2)
    assert "--not-before" in too_long.stderr  # the way to wait longer than the bound
    assert opqueue.load(cfg, JOB).lease_until == before

    cli(cfg, "operator-done", JOB, "rolled")
    assert cli(cfg, "operator-hold", JOB, "90m", WHY).returncode == 1  # no live job


@pytest.mark.parametrize("clock, until", [
    ((2026, 10, 6, 12, 0), "13:30"),  # it ends today: the time alone
    ((2026, 10, 6, 23, 15), "2026-10-07 00:45"),  # past midnight: the day is said too
])
def test_status_shows_a_declared_wait_and_until_when(cfg, log, monkeypatch, capsys, clock, until):
    """The clock is pinned: 90 minutes from now is another day late in the
    evening, and the line then carries the date (``opqueue.hhmm``)."""
    at = time.mktime((*clock, 0, 0, 0, -1))
    monkeypatch.setattr(time, "time", lambda: at)
    project = ["--project-dir", str(cfg.project_dir)]
    running(cfg, log)
    monkeypatch.setenv(operator_mod.JOB_ENV, JOB)
    assert cli_main([*project, "operator-hold", JOB, "90m", WHY]) == 0
    assert opqueue.load(cfg, JOB).hold_until == pytest.approx(at + minutes(90))
    capsys.readouterr()

    assert cli_main([*project, "status"]) == 0

    out = capsys.readouterr().out
    assert f"current: {JOB} [running, at long work until {until}: {WHY}]" in out


# -- the session is not needed: the wait goes back to the queue -----------
def test_a_wait_handed_back_frees_the_window_and_keeps_the_attempt(cfg, log):
    running(cfg, log)

    result = cli(cfg, "operator-done", JOB, "run started 05:32, ends about 07:00;",
                 "read its result and record it", "--not-before", "90m")

    assert result.returncode == 0, result.stderr
    item = opqueue.load(cfg, JOB)
    assert (item.state, item.attempts) == (opqueue.QUEUED, 0)
    assert item.put_back_at > 0
    assert item.resume_note == (
        "run started 05:32, ends about 07:00; read its result and record it")


def test_the_note_of_a_handed_back_wait_survives_a_reopening_that_failed(cfg, log):
    """It is the next session's whole starting point, so one bad start must not eat it."""
    running(cfg, log)
    opqueue.later(cfg, JOB, time.time() - 1, "run started 05:32; read its result")
    with state_mod.transaction(cfg) as st:
        st.release_operator()
    assert operator_mod.dispatch(cfg, JOB, log) is True

    item = opqueue.release(cfg, JOB, "the operator session would not start")

    line = operator_mod.brief(cfg, item)
    assert "run started 05:32; read its result" in line
    assert "would not start" in line
    assert "\n" not in line


def test_the_brief_tells_the_reopened_session_where_the_last_one_stopped(cfg, log):
    running(cfg, log)

    item = opqueue.later(cfg, JOB, time.time() + 60, "run started 05:32; read its result")

    line = operator_mod.brief(cfg, item)
    assert "put this job back" in line
    assert line.count("run started 05:32; read its result") == 1  # said once, not twice
    assert "operator-hold" in line  # the other way to wait is named in the brief


def test_a_handed_back_wait_reopens_at_its_time_whatever_the_triage_said(cfg, log):
    """`later` said when to open it first. Mid-job, the time it gave itself rules."""
    running(cfg, log)
    opqueue.set_triage(cfg, JOB, when=opqueue.LATER, why="keeps", group="", source="test")
    with state_mod.transaction(cfg) as st:
        st.release_operator()
    opqueue.later(cfg, JOB, time.time() + minutes(90), "run started; read its result")

    assert operator_mod.sweep(cfg, log, room=lambda: False) is False  # not before its time
    _skip_the_cool_off(cfg, JOB)  # ... and then its time has come
    assert operator_mod.sweep(cfg, log, room=lambda: False) is True
    assert state_mod.read(cfg).operator_phase == JOB


def test_a_job_never_reopens_while_its_own_mirror_is_still_merging(cfg, log):
    """What it committed before the wait must be on main before it carries on."""
    job = "L7"  # a phase's own hand-off: its mirror is not named after the job
    assert operator_mod.mirror_name(job) != job
    running(cfg, log, job)
    opqueue.later(cfg, job, time.time() - 1, "run started; read its result")
    with state_mod.transaction(cfg) as st:
        st.release_operator()
        st.integ_push(operator_mod.mirror_name(job), operator_mod.INTEG_STATUS)

    assert operator_mod.sweep(cfg, log) is False

    with state_mod.transaction(cfg) as st:
        st.integ_queue.clear()
    assert operator_mod.sweep(cfg, log) is True


def test_status_shows_where_a_handed_back_job_stopped(cfg, log):
    running(cfg, log)
    cli(cfg, "operator-done", JOB, "run started 05:32; read its result", "--not-before", "90m")
    operator_mod.release(cfg, log, JOB)  # what the supervisor does on the poke

    out = cli(cfg, "status").stdout

    assert f"  {JOB} [not before " in out
    assert "run started 05:32; read its result" in out


def test_the_dashboard_shows_a_declared_wait_and_where_a_put_back_job_stopped(cfg, log):
    from rich.text import Text

    from swarm_orchestrator.tui import home

    running(cfg, log)
    held = operator_mod.hold(cfg, JOB, time.time() + minutes(90), WHY, log)
    at_work = Text.from_markup(home.job_detail(held)).plain
    put_back = opqueue.later(cfg, JOB, time.time() + minutes(90), "run started; read its result")
    waiting = Text.from_markup(home.job_detail(put_back)).plain

    assert f"at long work until {opqueue.hhmm(held.hold_until)}: {WHY}" in at_work
    assert "queued, not before" in waiting and "where it stopped" in waiting
    assert waiting.count("run started; read its result") == 1  # not again as an error


def test_the_operator_prompt_says_how_to_wait_longer_than_an_hour():
    text = (operator_mod.resolver.prompt_path("operator.md")).read_text(encoding="utf-8")

    assert "swarm operator-hold <job>" in text
    assert "--not-before" in text and "one hour" in text
