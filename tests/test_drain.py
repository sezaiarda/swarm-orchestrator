"""``swarm down --drain``: hold launches, wait for the running work, stop, run the after-command."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from swarm_orchestrator import drain as drain_mod
from swarm_orchestrator import opqueue
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import cmd_drain_cancel, cmd_resume
from swarm_orchestrator.config import load
from swarm_orchestrator.state import State
from swarm_orchestrator.supervisor import Supervisor

LEDGER = "- [ ] `P0` · needs:—\n- [ ] `P1` · needs:—\n- [ ] `P2` · needs:—\n"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    cfg = load(project_dir=str(tmp_path))
    state_mod.init_state(cfg)
    return cfg


def _set(cfg, **fields) -> None:
    with state_mod.transaction(cfg) as st:
        for key, value in fields.items():
            setattr(st, key, value)


# -- what it waits for, in words -----------------------------------------
def test_waits_for_working_workers_not_ones_waiting_on_the_owner(cfg):
    st = State.fresh(3)
    st.claim_slot("P0")
    st.claim_slot("P1")
    st.claim_slot("P2")
    st.waiting["P2"] = time.time() + 60
    st.parked = ["P3"]
    assert drain_mod.waiting_for(cfg, st) == ["2 workers"]
    assert drain_mod.waiting_for(cfg, st, launching=1, overseer=True) == [
        "3 workers", "an Overseer pass"]


def test_waits_for_a_moving_queue_and_a_resolver_but_not_a_dirty_hold(cfg):
    st = State.fresh(1)
    st.integ_push("P0", "ok")
    assert drain_mod.waiting_for(cfg, st) == ["the merge queue"]
    st.integ_blocked = "P0"
    assert drain_mod.waiting_for(cfg, st) == []  # held on the owner's tree
    st.windows["resolve:P0"] = "@9"
    assert drain_mod.waiting_for(cfg, st) == ["a merge-conflict resolver"]


def test_waits_for_an_operator_job_unless_it_asked_the_owner(cfg):
    now = time.time()
    st = State.fresh(1)
    st.claim_operator("J1", now + 600, now)
    assert drain_mod.waiting_for(cfg, st, now=now) == ["an operator job"]
    cfg.operator_enabled = True
    opqueue.add(cfg, "J1", status="ok", note="do it", branch="")
    opqueue.lease(cfg, "J1", now)
    opqueue.wait_on_owner(cfg, "J1", "which way?", now)
    assert drain_mod.waiting_for(cfg, st, now=now) == []


def test_the_line_is_plain_english():
    assert drain_mod.line({}) == ""
    assert drain_mod.line({"waiting": ["2 workers", "the merge queue"], "then": "sudo shutdown now"}) == (
        "Draining: waiting for 2 workers and the merge queue, then stop, then: sudo shutdown now")
    assert drain_mod.line({"waiting": [], "stopping_at": 1.0}) == (
        "Draining: done waiting, stopping the swarm now")


def test_sudo_is_checked_only_when_the_command_uses_it(monkeypatch):
    calls = []

    def fake_run(argv, **_kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(drain_mod.subprocess, "run", fake_run)
    assert drain_mod.sudo_warning("sleep 120; echo pseudo") is None
    assert calls == []
    assert "password" in drain_mod.sudo_warning("sleep 120; sudo shutdown now")
    assert calls == [["sudo", "-n", "true"]]


# -- the supervisor ------------------------------------------------------
def test_a_drain_holds_launches_then_stops_once_the_last_worker_is_done(cfg, monkeypatch):
    spawned = []
    monkeypatch.setattr(drain_mod, "spawn_down", lambda c: spawned.append(c) or True)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
        st.drain = {"since": time.time(), "then": "sleep 1"}
    sup = Supervisor(cfg)
    sup._bootstrapped = True

    sup._drain_tick()
    assert state_mod.read(cfg).drain["waiting"] == ["1 worker"]
    assert sup._fill_slots("test") == []  # the hold: P1/P2 are ready, nothing launches
    assert not spawned

    sup._advance_done("P0", "ok")  # the last worker finishes
    sup._drain_tick()

    st = state_mod.read(cfg)
    assert st.drain["stopping_at"] and st.drain["waiting"] == []
    assert not st.finished  # the drain stops the run; it does not "finish" it
    assert len(spawned) == 1
    assert "stopping now, then running: sleep 1" in (cfg.state_dir.parent / "tg.log").read_text()
    sup._drain_tick()
    assert len(spawned) == 1  # once


def test_cancel_and_resume_lift_the_drain_but_not_a_stop_under_way(cfg, capsys):
    _set(cfg, drain={"since": 1.0, "then": ""})
    assert cmd_drain_cancel(cfg) == 0
    assert state_mod.read(cfg).drain == {}
    _set(cfg, drain={"since": 1.0, "then": ""}, paused=True)
    cmd_resume(cfg)
    st = state_mod.read(cfg)
    assert st.drain == {} and not st.paused
    _set(cfg, drain={"since": 1.0, "stopping_at": 2.0})
    assert cmd_drain_cancel(cfg) == 1
    assert state_mod.read(cfg).drain["stopping_at"] == 2.0
    assert "too late" in capsys.readouterr().err


def test_no_operator_session_opens_while_draining(cfg, monkeypatch):
    from swarm_orchestrator import operator as operator_mod
    from swarm_orchestrator.logutil import Log

    monkeypatch.setattr(cfg, "operator_enabled", True)
    _set(cfg, drain={"since": 1.0})
    log = Log(cfg.supervisor_log)
    try:
        assert operator_mod.dispatch(cfg, "J1", log) is False
    finally:
        log.close()
    assert "OPERATOR-HELD J1 draining" in cfg.supervisor_log.read_text()
    assert state_mod.read(cfg).operator_phase is None


# -- end to end ----------------------------------------------------------
def test_down_drain_then_runs_the_command_after_the_swarm_is_gone(swarm, tmp_path):
    swarm.env["FAKE_WORKER_PARK"] = "1"  # the worker holds its slot; the test says done
    swarm.up()
    assert swarm.wait(lambda: swarm.busy_phases() == ["P0"], timeout=20), swarm.log_text()
    marker = tmp_path / "after.txt"

    r = swarm.cli("down", "--drain", "--then", f"echo stopped > {marker}")
    assert "draining" in r.stdout
    assert swarm.wait(lambda: swarm.state()["drain"].get("waiting") == ["1 worker"], timeout=10)
    assert "waiting for 1 worker, then stop, then: echo" in swarm.cli("status").stdout

    swarm.cli("done", "P0", "ok")
    assert swarm.wait(marker.is_file, timeout=30), (
        swarm.log_text() + (swarm.state_dir / "logs" / "drain-down.log").read_text())
    assert marker.read_text().strip() == "stopped"
    assert not swarm.cli("status").stdout.count("BUSY")  # nothing else launched
    assert "DRAIN-COMPLETE" in swarm.log_text() and "SUPERVISOR-STOP" in swarm.log_text()
    assert any("stopping now" in ln for ln in swarm.tg_lines())
    assert "swarm down" in (swarm.state_dir / "logs" / "drain-down.log").read_text()


def test_down_drain_with_no_supervisor_stops_at_once(swarm, tmp_path):
    marker = tmp_path / "after.txt"
    r = swarm.cli("down", "--drain", "--then", f"echo now > {marker}")
    assert "nothing to wait for" in r.stdout
    assert swarm.wait(marker.is_file, timeout=10)
    assert Path(swarm.state_dir / "logs" / drain_mod.AFTER_LOG).is_file()


def test_an_overseer_pass_waiting_on_the_owner_does_not_hold_a_drain(cfg, monkeypatch):
    spawned = []
    monkeypatch.setattr(drain_mod, "spawn_down", lambda c: spawned.append(c) or True)
    with state_mod.transaction(cfg) as st:
        st.drain = {"since": time.time(), "then": ""}
    sup = Supervisor(cfg)
    sup._bootstrapped = True
    sup._overseer_live = "ovs-1"
    sup._drain_tick()
    assert state_mod.read(cfg).drain["waiting"] == ["an Overseer pass"]
    with state_mod.transaction(cfg) as st:
        st.waiting[state_mod.waiter_key(state_mod.OVERSEER, "ovs-1")] = time.time() + 600
    sup._drain_tick()
    assert len(spawned) == 1
