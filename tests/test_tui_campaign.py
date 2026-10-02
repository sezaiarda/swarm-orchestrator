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


def test_a_row_waiting_for_its_after_date_is_dated_not_ready():
    """perf-F36 (`after:2026-10-01`) counted as ready on the dashboard and in
    `swarm status`, while the launcher, rightly, left it alone."""
    graph = {"perf-F35": set(), "perf-F36": {"perf-F35"}, "perf-F37": {"perf-F36"}}
    (perf,) = campaign.summarise(graph, {"perf-F35": "ok"}, deferred={"perf-F36": "2026-10-01"})
    assert perf.ready == [] and perf.dated == 1 and perf.blocked == 1
    assert not perf.active
    assert campaign.overall([perf]).dated == 1


def test_a_row_whose_worker_waits_on_the_owner_is_asking_not_ready_or_running():
    """Its session is alive, so the launcher leaves the row alone: wherever the
    worker sits, in its slot (so in ``busy`` too) or parked, the row is asking."""
    graph = {"a-W1": set(), "a-W2": set(), "a-W3": set(), "a-W4": {"a-W2"}, "a-W5": set()}
    (a,) = campaign.summarise(graph, {}, busy={"a-W1", "a-W3"}, asking={"a-W2", "a-W3"})
    assert a.asking == ["a-W2", "a-W3"]
    assert (a.running, a.ready, a.blocked) == (["a-W1"], ["a-W5"], 1)
    total = campaign.overall([a])
    assert total.asking == ["a-W2", "a-W3"]
    assert (total.built + len(total.running) + len(total.asking) + len(total.ready)
            + total.blocked + total.dated + total.failed) == total.live_total == 5


def test_a_campaign_with_only_a_question_open_is_still_the_one_being_worked():
    graph = {"ask-W1": set(), "big-W1": set(), "big-W2": set(), "idle-W1": {"ask-W1"}}
    ask, big, idle = campaign.summarise(graph, {}, asking={"ask-W1"})
    # A worker holds a row of it: it leads the bigger campaign that is only ready.
    assert (ask.name, big.name, idle.name) == ("ask", "big", "idle")
    assert ask.active and ask.manned and big.active and not big.manned and not idle.active
    assert campaign.active([ask, big, idle]) is ask


def test_a_phase_nobody_asks_about_counts_as_it_did():
    graph = {"a-W1": set(), "a-W2": set()}
    assert campaign.summarise(graph, {}, busy={"a-W1"}) == campaign.summarise(
        graph, {}, busy={"a-W1"}, asking={"not-in-the-ledger"})
