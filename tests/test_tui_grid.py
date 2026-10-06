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

from conftest import machine_toml
from rich.console import Console
from rich.text import Text

from swarm_orchestrator.config import load
from swarm_orchestrator.eta.forecast import Forecast, Range
from swarm_orchestrator.tui import alerts, charts, data, home, usagebox
from swarm_orchestrator import usage
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
    assert cut["books"] == home.TALL_BOOKS and cut["usage_chart"] == 5
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


def ahead(dash, window="week"):
    """The outlook's sentence for one window."""
    name = usagebox.NAMES[window]
    return next((t, st) for t, st in usagebox.outlook(dash, NOW) if t.startswith(name))


def test_the_outlook_says_when_a_cap_is_hit_at_this_pace_and_when_the_window_resets():
    # 86% now, 1 point per busy worker-hour at 1 worker: 90% in 4h, the reset in 20h.
    dash = usage_dash(week_samples([86]), burn={"week": 1.0}, overall=NOW + 10 * H)
    text, state = ahead(dash)
    hit, resets = data.fmt_when(NOW + 4 * H, NOW), data.fmt_when(NOW + 20 * H, NOW)
    assert text == f"weekly hits the 90% pause ~{hit} at this pace (resets {resets})"
    assert state == "warn"


def test_a_window_that_resets_first_wont_hit_its_cap():
    dash = usage_dash(week_samples([86]), burn={"week": 0.01}, overall=NOW + 2 * H)
    text, state = ahead(dash)
    assert text == f"weekly won't hit the 90% pause before it resets {data.fmt_when(NOW + 20 * H, NOW)}"
    assert state == "ok"


def test_the_outlook_speaks_for_both_windows_and_is_never_cut():
    dash = usage_dash(week_samples([86]), burn={"week": 1.0, "five_hour": 50.0})
    said = [t for t, _ in usagebox.outlook(dash, NOW)]
    assert said[0].startswith("5-hour hits the 90% pause ~") and said[1].startswith("weekly hits")
    for width in (80, 42):
        lines = [plain(x) for x in usagebox.box_lines(dash, width, NOW, chart_rows=0)]
        joined = " ".join(x[usagebox.LABEL_W:].strip() for x in lines if x.startswith(("ahead", "   ")))
        assert all(t in joined for t in said), (width, lines)
        assert all(len(x) <= width and "…" not in x for x in lines), lines


def test_nothing_running_burns_nothing():
    dash = usage_dash(week_samples([86]), busy=0, burn={"week": 1.0})
    assert ahead(dash)[0] == "weekly won't hit the 90% pause: nothing is running"


def test_a_held_window_says_until_when():
    dash = usage_dash(week_samples([91]), hold={"week": {"resets_at": NOW + 20 * H}})
    text, state = ahead(dash)
    assert text.startswith("weekly cap holds new work until") and state == "bad"


def test_both_bars_share_one_width_start_and_scale():
    """A shorter reset text once bought one bar more cells: 39% drew longer than 90%."""
    samples = [Sample(ts=NOW - H, five_pct=50, five_resets_at=NOW + 600,
                      week_pct=50, week_resets_at=NOW + 3 * 86400 + 5 * H)]
    for width in (80, 60, 42):
        five, week = (plain(x) for x in usagebox.box_lines(usage_dash(samples), width, NOW,
                                                           chart_rows=0)[:2])
        cells = [line[usagebox.LABEL_W:].split(" ")[0] for line in (five, week)]
        assert len(cells[0]) == len(cells[1]), (width, five, week)
        assert cells[0].count("█") == cells[1].count("█"), (width, five, week)
        assert five.index("50%") == week.index("50%")


def test_the_chart_runs_to_the_reset_with_the_projection_dotted_in():
    dash = usage_dash(week_samples([60, 62, 64, 66]), burn={"week": 1.0}, busy=2)
    rows = [plain(x) for x in usagebox.chart_lines(dash, 80, 5, NOW)]
    week = [r[40:] for r in rows]
    assert any(charts.PROJ in r for r in week), week
    assert data.fmt_when(NOW + 20 * H, NOW)[-5:] in rows[-1] or "03:00" in rows[-1] or rows[-1]


def test_an_account_switch_breaks_the_chart_and_restarts_the_burn():
    """The owner switched accounts: weekly 90% -> 31%, its reset a day later."""
    old = [Sample(ts=NOW - (10 - i) * H, five_pct=30 + i, five_resets_at=NOW + 3 * H,
                  week_pct=80 + i, week_resets_at=NOW + 20 * H) for i in range(8)]
    new = [Sample(ts=NOW - (3 - i) * H + 1, five_pct=6 + i, five_resets_at=NOW + 4 * H,
                  week_pct=31 + i, week_resets_at=NOW + 40 * H) for i in range(3)]
    samples = old + new
    assert usage.accounts(samples) == [0] * 8 + [1] * 3
    assert usage.switched_at(samples) == new[0].ts
    # The switch is not usage: the new account's 31% is its baseline.
    pace = usage.week_pace(samples, NOW - 12 * H, NOW)
    assert pace.used == 7 + 2
    pts = usagebox.series(samples, "week", NOW - 12 * H, NOW)
    assert pts[-3:] == [(new[0].ts, 31), (new[1].ts, 32), (new[2].ts, 33)]
    rows = [plain(x) for x in usagebox.chart_lines(usage_dash(samples), 80, 5, NOW)]
    assert any(charts.BREAK in r[40:] for r in rows), rows


def test_a_lone_reading_of_the_other_account_is_ignored():
    """A lagging session still on the old account is not a switch back."""
    a = dict(week_resets_at=NOW + 20 * H)
    b = dict(week_resets_at=NOW + 40 * H)
    samples = [Sample(ts=NOW - 5 * H, week_pct=90, **a), Sample(ts=NOW - 4 * H, week_pct=31, **b),
               Sample(ts=NOW - 3 * H, week_pct=32, **b), Sample(ts=NOW - 2 * H, week_pct=90, **a),
               Sample(ts=NOW - H, week_pct=33, **b)]
    assert usage.accounts(samples) == [0, 1, 1, None, 1]
    assert usage.week_pace(samples, NOW - 6 * H, NOW).used == 2


def test_a_reset_is_not_an_account_switch():
    samples = [Sample(ts=NOW - 3 * H, week_pct=98, week_resets_at=NOW - 2 * H),
               Sample(ts=NOW - H, week_pct=1, week_resets_at=NOW + 7 * 86400 - 2 * H),
               Sample(ts=NOW - 0.5 * H, week_pct=2, week_resets_at=NOW + 7 * 86400 - 2 * H)]
    assert usage.accounts(samples) == [0, 0, 0] and usage.switched_at(samples) is None


def test_the_burn_is_measured_from_the_switch():
    from swarm_orchestrator.eta import engine

    old = [Sample(ts=NOW - (30 - i) * H, week_pct=40 + 2 * i, week_resets_at=NOW + 20 * H)
           for i in range(25)]
    new = [Sample(ts=NOW - (4 - i) * H, week_pct=31 + 0.5 * i, week_resets_at=NOW + 40 * H)
           for i in range(4)]
    events = [SimpleNamespace(kind="launch", phase="P0", ts=NOW - 40 * H, slot=0, status=None)]
    got = engine.burn_of(events, 1, old + new, NOW)
    assert got["week"] == pytest.approx(1.5 / 4, rel=0.05)  # not the old 2 points an hour


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
    wide = plain(alerts.note_line(note(kind="lane-unprepared"), 80, NOW))
    narrow = plain(alerts.note_line(note(kind="lane-unprepared"), 40, NOW))
    assert "lane-unprepared" in wide and "lane" not in narrow
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
        + json.dumps({"ts": NOW - 900, "kind": "lane-unprepared", "phase": None,
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


# -- the web board: the dashboard says where it is, and does not serve it ---------------
@pytest.fixture
def web_cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_WEB", "1")
    machine_toml(web={"host": "127.0.0.1", "port": free_port()})
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    return cfg


def _serving(at):
    srv = web_server.make_server(at.root, at.host, at.port)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


def test_the_status_bar_says_where_this_swarms_page_is(web_cfg):
    from swarm_orchestrator.tui.app import board_standing

    at = lifecycle.place(web_cfg.state_dir)
    (text, tone), link = board_standing(web_cfg)
    assert "not running" in text and "swarm web" in text and (tone, link) == ("muted", "")
    srv = _serving(at)
    try:
        (text, tone), link = board_standing(web_cfg)
        assert link == f"http://127.0.0.1:{at.port}/s/state/"
        assert (text, tone) == (f"board {link}", "muted")
    finally:
        web_server.close(srv)


def test_a_port_held_by_another_program_is_reported_not_fought(web_cfg, monkeypatch):
    from swarm_orchestrator.tui.app import board_standing

    monkeypatch.setattr(lifecycle, "probe", lambda at: lifecycle.Found(
        lifecycle.TAKEN, "nginx (pid 7)", "set [web].port"))
    (text, tone), link = board_standing(web_cfg)
    assert "held by nginx (pid 7)" in text and (tone, link) == ("warn", "")


def test_the_app_shows_where_the_board_is_and_serves_none(web_cfg, monkeypatch, capfd):
    at = lifecycle.place(web_cfg.state_dir)

    async def steps(app, pilot, got):
        for _ in range(40):
            if app.board_line[0]:
                break
            await pilot.pause(0.1)
        got["closed"] = (app.board_line[0], lifecycle.probe(at).state)
        srv = _serving(at)
        try:
            app._board_tick()
            for _ in range(40):
                if app.board_link:
                    break
                await pilot.pause(0.1)
            app.refresh_all()
            await pilot.pause()
            got["bar"] = str(app.query_one("#statusbar").render())
            got["url"] = app.board_url()
        finally:
            web_server.close(srv)

    got = _boot(web_cfg, (200, 40), steps, monkeypatch, capfd)
    # The dashboard starts no board: the machine's is `swarm up`'s to start.
    assert "not running" in got["closed"][0] and got["closed"][1] == lifecycle.CLOSED
    assert got["url"] == f"http://127.0.0.1:{at.port}/s/state/" and got["url"] in got["bar"]


def test_the_app_leaves_the_board_alone_when_web_is_off(seeded, monkeypatch, capfd):
    async def steps(app, pilot, got):
        await pilot.pause(0.3)
        got["board"] = (app.shows_board, app.board_line, app.board_link)

    assert _boot(seeded, (120, 40), steps, monkeypatch, capfd)["board"] == (False, ("", ""), "")


# -- the restart bug: a new board while the old one still holds the port ------------------
def test_the_server_socket_reuses_the_address(web_cfg):
    srv = web_server.make_server(web_cfg.state_dir.parent, "127.0.0.1", 0)
    try:
        assert srv.socket.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR)
    finally:
        srv.hub.stop()
        srv.server_close()


def test_a_restart_waits_for_the_old_board_to_let_go_of_the_port(web_cfg):
    port = lifecycle.place(web_cfg.state_dir).port
    old = socket.socket()
    old.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    old.bind(("127.0.0.1", port))
    old.listen(5)
    threading.Timer(0.6, old.close).start()  # the old board finishes closing
    began = time.monotonic()
    srv = web_server.make_server(web_cfg.state_dir.parent, "127.0.0.1", port, bind_wait_s=5.0)
    try:
        assert srv.server_address[1] == port
        assert time.monotonic() - began >= 0.5
    finally:
        srv.hub.stop()
        srv.server_close()


def test_a_port_that_stays_taken_still_fails(web_cfg):
    port = lifecycle.place(web_cfg.state_dir).port
    with socket.socket() as held:
        held.bind(("127.0.0.1", port))
        held.listen(1)
        with pytest.raises(OSError):
            web_server.make_server(web_cfg.state_dir.parent, "127.0.0.1", port, bind_wait_s=0.3)


# -- the owner console ---------------------------------------------------------------
def test_o_reopens_the_owner_console_and_is_listed(seeded, monkeypatch, capfd):
    """`o` in the footer, the help and the palette; pressing it asks the console to
    open (tmux is faked here: tests/test_console.py runs the real window), once
    per press, and says so when it cannot."""
    from swarm_orchestrator import console
    from swarm_orchestrator.tui.app import HelpScreen

    calls = []
    monkeypatch.setattr(console, "open_console",
                        lambda cfg: calls.append(cfg) or (console.FOCUSED, "@9"))

    async def steps(app, pilot, got):
        got["text"] = screen_text(app)
        got["palette"] = [c.title for c in app.get_system_commands(app.screen)]
        await pilot.press("o")
        await app.workers.wait_for_complete()
        await pilot.pause()
        got["calls"] = list(calls)

        def refuse(cfg):
            raise console.ConsoleError("the console is off")
        monkeypatch.setattr(console, "open_console", refuse)
        await pilot.press("o")
        await app.workers.wait_for_complete()
        await pilot.pause()
        got["toasts"] = [str(n.message) for n in app._notifications]

    got = _boot(seeded, (209, 50), steps, monkeypatch, capfd)
    assert "o console" in got["text"].rstrip().splitlines()[-1]
    assert "Owner console" in got["palette"]
    assert "owner console" in HelpScreen.HELP
    assert len(got["calls"]) == 1
    assert any("console: the console is off" in t for t in got["toasts"])


@pytest.mark.parametrize("size", [(209, 50), (120, 40)], ids=lambda s: f"{s[0]}x{s[1]}")
def test_home_fits_the_resources_box_beside_the_console_key(seeded, size, monkeypatch, capfd):
    """Both features on one screen: the footer keeps `o console`, and the side
    column fits the resources box (a build, an idle holder, a queue) above the
    alerts, all inside the window."""
    from swarm_orchestrator.resources import store as resources_store

    now = time.time()
    resources_store.write_now(seeded.state_dir, {
        "ts": now, "static": {"ncpu": 8}, "queued": 2,
        "host": {"cpu": 42.0, "load": 3.1, "avail_mb": 9000, "anon_mb": 5000,
                 "cache_mb": 2000, "swap_mb": 0, "psi": {"mem": 1, "memf": 0, "io": 2, "iof": 1},
                 "wr_mbs": 12.5},
        "disk": {"headroom_gb": 80.0},
        "builds": [{"id": "a", "slot": 0, "phase": "P1", "age_s": 300, "cores": 3.2,
                    "anon_mb": 2048, "idle": False},
                   {"id": "b", "slot": 1, "phase": "P3", "age_s": 900, "cores": 0.0,
                    "anon_mb": 300, "idle": True}],
    })

    async def steps(app, pilot, got):
        got["text"] = screen_text(app)
        node = app.query_one("#tab-home")
        got["res"] = node.query_one("#p-resources").region
        got["alerts"] = node.query_one("#p-alerts").region
        got["h"] = app.size.height

    got = _boot(seeded, size, steps, monkeypatch, capfd)
    text = got["text"]
    assert "o console" in text.rstrip().splitlines()[-1]
    assert "─ resources" in text and "2 build(s) · swarm resources" in text
    assert "IDLE holder" in text and "2 build(s) queued behind" in text
    assert got["res"].height >= 7 and got["res"].bottom <= got["alerts"].y
    assert got["alerts"].height >= 6 and got["alerts"].bottom < got["h"]


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
