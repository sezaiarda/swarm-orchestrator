"""The real app, booted headless at three terminal sizes against a seeded run.

The owner runs the cockpit in split panes as often as full screen, and the
previous layout assumed 124+ columns: tables scrolled sideways, the drawer
squeezed the tab to 36 columns, and home pushed the one list worth reading off
the bottom. These drive the whole :class:`SwarmApp` at 80x24, 100x30 and 140x40
and check what a person would check: nothing raised, the key widgets are on
screen, the tables dropped columns instead of overflowing, and the drawer floats
rather than squeezes where the screen is narrow.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

import pytest

from swarm_orchestrator.config import load as load_config

SIZES = [(80, 24), (100, 30), (140, 40)]


def _log_line(ts: float, message: str) -> str:
    when = datetime.fromtimestamp(ts)
    return f"{when.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {ts % 100000:.3f} {message}\n"


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A run with a busy worker, a parked question, finishes, notes and recaps."""
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    ledger = "\n".join(["P0", "P1 needs:P0", "P2 needs:P0", "P3 needs:P1,P2", "P4 needs:P3",
                        "P5 needs:P0"]) + "\n"
    (project / "docs" / "PHASE-LEDGER.md").write_text(ledger, encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "test")
    cfg = load_config(project_dir=str(project))
    cfg.ensure_dirs()
    now = time.time()
    cfg.state_path.write_text(json.dumps({
        "slots": [
            {"id": 0, "busy": True, "phase": "P1", "pane_id": "%1", "branch": "swarm/P1"},
            {"id": 1, "busy": False},
        ],
        "done": {"P0": "ok", "P5": "ok"},
        "parked": ["P2"],
        "supervisor_pid": 1,
        "last_event_at": now - 30,
    }), encoding="utf-8")
    cfg.supervisor_log.write_text(
        _log_line(now - 7200, "LAUNCH P0 slot=0")
        + _log_line(now - 5400, "EVENT done P0 ok")
        + _log_line(now - 5000, "LAUNCH P5 slot=1")
        + _log_line(now - 3000, "EVENT done P5 ok")
        + _log_line(now - 2000, "LAUNCH P2 slot=1")
        + _log_line(now - 600, "LAUNCH P1 slot=0"),
        encoding="utf-8")
    (cfg.done_dir / "P0.ok").write_text("P0 ok built the base [with brackets]", encoding="utf-8")
    (cfg.done_dir / "P5.ok").write_text("P5 ok wired the side path", encoding="utf-8")
    recaps = cfg.state_dir / "recaps"
    recaps.mkdir()
    (recaps / "P0.json").write_text(json.dumps(
        {"phase": "P0", "status": "ok", "summary": "Built the base. " * 12}), encoding="utf-8")
    notes = cfg.state_dir / "notes"
    notes.mkdir()
    (notes / "P0.jsonl").write_text(
        json.dumps({"phase": "P0", "kind": "decision", "text": "kept the v1 schema",
                    "ts": now - 5600}) + "\n"
        + json.dumps({"phase": "P0", "kind": "owner_decision", "text": "ship without the graph",
                      "ts": now - 5500}) + "\n", encoding="utf-8")
    (cfg.state_dir / "notifications.jsonl").write_text(json.dumps(
        {"ts": now - 1900, "kind": "question", "phase": "P2", "delivered": True,
         "text": "which schema should P2 read?"}) + "\n", encoding="utf-8")
    return cfg


def _boot(cfg, size, steps, monkeypatch, capfd):
    """Boot the real app at ``size`` and run ``steps(app, pilot)``; raise what it raised."""
    from swarm_orchestrator.tui.app import SwarmApp
    from swarm_orchestrator.tui.dash import Dash

    monkeypatch.setattr(Dash, "probe", lambda self, now=None: None)  # no tmux/claude/git
    app = SwarmApp(cfg)
    got: dict = {}

    async def drive():
        async with app.run_test(size=size) as pilot:
            app.dash.poll()
            app.refresh_all()
            await pilot.pause()
            await steps(app, pilot, got)

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(drive(), timeout=30))
    return got


def _on_screen(app, widget) -> bool:
    """Displayed, with at least one row inside the terminal."""
    region = widget.region
    return bool(widget.display) and region.height > 0 and region.y < app.size.height


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_home_puts_needs_you_work_and_the_feed_on_screen(seeded, size, monkeypatch, capfd):
    async def steps(app, pilot, got):
        home = app.query_one("#tab-home")
        home.update(app.dash)
        await pilot.pause()
        got["needs"] = _on_screen(app, home.query_one("#p-needs"))
        got["work"] = _on_screen(app, home.query_one("#p-work"))
        got["feed"] = _on_screen(app, home.query_one("#p-feed"))
        got["feed_rows"] = [r._swarm_text for r in home.query("#feed-rows Row") if r.display]
        got["chart"] = home.query_one("#p-chart").display
        got["narrow"] = home.has_class("-narrow")

    got = _boot(seeded, size, steps, monkeypatch, capfd)
    assert got["needs"] and got["work"] and got["feed"]
    text = "\n".join(got["feed_rows"])
    assert "ship without the graph" in text and "kept the v1 schema" in text
    assert "Built the base" in text
    assert got["narrow"] is (size[0] < 100)
    assert got["chart"] is (size[1] >= 36)  # short screens give the room to the feed


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_tables_drop_columns_instead_of_scrolling_sideways(seeded, size, monkeypatch, capfd):
    async def steps(app, pilot, got):
        for key, tab in (("2", "#tab-workers"), ("3", "#tab-history")):
            await pilot.press(key)
            await pilot.pause()
            node = app.query_one(tab)
            table = node.table
            got[tab] = {
                "cols": [str(c.label) for c in table.columns.values()],
                "width": sum(c.get_render_width(table) for c in table.columns.values()),
                "room": table.scrollable_content_region.width,
                "detail": node.query_one(".detail-body")._swarm_text,
                "detail_on_screen": _on_screen(app, node.detail_panel),
            }

    got = _boot(seeded, size, steps, monkeypatch, capfd)
    workers, history = got["#tab-workers"], got["#tab-history"]
    for tab in (workers, history):
        assert tab["width"] <= tab["room"], tab
        assert tab["detail_on_screen"]
    assert {"slot", "phase", "live", "context"} <= set(workers["cols"])
    assert {"phase", "status", "what it did"} <= set(history["cols"])
    if size[0] <= 80:
        assert "branch" not in workers["cols"] and "campaign" not in history["cols"]
    if size[0] >= 140:
        assert "campaign" in history["cols"]
    assert "P1" in workers["detail"]
    assert "recap" in history["detail"] or "P1" in history["detail"]


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
def test_the_drawer_floats_over_a_narrow_screen_and_docks_on_a_wide_one(
        seeded, size, monkeypatch, capfd):
    async def steps(app, pilot, got):
        tabs = app.query_one("#tabs")
        before = tabs.region.width
        await pilot.press("n")
        await pilot.pause()
        drawer = app.query_one("#drawer")
        got["open"] = drawer.is_open
        got["overlay"] = drawer.has_class("-overlay")
        got["squeezed"] = tabs.region.width < before
        await pilot.press("escape")
        await pilot.pause()
        got["closed"] = not drawer.is_open

    got = _boot(seeded, size, steps, monkeypatch, capfd)
    assert got["open"] and got["closed"]
    assert got["overlay"] is (size[0] < 110)
    assert got["squeezed"] is (size[0] >= 110)


def test_enter_on_a_feed_row_opens_that_phase_in_history(seeded, monkeypatch, capfd):
    async def steps(app, pilot, got):
        home = app.query_one("#tab-home")
        home.update(app.dash)
        await pilot.pause()
        home.focus()
        # Walk to the first feed row about P0, then open it.
        for _ in range(12):
            target = home._target()
            if target and target[0] == "feed" and target[2] == "P0":
                break
            await pilot.press("j")
            await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        got["tab"] = app.query_one("#tabs").active
        got["phase"] = app.query_one("#tab-history").selected_phase()

    got = _boot(seeded, (120, 40), steps, monkeypatch, capfd)
    assert got == {"tab": "history", "phase": "P0"}
