"""The dashboard shows a parked worker the owner has answered, and where it works.

An answered parked worker is at work again, in a tmux window of its own
(``wait:<phase>``) and in no slot. History and the progress count already read
it as running; these tests hold the rest of the dashboard to the same reading:
the forecast is remade when the owner answers and keeps the running cadence
while that worker is the only one at work, and home's working-now box, the
Workers tab and the History detail each name the phase's window, so a running
phase that no slot row shows can still be found.

Every test reads one real state file through a real ``Dash``, in the three
shapes of ``tests/test_parked_working_readers.py``. The answered session is the
case; the other two are the control, and read as they always did: no worker
row, no window in History, the same forecast key.
"""

from __future__ import annotations

import asyncio
import time
from typing import NamedTuple

from textual.app import App

from swarm_orchestrator import state as state_mod
from swarm_orchestrator.eta import engine
from swarm_orchestrator.tui import home, tables
from swarm_orchestrator.tui.dash import Dash
from swarm_orchestrator.tui.data import fmt_duration

from test_parked_working_readers import (  # noqa: F401 - ``cfg`` is the fixture
    ANSWERED,
    ASKING,
    LAUNCHED,
    STILL_ASKING,
    WORKING,
    cfg,
    run,
)
from test_tui_home import plain

WINDOW = "wait:P1"


def aged(render):
    """``render()``, and the ages a run launched at ``LAUNCHED`` can show in it.

    A row reads the clock when it is made, and ``LAUNCHED`` was set when the
    module it comes from was imported, minutes ago in a whole-suite run: the age
    is whatever the clock says around the render, not a fixed three hours.
    """
    before = fmt_duration(time.time() - LAUNCHED)
    made = render()
    return made, {before, fmt_duration(time.time() - LAUNCHED)}


class Asked(NamedTuple):
    """One question a dash put to its forecast gate (:meth:`eta.engine.Gate.due`)."""

    real: tuple
    soft: tuple
    every: float


def dash_of(cfg, monkeypatch) -> tuple[Dash, list[Asked]]:
    """A real dash over ``cfg``, polled once, and what it asks its forecast gate.

    The gate is the seam: it records each question and answers "not due", so no
    forecast is simulated behind a test that only reads what the dash holds.
    """
    dash = Dash(cfg)
    asked: list[Asked] = []
    monkeypatch.setattr(dash.eta.gate, "due", lambda real, soft, now, every:
                        asked.append(Asked(real, soft, every)) or False)
    dash.poll()
    return dash, asked


def free_the_slot(cfg) -> None:
    """P0 leaves its slot: the parked session is the only one left."""
    with state_mod.transaction(cfg) as st:
        st.free_slot_for("P0")


# -- the forecast ------------------------------------------------------------------
def test_the_dashboard_remakes_its_forecast_when_the_owner_answers(cfg, monkeypatch):
    run(cfg, ASKING)
    dash, asked = dash_of(cfg, monkeypatch)
    with state_mod.transaction(cfg) as st:
        st.answer("P1", ANSWERED)
    dash.poll()
    before, after = asked[-2], asked[-1]
    assert after.soft != before.soft  # the answer is a change the forecast must show
    assert after.real == before.real  # of the kind that waits its turn, like a question


def test_the_forecast_key_stands_while_a_parked_session_still_asks(cfg, monkeypatch):
    """Since when it asks is no part of the key: a file without the marks reads the same."""
    run(cfg, ASKING)
    dash, asked = dash_of(cfg, monkeypatch)
    with state_mod.transaction(cfg) as st:
        st.asked.clear()
    dash.poll()
    assert asked[-1].soft == asked[-2].soft and asked[-1].real == asked[-2].real


def test_a_parked_worker_at_work_alone_keeps_the_running_cadence(cfg, monkeypatch):
    run(cfg, WORKING)
    free_the_slot(cfg)
    dash, asked = dash_of(cfg, monkeypatch)
    assert not any(s.busy for s in dash.snapshot.slots)
    assert asked[-1].every == engine.RECOMPUTE_S


@STILL_ASKING
def test_a_parked_session_that_asks_alone_leaves_the_idle_cadence(cfg, monkeypatch, mark):
    run(cfg, mark)
    free_the_slot(cfg)
    _, asked = dash_of(cfg, monkeypatch)
    assert asked[-1].every == engine.IDLE_RECOMPUTE_S


def test_a_busy_slot_keeps_the_running_cadence_as_before(cfg, monkeypatch):
    run(cfg, ASKING)
    _, asked = dash_of(cfg, monkeypatch)
    assert asked[-1].every == engine.RECOMPUTE_S


# -- home: working now ---------------------------------------------------------------
def test_home_lists_a_working_parked_phase_with_its_window(cfg, monkeypatch):
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    rows, ages = aged(lambda: home.worker_rows(dash, 60))
    # After the slots, and with no slot of its own to select or jump to.
    assert [(phase, slot) for _, phase, slot in rows] == [("P0", 0), (None, 1), ("P1", None)]
    text = plain(rows[-1][0])
    assert "P1" in text and WINDOW in text and "your answer" in text
    assert any(age in text for age in ages)  # aged from its launch, like a slot's worker


def test_home_keeps_the_window_of_a_working_parked_phase_when_it_is_narrow(cfg, monkeypatch):
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    assert WINDOW in plain(home.worker_rows(dash, 30)[-1][0])


def test_home_names_a_working_parked_phase_that_is_below_the_fold(cfg, monkeypatch):
    """The box shows four rows; with four slots the parked worker is the fifth."""
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    slots = [(f"slot {i}", f"S{i}", i) for i in range(4)]
    rows = slots + home.worker_rows(dash, 60)[-1:]
    assert home.work_hint(rows, 0) == "↓ 1 more (P1 in own window)"
    assert home.work_hint(rows, 1) == "↑ 1 above"  # scrolled to it: the row says it
    assert home.work_hint(slots + slots[:1], 0) == "↓ 1 more"  # a fifth slot, as before
    assert home.work_hint(slots, 0) == ""


@STILL_ASKING
def test_home_lists_no_worker_for_a_parked_phase_that_asks(cfg, monkeypatch, mark):
    run(cfg, mark)
    dash, _ = dash_of(cfg, monkeypatch)
    rows = home.worker_rows(dash, 60)
    assert [(phase, slot) for _, phase, slot in rows] == [("P0", 0), (None, 1)]
    assert not any(WINDOW in plain(text) for text, _, _ in rows)


def test_home_does_not_open_or_select_a_working_parked_row(cfg, monkeypatch):
    """It holds no slot, so there is no pane to jump to: a click on it opens nothing."""
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    seen = {}

    class Host(App):
        opened: list = []

        def compose(self):
            yield home.Home(id="tab-home")

        def on_home_open_phase(self, event: home.Home.OpenPhase) -> None:
            self.opened.append((event.phase, event.slot))

    app = Host()

    async def steps():
        async with app.run_test(size=(100, 30)) as pilot:
            screen = app.query_one(home.Home)
            screen.update(dash)
            await pilot.pause()
            shown = [r for r in app.query("#work-rows Row") if r.display]
            seen["work"] = [plain(r._swarm_text) for r in shown]
            await pilot.click(shown[-1])
            await pilot.pause()
            seen["selected"] = screen.selected_phase()

    asyncio.run(asyncio.wait_for(steps(), timeout=30))
    assert len(seen["work"]) == 3 and WINDOW in seen["work"][-1]
    assert app.opened == [] and seen["selected"] == "P0"


# -- home: the headline and what runs next ------------------------------------------
def test_the_headline_counts_a_working_parked_phase_as_running(cfg, monkeypatch):
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    counts = plain(home.headline(dash, 76)).splitlines()[-1]
    assert "2 running" in counts and "ready" not in counts  # P2 waits on P1, at work


@STILL_ASKING
def test_the_headline_counts_only_the_slot_while_a_parked_phase_asks(cfg, monkeypatch, mark):
    run(cfg, mark)
    dash, _ = dash_of(cfg, monkeypatch)
    assert "1 running" in plain(home.headline(dash, 76)).splitlines()[-1]


def queued(dash) -> list[str]:
    """The phases home lists under what's next, in its order."""
    return [plain(line).split()[1] for line in home.next_lines(dash, 60)]


def test_whats_next_does_not_queue_a_phase_that_works_in_its_window(cfg, monkeypatch):
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    assert queued(dash) == ["P2"]  # P1 is running, not next in line


@STILL_ASKING
def test_whats_next_does_not_queue_a_parked_phase_that_asks(cfg, monkeypatch, mark):
    run(cfg, mark)
    dash, _ = dash_of(cfg, monkeypatch)
    assert queued(dash) == ["P2"]


# -- the Workers tab ------------------------------------------------------------------
def workers_tab(dash) -> dict:
    """The Workers tab alone, painted from ``dash``, with the cursor on its last row."""
    seen = {}

    class Host(App):
        def compose(self):
            yield tables.Workers()

    app = Host()

    async def steps():
        async with app.run_test(size=(140, 30)) as pilot:
            tab = app.query_one(tables.Workers)
            tab.update(dash)
            await pilot.pause()
            tab.table.move_cursor(row=len(tab.rows) - 1)
            await pilot.pause()
            seen.update(rows=list(tab.rows), keys=list(tab._keys),
                        head=plain(tab.query_one(".tab-head")._swarm_text),
                        detail=plain(tab.detail_text(dash)), phase=tab.selected_phase())

    asyncio.run(asyncio.wait_for(steps(), timeout=30))
    return seen


def col(row, name: str) -> str:
    """A worker row's cell by column name, as the screen shows it."""
    return plain(row[[n for n, _ in tables.WORKER_COLUMNS].index(name)])


def test_the_workers_tab_lists_a_working_parked_phase_with_its_window(cfg, monkeypatch):
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    tab = workers_tab(dash)
    assert len(tab["rows"]) == 3 and len(set(tab["keys"])) == 3  # two slots, then P1
    row, ages = aged(lambda: tables.worker_row(tab["rows"][-1]))
    assert len(row) == len(tables.WORKER_COLUMNS)
    assert col(row, "slot").strip() == "—" and col(row, "phase").strip() == "P1"
    assert col(row, "live").strip() == "in window" and col(row, "branch").strip() == WINDOW
    assert col(row, "elapsed").strip() in ages  # aged from its launch
    # The head still counts slots, and says what works outside them.
    assert "1/2 slots busy" in tab["head"] and "1 in own window" in tab["head"]
    assert tab["phase"] == "P1"
    assert f"works on your answer in tmux window {WINDOW}" in tab["detail"]
    assert "no slot" in tab["detail"]


@STILL_ASKING
def test_the_workers_tab_lists_only_slots_while_a_parked_phase_asks(cfg, monkeypatch, mark):
    run(cfg, mark)
    dash, _ = dash_of(cfg, monkeypatch)
    tab = workers_tab(dash)
    assert tab["keys"] == ["slot-0", "slot-1"]
    assert tab["head"] == "1/2 slots busy"
    cells = [plain(c) for r in tab["rows"] for c in tables.worker_row(r)]
    assert not any(WINDOW in c for c in cells) and WINDOW not in tab["detail"]


# -- History --------------------------------------------------------------------------
def history_detail(dash) -> str:
    """The History detail of P1's run, as the screen shows it."""
    p1 = next(r for r in dash.history if r.phase == "P1")
    return plain(tables.history_detail(p1, dash))


def test_history_says_where_a_running_parked_phase_works(cfg, monkeypatch):
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    detail = history_detail(dash)
    assert "running" in detail.splitlines()[0]
    assert f"tmux window {WINDOW}" in detail and "your answer" in detail


def test_history_names_no_window_for_a_run_in_a_slot(cfg, monkeypatch):
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    p0 = next(r for r in dash.history if r.phase == "P0")
    detail = plain(tables.history_detail(p0, dash))
    assert "running" in detail.splitlines()[0] and "wait:" not in detail


@STILL_ASKING
def test_history_reads_a_parked_phase_that_asks_as_parked(cfg, monkeypatch, mark):
    run(cfg, mark)
    dash, _ = dash_of(cfg, monkeypatch)
    detail = history_detail(dash)
    assert "parked" in detail.splitlines()[0] and "running" not in detail.splitlines()[0]
    assert WINDOW not in detail and "your answer" not in detail
