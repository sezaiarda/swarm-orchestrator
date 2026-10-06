"""Self-classified completion: a finishing worker's status decides what the owner hears.

Bare driver (no tmux/claude), real supervisor + FIFO + sentinels. Workers park
(hold their slot, emit no `done`) so the test drives `swarm done <phase> <status>`
itself and inspects the telegram sink:

  - `ok`          -> integrate/advance, NO telegram (silent success).
  - `needs-owner` -> retired: recorded as `operator`, which integrates like
                     `ok` and hands off to an operator session; with the operator
                     off the owner is asked to do it.
  - `fail`        -> rolled back; with no Overseer to retry it, the owner is asked
                     (the suite runs with the Overseer off).
"""

from __future__ import annotations


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


def test_done_needs_owner_is_recorded_as_operator_and_still_advances(swarm):
    """The retired spelling still works: it lands as `operator`, like `ok`.

    With the operator off (the demo's default) the owner is asked, once, to do
    the hand-off — never told it failed, and never not at all.
    """
    _up_with_parked_p0(swarm)

    swarm.cli("done", "P0", "needs-owner", "check", "the", "auth", "change")

    assert swarm.wait(
        lambda: set(swarm.busy_phases()) == {"P1", "P2", "P3"}, timeout=20
    ), swarm.log_text()
    assert swarm.state()["done"].get("P0") == "operator"
    asks = [ln for ln in swarm.tg_lines() if "P0" in ln]
    assert asks == [
        f"[{swarm.project.name}] Asks you: Do the follow-up that P0 left behind: the operator"
        " is switched off, so nobody else will. `swarm todo` shows what is left to do."
    ], swarm.tg_lines()
    assert "check the auth change" in swarm.cli("todo").stdout  # the recap is the to-do


def test_done_fail_asks_the_owner_and_keeps_the_recap(swarm):
    """A `fail` nothing will retry asks the owner what to do; the recap stays in the log."""
    import json

    _up_with_parked_p0(swarm)

    swarm.cli("done", "P0", "fail", "build", "broke")

    asks = [ln for ln in swarm.tg_lines() if "Asks you:" in ln]
    assert len(asks) == 1, swarm.tg_lines()
    assert asks[0].startswith(f"[{swarm.project.name}] Asks you: Fix what stopped P0")
    assert "then run `swarm retry P0`: it failed, and 4 phases wait on it" in asks[0]
    rows = [json.loads(ln) for ln in
            (swarm.state_dir / "notifications.jsonl").read_text().splitlines()]
    [row] = [r for r in rows if r["kind"] == "worker-done"]
    assert row["class"] == "ask" and row["detail"] == "build broke"
