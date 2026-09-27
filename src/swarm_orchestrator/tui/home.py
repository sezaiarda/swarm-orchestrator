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

``needs you`` sits above everything when it is on screen at all, with how long
each thing has waited and how many phases sit behind it. The toast and the
``n`` drawer still exist; the strip is what makes a waiting question impossible
to miss on the screen the owner actually leaves open.

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

from ..drain import line as drain_line
from . import probes
from . import timeline as tl
from .campaign import active, summarise
from .charts import area, axis, axis_time, hold_last, meter, time_grid
from .data import (
    CONTEXT_BUDGET,
    completions_series,
    eta,
    eta_runs_of,
    fmt_clock,
    fmt_coarse,
    fmt_duration,
    fmt_stamp,
    fmt_when,
    forecast,
    held_merge,
    phase_eta,
    usage_outlook,
)
from .shell import STALE_BAD_S, STALE_WARN_S
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

#: Below this many columns the two-up row stacks instead of squeezing. A 40-cell
#: panel cannot hold a phase name, an elapsed and a meter without lying.
NARROW_COLS = 100

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
def headline(dash, width: int = 76, compact: bool = False) -> str:
    """The page title: what is being built, how far along, how long left.

    Unbordered on purpose. A box around the title of the page is a box for its
    own sake, and this screen only spends borders on things that move.
    ``compact`` (a short terminal) drops the campaign's description and puts the
    usage windows on one line, so the feed still fits on an 80x24 screen.
    """
    snap = dash.snapshot
    busy = {s.phase for s in snap.slots if s.busy and s.phase}
    excluded = set(getattr(dash.cfg, "exclude", None) or [])
    camps = [
        c for c in summarise(dash.graph or {}, snap.landed, busy, excluded)
        if c.live_total > c.skipped  # a campaign of nothing but skips is noise
    ]
    if not camps:
        return PAD + paint("no phases scheduled — check the ledger", MUTED)

    cur = active(camps) or camps[0]
    inner = max(30, width - len(PAD))
    count = f"{cur.built} / {cur.live_total} phases · {cur.pct:.0f}%"
    gap = max(2, inner - len(cur.name) - len(count))
    first = f"{PAD}[b]{escape(cur.name)}[/b]{' ' * gap}{paint(count, MUTED)}"
    # What the campaign *is*, in the ledger's own words — a name like `look`
    # says nothing to someone who did not write the ledger.
    what = (getattr(dash, "campaign_what", None) or {}).get(cur.name, "")

    workers = len(snap.slots) or int(getattr(dash.cfg, "max_workers", 0) or 0)
    args = (eta_runs_of(dash), cur.live_total - cur.built, workers)
    left = eta(*args, running=len(cur.running), ready=len(cur.ready))
    finish_in, _ = forecast(*args, running=len(cur.running), ready=len(cur.ready))
    if finish_in and cur.live_total > cur.built:
        left += f" · done ~{fmt_when(time.time() + finish_in)}"
        # This run has too few finished phases of its own to time the next.
        if getattr(dash, "eta_from_history", False):
            left += " (from history)"
    fill = OK if cur.complete else (INFO if cur.running or cur.ready else MUTED)
    second = (
        f"{PAD}{paint(bar(cur.built, max(1, cur.live_total), max(10, inner - len(left) - 2)), fill)}"
        f"  {paint(left, WARN if left == 'stalled' else MUTED)}"
    )

    counts = []
    if cur.running:
        counts.append(paint(f"{len(cur.running)} running", INFO))
    if cur.ready:
        counts.append(paint(f"{len(cur.ready)} ready", READY))
    if cur.blocked:
        counts.append(paint(f"{cur.blocked} blocked", BLOCKED))
    if cur.failed:
        counts.append(paint(f"{cur.failed} failed", BAD))
    if not counts:
        counts.append(
            paint("complete", OK) if cur.complete else paint("nothing scheduled", MUTED)
        )
    if not snap.ok:
        counts.append(paint("run not started — `swarm up`", MUTED))
    if getattr(dash, "big_picture", ""):
        counts.append(paint(escape(dash.big_picture), MUTED))
    lines = [first]
    if what and not compact:
        lines.append(PAD + paint(escape(clip(what, inner)), MUTED))
    lines += [second, PAD + " · ".join(counts)]
    # Only once a worker's status line has reported: a run launched before the
    # meters tap existed would otherwise carry a permanent "not reported" line.
    if getattr(dash, "meters", None):
        outlook = usage_outlook(getattr(dash, "limits", None),
                                getattr(dash, "usage", None), finish_in)
        if compact and outlook:
            # One line: the verdicts, most severe colour, cut to fit.
            worst = next((st for st in (BAD, WARN) if any(o[1] == st for o in outlook)),
                         outlook[0][1])
            lines.append(PAD + paint(clip(" · ".join(t for t, _ in outlook), inner), worst))
        else:
            for text, state in outlook:
                lines.append(PAD + paint(clip(text, inner), state))
    return rows(*lines)


# -- working now ----------------------------------------------------------
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

    Returns rows rather than one blob because the caller mounts a widget per row;
    a click has to land on the row it hit.
    """
    snap = dash.snapshot
    slots = list(dash.slot_rows())
    if not slots:
        return [(paint(snap.reason or "no workers yet — `swarm up` starts them", MUTED), None, None)]

    blocked = {b.phase: b for b in snap.blockers}
    phase_w = max(8, min(20, width - 22 - ETA_W))
    meters = getattr(dash, "meters", None) or {}
    out = []
    # Free slots share one line: four rows of "free" said one thing four times
    # and pushed the feed down the screen to say it.
    free = [str(row[0].id) for row in slots if not row[0].busy or not row[0].phase]
    for row in slots:
        slot, status, waiting_for, ctx = row[0], row[1], row[2], row[3]
        mark = "▸ " if selected is not None and selected == slot.id else "  "
        if not slot.busy or not slot.phase:
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
    if free:
        label = "all free" if len(free) == len(slots) else "free"
        out.append((f"  {' '.join(free)}  " + paint(f"{GLYPH[IDLE]} {label}", IDLE), None, None))
    return out


# -- phases done ----------------------------------------------------------
def can_plot(dash) -> bool:
    """Whether the run has finished enough phases, over enough time, to chart."""
    events = getattr(getattr(dash, "tail", None), "events", None) or []
    series = completions_series(events)
    span = series.span
    return len(series.points) >= 2 and span is not None and span[1] > span[0]


def next_lines(dash, width: int = 44, limit: int = 6) -> list[str]:
    """What runs next — the chart panel's content until there is a chart.

    "Not enough finished phases to plot" was true and told the owner nothing on
    the one day they most wanted to know what the swarm was about to do. The
    queue is known on day one: ready phases in ledger order, then the nearest
    blocked ones with what they are waiting for.
    """
    snap = dash.snapshot
    busy = {s.phase for s in snap.slots if s.busy and s.phase}
    excluded = set(getattr(dash.cfg, "exclude", None) or [])
    graph = dash.graph or {}
    camps = summarise(graph, snap.landed, busy, excluded)
    cur = active(camps)
    waiting = {b.phase for b in snap.blockers}
    items = tl.upcoming(graph, snap.landed, busy, excluded, waiting,
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


def chart_lines(dash, width: int = 44, height: int = 5) -> list[str]:
    """Cumulative completions against the run's wall clock.

    The one graph worth a permanent box: it answers "is this thing still moving"
    without reading a single number. Sampled onto a uniform time grid first —
    the supervisor's series is event-indexed, so plotting it raw would give ten
    completions in a minute the same width as a six-hour stall, and the axis
    under it would be a lie.
    """
    if not can_plot(dash):
        return [paint("not enough finished phases to plot", MUTED)]
    events = getattr(getattr(dash, "tail", None), "events", None) or []
    series = completions_series(events)
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
    parts: list[tuple[str, str]] = []

    if not snap.ok:
        parts.append(("no run yet", MUTED))
    elif snap.drain and snap.supervisor_alive:
        parts.append((escape(drain_line(snap.drain)), WARN))
    elif snap.paused:
        parts.append(("paused, no new workers — `swarm resume`", WARN))
    elif snap.usage_hold:
        parts.append((snap.usage_hold, WARN))
    elif not snap.supervisor_alive and not snap.finished:
        parts.append(("swarm not running — `swarm up`", BAD))
    if snap.integ_blocked:
        parts.append((f"merging stopped: {snap.integ_blocked}, "
                      f"{held_merge(snap.integ_blocked_kind)}", BAD))
    dropped = [n for n in (dash.notifications or []) if n.dropped]
    if dropped:
        parts.append((f"{len(dropped)} ping(s) never reached your phone", BAD))

    busy = any(s.busy for s in snap.slots)
    age = None if snap.last_event_at is None else max(0.0, now - snap.last_event_at)
    if busy and age is not None and age > STALE_BAD_S:
        parts.append((f"nothing has happened for {fmt_duration(age)}", BAD))
    elif busy and age is not None and age > STALE_WARN_S:
        parts.append((f"quiet for {fmt_duration(age)}", WARN))

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


class Home(Vertical):
    """The default tab: headline, what needs you, what is running, what happened.

    Holds one cursor over everything selectable — what waits on the owner, every
    busy slot, then the feed — so ``enter`` (and the global actions) act on a
    chosen row rather than on whatever happens to be first. The cursor is keyed,
    not indexed, so a new feed row arriving on top does not slide it onto a
    different entry; it clamps itself when its row goes away.
    """

    DEFAULT_CSS = """
    Home { height: 1fr; overflow-y: auto; padding: 0 1; scrollbar-gutter: stable; }
    Home > #headline { margin: 0 0 1 0; }
    Home.-short #p-chart { display: none; }
    Home.-short #p-work { margin-right: 0; }
    Home.-short > #headline, Home.-short #p-needs { margin-bottom: 0; }
    Home.-short #p-feed { min-height: 6; margin-top: 0; }
    Home > Horizontal { height: auto; }
    Home Panel { width: 1fr; }
    Home #p-work { margin-right: 1; }
    Home.-narrow > #home-row { layout: vertical; }
    Home.-narrow #p-work { margin-right: 0; }
    #p-needs { display: none; margin-bottom: 1; }
    #p-needs.-on { display: block; }
    #p-feed { height: 1fr; min-height: 10; margin-top: 1; }
    #work-rows, #need-rows { height: auto; }
    #feed-rows { height: 1fr; }
    Home > #footer { margin-top: 1; }
    """

    BINDINGS = [
        Binding("j,down", "cursor(1)", "next", show=False),
        Binding("k,up", "cursor(-1)", "prev", show=False),
        Binding("enter", "open", "open", show=False),
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
        """Show a composed record — an Overseer pass, an ad-hoc operator job.

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
        self._disk_key = None
        self._disk_text = ""
        self._disk_at = 0.0
        self._feed_key = None
        self._feed: list = []
        self._needs: list = []

    def compose(self):
        yield Body(id="headline")
        with Panel("needs you", id="p-needs", state="you"):
            yield Vertical(id="need-rows")
        with Horizontal(id="home-row"):
            with Panel("working now", id="p-work"):
                yield Vertical(id="work-rows")
            with Panel("phases done", id="p-chart"):
                yield Body(id="b-chart")
        with Panel("feed", id="p-feed"):
            yield VerticalScroll(id="feed-rows")
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

    def _open(self, kind: str, key, phase: str | None, item) -> None:
        """Open a row: a phase's History (or its live worker), or a record."""
        dash = self._dash
        if kind == "work":
            self.post_message(self.OpenPhase(phase, key))
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
    def update(self, dash) -> None:
        """Repaint. Never raises — a dead section is not a dead cockpit."""
        self._dash = dash
        # Stack by the space home really has (a docked drawer takes 44 of it);
        # paint to the width inside the padding *and* the scrollbar gutter, which
        # is reserved (``scrollbar-gutter: stable``) so the width cannot change
        # under lines already cut to it when the content first overflows.
        narrow = (self.region.width or 120) < NARROW_COLS
        short = 0 < self.app.size.height < SHORT_ROWS
        self.set_class(narrow, "-narrow")
        self.set_class(short, "-short")
        width = self.scrollable_content_region.width or 100
        # A Panel spends 2 columns on its border and 2 on its padding.
        full = max(40, width)
        inner = full - 4
        half = max(24, (full if narrow else (full - 1) // 2) - 4)

        try:
            self._retarget(dash)
        except Exception:  # noqa: BLE001 - a bad snapshot must not lose the cursor
            self._targets = []
        kind, key = (self._target() or ("", None))[:2]

        needs = self._build(lambda: need_rows(self._needs, inner, key if kind == "need" else None))
        self._rows("#need-rows", [(t, None, k) for t, k, _ in needs], "need")
        panel = self._panel("#p-needs")
        if panel is not None:
            panel.set_class(bool(self._needs), "-on")
            extra = len(self._needs) - MAX_NEEDS
            panel.set_title(f"needs you ({len(self._needs)})",
                            f"+{extra} more · n lists them" if extra > 0 else "enter opens")

        work = self._build(lambda: worker_rows(dash, half, key if kind == "work" else None))
        self._rows("#work-rows", work, "work")

        work_lines = sum(text.count("\n") + 1 for text, _, _ in work)
        height = max(4, work_lines - 1)
        plot = self._build_flag(lambda: can_plot(dash))
        chart = self._panel("#p-chart")
        if chart is not None:
            chart.set_title("phases done" if plot else "what's next")
        body = (self._chart_rows(dash, half, height) if plot
                else self._build_lines(lambda: next_lines(dash, half, max(3, height))))
        self._set("#b-chart", "\n".join(body))
        # Side by side, the two cards are one row: give them one height, or the
        # row reads as two things that happen to be adjacent.
        self._pair_height(None if narrow or short else max(work_lines, len(body)) + 2)

        if self._feed:
            feed = self._build(lambda: feed_rows(self._feed, inner - 2,
                                                 key if kind == "feed" else None))
            self._rows("#feed-rows", [(t, None, k) for t, k, _ in feed], "feed")
        else:
            lines = self._build_lines(lambda: empty_feed_lines(dash, inner))
            self._rows("#feed-rows", [("\n".join(lines), None, None)], "feed")
        feed_panel = self._panel("#p-feed")
        if feed_panel is not None:
            feed_panel.set_title("feed", "newest first · enter opens" if self._feed else "")

        self._set("#headline", self._build_text(lambda: headline(dash, full, short)))
        self._set("#footer", self._build_text(
            lambda: footer_line(dash, full, self._disk_line(dash, full // 2))
        ))

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
        self._targets = (
            [("need", n.key, n.phase, n) for n in self._needs[:MAX_NEEDS]]
            + [("work", s.id, s.phase, None) for s in snap.slots if s.busy and s.phase]
            + [("feed", feed_key(f), f.phase, f) for f in self._feed[:FEED_ROWS]]
        )
        if self._cursor_key is not None:
            for index, target in enumerate(self._targets):
                if target[:2] == self._cursor_key:
                    self._cursor = index
                    break
        self._cursor = min(self._cursor, max(0, len(self._targets) - 1))

    def _chart_rows(self, dash, width: int, height: int) -> list[str]:
        """The chart, redrawn only when the log actually grew."""
        events = getattr(getattr(dash, "tail", None), "events", None) or []
        key = (len(events), width, height)
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
