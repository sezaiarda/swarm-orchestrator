"""Home as a grid: usage with its chart, one alerts & notifications box, the shells,
the web board served from the dashboard, and the tab keys listed once.

The layout brief: usage in its own box with a chart and the limits,
alerts and notifications together in one box, the shells on home, the web board
inside the TUI instead of its own tmux window, and the tabs shown once, at the
top. Pure painters are tested on namespaces; the layout and the tab strip on
the real app, booted headless.
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest
from rich.console import Console
from rich.text import Text

from swarm_orchestrator.config import load
from swarm_orchestrator.eta.forecast import Forecast, Range
from swarm_orchestrator.tui import alerts, charts, data, home, usagebox
from swarm_orchestrator.usage import Sample
from swarm_orchestrator.web import lifecycle
from swarm_orchestrator.web import server as web_server

NOW = time.time()
H = 3600.0
RULES = [{"window": "week", "at": 90, "action": "pause"},
         {"window": "week", "at": 95, "action": "down"},
         {"window": "five_hour", "at": 90, "action": "pause"}]


def plain(markup: str) -> str:
    return Text.from_markup(markup).plain


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def usage_dash(samples=(), busy=1, burn=None, overall=None, hold=None, run_usage=None,
               enabled=True):
    slots = [data.SlotView(id=i, busy=i < busy, phase=f"P{i}" if i < busy else None,
                           pane_id=f"%{i}", branch=None, worktree=None) for i in range(4)]
    fc = None
    if overall is not None:
        fc = Forecast(made_at=NOW, runs=500, workers=4, overall=Range(overall, overall, overall))
    return SimpleNamespace(
        cfg=SimpleNamespace(usage_enabled=enabled, usage_rules=RULES, max_workers=4),
        samples=list(samples), burn=burn or {}, usage=run_usage,
        usage_state=(hold or {}, {}), forecast=fc,
        snapshot=data.Snapshot(ok=True, slots=slots, supervisor_alive=True))


def week_samples(pcts, start=NOW - 5 * H, step=H, five=20.0):
    return [Sample(ts=start + i * step, five_pct=five, five_resets_at=NOW + 2 * H,
                   week_pct=p, week_resets_at=NOW + 20 * H) for i, p in enumerate(pcts)]


# -- the grid ------------------------------------------------------------------
def test_the_owners_size_is_two_columns_with_everything_shown():
    cut = home.grid_layout(205, 50)
    assert not cut["single"] and not cut["narrow"] and cut["chart"]
    assert cut["books"] is None and cut["usage_chart"] == 5
    assert cut["main"] + 1 + cut["side"] == 205
    assert 76 <= cut["side"] <= home.SIDE_MAX


def test_120x40_keeps_the_grid_and_gives_up_the_chart_and_some_books():
    cut = home.grid_layout(116, 40)
    assert not cut["single"] and cut["narrow"] and not cut["chart"]
    assert cut["books"] == home.MID_BOOKS and cut["usage_chart"] == 3
    assert cut["side"] == home.SIDE_MIN


def test_a_split_pane_stacks_the_side_column():
    cut = home.grid_layout(96, 30)
    assert cut["single"] and cut["short"] and cut["usage_chart"] == 0


# -- the chart -----------------------------------------------------------------
def test_level_chart_is_on_a_fixed_scale_with_the_caps_drawn_across_it():
    rows = charts.level_chart([30.0] * 20, 24, 5, marks=[(90, "┄"), (95, "━")])
    assert all(len(r) == 24 for r in rows)
    assert rows[0].startswith(" 90┤") and "┄" in rows[0]  # the lower cap wins the row
    assert rows[-1].startswith("  0┤")
    # 30% fills one of five rows and a bit: the top three rows hold no block.
    assert all(not any(b in r for b in "▁▂▃▄▅▆▇█") for r in rows[:3])
    assert "█" in rows[-1]


def test_level_chart_leaves_no_reading_blank():
    rows = charts.level_chart([None] * 6 + [50.0] * 6, 16, 2, marks=[])
    assert rows[-1][4:10] == "      " and rows[-1][10:] == "█" * 6


def test_series_drops_to_zero_at_each_reset_and_ignores_a_lagging_reading():
    samples = [Sample(ts=NOW - 4 * H, five_pct=40, five_resets_at=NOW - 3 * H),
               Sample(ts=NOW - 3.5 * H, five_pct=30, five_resets_at=NOW - 3 * H),  # lagging
               Sample(ts=NOW - 2 * H, five_pct=10, five_resets_at=NOW + 2 * H)]
    pts = usagebox.series(samples, "five", NOW - 5 * H, NOW)
    assert pts == [(NOW - 4 * H, 40), (NOW - 3.5 * H, 40), (NOW - 3 * H, 0.0),
                   (NOW - 2 * H, 10)]


# -- the usage box -------------------------------------------------------------
def test_usage_box_shows_both_windows_the_rules_the_burn_and_a_chart():
    dash = usage_dash(week_samples([80, 82, 84, 86]),
                      run_usage={"five_pct_per_h": 4.9, "week_pct_per_h": 1.3, "usd_per_h": 14.8})
    lines = [plain(x) for x in usagebox.box_lines(dash, 80, NOW, chart_rows=5)]
    text = "\n".join(lines)
    assert lines[0].startswith("5-hour") and "20%" in lines[0] and "resets" in lines[0]
    assert lines[1].startswith("week") and "86%" in lines[1]
    assert "┄" in lines[1] and "━" in lines[1]  # pause and stop marked on the meter
    assert "pause 5-hour 90% · pause week 90% · stop week 95%" in text
    assert "this run 5-hour 4.9%/h · week 1.3%/h · $14.80/h" in text
    assert "5-hour %" in text and "week %" in text and "90┤" in text
    assert all(len(line) <= 80 for line in lines), [len(x) for x in lines]


def test_usage_box_fits_a_narrow_column():
    dash = usage_dash(week_samples([80, 86]),
                      run_usage={"five_pct_per_h": 4.9, "week_pct_per_h": 1.3, "usd_per_h": 14.8},
                      burn={"week": 1.0}, overall=NOW + 40 * H)
    lines = [plain(x) for x in usagebox.box_lines(dash, 42, NOW, chart_rows=3)]
    assert all(len(line) <= 42 for line in lines), [(len(x), x) for x in lines]
    assert any("pause 5h 90%, wk 90% · stop wk 95%" in line for line in lines)


def test_next_cap_before_the_work_is_done():
    # 86% now, 1 point per busy worker-hour at 1 worker: 90% in 4h, the reset in 20h.
    dash = usage_dash(week_samples([86]), burn={"week": 1.0}, overall=NOW + 10 * H)
    text, state = usagebox.next_cap(dash, NOW)
    assert text.startswith("week 90% ~") and "(in 4h" in text and "before the work is done" in text
    assert state == "warn"


def test_next_cap_after_the_work_is_done_is_fine():
    dash = usage_dash(week_samples([86]), burn={"week": 1.0}, overall=NOW + 2 * H)
    text, state = usagebox.next_cap(dash, NOW)
    assert "after the work is done" in text and state == "ok"


def test_a_window_that_resets_first_is_no_cap():
    dash = usage_dash(week_samples([86]), burn={"week": 0.01}, overall=NOW + 2 * H)
    text, state = usagebox.next_cap(dash, NOW)
    assert text.startswith("none — week reset before") and state == "ok"


def test_nothing_running_burns_nothing():
    dash = usage_dash(week_samples([86]), busy=0, burn={"week": 1.0})
    assert usagebox.next_cap(dash, NOW)[0].startswith("none — nothing is running")


def test_a_held_window_says_until_when():
    dash = usage_dash(week_samples([91]), hold={"week": {"resets_at": NOW + 20 * H}})
    text, state = usagebox.next_cap(dash, NOW)
    assert text.startswith("held by the week cap until") and state == "bad"


def test_caps_off_says_so():
    dash = usage_dash(week_samples([50]), enabled=False)
    text = "\n".join(plain(x) for x in usagebox.box_lines(dash, 80, NOW, chart_rows=0))
    assert "usage caps are off" in text


# -- alerts & notifications ------------------------------------------------------
def note(kind="waiting", delivered=True, suppressed="", ts=NOW - 60, phase="P2",
         text="which schema should P2 read?"):
    return data.Notification(ts=ts, kind=kind, phase=phase, source="t", text=text,
                             delivered=delivered, error="", suppressed=suppressed)


def test_a_ping_says_sent_held_or_lost_by_its_mark():
    assert plain(alerts.note_line(note(), 80, NOW)).split()[1] == "✓"
    assert plain(alerts.note_line(note(delivered=False, suppressed="routine"), 80, NOW)).split()[1] == "·"
    lost = alerts.note_line(note(delivered=False), 80, NOW)
    assert plain(lost).split()[1] == "✗" and "#f85149" in lost  # red until acknowledged
    seen = alerts.note_line(note(delivered=False, ts=NOW - 100), 80, NOW, acked=NOW - 50)
    assert "#f85149" not in seen.split("✗")[0][-12:]


def test_a_narrow_box_drops_the_kind_column_and_never_wraps():
    wide = plain(alerts.note_line(note(kind="overseer-digest"), 80, NOW))
    narrow = plain(alerts.note_line(note(kind="overseer-digest"), 40, NOW))
    assert "overseer-digest" in wide and "overseer" not in narrow
    assert len(wide) <= 80 and len(narrow) <= 40


def test_recent_notes_are_newest_first_and_keyed_by_file_order():
    notes = [note(text=f"n{i}", ts=NOW - 100 + i) for i in range(5)]
    got = alerts.recent_notes(notes, limit=3)
    assert [k for k, _ in got] == ["n4", "n3", "n2"]


def test_problems_are_what_the_footer_draws_from():
    snap = data.Snapshot(ok=True, supervisor_alive=False, integ_blocked="P3",
                         integ_blocked_kind="conflict")
    dash = SimpleNamespace(snapshot=snap, notifications=[note(delivered=False)],
                           pings_acked_at=0.0)
    texts = [t for t, _ in alerts.problems(dash, NOW)]
    assert texts[0] == "swarm not running — `swarm up`"
    assert any(t.startswith("merging stopped: P3") for t in texts)
    assert "1 ping(s) never reached your phone (x clears)" in texts
    foot = plain(home.footer_line(dash, 200, now=NOW))
    assert all(t.split(" ")[0] in foot for t in texts)


def test_shells_are_one_line_each():
    rows = [{"name": "tunnel", "why": "the owner's phone reaches staging through it",
             "alive": True, "stale": False, "age": "3h"},
            {"name": "old", "why": "", "alive": False, "stale": False, "age": "2d"}]
    lines = [plain(x) for x in alerts.shell_lines(rows, 50)]
    assert len(lines) == 2 and all(len(x) <= 50 for x in lines)
    assert lines[0].startswith("● tunnel") and lines[0].endswith("3h")
    assert "no why recorded" in lines[1] and lines[1].endswith("dead")
    assert "nothing kept running" in plain(alerts.shell_lines([], 50)[0])


# -- the real app ----------------------------------------------------------------
@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A run with a busy worker, a parked question, usage readings, pings and a kept shell."""
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\nP1 needs:P0\nP2 needs:P0\n",
                                                      encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "grid")
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    cfg.state_path.write_text(json.dumps({
        "slots": [{"id": 0, "busy": True, "phase": "P1", "pane_id": "%1"},
                  {"id": 1, "busy": False}],
        "done": {"P0": "ok"}, "parked": ["P2"], "supervisor_pid": 1,
        "last_event_at": NOW - 30,
    }), encoding="utf-8")
    meters = cfg.state_dir / "meters"
    meters.mkdir(exist_ok=True)
    (meters / "limits.jsonl").write_text("".join(
        json.dumps({"ts": NOW - (6 - i) * H, "run_id": "r", "five_pct": 10.0 * i,
                    "five_resets_at": NOW + 2 * H, "week_pct": 70.0 + i,
                    "week_resets_at": NOW + 30 * H}) + "\n" for i in range(6)), encoding="utf-8")
    (cfg.state_dir / "notifications.jsonl").write_text(
        json.dumps({"ts": NOW - 1900, "kind": "park", "phase": "P2", "delivered": True,
                    "text": "which schema should P2 read?"}) + "\n"
        + json.dumps({"ts": NOW - 900, "kind": "overseer-digest", "phase": None,
                      "delivered": False, "suppressed": "routine",
                      "text": "Overseer: nothing needs you."}) + "\n", encoding="utf-8")
    return cfg


def _boot(cfg, size, steps, monkeypatch, capfd, **kw):
    from swarm_orchestrator.tui.app import SwarmApp
    from swarm_orchestrator.tui.dash import Dash

    monkeypatch.setattr(Dash, "probe", lambda self, now=None: None)  # no tmux/claude/git
    app = SwarmApp(cfg, **kw)
    got: dict = {}

    async def drive():
        async with app.run_test(size=size) as pilot:
            app.dash.poll()
            app.refresh_all()
            await pilot.pause()
            app.refresh_all()
            await pilot.pause()
            await steps(app, pilot, got)
        app.stop_board()

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(drive(), timeout=30))
    return got


def screen_text(app) -> str:
    console = Console(width=app.size.width, height=app.size.height, file=io.StringIO(),
                      record=True, legacy_windows=False)
    console.print(app.screen._compositor.render_update(full=True))
    return console.export_text()


@pytest.mark.parametrize("size", [(209, 50), (120, 40)], ids=lambda s: f"{s[0]}x{s[1]}")
def test_home_is_a_grid_of_boxes_and_the_tabs_are_listed_once(seeded, size, monkeypatch, capfd):
    async def steps(app, pilot, got):
        got["text"] = screen_text(app)
        node = app.query_one("#tab-home")
        got["side_x"] = node.query_one("#p-usage").region.x
        got["books_x"] = node.query_one("#p-books").region.x
        got["strip"] = node.query_one("#p-needs").display
        got["pinned"] = [r._swarm_text for r in node.query("#alert-need-rows Row") if r.display]
        got["notes"] = [r._swarm_text for r in node.query("#alert-rows Row") if r.display]

    got = _boot(seeded, size, steps, monkeypatch, capfd)
    text = got["text"]
    for title in ("phase books", "working now", "feed", "usage", "alerts & notifications",
                  "shells"):
        assert f"─ {title}" in text, title
    assert got["side_x"] > got["books_x"]  # two columns
    assert not got["strip"] and len(got["pinned"]) == 1 and "P2" in plain(got["pinned"][0])
    assert "which schema" in plain(got["notes"][1]) and "Overseer" in plain(got["notes"][0])
    assert text.count("1 home") == 1 and text.count("0 shells") == 1
    footer = text.rstrip().splitlines()[-1]
    assert "? help" in footer and "q quit" in footer and "home" not in footer
    # The suite runs with caps off: a plain 0-100 chart, and says so.
    assert "5-hour" in text and "usage caps are off" in text and "100┤" in text
    # Nothing clipped into garbage: every line inside the grid closes on a border.
    for line in text.splitlines():
        if line.lstrip().startswith(("│", "╭", "╰")):
            assert line.rstrip()[-1] in "│╮╯", line


def test_help_still_lists_every_tab_key():
    from swarm_orchestrator.tui.app import HelpScreen

    help_text = plain(HelpScreen.HELP)
    for n, name in [("1", "home"), ("4", "alerts"), ("0", "shells"), ("9", "runs")]:
        assert f"{n} {name}" in help_text


def test_x_on_home_acknowledges_the_lost_pings(seeded, monkeypatch, capfd):
    from swarm_orchestrator import telegram

    with (seeded.state_dir / "notifications.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": NOW - 100, "kind": "waiting", "phase": "P1",
                             "delivered": False, "error": "boom", "text": "t"}) + "\n")

    async def steps(app, pilot, got):
        node = app.query_one("#tab-home")
        node.focus()
        got["before"] = node.query_one("#b-problems")._swarm_text
        await pilot.press("x")
        await pilot.pause()
        app.refresh_all()
        await pilot.pause()
        got["after"] = node.query_one("#b-problems")._swarm_text

    got = _boot(seeded, (209, 50), steps, monkeypatch, capfd)
    assert "never reached your phone" in plain(got["before"])
    assert "never reached your phone" not in plain(got["after"])
    assert telegram.acked_at(seeded.state_dir) > 0


# -- the web board, in the dashboard -------------------------------------------------
@pytest.fixture
def web_cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_WEB", "1")
    monkeypatch.setenv("SWARM_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("SWARM_WEB_PORT", str(free_port()))
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    return cfg


def test_the_dashboard_serves_the_board_and_stops_it(web_cfg):
    from swarm_orchestrator.tui.webboard import SERVING, WebBoard

    board = WebBoard(web_cfg)
    try:
        assert board.ensure() == SERVING
        assert lifecycle.probe(web_cfg)[0] == lifecycle.OURS
        assert board.url.endswith(f":{web_cfg.web_port}/")
        assert board.ensure() == SERVING  # a second tick changes nothing
    finally:
        board.stop()
    assert lifecycle.probe(web_cfg)[0] == lifecycle.CLOSED
    assert board.ensure() == "off"  # stopped for good: the dashboard is exiting


def test_a_board_already_served_is_never_started_twice(web_cfg):
    from swarm_orchestrator.tui.webboard import ELSEWHERE, SERVING, WebBoard

    first, second = WebBoard(web_cfg), WebBoard(web_cfg)
    try:
        assert first.ensure() == SERVING
        assert second.ensure() == ELSEWHERE and not second.serving
        assert "another process" in second.line()[0]
    finally:
        first.stop()
        second.stop()


def test_a_port_held_by_another_program_is_reported_not_fought(web_cfg):
    from swarm_orchestrator.tui.webboard import TAKEN, WebBoard

    board = WebBoard(web_cfg, probe=lambda cfg: (lifecycle.TAKEN, "nginx (pid 7)"))
    assert board.ensure() == TAKEN and not board.serving
    text, tone = board.line()
    assert "held by nginx (pid 7)" in text and tone == "warn"


def test_the_app_starts_the_board_and_shows_where(web_cfg, monkeypatch, capfd):
    async def steps(app, pilot, got):
        for _ in range(40):
            if app.web_board.state != "off":
                break
            await pilot.pause(0.1)
        app.refresh_all()
        await pilot.pause()
        got["state"] = app.web_board.state
        got["bar"] = str(app.query_one("#statusbar").render())
        got["probe"] = lifecycle.probe(web_cfg)[0]

    got = _boot(web_cfg, (160, 40), steps, monkeypatch, capfd)
    assert got["state"] == "serving" and got["probe"] == lifecycle.OURS
    assert f":{web_cfg.web_port}/" in got["bar"]
    assert lifecycle.probe(web_cfg)[0] == lifecycle.CLOSED  # gone with the dashboard


def test_the_app_leaves_the_board_alone_when_web_is_off(seeded, monkeypatch, capfd):
    async def steps(app, pilot, got):
        got["board"] = app.web_board

    assert _boot(seeded, (120, 40), steps, monkeypatch, capfd)["board"] is None


# -- the restart bug: a new board while the old one still holds the port ------------------
def test_the_server_socket_reuses_the_address(web_cfg):
    srv = web_server.make_server(web_cfg, "127.0.0.1", 0)
    try:
        assert srv.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
    finally:
        srv.feed.stop()
        srv.server_close()


def test_a_restart_waits_for_the_old_board_to_let_go_of_the_port(web_cfg):
    port = int(web_cfg.web_port)
    old = socket.socket()
    old.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    old.bind(("127.0.0.1", port))
    old.listen(5)
    threading.Timer(0.6, old.close).start()  # the old board finishes closing
    began = time.monotonic()
    srv = web_server.make_server(web_cfg, "127.0.0.1", port, bind_wait_s=5.0)
    try:
        assert srv.server_address[1] == port
        assert time.monotonic() - began >= 0.5
    finally:
        srv.feed.stop()
        srv.server_close()


def test_a_port_that_stays_taken_still_fails(web_cfg):
    port = int(web_cfg.web_port)
    with socket.socket() as held:
        held.bind(("127.0.0.1", port))
        held.listen(1)
        with pytest.raises(OSError):
            web_server.make_server(web_cfg, "127.0.0.1", port, bind_wait_s=0.3)


# -- the owner's guide ---------------------------------------------------------------
def test_g_opens_the_guide_and_the_hint_counts_the_owners_todos(seeded, monkeypatch, capfd):
    """`g guide me (N)` in the footer, the count in the alerts box, `g` and the
    palette open the guide (its window is faked: no tmux here)."""
    from swarm_orchestrator import guide

    calls = []
    monkeypatch.setattr(guide, "open_window",
                        lambda cfg: calls.append(cfg) or (guide.FOCUSED, "@9"))

    async def steps(app, pilot, got):
        app.dash.todos = [object(), object()]
        app.refresh_all()
        await pilot.pause()
        got["text"] = screen_text(app)
        got["problems"] = app.query_one("#tab-home").query_one("#b-problems")._swarm_text
        got["palette"] = [c.title for c in app.get_system_commands(app.screen)]
        await pilot.press("g")
        await app.workers.wait_for_complete()
        await pilot.pause()

    got = _boot(seeded, (209, 50), steps, monkeypatch, capfd)
    footer = got["text"].rstrip().splitlines()[-1]
    assert "g guide me (2)" in footer
    assert "2 to-do(s) for you" in plain(got["problems"])
    assert "2 to-do(s)" in got["text"] and "g guide me ·" in got["text"]
    assert "Guide me" in got["palette"]
    assert len(calls) == 1
