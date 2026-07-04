"""Tier A: the pure-injection lifecycle end-to-end, bare driver (no tmux/claude).

Real supervisor + FIFO + sentinels + state machine, driven by fake master/worker
processes. Proves fan-out, dep gating, injection, convergence, deadlock-freedom
under the four rules, and that a parked worker blocks finish.
"""

from __future__ import annotations

import time


def _point_ledger(swarm, name: str) -> None:
    toml = swarm.project / ".swarm.toml"
    toml.write_text(
        toml.read_text().replace('ledger = "ledger.txt"', f'ledger = "{name}"'),
        encoding="utf-8",
    )


def test_fanout_convergence_and_single_finish(swarm):
    """P0 -> {P1,P2,P3} fan-out -> P4 join -> finish exactly once + 1 telegram."""
    swarm.env["FAKE_WORKER_SLEEP"] = "2"  # widen the 3-busy window
    swarm.up()

    # Assertion 1: three slots busy at fan-out.
    assert swarm.wait(lambda: swarm.busy_count() == 3, timeout=25), swarm.log_text()
    assert set(swarm.busy_phases()) == {"P1", "P2", "P3"}

    # Assertion: converges and finishes. Wait on the terminal log line, which is
    # written last (after state.finished + telegram), to avoid reading mid-finish.
    assert swarm.wait(
        lambda: "ACTION finish" in swarm.log_text(), timeout=40
    ), swarm.log_text()
    st = swarm.state()
    assert st["finished"]
    assert set(st["done"]) >= {"P0", "P1", "P2", "P3", "P4"}
    assert swarm.log_text().count("ACTION finish") == 1  # finish fires once
    assert swarm.wait(lambda: len(swarm.tg_lines()) == 1, timeout=5)
    assert "finished" in swarm.tg_lines()[0]


def test_two_dones_while_master_alive_are_injected(swarm):
    """Assertion 3: two dones while a master is ALIVE -> both injected + reused."""
    swarm.env["FAKE_MASTER_WAIT"] = "8"  # keep the master alive across both dones
    swarm.env["FAKE_WORKER_PARK"] = "1"  # workers hold slots; test controls `done`
    _point_ledger(swarm, "ledger_inject.txt")
    swarm.up()

    # init master launches A,B (parked); it then idles-waits, ALIVE.
    assert swarm.wait(
        lambda: swarm.busy_phases() == ["A", "B"] and swarm.state()["master_alive"],
        timeout=20,
    ), swarm.log_text()

    # Fire both completions in quick succession while the master is alive.
    swarm.cli("done", "A", "ok")
    swarm.cli("done", "B", "ok")

    # Both injected -> the live master launches X and Y into the freed slots.
    assert swarm.wait(lambda: swarm.busy_phases() == ["X", "Y"], timeout=20), (
        swarm.log_text()
    )
    log = swarm.log_text()
    assert log.count("ACTION inject-master") >= 2  # both dones injected
    assert log.count("ACTION spawn-master") == 1  # only the init master spawned


def test_parked_worker_blocks_finish(swarm):
    """Assertion 5 variant: a worker that never signals `done` blocks finish."""
    swarm.env["FAKE_WORKER_PARK"] = "1"
    swarm.up()

    assert swarm.wait(lambda: swarm.busy_phases() == ["P0"], timeout=20), (
        swarm.log_text()
    )
    # Let the master idle + be killed; finish must NOT fire (slot parked busy).
    time.sleep(2.5)
    assert not swarm.finished()
    assert not any("finished" in ln for ln in swarm.tg_lines())
    assert swarm.busy_phases() == ["P0"]


def test_pause_holds_then_resume_advances(swarm):
    """`pause` frees a finished worker's slot but launches nothing + never
    finishes; `resume` then advances the fan-out."""
    swarm.env["FAKE_WORKER_PARK"] = "1"  # workers hold their slot; test drives `done`
    swarm.up()

    # init master launches P0 (parked), then idles.
    assert swarm.wait(lambda: swarm.busy_phases() == ["P0"], timeout=20), swarm.log_text()

    swarm.cli("pause")
    swarm.cli("done", "P0", "ok")  # P0 completes while paused

    # Held: P0's slot frees, but nothing new launches and finish never fires.
    assert swarm.wait(lambda: swarm.busy_phases() == [], timeout=10), swarm.log_text()
    time.sleep(1.5)
    assert not swarm.finished()
    assert swarm.busy_phases() == []  # P1/P2/P3 NOT launched while paused
    assert "DONE-PAUSED" in swarm.log_text()

    # Resume -> the fan-out launches into the free slots.
    swarm.cli("resume")
    assert swarm.wait(
        lambda: set(swarm.busy_phases()) == {"P1", "P2", "P3"}, timeout=20
    ), swarm.log_text()


def test_done_never_hangs_when_supervisor_down(swarm):
    """Assertion 4: `swarm done` returns immediately with no supervisor."""
    import os

    swarm.state_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(swarm.state_dir / "control.fifo")  # exists, but no reader (ENXIO)

    start = time.monotonic()
    proc = swarm.cli("done", "P0", "ok", timeout=5)
    elapsed = time.monotonic() - start

    assert proc.returncode == 0
    assert elapsed < 3.0  # best-effort poke never blocks
    assert (swarm.state_dir / "done" / "P0.ok").is_file()
