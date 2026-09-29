"""Tests for the home screen's view model.

The home screen is the only thing on the owner's monitor all day, and its whole
job is to be right at a glance. The rebuild traded five panels for two: a
borderless headline strip, a bordered ``working now``, a bordered ``phases
done``, three borderless ``just finished`` rows and one health line. What is
tested here is the pure builders behind those — rendering is not, deliberately;
``tests/test_tui.py`` already proves the app boots.

Three properties get the most coverage:

* **the headline counts live work, not history** — the ledger holds every phase
  the project ever had, most of them ancient ``skip`` entries, and averaging
  those in produces a number that cannot move;
* **the headline says what the forecast says** — every open phase book in one
  line, what it was made on, and what waits on the owner; before the first
  forecast lands it says it is working it out rather than inventing a time;
* **nothing raises** — every builder is handed a swarm that never started, an
  empty ``Dash``, and garbage, because a panel that raises takes the cockpit
  with it.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from textual.content import Content

from swarm_orchestrator.eta.forecast import Book, Forecast, Range, RowView
from swarm_orchestrator.eta.plan import OWNER_RUN, Stuck
from swarm_orchestrator.tui import data, home
from swarm_orchestrator.tui import timeline as tl
from swarm_orchestrator.tui.theme import BAD, COLOR, YOU

NOW = 1_700_000_000.0


def plain(markup: str) -> str:
    """What the screen actually shows, with the colour markup resolved away.

    Goes through Textual's ``Content`` rather than Rich's ``Text`` because that
    is the real render path: ``Static.update`` parses Textual markup, and the
    ``rich.markup.escape`` the builders apply to worker prose has to survive
    *that* parser, not a different one.
    """
    return Content.from_markup(markup).plain


class FakeDash:
    """A :class:`~swarm_orchestrator.tui.dash.Dash` with nothing behind it.

    The real one needs a state dir, a tmux server and a live ``claude``; the
    builders only ever read attributes, so a namespace is a complete stand-in and
    lets a test state one awkward condition at a time.
    """

    def __init__(self, snapshot=None, rows=(), **kw) -> None:
        self.snapshot = snapshot if snapshot is not None else data.Snapshot()
        self.graph = kw.get("graph", {})
        self.history = kw.get("history", [])
        self.recaps = kw.get("recaps", {})
        self.sentinels = kw.get("sentinels", {})
        self.notes = kw.get("notes", {})
        self.notifications = kw.get("notifications", [])
        self.tails = kw.get("tails", {})
        self.contexts = kw.get("contexts", {})
        self.tail = SimpleNamespace(events=kw.get("events", []))
        self.finished = kw.get("finished", {})
        self.forecast = kw.get("forecast")
        self.deferred = kw.get("deferred", {})
        self.ticked = kw.get("ticked")
        self.cfg = kw.get(
            "cfg", SimpleNamespace(exclude=[], state_dir=None, project_dir=".", max_workers=4)
        )
        self._rows = list(rows)

    def slot_rows(self):
        return self._rows


def slot(id_=0, phase="dash-W3", busy=True, **kw):
    return data.SlotView(
        id=id_,
        busy=busy,
        phase=phase,
        pane_id=kw.get("pane_id", f"%{id_}"),
        branch=None,
        worktree=None,
        retiring=kw.get("retiring", False),
        started_at=kw.get("started_at", time.time() - 2820),
    )


def done_event(ts, phase, status="ok"):
    return data.Event(ts, "done", phase, status, {}, f"EVENT done {phase} {status}")


# -- text primitives ------------------------------------------------------
def test_clip_collapses_newlines_and_ellipsises():
    assert home.clip("a\nb   c", 20) == "a b c"
    assert home.clip("x" * 30, 10) == "x" * 9 + "…"
    assert home.clip("anything", 0) == ""


def test_activity_reads_past_the_frame():
    tail = "\n".join(
        [
            "⏺ Read(collector.rs)",
            "✻ Wrangling… (23s · ↑ 1.2k tokens · esc to interrupt)",
            "╭───────────────╮",
            "│ >             │",
            "╰───────────────╯",
            "  420k/1.0M",
        ]
    )
    assert home.activity(tail) == "Wrangling…"
    assert home.activity("") == ""
    assert home.activity("╭──╮\n│ >│") == ""


def test_set_text_skips_an_unchanged_assignment():
    """A tick where nothing changed must produce no widget writes."""

    class Spy:
        def __init__(self):
            self.writes = 0

        def update(self, text):
            self.writes += 1

    spy = Spy()
    home.set_text(spy, "one")
    home.set_text(spy, "one")
    home.set_text(spy, "two")
    assert spy.writes == 2


# -- the forecast -----------------------------------------------------------
H = 3600.0


def forecast(**kw) -> Forecast:
    """A forecast the way the engine hands one over: two books, one behind the owner."""
    books = kw.pop("books", (
        Book("dash", 1, 8, 1, 2, finish=Range(NOW + 2 * H, NOW + 4 * H, NOW + 6 * H),
             rows=(RowView("dash-W2", "running", "", Range(NOW + H, NOW + 2 * H, NOW + 3 * H)),)),
        Book("rec", 0, 3, 0, 0, behind=3),
    ))
    base = dict(made_at=NOW, runs=500, workers=4, overall=Range(NOW + 2 * H, NOW + 4 * H,
                                                                 NOW + 6 * H),
                books=books, stuck=(Stuck("ivory-W0", OWNER_RUN, ("ivory-W1", "ivory-W2", "ivory-W3")),),
                caps=(("week", 90.0, 80.0),), working=0.4)
    return Forecast(**(base | kw))


def test_fmt_coarse_never_claims_a_second():
    assert data.fmt_coarse(6600) == "1h 50m"
    assert data.fmt_coarse(3000) == "50m"
    assert data.fmt_coarse(20) == "< 1m"


# -- headline -------------------------------------------------------------
def myproject_graph():
    """The shape of a mixed ledger: one live campaign, buried.

    22 ``dash`` waves being built now, 19 ``rec`` waves finished earlier, and 118
    single-phase ``skip`` artefacts seeded from campaigns that ended weeks ago.
    """
    graph, done = {}, {}
    for i in range(1, 23):
        graph[f"dash-W{i}"] = set()
        if i <= 9:
            done[f"dash-W{i}"] = "ok"
    for i in range(1, 20):
        graph[f"rec-W{i}"] = set()
        done[f"rec-W{i}"] = "ok"
    for i in range(1, 119):
        graph[f"I{i}"] = set()
        done[f"I{i}"] = "skip"
    return graph, done


def test_headline_counts_live_work_not_the_ledger_total():
    graph, done = myproject_graph()
    snap = data.Snapshot(ok=True, done=done, slots=[slot(0, "dash-W10")])
    text = plain(home.headline(FakeDash(snap, graph=graph), 76))
    assert "dash" in text and "9 / 22 phases · 41%" in text
    assert "161" not in text
    assert "1 running" in text and "12 ready" in text


def test_headline_skips_a_campaign_the_ledger_already_ticked():
    # A campaign whose rows were mostly ticked in the ledger, none in the done map,
    # once headlined as "read 0 / 104" with an ETA timed on all of them.
    graph = {f"read-W{i}": set() for i in range(1, 6)}
    graph.update({"teal-W1": set(), "teal-W2": {"teal-W1"}})
    landed = {f"read-W{i}": "ledger" for i in range(1, 6)}
    snap = data.Snapshot(ok=True, done={}, landed=landed, slots=[slot(0, "teal-W1")])
    text = plain(home.headline(FakeDash(snap, graph=graph), 76))
    assert "teal" in text and "0 / 2 phases" in text
    assert "read" not in text


def test_headline_says_when_the_big_picture_was_refreshed():
    graph = {f"dash-W{i}": set() for i in range(1, 4)}
    dash = FakeDash(data.Snapshot(ok=True, slots=[slot(0, "dash-W1")]), graph=graph)
    assert "big picture" not in plain(home.headline(dash, 76))
    dash.big_picture = "big picture 2.0h ago"
    assert "big picture 2.0h ago" in plain(home.headline(dash, 76))


def test_headline_carries_the_forecast_and_what_it_was_made_on():
    graph = {f"dash-W{i}": set() for i in range(1, 9)}
    dash = FakeDash(data.Snapshot(ok=True, done={"dash-W1": "ok"}, slots=[slot(0, "dash-W2")]),
                    graph=graph, forecast=forecast())
    text = plain(home.headline(dash, 160))
    assert "7 rows left · done ~" in text
    assert "simulated 500× · 4 workers · working 40% of the time" in text
    assert "weekly cap pauses at 90% (80% now)" in text
    assert "waiting on you: ivory-W0 holds 3 rows (ivory)" in text


def test_headline_says_it_is_working_it_out_before_the_first_forecast():
    graph = {f"dash-W{i}": set() for i in range(1, 9)}
    dash = FakeDash(data.Snapshot(ok=True, slots=[slot(0, "dash-W1")]), graph=graph)
    assert "working out when" in plain(home.headline(dash, 76))
    dash.eta = SimpleNamespace(error="ValueError: bad row")
    assert "no forecast: ValueError: bad row" in plain(home.headline(dash, 100))


def test_a_paused_swarm_says_its_work_and_a_held_one_says_until_when():
    """"done ~16:28" for a swarm the owner paused is a lie, and one that goes staler by
    the hour it stays paused; the work left, once it runs again, is neither."""
    graph = {f"dash-W{i}": set() for i in range(1, 5)}
    snap = data.Snapshot(ok=True, slots=[slot(0, None, busy=False)])
    paused = plain(home.headline(FakeDash(snap, graph=graph, forecast=forecast(stopped="paused")),
                                 160))
    assert "paused · ~2–4h of work once it runs again" in paused
    held = plain(home.headline(FakeDash(snap, graph=graph,
                                        forecast=forecast(held_until=NOW + 40 * H)), 200))
    assert "held by the cap until" in held


def test_headline_says_how_many_done_rows_the_ledger_still_shows_open():
    """30/34 against a ledger with 9 open rows needs its five explained, on the screen."""
    graph = {f"perf-F{i}": set() for i in range(1, 7)}
    landed = {"perf-F1": "ledger", "perf-F2": "ledger", "perf-F3": "operator", "perf-F4": "ok"}
    snap = data.Snapshot(ok=True, landed=landed, slots=[slot(0, "perf-F5")])
    text = plain(home.headline(FakeDash(snap, graph=graph, ticked={"perf-F1", "perf-F2"}), 100))
    assert "4 / 6 phases" in text and "2 done but still open in the ledger" in text


def test_headline_counts_a_failed_row_and_what_waits_behind_it():
    """Every remaining phase depends on the one that failed: the forecast leaves it out."""
    graph = {"dash-W1": set(), "dash-W2": {"dash-W1"}}
    dash = FakeDash(data.Snapshot(ok=True, done={"dash-W1": "fail"}), graph=graph)
    text = plain(home.headline(dash, 76))
    assert "1 failed" in text and "1 blocked" in text


def test_headline_counts_a_row_waiting_for_its_date_apart_from_ready():
    """perf-F36 had `after:2026-10-01` and was counted ready: the launcher never runs it early."""
    graph = {"perf-F35": set(), "perf-F36": set()}
    snap = data.Snapshot(ok=True, slots=[slot(0, "perf-F35")])
    text = plain(home.headline(FakeDash(snap, graph=graph, deferred={"perf-F36": "2026-10-01"}),
                               100))
    assert "1 running" in text and "1 waiting for a date" in text and "ready" not in text


def test_headline_before_the_run_starts():
    graph = {f"dash-W{i}": set() for i in range(1, 4)}
    text = plain(home.headline(FakeDash(data.Snapshot(ok=False), graph=graph), 76))
    assert "0 / 3 phases · 0%" in text and "run not started" in text


def test_headline_with_no_ledger():
    assert "no phases scheduled" in plain(home.headline(FakeDash(), 76))


def test_headline_never_headlines_a_skip_artefact():
    """A single-phase ``skip`` can neither move nor show work — it is pure noise."""
    graph = {f"I{i}": set() for i in range(1, 6)}
    done = {p: "skip" for p in graph}
    text = plain(home.headline(FakeDash(data.Snapshot(ok=True, done=done), graph=graph), 76))
    assert "no phases scheduled" in text


# -- working now ----------------------------------------------------------
def test_worker_rows_carry_their_phase_and_slot():
    rows = [(slot(0, "dash-W3"), "busy", "", 38.0, None)]
    out = home.worker_rows(FakeDash(data.Snapshot(ok=True, slots=[rows[0][0]]), rows), 44)
    text, phase, key = out[0]
    assert (phase, key) == ("dash-W3", 0)
    assert "dash-W3" in plain(text) and "47m" in plain(text) and "38%" in plain(text)


def test_worker_rows_free_slot_has_no_phase_to_open():
    free = [(slot(1, None, busy=False), "idle", "", None, None)]
    text, phase, key = home.worker_rows(FakeDash(data.Snapshot(ok=True), free), 44)[0]
    assert phase is None and key is None
    assert "free" in plain(text)


def test_worker_rows_show_the_note_under_the_row():
    rows = [(slot(0, "dash-W3"), "busy", "", 38.0, None)]
    dash = FakeDash(
        data.Snapshot(ok=True, slots=[rows[0][0]]),
        rows,
        tails={"%0": "⏺ wiring the cgroup collector"},
    )
    text = plain(home.worker_rows(dash, 44)[0][0])
    assert "wiring the cgroup collector" in text
    assert len(text.splitlines()) == 2


def test_worker_rows_mark_the_selected_slot():
    rows = [(slot(0, "dash-W3"), "busy", "", 38.0, None), (slot(1, "dash-W4"), "busy", "", 12.0, None)]
    snap = data.Snapshot(ok=True, slots=[rows[0][0], rows[1][0]])
    out = home.worker_rows(FakeDash(snap, rows), 44, selected=1)
    assert not plain(out[0][0]).startswith("▸")
    assert plain(out[1][0]).startswith("▸")


def test_worker_rows_fold_the_free_slots_into_one_line():
    """Four rows of "free" said one thing four times and pushed the feed down."""
    rows_ = [(slot(i, None, busy=False), "idle", "", None, None) for i in range(4)]
    rows_.insert(1, (slot(9, "P1"), "busy", "", None, None))
    out = home.worker_rows(FakeDash(data.Snapshot(ok=True), rows_), 60)
    assert len(out) == 2
    assert out[0][1] == "P1"
    assert "0 1 2 3" in plain(out[1][0]) and "free" in plain(out[1][0])


def test_worker_rows_repaint_a_blocked_slot_as_waiting():
    """The row and the blocker drawer must never disagree about who is stuck."""
    s = slot(0, "dash-W10")
    snap = data.Snapshot(
        ok=True, slots=[s], blockers=[data.Blocker(phase="dash-W10", kind="parked", question="?")]
    )
    text = home.worker_rows(FakeDash(snap, [(s, "busy", "", 10.0, None)]), 44)[0][0]

    assert COLOR[YOU] in text
    assert "waiting: ?" in plain(text)


def test_worker_rows_flag_a_slot_whose_pane_died():
    """State says busy, nothing is in the pane — the stall this panel exists for."""
    s = slot(0, "dash-W3")
    text = home.worker_rows(FakeDash(data.Snapshot(ok=True, slots=[s]), [(s, "gone", "", None, None)]), 44)[0][0]
    assert "gone" in plain(text)


def test_worker_rows_with_no_slots_say_why():
    dash = FakeDash(data.Snapshot(ok=False, reason="no state yet — has `swarm up` run?"))
    assert "swarm up" in plain(home.worker_rows(dash, 44)[0][0])


def test_worker_note_prefers_the_live_pane():
    s = slot(0, "dash-W3")
    dash = FakeDash(
        tails={"%0": "⏺ building"}, recaps={"dash-W3": data.Recap("dash-W3", summary="done")}
    )
    assert home.worker_note(dash, s, "swap policy") == "waiting: swap policy"
    assert home.worker_note(dash, s, "") == "building"
    assert home.worker_note(FakeDash(recaps=dash.recaps), s, "") == "done"
    assert home.worker_note(FakeDash(), s, "") == ""


# -- phases done ----------------------------------------------------------
def finishes(count, step=600.0, end=NOW):
    return {f"P{i}": end - (count - i) * step for i in range(count)}


def test_chart_lines_need_something_to_plot():
    assert "not enough" in plain(home.chart_lines(FakeDash(), 40, 5, now=NOW)[0])
    one = FakeDash(finished=finishes(1))
    assert "not enough" in plain(home.chart_lines(one, 40, 5, now=NOW)[0])


def test_chart_lines_are_height_plus_an_axis_and_never_overflow():
    """A chart one column too wide does not look wrong — it reflows the panel."""
    lines = home.chart_lines(FakeDash(finished=finishes(12)), 40, 5, now=NOW)
    assert len(lines) == 6
    assert all(len(plain(line)) == 40 for line in lines)


def test_chart_lines_put_the_ticks_under_the_curve():
    """The axis starts where the plot does, past the y-scale gutter."""
    lines = home.chart_lines(FakeDash(finished=finishes(12)), 40, 5, now=NOW)
    gutter = plain(lines[0]).index("┤") + 1
    assert plain(lines[-1])[:gutter].strip() == ""
    assert plain(lines[-1]).strip()


def test_the_chart_counts_every_machine_s_finishes_and_runs_on_to_now():
    """It stopped when the swarm moved machines; the ledger's ticks carry it to today."""
    here = finishes(4, end=NOW - 3 * 86400)
    there = {f"Q{i}": NOW - 86400 + i * 600 for i in range(60)}
    series = home.finish_series(FakeDash(finished=here | there), NOW)
    assert series.points[-1] == (NOW, 64.0)
    assert series.span[0] == min(here.values())
    old = {"ancient": NOW - 30 * 86400}  # past the window: not this chart's business
    assert home.finish_series(FakeDash(finished=old | here), NOW).points[-1][1] == 4.0


# -- the feed -------------------------------------------------------------
def feed_of(*items):
    return list(items)


def test_feed_rows_show_what_each_phase_said_and_decided():
    items = [
        tl.FeedItem(NOW - 60, tl.FINISH, "dash-W2", "ok", "cgroup collector built"),
        tl.FeedItem(NOW - 120, "decision", "dash-W2", "", "kept the v1 schema"),
        tl.FeedItem(NOW - 180, tl.OWNER, "dash-W1", "", "ship it without the graph"),
    ]
    rows = home.feed_rows(items, 100, now=NOW)
    text = " ".join(plain(row) for row, _, _ in rows)
    assert "cgroup collector built" in text and "finished" in text
    assert "kept the v1 schema" in text and "decided" in text
    assert "ship it without the graph" in text and "you decided" in text


def test_feed_rows_highlight_the_owners_own_decisions():
    """Of every call in the feed, the owner's are the ones they look for."""
    row = home.feed_rows([tl.FeedItem(NOW, tl.OWNER, "P1", "", "yes")], 100, now=NOW)[0][0]
    assert COLOR[YOU] in row and "[b]" in row


def test_feed_rows_give_a_long_recap_a_second_line_and_notes_one():
    long = "word " * 60
    rows = home.feed_rows([tl.FeedItem(NOW, tl.FINISH, "P1", "ok", long),
                           tl.FeedItem(NOW, "decision", "P1", "", long)], 90, now=NOW)
    assert plain(rows[0][0]).count("\n") == 1
    assert plain(rows[1][0]).count("\n") == 0
    for row, _, _ in rows:
        assert all(len(line) <= 90 for line in plain(row).splitlines())


def test_feed_rows_drop_the_label_column_when_narrow():
    item = tl.FeedItem(NOW, "decision", "P1", "", "a call")
    assert "decided" in plain(home.feed_rows([item], 90, now=NOW)[0][0])
    assert "decided" not in plain(home.feed_rows([item], 60, now=NOW)[0][0])


def test_feed_rows_mark_the_selected_key_only():
    items = [tl.FeedItem(NOW - i, tl.FINISH, "P1", "ok", "x") for i in range(2)]
    key = home.feed_key(items[1])
    rows = home.feed_rows(items, 80, selected=key, now=NOW)
    assert [plain(r).startswith("▸") for r, _, _ in rows] == [False, True]
    assert rows[0][1] != rows[1][1]  # a retried phase still gets two keys


def test_feed_rows_escape_worker_text():
    """Worker prose routinely contains ``[``; unescaped it would eat the line."""
    item = tl.FeedItem(NOW, tl.FINISH, "P1", "ok", "fixed [bold]cfg[/] parse")
    assert "[bold]cfg[/]" in plain(home.feed_rows([item], 90, now=NOW)[0][0])


def test_feed_rows_say_what_the_overseer_left_for_the_owner():
    item = tl.FeedItem(NOW, tl.OVERSEER, None, "done", "filed two rows", left="decide W12",
                       ref="20260923T100000Z")
    text = plain(home.feed_rows([item], 120, now=NOW)[0][0])
    assert "filed two rows" in text and "left for you: decide W12" in text


def test_empty_feed_lists_what_runs_next():
    dash = FakeDash(data.Snapshot(ok=True, done={"P0": "ok"}),
                    graph={"P0": set(), "P1": {"P0"}, "P2": {"P9"}})
    text = "\n".join(plain(x) for x in home.empty_feed_lines(dash, 80))
    assert "up next" in text
    assert "P1" in text and "ready" in text
    assert "P2" in text and "waits on P9" in text
    assert text.index("P1") < text.index("P2")  # ready before blocked


def test_next_lines_when_the_ledger_is_built():
    dash = FakeDash(data.Snapshot(ok=True, done={"P0": "ok"}), graph={"P0": set()})
    assert "nothing left to run" in plain(home.next_lines(dash, 40)[0])


# -- needs you ------------------------------------------------------------
def test_need_rows_say_how_long_and_how_much_waits_behind_it():
    need = tl.Need(key="waiting:P1", kind="worker asks", phase="P1", question="which schema?",
                   since=NOW - 2460, blocks=7)
    head, question = plain(home.need_rows([need], 100, now=NOW)[0][0]).splitlines()
    assert "P1" in head and "worker asks" in head
    assert "waited 41m" in head and "blocks 7 phases" in head
    assert "which schema?" in question


def test_need_rows_stop_at_the_cap():
    needs = [tl.Need(key=f"k{i}", kind="worker asks", phase=f"P{i}", question="q", since=NOW)
             for i in range(9)]
    assert len(home.need_rows(needs, 80, now=NOW)) == home.MAX_NEEDS


def test_a_held_merge_queue_is_red_in_the_strip():
    need = tl.Need(key="integ:P1", kind=tl.NEED_LABEL["integ"], phase="P1", question="q",
                   since=None)
    assert COLOR[BAD] in home.need_rows([need], 80, now=NOW)[0][0]


def test_pass_detail_reads_as_sections():
    rec = SimpleNamespace(id="20260923T100000Z", status="done", started_at=NOW - 60,
                          ended_at=NOW, reasons=[{"text": "3 phases finished"}], summary="s",
                          saw="saw it", did="did it", left="your call", question="", answer="")
    text = plain(home.pass_detail(rec))
    for heading in ("why it ran", "what it saw", "what it did", "left for you"):
        assert heading in text
    assert "3 phases finished" in text and "your call" in text


# -- footer ---------------------------------------------------------------
def healthy(**kw):
    return data.Snapshot(
        ok=True, supervisor_pid=42, supervisor_alive=True, last_event_at=NOW - 12, **kw
    )


def test_footer_is_one_line_when_everything_is_fine():
    text = plain(home.footer_line(FakeDash(healthy()), 76, now=NOW))
    assert len(text.splitlines()) == 1
    assert "all clear" in text and "last event 12s" in text
    assert text.rstrip().endswith("5 disk →")


def test_footer_lists_only_what_is_wrong():
    """No wall of green dots: a healthy subsystem gets no words at all."""
    snap = healthy(integ_blocked="dash-W2", integ_blocked_kind="conflict")
    text = plain(home.footer_line(FakeDash(snap), 90, now=NOW))
    assert "all clear" not in text
    assert "merging stopped: dash-W2, a conflict with main" in text
    assert "ping" not in text and "not running" not in text


def test_footer_flags_dropped_telegrams():
    note = data.Notification(ts=NOW, kind="waiting", phase="P1", source="w", text="t",
                             delivered=False, error="curl: (6)")
    text = plain(home.footer_line(FakeDash(healthy(), notifications=[note]), 90, now=NOW))
    assert "1 ping(s) never reached your phone" in text


def test_footer_flags_a_dead_supervisor():
    snap = data.Snapshot(ok=True, last_event_at=NOW - 12)
    assert "swarm not running" in plain(home.footer_line(FakeDash(snap), 76, now=NOW))


def test_footer_says_what_a_drain_waits_for():
    snap = healthy(paused=True, drain={"waiting": ["1 worker"], "then": "sudo shutdown now"})
    text = plain(home.footer_line(FakeDash(snap), 120, now=NOW))
    assert "Draining: waiting for 1 worker, then stop, then: sudo shutdown now" in text
    assert "paused" not in text


def test_footer_paused_reads_as_paused_not_dead():
    snap = healthy(paused=True)
    text = plain(home.footer_line(FakeDash(snap), 76, now=NOW))
    assert "paused" in text and "not running" not in text


@pytest.mark.parametrize(
    "age,busy,expected",
    [(60, True, "all clear"), (3600, True, "quiet for"), (10800, True, "nothing has happened"),
     (10800, False, "all clear")],
)
def test_footer_escalates_a_stale_run_only_while_something_is_busy(age, busy, expected):
    """An idle or finished swarm is *supposed* to be quiet; warning about it is noise."""
    snap = data.Snapshot(
        ok=True, supervisor_pid=42, supervisor_alive=True, last_event_at=NOW - age,
        slots=[slot(0, "dash-W3", busy=busy)],
    )
    assert expected in plain(home.footer_line(FakeDash(snap), 90, now=NOW))


def test_footer_carries_the_disk_figure_it_is_given():
    """Home never measures anything; the disk tab's cached line is passed in."""
    text = plain(home.footer_line(FakeDash(healthy()), 90, disk="12.4G swarm · 694G free", now=NOW))
    assert "694G free" in text


def test_footer_counts_the_problems_it_cannot_fit():
    """A wrapped footer is not a footer, but a silently dropped failure is worse."""
    note = data.Notification(ts=NOW, kind="w", phase="P1", source="w", text="t",
                             delivered=False, error="x")
    snap = data.Snapshot(
        ok=True, paused=True, supervisor_alive=True, supervisor_pid=1,
        slots=[slot(0, "P1", busy=True)], last_event_at=NOW - 20000,
        integ_blocked="dash-W2", integ_blocked_kind="conflict",
    )
    text = plain(home.footer_line(FakeDash(snap, notifications=[note]), 70, now=NOW))
    assert len(text) == 70 and len(text.splitlines()) == 1
    assert "paused" in text and "+3 more" in text


def test_footer_before_any_run():
    assert "no run yet" in plain(home.footer_line(FakeDash(), 76, now=NOW))


# -- never raises ---------------------------------------------------------
@pytest.mark.parametrize("width", [20, 44, 120])
def test_every_builder_survives_an_empty_dash(width):
    dash = FakeDash()
    assert home.headline(dash, width) is not None
    assert home.worker_rows(dash, width) is not None
    assert home.chart_lines(dash, width, 5) is not None
    assert home.next_lines(dash, width) is not None
    assert home.feed_rows(tl.build_feed(), width) == []
    assert home.empty_feed_lines(dash, width)
    assert home.need_rows(tl.needs_you(dash), width) == []
    assert home.footer_line(dash, width, now=NOW) is not None


# -- the widget -----------------------------------------------------------
def busy_dash():
    """Two workers, three finishes and a chart's worth of log."""
    slots = [slot(0, "dash-W12"), slot(1, "dash-W13"), slot(2, None, busy=False)]
    snap = data.Snapshot(
        ok=True, slots=slots, done={f"dash-W{i}": "ok" for i in range(1, 12)},
        supervisor_pid=9, supervisor_alive=True, last_event_at=NOW - 12,
    )
    return FakeDash(
        snap,
        [(s, "busy" if s.busy else "idle", "", 40.0 if s.busy else None, None) for s in slots],
        graph={f"dash-W{i}": set() for i in range(1, 25)},
        history=[
            data.PhaseRun(phase=f"dash-W{11 - i}", status="ok", started_at=NOW - 900 * (i + 2),
                          ended_at=NOW - 900 * i, summary=f"did thing {i}")
            for i in range(4)
        ],
        events=[done_event(NOW - 8000 + i * 700, f"dash-W{i}") for i in range(1, 12)],
        finished={f"dash-W{i}": time.time() - 8000 + i * 700 for i in range(1, 12)},
    )


def drive(steps, size=(100, 30)):
    """Boot a bare app holding one :class:`Home` and run ``steps(app, home, pilot)``."""
    import asyncio

    from textual.app import App

    class Host(App):
        def __init__(self):
            super().__init__()
            self.opened = []

        def compose(self):
            yield home.Home(id="tab-home")

        def on_home_open_phase(self, event: home.Home.OpenPhase) -> None:
            self.opened.append((event.phase, event.slot))

    app = Host()

    async def run():
        async with app.run_test(size=size) as pilot:
            await steps(app, app.query_one(home.Home), pilot)

    asyncio.run(asyncio.wait_for(run(), timeout=30))
    return app


def test_home_paints_every_row_on_the_first_update():
    """A newly mounted pool must render now, not one 2s tick later."""
    seen = {}

    async def steps(app, screen, pilot):
        screen.update(busy_dash())
        await pilot.pause()
        seen["work"] = [r._swarm_text for r in app.query("#work-rows Row")]
        seen["feed"] = [r._swarm_text for r in app.query("#feed-rows Row")]

    drive(steps)
    assert len(seen["work"]) == 3 and all(seen["work"])
    assert len(seen["feed"]) == 4 and all(seen["feed"])


def test_home_writes_nothing_when_nothing_changed():
    """The whole premise of the screen: an unchanged tick costs no widget writes.

    The dash is deliberately clockless — no ``started_at``, no ``last_event_at``
    — so the only thing that could differ between the two ticks is the guard
    itself, not a second ticking over between them.
    """
    writes = []
    slots = [slot(0, "dash-W12", started_at=None), slot(1, None, busy=False, started_at=None)]
    dash = FakeDash(
        data.Snapshot(ok=True, slots=slots, supervisor_alive=True, supervisor_pid=1),
        [(s, "busy" if s.busy else "idle", "", None, None) for s in slots],
        graph={f"dash-W{i}": set() for i in range(1, 6)},
        history=[data.PhaseRun(phase="dash-W1", status="ok", ended_at=NOW, summary="x")],
        events=[done_event(NOW + i * 600, f"dash-W{i}") for i in range(1, 6)],
    )

    async def steps(app, screen, pilot):
        screen.update(dash)
        await pilot.pause()
        for node in app.query("Static"):
            original = node.update
            node.update = lambda text, _n=node: (writes.append(_n), original(text))[1]
        screen.update(dash)
        await pilot.pause()

    drive(steps)
    assert writes == []


def test_clicking_a_worker_row_opens_that_phase_and_moves_the_cursor():
    """A click has to land on the row under the pointer, slot and all."""

    async def steps(app, screen, pilot):
        screen.update(busy_dash())
        await pilot.pause()
        row = list(app.query("#work-rows Row"))[1]
        await pilot.click(row)
        await pilot.pause()

    app = drive(steps)
    assert app.opened == [("dash-W13", 1)]


def test_clicking_a_feed_row_opens_it_with_no_slot():
    """Nothing is running it any more, so there is no pane to jump to."""

    async def steps(app, screen, pilot):
        screen.update(busy_dash())
        await pilot.pause()
        await pilot.click(list(app.query("#feed-rows Row"))[2])
        await pilot.pause()

    app = drive(steps)
    assert app.opened == [("dash-W9", None)]


def test_cursor_walks_the_workers_then_the_feed():
    """``j``/``k`` cross the two lists, because the owner reads them as one."""
    seen = []

    async def steps(app, screen, pilot):
        screen.update(busy_dash())
        await pilot.pause()
        screen.focus()
        for _ in range(4):
            seen.append(screen.selected_phase())
            await pilot.press("j")
            await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    app = drive(steps)
    assert seen == ["dash-W12", "dash-W13", "dash-W11", "dash-W10"]
    assert app.opened == [("dash-W9", None)]


def test_a_free_slot_is_not_selectable():
    """There is nothing to open; the cursor must skip it entirely."""

    async def steps(app, screen, pilot):
        screen.update(busy_dash())
        await pilot.pause()
        await pilot.click(list(app.query("#work-rows Row"))[2])
        await pilot.pause()

    app = drive(steps)
    assert app.opened == []


def test_chart_is_not_redrawn_while_nothing_finishes(monkeypatch):
    """Resampling a week of finishes twice a second is the cost this avoids."""
    monkeypatch.setattr(home, "CHART_REDRAW_S", 1e12)  # no clock roll mid-test
    calls = []

    async def steps(app, screen, pilot):
        dash = busy_dash()
        original = home.chart_lines
        home.chart_lines = lambda *a, **kw: (calls.append(1), original(*a, **kw))[1]
        try:
            screen.update(dash)
            await pilot.pause()
            screen.update(dash)
            await pilot.pause()
            dash.finished = dict(dash.finished, **{"dash-W12": time.time()})
            screen.update(dash)
            await pilot.pause()
        finally:
            home.chart_lines = original

    drive(steps, size=(100, 40))  # tall enough to show the chart: a hidden one is not drawn
    assert len(calls) == 2


def test_home_stacks_below_the_narrow_threshold():
    got = {}

    async def steps(app, screen, pilot):
        screen.update(busy_dash())
        await pilot.pause()
        got["narrow"] = screen.has_class("-narrow")

    drive(steps, size=(80, 30))
    assert got["narrow"] is True
