"""Campaign standings: what counts as work left, and what is history."""

from __future__ import annotations

from swarm_orchestrator.tui import campaign


def test_ledger_ticked_rows_are_history_not_work_left():
    # A ledger with many ticked rows and a few open. The ticked ones carried
    # status `ledger`, fell through to "ready", and the home header counted them as work left
    # with an ETA timed on all of them.
    graph = {"read-W1": set(), "read-W2": {"read-W1"}, "read-W3": {"read-W2"},
             "read-W4": {"read-W3"}}
    done = {"read-W1": "ledger", "read-W2": "ledger", "read-W3": "ok"}

    (read,) = campaign.summarise(graph, done)

    assert read.skipped == 2 and read.built == 1
    assert read.ready == ["read-W4"] and read.live_total == 2
    assert read.live_total - read.built == 1  # what the ETA times


def test_a_campaign_that_is_all_history_has_nothing_live():
    (old,) = campaign.summarise({"old-W1": set()}, {"old-W1": "ledger"})
    assert old.live_total == 0 and not old.active
