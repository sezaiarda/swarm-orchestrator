"""Tests for the "needs you" drawer: the toast that fires and the list behind it.

Attention is a transition, not a state. The panel this replaced sat empty nine
hours out of ten and trained the eye to skip it, so the drawer's whole value is in
two promises, and most of what is tested here is one or the other:

* **a toast fires once per blocker, and never for what was already there** — a
  toast that repeats every tick is a panel again, and a dashboard that opens onto
  a blocked run and toasts all of it is a startup storm;
* **shut, it costs nothing** — the cockpit sits in tmux window 0 for the life of
  a run on a host this swarm has already OOM'd, so a closed drawer must not build
  rows or write a single widget.

Everything with logic in it is a pure function over the ``Dash``, so most tests
hand it a namespace with one awkward condition set. A handful drive the real
widget through a Textual pilot for the keys, the focus hand-back and the clicks.
Assertions go through Textual's markup parser, because worker-written text with a
stray ``[`` in it has to survive *that* parser.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from textual.content import Content

from swarm_orchestrator import opqueue
from swarm_orchestrator.tui import data, drawer
from swarm_orchestrator.tui.theme import BAD, COLOR, MUTED, WARN, YOU

NOW = 1_700_000_000.0


def plain(markup: str) -> str:
    return Content.from_markup(markup).plain


class FakeDash:
    """The three attributes the drawer reads, and nothing else."""

    def __init__(self, snapshot=None, notifications=(), sentinels=None) -> None:
        self.snapshot = snapshot if snapshot is not None else data.Snapshot()
        self.notifications = list(notifications)
        self.sentinels = sentinels or {}


def snap(*blockers, **kw) -> data.Snapshot:
    kw.setdefault("ok", True)
    kw.setdefault("supervisor_pid", 42)
    kw.setdefault("supervisor_alive", True)
    return data.Snapshot(blockers=list(blockers), **kw)


def blocker(phase="P1", kind="waiting", question="which schema?", **kw) -> data.Blocker:
    return data.Blocker(phase=phase, kind=kind, question=question, **kw)


def note(phase="P1", text="ping", delivered=True, ts=NOW - 60, **kw) -> data.Notification:
    return data.Notification(
        ts=ts, kind=kw.get("kind", "waiting"), phase=phase, source=kw.get("source", ""),
        text=text, delivered=delivered, error=kw.get("error", ""),
    )


def slot(id_: int, phase: str | None, busy: bool = True) -> data.SlotView:
    return data.SlotView(id=id_, busy=busy, phase=phase, pane_id=f"%{id_}", branch=None,
                         worktree=None)


# -- text primitives ----------------------------------------------------------
def test_clip_is_one_line_and_ellipsised():
    assert drawer.clip("a\n  b   c", 20) == "a b c"
    assert drawer.clip("x" * 30, 10) == "x" * 9 + "…"
    assert drawer.clip(None, 10) == "" and drawer.clip("abc", 1) == ""


def test_wrap_caps_the_prose_and_marks_the_cut():
    out = drawer.wrap("word " * 40, 20, lines=2)
    assert len(out) == 2 and out[-1].endswith("…")
    assert all(len(line) <= 20 for line in out)
    assert drawer.wrap("short", 20) == ["short"]
    assert drawer.wrap("   ", 20) == []


def test_set_text_skips_an_unchanged_write():
    """Static.update is a full relayout; the 2s tick must not pay it for nothing."""

    class Spy:
        writes = 0

        def update(self, text):
            self.writes += 1

    spy = Spy()
    for _ in range(3):
        drawer.set_text(spy, "same")
    drawer.set_text(spy, "different")
    assert spy.writes == 2


def test_mark_uses_the_gutter_only():
    assert drawer.mark("  row") == "▸ row"
    assert drawer.mark("no gutter") == "no gutter"


# -- alerts ---------------------------------------------------------------------
def test_a_swarm_that_never_ran_raises_nothing():
    assert drawer.alerts(FakeDash()) == []
    assert drawer.alerts(SimpleNamespace()) == []


def test_a_held_merge_queue_is_an_error_a_question_is_a_warning():
    alerts = drawer.alerts(FakeDash(snap(blocker("P1"), blocker("P2", kind="integ"))))
    by_phase = {a.phase: a for a in alerts}
    assert by_phase["P1"].severity == "warning"
    assert by_phase["P2"].severity == "error"
    assert by_phase["P1"].title == "needs you: P1"
    assert by_phase["P1"].body == f"waiting · which schema?\n{drawer.TOAST_HINT}"


def test_an_alert_falls_back_to_the_ping_then_the_detail():
    dash = FakeDash(
        snap(blocker("P1", question=""), blocker("P2", question="", detail="off-grid"),
             blocker("P3", question="")),
        notifications=[note("P1", "asked over telegram")],
    )
    bodies = {a.phase: a.body for a in drawer.alerts(dash)}
    assert "asked over telegram" in bodies["P1"]
    assert "off-grid" in bodies["P2"]
    assert "no question text was captured" in bodies["P3"]


def test_a_failed_phase_and_a_dead_supervisor_are_alerts():
    down = drawer.alerts(FakeDash(snap(done={"P3": "fail", "P2": "ok"}, supervisor_alive=False)))
    assert [(a.key, a.severity) for a in down] == [("fail:P3", "error"), ("down:42", "error")]
    assert "`swarm why P3`" in down[0].body


def test_a_finished_run_without_a_supervisor_is_not_an_alert():
    assert drawer.alerts(FakeDash(snap(finished=True, supervisor_alive=False))) == []


def test_the_alert_key_ignores_the_ticking_clock_and_rewording():
    first = blocker("P1", question="which schema?", since=NOW - 60)
    later = blocker("P1", question="which schema, v1 or v2?", since=NOW - 60)
    retry = blocker("P1", since=NOW)
    assert drawer.alert_key(first) == drawer.alert_key(later)
    assert drawer.alert_key(first) != drawer.alert_key(retry)


# -- the toaster -----------------------------------------------------------------
def test_the_first_real_read_primes_and_toasts_nothing():
    """A dashboard opened on a blocked run must not toast the whole backlog."""
    toaster = drawer.Toaster()
    assert toaster.poll(FakeDash()) == []  # mount-time empty snapshot: not a read
    assert toaster.poll(FakeDash(snap(blocker("P1")))) == []  # the backlog
    fresh = toaster.poll(FakeDash(snap(blocker("P1"), blocker("P2"))))
    assert [a.phase for a in fresh] == ["P2"]


def test_a_blocker_toasts_exactly_once():
    toaster = drawer.Toaster()
    toaster.poll(FakeDash(snap()))
    assert len(toaster.poll(FakeDash(snap(blocker("P1"))))) == 1
    for _ in range(3):
        assert toaster.poll(FakeDash(snap(blocker("P1", question="reworded")))) == []


def test_a_blocker_that_flickers_out_and_back_is_not_retoasted():
    toaster = drawer.Toaster()
    toaster.poll(FakeDash(snap()))
    toaster.poll(FakeDash(snap(blocker("P1"))))
    toaster.poll(FakeDash(snap()))
    assert toaster.poll(FakeDash(snap(blocker("P1")))) == []


def test_a_relaunched_phase_asking_again_is_a_new_alert():
    toaster = drawer.Toaster()
    toaster.poll(FakeDash(snap()))
    toaster.poll(FakeDash(snap(blocker("P1", since=NOW - 600))))
    assert len(toaster.poll(FakeDash(snap(blocker("P1", since=NOW))))) == 1


def test_a_burst_is_capped_and_never_parsed_as_markup():
    calls = []
    app = SimpleNamespace(notify=lambda body, **kw: calls.append((body, kw)))
    burst = [drawer.Alert(f"k{i}", "warning", f"t{i}", "[red]worker text") for i in range(6)]
    drawer.push_toasts(app, burst)
    assert len(calls) == drawer.MAX_TOASTS + 1
    assert calls[-1][0].startswith("+2 more")
    assert all(kw["markup"] is False for _, kw in calls)
    assert calls[0][1]["title"] == "t0" and calls[0][1]["timeout"] == drawer.TOAST_TIMEOUT_S


# -- blocker rows ------------------------------------------------------------------
def test_no_blockers_no_rows():
    assert drawer.blocker_rows(FakeDash(snap())) == []


def test_the_age_comes_from_the_launch_then_the_sentinel_then_the_ping():
    sentinels = {"P2": data.Sentinel("P2", "needs-owner", "check auth", mtime=NOW - 3600)}
    dash = FakeDash(
        snap(blocker("P1", since=NOW - 120), blocker("P2", kind="needs-owner", question=""),
             blocker("P3", question="")),
        notifications=[note("P3", "which port?", ts=NOW - 900)],
        sentinels=sentinels,
    )
    rows = [plain(r.text) for r in drawer.blocker_rows(dash, now=NOW)]
    assert data.fmt_ago(NOW - 120, NOW) in rows[0]
    assert data.fmt_ago(NOW - 3600, NOW) in rows[1] and "check auth" in rows[1]
    assert data.fmt_ago(NOW - 900, NOW) in rows[2] and "which port?" in rows[2]


def test_on_a_narrow_drawer_the_name_yields_before_the_age():
    long = "source-provider-Pkg-with-a-very-long-name"
    width = drawer.WIDTH - 4  # the drawer's real content width
    rows = drawer.blocker_rows(FakeDash(snap(blocker(long, since=NOW - 7200))), width, now=NOW)
    head = plain(rows[0].text).splitlines()[0]
    assert head.endswith(f"waiting · {data.fmt_ago(NOW - 7200, NOW)}")
    assert "…" in head and long not in head
    assert len(head) <= width


def test_worker_markup_is_shown_not_obeyed():
    rows = drawer.blocker_rows(FakeDash(snap(blocker("P[1]", question="use [bold]x[/bold]?"))))
    text = plain(rows[0].text)
    assert "P[1]" in text and "use [bold]x[/bold]?" in text


def test_rows_stop_at_the_cap():
    many = snap(*(blocker(f"P{i}") for i in range(drawer.MAX_BLOCKERS + 3)))
    assert len(drawer.blocker_rows(FakeDash(many))) == drawer.MAX_BLOCKERS


def test_a_row_carries_the_slot_only_while_a_worker_holds_the_phase():
    s = snap(blocker("P1"), blocker("P2"), slots=[slot(0, "P1"), slot(1, "P2", busy=False)])
    rows = drawer.blocker_rows(FakeDash(s))
    assert [(r.phase, r.slot) for r in rows] == [("P1", 0), ("P2", None)]


def test_a_held_merge_queue_row_is_red():
    rows = drawer.blocker_rows(FakeDash(snap(blocker("P1", kind="integ"))))
    assert rows[0].text.startswith(f"  [{COLOR[BAD]}]✗[/]")


def test_a_question_row_is_the_needs_you_colour_not_a_warning():
    """A question needs a person; it is not something going wrong."""
    rows = drawer.blocker_rows(FakeDash(snap(blocker("P1", kind="waiting"))))
    assert rows[0].text.startswith(f"  [{COLOR[YOU]}]◆[/]")
    assert COLOR[YOU] != COLOR[WARN]


# -- operator rows -----------------------------------------------------------------
def item(phase, state=opqueue.QUEUED, **kw) -> opqueue.Item:
    return opqueue.Item(phase=phase, state=state, queued_at=kw.pop("queued_at", NOW - 300), **kw)


def test_only_owed_hand_offs_are_listed():
    queue = [item("P1"), item("P2", opqueue.DONE), item("P3", opqueue.RUNNING),
             item("P4", opqueue.ABANDONED)]
    dash = FakeDash(snap(operator=queue))
    assert [r.phase for r in drawer.operator_rows(dash, now=NOW)] == ["P1", "P3"]
    assert plain(drawer.operator_head(dash)) == "operator queue (2)"


def test_an_operator_row_shows_its_state_triage_and_age():
    dash = FakeDash(snap(operator=[item("P1", triage={"when": "now"})]))
    text = plain(drawer.operator_rows(dash, now=NOW)[0].text)
    assert "queued/now" in text and data.fmt_ago(NOW - 300, NOW) in text


def test_a_drained_queue_has_no_heading():
    assert drawer.operator_head(FakeDash(snap(operator=[item("P1", opqueue.DONE)]))) == ""
    assert drawer.operator_head(FakeDash()) == ""


# -- pings -----------------------------------------------------------------------
def test_pings_are_newest_first_and_capped():
    notes = [note(f"P{i}", f"msg {i}", ts=NOW + i) for i in range(drawer.MAX_NOTES + 2)]
    rows = drawer.notification_rows(FakeDash(snap(), notifications=notes))
    assert len(rows) == drawer.MAX_NOTES
    assert [r.phase for r in rows][:2] == [f"P{drawer.MAX_NOTES + 1}", f"P{drawer.MAX_NOTES}"]


def test_a_ping_that_never_landed_is_red():
    rows = drawer.notification_rows(FakeDash(snap(), notifications=[note(delivered=False)]))
    assert "✗" in plain(rows[0].text) and COLOR[BAD] in rows[0].text


def test_a_ping_with_no_phase_names_its_source():
    rows = drawer.notification_rows(
        FakeDash(snap(), notifications=[note(phase=None, text="run over", source="supervisor")])
    )
    assert "supervisor · run over" in plain(rows[0].text)
    assert rows[0].phase is None


def test_the_pings_heading_counts_the_drops():
    ok = FakeDash(snap(), notifications=[note()])
    bad = FakeDash(snap(), notifications=[note(), note(delivered=False), note(delivered=False)])
    assert drawer.pings_head(ok) == f"[{COLOR[MUTED]}]pings[/]"
    assert plain(drawer.pings_head(bad)) == "pings · 2 NOT DELIVERED"
    assert COLOR[BAD] in drawer.pings_head(bad)


# -- the head line -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("snapshot", "state", "count"),
    [
        (snap(), MUTED, 0),
        (snap(blocker("P1")), YOU, 1),
        (snap(blocker("P1"), blocker("P2", kind="integ")), BAD, 2),
        (snap(supervisor_alive=False), BAD, 0),
        (snap(done={"P1": "fail"}), MUTED, 0),  # history is not an emergency
        (data.Snapshot(), MUTED, 0),  # nothing read yet: no supervisor is not news
    ],
)
def test_head_line(snapshot, state, count):
    got_state, markup = drawer.head_line(FakeDash(snapshot))
    assert got_state == state
    assert plain(markup) == f"needs you ({count})"


# -- the widget ----------------------------------------------------------------------
def busy_dash() -> FakeDash:
    return FakeDash(
        snap(blocker("P1", since=NOW), blocker("P2", kind="parked", since=NOW),
             slots=[slot(0, "P1"), slot(1, None, busy=False)]),
        notifications=[note("P1", "which schema?")],
    )


def drive(steps, size=(120, 30)):
    """Boot a bare app: something focusable, and the drawer beside it."""
    from textual.app import App
    from textual.widgets import Static

    class Pad(Static):
        can_focus = True

    class Host(App):
        BINDINGS = [drawer.APP_BINDING]

        def __init__(self):
            super().__init__()
            self.opened = []
            self.toasts = []

        def compose(self):
            yield Pad("main", id="pad")
            yield drawer.Drawer(id="drawer")

        def on_mount(self):
            self.query_one("#pad").focus()

        def action_toggle_drawer(self):
            self.query_one(drawer.Drawer).toggle()

        def notify(self, message, **kw):
            self.toasts.append((message, kw))

        def on_open_phase(self, event: drawer.OpenPhase) -> None:
            self.opened.append((event.phase, event.slot))

    app = Host()

    async def run():
        async with app.run_test(size=size) as pilot:
            await steps(app, app.query_one(drawer.Drawer), pilot)

    asyncio.run(asyncio.wait_for(run(), timeout=30))
    return app


def test_shut_it_toasts_but_writes_no_rows():
    got = {}

    async def steps(app, box, pilot):
        box.update(FakeDash(snap()))
        box.update(busy_dash())
        await pilot.pause()
        got["rows"] = [getattr(r, "_swarm_text", None) for r in app.query(drawer.DrawerRow)]
        got["open"] = box.is_open

    app = drive(steps)
    assert got["open"] is False
    assert got["rows"] == [None] * len(got["rows"])
    assert [kw["title"] for _, kw in app.toasts] == ["needs you: P1", "needs you: P2"]


def test_n_opens_it_painted_and_escape_hands_focus_back():
    got = {}

    async def steps(app, box, pilot):
        box.update(busy_dash())
        await pilot.press("n")
        await pilot.pause()
        got["open"] = box.is_open
        got["focused"] = app.focused is box
        got["head"] = plain(box.query_one("#drawer-head").content)
        got["first"] = plain(box.query_one("#drawer-b0")._swarm_text)
        got["pings"] = plain(box.query_one("#drawer-pings").content)
        await pilot.press("escape")
        await pilot.pause()
        got["closed"] = not box.is_open
        got["back"] = app.focused.id

    drive(steps)
    assert got["open"] and got["focused"]
    assert got["head"] == "needs you (2)"
    assert got["first"].startswith("▸ ◆ P1")
    assert got["pings"] == "pings"
    assert got["closed"] and got["back"] == "pad"


def test_the_cursor_walks_blockers_then_pings_and_enter_opens_with_the_slot():
    got = []

    async def steps(app, box, pilot):
        box.update(busy_dash())
        await pilot.press("n")
        for _ in range(3):
            got.append(box.selected_phase())
            await pilot.press("j")
        await pilot.press("k", "k", "k")
        got.append(box.selected_phase())
        await pilot.press("enter")
        await pilot.pause()

    app = drive(steps)
    assert got == ["P1", "P2", "P1", "P1"]  # blocker, blocker, ping; clamped at top
    assert app.opened == [("P1", 0)]


def test_shut_it_leaves_the_shared_keys_alone():
    """j/k/enter belong to the tabs until the drawer is actually up."""
    got = {}

    async def steps(app, box, pilot):
        got["shut"] = [box.check_action(a, ()) for a in ("cursor", "select", "close", "toggle")]
        box.open()
        await pilot.pause()
        got["open"] = [box.check_action(a, ()) for a in ("cursor", "select", "close")]

    drive(steps)
    assert got["shut"] == [None, None, None, True]
    assert got["open"] == [True, True, True]


def test_an_empty_drawer_says_so_in_one_line():
    got = {}

    async def steps(app, box, pilot):
        box.update(FakeDash(snap()))
        box.open()
        await pilot.pause()
        got["empty"] = plain(box.query_one("#drawer-empty").content)
        got["visible"] = [r.id for r in app.query(drawer.DrawerRow) if not r.has_class("-off")]

    drive(steps)
    assert got["empty"].strip() == drawer.EMPTY
    assert got["visible"] == []


def test_clicking_a_row_selects_it_and_opens_it():
    got = {}

    async def steps(app, box, pilot):
        box.update(busy_dash())
        box.open()
        await pilot.pause()
        await pilot.click("#drawer-b1")
        await pilot.pause()
        got["selected"] = box.selected_phase()

    app = drive(steps)
    assert got["selected"] == "P2"
    assert app.opened == [("P2", None)]
