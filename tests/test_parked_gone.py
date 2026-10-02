"""A parked session that is gone is noticed, and settled by the watchdog's sweep.

A parked session holds no slot: it is in a tmux window of its own, and it leaves
``parked`` only by reporting. The sweep looked at busy slots, and ``swarm
doctor`` had no check for a parked key, so a parked worker that crashed, or whose
window was closed by hand, stayed for good. One the owner had answered read as
at work everywhere and a drain waited for it without end; one that still asked
stayed on the owner's list.

Here a real supervisor sweeps over a real state file, and the session is a real
process carrying the session's markers (or none, once it has died). Gone on two
sweeps in a row, the session is settled the way its kind is when it dies
anywhere else: a worker like one that died in its slot, an operator job back to
its queue, an Overseer pass as interrupted. The controls are a session that is
alive, one that has reported and whose work is landing, one a restart carries,
and one tmux cannot be asked about: each is left alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from swarm_orchestrator import doctor, gitq, opqueue, ovrecord, procs
from swarm_orchestrator import drain as drain_mod
from swarm_orchestrator import keep as keep_mod
from swarm_orchestrator import master as master_mod
from swarm_orchestrator import restart as restart_mod
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.supervisor import Supervisor

from test_doctor import log as stamped
from test_parked_working_drain import (  # noqa: F401 - ``cfg`` is the fixture
    ASKING,
    STILL_ASKING,
    WORKING,
    cfg,
    park,
    supervised,
)

OPERATOR_JOB = state_mod.waiter_key(state_mod.OPERATOR, "J1")
OVERSEER_PASS = state_mod.waiter_key(state_mod.OVERSEER, "20260101T000000Z")
WINDOW = "@7"


def session_process(cfg, key: str) -> subprocess.Popen:
    """A live process carrying the markers of the session ``key`` names."""
    kind, ident = state_mod.waiter(key)
    env = {"PATH": os.environ["PATH"], "SWARM_STATE_DIR": str(cfg.state_dir),
           procs.SESSION_ENV: f"{kind}:{ident}"}
    return subprocess.Popen(["sleep", "300"], env=env)


def end(child: subprocess.Popen | None) -> None:
    if child is not None:
        child.kill()
        child.wait()


@pytest.fixture
def run(cfg, monkeypatch):
    """Start a supervisor over ``cfg``'s state; every session it settles has its
    work kept, which is recorded in ``sup.aside``."""
    made: list[Supervisor] = []
    aside: list[str] = []
    monkeypatch.setattr(gitq, "set_aside", lambda c, phase, log: aside.append(phase))
    # Ending a session's leftovers is not the subject here, and off the loop's
    # thread it would outlive the test.
    monkeypatch.setattr(session_mod, "REAP_GRACE_S", 0.0)

    def start(*, isolation: str = "worktree", watchdog: int = 1) -> Supervisor:
        cfg.git_isolation = isolation
        cfg.watchdog_s = watchdog
        with state_mod.transaction(cfg) as st:
            st.supervisor_pid = os.getpid()
        sup = Supervisor(cfg)
        sup._bootstrapped = True
        sup.aside = aside
        made.append(sup)
        return sup

    yield start
    session_mod.join_reaps()
    for sup in made:
        sup.log.close()


def sweep(sup: Supervisor, times: int = 1) -> None:
    for _ in range(times):
        sup._last_sweep = 0.0
        sup._watchdog_tick()


def parked(cfg, key: str = "P1", mark: str = WORKING, window: str | None = None) -> None:
    with state_mod.transaction(cfg) as st:
        park(st, key, mark)
        if window:
            st.windows[state_mod.wait_window(key)] = window


def logged(cfg) -> str:
    return cfg.supervisor_log.read_text(encoding="utf-8")


def tg(cfg) -> list[str]:
    path = Path(os.environ["SWARM_TG_SINK"])
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


# -- a worker ----------------------------------------------------------------------
def test_a_drain_ends_once_the_sweep_has_seen_a_dead_parked_worker_twice(cfg, run, monkeypatch):
    spawned: list = []
    monkeypatch.setattr(drain_mod, "spawn_down", lambda c: spawned.append(c) or True)
    parked(cfg)
    child = session_process(cfg, "P1")
    try:
        with state_mod.transaction(cfg) as st:
            st.drain = {"since": time.time(), "then": ""}
        sup = run()
        sweep(sup)
        sup._drain_tick()
        assert state_mod.read(cfg).drain["waiting"] == ["1 worker (P1 in its own window)"]
        assert "WATCHDOG-SUSPECT" not in logged(cfg)  # it is alive: nothing to suspect
    finally:
        end(child)  # the session dies in its window

    sweep(sup)
    sup._drain_tick()
    st = state_mod.read(cfg)
    assert st.parked == ["P1"] and not spawned, "settled on a single sighting"
    assert "WATCHDOG-SUSPECT P1 parked: the worker on P1" in logged(cfg)

    sweep(sup)
    sup._drain_tick()
    st = state_mod.read(cfg)
    assert st.parked == [] and "P1" not in st.answered and "P1" not in st.asked
    assert "P1" not in st.done  # not recorded: the phase is started again later
    assert sup.aside == ["P1"]  # its work is kept for that
    text = logged(cfg)
    assert "WATCHDOG-REAP-PARKED P1 the worker on P1 (no process of its session is left)" in text
    assert "WATCHDOG-REAP P1 parked-gone" in text
    assert spawned and st.drain.get("stopping_at"), text  # the drain has nothing left to wait for
    assert "DRAIN-COMPLETE" in text
    assert any("the worker for P1 stopped without finishing" in ln for ln in tg(cfg))


def test_a_parked_worker_that_is_alive_is_left_alone(cfg, run, monkeypatch):
    spawned: list = []
    monkeypatch.setattr(drain_mod, "spawn_down", lambda c: spawned.append(c) or True)
    parked(cfg)
    with state_mod.transaction(cfg) as st:
        st.drain = {"since": time.time(), "then": ""}
    child = session_process(cfg, "P1")
    try:
        sup = run()
        sweep(sup, 3)
        sup._drain_tick()
        st = state_mod.read(cfg)
        assert st.parked == ["P1"] and st.working_parked() == ["P1"]
        assert st.drain["waiting"] == ["1 worker (P1 in its own window)"] and not spawned
        assert child.poll() is None
    finally:
        end(child)
    assert sup.aside == [] and "WATCHDOG-SUSPECT" not in logged(cfg)


@STILL_ASKING
def test_a_dead_parked_worker_that_still_asks_leaves_the_owners_list(cfg, run, mark):
    parked(cfg, mark=mark)
    before = state_mod.read(cfg)
    assert before.on_owner() == ["P1"]
    assert "you are the blocker" in doctor._check_owner(cfg, before).detail
    sup = run()
    sweep(sup, 2)
    st = state_mod.read(cfg)
    assert st.on_owner() == [] and st.parked == [] and "P1" not in st.asked
    assert restart_mod.questions(cfg, st) == []
    assert doctor._check_owner(cfg, st).detail == "no worker is waiting on you"
    assert "WATCHDOG-REAP P1 parked-gone" in logged(cfg)
    # Nothing holds it any more, so the launcher takes it up again.
    assert "P1" in (master_mod.build_context(cfg, st)["ready"] + getattr(sup, "stub_launches", []))


@pytest.mark.parametrize("how", ["queued to merge", "sentinel"])
def test_a_parked_worker_that_has_reported_is_landing_not_gone(cfg, run, how):
    """``swarm done`` ends the worker's session on purpose; its key stays in
    ``parked`` until its work has landed, which is minutes."""
    parked(cfg)
    if how == "sentinel":
        stamped(cfg, (3600, "CLAIM P1 slot=0"))
        cfg.done_dir.mkdir(parents=True, exist_ok=True)
        (cfg.done_dir / "P1.ok").write_text("", encoding="utf-8")
    else:
        with state_mod.transaction(cfg) as st:
            st.integ_push("P1", "ok")
    sup = run()
    sup._pump_integrations = lambda: None  # the queue is not the subject
    assert doctor._check_parked(cfg, state_mod.read(cfg)).status == doctor.OK
    sweep(sup, 3)
    assert state_mod.read(cfg).parked == ["P1"]
    assert sup.aside == [] and "WATCHDOG-SUSPECT" not in logged(cfg)


def test_the_two_sightings_must_follow_each_other(cfg, run):
    parked(cfg)
    sup = run()
    sweep(sup)  # gone
    child = session_process(cfg, "P1")
    try:
        sweep(sup)  # there again: the count starts over
        assert state_mod.read(cfg).parked == ["P1"]
    finally:
        end(child)
    sweep(sup)  # gone, a first sighting again
    assert state_mod.read(cfg).parked == ["P1"] and sup.aside == []
    sweep(sup)
    assert state_mod.read(cfg).parked == [] and sup.aside == ["P1"]


def test_a_session_a_restart_is_carrying_is_not_gone(cfg, run):
    """Between ``carry_out`` and ``carry_in`` a kept session is nobody's to end."""
    parked(cfg)
    markers = list(session_mod.session_markers(cfg, "worker", "P1"))
    restart_mod.kept_path(cfg).write_text(
        json.dumps({"sessions": [{"key": "P1", "markers": markers}]}), encoding="utf-8")
    sup = run()
    sweep(sup, 3)
    assert state_mod.read(cfg).parked == ["P1"] and "WATCHDOG-SUSPECT" not in logged(cfg)


def test_a_kept_process_does_not_keep_its_session_alive(cfg, monkeypatch):
    """``swarm keep`` is how a process outlives its session, so it says nothing
    about whether the session is still there."""
    markers = session_mod.session_markers(cfg, "worker", "P1")
    child = session_process(cfg, "P1")
    try:
        assert session_mod.session_alive(cfg, markers) == {child.pid}
        monkeypatch.setattr(keep_mod, "live_pids", lambda c: {child.pid})
        assert session_mod.session_alive(cfg, markers) == set()
    finally:
        end(child)


def test_a_parked_session_asking_about_itself_is_not_gone(cfg):
    """``swarm doctor`` typed inside a parked session is a process of that
    session, and so are the shells above it: the session is there."""
    parked(cfg)
    code = (
        "import sys\n"
        "from swarm_orchestrator import doctor, state\n"
        "from swarm_orchestrator.config import load\n"
        "cfg = load(project_dir=sys.argv[1])\n"
        "print([g.key for g in doctor.parked_probe(cfg, state.read(cfg))[0]])\n"
    )
    env = {**os.environ, procs.SESSION_ENV: "worker:P1"}
    inside = subprocess.run([sys.executable, "-c", code, str(cfg.project_dir)],
                            env=env, capture_output=True, text=True, check=True)
    assert inside.stdout.strip() == "[]"
    assert [g.key for g in doctor.parked_probe(cfg, state_mod.read(cfg))[0]] == ["P1"]


# -- an operator job, an Overseer pass ---------------------------------------------
def leased_job(cfg) -> None:
    """Operator job J1, taken by a session: one attempt used, its lease running."""
    cfg.operator_enabled = True
    opqueue.add(cfg, "J1", status="ok", note="roll the gateway")
    assert opqueue.lease(cfg, "J1") is not None


def test_a_dead_parked_operator_job_goes_back_to_its_queue(cfg, run):
    leased_job(cfg)
    parked(cfg, OPERATOR_JOB)
    sup = run()
    sweep(sup)
    assert state_mod.read(cfg).parked == [OPERATOR_JOB]
    sweep(sup)
    st = state_mod.read(cfg)
    assert st.parked == [] and OPERATOR_JOB not in st.answered
    item = opqueue.load(cfg, "J1")
    assert item.state == opqueue.QUEUED and item.attempts == 1
    assert item.last_error == "the operator session stopped without finishing"
    text = logged(cfg)
    assert f"WATCHDOG-REAP-PARKED {OPERATOR_JOB} operator job J1" in text
    assert f"UNPARK {OPERATOR_JOB}" in text


def test_a_dead_parked_operator_job_that_had_finished_is_ended_as_done(cfg, run):
    """Its ``operator-done`` settled the item and the poke was lost."""
    leased_job(cfg)
    opqueue.complete(cfg, "J1", "rolled")
    parked(cfg, OPERATOR_JOB)
    sup = run()
    sweep(sup, 2)
    assert state_mod.read(cfg).parked == []
    assert opqueue.load(cfg, "J1").state == opqueue.DONE
    assert "EVENT operator-done J1" in logged(cfg)


def test_a_dead_parked_overseer_pass_ends_as_interrupted(cfg, run):
    pid = state_mod.waiter(OVERSEER_PASS)[1]
    ovrecord.overseer_dir(cfg).mkdir(parents=True, exist_ok=True)
    ovrecord.create(cfg, pid, [], None)
    parked(cfg, OVERSEER_PASS)
    sup = run()
    sweep(sup, 2)
    st = state_mod.read(cfg)
    assert st.parked == [] and st.live_passes() == set()
    assert ovrecord.load_json(cfg, pid).status == ovrecord.INTERRUPTED
    text = logged(cfg)
    assert f"WATCHDOG-REAP-PARKED {OVERSEER_PASS} Overseer pass {pid}" in text
    assert f"OVERSEER-PASS-END {pid} interrupted" in text


@pytest.mark.parametrize("key", [OPERATOR_JOB, OVERSEER_PASS])
def test_a_parked_job_or_pass_that_is_alive_is_left_alone(cfg, run, key):
    parked(cfg, key)
    child = session_process(cfg, key)
    try:
        sup = run()
        sweep(sup, 3)
        assert state_mod.read(cfg).parked == [key]
    finally:
        end(child)
    assert "WATCHDOG-SUSPECT" not in logged(cfg)


# -- in a tmux window --------------------------------------------------------------
class _R:
    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


def tmux_answers(monkeypatch, listing: str | None, calls: list | None = None) -> None:
    """Stand-in for ``tmux run``: the session's panes are ``listing`` (lines of
    ``<window> <dead>``), or tmux gives no answer when it is ``None``."""

    def fake(args, check=False, input_text=None):
        if calls is not None:
            calls.append(list(args))
        if args[0] == "list-panes":
            return _R(1) if listing is None else _R(0, listing)
        return _R(0, "")

    monkeypatch.setattr("swarm_orchestrator.tmux.run", fake)


@pytest.mark.parametrize("listing, why", [
    ("@1 0\n", "its window wait:P1 is gone"),
    ("@1 0\n@7 1\n", "nothing runs in its window wait:P1"),
])
def test_a_parked_worker_whose_window_is_gone_or_empty_is_settled(
    cfg, run, monkeypatch, listing, why
):
    """A window closed by hand can leave something of the session running with
    no pane at all. It is gone all the same, as a slot whose pane is gone is,
    and what is left of it is ended before its work is saved."""
    monkeypatch.setenv("SWARM_DRIVER", "tmux")
    cfg.driver = "tmux"
    calls: list[list[str]] = []
    tmux_answers(monkeypatch, listing, calls)
    parked(cfg, window=WINDOW)
    child = session_process(cfg, "P1")
    try:
        sup = run()
        sweep(sup, 2)
        st = state_mod.read(cfg)
        assert st.parked == [] and state_mod.wait_window("P1") not in st.windows
        assert child.wait(timeout=10) is not None  # ended with its session
    finally:
        end(child)
    assert sup.aside == ["P1"]
    assert f"WATCHDOG-REAP-PARKED P1 the worker on P1 ({why})" in logged(cfg)
    assert ["kill-window", "-t", WINDOW] in calls


def test_a_parked_worker_in_a_live_window_is_judged_by_its_processes(cfg, run, monkeypatch):
    monkeypatch.setenv("SWARM_DRIVER", "tmux")
    cfg.driver = "tmux"
    tmux_answers(monkeypatch, "@7 0\n")
    parked(cfg, window=WINDOW)
    child = session_process(cfg, "P1")
    try:
        sup = run()
        sweep(sup, 3)
        assert state_mod.read(cfg).parked == ["P1"]
    finally:
        end(child)
    sweep(sup, 2)
    assert state_mod.read(cfg).parked == []
    assert "(no process of its session is left)" in logged(cfg)


def test_nothing_is_settled_while_tmux_cannot_answer(cfg, run, monkeypatch):
    """With no answer every window would look gone: nothing may be inferred."""
    monkeypatch.setenv("SWARM_DRIVER", "tmux")
    cfg.driver = "tmux"
    tmux_answers(monkeypatch, None)
    parked(cfg, window=WINDOW)
    sup = run()
    sweep(sup, 3)
    assert state_mod.read(cfg).parked == ["P1"] and sup.aside == []
    check = doctor._check_parked(cfg, state_mod.read(cfg))
    assert check.status == doctor.OK and "1 unreadable" in check.detail


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not available")
def test_the_rule_reads_a_real_tmux(cfg, monkeypatch):
    """A real parked pane on an isolated server (its own ``TMUX_TMPDIR``, killed
    after): alive in its window, then its process killed (the pane stays, dead),
    then the window closed, then no server to ask."""
    from swarm_orchestrator import tmux

    sockdir = tempfile.mkdtemp(prefix="pgone-")
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)  # never nest onto the outer server
    monkeypatch.setenv("SWARM_DRIVER", "tmux")
    cfg.driver = "tmux"
    markers = {"SWARM_STATE_DIR": str(cfg.state_dir), procs.SESSION_ENV: "worker:P1"}

    def found():
        st = state_mod.read(cfg)
        return doctor._parked_session(cfg, st, "P1", doctor.window_states(cfg))

    try:
        win = tmux.new_session(cfg.session)
        tmux.harden(cfg.session)
        pane, _sibling = tmux.split_layout(win, 2)
        tmux.respawn_pane(pane, "exec sleep 300", env=markers)
        wait_win, _replacement = tmux.park_pane(win, pane, 0, "wait:P1", cfg.session)
        parked(cfg, window=wait_win)
        assert doctor.window_states(cfg)[wait_win] is True
        assert found() == doctor._WORKING

        pid = int(tmux.run(["display-message", "-p", "-t", pane, "#{pane_pid}"]).stdout)
        os.kill(pid, 9)
        assert _eventually(lambda: doctor.window_states(cfg)[wait_win] is False)
        gone = found()
        assert isinstance(gone, doctor.Gone)
        assert gone.what == "the worker on P1 (nothing runs in its window wait:P1)"

        tmux.kill_window(wait_win)
        states = doctor.window_states(cfg)
        assert states and wait_win not in states  # the session's other window answers
        assert found().what == "the worker on P1 (its window wait:P1 is gone)"

        tmux.run(["kill-server"])
        assert doctor.window_states(cfg) is None
        assert found() == doctor._UNREADABLE
    finally:
        tmux.run(["kill-server"])  # the isolated server only
        shutil.rmtree(sockdir, ignore_errors=True)


def _eventually(pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


# -- swarm doctor ------------------------------------------------------------------
def test_doctor_is_quiet_about_a_parked_session_that_is_there(cfg):
    assert doctor._check_parked(cfg, state_mod.read(cfg)) == doctor.Check(
        "parked.sessions", doctor.OK, "no parked sessions")
    parked(cfg)
    child = session_process(cfg, "P1")
    try:
        check = doctor._check_parked(cfg, state_mod.read(cfg))
    finally:
        end(child)
    assert check == doctor.Check(
        "parked.sessions", doctor.OK, "1 parked session(s), all still there")


def test_doctor_warns_of_a_dead_parked_session_the_sweep_will_settle(cfg, monkeypatch):
    supervised(cfg, monkeypatch)
    cfg.watchdog_s = 300
    parked(cfg)
    with state_mod.transaction(cfg) as st:
        st.supervisor_pid = os.getpid()
    check = doctor._check_parked(cfg, state_mod.read(cfg))
    assert check.status == doctor.WARN and check.fix_hint is None
    assert "the worker on P1 (no process of its session is left)" in check.detail
    assert "sweep (300s) settles it on its second sighting" in check.detail
    assert doctor.exit_code([check]) == 0


@pytest.mark.parametrize("why", ["an earlier supervisor", "no supervisor", "watchdog off"])
def test_doctor_fails_for_a_dead_parked_session_nothing_will_settle(cfg, monkeypatch, why):
    """What the sweep will do is said only of a supervisor that told of it: the
    command runs the code on disk, the supervisor the code it started on."""
    cfg.watchdog_s = 0 if why == "watchdog off" else 300
    if why != "no supervisor":
        earlier = ("handover", "restart-at", "keep-later", "reap-stopped", "drain-parked")
        supervised(cfg, monkeypatch, caps=earlier if why == "an earlier supervisor" else None)
        with state_mod.transaction(cfg) as st:
            st.supervisor_pid = os.getpid()
    parked(cfg)
    check = doctor._check_parked(cfg, state_mod.read(cfg))
    assert check.status == doctor.FAIL and "settles it on" not in check.detail
    assert "the worker on P1 (no process of its session is left)" in check.detail
    if why == "watchdog off":
        assert "the watchdog is off" in check.detail
        assert check.fix_hint.startswith("swarm free P1")
    elif why == "no supervisor":
        assert "no supervisor is running" in check.detail
        assert check.fix_hint.startswith("swarm up")
    else:
        assert "`swarm restart` loads the rule" in check.detail
        assert check.fix_hint.startswith("swarm restart")


@pytest.mark.parametrize("case", ["dead", "alive", "landing", "carried", "asking"])
def test_doctor_says_settles_of_exactly_the_sessions_the_sweep_settles(
    cfg, run, monkeypatch, case
):
    supervised(cfg, monkeypatch)
    parked(cfg, mark=ASKING if case == "asking" else WORKING)
    if case == "landing":
        with state_mod.transaction(cfg) as st:
            st.integ_push("P1", "ok")
    if case == "carried":
        markers = list(session_mod.session_markers(cfg, "worker", "P1"))
        restart_mod.kept_path(cfg).write_text(
            json.dumps({"sessions": [{"key": "P1", "markers": markers}]}), encoding="utf-8")
    child = session_process(cfg, "P1") if case == "alive" else None
    try:
        sup = run(watchdog=300)
        sup._pump_integrations = lambda: None  # the queue is not the subject
        said = doctor._check_parked(cfg, state_mod.read(cfg))
        sup.watchdog_s = 1
        sweep(sup, 2)
        settled = state_mod.read(cfg).parked == []
    finally:
        end(child)
    assert settled == (case in ("dead", "asking"))
    assert ("settles it on its second sighting" in said.detail) == settled, said.detail
    assert (said.status == doctor.WARN) == settled


def test_doctor_does_not_fail_in_the_moment_a_restart_swaps_the_supervisor(cfg):
    """``swarm restart`` holds the pipe between two supervisors, and the next
    one starts on the code on disk: its sweep settles the session."""
    cfg.watchdog_s = 300
    parked(cfg)
    restart_mod.save(cfg, {"id": "r1", "stage": restart_mod.RESTARTING,
                           "runner_pid": os.getpid()})
    check = doctor._check_parked(cfg, state_mod.read(cfg))
    assert check.status == doctor.WARN and "settles it on its second sighting" in check.detail


def test_the_supervisor_says_in_its_mark_that_it_settles_parked_sessions(cfg):
    restart_mod.mark_supervisor(cfg, adopted=False)
    assert "reap-parked" in restart_mod.supervisor_mark(cfg)["caps"]
    assert restart_mod.capable(cfg, os.getpid(), "reap-parked")


def test_doctor_lists_the_check_in_its_run(cfg, monkeypatch):
    monkeypatch.setattr(doctor, "_dir_size", lambda path: 0)
    parked(cfg)
    check = next(c for c in doctor.run_checks(cfg) if c.name == "parked.sessions")
    assert check.status == doctor.FAIL  # no supervisor runs here: nothing settles it


# -- end to end ---------------------------------------------------------------------
def _session_pids(swarm, phase: str) -> list[int]:
    """The live processes of ``phase``'s worker session in this run."""
    run = f"SWARM_STATE_DIR={swarm.state_dir}".encode()
    want = f"{procs.SESSION_ENV}=worker:{phase}".encode()
    return [pid for pid in procs.table()
            if run in (env := procs.environ(pid)) and want in env]


def test_a_drain_stops_the_swarm_after_its_parked_worker_died(swarm):
    """The real supervisor, its real loop: the drain that waited for a parked
    worker at work ends by itself once that worker's process is gone."""
    (swarm.project / "ledger.txt").write_text("W0\nW1\n", encoding="utf-8")
    swarm.env["FAKE_WORKER_SLEEP"] = "300"
    swarm.env["SWARM_PARK_AFTER"] = "1"
    swarm.env["SWARM_WATCHDOG"] = "1"
    swarm.up()
    assert swarm.wait(lambda: swarm.busy_phases() == ["W0", "W1"], timeout=30), swarm.log_text()
    swarm.cli("waiting", "W1", "drop the legacy column, yes or no?")
    assert swarm.wait(lambda: swarm.state()["parked"] == ["W1"], timeout=20), swarm.log_text()
    swarm.cli("resumed", "W1", "yes, drop it")
    assert swarm.wait(lambda: "W1" in (swarm.state().get("answered") or {}), timeout=10)
    supervisor = swarm.state()["supervisor_pid"]

    swarm.cli("down", "--drain")
    swarm.cli("done", "W0", "ok")
    waits = ["1 worker (W1 in its own window)"]
    assert swarm.wait(lambda: swarm.state()["drain"].get("waiting") == waits, timeout=20), (
        swarm.log_text())
    time.sleep(3.5)  # three sweeps with the worker alive: it is left alone
    st = swarm.state()
    assert st["parked"] == ["W1"] and st["drain"].get("waiting") == waits
    assert "WATCHDOG-SUSPECT W1" not in swarm.log_text() and procs.alive(supervisor)

    pids = _session_pids(swarm, "W1")
    assert pids, "the parked worker has no process to kill"
    for pid in pids:
        os.kill(pid, 9)
    assert swarm.wait(lambda: "DRAIN-COMPLETE" in swarm.log_text(), timeout=30), swarm.log_text()
    text = swarm.log_text()
    assert "WATCHDOG-SUSPECT W1 parked: the worker on W1" in text
    assert "WATCHDOG-REAP-PARKED W1 the worker on W1" in text
    assert "WATCHDOG-REAP W1 parked-gone" in text
    assert text.index("WATCHDOG-REAP W1 parked-gone") < text.index("DRAIN-COMPLETE")
    assert swarm.wait(lambda: not procs.alive(supervisor), timeout=60), swarm.log_text()
    st = swarm.state()
    assert st["parked"] == [] and "W1" not in st["done"]
