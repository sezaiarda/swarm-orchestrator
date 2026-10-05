"""Frozen time is not elapsed time: what ``swarm thaw`` does about the clock.

Three tiers, all against the ``fake_cgroups`` tree:

* the move itself — every clock the swarm keeps for itself goes along by
  exactly how long the freeze lasted, the ones somebody else set stay, and a
  thaw run twice moves nothing twice;
* the spans — what a timeout, a grace and a count of working hours take out;
* the first wake — a supervisor that stood still for hours fires nothing when
  it carries on, and one that is not moved along fires everything.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime

import pytest

from swarm_orchestrator import bigpic
from swarm_orchestrator import blockedping
from swarm_orchestrator import buildidle
from swarm_orchestrator import buildsem
from swarm_orchestrator import cli
from swarm_orchestrator import doctor
from swarm_orchestrator import freezer
from swarm_orchestrator import opqueue
from swarm_orchestrator import overseer
from swarm_orchestrator import repocmd
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.state import State
from swarm_orchestrator.supervisor import Supervisor

LEDGER = "- [ ] `P0` · needs:—\n- [ ] `P1` · needs:—\n- [ ] `P2` · needs:—\n"
#: How long the freeze of these tests lasted, and how long before it began
#: every clock below was set.
FROZE = 6 * 3600.0
BEFORE = 30.0


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


def _record(cfg, since: float, **more) -> None:
    with state_mod.transaction(cfg) as st:
        st.frozen = {"since": since, "stage": freezer.FROZEN, "quiet_at": 0.0, "waiting": [],
                     "quiesced": True, "cgroups": [], "awake": [], **more}


def _close(cfg, since: float, until: float) -> None:
    freezer.close_span(cfg, since, until)


# -- the move: the state ----------------------------------------------------------
def _clocked(t0: float) -> State:
    st = State.fresh(2)
    st.waiting = {"P0": t0 + 60, "operator:op-1": t0 + 90}
    st.parked = ["P1", "P2"]
    st.asked = {"P1": t0 - 100}
    st.answered = {"P2": t0 - 50}
    st.last_event_at = t0
    st.operator_phase, st.operator_lease_until = "op-1", t0 + 3600
    st.overseer_pass, st.overseer_deadline = "ovs-1", t0 + 1800
    st.push_owed = {"/r/a": {"phase": "P0", "reason": "x", "since": t0 - 10, "tried": t0 - 5}}
    st.landing = {"P1": {"/r/a": {"stage": "unprepared", "at": t0 - 20}}}
    st.launch_fails = {"P2": [2, t0 - 15]}
    st.pause_at = t0 + 7200
    st.usage_hold = {"week": {"at": 90, "pct": 91, "resets_at": t0 + 100, "since": t0}}
    st.usage_fired = {"week:down": t0 + 100}
    st.usage_override = {"five_hour": t0 + 200}
    st.usage_api_at = t0 - 1
    st.run_epoch = t0 - 9000
    st.drain = {"since": t0 - 3}
    return st


def test_every_clock_the_swarm_keeps_moves_by_exactly_how_long_it_stood_frozen():
    now = time.time()
    t0 = now - FROZE - BEFORE
    st, was = _clocked(t0), _clocked(t0)

    st.shift(FROZE, now)

    assert st.waiting == {k: v + FROZE for k, v in was.waiting.items()}
    assert st.asked == {"P1": was.asked["P1"] + FROZE}
    assert st.answered == {"P2": was.answered["P2"] + FROZE}
    assert st.last_event_at == was.last_event_at + FROZE
    assert st.operator_lease_until == was.operator_lease_until + FROZE
    assert st.overseer_deadline == was.overseer_deadline + FROZE
    assert st.push_owed["/r/a"]["since"] == was.push_owed["/r/a"]["since"] + FROZE
    assert st.push_owed["/r/a"]["tried"] == was.push_owed["/r/a"]["tried"] + FROZE
    assert st.landing["P1"]["/r/a"]["at"] == was.landing["P1"]["/r/a"]["at"] + FROZE
    assert st.launch_fails == {"P2": [2, was.launch_fails["P2"][1] + FROZE]}
    # The park deadline moved, so the question is as old as it was.
    assert st.asked_at("P0", 120) == was.asked_at("P0", 120) + FROZE


def test_a_time_somebody_else_set_does_not_move():
    now = time.time()
    t0 = now - FROZE - BEFORE
    st, was = _clocked(t0), _clocked(t0)

    st.shift(FROZE, now)

    assert st.pause_at == was.pause_at  # the owner chose it on the clock
    assert st.usage_hold == was.usage_hold  # the provider's reset
    assert st.usage_fired == was.usage_fired and st.usage_override == was.usage_override
    assert st.usage_api_at == was.usage_api_at
    assert st.run_epoch == was.run_epoch and st.drain == was.drain


def test_never_and_a_moment_inside_the_freeze():
    now = time.time()
    st = State.fresh(1)
    st.asked = {"P0": now - 60}  # asked a minute before the thaw, inside the freeze
    st.parked = ["P0"]

    st.shift(FROZE, now)

    assert st.last_event_at == 0.0 and st.operator_lease_until == 0.0  # 0 is "never"
    assert st.overseer_deadline == 0.0
    assert st.asked == {"P0": now}  # not older than the freeze let it get, never ahead


# -- the move: the files ----------------------------------------------------------
def _job(cfg, name: str, **fields) -> None:
    opqueue._write(cfg, opqueue.Item(phase=name, **fields))


def test_the_operator_queue_moves_its_leases_and_back_offs_not_a_chosen_time(cfg):
    now = time.time()
    t0 = now - FROZE - BEFORE
    _job(cfg, "run", state=opqueue.RUNNING, queued_at=t0 - 500, lease_until=t0 + 3600,
         hold_until=t0 + 3600)
    _job(cfg, "ask", state=opqueue.WAITING, queued_at=t0 - 400, lease_until=t0 + 86400,
         asked_at=t0 - 40)
    _job(cfg, "retry", state=opqueue.QUEUED, queued_at=t0 - 300, run_after=t0 + 200)
    _job(cfg, "dated", state=opqueue.QUEUED, queued_at=t0 - 200, run_after=t0 + 86400)
    _job(cfg, "done", state=opqueue.DONE, queued_at=t0 - 100, done_at=t0 - 10)

    opqueue.shift(cfg, FROZE, now)

    got = {i.phase: i for i in opqueue.load_all(cfg)}
    assert got["run"].lease_until == got["run"].hold_until == pytest.approx(t0 + 3600 + FROZE)
    assert got["ask"].lease_until == pytest.approx(t0 + 86400 + FROZE)
    assert got["ask"].asked_at == pytest.approx(t0 - 40 + FROZE)
    assert got["retry"].run_after == pytest.approx(t0 + 200 + FROZE)  # a failed start's back-off
    assert got["dated"].run_after == pytest.approx(t0 + 86400)  # `--not-before`: on the clock
    # The order of the queue stays; what a job has waited does not count the freeze.
    assert [i.phase for i in opqueue.load_all(cfg)] == ["run", "ask", "retry", "dated", "done"]
    assert got["retry"].queued_at == pytest.approx(t0 - 300)
    assert got["retry"].age_s(now) == pytest.approx(300 + BEFORE)
    assert asdict(got["done"]) == asdict(opqueue.Item(
        phase="done", state=opqueue.DONE, queued_at=got["done"].queued_at,
        done_at=got["done"].done_at))  # a finished job is left as it was


def test_the_overseers_policy_moves_and_still_reads_on_from_the_real_moment(cfg):
    now = time.time()
    t0 = now - FROZE - BEFORE
    policy = overseer.Policy(cfg)
    policy.mem = overseer.Memory(
        last_pass_at=t0 - 600, last_pass_end=t0 - 300, anchor=t0 - 5000, hold_since=t0 - 60,
        seen_hold="P0:conflict", owner_since={"P1": t0 - 100}, push_since={"/r/a": t0 - 10},
        starving_since=t0 - 30, pending=[{"key": "every", "text": "x", "at": t0 - 1}])
    policy.save()

    overseer.shift(cfg, FROZE, now)  # the thaw's, on the file

    mem = overseer.Policy(cfg).mem
    assert mem.last_pass_at == pytest.approx(t0 - 600 + FROZE)
    assert mem.anchor == pytest.approx(t0 - 5000 + FROZE)
    assert mem.hold_since == pytest.approx(t0 - 60 + FROZE)
    assert mem.owner_since == {"P1": pytest.approx(t0 - 100 + FROZE)}
    assert mem.push_since == {"/r/a": pytest.approx(t0 - 10 + FROZE)}
    assert mem.starving_since == pytest.approx(t0 - 30 + FROZE)
    assert mem.last_pass_end == pytest.approx(t0 - 300)  # a record, never compared with now
    assert mem.pending[0]["at"] == pytest.approx(t0 - 1)
    # What finished since the last pass is read from when it really began.
    assert overseer.Policy(cfg).since(now) == pytest.approx(t0 - 600)
    fresh = overseer.Policy(cfg)
    fresh.begin(now)
    assert fresh.since(now + 5) == pytest.approx(now)  # a new pass starts a new count


def test_the_big_picture_memory_moves_and_a_pass_keeps_its_real_start(cfg):
    now = time.time()
    t0 = now - FROZE - BEFORE
    bigpic.save(cfg, bigpic.Memory(live="bp-1", live_at=t0 - 300, last_at=t0 - 7200,
                                   last_end=t0 - 7000, retry_at=t0 + 600, anchor=t0 - 9000))

    bigpic.shift(cfg, FROZE, now)

    mem = bigpic.load(cfg)
    assert mem.live_at == pytest.approx(t0 - 300 + FROZE)
    assert mem.last_at == pytest.approx(t0 - 7200 + FROZE)
    assert mem.retry_at == pytest.approx(t0 + 600 + FROZE)
    assert mem.anchor == pytest.approx(t0 - 9000 + FROZE)
    assert mem.last_end == pytest.approx(t0 - 7000)  # a record
    assert mem.last_began() == pytest.approx(t0 - 7200)  # what landed since is read from here
    runner = bigpic.Runner(cfg, None)
    runner._ended(bigpic.LANDED, now, produced=True)
    assert runner.mem.last_at == pytest.approx(t0 - 300 + FROZE)
    assert runner.mem.last_began() == pytest.approx(t0 - 300)


def test_a_gathered_burst_of_blocked_pings_moves(cfg):
    now = time.time()
    t0 = now - FROZE - BEFORE
    blockedping.gather(cfg, "P0", "the host refuses the key", now=t0)
    blockedping.gather(cfg, "P1", "the host refuses the key", now=t0 + 5)

    blockedping.shift(cfg, FROZE, now)

    assert blockedping.deadline(cfg) == pytest.approx(t0 + FROZE + blockedping.GATHER_S)
    assert [r["phase"] for r in blockedping._read(cfg)] == ["P0", "P1"]


def test_the_build_gates_idle_samples_move_and_a_newer_one_stays(cfg, monkeypatch):
    now = time.time()
    t0 = now - FROZE - BEFORE
    monkeypatch.setattr(cfg, "build_idle_yield_s", 600)
    cfg.buildsem_dir.mkdir(parents=True, exist_ok=True)
    buildidle._save(cfg, {"ts": t0, "h": {
        "old": {"t": t0, "cpu": 1.0, "io": 0, "quiet": t0 - 500, "yielded": t0 - 100},
        "new": {"t": now - 2, "cpu": 1.0, "io": 0, "quiet": now - 2, "yielded": None},
    }})

    buildidle.shift(cfg, FROZE, now)

    got = buildidle.load(cfg)
    assert got["ts"] == pytest.approx(t0 + FROZE)
    assert got["h"]["old"]["t"] == pytest.approx(t0 + FROZE)
    assert got["h"]["old"]["quiet"] == pytest.approx(t0 - 500 + FROZE)
    assert got["h"]["old"]["yielded"] == pytest.approx(t0 - 100 + FROZE)
    # Sampled by a waiter the thaw had already woken: newer than the freeze.
    assert got["h"]["new"]["t"] == pytest.approx(now) and got["h"]["new"]["yielded"] is None


# -- the thaw moves them once -------------------------------------------------------
def _frozen_run(cfg, t0: float, since: float) -> None:
    """A run as it stood when a freeze began at ``since``, its clocks set at ``t0``."""
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
        st.waiting["P0"] = t0 + 60
        st.last_event_at = t0
        st.operator_phase, st.operator_lease_until = "run", t0 + 3600
        st.overseer_pass, st.overseer_deadline = "ovs-1", t0 + 1800
        st.pause_at = t0 + FROZE / 2
    _job(cfg, "run", state=opqueue.RUNNING, queued_at=t0 - 500, lease_until=t0 + 3600)
    policy = overseer.Policy(cfg)
    policy.mem = overseer.Memory(last_pass_at=t0 - 600, anchor=t0 - 5000,
                                 owner_since={"P0": t0 - 60})
    policy.save()
    bigpic.save(cfg, bigpic.Memory(live="bp-1", live_at=t0 - 300, anchor=t0 - 9000))
    blockedping.gather(cfg, "P9", "the host refuses the key", now=t0)
    _record(cfg, since)


def _clocks(cfg) -> dict:
    st = state_mod.read(cfg)
    return {
        "park": st.waiting["P0"], "event": st.last_event_at, "lease": st.operator_lease_until,
        "pass": st.overseer_deadline, "job": opqueue.load(cfg, "run").lease_until,
        "policy": overseer.Policy(cfg).mem.last_pass_at, "owner": overseer.Policy(cfg).mem.
        owner_since["P0"], "bigpic": bigpic.load(cfg).live_at,
        "blocked": blockedping.deadline(cfg),
    }


def test_swarm_thaw_moves_every_file_by_the_freeze_and_leaves_the_chosen_time(
        cfg, fake_cgroups):
    since = time.time() - FROZE
    _frozen_run(cfg, since - BEFORE, since)
    was, pause_at = _clocks(cfg), state_mod.read(cfg).pause_at

    assert cli.cmd_thaw(cfg, gap_s=0) == 0

    (span,) = freezer.spans(cfg)
    frozen_s = span[1] - span[0]
    assert span[0] == since and frozen_s == pytest.approx(FROZE, abs=5)
    assert _clocks(cfg) == {k: pytest.approx(v + frozen_s) for k, v in was.items()}
    assert state_mod.read(cfg).pause_at == pause_at
    assert "THAW-CLOCKS-FAILED" not in cfg.supervisor_log.read_text()


def test_a_thaw_cut_short_and_run_again_moves_nothing_twice(cfg, fake_cgroups, monkeypatch):
    since = time.time() - FROZE
    _frozen_run(cfg, since - BEFORE, since)
    was = _clocks(cfg)
    real = blockedping.shift

    def cut(*_a, **_k):
        raise KeyboardInterrupt  # the thaw is killed with half the clocks moved

    monkeypatch.setattr(blockedping, "shift", cut)
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_thaw(cfg, gap_s=0)

    record = state_mod.read(cfg).frozen
    assert record["stage"] == freezer.THAWING
    assert record["shifted"] == ["state", "operator", "overseer", "bigpic"]
    frozen_s = record["until"] - since
    half = _clocks(cfg)
    assert half["park"] == pytest.approx(was["park"] + frozen_s)
    assert half["blocked"] == pytest.approx(was["blocked"])  # not reached yet

    monkeypatch.setattr(blockedping, "shift", real)
    time.sleep(0.05)
    assert cli.cmd_thaw(cfg, gap_s=0) == 0  # run again: it finishes what is left

    assert _clocks(cfg) == {k: pytest.approx(v + frozen_s) for k, v in was.items()}
    assert state_mod.read(cfg).frozen == {}
    assert freezer.spans(cfg) == [(since, record["until"])]  # the same span, once
    assert cli.cmd_thaw(cfg, gap_s=0) == 0  # and a third time: nothing is frozen
    assert _clocks(cfg) == {k: pytest.approx(v + frozen_s) for k, v in was.items()}


def test_a_clock_that_cannot_be_moved_does_not_stop_the_thaw(cfg, fake_cgroups, monkeypatch):
    since = time.time() - FROZE
    _frozen_run(cfg, since - BEFORE, since)
    was = _clocks(cfg)

    def broken(*_a, **_k):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(opqueue, "shift", broken)

    assert cli.cmd_thaw(cfg, gap_s=0) == 0

    assert state_mod.read(cfg).frozen == {}  # the run carries on
    assert "THAW-CLOCKS-FAILED operator" in cfg.supervisor_log.read_text()
    now = _clocks(cfg)
    assert now["job"] == was["job"] and now["park"] > was["park"] + FROZE - 5


# -- the spans ----------------------------------------------------------------------
def test_awake_elapsed_takes_out_closed_spans_and_the_open_one(cfg):
    now = 1_000_000.0
    _close(cfg, now - 5000, now - 4000)  # 1000 s frozen
    _close(cfg, now - 3000, now - 2500)  # 500 s frozen
    assert freezer.spans(cfg, now) == [(now - 5000, now - 4000), (now - 3000, now - 2500)]
    assert freezer.awake_elapsed(cfg, now - 6000, now) == 6000 - 1500
    assert freezer.awake_elapsed(cfg, now - 4500, now) == 4500 - 500 - 500  # began inside one
    assert freezer.awake_elapsed(cfg, now - 2000, now) == 2000  # after both
    assert freezer.awake_elapsed(cfg, now - 5000, now - 4000) == 0  # frozen from end to end

    _record(cfg, now - 600)  # frozen now, for ten minutes so far
    assert freezer.spans(cfg, now)[-1] == (now - 600, now)
    assert freezer.awake_elapsed(cfg, now - 6000, now) == 6000 - 1500 - 600
    assert freezer.awake_elapsed(cfg, now - 6000, now + 300) == 6000 - 1500 - 600  # still frozen
    _record(cfg, now - 600, until=now - 100, stage=freezer.THAWING)  # woken, not yet closed
    assert freezer.awake_elapsed(cfg, now - 6000, now) == 6000 - 1500 - 500


def test_a_span_closed_while_its_record_is_still_there_counts_once(cfg):
    now = 1_000_000.0
    _record(cfg, now - 600, until=now - 100)
    _close(cfg, now - 600, now - 100)
    (cfg.state_dir / "history" / freezer.SPANS).open("a").write("not json\n")

    assert freezer.spans(cfg, now) == [(now - 600, now - 100)]
    assert freezer.frozen_in(freezer.spans(cfg, now), now - 1000, now) == 500


def test_the_states_clock_stands_still_until_the_thaw_has_moved_it(cfg):
    now = time.time()
    assert freezer.state_now({}, now) == now
    assert freezer.state_now({"since": now - 600}, now) == now - 600
    assert freezer.state_now({"since": now - 600, "shifted": ["state"]}, now) == now


# -- a timeout counts the time awake -------------------------------------------------
def test_a_build_survives_a_freeze_longer_than_its_timeout(cfg, fake_cgroups, capfd):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2.5)"])
    # Started just before a freeze that lasted an hour: by the wall clock the
    # build has run for an hour, by the run's for a moment.
    start = time.time() - 3600 - 1
    _close(cfg, start + 1, time.time())

    assert buildsem._wait_child(cfg, proc, 60.0, start) == 0

    assert "--timeout" not in capfd.readouterr().err


def test_a_build_frozen_right_now_is_not_timed_out(cfg, fake_cgroups, capfd):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2.5)"])
    start = time.time() - 3600 - 1
    _record(cfg, start + 1)  # the freeze is still on (this look is the thaw's first)

    assert buildsem._wait_child(cfg, proc, 60.0, start) == 0

    assert "--timeout" not in capfd.readouterr().err


def test_a_build_that_ran_out_its_timeout_awake_is_still_stopped(cfg, fake_cgroups, capfd):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    start = time.time() - 3600 - 90
    _close(cfg, start + 1, start + 3601)  # frozen an hour, then awake for 89 s

    assert buildsem._wait_child(cfg, proc, 60.0, start) == 124

    assert "--timeout 1m" in capfd.readouterr().err


def test_a_run_through_the_gate_records_the_time_it_ran_awake(cfg, fake_cgroups, monkeypatch):
    monkeypatch.chdir(cfg.project_dir)
    real = time.time

    def frozen_meanwhile(*a, **k):
        # The build "stood frozen" for an hour while it ran.
        _close(cfg, real() - 3600, real() - 10)
        return 0

    monkeypatch.setattr(buildsem, "_wait_child", frozen_meanwhile)
    clock = iter([real() - 3700])
    monkeypatch.setattr(buildsem.time, "time", lambda: next(clock, real()))

    assert buildsem.run(cfg, ["true"]) == 0

    ends = [json.loads(ln) for ln in (cfg.buildsem_dir / "events.jsonl").read_text().splitlines()
            if '"end"' in ln]
    assert ends and ends[-1]["run_s"] < 3600  # not the wall clock's hour and more


def test_a_command_the_swarm_runs_is_not_timed_out_by_frozen_time(cfg, fake_cgroups, tmp_path):
    began = time.time()
    with (tmp_path / "out.log").open("w+") as fh:
        # The command outlives its 0.3 s, and the run has been frozen all along.
        _record(cfg, began - 1)
        proc_ok = None

        def thaw_soon() -> None:
            time.sleep(0.6)
            with state_mod.transaction(cfg) as st:
                st.frozen["until"] = time.time()

        import threading

        thread = threading.Thread(target=thaw_soon)
        thread.start()
        proc_ok = repocmd._spawn("sleep 0.8", tmp_path, fh, 0.3, cfg)
        thread.join()
    assert proc_ok is True  # 0.8 s by the wall clock, 0.2 s awake
    with (tmp_path / "out2.log").open("w+") as fh:
        assert repocmd._spawn("sleep 5", tmp_path, fh, 0.3, cfg) is False  # awake: stopped
        fh.seek(0)
        assert "timed out after 0.3s" in fh.read()


def test_a_waiter_does_not_leave_the_build_queue_over_frozen_time(cfg, fake_cgroups, monkeypatch):
    looks: list[float] = []

    def never(_cfg, _t, _overtake, _short):
        looks.append(time.time())
        return None, buildsem.View([], {}, False, None, None)

    monkeypatch.setattr(buildsem, "_try_turn", never)
    monkeypatch.setattr(buildsem, "_POLL_S", 0.05)
    began = time.time()
    _record(cfg, began - 1, until=began + 0.5)  # frozen for the first half second

    got = buildsem._wait_turn(cfg, None, None, announce=False, leave_ts=began + 0.3)

    assert got is None
    assert 0.75 <= time.time() - began < 3  # 0.3 s of waiting awake, after the freeze


def test_a_frozen_waiters_ticket_still_counts_as_polling(cfg, fake_cgroups):
    now = time.time()
    refreshed = now - 3600  # last refreshed an hour ago, just before the freeze
    assert not buildsem._polling(cfg, refreshed, now)
    _record(cfg, refreshed + 2)  # frozen two seconds later, and still
    assert buildsem._polling(cfg, refreshed, now)
    with state_mod.transaction(cfg) as st:
        st.frozen = {}
    _close(cfg, refreshed + 2, now - 5)  # thawed five seconds ago
    assert buildsem._polling(cfg, refreshed, now)
    assert not buildsem._polling(cfg, refreshed, now + 60)  # awake a minute, never polled


# -- the first wake -----------------------------------------------------------------
FIRES = ("WATCHDOG", "PARK", "OPERATOR-LEASE-EXPIRED", "OPERATOR-REQUEUED", "OVERSEER-TRIGGER",
         "OVERSEER-TIMEOUT", "BLOCKED-PING", "GC-DUE", "BACKUP-DUE", "BIGPIC-TIMEOUT",
         "HANDLER-ERROR")


def _standing_supervisor(cfg, monkeypatch, since: float) -> Supervisor:
    """A supervisor that has stood still since ``since`` with every timer it
    keeps set :data:`BEFORE` seconds earlier, each one due inside the freeze."""
    t0 = since - BEFORE
    for key, value in {
        "operator_enabled": True, "overseer_enabled": True, "overseer_every_s": 1800,
        "overseer_owner_wait_s": 900, "overseer_min_gap_s": 600, "gc_auto": True,
        "gc_every_s": 3600, "gc_idle_s": 0, "backup_every_s": 3600, "watchdog_s": 300,
        "park_after": 120, "big_picture_every": 5, "telegram_pings": "necessary",
    }.items():
        monkeypatch.setattr(cfg, key, value)
    _frozen_run(cfg, t0, since)
    sup = Supervisor(cfg)
    sup._bootstrapped = True
    sup._overseer_live = "ovs-1"
    sup._last_sweep = t0
    sup._doctor_probed = t0
    sup._gc_last = t0
    sup._gc_retry_at = t0 + 600
    sup._backup_last = t0
    sup._pinged = {"reap:P0": t0}
    sup._crashes = {"P0": [t0 - 100, t0]}
    sup._launch_fails = {"P1": (1, t0)}
    sup._adopt_recheck = {"P2": t0 + 60}
    sup._usage_last = t0
    sup.bigpic.mem = bigpic.load(cfg)
    sup.bigpic._spawned = True
    monkeypatch.setattr(sup.bigpic, "_alive", lambda: True)
    # What a due timer would start is written down instead of started.
    monkeypatch.setattr(sup, "_gc_run", lambda *_a: sup.log.line("GC-DUE"))
    monkeypatch.setattr(sup, "_backup_run", lambda: sup.log.line("BACKUP-DUE"))
    monkeypatch.setattr(sup, "_overseer_timeout", lambda pid: sup.log.line(
        f"OVERSEER-TIMEOUT {pid}"))
    monkeypatch.setattr(sup, "_maybe_start_overseer", lambda *_a: None)
    monkeypatch.setattr(sup, "_reap_dead_panes", lambda _st: False)
    monkeypatch.setattr(sup, "_start_resources", lambda: None)
    monkeypatch.setattr(sup, "_adopt_recheck_tick", lambda: None)
    monkeypatch.setattr(sup.master, "is_alive", lambda: False)
    sup._freeze_tick()
    assert sup._frozen_since == since
    return sup


def _settle(sup: Supervisor) -> None:
    for thread in (sup._gc_thread, sup._backup_thread):
        if thread is not None:
            thread.join(5)


def test_nothing_fires_on_the_first_wake_after_a_long_freeze(
        cfg, fake_cgroups, monkeypatch, tmp_path):
    since = time.time() - FROZE
    sup = _standing_supervisor(cfg, monkeypatch, since)
    try:
        assert cli.cmd_thaw(cfg, gap_s=0) == 0
        (span,) = freezer.spans(cfg)
        mark = len(cfg.supervisor_log.read_text())

        sup._handle(f"thaw {span[1] - span[0]:.3f} {since:.3f}")
        sup._wake_ticks()
        _settle(sup)

        log = cfg.supervisor_log.read_text()[mark:]
        assert "THAWED frozen=" in log
        for word in FIRES:
            assert word not in log, log
        st = state_mod.read(cfg)
        assert "P0" in st.waiting and not st.parked  # still on its park timer
        assert st.operator_phase == "run" and st.overseer_pass == "ovs-1"
        assert opqueue.load(cfg, "run").state == opqueue.RUNNING
        assert sup.bigpic.mem.live == "bp-1"
        assert blockedping._read(cfg)  # the burst is still gathering
        assert not (tmp_path / "tg.log").exists()  # and nobody was pinged
        # Each timer is as far off as it was when the freeze began.
        frozen_s = span[1] - span[0]
        t0 = since - BEFORE
        assert sup._last_sweep == pytest.approx(t0 + frozen_s, abs=2)
        assert sup._gc_last == pytest.approx(t0 + frozen_s, abs=2)
        assert sup._gc_retry_at == pytest.approx(t0 + 600 + frozen_s, abs=2)
        assert sup._backup_last == pytest.approx(t0 + frozen_s, abs=2)
        assert sup._pinged == {"reap:P0": pytest.approx(t0 + frozen_s, abs=2)}
        assert sup._crashes == {"P0": [pytest.approx(t0 - 100 + frozen_s, abs=2),
                                       pytest.approx(t0 + frozen_s, abs=2)]}
        assert sup._launch_fails == {"P1": (1, pytest.approx(t0 + frozen_s, abs=2))}
        assert sup._adopt_recheck == {"P2": pytest.approx(t0 + 60 + frozen_s, abs=2)}
        # The one check that keeps the wall clock: the provider's windows ran
        # on while the run stood still, so usage is read on this wake.
        assert "USAGE-CHECK" in log and sup._usage_last > since + FROZE - 5
        # The supervisor's memory and the files the thaw moved say the same.
        assert sup.overseer.mem.last_pass_at == pytest.approx(
            overseer.Policy(cfg).mem.last_pass_at, abs=2)
        assert sup.bigpic.mem.live_at == pytest.approx(t0 - 300 + frozen_s, abs=2)
        # And the loop goes back to sleep: the nearest deadline is the park
        # timer, half a minute away as it was.
        assert 5 < sup._select_timeout() <= BEFORE + 1
    finally:
        sup.log.close()


def test_a_supervisor_whose_clocks_are_not_moved_fires_all_of_it(
        cfg, fake_cgroups, monkeypatch, tmp_path):
    """The same wake with the move taken out: what the test above guards."""
    since = time.time() - FROZE
    sup = _standing_supervisor(cfg, monkeypatch, since)
    monkeypatch.setattr(freezer, "rebase", lambda *_a: [])
    monkeypatch.setattr(sup, "_shift_clocks", lambda _d: None)
    try:
        assert cli.cmd_thaw(cfg, gap_s=0) == 0
        mark = len(cfg.supervisor_log.read_text())

        sup._handle(f"thaw {FROZE:.3f} {since:.3f}")
        sup._wake_ticks()
        _settle(sup)

        log = cfg.supervisor_log.read_text()[mark:]
        for word in ("WATCHDOG idle=", "PARK P0", "OPERATOR-LEASE-EXPIRED run",
                     "OVERSEER-TRIGGER", "OVERSEER-TIMEOUT ovs-1", "BLOCKED-PING",
                     "GC-DUE", "BACKUP-DUE", "BIGPIC-TIMEOUT bp-1"):
            assert word in log, (word, log)
        assert (tmp_path / "tg.log").exists()
    finally:
        sup.log.close()


def test_a_thaw_the_supervisor_never_stood_still_for_still_moves_its_clocks(
        cfg, fake_cgroups, monkeypatch):
    """It was inside one long event from before the freeze until after the thaw."""
    since = time.time() - FROZE
    t0 = since - BEFORE
    sup = Supervisor(cfg)
    monkeypatch.setattr(sup, "_start_resources", lambda: None)
    sup._last_sweep = sup._backup_last = t0
    sup._ledger_held_until = t0 + 900  # held notes, due inside the freeze
    try:
        sup._handle("freeze")  # read late: the record is gone again by now
        assert sup._frozen_since is None

        sup._handle(f"thaw {FROZE:.3f} {since:.3f}")

        assert sup._last_sweep == pytest.approx(t0 + FROZE)
        assert sup._backup_last == pytest.approx(t0 + FROZE)
        assert sup._ledger_held_until == pytest.approx(t0 + 900 + FROZE)
        log = cfg.supervisor_log.read_text()
        assert "THAW-MISSED frozen=21600s" in log

        sup._handle(f"thaw {FROZE:.3f} {since:.3f}")  # the same thaw, said twice
        assert sup._last_sweep == pytest.approx(t0 + FROZE)
    finally:
        sup.log.close()


def test_a_thaw_poke_after_the_supervisor_carried_on_by_itself_moves_nothing_again(
        cfg, fake_cgroups, monkeypatch):
    since = time.time() - FROZE
    sup = Supervisor(cfg)
    monkeypatch.setattr(sup, "_start_resources", lambda: None)
    sup._last_sweep = since - BEFORE
    _record(cfg, since)
    try:
        sup._freeze_tick()
        with state_mod.transaction(cfg) as st:
            st.frozen = {}
        sup._freeze_tick()  # sees the record gone before the poke arrives
        moved = sup._last_sweep
        assert moved == pytest.approx(since - BEFORE + FROZE, abs=2)

        sup._handle(f"thaw {FROZE:.3f} {since:.3f}")

        assert sup._last_sweep == moved
        assert "THAW-MISSED" not in cfg.supervisor_log.read_text()
    finally:
        sup.log.close()


def test_a_real_supervisor_parks_nobody_after_a_freeze_longer_than_the_park_timer(
        swarm, fake_cgroups):
    swarm.env.update({"FAKE_WORKER_SLEEP": "300", "SWARM_WATCHDOG": "1",
                      "SWARM_PARK_AFTER": "2"})
    swarm.up()
    assert swarm.wait(lambda: swarm.busy_phases() == ["P0"], timeout=20), swarm.log_text()
    fake_cgroups.settle()
    swarm.cli("waiting", "P0", "which way?")  # a park timer two seconds long
    assert swarm.wait(lambda: "P0" in swarm.state()["waiting"], timeout=10)
    deadline = swarm.state()["waiting"]["P0"]
    swarm.cli("freeze", "--wait", "20")
    # The same freeze, begun an hour ago.
    code = (
        "import sys\n"
        "from swarm_orchestrator import state as s\n"
        "from swarm_orchestrator.config import load\n"
        "c = load(project_dir=sys.argv[1])\n"
        "with s.transaction(c) as st:\n"
        "    st.frozen['since'] -= 3600\n"
    )
    subprocess.run([sys.executable, "-c", code, str(swarm.project)], env=swarm.env, check=True)
    time.sleep(2.5)  # the park timer runs out while everything stands frozen
    mark = len(swarm.log_text())

    swarm.cli("thaw", "--gap", "0")

    assert swarm.wait(lambda: "THAWED frozen=36" in swarm.log_text()[mark:], timeout=10), (
        swarm.log_text())
    # The watchdog sweeps again (it is a second long) and still nobody is parked.
    assert swarm.wait(lambda: "WATCHDOG idle=" in swarm.log_text()[mark:], timeout=10)
    after = swarm.log_text()[mark:]
    assert "PARK" not in after and "WATCHDOG-REAP" not in after, after
    st = swarm.state()
    assert not st["parked"] and "frozen" not in st
    assert st["waiting"]["P0"] == pytest.approx(deadline + 3600, abs=30)
    assert st["last_event_at"] <= time.time()  # behind, never ahead
    (span,) = [json.loads(ln) for ln in
               (swarm.state_dir / "history" / freezer.SPANS).read_text().splitlines()]
    assert span["until"] - span["since"] == pytest.approx(3600, abs=30)


# -- doctor ---------------------------------------------------------------------------
def _stalled_run(cfg, t0: float) -> None:
    """A busy slot whose last event and whose launch are both ``t0``."""
    with state_mod.transaction(cfg) as st:
        slot = st.claim_slot("P0")
        slot.worktree = str(cfg.project_dir)
        st.last_event_at = t0
    _log_at(cfg, t0, "LAUNCH P0 slot=0")


def _log_at(cfg, ts: float, message: str) -> None:
    """One supervisor-log line as it would have been written at ``ts``."""
    stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    cfg.supervisor_log.parent.mkdir(parents=True, exist_ok=True)
    with cfg.supervisor_log.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} 1.000 {message}\n")


def test_doctor_says_frozen_and_raises_no_stall_or_activity_warning(
        cfg, fake_cgroups, monkeypatch):
    monkeypatch.setattr(doctor, "_worktree_activity", lambda *_a: 0)
    monkeypatch.setattr(doctor, "_lane_activity", lambda *_a: 0)
    since = time.time() - FROZE
    _stalled_run(cfg, since - 60)  # launched a minute before the freeze, nothing written yet
    st = state_mod.read(cfg)
    # Unfrozen, the same run reads as six hours stalled with nothing to show.
    assert doctor._check_stall(cfg, st).status == doctor.WARN
    assert doctor._check_activity(cfg, st).status == doctor.FAIL
    assert doctor._check_frozen(st) == doctor.Check("run.frozen", doctor.OK, "not frozen")

    _record(cfg, since, cgroups=[{"path": "/run/worker-P0", "kind": "worker", "id": "P0"}])
    st = state_mod.read(cfg)

    frozen = doctor._check_frozen(st)
    assert frozen.status == doctor.WARN and frozen.fix_hint == "swarm thaw"
    assert "Frozen since " in frozen.detail and "1 group frozen" in frozen.detail
    assert "`swarm thaw`" in frozen.detail
    stall = doctor._check_stall(cfg, st)
    assert stall.status == doctor.OK and "60s ago" in stall.detail  # as when it froze
    assert doctor._check_activity(cfg, st).status == doctor.OK
    assert "not applicable (frozen)" in doctor._check_nudge(st, ["P1"], [1]).detail


def test_doctor_raises_none_right_after_the_thaw_either(cfg, fake_cgroups, monkeypatch):
    monkeypatch.setattr(doctor, "_worktree_activity", lambda *_a: 0)
    monkeypatch.setattr(doctor, "_lane_activity", lambda *_a: 0)
    since = time.time() - FROZE
    _stalled_run(cfg, since - 60)
    _record(cfg, since)

    assert cli.cmd_thaw(cfg, gap_s=0) == 0

    st = state_mod.read(cfg)
    assert doctor._check_frozen(st).status == doctor.OK
    stall = doctor._check_stall(cfg, st)
    assert stall.status == doctor.OK and "last supervisor event 6" in stall.detail  # 60-odd s
    assert doctor._check_activity(cfg, st).status == doctor.OK


def test_the_start_grace_of_a_claimed_slot_does_not_run_out_while_frozen(
        cfg, fake_cgroups, monkeypatch):
    since = time.time() - FROZE
    with state_mod.transaction(cfg) as st:
        slot = st.claim_slot("P0")
    monkeypatch.setattr(doctor, "_unlaunched", lambda _c, _p: since - 10)
    monkeypatch.setattr(doctor, "_reported", lambda *_a: False)
    st = state_mod.read(cfg)
    assert isinstance(doctor._slot_worker(cfg, st, slot, time.time(), lambda _p: "sh"),
                      doctor.Stopped)  # six hours claimed and never launched
    _close(cfg, since, time.time() - 5)
    assert doctor._slot_worker(cfg, st, slot, time.time(), lambda _p: "sh") == doctor._STARTING
