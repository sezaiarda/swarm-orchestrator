"""Everything waiting on the owner: a toast when it appears, a drawer to read it in.

``NEEDS YOU`` used to be a fifth of the home screen, permanently, for a run that
needs nothing most of the day. A panel that is empty nine hours out of ten trains
the eye to skip it, so the one hour it filled up it was already invisible.

Attention is a *transition*, not a state. So it is split in two here:

* a **toast** fires the moment something starts waiting — once per blocker, never
  again, and never on the first poll of a run that was already blocked when the
  dashboard opened;
* a **drawer** docked to the right edge holds the full list, shut until ``n``
  opens it.

Shut, this costs one set-difference per tick and nothing else: no rows are built,
no markup is parsed, nothing repaints. That is not a nicety — the cockpit sits in
tmux window 0 for the life of a run, on a host that may be memory-constrained.

Everything with logic in it is a module-level pure function over
:class:`~swarm_orchestrator.tui.dash.Dash`; the widget is a dumb pump. That half
is what ``tests/test_tui_drawer.py`` exercises.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from rich.markup import escape
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Static

from .data import fmt_ago, fmt_clock, question_index
from .theme import BAD, COLOR, GLYPH, MUTED, OK, YOU, paint, token
from .timeline import NEED_LABEL, blocker_since

#: Rows the drawer holds. Past this it stops being a nudge and starts being the
#: alerts tab, which already exists and is better at it.
MAX_BLOCKERS = 5
MAX_NOTES = 6
MAX_OPERATOR = 4

#: Columns. Enough for a phase id, an elapsed, and two lines of question.
WIDTH = 44

#: A burst bigger than this is noise, not information — the rest are one keypress
#: away and already counted in the status bar.
MAX_TOASTS = 4
TOAST_TIMEOUT_S = 8.0
TOAST_HINT = "press n for the full list"

#: Blocker kinds that are a failure rather than a question. A held merge queue
#: stops *every* integration, not just its own phase, so it is red, not amber.
FAILED_KINDS = ("integ",)

#: The whole drawer when there is genuinely nothing in it. One quiet line beats
#: an empty box, and beats inventing content to fill one.
EMPTY = "nothing is waiting on you"


# -- text primitives ------------------------------------------------------
def clip(text: str, width: int) -> str:
    """One line, at most ``width`` cells, ellipsised when cut."""
    flat = " ".join((text or "").split())
    if width <= 1:
        return ""
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def wrap(text: str, width: int, lines: int = 2) -> list[str]:
    """``text`` as at most ``lines`` wrapped lines — the only prose in the drawer."""
    flat = " ".join((text or "").split())
    if not flat or width <= 1:
        return []
    out = textwrap.wrap(flat, width) or []
    if len(out) > lines:
        out = out[:lines]
        out[-1] = clip(out[-1] + " …", width)
    return out


def set_text(widget, text: str) -> None:
    """Write markup into a ``Static`` only when it actually changed.

    ``Static.update`` re-parses the markup and forces a layout pass every time,
    so an unguarded repaint on the 2s tick is a full relayout a second forever.
    Same guard ``tables.set_text`` carries, duplicated rather than imported so the
    drawer cannot be taken down by the tables module.
    """
    if getattr(widget, "_swarm_text", None) == text:
        return
    widget._swarm_text = text
    widget.update(text)


def mark(text: str) -> str:
    """Put the cursor on a row. Rows are built with a two-cell gutter for it."""
    return "▸ " + text[2:] if text.startswith("  ") else text


# -- alerts ---------------------------------------------------------------
@dataclass(frozen=True)
class Alert:
    """One thing worth interrupting for, plus the identity that stops it twice."""

    key: str
    severity: str  # information | warning | error — Textual's own vocabulary
    title: str
    body: str
    phase: str | None = None


def alert_key(blocker) -> str:
    """Stable identity for a blocker across ticks.

    Deliberately not the rendered message: the "waiting 41m" clock moves every
    tick and a worker may re-word its question (a later notification wins), so a
    text key re-toasts the same blocker forever. ``since`` is the phase's most
    recent ``LAUNCH``, which makes a retry after a resolution a genuinely new
    alert rather than a suppressed one.
    """
    return f"{blocker.kind}:{blocker.phase}:{blocker.since or 0:.0f}"


def blocker_text(blocker, questions: dict) -> str:
    """What this blocker is actually asking, best source first."""
    return blocker.question or questions.get(blocker.phase, "") or blocker.detail


def alerts(dash) -> list[Alert]:
    """Everything currently worth a toast. Pure; safe on a swarm that never ran."""
    snap = getattr(dash, "snapshot", None)
    if snap is None or not snap.ok:
        return []
    questions = (
        question_index(dash.notifications or [], dash.sentinels or {})
        if snap.blockers
        else {}
    )
    out: list[Alert] = []
    for blocker in snap.blockers:
        text = clip(blocker_text(blocker, questions), 120) or "no question text was captured"
        out.append(
            Alert(
                key=alert_key(blocker),
                severity="error" if blocker.kind in FAILED_KINDS else "warning",
                title=f"needs you: {blocker.phase}",
                body=f"{NEED_LABEL.get(blocker.kind, blocker.kind)} · {text}\n{TOAST_HINT}",
                phase=blocker.phase,
            )
        )
    for phase, status in sorted((snap.done or {}).items()):
        if status == "fail":
            out.append(
                Alert(
                    key=f"fail:{phase}",
                    severity="error",
                    title=f"{phase} failed",
                    body=f"its work was set aside and the phases after it wait"
                    f" — `swarm why {phase}` says more\n{TOAST_HINT}",
                    phase=phase,
                )
            )
    if not snap.finished and not snap.supervisor_alive:
        out.append(
            Alert(
                key=f"down:{snap.supervisor_pid}",
                severity="error",
                title="the swarm is not running",
                body=f"nothing starts or merges until you run `swarm up`\n{TOAST_HINT}",
            )
        )
    return out


class Toaster:
    """Which alerts have already been shouted about.

    The seen-set is never pruned: a run's whole alert history is a few hundred
    short strings, and pruning would re-toast anything that flickered out and back
    between two polls.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._primed = False

    def poll(self, dash) -> list[Alert]:
        """The alerts that are new since the last call — empty on the first one.

        Priming waits for a snapshot that actually read something. The app paints
        once at mount, before any poll, with a default empty ``Snapshot``; priming
        on that would seed nothing and then toast the entire existing run two
        seconds later, which is exactly the startup storm this avoids.
        """
        current = alerts(dash)
        if not self._primed:
            if not getattr(getattr(dash, "snapshot", None), "ok", False):
                return []
            self._primed = True
            self._seen = {alert.key for alert in current}
            return []
        fresh = [alert for alert in current if alert.key not in self._seen]
        self._seen.update(alert.key for alert in fresh)
        return fresh


def push_toasts(app, fresh: list[Alert], timeout: float = TOAST_TIMEOUT_S) -> None:
    """Raise ``fresh`` as Textual toasts, capped so a mass failure is not a wall.

    ``markup=False`` because every body here ends in text a worker wrote, and a
    stray ``[`` in it would otherwise eat the rest of the toast.
    """
    for alert in fresh[:MAX_TOASTS]:
        app.notify(
            alert.body,
            title=alert.title,
            severity=alert.severity,
            timeout=timeout,
            markup=False,
        )
    extra = len(fresh) - MAX_TOASTS
    if extra > 0:
        app.notify(f"+{extra} more — {TOAST_HINT}", severity="warning", markup=False)


# -- rows -----------------------------------------------------------------
@dataclass(frozen=True)
class Row:
    """One drawer line: what it says, and what selecting it opens."""

    text: str
    phase: str | None = None
    slot: int | None = None


def slot_of(snap, phase: str | None) -> int | None:
    """The slot holding ``phase`` right now, so a selection can jump to its pane."""
    if not phase:
        return None
    for slot in snap.slots:
        if slot.busy and slot.phase == phase:
            return slot.id
    return None


#: Columns a blocker's header spends around the phase name: "  ● " and the two
#: spaces before the kind.
_BLOCKER_GUTTER = len("  ● ") + len("  ")


def blocker_rows(dash, width: int = WIDTH - 4, now: float | None = None) -> list[Row]:
    """The old ``NEEDS YOU`` panel, one :class:`Row` per blocker.

    "How long has this been sitting" is the number that decides whether the owner
    gets up. A blocker that was never launched (a ``needs-owner`` finish, a phase
    whose ``LAUNCH`` rotated out of the log) has no ``since``, and the sentinel —
    then the ping that told the owner about it — is the only clock there is.
    """
    snap = dash.snapshot
    if not snap.blockers:
        return []
    notes = dash.notifications or []
    sentinels = dash.sentinels or {}
    questions = question_index(notes, sentinels)
    asked = {n.phase: n.ts for n in notes if n.phase and n.ts}
    out: list[Row] = []
    for blocker in snap.blockers[:MAX_BLOCKERS]:
        state = BAD if blocker.kind in FAILED_KINDS else token(blocker.kind)
        since = blocker_since(blocker, sentinels, asked)
        # The age is what decides whether the owner gets up, so it survives whole
        # and the phase name yields first. `source-provider-Pkg` plus a kind and
        # an age is 48 columns against a 40-column drawer.
        room = width - _BLOCKER_GUTTER
        label = NEED_LABEL.get(blocker.kind, blocker.kind)
        tail = clip(f"{label} · {fmt_ago(since, now)}", max(8, room - 6))
        name = clip(escape(blocker.phase), max(6, room - len(tail)))
        lines = [f"  [{COLOR[state]}]{GLYPH[state]}[/] [bold]{name}[/]  " + paint(tail, state)]
        text = blocker_text(blocker, questions)
        for line in wrap(escape(text) or "no question text was captured", max(10, width - 6)):
            lines.append(f"      {line}")
        out.append(Row("\n".join(lines), blocker.phase, slot_of(snap, blocker.phase)))
    return out


#: Columns an operator row spends around the phase name — the blocker header's
#: gutter, because it is the same shape and must stay in the same columns.
_OPERATOR_GUTTER = _BLOCKER_GUTTER


def owed(dash) -> list:
    """The hand-offs still owed — anything the queue has not finished with."""
    queue = getattr(getattr(dash, "snapshot", None), "operator", None) or []
    return [item for item in queue if not item.terminal]


def operator_rows(dash, width: int = WIDTH - 4, now: float | None = None) -> list[Row]:
    """The operator hand-off queue: what the swarm still owes *itself*.

    Its own section, below the blockers, because none of it is a question for the
    owner — an `operator` finish hands its action to a session, and putting it in
    "needs you" would refill that list with exactly what the status exists to keep
    out. State and age are the two numbers that matter: an item that has been
    ``queued`` for hours means nothing is draining, which no other line says.
    """
    out: list[Row] = []
    for item in owed(dash)[:MAX_OPERATOR]:
        when = str((item.triage or {}).get("when", ""))
        state = f"{item.state}/{when}" if when else item.state
        room = width - _OPERATOR_GUTTER
        age = fmt_ago(item.queued_at or None, now)
        tail = clip(f"{state} · {age}", max(8, room - 6))
        name = clip(escape(item.phase), max(6, room - len(tail)))
        out.append(
            Row(
                f"  [{COLOR[token(item.state)]}]{GLYPH[token(item.state)]}[/] [bold]{name}[/]  "
                + paint(escape(tail), token(item.state)),
                item.phase,
                slot_of(dash.snapshot, item.phase),
            )
        )
    return out


def operator_head(dash) -> str:
    """``operator queue (n)`` — the section label, or "" when there is nothing."""
    items = owed(dash)
    return paint(f"operator queue ({len(items)})", MUTED) if items else ""


#: Columns a ping row spends before its body: "  " + mark + " " + "HH:MM" + " ".
#: Counted, not guessed — it was guessed as 9 once, and every row wrapped.
_PING_GUTTER = len("  ") + 1 + len(" ") + len("HH:MM") + len(" ")


def notification_rows(dash, width: int = WIDTH - 4) -> list[Row]:
    """The last few pings, newest first, one line each.

    A ping that never landed is the whole reason these are here: the sender logs
    the failure and moves on, and the run then waits forever on an owner who was
    never told.
    """
    notes = list(dash.notifications or [])[-MAX_NOTES:]
    notes.reverse()
    snap = dash.snapshot
    out: list[Row] = []
    for note in notes:
        state = MUTED if note.suppressed else (OK if note.delivered else BAD)
        glyph = "·" if note.suppressed else ("✓" if note.delivered else "✗")
        who = note.phase or note.kind or "—"
        body = escape(clip(f"{who} · {note.text or '—'}", max(12, width - _PING_GUTTER)))
        out.append(
            Row(
                f"  {paint(glyph, state)} "
                f"[{COLOR[MUTED]}]{fmt_clock(note.ts)[:5]}[/] "
                + (paint(body, BAD) if note.dropped else body),
                note.phase,
                slot_of(snap, note.phase),
            )
        )
    return out


def pings_head(dash) -> str:
    """``pings`` — red, and counting, the moment one of them never landed."""
    notes = dash.notifications or []
    dropped = sum(1 for note in notes if note.dropped)
    if dropped:
        return paint(f"pings · {dropped} NOT DELIVERED", BAD)
    return paint("pings", MUTED)


def head_line(dash) -> tuple[str, str]:
    """``(state, markup)`` for the count header — the drawer's one loud line.

    Red is reserved for a blocker nobody can answer by answering it (a held merge
    queue) and for a supervisor that is not there. Historical ``fail`` sentinels
    are deliberately not counted: a run with one old failure would otherwise sit
    red forever, which is how a warning stops being one.
    """
    snap = getattr(dash, "snapshot", None)
    blockers = list(getattr(snap, "blockers", None) or [])
    loud = any(b.kind in FAILED_KINDS for b in blockers) or (
        getattr(snap, "ok", False)
        and not snap.finished
        and not snap.supervisor_alive
    )
    state = BAD if loud else (YOU if blockers else MUTED)
    return state, paint(f"needs you ({len(blockers)})", state)


# -- the widget -----------------------------------------------------------
class OpenPhase(Message):
    """A drawer row was chosen: open this phase.

    The drawer knows *which* phase; the app knows *where* a phase opens. The slot
    rides along because the drawer already had to find it, and it is the
    difference between jumping straight to a live pane and searching for one.
    """

    def __init__(self, phase: str, slot: int | None = None) -> None:
        super().__init__()
        self.phase = phase
        self.slot = slot


class DrawerRow(Static):
    """One clickable line.

    A real widget per row, so a mouse click lands on the row it looks like it
    landed on — the alternative is reverse-engineering a y offset that every
    wrapped question silently changes.
    """

    DEFAULT_CSS = """
    DrawerRow { height: auto; }
    DrawerRow:hover { background: #1c2330; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.phase: str | None = None
        self.slot: int | None = None

    def on_click(self) -> None:
        if self.phase:
            self.post_message(OpenPhase(self.phase, self.slot))


#: The binding the app needs for `n` to be global and to show in the footer — a
#: binding on the drawer alone is only live while the drawer itself has focus.
#: Pair it with `def action_toggle_drawer(self): self.query_one(Drawer).toggle()`.
APP_BINDING = Binding("n", "toggle_drawer", "needs you")


class Drawer(Vertical):
    """The right-edge drawer: blockers, then pings, hidden until asked for.

    Row widgets are mounted once and reused. Building and tearing down rows on a
    2s tick is the expensive half of a dashboard; toggling ``display`` on a fixed
    pool and guarding every text write is the cheap half, and the pool is small
    enough (11 rows) that the waste is nil.
    """

    # Below ~110 columns (``-overlay``, set by the app) the drawer moves to its
    # own layer: docks only inset widgets on their own layer, so it then floats
    # over the tab instead of squeezing a 36-column tab into unreadable columns.
    DEFAULT_CSS = """
    Drawer {
        display: none;
        dock: right;
        width: 44;
        height: 1fr;
        padding: 0 1;
        background: #161b22;
        border-left: thick #30363d;
    }
    Drawer.-open { display: block; }
    Drawer.-overlay { layer: overlay; border-left: thick #f0883e; width: 46; max-width: 90%; }
    Drawer > #drawer-head { height: 1; }
    Drawer > #drawer-body { height: 1fr; }
    Drawer .-off { display: none; }
    """

    BINDINGS = [
        Binding("n", "toggle", "needs you"),
        Binding("escape", "close", "close", show=False),
        Binding("j,down", "cursor(1)", "next", show=False),
        Binding("k,up", "cursor(-1)", "prev", show=False),
        Binding("enter", "select", "open", show=False),
    ]

    can_focus = True

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._toaster = Toaster()
        self._dash = None
        self._cursor = 0
        self._targets: list[tuple[str, int | None]] = []
        self._returning = None

    def compose(self):
        yield Static(id="drawer-head")
        with VerticalScroll(id="drawer-body"):
            for index in range(MAX_BLOCKERS):
                yield DrawerRow(id=f"drawer-b{index}", classes="-off")
            yield Static(id="drawer-empty", classes="-off")
            yield Static(id="drawer-more", classes="-off")
            yield Static(id="drawer-ops", classes="-off")
            for index in range(MAX_OPERATOR):
                yield DrawerRow(id=f"drawer-o{index}", classes="-off")
            yield Static(id="drawer-pings", classes="-off")
            for index in range(MAX_NOTES):
                yield DrawerRow(id=f"drawer-n{index}", classes="-off")

    # -- open / close -----------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.has_class("-open")

    def toggle(self) -> None:
        self.close() if self.is_open else self.open()

    def open(self) -> None:
        if self.is_open:
            return
        self._returning = self.screen.focused
        self.add_class("-open")
        if self._dash is not None:
            self._paint(self._dash)
        self.focus()

    def close(self) -> None:
        if not self.is_open:
            return
        self.remove_class("-open")
        # Focus has to go somewhere real: left on a hidden widget, the tabs' own
        # j/k bindings drop out of the binding chain and the app reads as dead.
        back, self._returning = self._returning, None
        if back is not None and back.is_attached:
            back.focus()

    # -- refresh ----------------------------------------------------------
    def update(self, dash) -> None:
        """Toast whatever is new; repaint only when the drawer is actually open."""
        self._dash = dash
        fresh = self._toaster.poll(dash)
        if fresh:
            push_toasts(self.app, fresh)
        if self.is_open:
            self._paint(dash)

    def _paint(self, dash) -> None:
        width = self.content_size.width or (WIDTH - 4)
        blockers = blocker_rows(dash, width)
        ops = operator_rows(dash, width)
        pings = notification_rows(dash, width)
        rows = blockers + ops + pings

        selectable = [i for i, row in enumerate(rows) if row.phase]
        self._cursor = min(self._cursor, max(0, len(selectable) - 1))
        self._targets = [(rows[i].phase, rows[i].slot) for i in selectable]
        cursor = selectable[self._cursor] if selectable else -1

        set_text(self.query_one("#drawer-head", Static), head_line(dash)[1])
        for index in range(MAX_BLOCKERS):
            row = blockers[index] if index < len(blockers) else None
            self._row(f"#drawer-b{index}", row, index == cursor)
        for index in range(MAX_OPERATOR):
            row = ops[index] if index < len(ops) else None
            self._row(f"#drawer-o{index}", row, len(blockers) + index == cursor)
        for index in range(MAX_NOTES):
            row = pings[index] if index < len(pings) else None
            self._row(
                f"#drawer-n{index}", row, len(blockers) + len(ops) + index == cursor
            )

        extra = len(dash.snapshot.blockers) - MAX_BLOCKERS
        self._quiet("#drawer-more", paint(f"  +{extra} more", MUTED) if extra > 0 else "")
        self._quiet(
            "#drawer-empty",
            "" if blockers else paint(f"  {EMPTY}", MUTED),
        )
        self._quiet("#drawer-ops", operator_head(dash))
        self._quiet("#drawer-pings", pings_head(dash) if pings else "")

    def _row(self, selector: str, row: Row | None, selected: bool) -> None:
        widget = self.query_one(selector, DrawerRow)
        widget.set_class(row is None, "-off")
        if row is None:
            widget.phase = widget.slot = None
            return
        widget.phase, widget.slot = row.phase, row.slot
        set_text(widget, mark(row.text) if selected else row.text)

    def _quiet(self, selector: str, text: str) -> None:
        widget = self.query_one(selector, Static)
        widget.set_class(not text, "-off")
        if text:
            set_text(widget, text)

    # -- selection --------------------------------------------------------
    def selected_phase(self) -> str | None:
        """The phase the global actions should act on — matches the tab contract."""
        if not self._targets:
            return None
        return self._targets[min(self._cursor, len(self._targets) - 1)][0]

    def on_open_phase(self, event: OpenPhase) -> None:
        """A click selects the row it landed on. Not stopped — the app still gets it."""
        for index, (phase, _slot) in enumerate(self._targets):
            if phase == event.phase:
                self._cursor = index
                break
        if self._dash is not None and self.is_open:
            self._paint(self._dash)

    # -- actions ----------------------------------------------------------
    def check_action(self, action: str, parameters) -> bool | None:
        """Only claim the shared keys while the drawer is up.

        Returning ``None`` leaves the key to whatever is further out in the
        binding chain rather than silently eating it.
        """
        if action in ("close", "cursor", "select"):
            return self.is_open or None
        return True

    def action_toggle(self) -> None:
        self.toggle()

    def action_close(self) -> None:
        self.close()

    def action_cursor(self, delta: int) -> None:
        self._cursor = max(0, self._cursor + delta)
        if self._dash is not None:
            self._paint(self._dash)  # clamps the cursor and redraws the mark

    def action_select(self) -> None:
        if self._targets:
            phase, slot = self._targets[min(self._cursor, len(self._targets) - 1)]
            self.post_message(OpenPhase(phase, slot))
