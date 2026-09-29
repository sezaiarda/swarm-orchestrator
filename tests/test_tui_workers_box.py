"""Home shows every worker slot, always; the phase books scroll in a fixed box.

Home must show every worker. Working now sits at the top of the left column
with every slot, busy or free; phase books is a shorter fixed-height box that
scrolls (keys, wheel), keeps its selection and says how many rows lie below.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from textual import events

from swarm_orchestrator.config import load as load_config
from swarm_orchestrator.eta.forecast import Book, Forecast, Range

SIZES = [(209, 50), (120, 40)]
SLOTS = 6  # more than the usual four: none may be cut off
BOOKS = 20


@pytest.fixture
def busy(tmp_path, monkeypatch):
    """Six slots (five busy, one free) and twenty phase books."""
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text(
        "".join(f"- [ ] b{i:02d}-W1 build {i}\n" for i in range(BOOKS)) or "", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "wbox")
    cfg = load_config(project_dir=str(project))
    cfg.ensure_dirs()
    now = time.time()
    slots = [{"id": i, "busy": i < SLOTS - 1, "phase": f"b{i:02d}-W1" if i < SLOTS - 1 else None,
              "pane_id": f"%{i}", "branch": f"swarm/b{i:02d}-W1"} for i in range(SLOTS)]
    cfg.state_path.write_text(json.dumps({
        "slots": slots, "done": {}, "parked": [], "supervisor_pid": 1,
        "last_event_at": now - 30}), encoding="utf-8")
    return cfg


def _forecast():
    now = time.time()
    rng = Range(now + 3600, now + 7200, now + 10800)
    books = tuple(Book(f"b{i:02d}", 0, 1, 0, 1, finish=rng, rows=()) for i in range(BOOKS))
    return Forecast(made_at=now, runs=500, workers=4, overall=rng, books=books, stuck=(),
                    caps=(), working=0.4)


def _boot(cfg, size, steps, monkeypatch, capfd):
    from swarm_orchestrator.tui.app import SwarmApp
    from swarm_orchestrator.tui.dash import Dash

    monkeypatch.setattr(Dash, "probe", lambda self, now=None: None)
    app = SwarmApp(cfg)
    got: dict = {}

    async def drive():
        async with app.run_test(size=size) as pilot:
            app.dash.poll()
            app.refresh_all()
            await pilot.pause()
            home = app.query_one("#tab-home")
            app.dash.eta.result = _forecast()
            home.update(app.dash)
            await pilot.pause()
            await steps(app, pilot, home, got)

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(drive(), timeout=30))
    return got


def _books(home):
    return [r.row_key for r in home.query("#book-rows Row") if r.display]


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_every_slot_is_fully_on_screen(busy, size, monkeypatch, capfd):
    async def steps(app, pilot, home, got):
        panel = home.query_one("#p-work")
        rows = [r for r in home.query("#work-rows Row") if r.display]
        got["ids"] = [r.row_key for r in rows]
        got["inside"] = all(r.region.height > 0 and r.region.bottom <= app.size.height
                            for r in rows)
        got["panel_bottom"] = panel.region.bottom
        got["books_top"] = home.query_one("#p-books").region.y
        got["text"] = [r._swarm_text for r in rows]

    got = _boot(busy, size, steps, monkeypatch, capfd)
    assert got["ids"] == list(range(SLOTS))  # busy and free, none folded away
    assert got["inside"]
    assert got["panel_bottom"] <= got["books_top"]  # working now leads the column
    assert sum("b0" in t for t in got["text"]) == SLOTS - 1
    assert "free" in got["text"][-1]


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_the_book_box_is_fixed_and_says_how_many_lie_below(busy, size, monkeypatch, capfd):
    async def steps(app, pilot, home, got):
        panel = home.query_one("#p-books")
        got["shown"] = len(_books(home))
        got["height"] = panel.region.height
        got["subtitle"] = str(panel.border_subtitle)
        await pilot.press("j")
        got["after_j"] = panel.region.height

    got = _boot(busy, size, steps, monkeypatch, capfd)
    assert got["shown"] < BOOKS
    assert f"↓ {BOOKS - got['shown']} more" in got["subtitle"]
    assert got["height"] == got["after_j"]
    assert got["height"] <= 14


def test_the_book_box_scrolls_by_keys_and_by_wheel(busy, monkeypatch, capfd):
    async def steps(app, pilot, home, got):
        panel = home.query_one("#p-books")
        got["first"] = _books(home)
        # Keys: walk the cursor past the last visible book.
        home.focus()
        for _ in range(SLOTS - 1 + len(got["first"]) + 2):
            await pilot.press("j")
        await pilot.pause()
        got["keyed"] = _books(home)
        got["keyed_on"] = [r.row_key for r in home.query("#book-rows Row.-on")]
        got["keyed_sub"] = str(panel.border_subtitle)
        # Wheel: a real mouse-scroll event over the box moves the window back up.
        for _ in range(20):
            panel.post_message(events.MouseScrollUp(
                panel, 2, 2, 0, -1, 0, False, False, False))
            await pilot.pause()
        got["wheeled_up"] = _books(home)
        for _ in range(40):
            panel.post_message(events.MouseScrollDown(
                panel, 2, 2, 0, 1, 0, False, False, False))
            await pilot.pause()
        got["wheeled_down"] = _books(home)
        got["end_sub"] = str(panel.border_subtitle)

    got = _boot(busy, (209, 50), steps, monkeypatch, capfd)
    n = len(got["first"])
    assert got["keyed"] != got["first"] and len(got["keyed"]) == n
    assert got["keyed_on"] and got["keyed_on"][0] in got["keyed"]  # the cursor stays in view
    assert "↑" in got["keyed_sub"]
    assert got["wheeled_up"] == got["first"]
    assert got["wheeled_down"][-1] == f"b{BOOKS - 1:02d}" or len(got["wheeled_down"]) == n
    assert "↓" not in got["end_sub"]


def test_selection_and_enter_still_open_a_book_and_a_worker(busy, monkeypatch, capfd):
    async def steps(app, pilot, home, got):
        home.focus()
        opened = []
        home.post_message = (lambda m, _o=home.post_message: (opened.append(m), _o(m))[1])
        # Workers come first, in reading order; j past the busy ones reaches a book.
        got["start"] = home._target()[:2]
        for _ in range(SLOTS - 1):
            await pilot.press("j")
        got["on"] = home._target()[:2]
        got["marked"] = [str(r._swarm_text)[:2] for r in home.query("#book-rows Row.-on")]
        await pilot.press("enter")
        await pilot.pause()
        got["opened"] = [type(m).__name__ for m in opened]
        got["open_text"] = [m.title for m in opened if hasattr(m, "title")]

    got = _boot(busy, (120, 40), steps, monkeypatch, capfd)
    assert got["start"] == ("work", 0)
    assert got["on"][0] == "book"
    assert got["marked"] and "▸" in got["marked"][0]
    assert got["open_text"][-1].startswith("phase book ")
