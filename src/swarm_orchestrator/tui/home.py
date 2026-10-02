"""The home screen: watch it run, and see what it decided.

The previous home was five bordered panels, three of them a box around a single
line of text. A border is a promise that what is inside is worth stopping for,
so this screen spends them on content that moves or matters: ``working now``,
``phases done`` (``what's next`` until there is something to chart), the
**feed**, and — only while something waits on the owner — **needs you**.

The feed is the reason for this version. What each phase did and the decisions
its worker took are the most useful thing the dashboard has, so home now leads
with it: phases
finishing with their recap, the calls workers made on their own, the owner's own
answers (highlighted), operator outcomes and what the Overseer did or left for
them, newest first. Selecting a row opens that phase's History detail, or the
pass/job record for rows that have no phase. The data is
:mod:`~swarm_orchestrator.tui.timeline`; this module only paints it.

The screen is a grid. The work is on the left — phase books, working now and
phases done, the feed — and the run's surroundings on the right, each in a box of its own:
**usage** with its chart (:mod:`.usagebox`), **alerts & notifications**
(:mod:`.alerts`: what needs you, what is wrong now, every ping sent or held) and
the kept **shells**. ``needs you`` leads the alerts box, with how long each thing
has waited and how many phases sit behind it; when the terminal is too narrow for
two columns the side boxes move under the feed and ``needs you`` returns as a
strip at the top, so a waiting question is never below the fold.

Two structural rules hold the rest together:

* **Rows are widgets, not lines of a Static.** A click has to land on the row
  under the pointer, and turning a click's y-offset back into a row index is a
  guess that breaks the first time a worker note wraps. Each row carries its own
  kind and key and posts :class:`Home.OpenPhase` / :class:`Home.OpenText`.
* **A tick costs nothing.** :meth:`Home.update` runs every 2s for the life of the
  run on a small host where a "freeze" can be the OOM killer, so it does no
  I/O at all — it renders what ``Dash`` already holds, the feed is re-sorted
  only when one of its sources was replaced, the chart is recomputed only when
  the log actually grew, and every assignment goes through :func:`set_text` so a
  tick where nothing changed writes nothing.

Free text (recaps, notes, pane tails) is written by workers and routinely
contains ``[``; every one of those strings goes through ``rich.markup.escape``
before it is painted, or a stray bracket eats the line.
"""

from __future__ import annotations

import time

from rich.markup import escape
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static

from . import alerts as alerts_mod
from . import books as books_mod
from . import probes
from . import timeline as tl
from . import resourcebox
from . import usagebox
from .campaign import active, overall, summarise
from .charts import area, axis, axis_time, hold_last, meter, time_grid
from .data import (
    CONTEXT_BUDGET,
    Series,
    eta_runs_of,
    fmt_clock,
    fmt_coarse,
    fmt_duration,
    fmt_stamp,
    kept_rows,
    open_drops,
    phase_eta,
)
from .theme import (
    BAD,
    BLOCKED,
    BRIGHT,
    COLOR,
    GLYPH,
    IDLE,
    INFO,
    MUTED,
    OK,
    OPERATOR,
    OVERSEER,
    READY,
    SOFT,
    WARN,
    YOU,
    Body,
    Panel,
    bar,
    meter_state,
    paint,
    rows,
    section,
    token,
)

try:  # the disk tab is its own module and may not be there yet
    from . import disk as _disk
except Exception:  # noqa: BLE001 - home renders with or without a disk figure
    _disk = None

#: Rows the feed card shows. Its data keeps :data:`timeline.FEED_MAX`; the card
#: scrolls, and the History tab holds the rest.
FEED_ROWS = 40

#: Things waiting on the owner listed on home before the rest become "+n more".
MAX_NEEDS = 4

#: Book rows one wheel notch moves.
WHEEL_ROWS = 2

#: Below this many columns the two-up row stacks instead of squeezing. A 40-cell
#: panel cannot hold a phase name, an elapsed and a meter without lying.
NARROW_COLS = 100
#: The narrowest working-now box that still shows a 20-column phase name.
WORK_COLS = 56

#: A terminal shorter than this drops the chart panel and compacts the headline:
#: at 80x24 and 100x30 the chart pushed the feed — the part most worth
#: seeing first — off the bottom of the screen.
SHORT_ROWS = 36

#: Left gutter for the borderless sections, so they line up under the panels.
PAD = "  "

#: How often the disk line may be re-asked. With no scan yet ``disk.summary``
#: falls back to one statvfs, and a repaint tick must stay free.
DISK_EVERY_S = 30.0

#: The footer's right-aligned pointer at the tab that holds the detail.
_HINT = "5 disk →"

#: Box-drawing leads a claude pane's frame; a line starting with one is chrome.
_BOX = set("│╭╰╮╯─═┌┐└┘├┤┬┴┼")

#: Bullets/spinners claude prefixes its own status lines with.
_LEAD = "✻✽✢✶✳✱·*●⏺⎿→↑↓ \t"


# -- text primitives ------------------------------------------------------
def clip(text: str, width: int) -> str:
    """One line, at most ``width`` cells, ellipsised when cut.

    Collapses whitespace first: worker text arrives with newlines and runs of
    spaces in it, and a single embedded ``\\n`` would silently break a row's
    alignment for every row beneath it.
    """
    flat = " ".join((text or "").split())
    if width <= 1:
        return ""
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def activity(tail: str) -> str:
    """What a worker's pane says it is doing right now, or ``""``.

    A running worker has no recap yet — recaps are written at completion — so the
    only live account of what it is doing is the last real line of its own TUI.
    Scanning bottom-up past the frame and the input box lands on either the
    spinner (``✻ Wrangling… (23s · esc to interrupt)``) or the last tool line,
    both of which are exactly the sentence a glancing owner wants. The
    parenthetical timer is dropped: it duplicates the elapsed column and changes
    every tick, which makes a still panel look busy.
    """
    for raw in reversed((tail or "").splitlines()):
        line = raw.strip()
        if not line or line[0] in _BOX or line.startswith("⏵"):
            continue
        if probes.parse_context(line):
            continue  # the status bar's `420k/1.0M`, already shown as the meter
        line = line.lstrip(_LEAD).strip()
        if "esc to interrupt" in line:
            line = line.split("(")[0].strip()
        if len(line) >= 4:
            return line
    return ""


def set_text(widget, text: str) -> None:
    """Update a ``Static`` only when its text actually changed.

    ``Static.update`` re-parses the markup and calls ``refresh(layout=True)``
    unconditionally, so repainting an unchanged row on every tick is a full
    layout pass a second for the life of the run. Deliberately a local copy of
    the guard in ``tables``: home is the tab that is open all day, and it must
    not go down with a module it otherwise has no reason to import.
    """
    if getattr(widget, "_swarm_text", None) == text:
        return
    widget._swarm_text = text
    widget.update(text)


# -- headline -------------------------------------------------------------
def at_work(dash) -> set[str]:
    """The phases a worker is at work on: in a busy slot, or parked and at work on
    the owner's answer in a window of its own (``Dash.working_parked``)."""
    return ({s.phase for s in dash.snapshot.slots if s.busy and s.phase}
            | {w.phase for w in getattr(dash, "working_parked", None) or ()})


def headline(dash, width: int = 76, compact: bool = False) -> str:
    """The page title, one answer: how far along the open phase books are, when
    all of them are done, and what is moving.

    Unbordered on purpose. A box around the title of the page is a box for its
    own sake, and this screen only spends borders on things that move. The
    books themselves are the panel under it (:func:`book_rows`). What the
    forecast was made on is in a phase book's detail and under ``?``; the caps
    are in the usage box (:mod:`.usagebox`); what waits on the owner is ``g``
    (to-dos) and ``n`` (questions). ``compact`` is kept for callers; the
    headline is three lines at every size.
    """
    snap = dash.snapshot
    busy = at_work(dash)
    excluded = set(getattr(dash.cfg, "exclude", None) or [])
    camps = [
        c for c in summarise(dash.graph or {}, snap.landed, busy, excluded,
                             getattr(dash, "ticked", None), getattr(dash, "deferred", None))
        if c.live_total > c.skipped  # a campaign of nothing but skips is noise
    ]
    if not camps:
        return PAD + paint("no phases scheduled — check the ledger", MUTED)
    books = [c for c in camps if not c.complete] or camps
    cur = overall(books)
    inner = max(30, width - len(PAD))
    title = f"phase books ({len(books)} open)" if len(books) > 1 else books[0].name
    count = f"{cur.built} / {cur.live_total} phases · {cur.pct:.0f}%"
    gap = max(2, inner - len(title) - len(count))
    first = f"{PAD}[b]{escape(title)}[/b]{' ' * gap}{paint(count, MUTED)}"

    now = time.time()
    left = finish_text(dash, now)
    fill = OK if cur.complete else (INFO if cur.running or cur.ready else MUTED)
    left = clip(left, max(10, inner - 12))
    second = (
        f"{PAD}{paint(bar(cur.built, max(1, cur.live_total), max(10, inner - len(left) - 2)), fill)}"
        f"  [b]{escape(left)}[/b]"
    )

    counts = []
    if cur.running:
        counts.append(paint(f"{len(cur.running)} running", INFO))
    if cur.ready:
        counts.append(paint(f"{len(cur.ready)} ready", READY))
    if cur.failed:
        counts.append(paint(f"{cur.failed} failed", BAD))
    if not counts:
        counts.append(
            paint("complete", OK) if cur.complete else paint("nothing can start yet", MUTED)
        )
    if not snap.ok:
        counts.append(paint("run not started — `swarm up`", MUTED))
    if getattr(dash, "big_picture", ""):
        counts.append(paint(escape(dash.big_picture), MUTED))
    return rows(first, second, PAD + " · ".join(counts))


def finish_text(dash, now: float) -> str:
    """``all done ~Mon 16:20 (by Wed 09:00)``, or why there is no such time yet."""
    fc = getattr(dash, "forecast", None)
    if fc is not None:
        return books_mod.finish_phrase(fc, now)
    failed = getattr(getattr(dash, "eta", None), "error", "")
    return f"no forecast: {failed}" if failed else "working out when…"


def basis_text(dash, now: float | None = None) -> str:
    """What the forecast was made on, for the detail views."""
    fc = getattr(dash, "forecast", None)
    if fc is None:
        return ""
    return f"forecast basis: {books_mod.basis_line(fc, time.time() if now is None else now)}"


#: Phase-book rows the box shows before it scrolls, on a short terminal.
SHORT_BOOKS = 4
#: ... and on a tall one; a mid-size one lists :data:`MID_BOOKS`. The box has a
#: fixed height so working now, above it, is never pushed off the screen.
TALL_BOOKS = 10
#: The rows a slot beyond the usual four costs the box.
BASE_SLOTS = 4
#: Working now shows this many slots; past it the box scrolls like the books.
WORK_VIS = 4
MIN_BOOKS = 3


def book_rows(dash, width: int = 76, selected: str | None = None,
              limit: int | None = None, top: int = 0) -> list[tuple]:
    """``(text, None, name)`` per open phase book, soonest finish first.

    ``limit`` rows from index ``top``: the box shows a window and scrolls.
    """
    fc = getattr(dash, "forecast", None)
    if fc is None:
        return [(PAD + paint("working out when each phase book finishes…", MUTED), None, None)]
    now = time.time()
    books = list(fc.books)[top:top + limit] if limit else list(fc.books)
    out = []
    for b in books:
        text = books_mod.book_line(b, fc, now, width - 2)
        if len(text) > width - 2:  # cut, never re-spaced: the columns must stay aligned
            text = text[: max(1, width - 3)] + "…"
        tone = INFO if b.running else (READY if b.ready else MUTED)
        mark = "▸ " if b.name == selected else "  "
        out.append((mark + paint(escape(text), tone), None, b.name))
    if not out:
        out.append((PAD + paint("every phase book is done", OK), None, None))
    return out


def book_detail(dash, name: str) -> str:
    """The detail of one phase book, for :class:`Home.OpenText`."""
    fc = getattr(dash, "forecast", None)
    book = next((b for b in (fc.books if fc else ()) if b.name == name), None)
    if book is None:
        return f"{name}: no forecast yet"
    what = (getattr(dash, "campaign_what", None) or {}).get(name, "")
    now = time.time()
    body = books_mod.detail(book, fc, now) + "\n\n" + basis_text(dash, now)
    return f"{what}\n\n{body}" if what else body


#: Cells for a row's ETA: ``~1h 20m`` or ``+1h 20m``.
ETA_W = 8


def eta_cell(history, elapsed_s: float | None) -> str:
    """Time left in a running phase against the typical one, or how far past it.

    Past the median is flagged, not clamped at zero: a phase an hour over is the
    first visible sign of a stuck worker or of a phase that should have been two.
    """
    seconds, over = phase_eta(history, elapsed_s)
    if seconds is None:
        return paint(f"{'—':>{ETA_W}}", MUTED)
    text = f"{'+' if over else '~'}{fmt_coarse(seconds)}"
    return paint(f"{text:>{ETA_W}}", WARN if over else MUTED)


def context_cells(m, pct: float | None) -> tuple[str, str]:
    """``(gauge, label)`` for a worker's context.

    Measured against the ~300K budget rather than the window when the meters tap
    reported tokens: on a 1M window 300K is only 30%, a figure that looks fine
    while every turn re-sends a conversation the phase should have split.
    """
    tokens = getattr(m, "context_tokens", None)
    if tokens:
        share = tokens / CONTEXT_BUDGET
        cstate = BAD if share >= 1 else (WARN if share >= 0.8 else OK)
        return paint(meter(min(1.0, share), 4), cstate), paint(f"{tokens / 1000:>3.0f}k", cstate)
    if pct is None:
        return paint("░" * 4, MUTED), paint("  —", MUTED)
    cstate = meter_state(pct, warn=70, bad=90)
    return paint(meter(pct / 100.0, 4), cstate), paint(f"{pct:>3.0f}%", cstate)


def worker_note(dash, slot, waiting_for: str) -> str:
    """The one sentence to show under a worker row, best source first.

    ``waiting_for`` wins because a blocked worker is the thing the owner must
    know; the live pane beats a recap because a recap only exists once the phase
    is over; the sentinel note is the last resort for a slot still holding a
    finished phase.
    """
    if waiting_for:
        return f"waiting: {waiting_for}"
    live = activity((dash.tails or {}).get(slot.pane_id or "", ""))
    if live:
        return live
    recap = (dash.recaps or {}).get(slot.phase or "")
    if recap is not None and recap.summary:
        return recap.summary
    sentinel = (dash.sentinels or {}).get(slot.phase or "")
    if sentinel is not None and sentinel.note:
        return sentinel.note
    return ""


def worker_rows(dash, width: int = 44, selected: int | None = None) -> list[tuple]:
    """``(markup, phase, slot_id)`` per slot — the only live panel on the screen.

    A slot the state calls busy while its pane holds no claude is tagged ``gone``
    and goes red: that stall is invisible in ``state.json`` and is why this panel
    exists. A slot whose phase is in ``blockers`` is repainted as waiting, so the
    row and the blocker drawer can never disagree.

    After the slots comes a row per parked worker at work on the owner's answer
    (``Dash.working_parked``): it runs in a tmux window of its own, which the row
    names, and holds no slot, so its ``slot_id`` is ``None`` and there is nothing
    to select or jump to.

    Returns rows rather than one blob because the caller mounts a widget per row;
    a click has to land on the row it hit.
    """
    snap = dash.snapshot
    slots = list(dash.slot_rows())
    parked = list(getattr(dash, "working_parked", None) or ())
    if not slots and not parked:
        return [(paint(snap.reason or "no workers yet — `swarm up` starts them", MUTED), None, None)]

    blocked = {b.phase: b for b in snap.blockers}
    # The first line is 25 columns plus the eta cell around the phase name
    # (mark, id, elapsed, gauge, tokens): the name takes what is left, so a
    # worker is never wrapped onto a line of its own.
    phase_w = max(8, min(20, width - 25 - ETA_W - 2))
    meters = getattr(dash, "meters", None) or {}
    out = []
    for row in slots:
        slot, status, waiting_for, ctx = row[0], row[1], row[2], row[3]
        mark = "▸ " if selected is not None and selected == slot.id else "  "
        if not slot.busy or not slot.phase:
            # Every slot is always on screen, busy or free: seeing them all is
            # the point of this box.
            out.append((mark + paint(f"{slot.id:<2}  {GLYPH[IDLE]} free", IDLE), None, slot.id))
            continue
        if slot.phase in blocked:
            status = "waiting"
            # A blocked worker's pane is frozen on whatever it printed before it
            # asked, so its question is the only honest note for this row.
            waiting_for = waiting_for or blocked[slot.phase].question
        elif status == "unknown":
            # No `claude agents` on this host: state says it is running and
            # nothing contradicts that, so do not grey out a live worker.
            status = "busy"
        state = token(status)
        gauge, pct = context_cells(meters.get(slot.phase), ctx)
        tag = ""
        if slot.retiring:
            tag = paint(" retiring", MUTED)
        elif status not in ("busy", "running"):
            tag = " " + paint(status, state)
        line = (
            f"{mark}[{COLOR[state]}]{slot.id:<2}[/]  "
            f"{clip(escape(slot.phase), phase_w):<{phase_w}} "
            f"{fmt_duration(slot.elapsed_s):>6} {eta_cell(eta_runs_of(dash), slot.elapsed_s)}"
            f"  {gauge} {pct}{tag}"
        )
        note = worker_note(dash, slot, waiting_for)
        if note:
            line += "\n" + paint(f"      {clip(escape(note), max(10, width - 6))}", MUTED)
        out.append((line, slot.phase, slot.id))
    # A parked worker whose session is gone, by the dashboard's own probe
    # (``Dash.parked_gone``): it is tagged ``gone`` and goes red like a slot.
    lost = getattr(dash, "parked_gone", None) or {}
    for worker in parked:
        gone = worker.phase in lost
        gauge, pct = context_cells(meters.get(worker.phase), None)
        line = (
            f"  [{COLOR[BAD if gone else INFO]}]{'—':<2}[/]  "
            f"{clip(escape(worker.phase), phase_w):<{phase_w}} "
            f"{fmt_duration(worker.elapsed_s):>6} {eta_cell(eta_runs_of(dash), worker.elapsed_s)}"
            f"  {gauge} {pct}" + (" " + paint("gone", BAD) if gone else "")
        )
        # The window leads the note, so a narrow box clips the words and not the name.
        note = f"tmux window {worker.window} · " + (
            "its session is gone, it does no work" if gone else "works on your answer")
        line += "\n" + paint(f"      {clip(escape(note), max(10, width - 6))}",
                             BAD if gone else MUTED)
        out.append((line, worker.phase, None))
    return out


def work_hint(rows: list[tuple], top: int) -> str:
    """What working now has scrolled out of sight, for the box's border.

    A parked worker at work comes after the slots, so with four slots it is
    below the fold: it is named here, where the owner looks for what is running.
    """
    below = rows[top + WORK_VIS:]
    more = f"↓ {len(below)} more" if below else ""
    apart = [phase for _, phase, slot_id in below if phase and slot_id is None]
    if apart:
        more += f" ({', '.join(apart)} in own window)"
    return " · ".join(([f"↑ {top} above"] if top else []) + ([more] if more else []))


# -- phases done ----------------------------------------------------------
#: How far back the chart reaches: long enough to show a pace, short enough that
#: last month's campaign does not flatten this week's.
CHART_S = 7 * 86400
#: The chart runs on to now, so it is redrawn this often even when nothing finished.
CHART_REDRAW_S = 600.0


def finish_series(dash, now: float | None = None) -> Series:
    """Phases finished in the last :data:`CHART_S`, wherever they were built, to now.

    From ``dash.finished`` — the ledger's own tick times — not this machine's
    log: that log only knows what ran here, so phases built on another machine
    would be missing from the chart. It runs on to now, so a hold
    reads as the flat line it is.
    """
    now = time.time() if now is None else now
    times = sorted(t for t in (getattr(dash, "finished", None) or {}).values()
                   if now - CHART_S <= t <= now)
    points = [(t, float(n)) for n, t in enumerate(times, 1)]
    if points and points[-1][0] < now:
        points.append((now, float(len(times))))
    return Series("phases done", points)


def can_plot(dash, now: float | None = None) -> bool:
    """Whether enough phases finished, over enough time, to chart."""
    series = finish_series(dash, now)
    span = series.span
    # The last point holds the count: at least two finishes, over some time.
    return bool(series.points) and series.points[-1][1] >= 2 and span[1] > span[0]


def next_lines(dash, width: int = 44, limit: int = 6) -> list[str]:
    """What runs next — the chart panel's content until there is a chart.

    "Not enough finished phases to plot" was true and told the owner nothing on
    the one day they most wanted to know what the swarm was about to do. The
    queue is known on day one: ready phases in ledger order, then the nearest
    blocked ones with what they are waiting for.
    """
    snap = dash.snapshot
    busy = at_work(dash)
    excluded = set(getattr(dash.cfg, "exclude", None) or [])
    dated = getattr(dash, "deferred", None) or {}
    graph = dash.graph or {}
    camps = summarise(graph, snap.landed, busy, excluded, deferred=dated)
    cur = active(camps)
    waiting = {b.phase for b in snap.blockers}
    items = tl.upcoming(graph, snap.landed, busy, excluded | set(dated), waiting,
                        prefer=cur.name if cur else None, limit=limit)
    if not items:
        return [paint("nothing left to run — the ledger is built", OK if graph else MUTED)]
    phase_w = max(8, min(18, width // 3))
    out = []
    for u in items:
        state = READY if u.ready else BLOCKED
        tail = "ready" if u.ready else "waits on " + ", ".join(u.needs[:3]) + (
            f" +{len(u.needs) - 3}" if len(u.needs) > 3 else "")
        out.append(
            f"{paint(GLYPH[state], state)} {clip(escape(u.phase), phase_w):<{phase_w}} "
            + paint(escape(clip(tail, max(6, width - phase_w - 3))), state if u.ready else MUTED)
        )
    return out


def chart_lines(dash, width: int = 44, height: int = 5, now: float | None = None) -> list[str]:
    """Cumulative completions against the wall clock (:func:`finish_series`).

    The one graph worth a permanent box: it answers "is this thing still moving"
    without reading a single number. Sampled onto a uniform time grid first —
    the supervisor's series is event-indexed, so plotting it raw would give ten
    completions in a minute the same width as a six-hour stall, and the axis
    under it would be a lie.
    """
    if not can_plot(dash, now):
        return [paint("not enough finished phases to plot", MUTED)]
    series = finish_series(dash, now)
    span = series.span

    body = area(hold_last(series.points, time_grid(span[0], span[1], width)), width, height)
    if not body:
        return [paint("no room to plot", MUTED)]
    # The y-scale column shifts the plot right; read its width off the rendered
    # row rather than recomputing what `area` decided, so the ticks stay under
    # the curve whatever the top-of-scale number turns out to be.
    gutter = body[0].index("┤") + 1 if "┤" in body[0] else 0
    reach = span[1] - span[0]
    labels = [axis_time(span[0] + reach * f, reach) for f in (0.0, 0.5, 1.0)]
    return body + [paint(" " * gutter + axis(labels, max(1, width - gutter)), MUTED)]


# -- needs you -----------------------------------------------------------
def need_rows(needs: list, width: int = 76, selected: str | None = None,
              now: float | None = None) -> list[tuple]:
    """``(markup, key, need)`` per thing waiting on the owner, two lines each.

    The header carries the two numbers that decide whether to get up — how long
    it has waited and how many phases sit behind it — and the question itself
    goes on the line under it, because the question is what gets answered.
    """
    now = time.time() if now is None else now
    out = []
    for need in needs[:MAX_NEEDS]:
        held = need.kind in (tl.NEED_LABEL["integ"], tl.NEED_LABEL["operator-abandoned"])
        state = BAD if held else YOU
        shape = GLYPH[OVERSEER] if need.kind == tl.NEED_LABEL[tl.OVERSEER] else GLYPH[state]
        mark = "▸ " if selected is not None and selected == need.key else "  "
        facts = [need.kind]
        if need.since is not None:
            facts.append(f"waited {fmt_coarse(max(0.0, now - need.since))}")
        if need.blocks:
            facts.append(f"blocks {need.blocks} phase{'s' if need.blocks != 1 else ''}")
        where = answer_where(need)
        if where:
            facts.append(where)
        name = need.ref if need.phase is None else (need.ref or need.phase)
        tail = " · ".join(facts)
        name_w = max(8, min(24, width - len(tail) - 6))
        head = (f"{mark}{paint(shape, state)} [b]{clip(escape(name or '?'), name_w)}[/b]  "
                + paint(escape(clip(tail, max(8, width - name_w - 6))), state))
        question = clip(need.question, max(10, width - 6)) or "no question text was captured"
        out.append((head + "\n" + paint(f"    {escape(question)}", BRIGHT), need.key, need))
    return out


def answer_where(need) -> str:
    """Where the owner answers ``need``, when the kind alone says so."""
    from ..state import wait_window  # local: only needed for this one name

    if need.kind == tl.NEED_LABEL["waiting"]:
        return "answer in its worker pane"
    if need.kind == tl.NEED_LABEL["parked"] and need.phase:
        return f"answer in tmux window {wait_window(need.phase)}"
    return ""


# -- the feed -------------------------------------------------------------
#: ``kind -> (label, token)`` for the feed's non-finish rows.
_FEED_KIND = {
    tl.OWNER: ("you decided", YOU),
    "decision": ("decided", INFO),
    "assumption": ("assumed", WARN),
    "risk": ("risk", WARN),
    tl.OVERSEER: ("overseer", OVERSEER),
}
#: A finish's label by status.
_FINISH = {
    "ok": "finished",
    "fail": "failed",
    "operator": "handed off",
    "skip": "skipped",
    "needs-owner": "needs you",
}
LABEL_W = 11


def feed_key(item) -> str:
    """Stable across ticks, so the cursor stays on the row it was on."""
    return f"{item.kind}:{item.ts:.3f}:{item.phase or item.ref}"


def when(ts: float | None, now: float) -> str:
    """``HH:MM`` for today's rows, ``MM-DD`` past a day — five cells either way."""
    if ts is None:
        return "  —  "
    return fmt_clock(ts)[:5] if now - ts < 20 * 3600 else fmt_stamp(ts)[:5]


def feed_style(item) -> tuple[str, str, str]:
    """``(label, token, glyph)`` for one feed row."""
    if item.kind == tl.FINISH:
        state = token(item.status)
        return _FINISH.get(item.status, item.status or "finished"), state, GLYPH.get(state, "·")
    if item.kind == tl.OPERATOR:
        if item.status == "done":
            return "operator ✓", OPERATOR, GLYPH[OPERATOR]
        return "job gave up", BAD, GLYPH[BAD]
    label, state = _FEED_KIND.get(item.kind, (item.kind, MUTED))
    shape = GLYPH[OVERSEER] if item.kind == tl.OVERSEER else (
        GLYPH[YOU] if item.kind == tl.OWNER else ("!" if item.kind == "risk" else "›"))
    return label, state, shape


def feed_rows(items: list, width: int = 76, selected: str | None = None,
              now: float | None = None, limit: int = FEED_ROWS) -> list[tuple]:
    """``(markup, key, item)`` per feed entry, newest first.

    A finish gets two lines when its recap needs them — the recap is the 1-2
    sentences worth reading, and cutting it at 40 cells threw away
    the half that said *why*. Everything else is one line. The owner's own
    decisions are painted in the needs-you colour and bright text: of all the
    calls in the feed they are the ones the owner will look for.
    """
    now = time.time() if now is None else now
    compact = width < 70  # the label column goes first: the glyph already says it
    phase_w = max(8, min(16, width // 5))
    out = []
    for item in items[:limit]:
        key = feed_key(item)
        label, state, shape = feed_style(item)
        mark = "▸ " if selected is not None and selected == key else "  "
        name = item.phase or (item.ref if item.kind != tl.OVERSEER else "pass")
        lead = (f"{mark}{paint(when(item.ts, now), MUTED)}  {paint(shape, state)} "
                f"{clip(escape(name or '—'), phase_w):<{phase_w}} ")
        used = 2 + 5 + 2 + 2 + phase_w + 1
        if not compact:
            bold = "[b]" if item.kind == tl.OWNER else ""
            lead += f"{bold}{paint(f'{label:<{LABEL_W}}', state)}{'[/b]' if bold else ''} "
            used += LABEL_W + 1
        room = max(10, width - used)
        text = " ".join((item.text or "").split())
        if item.kind == tl.OVERSEER and item.left:
            text = f"{text} — left for you: {' '.join(item.left.split())}" if text else (
                f"left for you: {' '.join(item.left.split())}")
        body_state = BRIGHT if item.kind in (tl.OWNER, tl.FINISH) else SOFT
        if not text:
            out.append((lead + paint("—", MUTED), key, item))
            continue
        if item.kind == tl.FINISH and len(text) > room:
            cut = text.rfind(" ", 0, room)
            cut = cut if cut > room // 2 else room
            first, rest = text[:cut], text[cut:].strip()
            line = (lead + paint(escape(first), body_state) + "\n" + " " * used
                    + paint(escape(clip(rest, room)), body_state))
        else:
            line = lead + paint(escape(clip(text, room)), body_state)
        out.append((line, key, item))
    return out


def empty_feed_lines(dash, width: int = 76) -> list[str]:
    """The feed before anything has happened: what is about to."""
    lines = [paint("  nothing has happened yet — up next:", MUTED)]
    lines += ["  " + line for line in next_lines(dash, width - 2, limit=5)]
    return lines


# -- opening what the feed points at --------------------------------------
def pass_detail(rec) -> str:
    """One Overseer pass in full: why it ran, what it saw, did, and left."""
    lines = [
        f"{paint(GLYPH[OVERSEER], OVERSEER)} [b]overseer pass {escape(rec.id)}[/b]  "
        + paint(escape(rec.status), token(rec.status) if rec.status != "running" else INFO),
        paint(f"{fmt_stamp(rec.started_at or None)} → {fmt_stamp(rec.ended_at or None)}", MUTED),
    ]
    reasons = [str(r.get("text") or r.get("key") or "") for r in (rec.reasons or [])
               if isinstance(r, dict)]
    for title, body, state in (
        ("why it ran", "\n".join(f"  · {r}" for r in reasons if r), None),
        ("summary", rec.summary, None),
        ("what it saw", rec.saw, None),
        ("what it did", rec.did, None),
        ("left for you", rec.left, YOU),
        ("it asked you", rec.question, YOU),
        ("your answer", rec.answer, None),
    ):
        if body and body.strip():
            lines += [section(title, state=state), escape(body.strip())]
    return "\n".join(lines)


def job_detail(item) -> str:
    """One operator job in full — for jobs no ledger phase owns."""
    state = token(item.state)
    lines = [
        f"{paint(GLYPH.get(state, '·'), state)} [b]operator job {escape(item.phase)}[/b]  "
        + paint(escape(item.state), state),
        paint(f"queued {fmt_stamp(item.queued_at or None)} · done "
              f"{fmt_stamp(item.done_at or None)} · {item.attempts} attempt(s)", MUTED),
    ]
    for title, body, st in (("the job", item.note, None), ("outcome", item.outcome, None),
                            ("last error", item.last_error, BAD),
                            ("it asked you", item.question, YOU), ("your answer", item.answer, None)):
        if body and body.strip():
            lines += [section(title, state=st), escape(body.strip())]
    return "\n".join(lines)


def note_detail(note) -> str:
    """One ping in full, as the alerts tab shows it."""
    try:
        from .tables import notification_detail
    except Exception:  # noqa: BLE001 - home must not go down with the tables module
        return escape(getattr(note, "text", "") or "")
    return notification_detail(note)


# -- the grid -------------------------------------------------------------
#: At this many columns (inside home's padding) and up, home is two columns: the
#: work on the left, usage / alerts / shells on the right. Below it they stack.
GRID_COLS = 110
#: The right column's share of the width, and its bounds.
SIDE_SHARE = 0.4
SIDE_MIN, SIDE_MAX = 46, 86
#: Under this many rows the left column has no room for both working now and a
#: stacked phases-done chart, and lists fewer phase books.
TALL_ROWS = 46
MID_BOOKS = 5


def grid_layout(width: int, height: int, slots: int = BASE_SLOTS) -> dict:
    """How home is cut at ``width`` columns (inside its padding) and ``height`` rows.

    Pure, so every size is testable without booting the app: whether the grid
    stands in two columns (``single`` when not), the main and side columns'
    widths, whether working now and phases done stack (``narrow``), whether the
    phases-done chart shows at all, how many phase-book rows the (scrolling) box
    shows — fewer as ``slots`` grows past four, since working now shows every
    slot — and how many rows the usage charts get (0 = none).
    """
    short = 0 < height < SHORT_ROWS
    single = width < GRID_COLS
    side = width if single else max(SIDE_MIN, min(SIDE_MAX, round(width * SIDE_SHARE)))
    main = width if single else width - side - 1
    # Side by side, working now gets half the main column; below WORK_COLS that
    # squeezes the phase names, so it takes the whole width and the chart stacks.
    narrow = main + 4 < NARROW_COLS or (main - 1) // 2 - 4 < WORK_COLS
    tall = height <= 0 or height >= TALL_ROWS
    return {
        "short": short,
        "single": single,
        "side": side,
        "side_inner": side - 4,
        "main": main,
        "narrow": narrow,
        "chart": not short and (single or not narrow or tall),
        "books": max(MIN_BOOKS, (SHORT_BOOKS if short else TALL_BOOKS if tall else MID_BOOKS)
                     - max(0, min(slots, WORK_VIS) - BASE_SLOTS)),
        "usage_chart": 0 if short else (5 if tall else 3),
    }


# -- footer ---------------------------------------------------------------
def footer_line(dash, width: int = 76, disk: str = "", now: float | None = None) -> str:
    """Health in one line: ``all clear``, or only what is not.

    A column of green dots is a column nobody reads, so the five things that can
    actually be wrong get a word only while they are wrong. Anything in colour
    here means look; nothing in colour means don't.

    Staleness only escalates while a slot is busy — a finished or idle swarm is
    *supposed* to be quiet, and a dashboard that warns about that is a dashboard
    nobody trusts.
    """
    now = now if now is not None else time.time()
    snap = dash.snapshot
    parts = alerts_mod.problems(dash, now)
    age = None if snap.last_event_at is None else max(0.0, now - snap.last_event_at)
    if not parts:
        parts.append(("all clear", OK))

    budget = max(20, width - len(PAD) - len(_HINT) - 2)
    # More problems than fit is itself information, but a wrapped footer is not a
    # footer. Problems that do not fit are counted, never silently dropped.
    hidden = 0
    while len(parts) > 1 and not _fits(parts + [(f"+{hidden + 1} more", WARN)], budget):
        parts.pop()
        hidden += 1
    if hidden:
        return _footer(parts + [(f"+{hidden} more", WARN)], budget)

    # The disk figure is the longest thing here and the only one with a whole tab
    # of its own, so it takes whatever room is left over and nothing more.
    clock = f"last event {fmt_duration(age)}" if age is not None else ""
    room = budget - len(" · ".join([t for t, _ in parts] + ([clock] if clock else []))) - 3
    if disk and room >= 12:
        parts.append((clip(disk, room), MUTED))
    if clock and _fits(parts + [(clock, MUTED)], budget):
        parts.append((clock, MUTED))
    return _footer(parts, budget)


def _fits(parts, budget: int) -> bool:
    return len(" · ".join(text for text, _ in parts)) <= budget


def _footer(parts, budget: int) -> str:
    """Assemble the footer, ellipsising a lone over-long part rather than wrapping."""
    plain = " · ".join(text for text, _ in parts)
    if len(plain) > budget:
        text, state = parts[-1]
        parts[-1] = (clip(text, max(4, budget - len(plain) + len(text))), state)
        plain = " · ".join(text for text, _ in parts)
    return (
        PAD
        + " · ".join(paint(text, state) for text, state in parts)
        + " " * max(2, budget + 2 - len(plain))
        + paint(_HINT, MUTED)
    )


# -- the screen -----------------------------------------------------------
class Row(Static):
    """One selectable line (or line pair) that knows what it points at.

    A widget per row rather than one Static per list: Textual delivers a click to
    the widget under the pointer, and that is the only way to be sure the row the
    owner aimed at is the row that opens. Reconstructing an index from the click's
    y-offset breaks the moment a worker note wraps to two lines.
    """

    DEFAULT_CSS = """
    Row { height: auto; }
    Row:hover { background: #1c2330; }
    Row.-on { background: #1f2a3a; }
    """

    class Clicked(Message):
        """A row was clicked. Handled by :class:`Home`, which owns the cursor."""

        def __init__(self, row: "Row") -> None:
            super().__init__()
            self.row = row

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # `row_`-prefixed: Widget and MessagePump own a pile of short attribute
        # names, and shadowing one fails somewhere that looks unrelated.
        self.row_phase: str | None = None
        self.row_kind: str = ""
        self.row_key = None

    def on_click(self) -> None:
        self.post_message(self.Clicked(self))


class AckPings(Message):
    """``x`` on home: the owner has seen the pings that never arrived.

    The same message the alerts tab posts; the app acknowledges them."""


class BookPanel(Panel):
    """The phase-books box: a wheel over it scrolls its rows, not the whole tab."""

    class Scrolled(Message):
        """The wheel turned ``delta`` rows (positive: down)."""

        def __init__(self, delta: int, panel: str | None = None) -> None:
            super().__init__()
            self.delta = delta
            self.panel = panel

    def on_mouse_scroll_down(self, event) -> None:
        event.stop()
        event.prevent_default()
        self.post_message(self.Scrolled(WHEEL_ROWS, self.id))

    def on_mouse_scroll_up(self, event) -> None:
        event.stop()
        event.prevent_default()
        self.post_message(self.Scrolled(-WHEEL_ROWS, self.id))


class Home(Vertical):
    """The default tab: headline, then a grid of what is running and what wants you.

    Left, the work: working now (every slot, then each parked worker at work in a
    window of its own, beside phases done), the phase books (a fixed-height
    scrolling box), the feed. Right,
    the run's surroundings: usage with its chart, alerts & notifications, the
    kept shells. Under :data:`GRID_COLS` the right column moves under the feed,
    and ``needs you`` comes back as a strip at the top, where it cannot be missed.

    Holds one cursor over everything selectable — what waits on the owner, the
    every busy slot, the phase books, the feed, then the pings — so ``enter`` (and
    the global actions) act on a chosen row rather than on whatever happens to be
    first. The cursor is keyed, not indexed, so a new feed row arriving on top
    does not slide it onto a different entry; it clamps itself when its row goes
    away.
    """

    DEFAULT_CSS = """
    Home { height: 1fr; overflow-y: auto; padding: 0 1; scrollbar-gutter: stable; }
    Home > #headline { margin: 0 0 1 0; }
    Home Panel { width: 1fr; }
    #grid { height: 1fr; min-height: 16; }
    #main { width: 1fr; height: 1fr; }
    #side { width: 60; height: 1fr; margin-left: 1; }
    #p-books { margin-top: 1; }
    #book-rows { height: auto; }
    #home-row { height: auto; }
    #p-work { margin-right: 1; }
    Home.-narrow #home-row { layout: vertical; }
    Home.-narrow #p-work { margin-right: 0; }
    Home.-nochart #p-chart { display: none; }
    Home.-nochart #p-work { margin-right: 0; }
    #p-needs { display: none; margin-bottom: 1; }
    #p-needs.-on { display: block; }
    #p-feed { height: 1fr; min-height: 8; margin-top: 1; }
    #work-rows, #need-rows, #alert-need-rows { height: auto; }
    #feed-rows { height: 1fr; }
    #p-usage { height: auto; margin-bottom: 1; }
    #p-resources { height: auto; margin-bottom: 1; }
    #p-alerts { height: 1fr; min-height: 6; }
    #alert-rows { height: 1fr; }
    #p-shells { height: auto; margin-top: 1; }
    Home.-short #p-books { margin-top: 0; }
    Home.-short > #headline, Home.-short #p-needs { margin-bottom: 0; }
    Home.-short #p-feed { min-height: 6; margin-top: 0; }
    Home.-single #grid { layout: vertical; height: auto; }
    Home.-single #main { height: auto; }
    Home.-single #side { width: 1fr; height: auto; margin-left: 0; margin-top: 1; }
    Home.-single #p-alerts { height: auto; }
    Home.-single #alert-rows { height: auto; max-height: 12; }
    Home > #footer { margin-top: 1; }
    """

    BINDINGS = [
        Binding("j,down", "cursor(1)", "next", show=False),
        Binding("k,up", "cursor(-1)", "prev", show=False),
        Binding("enter", "open", "open", show=False),
        Binding("x", "ack_drops", "clear not-delivered", show=False),
    ]

    can_focus = True

    class OpenPhase(Message):
        """Open the detail for one phase — a click or ``enter`` on a row.

        ``slot`` is the worker slot the phase is running in, or ``None`` when the
        row came from the feed and there is no live pane to jump to.
        """

        def __init__(self, phase: str, slot: int | None = None) -> None:
            super().__init__()
            self.phase = phase
            self.slot = slot

    class OpenText(Message):
        """Show a composed record — an Overseer pass, an ad-hoc operator job, a ping.

        Those have no History row to jump to, so home composes the text and the
        app decides where it appears.
        """

        def __init__(self, title: str, body: str) -> None:
            super().__init__()
            self.title = title
            self.body = body

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        #: ``(kind, key, phase, item)`` in reading order.
        self._targets: list[tuple] = []
        self._cursor = 0
        self._cursor_key = None
        self._dash = None
        self._chart_key = None
        self._chart: list[str] = []
        self._usage_key = None
        self._usage: list[str] = []
        self._disk_key = None
        self._disk_text = ""
        self._disk_at = 0.0
        self._feed_key = None
        self._feed: list = []
        self._needs: list = []
        self._notes: list = []
        self._single = False
        self._book_top = 0
        self._book_vis = 0
        self._work_top = 0

    def compose(self):
        yield Body(id="headline")
        with Panel("needs you", id="p-needs", state="you"):
            yield Vertical(id="need-rows")
        with Horizontal(id="grid"):
            with Vertical(id="main"):
                with Horizontal(id="home-row"):
                    with BookPanel("working now", id="p-work"):
                        yield Vertical(id="work-rows")
                    with Panel("phases done", id="p-chart"):
                        yield Body(id="b-chart")
                with BookPanel("phase books", id="p-books"):
                    yield Vertical(id="book-rows")
                with Panel("feed", id="p-feed"):
                    yield VerticalScroll(id="feed-rows")
            with Vertical(id="side"):
                with Panel("usage", id="p-usage"):
                    yield Body(id="b-usage")
                with Panel("resources", id="p-resources"):
                    yield Body(id="b-resources")
                with Panel("alerts & notifications", id="p-alerts"):
                    yield Vertical(id="alert-need-rows")
                    yield Body(id="b-problems")
                    yield VerticalScroll(id="alert-rows")
                with Panel("shells", id="p-shells"):
                    yield Body(id="b-shells")
        yield Body(id="footer")

    # -- selection --------------------------------------------------------
    def selected_phase(self) -> str | None:
        """The phase the global actions should act on."""
        got = self._target()
        return got[2] if got else None

    def _target(self):
        if not self._targets:
            return None
        return self._targets[min(self._cursor, len(self._targets) - 1)]

    def action_cursor(self, delta: int) -> None:
        self.move(delta)

    def action_open(self) -> None:
        got = self._target()
        if got:
            self._open(*got)

    def action_ack_drops(self) -> None:
        self.post_message(AckPings())

    def _open(self, kind: str, key, phase: str | None, item) -> None:
        """Open a row: a phase's History (or its live worker), or a record."""
        dash = self._dash
        if kind == "work":
            self.post_message(self.OpenPhase(phase, key))
            return
        if kind == "book":
            self.post_message(self.OpenText(f"phase book {key}", book_detail(dash, key)))
            return
        if kind == "note":
            self.post_message(self.OpenText("notification", note_detail(item)))
            return
        ref = getattr(item, "ref", "")
        is_pass = (kind == "feed" and item.kind == tl.OVERSEER) or (
            kind == "need" and item.phase is None)
        if is_pass:
            rec = next((r for r in (getattr(dash, "passes", None) or []) if r.id == ref), None)
            if rec is not None:
                self.post_message(self.OpenText(f"overseer pass {rec.id}", pass_detail(rec)))
            return
        known = {r.phase for r in (getattr(dash, "history", None) or [])}
        if kind == "feed" and item.kind == tl.OPERATOR and phase not in known:
            job = next((j for j in (getattr(dash, "operator", None) or []) if j.phase == ref),
                       None)
            if job is not None:
                self.post_message(self.OpenText(f"operator job {job.phase}", job_detail(job)))
            return
        if phase:
            self.post_message(self.OpenPhase(phase, getattr(item, "slot", None)))

    def move(self, delta: int) -> None:
        """Move the cursor, clamped. Public so a rebuilt ``j``/``k`` can drive it."""
        if not self._targets:
            return
        self._cursor = max(0, min(len(self._targets) - 1, self._cursor + delta))
        self._cursor_key = self._targets[self._cursor][:2]
        self._follow_cursor()
        if self._dash is not None:
            self.update(self._dash)  # a cursor that lags a tick reads as broken

    def on_row_clicked(self, event: Row.Clicked) -> None:
        event.stop()
        row = event.row
        for index, target in enumerate(self._targets):
            if target[0] == row.row_kind and target[1] == row.row_key:
                self._cursor = index
                self._cursor_key = target[:2]
                self.focus()  # so j/k carry on from wherever the mouse landed
                self._open(*target)
                break
        else:
            return
        if self._dash is not None:
            self.update(self._dash)

    # -- refresh ----------------------------------------------------------
    def layout_for(self, width: int, height: int) -> dict:
        """:func:`grid_layout` — a method so a test can pin it."""
        return grid_layout(width, height, self._slot_count())

    def _slot_count(self) -> int:
        """The rows working now lists: every slot, and each parked worker at work."""
        try:
            parked = getattr(self._dash, "working_parked", None) or ()
            return max(BASE_SLOTS, len(self._dash.snapshot.slots) + len(parked))
        except Exception:  # noqa: BLE001 - no snapshot yet
            return BASE_SLOTS

    def _book_names(self) -> list[str]:
        return [t[1] for t in self._targets if t[0] == "book"]

    def _clamp_books(self, total: int) -> None:
        self._book_top = max(0, min(self._book_top, total - self._book_vis))

    def _follow_cursor(self) -> None:
        """Scroll the book window so the cursor's book is inside it."""
        got = self._target()
        if got and got[0] == "work":
            ids = [t[1] for t in self._targets if t[0] == "work"]
            if got[1] in ids:
                index = ids.index(got[1])
                if index < self._work_top:
                    self._work_top = index
                elif index >= self._work_top + WORK_VIS:
                    self._work_top = index - WORK_VIS + 1
            return
        if not got or got[0] != "book" or self._book_vis <= 0:
            return
        index = self._book_names().index(got[1])
        if index < self._book_top:
            self._book_top = index
        elif index >= self._book_top + self._book_vis:
            self._book_top = index - self._book_vis + 1

    def on_book_panel_scrolled(self, event: BookPanel.Scrolled) -> None:
        event.stop()
        if event.panel == "p-work":
            self.scroll_workers(event.delta)
        else:
            self.scroll_books(event.delta)

    def scroll_workers(self, delta: int) -> None:
        """Scroll working now by ``delta`` rows (only past :data:`WORK_VIS` of them)."""
        self._work_top += delta
        if self._dash is not None:
            self.update(self._dash)

    def scroll_books(self, delta: int) -> None:
        """Scroll the phase-books window by ``delta`` rows; the cursor stays put."""
        self._book_top += delta
        self._clamp_books(len(self._book_names()))
        if self._dash is not None:
            self.update(self._dash)

    def update(self, dash) -> None:
        """Repaint. Never raises — a dead section is not a dead cockpit."""
        self._dash = dash
        # Paint to the width inside the padding *and* the scrollbar gutter, which
        # is reserved (``scrollbar-gutter: stable``) so the width cannot change
        # under lines already cut to it when the content first overflows.
        full = max(40, self.scrollable_content_region.width or 100)
        app_h = self.app.size.height
        cut = self.layout_for(full, app_h)
        short, single = cut["short"], cut["single"]
        self._single = single
        self.set_class(cut["narrow"], "-narrow")
        self.set_class(short, "-short")
        self.set_class(single, "-single")
        self.set_class(not cut["chart"], "-nochart")
        self._side_width(None if single else cut["side"])
        main_inner = cut["main"] - 4  # a Panel spends 2 columns on its border, 2 on padding
        side_inner = cut["side_inner"]
        half = max(24, (main_inner if cut["narrow"] or not cut["chart"]
                        else (cut["main"] - 1) // 2 - 4))
        now = time.time()

        try:
            self._retarget(dash)
        except Exception:  # noqa: BLE001 - a bad snapshot must not lose the cursor
            self._targets = []
        kind, key = (self._target() or ("", None))[:2]

        # What waits on the owner: a strip at the top when the side column is
        # below the fold, else pinned at the top of alerts & notifications.
        needs = self._build(lambda: need_rows(self._needs, full - 4, key if kind == "need" else None))
        side_needs = self._build(lambda: [
            (alerts_mod.need_line(n, side_inner, now, kind == "need" and key == n.key), None, n.key)
            for n in self._needs[:MAX_NEEDS]])
        self._rows("#need-rows", [(t, None, k) for t, _, k in needs] if single else [], "need")
        self._rows("#alert-need-rows", [] if single else side_needs, "need")
        panel = self._panel("#p-needs")
        if panel is not None:
            panel.set_class(single and bool(self._needs), "-on")
            extra = len(self._needs) - MAX_NEEDS
            panel.set_title(f"needs you ({len(self._needs)})",
                            f"+{extra} more · n lists them" if extra > 0 else "enter opens")

        fc = getattr(dash, "forecast", None)
        total = len(fc.books) if fc is not None else 0
        self._book_vis = cut["books"]
        self._clamp_books(total)
        top = self._book_top
        books = self._build(lambda: book_rows(dash, main_inner, key if kind == "book" else None,
                                              cut["books"], top))
        self._rows("#book-rows", books, "book")
        panel = self._panel("#p-books")
        if panel is not None:
            below = max(0, total - top - cut["books"])
            hint = " · ".join(([f"↑ {top} above"] if top else [])
                              + ([f"↓ {below} more"] if below else []))
            name = f"phase books ({total})" if total else "phase books"
            done = f"{name} · {finish_text(dash, now)}" if fc is not None else name
            panel.set_title(done if len(done) <= main_inner - 4 else name,
                            f"{hint} · enter opens" if hint else
                            "soonest first · enter opens" if total else "")

        every = self._build(lambda: worker_rows(dash, half, key if kind == "work" else None))
        self._work_top = max(0, min(self._work_top, len(every) - WORK_VIS))
        wtop = self._work_top
        work = every[wtop:wtop + WORK_VIS]
        self._rows("#work-rows", work, "work")
        work_panel = self._panel("#p-work")
        if work_panel is not None:
            work_panel.set_title("working now", work_hint(every, wtop))

        work_lines = sum(text.count("\n") + 1 for text, _, _ in work)
        height = max(4, work_lines - 1)
        plot = self._build_flag(lambda: can_plot(dash))
        chart = self._panel("#p-chart")
        if chart is not None:
            chart.set_title("phases done" if plot else "what's next")
        body = []
        if cut["chart"]:
            body = (self._chart_rows(dash, half, height) if plot
                    else self._build_lines(lambda: next_lines(dash, half, max(3, height))))
            self._set("#b-chart", "\n".join(body))
        # Side by side, the two cards are one row: give them one height, or the
        # row reads as two things that happen to be adjacent.
        self._pair_height(None if cut["narrow"] or short or not cut["chart"]
                          else max(work_lines, len(body)) + 2)

        if self._feed:
            feed = self._build(lambda: feed_rows(self._feed, main_inner - 2,
                                                 key if kind == "feed" else None))
            self._rows("#feed-rows", [(t, None, k) for t, k, _ in feed], "feed")
        else:
            lines = self._build_lines(lambda: empty_feed_lines(dash, main_inner))
            self._rows("#feed-rows", [("\n".join(lines), None, None)], "feed")
        feed_panel = self._panel("#p-feed")
        if feed_panel is not None:
            feed_panel.set_title("feed", "newest first · enter opens" if self._feed else "")
            # Stacked, the feed cannot take "the rest of the screen": the side
            # boxes come after it. It keeps what the screen has under the top.
            feed_panel.styles.height = max(8, app_h - 18) if single else "1fr"

        self._paint_usage(dash, side_inner, cut["usage_chart"], now)
        self._paint_resources(dash, now)
        self._paint_alerts(dash, side_inner, kind, key, now)
        self._paint_shells(dash, side_inner)

        self._set("#headline", self._build_text(lambda: headline(dash, full, short)))
        self._set("#footer", self._build_text(
            lambda: footer_line(dash, full, self._disk_line(dash, full // 2))
        ))

    def _paint_usage(self, dash, width: int, chart_rows: int, now: float) -> None:
        """The usage box, rebuilt when a reading, the run or the forecast moved, or a minute passed."""
        fc = getattr(dash, "forecast", None)
        busy = sum(1 for s in dash.snapshot.slots if s.busy)
        key = (len(getattr(dash, "samples", None) or ()), id(getattr(dash, "usage", None)),
               id(fc), busy, width, chart_rows, int(now // 60))
        if key != self._usage_key:
            self._usage_key = key
            self._usage = self._build_lines(lambda: usagebox.box_lines(dash, width, now,
                                                                        chart_rows))
        self._set("#b-usage", "\n".join(self._usage))
        panel = self._panel("#p-usage")
        if panel is not None:
            panel.set_title("usage", self._build_text(lambda: usagebox.subtitle(dash, now)))

    def _paint_resources(self, dash, now: float) -> None:
        """The resources box: the sampler's snapshot, already read by ``Dash``."""
        snap = getattr(dash, "resources", None)
        self._set("#b-resources", "\n".join(
            self._build_lines(lambda: resourcebox.box_lines(snap, now))))
        panel = self._panel("#p-resources")
        if panel is not None:
            panel.set_title("resources", resourcebox.subtitle(snap))

    def _paint_alerts(self, dash, width: int, kind: str, key, now: float) -> None:
        """Alerts & notifications: needs you, what is wrong now, then every ping."""
        problems = self._build_lines(lambda: [
            alerts_mod.problem_line(text, state, width)
            for text, state in alerts_mod.problems(dash, now)] + self._doctor_lines(width, now))
        if self._single and self._needs:
            problems.insert(0, paint(f" {GLYPH[YOU]} {len(self._needs)} thing(s) need you — "
                                     "listed at the top", YOU))
        todos = len(getattr(dash, "todos", None) or [])
        if todos:
            problems.insert(0, paint(f" {GLYPH[YOU]} {todos} to-do(s) for you, not questions"
                                     " — g guide me", YOU))
        self._set("#b-problems", "\n".join(problems))
        try:
            box = self.query_one("#b-problems", Body)
            if box.display != bool(problems):
                box.display = bool(problems)
        except Exception:  # noqa: BLE001 - not mounted yet
            pass
        acked = getattr(dash, "pings_acked_at", 0.0)
        # The ping list scrolls: its scrollbar takes two columns of the box.
        room = width - 2
        notes = self._build(lambda: [
            (alerts_mod.note_line(n, room, now, acked, kind == "note" and key == k), None, k)
            for k, n in self._notes])
        if not self._notes:
            notes = [(paint(" no pings yet — they are logged here as they are sent or held",
                            MUTED), None, None)]
        self._rows("#alert-rows", notes, "note")
        panel = self._panel("#p-alerts")
        if panel is not None:
            dropped = open_drops(dash.notifications or [], getattr(dash, "pings_acked_at", 0.0))
            bad = any(state == BAD for _, state in alerts_mod.problems(dash, now))
            panel.set_class(bool(self._needs) and not self._single, "-you")
            panel.set_class(bad and not (self._needs and not self._single), "-bad")
            count = f" · {len(self._needs)} need you" if self._needs else ""
            count += f" · {todos} to-do(s)" if todos else ""
            hint = "x clears not-delivered · " if dropped else ""
            hint = ("g guide me · " if todos else "") + hint
            panel.set_title(f"alerts & notifications{count}",
                            f"newest first · {hint}4 for all")

    def _doctor_lines(self, width: int, now: float) -> list[str]:
        """Failing and warning checks from the last `swarm doctor` run, if any ran."""
        try:
            from .doctor import Doctor

            doc = self.app.query_one(Doctor)
        except Exception:  # noqa: BLE001 - no doctor tab: nothing to add
            return []
        checks = getattr(doc, "_doc_checks", None) or []
        ran = getattr(doc, "_doc_ran_at", None)
        return [alerts_mod.doctor_line(c, ran, width, now) for c in checks
                if str(c.get("status", "")).lower() in ("warn", "fail")]

    def _paint_shells(self, dash, width: int) -> None:
        rows = self._build_lines(lambda: kept_rows(getattr(dash, "kept", None) or []))
        rows = [r for r in rows if isinstance(r, dict)]
        self._set("#b-shells", "\n".join(self._build_lines(
            lambda: alerts_mod.shell_lines(rows, width))))
        panel = self._panel("#p-shells")
        if panel is not None:
            alive = sum(1 for r in rows if r.get("alive"))
            panel.set_title(f"shells ({alive} running)" if rows else "shells",
                            "0 for details · x there stops one" if rows else "")

    def _side_width(self, cols: int | None) -> None:
        if cols == getattr(self, "_side_cols", None):
            return
        self._side_cols = cols
        try:
            side = self.query_one("#side")
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        side.styles.width = cols if cols is not None else "1fr"

    def _retarget(self, dash) -> None:
        """Everything selectable, in the order the eye reads it.

        The feed is rebuilt only when one of its sources was replaced — the dash
        swaps in a new list or dict whenever a file moved, so identity is an
        exact and free change test — and the cursor follows its key.
        """
        snap = dash.snapshot
        sources = (getattr(dash, "history", None), getattr(dash, "notes", None),
                   getattr(dash, "operator", None), getattr(dash, "passes", None))
        fkey = tuple(id(x) for x in sources)
        if fkey != self._feed_key:
            self._feed_key = fkey
            self._feed = tl.build_feed(*sources)
        self._needs = tl.needs_you(dash)
        self._notes = alerts_mod.recent_notes(getattr(dash, "notifications", None) or [])
        fc = getattr(dash, "forecast", None)
        full = max(40, self.scrollable_content_region.width or 100)
        books = list(fc.books) if fc is not None else []
        self._targets = (
            [("need", n.key, n.phase, n) for n in self._needs[:MAX_NEEDS]]
            + [("work", s.id, s.phase, None) for s in snap.slots if s.busy and s.phase]
            + [("book", b.name, None, b) for b in books]
            + [("feed", feed_key(f), f.phase, f) for f in self._feed[:FEED_ROWS]]
            + [("note", k, n.phase, n) for k, n in self._notes]
        )
        if self._cursor_key is not None:
            for index, target in enumerate(self._targets):
                if target[:2] == self._cursor_key:
                    self._cursor = index
                    break
        self._cursor = min(self._cursor, max(0, len(self._targets) - 1))

    def _chart_rows(self, dash, width: int, height: int) -> list[str]:
        """The chart, redrawn when a phase finished, and every ten minutes as it runs to now."""
        finished = getattr(dash, "finished", None) or {}
        key = (len(finished), max(finished.values(), default=0.0), width, height,
               int(time.time() // CHART_REDRAW_S))
        if key != self._chart_key:
            self._chart_key = key
            self._chart = self._build_lines(lambda: chart_lines(dash, width, height))
        return self._chart

    def _disk_line(self, dash, width: int) -> str:
        """The disk tab's one-line figure, from its cache — never a scan.

        Re-asked at most every :data:`DISK_EVERY_S`: with no scan yet
        ``disk.summary`` falls back to a statvfs for free space, which is cheap
        but is still not something a 2s tick should do.
        """
        if _disk is None:
            return self._disk_text
        now = time.time()
        try:
            report = self.app.query_one(_disk.Disk).report
        except Exception:  # noqa: BLE001 - no disk tab, or not mounted yet
            report = None
        key = (id(report), width)
        if key == self._disk_key and now - self._disk_at < DISK_EVERY_S:
            return self._disk_text
        try:
            self._disk_text = _disk.summary(report, dash.cfg, width)
        except Exception:  # noqa: BLE001
            self._disk_text = ""
        self._disk_key, self._disk_at = key, now
        return self._disk_text

    # -- painting ---------------------------------------------------------
    def _build(self, make) -> list[tuple]:
        try:
            return make()
        except Exception as exc:  # noqa: BLE001
            return [(paint(f"unavailable: {escape(str(exc))}", BAD), None, None)]

    def _build_lines(self, make) -> list[str]:
        try:
            return make()
        except Exception as exc:  # noqa: BLE001
            return [paint(f"unavailable: {escape(str(exc))}", BAD)]

    def _build_text(self, make) -> str:
        try:
            return make()
        except Exception as exc:  # noqa: BLE001
            return paint(f"unavailable: {escape(str(exc))}", BAD)

    def _set(self, selector: str, text: str) -> None:
        try:
            set_text(self.query_one(selector, Body), text)
        except Exception:  # noqa: BLE001 - not mounted yet; nothing to show it in
            pass

    def _pair_height(self, rows: int | None) -> None:
        """Pin both top cards to ``rows`` tall (``None``: size to content)."""
        if rows == getattr(self, "_pair_rows", None):
            return
        self._pair_rows = rows
        for selector in ("#p-work", "#p-chart"):
            panel = self._panel(selector)
            if panel is not None:
                panel.styles.height = rows if rows is not None else "auto"

    def _panel(self, selector: str):
        try:
            return self.query_one(selector, Panel)
        except Exception:  # noqa: BLE001 - not mounted yet
            return None

    def _build_flag(self, make) -> bool:
        try:
            return bool(make())
        except Exception:  # noqa: BLE001
            return False

    def _rows(self, selector: str, entries: list[tuple], kind: str) -> None:
        """Paint ``entries`` into a pool of :class:`Row` widgets under ``selector``.

        The pool only ever grows. Slot counts move on a config reload at most,
        the feed is capped, and tearing widgets down on a repaint tick is exactly
        the churn this screen exists to not create.
        """
        try:
            container = self.query_one(selector)
            pool = list(container.query(Row))
            if len(pool) < len(entries):
                fresh = [Row() for _ in range(len(entries) - len(pool))]
                container.mount(*fresh)
                pool = pool + fresh
            current = self._target()
            on = current[:2] if current else None
            targets = {(t[0], t[1]): t for t in self._targets if t[0] == kind}
            for index, row in enumerate(pool):
                if index >= len(entries):
                    if row.display:
                        row.display = False
                    continue
                text, phase, key = entries[index]
                target = targets.get((kind, key))
                row.row_phase = target[2] if target else phase
                row.row_kind, row.row_key = kind, key
                row.set_class(target is not None and on == (kind, key), "-on")
                if not row.display:
                    row.display = True
                set_text(row, text)
        except Exception:  # noqa: BLE001 - one list, not the whole screen
            pass
