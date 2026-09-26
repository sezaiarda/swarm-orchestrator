"""Campaign standings: what counts as work left, and what is history."""

from __future__ import annotations

from swarm_orchestrator.tui import campaign


def test_ledger_ticked_rows_are_done_not_work_left():
    # A ledger with many ticked rows and a few open. The ticked ones carried
    # status `ledger`, fell through to "ready", and the home header counted them as work left
    # with an ETA timed on all of them.
    graph = {"read-W1": set(), "read-W2": {"read-W1"}, "read-W3": {"read-W2"},
             "read-W4": {"read-W3"}}
    done = {"read-W1": "ledger", "read-W2": "ledger", "read-W3": "ok"}

    (read,) = campaign.summarise(graph, done)

    assert read.built == 3 and read.live_total == 4
    assert read.ready == ["read-W4"]
    assert read.live_total - read.built == 1  # what the ETA times


def test_a_campaign_that_is_all_history_is_complete_and_idle():
    (old,) = campaign.summarise({"old-W1": set()}, {"old-W1": "ledger"})
    assert old.complete and not old.active


def test_counts_match_the_web_board_everything_done_over_everything_scheduled():
    graph = {f"teal-W{i}": set() for i in range(1, 19)} | {"teal-W19": set()}
    done = {f"teal-W{i}": "ledger" for i in range(1, 13)}
    done |= {"teal-W13": "ok", "teal-W14": "skip", "teal-W15": "operator"}
    done |= {"teal-W16": "ledger"}  # an operator-run row the ledger ticks: done
    (teal,) = campaign.summarise(graph, done, busy={"teal-W17"},
                                 excluded={"teal-W16", "teal-W19"})
    assert (teal.built, teal.live_total) == (16, 18)
    assert (teal.excluded, teal.running, teal.ready) == (1, ["teal-W17"], ["teal-W18"])
