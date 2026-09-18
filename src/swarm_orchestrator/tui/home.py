"""The home screen: watch it run.

The previous home was five bordered panels — ``WORKERS``, ``PROGRESS``, ``NEEDS
YOU``, ``HEALTH``, ``RECENT`` — and three of them were a box drawn around a
single line of text. A border is a promise that what is inside is worth stopping
for; spending one on "nothing is waiting on you" teaches the eye to skip boxes,
which is the exact opposite of what borders are for.

So this screen spends two. ``working now`` and ``phases done`` are live content
that changes while you watch and needs a frame to hold its shape. Everything else
is a line: the headline strip is the page title, ``just finished`` is three rows
in a gutter, and health collapses into one footer line that says ``all clear`` or
names only what is not.

``NEEDS YOU`` is gone entirely. Blockers surface as a toast and a drawer now, so
the one thing that must interrupt the owner does, instead of sitting in a box
that has to be remembered.

Two structural rules hold the rest together:

* **Rows are widgets, not lines of a Static.** A click has to land on the row
  under the pointer, and turning a click's y-offset back into a row index is a
  guess that breaks the first time a worker note wraps. Each row carries its own
  phase and posts :class:`Home.OpenPhase`.
* **A tick costs nothing.** :meth:`Home.update` runs every 2s for the life of the
  run on a small host where a "freeze" can be the OOM killer, so it does no
  I/O at all — it renders what ``Dash`` already holds, the chart is recomputed
  only when the log actually grew, and every assignment goes through
  :func:`set_text` so a tick where nothing changed writes nothing.

Free text (recap summaries, pane tails) is written by workers and routinely
contains ``[``; every one of those strings goes through ``rich.markup.escape``
before it is painted, or a stray bracket eats the line.
"""

from __future__ import annotations

import time

from rich.markup import escape
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static

from . import probes
from .campaign import active, summarise
from .charts import area, axis, axis_time, hold_last, meter, time_grid
from .data import (
    CONTEXT_BUDGET,
    completions_series,
    eta,
    fmt_clock,
    fmt_coarse,
    fmt_duration,
    fmt_when,
    forecast,
    limit_outlook,
    phase_eta,
)
from .shell import STALE_BAD_S, STALE_WARN_S
from .theme import (
    ACCENT,
    BAD,
    COLOR,
    INFO,
    MUTED,
    OK,
    WARN,
    Body,
    Panel,
    bar,
    meter_state,
    paint,
    rows,
    token,
)

try:  # the disk tab is its own module and may not be there yet
    from . import disk as _disk
except Exception:  # noqa: BLE001 - home renders with or without a disk figure
    _disk = None

#: The homepage shows only the last few completions; the rest live on the
#: History tab.
MAX_FINISHED = 3

#: Below this many columns the two-up row stacks instead of squeezing. A 40-cell
#: panel cannot hold a phase name, an elapsed and a meter without lying.
NARROW_COLS = 96

#: Left gutter for the borderless sections, so they line up under the panels.
PAD = "  "

#: The `just finished` label column. Kept in step with ``#done-label``'s width
#: in ``Home.DEFAULT_CSS``, which cannot read a Python constant.
LABEL_W = 15

#: How often the disk line may be re-asked. With no scan yet ``disk.summary``
#: falls back to one statvfs, and a repaint tick must stay free.
DISK_EVERY_S = 30.0

_SHORT = {"ok": "ok", "fail": "fail", "skip": "skip", "needs-owner": "you"}

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
def headline(dash, width: int = 76) -> str:
    """The page title: what is being built, how far along, how long left.

    Unbordered on purpose. A box around the title of the page is a box for its
    own sake, and this screen only spends borders on things that move.
    """
    snap = dash.snapshot
    busy = {s.phase for s in snap.slots if s.busy and s.phase}
    excluded = set(getattr(dash.cfg, "exclude", None) or [])
    camps = [
        c for c in summarise(dash.graph or {}, snap.done, busy, excluded)
        if c.live_total or c.built
    ]
    if not camps:
        return PAD + paint("no phases scheduled — check the ledger", MUTED)

    cur = active(camps) or camps[0]
    inner = max(30, width - len(PAD))
    count = f"{cur.built} / {cur.live_total} phases · {cur.pct:.0f}%"
    gap = max(2, inner - len(cur.name) - len(count))
    first = f"{PAD}[b]{escape(cur.name)}[/b]{' ' * gap}{paint(count, MUTED)}"

    workers = len(snap.slots) or int(getattr(dash.cfg, "max_workers", 0) or 0)
    args = (dash.history or [], cur.live_total - cur.built, workers)
    left = eta(*args, running=len(cur.running), ready=len(cur.ready))
    finish_in, _ = forecast(*args, running=len(cur.running), ready=len(cur.ready))
    if finish_in and cur.live_total > cur.built:
        left += f" · done ~{fmt_when(time.time() + finish_in)}"
    fill = OK if cur.complete else (INFO if cur.running or cur.ready else MUTED)
    second = (
        f"{PAD}{paint(bar(cur.built, max(1, cur.live_total), max(10, inner - len(left) - 2)), fill)}"
        f"  {paint(left, WARN if left == 'stalled' else MUTED)}"
    )

    counts = []
    if cur.running:
        counts.append(paint(f"{len(cur.running)} running", INFO))
    if cur.ready:
        counts.append(paint(f"{len(cur.ready)} ready", ACCENT))
    if cur.blocked:
        counts.append(paint(f"{cur.blocked} blocked", MUTED))
    if cur.failed:
        counts.append(paint(f"{cur.failed} failed", BAD))
    if not counts:
        counts.append(
            paint("complete", OK) if cur.complete else paint("nothing scheduled", MUTED)
        )
    if not snap.ok:
        counts.append(paint("run not started — `swarm up`", MUTED))
    lines = [first, second, PAD + " · ".join(counts)]
    # Only once a worker's status line has reported: a run launched before the
    # meters tap existed would otherwise carry a permanent "not reported" line.
    if getattr(dash, "meters", None):
        text, state = limit_outlook(getattr(dash, "limits", None), finish_in)
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
        return [(paint(snap.reason or "no slots — has `swarm up` run?", MUTED), None, None)]

    blocked = {b.phase: b for b in snap.blockers}
    phase_w = max(8, min(20, width - 22 - ETA_W))
    meters = getattr(dash, "meters", None) or {}
    out = []
    for row in slots:
        slot, status, waiting_for, ctx = row[0], row[1], row[2], row[3]
        mark = "▸ " if selected is not None and selected == slot.id else "  "
        if not slot.busy or not slot.phase:
            out.append((f"{mark}{slot.id:<2}  " + paint("free", MUTED), None, slot.id))
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
            f"{fmt_duration(slot.elapsed_s):>6} {eta_cell(dash.history or [], slot.elapsed_s)}"
            f"  {gauge} {pct}{tag}"
        )
        note = worker_note(dash, slot, waiting_for)
        if note:
            line += "\n" + paint(f"      {clip(escape(note), max(10, width - 6))}", MUTED)
        out.append((line, slot.phase, slot.id))
    return out


# -- phases done ----------------------------------------------------------
def chart_lines(dash, width: int = 44, height: int = 5) -> list[str]:
    """Cumulative completions against the run's wall clock.

    The one graph worth a permanent box: it answers "is this thing still moving"
    without reading a single number. Sampled onto a uniform time grid first —
    the supervisor's series is event-indexed, so plotting it raw would give ten
    completions in a minute the same width as a six-hour stall, and the axis
    under it would be a lie.
    """
    events = getattr(getattr(dash, "tail", None), "events", None) or []
    series = completions_series(events)
    span = series.span
    if len(series.points) < 2 or span is None or span[1] <= span[0]:
        return [paint("not enough finished phases to plot", MUTED)]

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


# -- just finished --------------------------------------------------------
def finished_rows(dash, width: int = 60, selected: int | None = None,
                  limit: int = MAX_FINISHED) -> list[tuple]:
    """``(markup, phase, index)`` for the last few completions.

    Three of them: the whole point of this
    strip is the glance, and the History tab is where a list belongs.
    """
    runs = [r for r in (dash.history or []) if not r.running][:limit]
    if not runs:
        # The two spaces are the cursor gutter every real row carries. Without
        # them this butts straight against the label column, which is exactly
        # as wide as its own text.
        return [(paint("  nothing has finished yet", MUTED), None, None)]
    phase_w = max(8, min(18, width - 28))
    out = []
    for index, run in enumerate(runs):
        state = token(run.status)
        status = _SHORT.get(run.status or "", (run.status or "—")[:4])
        mark = "▸ " if selected is not None and selected == index else "  "
        summary = clip(escape(run.summary or run.note), max(10, width - phase_w - 16))
        out.append(
            (
                f"{mark}[{COLOR[MUTED]}]{fmt_clock(run.ended_at)[:5]}[/]  "
                f"{clip(escape(run.phase), phase_w):<{phase_w}} "
                f"{paint(f'{status:<4}', state)}  {summary or paint('—', MUTED)}",
                run.phase,
                index,
            )
        )
    return out


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
    elif snap.paused:
        parts.append(("paused — `swarm resume`", WARN))
    elif not snap.supervisor_alive and not snap.finished:
        parts.append(("supervisor down — `swarm up`", BAD))
    if snap.integ_blocked:
        kind = snap.integ_blocked_kind or "conflict"
        parts.append((f"merge queue held: {snap.integ_blocked} {kind}", BAD))
    dropped = [n for n in (dash.notifications or []) if not n.delivered]
    if dropped:
        parts.append((f"{len(dropped)} telegram(s) dropped", BAD))

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
    Row.-on { background: #161b22; }
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
    """The default tab: a headline, two live panels, three rows and a line.

    Holds a cursor over everything selectable — every busy slot, then the last
    three finishes — so the global ``w``/``r``/``t``/``f`` keys act on a chosen
    phase rather than on whatever happens to be first. The cursor clamps itself
    on every update, because the list it indexes changes underneath it.
    """

    DEFAULT_CSS = """
    Home { height: 1fr; overflow-y: auto; }
    Home > Horizontal { height: auto; }
    Home Panel { width: 1fr; }
    Home.-narrow > #home-row { layout: vertical; }
    #work-rows { height: auto; }
    #done-label { width: 15; height: 1; color: #8b949e; }
    #done-rows { width: 1fr; height: auto; }
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
        row came from ``just finished`` and there is no live pane to jump to.
        """

        def __init__(self, phase: str, slot: int | None = None) -> None:
            super().__init__()
            self.phase = phase
            self.slot = slot

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._targets: list[tuple[str, int, str]] = []
        self._cursor = 0
        self._dash = None
        self._chart_key = None
        self._chart: list[str] = []
        self._disk_key = None
        self._disk_text = ""
        self._disk_at = 0.0

    def compose(self):
        yield Body(id="headline")
        with Horizontal(id="home-row"):
            with Panel("working now", id="p-work"):
                yield Vertical(id="work-rows")
            with Panel("phases done", id="p-chart"):
                yield Body(id="b-chart")
        with Horizontal(id="done-strip"):
            yield Static(f"{PAD}just finished", id="done-label")
            yield Vertical(id="done-rows")
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
            kind, key, phase = got
            self.post_message(self.OpenPhase(phase, key if kind == "work" else None))

    def move(self, delta: int) -> None:
        """Move the cursor, clamped. Public so a rebuilt ``j``/``k`` can drive it."""
        if not self._targets:
            return
        self._cursor = max(0, min(len(self._targets) - 1, self._cursor + delta))
        if self._dash is not None:
            self.update(self._dash)  # a cursor that lags a tick reads as broken

    def on_row_clicked(self, event: Row.Clicked) -> None:
        event.stop()
        row = event.row
        if row.row_phase is None:
            return
        for index, (kind, key, _phase) in enumerate(self._targets):
            if kind == row.row_kind and key == row.row_key:
                self._cursor = index
                break
        self.focus()  # so j/k carry on from wherever the mouse landed
        self.post_message(self.OpenPhase(row.row_phase, row.row_key if row.row_kind == "work" else None))
        if self._dash is not None:
            self.update(self._dash)

    # -- refresh ----------------------------------------------------------
    def update(self, dash) -> None:
        """Repaint. Never raises — a dead section is not a dead cockpit."""
        self._dash = dash
        width = self.size.width or 100
        narrow = width < NARROW_COLS
        self.set_class(narrow, "-narrow")
        # `size` is already the content width, so the borderless lines get all of
        # it; a Panel spends 2 columns on its border and 2 on its padding.
        full = max(40, width)
        half = max(24, (full if narrow else full // 2) - 4)

        try:
            self._retarget(dash)
        except Exception:  # noqa: BLE001 - a bad snapshot must not lose the cursor
            self._targets = []
        kind, key = (self._target() or ("", None, ""))[:2]

        work = self._build(lambda: worker_rows(dash, half, key if kind == "work" else None))
        self._rows("#work-rows", work, "work", key if kind == "work" else None)

        height = max(4, sum(text.count("\n") + 1 for text, _, _ in work) - 1)
        self._set("#b-chart", "\n".join(self._chart_rows(dash, half, height)))

        done = self._build(
            lambda: finished_rows(dash, full - LABEL_W, key if kind == "done" else None)
        )
        self._rows("#done-rows", done, "done", key if kind == "done" else None)

        self._set("#headline", self._build_text(lambda: headline(dash, full)))
        self._set("#footer", self._build_text(
            lambda: footer_line(dash, full, self._disk_line(dash, full // 2))
        ))

    def _retarget(self, dash) -> None:
        """Everything selectable, in the order the eye reads it."""
        snap = dash.snapshot
        runs = [r for r in (dash.history or []) if not r.running][:MAX_FINISHED]
        self._targets = [
            ("work", s.id, s.phase) for s in snap.slots if s.busy and s.phase
        ] + [("done", i, r.phase) for i, r in enumerate(runs)]
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

    def _rows(self, selector: str, entries: list[tuple], kind: str, selected) -> None:
        """Paint ``entries`` into a pool of :class:`Row` widgets under ``selector``.

        The pool only ever grows. Slot counts move on a config reload at most,
        and tearing widgets down on a repaint tick is exactly the churn this
        screen exists to not create.
        """
        try:
            container = self.query_one(selector, Vertical)
            pool = list(container.query(Row))
            if len(pool) < len(entries):
                fresh = [Row() for _ in range(len(entries) - len(pool))]
                container.mount(*fresh)
                pool = pool + fresh
            for index, row in enumerate(pool):
                if index >= len(entries):
                    if row.display:
                        row.display = False
                    continue
                text, phase, key = entries[index]
                row.row_phase, row.row_kind, row.row_key = phase, kind, key
                row.set_class(phase is not None and key == selected, "-on")
                if not row.display:
                    row.display = True
                set_text(row, text)
        except Exception:  # noqa: BLE001 - one list, not the whole screen
            pass
