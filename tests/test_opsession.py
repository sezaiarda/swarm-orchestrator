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

import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarm_orchestrator import operator as operator_mod
from swarm_orchestrator import opqueue, promptlint
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import _known_commands
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor

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
    # ...but the sweep still drains it, which is the only reason it is not lost.
    assert operator_mod.sweep(cfg, log) is True
    assert state_mod.read(cfg).operator_phase == PHASE


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


def test_operator_done_on_a_phase_with_no_hand_off_is_refused(cfg):
    assert cli(cfg, "operator-done", PHASE).returncode == 1


def test_operator_ask_telegrams_exactly_once(cfg, log):
    """The single message in this whole flow that reaches the owner."""
    queue(cfg, PHASE)
    assert operator_mod.dispatch(cfg, PHASE, log) is True

    result = cli(cfg, "operator-ask", PHASE, "which", "host", "is", "the", "gateway")

    assert result.returncode == 0, result.stderr
    asked = [ln for ln in tg_lines(cfg) if PHASE in ln]
    assert len(asked) == 1
    assert "which host is the gateway" in asked[0]
    # ...and it stops blocking, because an answer may never come.
    assert opqueue.load(cfg, PHASE).terminal
    assert Supervisor(cfg)._operator_blocking() == []


def test_operator_ask_does_not_telegram_a_second_time(cfg, log):
    queue(cfg, PHASE)
    cli(cfg, "operator-ask", PHASE, "which host")
    before = len(tg_lines(cfg))

    assert cli(cfg, "operator-ask", PHASE, "which host").returncode == 1
    assert len(tg_lines(cfg)) == before


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
