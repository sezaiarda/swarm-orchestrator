"""A stop waits for a parked session that is at work on the owner's answer.

A parked session holds no slot, so a drain that counts busy slots never saw it.
Once the owner has answered it (``State.answered``) it is working again in its
own window, and ``swarm down --drain`` or a full restart closed it mid-work;
under ``--wait-questions`` the drain counted it as a question though it asked
nothing. Here a real supervisor drains over a real state file: the session at
work holds the stop like any worker and is named, and a full restart no longer
refuses for it once a supervisor that waits for it is running.

The controls are the two shapes that still ask (a recorded question, and a state
file from before the marks): neither holds a stop, as before.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from swarm_orchestrator import drain as drain_mod
from swarm_orchestrator import procs
from swarm_orchestrator import restart as restart_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.state import State
from swarm_orchestrator.supervisor import Supervisor

LEDGER = "- [ ] `P0` · needs:—\n- [ ] `P1` · needs:—\n- [ ] `P2` · needs:—\n"
PARK_AFTER = 120

#: The three shapes a parked session's marks can be in.
WORKING = "answered"
ASKING = "asked"
UNMARKED = "a state file from before the marks"
STILL_ASKING = pytest.mark.parametrize("mark", [ASKING, UNMARKED])


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    for leak in ("SWARM_GIT_ISOLATION", "SWARM_PARK_AFTER"):
        monkeypatch.delenv(leak, raising=False)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    cfg = load(project_dir=str(tmp_path))
    state_mod.init_state(cfg)
    return cfg


def park(st: State, key: str, mark: str, now: float | None = None) -> None:
    """``key`` asked and was parked, and is now in the shape ``mark`` names."""
    now = time.time() if now is None else now
    if state_mod.waiter(key)[0] == state_mod.WORKER:
        st.claim_slot(key)
    st.waiting[key] = now - 600 + PARK_AFTER
    st.park(key, PARK_AFTER)
    if mark == WORKING:
        st.answer(key, now - 300)
    elif mark == UNMARKED:
        st.asked.clear()
    assert key in st.parked and st.asking(key) == (mark != WORKING)


def draining(cfg, monkeypatch, mark: str, *, busy=(), questions: str | None = None):
    """A supervisor over a run that drains: ``busy`` in slots, P1 parked as ``mark``."""
    spawned: list = []
    monkeypatch.setattr(drain_mod, "spawn_down", lambda c: spawned.append(c) or True)
    with state_mod.transaction(cfg) as st:
        for phase in busy:
            st.claim_slot(phase)
        park(st, "P1", mark)
        st.drain = {"since": time.time(), "then": ""}
        if questions:
            st.drain["questions"] = questions
    sup = Supervisor(cfg)
    sup._bootstrapped = True
    return sup, spawned


# -- what the stop waits for, in words ----------------------------------------------
def test_a_parked_worker_at_work_is_a_worker_the_stop_waits_for_and_names(cfg):
    st = State.fresh(3)
    park(st, "P1", WORKING)
    assert drain_mod.waiting_for(cfg, st) == ["1 worker (P1 in its own window)"]
    st.claim_slot("P0")
    assert drain_mod.waiting_for(cfg, st, launching=1) == ["3 workers (P1 in its own window)"]
    park(st, "P2", WORKING)
    assert drain_mod.waiting_for(cfg, st) == ["3 workers (P1 and P2 in their own windows)"]
    assert drain_mod.line({"waiting": drain_mod.waiting_for(cfg, st)}) == (
        "Draining: waiting for 3 workers (P1 and P2 in their own windows), then stop")


@STILL_ASKING
def test_a_parked_worker_that_asks_does_not_hold_the_stop(cfg, mark):
    st = State.fresh(3)
    park(st, "P1", mark)
    assert drain_mod.waiting_for(cfg, st) == []
    st.claim_slot("P0")
    assert drain_mod.waiting_for(cfg, st) == ["1 worker"]


def test_a_parked_worker_that_asks_again_stops_holding_the_stop(cfg):
    st = State.fresh(3)
    park(st, "P1", WORKING)
    assert drain_mod.waiting_for(cfg, st) == ["1 worker (P1 in its own window)"]
    st.ask("P1", time.time())
    assert drain_mod.waiting_for(cfg, st) == []


def test_a_parked_operator_job_or_pass_at_work_holds_the_stop_too(cfg):
    job = state_mod.waiter_key(state_mod.OPERATOR, "J1")
    ovs = state_mod.waiter_key(state_mod.OVERSEER, "ovs-1")
    st = State.fresh(1)
    park(st, job, WORKING)
    park(st, ovs, WORKING)
    assert drain_mod.waiting_for(cfg, st) == [
        "operator job J1 in its own window", "an Overseer pass in its own window"]
    st.ask(job, time.time())
    st.ask(ovs, time.time())
    assert drain_mod.waiting_for(cfg, st) == []


def test_waiting_for_the_questions_does_not_call_a_session_at_work_a_question(cfg):
    st = State.fresh(3)
    st.drain = {"questions": restart_mod.WAIT}
    park(st, "P1", WORKING)
    assert drain_mod.waiting_for(cfg, st) == ["1 worker (P1 in its own window)"]
    st.claim_slot("P0")
    st.waiting["P0"] = time.time() + 60
    assert drain_mod.waiting_for(cfg, st) == ["1 worker (P1 in its own window)", "1 question"]


@STILL_ASKING
def test_waiting_for_the_questions_counts_a_parked_session_that_asks(cfg, mark):
    st = State.fresh(3)
    st.drain = {"questions": restart_mod.WAIT}
    park(st, "P1", mark)
    assert drain_mod.waiting_for(cfg, st) == ["1 question"]


# -- the supervisor --------------------------------------------------------------------
def test_a_drain_waits_for_a_parked_worker_at_work_and_stops_once_it_is_done(cfg, monkeypatch):
    sup, spawned = draining(cfg, monkeypatch, WORKING)

    sup._drain_tick()
    st = state_mod.read(cfg)
    assert st.drain["waiting"] == ["1 worker (P1 in its own window)"]
    assert not st.drain.get("stopping_at") and not spawned
    assert drain_mod.line(st.drain) == (
        "Draining: waiting for 1 worker (P1 in its own window), then stop")
    assert "DRAIN-WAITING 1 worker (P1 in its own window)" in cfg.supervisor_log.read_text()

    sup._advance_done("P1", "ok")  # it reports done from its own window
    sup._drain_tick()

    st = state_mod.read(cfg)
    assert st.parked == [] and st.answered == {}
    assert st.drain["stopping_at"] and st.drain["waiting"] == []
    assert len(spawned) == 1


def test_a_drain_waits_for_the_slots_and_the_parked_worker_alike(cfg, monkeypatch):
    sup, spawned = draining(cfg, monkeypatch, WORKING, busy=("P0",))
    sup._drain_tick()
    assert state_mod.read(cfg).drain["waiting"] == ["2 workers (P1 in its own window)"]
    sup._advance_done("P0", "ok")
    sup._drain_tick()
    assert state_mod.read(cfg).drain["waiting"] == ["1 worker (P1 in its own window)"]
    assert not spawned


@STILL_ASKING
def test_a_drain_does_not_wait_for_a_parked_worker_that_asks(cfg, monkeypatch, mark):
    sup, spawned = draining(cfg, monkeypatch, mark)
    sup._drain_tick()
    st = state_mod.read(cfg)
    assert st.drain["stopping_at"] and st.drain["waiting"] == []
    assert st.parked == ["P1"] and len(spawned) == 1


def test_a_drain_that_waits_for_questions_names_the_session_at_work_as_a_worker(
        cfg, monkeypatch):
    sup, spawned = draining(cfg, monkeypatch, WORKING, questions=restart_mod.WAIT)
    sup._drain_tick()
    assert state_mod.read(cfg).drain["waiting"] == ["1 worker (P1 in its own window)"]
    assert not spawned


# -- swarm restart ---------------------------------------------------------------------
def supervised(cfg, monkeypatch, caps=None) -> int:
    """This process stands in for the run's supervisor: one started on this code
    (it says all it can do), or an earlier one that said only ``caps``."""
    pid = os.getpid()
    monkeypatch.setattr(restart_mod, "live_supervisor", lambda cfg, st=None: pid)
    restart_mod.mark_supervisor(cfg, adopted=False)
    if caps is not None:
        mark = {"pid": pid, "ticks": procs.start_ticks(pid), "caps": list(caps)}
        (Path(cfg.state_dir) / restart_mod.MARK_FILE).write_text(json.dumps(mark))
    return pid


def parked_run(cfg, mark: str) -> State:
    """P0 in a slot, P1 parked in the shape ``mark`` names."""
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
        park(st, "P1", mark)
    return state_mod.read(cfg)


def test_a_restart_counts_a_parked_worker_at_work_as_a_worker(cfg):
    st = parked_run(cfg, WORKING)
    assert restart_mod.counts(st) == (2, 0)
    assert restart_mod.counts_of(json.loads(cfg.state_path.read_text())) == (2, 0)
    plan = restart_mod.new_plan(cfg, restart_mod.FULL, time.time() + 3600, "owner terminal")
    assert "waits for 2 workers" in restart_mod.line(plan, *restart_mod.counts(st))


@STILL_ASKING
def test_a_restart_counts_a_parked_worker_that_asks_as_a_question(cfg, mark):
    st = parked_run(cfg, mark)
    assert restart_mod.counts(st) == (1, 1)
    assert restart_mod.counts_of(json.loads(cfg.state_path.read_text())) == (1, 1)


def test_a_restart_does_not_count_a_parked_operator_job_at_work_as_a_worker(cfg):
    with state_mod.transaction(cfg) as st:
        park(st, state_mod.waiter_key(state_mod.OPERATOR, "J1"), WORKING)
    assert restart_mod.counts(state_mod.read(cfg)) == (0, 0)
    assert restart_mod.counts_of(json.loads(cfg.state_path.read_text())) == (0, 0)


def test_a_full_restart_has_nothing_to_refuse_for_once_the_drain_waits_for_it(
        cfg, monkeypatch):
    supervised(cfg, monkeypatch)
    st = parked_run(cfg, WORKING)
    assert restart_mod.questions(cfg, st) == []


def test_a_full_restart_still_names_it_under_a_supervisor_whose_drain_would_close_it(
        cfg, monkeypatch):
    """The command runs the code on disk, the supervisor the code it started
    with: one from before this rule does not wait, so the session is still
    listed as one a stop would close."""
    supervised(cfg, monkeypatch, caps=("handover", "restart-at", "keep-later", "reap-stopped"))
    st = parked_run(cfg, WORKING)
    (q,) = restart_mod.questions(cfg, st)
    assert (q.key, q.parked, q.asking) == ("P1", True, False)


@STILL_ASKING
def test_a_full_restart_lists_a_parked_session_that_asks_whatever_the_supervisor(
        cfg, monkeypatch, mark):
    supervised(cfg, monkeypatch)
    (q,) = restart_mod.questions(cfg, parked_run(cfg, mark))
    assert (q.key, q.parked, q.asking) == ("P1", True, True)


def test_a_full_restart_loads_a_supervisor_that_waits_before_it_drains(cfg, monkeypatch):
    """Told to go ahead under a supervisor that would not wait for the session
    at work, a full restart replaces the supervisor first, then drains."""
    supervised(cfg, monkeypatch, caps=("handover", "restart-at", "keep-later", "reap-stopped"))
    parked_run(cfg, WORKING)
    loaded: list = []

    def in_place(c, plan, log):
        loaded.append(plan["id"])
        return restart_mod.OK, "supervisor replaced"

    monkeypatch.setattr(restart_mod, "in_place", in_place)
    monkeypatch.setattr(restart_mod, "has_reader", lambda c: True)
    monkeypatch.setattr(restart_mod, "_poke", lambda c, verb: True)
    plan = restart_mod.save(cfg, restart_mod.new_plan(
        cfg, restart_mod.FULL, time.time(), "owner terminal", questions=restart_mod.FORCE))
    log = Log(cfg.supervisor_log)
    try:
        assert restart_mod._run_full(cfg, plan, log) == 0
    finally:
        log.close()
    assert loaded == [plan["id"]]
    st = state_mod.read(cfg)
    assert st.drain["restart"] == plan["id"] and st.drain["questions"] == restart_mod.FORCE
    assert st.parked == ["P1"] and st.answered  # nothing was closed


# -- end to end ------------------------------------------------------------------------
def _plan(swarm) -> dict:
    path = swarm.state_dir / restart_mod.PLAN_FILE
    return json.loads(path.read_text()) if path.is_file() else {}


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2:][:1] not in ("Z", "X")


def test_a_full_restart_waits_for_a_parked_worker_at_work_instead_of_refusing(swarm):
    # Four slots: W0..W3 start, W1 asks and is parked, W4 takes its slot, W5 waits.
    (swarm.project / "ledger.txt").write_text(
        "".join(f"W{i}\n" for i in range(6)), encoding="utf-8")
    swarm.env["FAKE_WORKER_SLEEP"] = "300"
    swarm.env["SWARM_PARK_AFTER"] = "1"
    swarm.up()
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 4, timeout=30), swarm.log_text()
    swarm.cli("waiting", "W1", "drop the legacy column, yes or no?")
    assert swarm.wait(lambda: swarm.state()["parked"] == ["W1"], timeout=20), swarm.log_text()
    swarm.cli("resumed", "W1", "yes, drop it")
    assert swarm.wait(lambda: "W1" in (swarm.state().get("answered") or {}), timeout=10)
    slots = ["W0", "W2", "W3", "W4"]
    assert swarm.wait(lambda: swarm.busy_phases() == slots, timeout=30), swarm.log_text()
    old = swarm.state()["supervisor_pid"]

    r = swarm.cli("restart", "--full", check=False)
    assert r.returncode == 0, r.stdout + r.stderr  # not refused: the drain waits for it
    waits = ["5 workers (W1 in its own window)"]
    assert swarm.wait(lambda: swarm.state()["drain"].get("waiting") == waits, timeout=10), (
        swarm.state()["drain"])
    assert "waiting for 5 workers (W1 in its own window), then restart" in (
        swarm.cli("status").stdout)

    for phase in slots:  # the slots' workers finish; the parked one works on
        swarm.cli("done", phase, "ok")
    waits = ["1 worker (W1 in its own window)"]
    assert swarm.wait(lambda: swarm.state()["drain"].get("waiting") == waits, timeout=10), (
        swarm.state()["drain"])
    st = swarm.state()
    assert st["supervisor_pid"] == old and _alive(old) and not st["drain"].get("stopping_at")
    assert _plan(swarm).get("stage") == restart_mod.DRAINING
    assert "RUN-ENDED W1" not in swarm.log_text()

    swarm.cli("done", "W1", "ok")  # it reports done: nothing is left to wait for
    assert swarm.wait(lambda: _plan(swarm).get("stage") == restart_mod.DONE, timeout=90), (
        swarm.log_text() + (swarm.state_dir / "logs" / "drain-down.log").read_text())
    st = swarm.state()
    assert st["supervisor_pid"] != old and _alive(st["supervisor_pid"]) and not _alive(old)
    assert not st["drain"] and st["parked"] == []
    assert not any("did not restart" in ln for ln in swarm.tg_lines())
    # The new run carries on with what the drain held back.
    assert swarm.wait(lambda: swarm.busy_phases() == ["W5"], timeout=30), swarm.log_text()
