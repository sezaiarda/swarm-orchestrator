"""A phase whose worker waits on the owner is counted as that, never as ready.

``ready`` means the launcher would start the row next. A row whose worker asked
the owner something has a live session behind it, on its park timer in its slot
or parked in a window of its own, and the launcher leaves it alone: ``swarm
context`` never lists it under ``ready``. The ledger count behind ``swarm
status``, the dashboard's headline and its status bar had no word for it, so a
parked row that still asked fell through to ready.

These tests read one real state file through ``swarm status`` and a real
``Dash``. The row that asks is the case. The controls: a parked row the owner
has answered stays running, a row nobody works on stays ready, a state file
from before the marks reads as asking, and the counts add up to the ledger.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from textual.app import App

from swarm_orchestrator import cli, master
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.state import State
from swarm_orchestrator.tui import home, shell

from test_parked_working_dashboard import dash_of
from test_parked_working_readers import (  # noqa: F401 - ``cfg`` is the fixture
    ASKED,
    ASKING,
    LEDGER,
    PARK_AFTER,
    STILL_ASKING,
    UNMARKED,
    WORKING,
    cfg,
    run,
)
from test_tui_home import plain

#: The three shapes of a parked session, each a parameter of its own.
EVERY_SHAPE = pytest.mark.parametrize("mark", [WORKING, ASKING, UNMARKED])
#: The counts ``swarm status`` gives, which are disjoint.
PARTS = ("done", "running", "asking", "ready", "blocked", "dated", "failed")


def ledger(cfg, text: str) -> None:
    (cfg.project_dir / cfg.ledger).write_text(text, encoding="utf-8")


def standing(cfg, capsys) -> dict:
    """``phases`` of ``swarm status --json``."""
    capsys.readouterr()
    assert cli.cmd_status(cfg, as_json=True) == 0
    return json.loads(capsys.readouterr().out)["phases"]


def phases_line(cfg, capsys) -> str:
    """The ``phases:`` line of ``swarm status``."""
    capsys.readouterr()
    assert cli.cmd_status(cfg) == 0
    (line,) = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("phases:")]
    return line


def on_its_timer(cfg, slot: bool = True) -> None:
    """P0 builds in a slot; P1 asked and is still on its park timer. ``slot``
    says whether P1's slot is still marked busy for it."""
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(State.fresh(2).__dict__)
        st.claim_slot("P0")
        if slot:
            st.claim_slot("P1")
        st.waiting["P1"] = ASKED + PARK_AFTER
    st = state_mod.read(cfg)
    assert st.asking("P1") and not st.parked
    assert ("P1" in {s.phase for s in st.busy_slots()}) == slot


def counts(dash) -> str:
    """The last line of home's headline: what is moving."""
    return plain(home.headline(dash, 76)).splitlines()[-1]


# -- swarm status ----------------------------------------------------------------------
@STILL_ASKING
def test_swarm_status_json_does_not_count_a_parked_phase_that_asks_as_ready(cfg, capsys, mark):
    run(cfg, mark)
    n = standing(cfg, capsys)
    assert n["ready"] == 0
    assert (n["running"], n["asking"], n["blocked"]) == (1, 1, 1)
    # The launcher's own list agrees: nothing is ready.
    assert master.build_context(cfg, state_mod.read(cfg))["ready"] == []


@STILL_ASKING
def test_swarm_status_says_in_words_that_a_parked_phase_waits_on_the_owner(cfg, capsys, mark):
    run(cfg, mark)
    assert phases_line(cfg, capsys) == (
        "phases: 0 of 3 done · 1 running · 1 waiting on you · 1 blocked")


def test_swarm_status_keeps_an_answered_parked_phase_running(cfg, capsys):
    run(cfg, WORKING)
    n = standing(cfg, capsys)
    assert (n["running"], n.get("asking", 0), n["ready"], n["blocked"]) == (2, 0, 0, 1)
    assert phases_line(cfg, capsys) == "phases: 0 of 3 done · 2 running · 1 blocked"


@EVERY_SHAPE
def test_swarm_status_keeps_a_row_nobody_works_on_ready(cfg, capsys, mark):
    ledger(cfg, LEDGER + "- [ ] `P3` · needs:—\n")
    run(cfg, mark)
    n = standing(cfg, capsys)
    assert n["ready"] == 1
    assert n.get("asking", 0) == (0 if mark == WORKING else 1)
    assert master.build_context(cfg, state_mod.read(cfg))["ready"] == ["P3"]
    assert "1 ready" in phases_line(cfg, capsys)


@pytest.mark.parametrize("slot", [True, False])
def test_swarm_status_counts_a_worker_on_its_park_timer_as_waiting_on_the_owner(
        cfg, capsys, slot):
    """Whether or not its slot is counted, it asks: it is not ready, and it does
    not build while it waits for the answer."""
    on_its_timer(cfg, slot)
    n = standing(cfg, capsys)
    assert (n["running"], n["asking"], n["ready"], n["blocked"]) == (1, 1, 0, 1)
    assert "1 running · 1 waiting on you · 1 blocked" in phases_line(cfg, capsys)


@EVERY_SHAPE
def test_the_counts_of_swarm_status_add_up_to_the_ledger(cfg, capsys, mark):
    ledger(cfg, "- [x] `D0` · needs:—\n" + LEDGER
           + "- [ ] `P3` · needs:—\n"
           + "- [ ] `F0` · needs:—\n"
           + "- [ ] `L0` · needs:— · after:`2999-01-01`\n")
    run(cfg, mark)
    with state_mod.transaction(cfg) as st:
        st.done["F0"] = "fail"
    n = standing(cfg, capsys)
    got = [n.get(k, 0) for k in PARTS]
    assert n["total"] == 7 and sum(got) == n["total"]
    asking = 0 if mark == WORKING else 1
    assert got == [1, 2 - asking, asking, 1, 1, 1, 1]


# -- the dashboard's headline ------------------------------------------------------------
@STILL_ASKING
def test_the_headline_does_not_count_a_parked_phase_that_asks_as_ready(cfg, monkeypatch, mark):
    run(cfg, mark)
    dash, _ = dash_of(cfg, monkeypatch)
    line = counts(dash)
    assert "ready" not in line
    assert "1 running" in line and "1 waiting on you" in line


def test_the_headline_keeps_an_answered_parked_phase_running(cfg, monkeypatch):
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    line = counts(dash)
    assert "2 running" in line and "ready" not in line and "waiting on you" not in line


@EVERY_SHAPE
def test_the_headline_keeps_a_row_nobody_works_on_ready(cfg, monkeypatch, mark):
    ledger(cfg, LEDGER + "- [ ] `P3` · needs:—\n")
    run(cfg, mark)
    dash, _ = dash_of(cfg, monkeypatch)
    assert "1 ready" in counts(dash)


def test_the_headline_counts_a_worker_on_its_park_timer_as_waiting_on_the_owner(
        cfg, monkeypatch):
    on_its_timer(cfg)
    dash, _ = dash_of(cfg, monkeypatch)
    line = counts(dash)
    assert "1 running" in line and "1 waiting on you" in line and "ready" not in line


def test_a_headline_with_only_a_question_open_does_not_say_nothing_can_start(cfg, monkeypatch):
    ledger(cfg, "- [ ] `P1` · needs:—\n")
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(State.fresh(2).__dict__)
        st.parked.append("P1")
    dash, _ = dash_of(cfg, monkeypatch)
    line = counts(dash)
    assert line.strip().startswith("1 waiting on you") and "nothing can start" not in line


# -- the status bar ----------------------------------------------------------------------
#: A one-row book whose worker is parked, beside a bigger book that is only ready.
TWO_BOOKS = (
    "- [ ] `ask-W1` · needs:—\n"
    "- [ ] `big-W1` · needs:—\n"
    "- [ ] `big-W2` · needs:—\n"
    "- [ ] `big-W3` · needs:—\n"
)


def bar_of(cfg, monkeypatch) -> str:
    """The status bar over a real dash, as text."""
    dash, _ = dash_of(cfg, monkeypatch)
    seen = {}

    class Host(App):
        def compose(self):
            yield shell.StatusBar(id="statusbar")

    app = Host()
    app.cfg = cfg

    async def steps():
        async with app.run_test(size=(160, 10)) as pilot:
            bar = app.query_one(shell.StatusBar)
            bar.update_from(dash)
            await pilot.pause()
            seen["bar"] = str(bar.render())

    asyncio.run(asyncio.wait_for(steps(), timeout=30))
    return seen["bar"]


def parked_alone(cfg, mark: str) -> None:
    """No slot is busy; ``ask-W1`` is parked in the shape ``mark`` names."""
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(State.fresh(2).__dict__)
        st.claim_slot("ask-W1")
        st.waiting["ask-W1"] = ASKED + PARK_AFTER
        st.park("ask-W1", PARK_AFTER)
        if mark == WORKING:
            st.answer("ask-W1", ASKED + 60)
        elif mark == UNMARKED:
            st.asked.clear()


@EVERY_SHAPE
def test_the_status_bar_names_the_book_a_parked_worker_is_on(cfg, monkeypatch, mark):
    """A book with a worker on it is the one being built, whether that worker
    works on the answer or still waits for it: never the bigger book that is
    only ready to start."""
    ledger(cfg, TWO_BOOKS)
    parked_alone(cfg, mark)
    bar = bar_of(cfg, monkeypatch)
    assert "ask 0/1" in bar and "big" not in bar


def test_the_status_bar_names_the_ready_book_when_no_worker_is_on_another(cfg, monkeypatch):
    ledger(cfg, TWO_BOOKS)
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(State.fresh(2).__dict__)
    assert "big 0/3" in bar_of(cfg, monkeypatch)


def test_the_status_bar_does_not_name_a_book_that_only_waits_for_a_date(cfg, monkeypatch):
    """The headline leaves a dated row out of ready; the bar reads the same count."""
    ledger(cfg, "".join(f"- [ ] `late-W{i}` · needs:— · after:`2999-01-01`\n" for i in (1, 2, 3))
           + "- [ ] `now-W1` · needs:—\n")
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(State.fresh(2).__dict__)
    bar = bar_of(cfg, monkeypatch)
    assert "now 0/1" in bar and "late" not in bar
