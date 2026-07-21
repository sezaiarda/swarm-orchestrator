"""Self-classified completion: a finishing worker's status decides the ping.

Bare driver (no tmux/claude), real supervisor + FIFO + sentinels. Workers park
(hold their slot, emit no `done`) so the test drives `swarm done <phase> <status>`
itself and inspects the telegram sink:

  - `ok`          -> integrate/advance, NO telegram (silent success).
  - `needs-owner` -> integrate/advance EXACTLY like `ok`, PLUS ping the recap.
  - `fail`        -> ping the recap (rollback path).
"""

from __future__ import annotations

import time


def _up_with_parked_p0(swarm) -> None:
    """Bring the swarm up with parked workers and wait for P0 to hold its slot."""
    swarm.env["FAKE_WORKER_PARK"] = "1"  # workers hold slots; the test drives `done`
    swarm.up()
    assert swarm.wait(lambda: swarm.busy_phases() == ["P0"], timeout=20), swarm.log_text()


def test_done_ok_is_silent(swarm):
    """`swarm done P0 ok` advances the phase but sends NO telegram."""
    _up_with_parked_p0(swarm)

    swarm.cli("done", "P0", "ok")

    # ok integrates/advances: P0's slot frees, the fan-out launches, P0 recorded.
    assert swarm.wait(
        lambda: set(swarm.busy_phases()) == {"P1", "P2", "P3"}, timeout=20
    ), swarm.log_text()
    assert swarm.state()["done"].get("P0") == "ok"
    # ...and a clean success is silent: nothing about P0 hits the sink.
    assert not any("P0" in ln for ln in swarm.tg_lines()), swarm.tg_lines()


def test_done_needs_owner_pings_recap_and_still_advances(swarm):
    """`needs-owner` telegrams the recap AND advances the phase like `ok`."""
    _up_with_parked_p0(swarm)

    swarm.cli("done", "P0", "needs-owner", "check", "the", "auth", "change")

    # The recap reaches the owner (the ping is written synchronously by `done`).
    tg = swarm.tg_lines()
    ping = [ln for ln in tg if "check the auth change" in ln]
    assert ping, tg
    assert "P0" in ping[0] and "needs you" in ping[0]

    # Still advances exactly like ok: slot freed, fan-out launches, phase recorded.
    assert swarm.wait(
        lambda: set(swarm.busy_phases()) == {"P1", "P2", "P3"}, timeout=20
    ), swarm.log_text()
    assert swarm.state()["done"].get("P0") == "needs-owner"


def test_done_fail_pings_recap(swarm):
    """`fail` telegrams the owner the recap."""
    _up_with_parked_p0(swarm)

    swarm.cli("done", "P0", "fail", "build", "broke")

    tg = swarm.tg_lines()
    ping = [ln for ln in tg if "build broke" in ln]
    assert ping, tg
    assert "P0" in ping[0] and "FAILED" in ping[0]


def test_done_grace_detaches_the_poke(swarm):
    """With a grace, `swarm done` returns immediately; the poke lands later.

    The grace must not sleep inside the worker's own process — the bash tool's
    timeout would kill it, and the poke with it. A detached child delivers the
    delayed poke, so `done` is non-blocking and the phase still advances after
    the grace elapses.
    """
    swarm.env["SWARM_DONE_GRACE"] = "3"
    _up_with_parked_p0(swarm)

    t0 = time.monotonic()
    swarm.cli("done", "P0", "ok")
    elapsed = time.monotonic() - t0
    assert elapsed < 2.5, f"done blocked {elapsed:.1f}s (in-process grace sleep?)"

    # The slot is still held during the grace (the supervisor hasn't heard yet)...
    assert swarm.busy_phases() == ["P0"]
    # ...and the detached poke lands after ~grace: fan-out proceeds as usual.
    assert swarm.wait(
        lambda: set(swarm.busy_phases()) == {"P1", "P2", "P3"}, timeout=20
    ), swarm.log_text()
    assert swarm.state()["done"].get("P0") == "ok"
