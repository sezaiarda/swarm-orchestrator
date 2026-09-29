"""Tests for the operator session — the consumer that finally drains the queue.

Everything here runs on the **bare** driver, where :func:`operator.spawn` is a
no-op. That is not a limitation being worked around, it is the property being
protected: an operator session holds the owner's own authority on the host, and
a test suite must never be able to start one.

What is actually load-bearing:

* **the lease, not a pane probe** — ``session.setup`` leaves the operator window
  holding ``sleep infinity``, so a pane-based liveness check answers "alive"
  before any session has ever run and the operator never launches at all;
* **the blocker is bounded** — a hand-off that will never be drained must stop
  holding ``finish`` open, or the queue built to stop losing notes becomes the
  thing that hangs the run;
* **the queue reaches the supervisor's timeout** — a moving swarm never reaches
  the watchdog's quiet point, so a deadline nothing wakes for is a deadline that
  never fires.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarm_orchestrator import notes as notes_mod
from swarm_orchestrator import operator as operator_mod
from swarm_orchestrator import opqueue, promptlint
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import _known_commands
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import LAUNCH_GIVE_UP, Supervisor

REPO = Path(__file__).resolve().parent.parent
PHASE = "op-S1"
OTHER = "op-S2"
NOTE = "rebuilt the gateway image; the host still needs a compose up to pick it up"


def _cfg(tmp_path, monkeypatch, *, enabled: bool):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / ".swarm.toml").write_text(
        f"[operator]\nenabled = {'true' if enabled else 'false'}\n"
        '[swarm]\ndriver = "bare"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_BIN", "true")  # nothing detached may reach a model
    for leak in ("SWARM_TRIAGE_CMD", "SWARM_RECAP_CMD", "SWARM_OPERATOR",
                 "SWARM_DRIVER", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(leak, raising=False)
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return _cfg(tmp_path, monkeypatch, enabled=True)


@pytest.fixture
def off(tmp_path, monkeypatch):
    return _cfg(tmp_path, monkeypatch, enabled=False)


@pytest.fixture
def log(cfg):
    lg = Log(cfg.supervisor_log)
    yield lg
    lg.close()


def queue(cfg, phase: str = PHASE, note: str = NOTE) -> opqueue.Item:
    item = opqueue.add(cfg, phase, status="operator", note=note)
    assert item is not None
    return item


def tg_lines(cfg) -> list[str]:
    path = Path(str(cfg.state_dir)).parent / "tg.log"
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def cli(cfg, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "swarm_orchestrator",
         "--project-dir", str(cfg.project_dir), *args],
        cwd=str(cfg.project_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )


# -- the opt-in -----------------------------------------------------------
def test_a_disabled_operator_dispatches_nothing(off):
    """`enabled = false` is the only thing between a suite and a live session."""
    lg = Log(off.supervisor_log)
    assert opqueue.add(off, PHASE, status="operator", note=NOTE) is None
    assert operator_mod.on_finished(off, PHASE, lg) is False
    assert operator_mod.sweep(off, lg) is False
    assert state_mod.read(off).operator_phase is None
    lg.close()


def test_a_disabled_operator_refuses_even_a_hand_written_item(off, tmp_path):
    """An item forged past `add` still opens nothing: the check is at dispatch."""
    lg = Log(off.supervisor_log)
    off.operator_dir.mkdir(parents=True, exist_ok=True)
    opqueue._write(off, opqueue.Item(phase=PHASE, note=NOTE, queued_at=time.time()))

    assert operator_mod.dispatch(off, PHASE, lg) is False
    assert opqueue.load(off, PHASE).state == opqueue.QUEUED  # never even leased
    assert state_mod.read(off).operator_phase is None
    lg.close()


# -- the lease ------------------------------------------------------------
def test_the_lease_refuses_a_second_concurrent_claim(cfg, log):
    """One session at once, enforced check-and-set under the state flock.

    The trap this replaces: `session.setup` leaves the operator window running
    `sleep infinity`, so a pane probe would report a live operator forever and
    the first dispatch would never happen.
    """
    queue(cfg, PHASE)
    queue(cfg, OTHER, note="restart the unit on the build host")

    assert operator_mod.dispatch(cfg, PHASE, log) is True
    assert state_mod.read(cfg).operator_phase == PHASE

    assert operator_mod.dispatch(cfg, OTHER, log) is False
    assert state_mod.read(cfg).operator_phase == PHASE
    assert opqueue.load(cfg, OTHER).state == opqueue.QUEUED
    assert opqueue.load(cfg, OTHER).attempts == 0  # not even charged an attempt


def test_an_expired_lease_lets_the_next_claim_through(cfg, log):
    """A session that hangs must not pin the queue for the rest of the run."""
    queue(cfg, PHASE)
    queue(cfg, OTHER, note="restart the unit on the build host")
    assert operator_mod.dispatch(cfg, PHASE, log) is True

    with state_mod.transaction(cfg) as st:
        st.operator_lease_until = time.time() - 1  # the lease ran out

    assert operator_mod.dispatch(cfg, OTHER, log) is True
    assert state_mod.read(cfg).operator_phase == OTHER


def test_a_sweep_hands_back_both_halves_of_an_expired_lease(cfg, log):
    """The state lease and the item lease expire together, so both come back."""
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True

    later = time.time() + opqueue.LEASE_S + 1
    operator_mod.sweep(cfg, log, now=later)

    item = opqueue.load(cfg, PHASE)
    assert item.state == opqueue.QUEUED
    assert item.attempts == 1  # the attempt it burned is kept
    assert state_mod.read(cfg).operator_phase is None


def test_a_later_triage_is_not_dispatched_by_the_finish_hook(cfg, log):
    """`later` is the one thing that holds the merge-time dispatch back."""
    queue(cfg, PHASE)
    opqueue.set_triage(cfg, PHASE, when=opqueue.LATER, why="keeps", group="",
                       source="test")

    assert operator_mod.on_finished(cfg, PHASE, log) is False
    assert state_mod.read(cfg).operator_phase is None
    # ...and so does the sweep, until a worker slot is free that no launchable
    # phase wants: `later` is "it can wait for room".
    assert operator_mod.sweep(cfg, log) is False
    assert operator_mod.sweep(cfg, log, room=lambda: False) is False
    assert state_mod.read(cfg).operator_phase is None
    # ...but room drains it, which is the only reason it is not lost.
    assert operator_mod.sweep(cfg, log, room=lambda: True) is True
    assert state_mod.read(cfg).operator_phase == PHASE


def test_the_sweep_opens_a_due_job_past_an_older_later_one(cfg, log):
    """A held `later` job does not stand in front of one that may run now."""
    queue(cfg, PHASE)
    opqueue.set_triage(cfg, PHASE, when=opqueue.LATER, why="keeps", group="",
                       source="test")
    queue(cfg, OTHER, note="restart the unit on the build host")
    assert operator_mod.sweep(cfg, log, room=lambda: False) is True
    assert state_mod.read(cfg).operator_phase == OTHER


def _later(cfg) -> None:
    queue(cfg, PHASE)
    opqueue.set_triage(cfg, PHASE, when=opqueue.LATER, why="keeps", group="",
                       source="test")
    state_mod.init_state(cfg)


def test_the_supervisor_holds_a_later_job_while_every_slot_is_busy(cfg):
    _later(cfg)
    with state_mod.transaction(cfg) as st:
        busy = [f"busy-{i}" for i in range(len(st.slots))]
        for phase in busy:
            st.claim_slot(phase)
    sup = Supervisor(cfg)
    assert sup._operator_room() is False
    sup._check_operator_queue()
    assert state_mod.read(cfg).operator_phase is None

    with state_mod.transaction(cfg) as st:
        st.free_slot_for(busy[0])
    sup._launching.add("mid-launch")  # a launch in flight is about to take it
    assert sup._operator_room() is False
    sup._launching.clear()
    assert sup._operator_room() is True
    sup._check_operator_queue()
    assert state_mod.read(cfg).operator_phase == PHASE


def test_a_later_job_opens_while_a_phase_builds_and_another_merges(cfg):
    """The rule: run it whenever a slot is free. A busy slot and a
    merge in the queue used to hold it until the whole run went quiet."""
    _later(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("some-phase")
        st.integ_queue = ["merging-phase"]
    sup = Supervisor(cfg)
    assert sup._operator_room() is True
    sup._check_operator_queue()
    assert state_mod.read(cfg).operator_phase == PHASE


def test_a_ready_phase_keeps_the_free_slot_until_it_is_given_up_on(cfg):
    """The operator never takes the slot a launchable phase needs."""
    _later(cfg)
    ledger = Path(str(cfg.project_dir)) / cfg.ledger
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("- [ ] `R1` · needs:—\n", encoding="utf-8")
    sup = Supervisor(cfg)
    assert sup._operator_room() is False
    sup._check_operator_queue()
    assert state_mod.read(cfg).operator_phase is None

    sup._launch_fails["R1"] = (LAUNCH_GIVE_UP, time.time())
    assert sup._operator_room() is True
    sup._check_operator_queue()
    assert state_mod.read(cfg).operator_phase == PHASE


def _age(cfg, phase: str, seconds: float) -> None:
    """Backdate ``phase``'s job so it has been queued for ``seconds``."""
    item = opqueue.load(cfg, phase)
    item.queued_at = time.time() - seconds
    opqueue._write(cfg, item)


def _backlog(cfg) -> Supervisor:
    """Every slot busy and a ready row waiting: room() never comes."""
    with state_mod.transaction(cfg) as st:
        for i in range(len(st.slots)):
            st.claim_slot(f"busy-{i}")
    ledger = Path(str(cfg.project_dir)) / cfg.ledger
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("- [ ] `R1` · needs:—\n", encoding="utf-8")
    sup = Supervisor(cfg)
    assert sup._operator_room() is False
    return sup


def test_a_later_job_opens_once_it_has_waited_out_the_bound(cfg):
    """Later jobs used to wait for hours behind a backlog that never left a slot
    free. Held before `later_wait_s`, opened after it, still without room."""
    _later(cfg)
    sup = _backlog(cfg)
    _age(cfg, PHASE, cfg.operator_later_wait_s - 60)
    sup._check_operator_queue()
    assert state_mod.read(cfg).operator_phase is None

    _age(cfg, PHASE, cfg.operator_later_wait_s + 60)
    sup._check_operator_queue()
    assert state_mod.read(cfg).operator_phase == PHASE
    assert "later job waited" in cfg.supervisor_log.read_text(encoding="utf-8")


def test_overdue_later_jobs_still_open_one_at_a_time_oldest_first(cfg, log):
    _later(cfg)
    queue(cfg, OTHER, note="restart the unit on the build host")
    opqueue.set_triage(cfg, OTHER, when=opqueue.LATER, why="keeps", group="",
                       source="test")
    _age(cfg, OTHER, cfg.operator_later_wait_s + 60)
    _age(cfg, PHASE, cfg.operator_later_wait_s + 120)

    assert operator_mod.sweep(cfg, log) is True
    assert state_mod.read(cfg).operator_phase == PHASE
    assert operator_mod.sweep(cfg, log) is False  # the lease admits one session
    assert state_mod.read(cfg).operator_phase == PHASE
    assert opqueue.load(cfg, OTHER).state == opqueue.QUEUED


def test_an_overdue_later_job_is_still_held_while_its_phase_is_in_flight(cfg, log):
    _later(cfg)
    _age(cfg, PHASE, cfg.operator_later_wait_s + 60)
    with state_mod.transaction(cfg) as st:
        st.integ_queue = [PHASE]
    assert operator_mod.sweep(cfg, log) is False
    with state_mod.transaction(cfg) as st:
        st.integ_queue = []
        st.claim_slot(PHASE)
    assert operator_mod.sweep(cfg, log) is False
    assert state_mod.read(cfg).operator_phase is None


def test_a_zero_later_wait_keeps_a_later_job_for_room_alone(cfg, log):
    _later(cfg)
    _age(cfg, PHASE, 30 * 24 * 3600)
    cfg.operator_later_wait_s = 0
    assert operator_mod.sweep(cfg, log, room=lambda: False) is False
    assert operator_mod.sweep(cfg, log, room=lambda: True) is True


def test_a_settled_run_opens_its_later_job(cfg):
    """Settled but for a `later` job: the finish check opens it, since nothing
    else may ever wake the loop to."""
    queue(cfg, PHASE)
    opqueue.set_triage(cfg, PHASE, when=opqueue.LATER, why="keeps", group="",
                       source="test")
    sup = Supervisor(cfg)
    sup._finish_if_settled(_settled(cfg))
    assert state_mod.read(cfg).operator_phase == PHASE
    assert state_mod.read(cfg).finished is False


def test_a_poke_never_opens_a_job_before_its_phase_merges(cfg, log):
    """A `now` triage (or `swarm operator`) can land while the phase is still in
    the merge queue: held, then opened by the merge itself."""
    queue(cfg, PHASE)
    with state_mod.transaction(cfg) as st:
        st.integ_queue = [PHASE]
    assert operator_mod.on_poke(cfg, PHASE, log) is False
    assert state_mod.read(cfg).operator_phase is None
    assert "OPERATOR-HELD" in cfg.supervisor_log.read_text(encoding="utf-8")

    with state_mod.transaction(cfg) as st:
        st.integ_queue = []
    assert operator_mod.on_finished(cfg, PHASE, log) is True
    assert state_mod.read(cfg).operator_phase == PHASE


def test_swarm_operator_by_hand_overrides_a_later_triage(cfg, capsys):
    from swarm_orchestrator import cli

    queue(cfg, PHASE)
    opqueue.set_triage(cfg, PHASE, when=opqueue.LATER, why="keeps", group="deploy",
                       source="test")
    with state_mod.transaction(cfg) as st:
        st.integ_queue = [PHASE]
    assert cli.cmd_operator(cfg, PHASE) == 0
    item = opqueue.load(cfg, PHASE)
    assert item.triage["when"] == opqueue.NOW and item.triage["source"] == "owner"
    assert item.triage["group"] == "deploy"
    assert "held until" in capsys.readouterr().out


# -- the blocker, and the deadlock it must not become ---------------------
def _settled(cfg) -> state_mod.State:
    """State for a run with nothing in flight: only the queue can hold it open."""
    with state_mod.transaction(cfg) as st:
        st.finished = False
        st.paused = False
        st.integ_queue = []
        st.integ_blocked = None
        st.done = {PHASE: "operator"}
    return state_mod.read(cfg)


def test_a_live_hand_off_holds_the_finish_open(cfg):
    queue(cfg, PHASE)
    sup = Supervisor(cfg)

    assert sup._operator_blocking() == [PHASE]
    sup._finish_if_settled(_settled(cfg))

    assert state_mod.read(cfg).finished is False
    assert "FINISH-HELD operator=" in cfg.supervisor_log.read_text(encoding="utf-8")


def test_an_abandoned_hand_off_does_not_block_and_finish_completes(cfg):
    """The deadlock regression, in one test.

    `_operator_blocking` is checked BESIDE `State.pending()` and not inside it,
    and it is bounded by the queue's own attempt cap. Get either wrong and a
    hand-off nothing can drain becomes a run nothing can finish — the queue built
    to stop losing notes would instead hang the swarm on one.
    """
    queue(cfg, PHASE)
    opqueue.abandon(cfg, PHASE, "three attempts, three crashes")
    sup = Supervisor(cfg)

    assert sup._operator_blocking() == []
    assert operator_mod.outstanding(cfg) == []  # terminal: nothing is owed
    sup._finish_if_settled(_settled(cfg))

    assert state_mod.read(cfg).finished is True
    assert any("swarm finished" in ln for ln in tg_lines(cfg))


def test_an_item_past_its_attempt_cap_stops_blocking(cfg):
    """Bounded, not merely non-terminal: a blocker with no bound is a hang."""
    queue(cfg, PHASE)
    item = opqueue.load(cfg, PHASE)
    item.attempts = opqueue.MAX_ATTEMPTS
    opqueue._write(cfg, item)

    assert Supervisor(cfg)._operator_blocking() == []
    assert operator_mod.outstanding(cfg) == [PHASE]  # still named at finish


def test_a_running_hand_off_blocks_on_its_last_attempt(cfg, log):
    """A session live right now is not "past the cap" — it is the cap being spent."""
    queue(cfg, PHASE)
    item = opqueue.load(cfg, PHASE)
    item.attempts = opqueue.MAX_ATTEMPTS - 1
    opqueue._write(cfg, item)
    assert operator_mod.dispatch(cfg, PHASE, log) is True

    assert opqueue.load(cfg, PHASE).attempts == opqueue.MAX_ATTEMPTS
    assert Supervisor(cfg)._operator_blocking() == [PHASE]


# -- reaching the supervisor's clock --------------------------------------
def test_the_queues_next_deadline_reaches_the_select_timeout(cfg):
    """A backed-off item is a timer, and nothing else in the loop would wake for it.

    `_watchdog_tick` returns while `idle < watchdog_s` and `_handle` refreshes the
    idle clock on every FIFO line, so a moving swarm never reaches the quiet
    point. Folding `opqueue.next_deadline` into `_next_timeout` is what makes the
    deadline fire at all — and with `watchdog_s = 0` it is the only thing that does.
    """
    queue(cfg, PHASE)
    sup = Supervisor(cfg)
    assert sup._next_timeout() is None  # nothing scheduled: an eligible item is not a timer

    item = opqueue.load(cfg, PHASE)
    item.run_after = time.time() + 120
    opqueue._write(cfg, item)

    timeout = sup._next_timeout()
    assert timeout is not None and 0 < timeout <= 120
    assert opqueue.next_deadline(cfg) == pytest.approx(item.run_after)


def test_a_park_deadline_still_wins_when_it_is_sooner(cfg):
    """Both kinds of deadline share one `min()`; neither may shadow the other."""
    queue(cfg, PHASE)
    item = opqueue.load(cfg, PHASE)
    item.run_after = time.time() + 600
    opqueue._write(cfg, item)
    with state_mod.transaction(cfg) as st:
        st.waiting["some-phase"] = time.time() + 5

    assert Supervisor(cfg)._next_timeout() <= 5


# -- the session signalling back ------------------------------------------
def test_operator_done_completes_the_item_and_clears_the_lease(cfg, log):
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True

    result = cli(cfg, "operator-done", PHASE)

    assert result.returncode == 0, result.stderr
    assert opqueue.load(cfg, PHASE).state == opqueue.DONE
    # The CLI settles the item durably; the supervisor drops the lease on the poke.
    assert operator_mod.release(cfg, log) == PHASE
    assert state_mod.read(cfg).operator_phase is None
    assert Supervisor(cfg)._operator_blocking() == []


def test_operator_done_files_its_outcome_in_the_phase_history(cfg, log):
    from swarm_orchestrator import ledgerw

    ledger = cfg.project_dir / cfg.ledger
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("- [ ] `teal-W10` · needs:— · **the teal**\n", encoding="utf-8")
    queue(cfg, "teal-W10")
    assert operator_mod.dispatch(cfg, "teal-W10", log) is True

    assert cli(cfg, "operator-done", "teal-W10", "rolled the kit back").returncode == 0

    [op] = ledgerw.pending(cfg)[ledgerw.NOW]["ops"]
    assert (op["kind"], op["phase"], op["outcome"], op["note"]) == (
        "record", "teal-W10", "note", "rolled the kit back")


def test_operator_done_on_a_phase_with_no_hand_off_is_refused(cfg):
    assert cli(cfg, "operator-done", PHASE).returncode == 1


KEY = f"operator:{PHASE}"


def test_waiting_pings_once_and_keeps_the_session_waiting(cfg, log):
    """A ping plus a held lease — the session stays and asks in its own window."""
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True

    result = cli(cfg, "waiting", KEY, "which", "host", "is", "the", "gateway")

    assert result.returncode == 0, result.stderr
    assert "AskUserQuestion" in result.stdout and "swarm resumed" in result.stdout
    asked = [ln for ln in tg_lines(cfg) if "waiting on you" in ln]
    assert asked == [f"swarm: operator job {PHASE} is waiting on you — which host is the gateway"]
    item = opqueue.load(cfg, PHASE)
    assert item.state == opqueue.WAITING and not item.terminal
    st = state_mod.read(cfg)
    assert st.operator_phase == PHASE  # the lease is still the session's
    assert st.operator_lease_until == item.lease_until
    assert st.operator_lease_until > time.time() + opqueue.LEASE_S
    # ...and it holds the finish open: ending the run would kill the session.
    assert Supervisor(cfg)._operator_blocking() == [PHASE]


def test_inside_the_session_the_job_id_alone_is_enough(cfg, log, monkeypatch):
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True
    monkeypatch.setenv(operator_mod.JOB_ENV, PHASE)

    assert cli(cfg, "waiting", PHASE, "which host").returncode == 0
    assert opqueue.load(cfg, PHASE).state == opqueue.WAITING


def test_waiting_does_not_telegram_a_second_time(cfg, log):
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True
    cli(cfg, "waiting", KEY, "which host")
    before = len(tg_lines(cfg))

    assert cli(cfg, "waiting", KEY, "which host").returncode == 0
    assert len(tg_lines(cfg)) == before


def test_waiting_on_a_job_that_is_not_running_is_refused(cfg):
    queue(cfg, PHASE)  # queued, never dispatched

    assert cli(cfg, "waiting", KEY, "which host").returncode == 1
    assert tg_lines(cfg) == []


def test_a_waiting_session_survives_the_sweep_past_its_ordinary_lease(cfg, log):
    """The owner answers when they reach a keyboard; an hour is not a verdict."""
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True
    assert cli(cfg, "waiting", KEY, "which host").returncode == 0

    operator_mod.sweep(cfg, log, now=time.time() + opqueue.LEASE_S * 5)

    assert opqueue.load(cfg, PHASE).state == opqueue.WAITING
    assert state_mod.read(cfg).operator_phase == PHASE
    assert "OPERATOR-LEASE-EXPIRED" not in cfg.supervisor_log.read_text()


def test_a_waiting_session_still_lets_go_after_the_wait_lease(cfg, log):
    """A week, not forever: a session that truly vanished must free the queue."""
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True
    assert cli(cfg, "waiting", KEY, "which host").returncode == 0

    operator_mod.sweep(cfg, log, now=time.time() + opqueue.WAIT_LEASE_S + 60)

    assert opqueue.load(cfg, PHASE).state == opqueue.QUEUED
    assert state_mod.read(cfg).operator_phase is None


def test_resumed_puts_the_session_back_on_an_ordinary_lease(cfg, log):
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True
    assert cli(cfg, "waiting", KEY, "which host").returncode == 0

    result = cli(cfg, "resumed", KEY, "the", "staging", "box")

    assert result.returncode == 0, result.stderr
    item = opqueue.load(cfg, PHASE)
    assert item.state == opqueue.RUNNING and item.answer == "the staging box"
    st = state_mod.read(cfg)
    assert st.operator_phase == PHASE
    assert st.operator_lease_until == item.lease_until
    assert st.operator_lease_until <= time.time() + opqueue.LEASE_S + 1
    assert cli(cfg, "resumed", KEY).returncode == 1  # not waiting now
    # The owner's call is history, filed under the job's phase.
    [owner] = notes_mod.load(cfg, opqueue.owning_phase(PHASE))
    assert (owner.kind, owner.text) == (
        notes_mod.OWNER_DECISION, "the staging box (asked: which host)")


def ledger_rows(cfg) -> list[dict]:
    path = Path(str(cfg.state_dir)) / "notifications.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_a_routine_operator_outcome_is_recorded_but_pings_nobody(cfg, log):
    """The default is quiet: a stream of routine outcome pings is noise."""
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True

    result = cli(cfg, "operator-done", PHASE, "rolled", "webhooks;", "healthz", "green")

    assert result.returncode == 0, result.stderr
    item = opqueue.load(cfg, PHASE)
    assert item.state == opqueue.DONE and item.outcome == "rolled webhooks; healthz green"
    assert item.attention is False
    assert tg_lines(cfg) == []
    # Still in the ledger (the dashboard's alerts), marked held back, not dropped.
    [row] = ledger_rows(cfg)
    assert row["kind"] == "operator-done" and "rolled webhooks; healthz green" in row["text"]
    assert row["delivered"] is False and row["suppressed"] and row["error"] is None
    assert "not pinged" in result.stdout


def test_an_outcome_flagged_for_attention_is_sent(cfg, log):
    """``--attention`` is the outcome on the phone, not a question: a decision is
    asked with ``swarm waiting`` while the session can still act on it."""
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True

    result = cli(cfg, "operator-done", PHASE, "api-F26 NOT rolled; roll owed", "--attention")

    assert result.returncode == 0, result.stderr
    assert opqueue.load(cfg, PHASE).attention is True
    assert tg_lines(cfg) == [f"swarm: operator job {PHASE} needs your attention — api-F26 NOT rolled; roll owed"]


def test_notify_all_pings_every_outcome(cfg, log, monkeypatch):
    monkeypatch.setenv("SWARM_OPERATOR_NOTIFY", "all")
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True

    assert cli(cfg, "operator-done", PHASE, "already done").returncode == 0
    [line] = tg_lines(cfg)
    assert "already done" in line


def test_telegram_pings_all_restores_every_outcome_ping(cfg, log, monkeypatch):
    """`[telegram].pings = "all"` is the one switch back to the old behaviour."""
    monkeypatch.setenv("SWARM_TG_PINGS", "all")
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True
    assert cli(cfg, "operator-done", PHASE, "already done").returncode == 0
    assert "already done" in "\n".join(tg_lines(cfg))


def test_notify_none_silences_even_attention_but_not_questions_or_abandons(
    cfg, log, monkeypatch
):
    monkeypatch.setenv("SWARM_OPERATOR_NOTIFY", "none")
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True
    assert cli(cfg, "waiting", KEY, "which", "host?").returncode == 0
    assert cli(cfg, "resumed", KEY, "staging").returncode == 0
    assert cli(cfg, "operator-done", PHASE, "roll owed", "--attention").returncode == 0
    queue(cfg, OTHER)
    opqueue.abandon(cfg, OTHER, "three crashes")

    sent = "\n".join(tg_lines(cfg))
    assert "waiting on you" in sent and "which host?" in sent
    assert "ABANDONED" in sent and OTHER in sent
    assert "roll owed" not in sent
    assert [(r["kind"], r["delivered"]) for r in ledger_rows(cfg)] == [
        ("waiting", True), ("operator-done", False), ("operator-abandoned", True)]


def test_an_unknown_flag_never_fails_operator_done(cfg, log):
    """A newer or older prompt's flag must not lose a finished job."""
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True

    result = cli(cfg, "operator-done", PHASE, "--quiet", "done and verified", "--level=2")

    assert result.returncode == 0, result.stderr
    item = opqueue.load(cfg, PHASE)
    assert item.state == opqueue.DONE and item.outcome == "done and verified"
    # Every other command still refuses what it does not know.
    assert cli(cfg, "status", "--bogus").returncode == 2


def test_a_bad_notify_value_falls_back_to_the_quiet_default(cfg, monkeypatch):
    monkeypatch.setenv("SWARM_OPERATOR_NOTIFY", "loud")
    assert load(project_dir=str(cfg.project_dir)).operator_notify == "attention"


def test_the_brief_carries_the_job_its_exits_and_an_earlier_question(cfg):
    item = queue(cfg, PHASE)
    line = operator_mod.brief(cfg, item)

    assert "\n" not in line  # send-keys submits on every newline
    assert NOTE in line and f"swarm operator-done {PHASE}" in line
    assert f"swarm waiting {PHASE}" in line and f"swarm resumed {PHASE}" in line
    assert f"swarm operator-done {PHASE} " in line and "--not-before" in line
    assert "project itself" in line

    item.question, item.answer = "which host?", ""
    assert "An earlier attempt asked the owner: which host?" in operator_mod.brief(cfg, item)


def test_the_session_is_built_like_a_worker_but_with_its_own_model(cfg, monkeypatch):
    """Full, unrestrained: the worker's settings and effort, the operator's model,
    and no phase marker — it is not that phase's worker."""
    cfg.operator_model = "opus"
    cfg.worker_settings = '{"teammateMode":"in-process"}'
    cfg.worker_effort = "high"
    monkeypatch.delenv("SWARM_PHASE", raising=False)

    cmd = operator_mod.operator_command(cfg, PHASE)
    env = operator_mod._operator_env(cfg, PHASE)

    assert cmd.startswith(f"cd {cfg.project_dir} && exec claude --model opus")
    assert "--settings" in cmd and "--effort high" in cmd
    assert f"operator:{PHASE}" in cmd
    assert env["SWARM_STATE_DIR"] == str(cfg.state_dir)
    assert env[operator_mod.JOB_ENV] == PHASE
    assert cfg.env_marker not in env


# -- the queue order ------------------------------------------------------
def test_the_sweep_skips_a_phase_that_is_still_building(cfg, log):
    """Its hand-off is about work that is not in main yet."""
    queue(cfg, PHASE)
    queue(cfg, OTHER, note="restart the unit on the build host")
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot(PHASE)

    assert operator_mod.sweep(cfg, log) is True
    assert state_mod.read(cfg).operator_phase == OTHER


def test_operator_add_queues_a_job_behind_the_existing_ones(cfg, log):
    queue(cfg, PHASE)

    result = cli(cfg, "operator-add", "re-run", "the", "post-deploy", "smoke", "check")

    assert result.returncode == 0, result.stderr
    items = opqueue.load_all(cfg)
    assert [i.phase for i in items][0] == PHASE
    added = items[1]
    assert added.phase.startswith("op-") and added.source == opqueue.ADDED
    assert operator_mod.sweep(cfg, log) is True
    assert state_mod.read(cfg).operator_phase == PHASE  # oldest first


def test_operator_add_with_a_phase_names_the_job_after_it(cfg):
    result = cli(cfg, "operator-add", "verify the roll", "--phase", "olive-W3e")

    assert result.returncode == 0, result.stderr
    assert opqueue.load(cfg, "olive-W3e").note == "verify the roll"


def test_operator_add_is_refused_while_the_feature_is_off(off):
    result = cli(off, "operator-add", "verify the roll")

    assert result.returncode == 2 and "enabled" in result.stderr
    assert opqueue.load_all(off) == []


# -- visibility -----------------------------------------------------------
def test_status_shows_the_queue_counts_and_the_job_waiting_on_you(cfg, log):
    queue(cfg, PHASE)
    queue(cfg, OTHER, note="restart the unit on the build host")
    assert operator_mod.dispatch(cfg, PHASE, log) is True
    assert cli(cfg, "waiting", KEY, "which host").returncode == 0

    out = cli(cfg, "status").stdout

    assert "queued=1 running=0 waiting-on-owner=1 done=0 abandoned=0" in out
    assert f"current: {PHASE} [WAITING ON YOU: which host]" in out


def test_status_says_when_the_operator_is_off(off):
    off.operator_dir.mkdir(parents=True, exist_ok=True)
    opqueue._write(off, opqueue.Item(phase=PHASE, note=NOTE, queued_at=time.time()))

    assert "operator (OFF" in cli(off, "status").stdout


def test_doctor_warns_on_a_job_waiting_on_you_over_an_hour_and_on_abandoned(cfg):
    from swarm_orchestrator import doctor

    state_mod.init_state(cfg)
    queue(cfg, PHASE)
    queue(cfg, OTHER, note="restart the unit on the build host")
    opqueue.lease(cfg, PHASE)
    opqueue.wait_on_owner(cfg, PHASE, "which host?", now=time.time() - 7200)
    opqueue.abandon(cfg, OTHER, "gave up")

    [check] = [c for c in doctor.run_checks(cfg) if c.name == "operator"]

    assert check.status == doctor.WARN
    assert PHASE in check.detail and "which host?" in check.detail
    assert f"{OTHER} abandoned" in check.detail


def test_doctor_is_quiet_about_a_fresh_question(cfg):
    from swarm_orchestrator import doctor

    queue(cfg, PHASE)
    opqueue.lease(cfg, PHASE)
    opqueue.wait_on_owner(cfg, PHASE, "which host?")

    assert doctor._check_operator(cfg).status == doctor.OK


def test_the_drawer_renders_a_waiting_job(cfg):
    from types import SimpleNamespace

    from swarm_orchestrator.tui import data, drawer

    queue(cfg, PHASE)
    opqueue.lease(cfg, PHASE)
    opqueue.wait_on_owner(cfg, PHASE, "which host?")
    snap = data.build_snapshot(
        cfg, {"slots": [], "done": {}}, operator=opqueue.load_all(cfg)
    )

    rows = drawer.operator_rows(SimpleNamespace(snapshot=snap))
    assert len(rows) == 1 and "waiting" in rows[0].text
    [blocker] = snap.blockers
    assert (blocker.kind, blocker.question) == ("operator-ask", "which host?")


# -- the replay of the dropped hand-offs ----------------------------------
def test_enabling_the_operator_replays_legacy_sentinels_one_at_a_time_oldest_first(
    cfg, log
):
    """The hand-offs dropped while the operator was off: each left only
    its one-line `done/<phase>.operator` sentinel. Turning the operator on and
    running `swarm up` must queue every one, and drain them one at a time,
    oldest first."""
    cfg.done_dir.mkdir(parents=True, exist_ok=True)
    phases = ["coral-W4", "olive-W2", "api-F9"]
    for n, phase in enumerate(phases):
        path = cfg.done_dir / f"{phase}.operator"
        path.write_text(f"{phase} operator roll {phase} to the live box and verify\n",
                        encoding="utf-8")
        stamp = 1_700_000_000 + n * 60
        import os

        os.utime(path, (stamp, stamp))
    assert opqueue.load_all(cfg) == []

    rebuilt = opqueue.reconcile(cfg, log)

    assert sorted(rebuilt) == sorted(f"rebuilt {p}" for p in phases)
    items = opqueue.load_all(cfg)
    assert [i.phase for i in items] == phases  # oldest sentinel first
    assert items[0].note == "roll coral-W4 to the live box and verify"
    for expected in phases:
        assert operator_mod.sweep(cfg, log) is True
        assert state_mod.read(cfg).operator_phase == expected
        assert operator_mod.sweep(cfg, log) is False  # one at a time
        opqueue.complete(cfg, expected, "done")
        operator_mod.release(cfg, log)
    assert opqueue.pending(cfg) == []


# -- the owner's escape hatches -------------------------------------------
def test_finish_refuses_an_undrained_queue_without_force(cfg):
    """`finish` used to poke a bare shutdown, which killed the session and the
    queue with it — silently."""
    queue(cfg, PHASE)

    refused = cli(cfg, "finish")

    assert refused.returncode == 1
    assert PHASE in refused.stderr
    assert "--force" in refused.stderr
    assert cli(cfg, "finish", "--force").returncode == 0


def test_finish_is_unchanged_when_the_queue_is_empty(cfg):
    assert cli(cfg, "finish").returncode == 0


def test_swarm_operator_queues_a_legacy_sentinel_by_hand(cfg):
    """Legacy `needs-owner` notes: opt-in, one at a time.

    `opqueue.reconcile` deliberately never rebuilds these — auto-routing notes
    that landed days ago would open a session per note on the next `swarm up`.
    """
    cfg.done_dir.mkdir(parents=True, exist_ok=True)
    (cfg.done_dir / f"{PHASE}.needs-owner").write_text(
        f"{PHASE} needs-owner {NOTE}\n", encoding="utf-8"
    )
    assert opqueue.load(cfg, PHASE) is None

    result = cli(cfg, "operator", PHASE)

    assert result.returncode == 0, result.stderr
    item = opqueue.load(cfg, PHASE)
    assert item is not None and item.note == NOTE


def test_swarm_operator_on_an_unknown_phase_says_so(cfg):
    assert cli(cfg, "operator", "no-such-phase").returncode == 1


def test_swarm_operator_is_refused_while_the_feature_is_off(off):
    assert cli(off, "operator", PHASE).returncode == 2


# -- the prompt -----------------------------------------------------------
def test_the_operator_prompt_lints_clean():
    """It ships in `cli._prompt_files`, so `swarm check` reads it on every run."""
    text = (REPO / "prompts" / "operator.md").read_text(encoding="utf-8")
    assert promptlint.lint(text, known_commands=_known_commands()) == []


def test_swarm_check_passes_with_the_operator_prompt_shipped(swarm):
    result = swarm.cli("check")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "prompts/operator.md" not in result.stdout  # only unclean prompts print
    assert "all checks passed" in result.stdout


# -- through the real supervisor loop -------------------------------------
def _operator_run(swarm) -> None:
    """A one-phase run with the operator armed and no model within reach.

    The triage seam is not optional here: with `[operator]` on, `swarm done`
    detaches a real `swarm operator-triage`, and without the seam that reaches
    for a model. It answers `now` so the dispatch is deterministic whichever of
    the two paths wins the race — the merge-time hook, or the triage child's poke.
    """
    (swarm.project / "oponly.txt").write_text("OP1\n", encoding="utf-8")
    toml = swarm.project / ".swarm.toml"
    toml.write_text(
        toml.read_text().replace('ledger = "ledger.txt"', 'ledger = "oponly.txt"')
        + '\n[operator]\nenabled = true\n',
        encoding="utf-8",
    )
    swarm.env["SWARM_TRIAGE_CMD"] = 'echo \'{"when":"now","why":"t","group":"deploy"}\''
    swarm.env["FAKE_WORKER_PARK"] = "1"  # the worker's `done` is the test's to send


def test_a_merged_operator_finish_opens_a_session_and_holds_the_finish(swarm):
    """The whole feature, end to end: `_advance_done` dispatches, and the run
    cannot finish while the hand-off is owed."""
    _operator_run(swarm)
    swarm.up()
    assert swarm.wait(lambda: swarm.busy_phases() == ["OP1"], timeout=25), swarm.log_text()

    swarm.cli("done", "OP1", "operator", NOTE)

    assert swarm.wait(
        lambda: "OPERATOR-DISPATCH OP1" in swarm.log_text(), timeout=25
    ), swarm.log_text()
    assert swarm.state()["operator_phase"] == "OP1"
    assert swarm.wait(
        lambda: "MASTER-IDLE" in swarm.log_text() or "master-idle" in swarm.log_text(),
        timeout=25,
    ), swarm.log_text()
    assert not swarm.finished(), swarm.log_text()
    assert "ACTION finish" not in swarm.log_text()


def test_operator_done_releases_the_lease_and_lets_the_run_finish(swarm):
    """`operator-done` is what ends the run that its hand-off was holding open."""
    _operator_run(swarm)
    swarm.up()
    assert swarm.wait(lambda: swarm.busy_phases() == ["OP1"], timeout=25), swarm.log_text()
    swarm.cli("done", "OP1", "operator", NOTE)
    assert swarm.wait(
        lambda: "OPERATOR-DISPATCH OP1" in swarm.log_text(), timeout=25
    ), swarm.log_text()

    swarm.cli("operator-done", "OP1")

    assert swarm.wait(
        lambda: "ACTION finish" in swarm.log_text(), timeout=30
    ), swarm.log_text()
    st = swarm.state()
    assert st["finished"]
    assert st["operator_phase"] is None
    assert sum("swarm finished" in ln for ln in swarm.tg_lines()) == 1
    # Drained, so the finish has nothing to name.
    assert not any("undrained" in ln for ln in swarm.tg_lines())


def test_swarm_up_requeues_dropped_hand_offs_under_isolation_none(swarm):
    """The requeue ran only under worktree isolation, and the swarm also runs
    with "none" — so turning the operator on would have replayed nothing there."""
    _operator_run(swarm)
    done_dir = swarm.state_dir / "done"
    done_dir.mkdir(parents=True, exist_ok=True)
    (done_dir / "old-W1.operator").write_text(
        f"old-W1 operator {NOTE}\n", encoding="utf-8"
    )

    out = swarm.up().stdout

    assert "operator queue: rebuilt old-W1" in out
    assert (swarm.state_dir / "operator" / "old-W1.json").is_file()
