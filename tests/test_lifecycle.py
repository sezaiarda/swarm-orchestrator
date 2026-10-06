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
    # A clean-success (`ok`) worker is SILENT: it self-classifies its outcome, and
    # nothing is said for a clean success. Every phase here finishes `ok`, so the
    # ONLY telegram the owner gets is the run's last summary. The ACTION-finish
    # wait above guarantees it is flushed.
    tg = swarm.tg_lines()
    assert tg == [f"[{swarm.project.name}] Overseer: The run has finished: 5 phase(s)"
                  " landed. Nothing waits on you."]
    assert not any("worker complete" in ln for ln in tg)  # ok completions silent


def test_second_up_is_refused_while_supervisor_running(swarm):
    """A second `swarm up` must not spawn a co-reader on the control FIFO.

    Regression for the two-supervisor bug: a stray supervisor holding the FIFO
    would otherwise race a fresh one, so `up` must refuse while one is attached.
    """
    swarm.up()
    assert swarm.wait(
        lambda: bool(swarm.state() and swarm.state().get("supervisor_pid")), timeout=20
    ), swarm.log_text()
    proc = swarm.cli("up", check=False)
    assert proc.returncode != 0
    assert "already running" in (proc.stdout + proc.stderr).lower()


def test_two_quick_dones_launch_both_successors_without_a_master(swarm):
    """Assertion 3: two dones in quick succession each fill their freed slot —
    straight from the supervisor, with no master spawned or nudged after the
    init pass. (This used to prove both were INJECTED into a live master.)"""
    swarm.env["FAKE_WORKER_PARK"] = "1"  # workers hold slots; test controls `done`
    _point_ledger(swarm, "ledger_inject.txt")
    swarm.up()

    # The supervisor launches A,B once the init master idles.
    assert swarm.wait(lambda: swarm.busy_phases() == ["A", "B"], timeout=20), (
        swarm.log_text()
    )
    assert swarm.wait(lambda: "EVENT master-idle" in swarm.log_text(), timeout=10)

    # Fire both completions in quick succession.
    swarm.cli("done", "A", "ok")
    swarm.cli("done", "B", "ok")

    assert swarm.wait(lambda: swarm.busy_phases() == ["X", "Y"], timeout=20), (
        swarm.log_text()
    )
    log = swarm.log_text()
    assert log.count("ACTION spawn-master") == 1  # only the init master spawned
    assert "ACTION inject-master" not in log  # nothing nudged a master
    assert "LAUNCH-READY A B (init master idle)" in log
    assert log.count("LAUNCH X ") == 1 and log.count("LAUNCH Y ") == 1  # no double launch


def test_the_first_launch_waits_for_the_init_master(swarm):
    """The init master commits the patched worker command; a worktree cut before
    that commit would not carry it. So nothing launches while it is mid-pass."""
    swarm.env["FAKE_MASTER_WAIT"] = "3"
    swarm.up()
    assert swarm.wait(lambda: "ACTION spawn-master kind=init" in swarm.log_text(), timeout=10)
    time.sleep(1.5)
    assert swarm.busy_phases() == []  # held behind the bootstrap pass
    assert swarm.wait(lambda: swarm.busy_phases() == ["P0"] or "LAUNCH P0" in swarm.log_text(),
                      timeout=20), swarm.log_text()
    log = swarm.log_text()
    assert log.index("EVENT master-idle") < log.index("CLAIM P0")


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


def test_duplicate_done_is_ignored(swarm):
    """A second `swarm done` for an already-finished phase is a true no-op."""
    swarm.env["FAKE_WORKER_PARK"] = "1"  # workers hold; test drives `done`
    swarm.up()
    assert swarm.wait(lambda: swarm.busy_phases() == ["P0"], timeout=20), swarm.log_text()

    swarm.cli("done", "P0", "ok")  # P0 completes -> fan-out launches
    assert swarm.wait(
        lambda: set(swarm.busy_phases()) == {"P1", "P2", "P3"}, timeout=20
    ), swarm.log_text()

    busy_before = sorted(swarm.busy_phases())
    again = swarm.cli("done", "P0", "ok")  # duplicate -> must be ignored
    time.sleep(1.5)
    assert sorted(swarm.busy_phases()) == busy_before  # nothing disturbed
    assert "already recorded ok" in again.stdout


def test_done_never_hangs_when_supervisor_down(swarm):
    """Assertion 4: `swarm done` returns immediately with no supervisor."""
    import os

    swarm.state_dir.mkdir(parents=True, exist_ok=True)
    os.mkfifo(swarm.state_dir / "control.fifo")  # exists, but no reader (ENXIO)
    swarm.claim("P0")  # its worker outlived the supervisor

    start = time.monotonic()
    proc = swarm.cli("done", "P0", "ok", timeout=5)
    elapsed = time.monotonic() - start

    assert proc.returncode == 0
    assert elapsed < 3.0  # best-effort poke never blocks
    assert (swarm.state_dir / "done" / "P0.ok").is_file()


def test_pause_warns_when_no_supervisor(swarm):
    """`swarm pause`/`resume` warn (not silently no-op) when nothing is reading the
    state — the wrong-cwd footgun that makes a pause land on a state no live swarm
    observes. The flag is still written, but stderr says no supervisor is running.
    """
    r = swarm.cli("pause")
    assert r.returncode == 0
    assert swarm.state()["paused"] is True  # the flag IS written
    assert "no swarm supervisor is running" in r.stderr  # ...and the no-op is surfaced

    rr = swarm.cli("resume")
    assert rr.returncode == 0
    assert swarm.state()["paused"] is False
    assert "no swarm supervisor is running" in rr.stderr
