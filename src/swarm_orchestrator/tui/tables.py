"""The three data tabs: Workers, History and Notifications.

The dashboards these replace had a lot of text and no structure: every tab built
one big string and dropped it into
a ``Static``. A paragraph has to be **read**; a table can be **scanned**. Nothing
in a wall of text carries meaning by its position, so finding "which slot is
stuck" meant parsing prose at 1Hz.

Everything here is therefore built on three rules:

* **Structure before words.** Real :class:`~textual.widgets.DataTable` columns,
  so a value's *column* says what it is and the eye can go straight down one.
  Colour is semantic and comes from :mod:`~swarm_orchestrator.tui.theme` only —
  a hardcoded hex here would mean green stops meaning one thing everywhere.
* **Idle must be free.** This runs in tmux window 0 for the entire life of a run.
  :meth:`TableTab.sync` diffs the rendered cells and, when the *row set* is
  unchanged, patches only the individual cells that moved
  (:meth:`~textual.widgets.DataTable.update_cell_at`) instead of clearing and
  refilling. A ticking "elapsed" column therefore costs a handful of cell writes
  a second and — critically — never touches the cursor.
* **A bad row degrades one panel, not the cockpit.** Every ``update`` is wrapped;
  a raise paints the failure into that tab's detail pane and leaves the rest of
  the dashboard alive. The data layer already promises never to raise, but it is
  fed by four concurrently-evolving writers and this is the last line.

The row-building functions are module-level and pure: they take dataclasses and
return strings. That is the half worth testing, and it is tested in
``tests/test_tui_tables.py``. Rendering is not tested. The plain-text charts
that used to live here moved to :mod:`~swarm_orchestrator.tui.charts` when the
Graphs tab was dropped; the home screen draws the two that earned their space.

**Cursor stability** is worth spelling out. Rows are addressed by a *stable key*
(``slot-2``, ``dash-W7@1756370000``, the notification's file index), not by
position. History is newest-first, so a completing phase pushes every row down
one; restoring the cursor by index would silently move the owner's selection to a
different phase, and the next keypress would act on the wrong one. Restoring by
key keeps the selection on the row it was on.
"""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.coordinate import Coordinate
from textual.message import Message
from textual.widgets import DataTable

from . import data
from .campaign import campaign_of
from .data import (
    Notification,
    PhaseRun,
    SlotView,
    fmt_ago,
    fmt_clock,
    fmt_duration,
    fmt_stamp,
    load_attempts,
    load_notes,
)
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
    dot,
    field,
    meter_state,
    paint,
    rows as join_rows,
    token,
)

#: A worker that has been on one phase this long is not "making progress", it is
#: a thing to go look at. Purely presentational — nothing here acts on it.
STALE_ELAPSED_S = 2 * 3600.0
DEAD_ELAPSED_S = 4 * 3600.0


# -- small text helpers ----------------------------------------------------
def clip(text, width: int) -> str:
    """One line, at most ``width`` columns, ellipsised.

    Every free-text field on this screen (a recap, a worker note, a telegram
    body) is multi-line prose written by an agent. Left alone it would blow the
    row height out and reintroduce exactly the wall of text this replaces.
    """
    line = " ".join(str(text if text is not None else "").split())
    if width <= 0:
        return ""
    return line if len(line) <= width else line[: max(1, width - 1)] + "…"


def cell(text, width: int, state: str | None = None) -> str:
    """A clipped, markup-escaped, optionally coloured table cell.

    Escaping is not optional: ``DataTable`` renders a ``str`` cell as Rich markup
    and most of what lands in one is text a worker wrote. A recap containing
    ``[skip]`` would otherwise eat the rest of the row.
    """
    body = escape(clip(text, width))
    return paint(body, state) if state else body


def unique_keys(keys: list[str]) -> list[str]:
    """Make row keys unique, suffixing only the collisions.

    A duplicate key would make ``DataTable.add_row`` raise and blank the tab. The
    first occurrence keeps its bare key so the common case stays *stable* across
    rebuilds — that stability is the whole point of keying rows.
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for key in keys:
        count = seen.get(key, 0)
        seen[key] = count + 1
        out.append(key if count == 0 else f"{key}#{count}")
    return out


def set_text(widget, text: str) -> None:
    """Update a ``Static`` only when its text actually changed.

    ``Static.update`` re-parses the markup and calls ``refresh(layout=True)``
    unconditionally, so repainting an unchanged detail pane on every 1s tick is a
    full layout pass a second for the life of the run — on a dashboard whose
    entire premise is that idle costs nothing. Guarding the assignment is the
    difference between "redraws when something happened" and "redraws forever".
    """
    if getattr(widget, "_swarm_text", None) == text:
        return
    widget._swarm_text = text
    widget.update(text)


def set_border(widget, *, title: str | None = None, subtitle: str | None = None) -> None:
    """Same guard for a border title/subtitle — those setters always ``refresh()``.

    The last assignment is stashed rather than compared against the getter: the
    getter returns the markup round-trip of what was set, which does not always
    equal the string that was given, and a false mismatch would defeat the guard.
    """
    if title is not None and getattr(widget, "_swarm_title", None) != title:
        widget._swarm_title = title
        widget.border_title = title
    if subtitle is not None and getattr(widget, "_swarm_subtitle", None) != subtitle:
        widget._swarm_subtitle = subtitle
        widget.border_subtitle = subtitle


# -- workers ---------------------------------------------------------------
WORKER_COLUMNS: tuple[tuple[str, int], ...] = (
    ("", 2),
    ("slot", 4),
    ("phase", 20),
    ("live", 9),
    ("elapsed", 8),
    ("context", 15),
    ("+", 4),
    ("~", 4),
    ("branch", 26),
)

#: Statuses that mean "state.json still thinks this slot is working". A pane that
#: died leaves the run looking perfectly healthy everywhere else, which is why it
#: gets the one shouting cell on the screen.
GONE = "gone"


def unpack_slot_row(entry) -> tuple[SlotView, str, str, float | None, object]:
    """Normalise one :meth:`Dash.slot_rows` entry.

    The tuple has grown a member before (a ``PaneInfo`` was appended) and may
    again, so this pads and truncates rather than unpacking positionally: a tab
    that hard-unpacks a 5-tuple is a ``ValueError`` away from disappearing the
    moment the producer adds a field.
    """
    items = tuple(entry) if isinstance(entry, (list, tuple)) else (entry,)
    items = items + (None,) * 5
    slot, status, waiting, ctx, pane = items[:5]
    if not isinstance(slot, SlotView):
        slot = SlotView(id=-1, busy=False, phase=None, pane_id=None, branch=None, worktree=None)
    if not isinstance(ctx, (int, float)) or isinstance(ctx, bool):
        ctx = None
    return slot, str(status or "unknown"), str(waiting or ""), ctx, pane


def elapsed_state(seconds: float | None) -> str | None:
    """Colour for a running worker's elapsed clock, or ``None`` to leave it plain."""
    if seconds is None:
        return None
    if seconds >= DEAD_ELAPSED_S:
        return BAD
    if seconds >= STALE_ELAPSED_S:
        return WARN
    return None


def worker_key(entry) -> str:
    """Stable row key: a slot's identity is its number, whatever is in it."""
    slot, *_ = unpack_slot_row(entry)
    return f"slot-{slot.id}"


def worker_row(entry, repo=None) -> tuple[str, ...]:
    """One Workers row, one cell per :data:`WORKER_COLUMNS` entry.

    ``gone`` gets three separate tells — the marker glyph, the phase turning red
    and the word in caps — because it is the failure mode that otherwise looks
    identical to a healthy run and the one this tab exists to surface.
    """
    slot, status, waiting, ctx, pane = unpack_slot_row(entry)
    gone = status == GONE
    live = status
    if gone:
        live = "GONE"
    elif status == "waiting" and waiting:
        live = "waiting"
    state = token(live)

    if ctx is None:
        context = paint("—", MUTED)
    else:
        context = paint(f"{ctx:3.0f}% {bar(ctx, 100, 8)}", meter_state(ctx))

    commits = getattr(repo, "commits", None)
    dirty = getattr(repo, "dirty", 0) or 0
    return (
        paint("✖" if gone else "●", state),
        str(slot.id) if slot.id >= 0 else "—",
        cell(slot.phase or "—", 20, BAD if gone else (None if slot.busy else MUTED)),
        paint(live, state),
        cell(fmt_duration(slot.elapsed_s) if slot.busy else "—", 8,
             elapsed_state(slot.elapsed_s if slot.busy else None)),
        context,
        paint("—" if commits is None else str(commits), MUTED if not commits else OK),
        paint(str(dirty) if dirty else "—", WARN if dirty else MUTED),
        cell(slot.branch or "—", 26, MUTED),
    )


def worker_detail(entry, dash) -> str:
    """The selected slot in full: where it is, what it decided, what it just printed.

    The pane tail is the only place a worker's *current* thought is visible; the
    recap and the notes are the only places its past ones are. Together they are
    the answer to "what is this thing actually doing", which used to require
    switching to its tmux pane and reading.
    """
    slot, status, waiting, ctx, pane = unpack_slot_row(entry)
    gone = status == GONE
    head = (
        f"{dot(status, f'slot {slot.id}')}  "
        f"[bold]{escape(slot.phase or 'free')}[/]  "
        f"{paint(status, token(status))}"
    )
    lines = [head]
    if gone:
        lines.append(
            paint(
                "✖ PANE GONE — state still says this slot is busy, but nothing is "
                "running in it. The run will not advance until it is freed.",
                BAD,
            )
        )
    if not slot.busy:
        lines.append(paint("slot is free — nothing assigned", MUTED))
        return join_rows(*lines)

    lines.append(field("pane", escape(slot.pane_id or "—")))
    if pane is not None and getattr(pane, "window_name", ""):
        lines.append(field("window", escape(str(pane.window_name))))
    lines.append(field("branch", escape(slot.branch or "—")))
    lines.append(field("worktree", escape(slot.worktree or "—")))
    lines.append(field("started", fmt_ago(slot.started_at)))
    if ctx is not None:
        lines.append(
            field("context", f"{ctx:.0f}% {bar(ctx, 100, 16)}", state=meter_state(ctx))
        )
    if waiting:
        lines.append(field("waiting for", escape(clip(waiting, 160)), state=WARN))

    repo = (dash.repos or {}).get(slot.phase or "")
    if repo is not None:
        ahead = "—" if repo.commits is None else str(repo.commits)
        lines.append(field("branch work", f"{ahead} commit(s), {repo.dirty} dirty file(s)"))

    recap = (dash.recaps or {}).get(slot.phase or "")
    if recap is not None and recap.summary:
        lines.append("")
        lines.append(paint("recap", ACCENT))
        lines.append(escape(clip(recap.summary, 600)))

    notes = (dash.notes or {}).get(slot.phase or "") or []
    if notes:
        lines.append("")
        lines.append(paint(f"decisions it made on its own ({len(notes)})", ACCENT))
        for note in notes[-6:]:
            lines.append(
                f"  [{COLOR[MUTED]}]{fmt_clock(note.ts)}[/] "
                f"{paint(escape(note.kind), token(note.kind) if note.kind != 'decision' else INFO)} "
                f"{escape(clip(note.text, 140))}"
            )

    tail = (dash.tails or {}).get(slot.pane_id or "", "")
    body = [ln for ln in tail.splitlines() if ln.strip()][-6:]
    if body:
        lines.append("")
        lines.append(paint("last lines in its pane", ACCENT))
        lines.extend(f"  [{COLOR[MUTED]}]{escape(clip(ln, 160))}[/]" for ln in body)
    return join_rows(*lines)


# -- history ---------------------------------------------------------------
HISTORY_COLUMNS: tuple[tuple[str, int], ...] = (
    ("", 2),
    ("phase", 20),
    ("campaign", 12),
    ("status", 11),
    ("started", 13),
    ("took", 8),
    ("what it did", 58),
)


def history_key(run: PhaseRun) -> str:
    """Stable across refreshes: a run is its phase plus when it started.

    Not the row index — History is newest-first, so every completion shifts the
    whole table down and an index-restored cursor would land on a different run
    than the one the owner selected.
    """
    stamp = run.started_at if run.started_at is not None else run.ended_at
    return f"{run.phase}@{0.0 if stamp is None else stamp:.0f}"


def history_status(run: PhaseRun) -> str:
    return "running" if run.running else (run.status or "?")


def history_row(run: PhaseRun) -> tuple[str, ...]:
    """One History row. The last column is why this tab exists: the recap."""
    status = history_status(run)
    state = INFO if status == "running" else token(status)
    recap = run.summary or run.note
    return (
        paint("●", state),
        cell(run.phase, 20),
        cell(campaign_of(run.phase or ""), 12, MUTED),
        paint(clip(status, 11), state),
        cell(fmt_stamp(run.started_at), 13, MUTED),
        cell(fmt_duration(run.duration_s), 8),
        cell(recap or "— no recap recorded —", 58, None if recap else MUTED),
    )


def history_detail(run: PhaseRun, dash) -> str:
    """Everything on disk about one run — the "what did that worker do" answer.

    Four independent sources are joined here because each holds something the
    others do not: the generated recap (what it says it did), the sentinel note
    (what it wrote at the moment it finished), ``swarm note`` entries (calls it
    made without asking) and ``done/<phase>.jsonl`` (that it took four goes).
    """
    status = history_status(run)
    lines = [
        f"{dot(status, escape(run.phase))}  {paint(status, token(status))}  "
        f"[{COLOR[MUTED]}]{fmt_stamp(run.started_at)} → {fmt_stamp(run.ended_at)}[/]  "
        f"{fmt_duration(run.duration_s)}"
    ]
    meta = [f"campaign {campaign_of(run.phase or '')}"]
    if run.slot:
        meta.append(f"slot {run.slot}")
    if run.parked:
        meta.append("parked")
    lines.append(paint(" · ".join(meta), MUTED))

    if run.summary:
        lines.append("")
        lines.append(paint("recap", ACCENT))
        lines.append(escape(run.summary.strip()))
    if run.note:
        lines.append("")
        lines.append(paint("what it wrote when it finished", ACCENT))
        lines.append(escape(run.note.strip()))
    if not run.summary and not run.note:
        lines.append("")
        lines.append(paint("no recap on disk — press r to ask a live worker for one", MUTED))

    try:
        notes = load_notes(dash.notes_dir, run.phase)
    except Exception:  # noqa: BLE001 - a detail pane never costs more than itself
        notes = []
    if notes:
        lines.append("")
        lines.append(paint(f"decisions it made on its own ({len(notes)})", ACCENT))
        for note in notes[-10:]:
            lines.append(
                f"  [{COLOR[MUTED]}]{fmt_stamp(note.ts)}[/] "
                f"{paint(escape(note.kind), WARN if note.kind in ('risk', 'assumption') else INFO)}"
                f"  {escape(clip(note.text, 200))}"
            )

    try:
        attempts = load_attempts(dash.cfg.done_dir, run.phase)
    except Exception:  # noqa: BLE001
        attempts = []
    if attempts:
        lines.append("")
        lines.append(paint(f"attempts ({len(attempts)})", ACCENT))
        for att in attempts[-8:]:
            status_ = str(att.get("status") or "?")
            body = str(att.get("note") or att.get("summary") or "")
            lines.append(
                f"  [{COLOR[MUTED]}]{fmt_stamp(data.coerce_ts(att.get('ts')))}[/] "
                f"{paint(escape(status_), token(status_))}  {escape(clip(body, 160))}"
            )
    return join_rows(*lines)


# -- notifications ---------------------------------------------------------
NOTIFICATION_COLUMNS: tuple[tuple[str, int], ...] = (
    ("", 2),
    ("time", 13),
    ("kind", 14),
    ("phase", 16),
    ("source", 20),
    ("message", 56),
)

#: Cycled with `F`. "failed" is first after "all" because a dropped ping is the
#: reason to ever open this tab: it is how an unattended run goes wrong silently.
NOTIFICATION_MODES = ("all", "failed", "delivered")


def notification_key(index: int) -> str:
    """File order. The log is append-only, so an existing line's index never moves."""
    return f"n{index}"


def notification_row(note: Notification) -> tuple[str, ...]:
    """One Notifications row. A failed delivery is red in three columns."""
    state = OK if note.delivered else BAD
    return (
        paint("✓" if note.delivered else "✗", state),
        cell(fmt_stamp(note.ts), 13, MUTED),
        cell(note.kind or "—", 14, None if note.delivered else BAD),
        cell(note.phase or "—", 16),
        cell(note.source or "unknown", 20, MUTED),
        cell(note.text or "—", 56, None if note.delivered else BAD),
    )


def notification_matches(note: Notification, needle: str) -> bool:
    """`/` search over every column that carries words."""
    if not needle:
        return True
    low = needle.lower()
    return any(
        low in (value or "").lower()
        for value in (note.text, note.phase, note.kind, note.source, note.error)
    )


def notification_detail(note: Notification) -> str:
    """One ping in full. A failure leads with the error, not with the message."""
    head = (
        paint("✓ delivered", OK)
        if note.delivered
        else paint("✗ NOT DELIVERED — this ping never reached you", BAD)
    )
    lines = [
        head,
        field("kind", escape(note.kind or "—")),
        field("phase", escape(note.phase or "—")),
        field("sent", f"{fmt_stamp(note.ts)}  ({fmt_ago(note.ts)})"),
        field("sent by", escape(note.source or "unknown code path")),
    ]
    if note.error:
        lines.append("")
        lines.append(paint("error", BAD))
        lines.append(paint(escape(note.error), BAD))
    lines.append("")
    lines.append(paint("message", ACCENT))
    lines.append(escape(note.text or "(empty)"))
    return join_rows(*lines)


# -- the shared table tab --------------------------------------------------
class OpenDetail(Message):
    """`enter` on a row: the tab hands the app a fully composed detail body.

    The tab owns *what* the detail says; the app owns *where* it appears. Without
    this seam the tabs would have to import the app's modal screen, which is the
    import cycle that put all of this in one 1500-line file to begin with.
    """

    def __init__(self, title: str, body: str, phase: str | None = None) -> None:
        super().__init__()
        self.title = title
        self.body = body
        self.phase = phase


class TableTab(Vertical):
    """One tab: a header line, a :class:`DataTable`, and a detail panel.

    Subclasses supply ``COLUMNS`` and implement :meth:`_update` (build the rows)
    plus :meth:`detail_text` (describe the selected one). Everything about not
    costing anything at idle, not losing the cursor and not taking the dashboard
    down with it lives here so no tab can forget it.
    """

    COLUMNS: tuple[tuple[str, int], ...] = ()
    DETAIL_TITLE = "detail"

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("enter", "open_detail", "detail"),
        Binding("escape", "clear_filter", "clear filter", show=False),
    ]

    DEFAULT_CSS = """
    TableTab { layout: vertical; height: 1fr; }
    TableTab > .tab-head { height: 1; padding: 0 1; }
    TableTab > DataTable { height: 1fr; min-height: 5; }
    TableTab > .detail-pane { height: auto; max-height: 45%; overflow-y: auto; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rows: list = []
        self.filter = ""
        self._keys: list[str] = []
        self._cells: list[tuple[str, ...]] = []
        self._dash = None

    # -- composition ------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Body("", classes="tab-head")
        table = DataTable(cursor_type="row", zebra_stripes=True)
        for label, width in self.COLUMNS:
            table.add_column(label, width=width)
        yield table
        with Panel(self.DETAIL_TITLE, classes="detail-pane"):
            yield Body("", classes="detail-body")

    @property
    def table(self) -> DataTable:
        return self.query_one(DataTable)

    @property
    def detail_panel(self) -> Panel:
        return self.query_one(Panel)

    # -- the contract -----------------------------------------------------
    def update(self, dash) -> None:
        """Refresh the tab. **Never raises** — a failure degrades this panel only."""
        self._dash = dash
        try:
            self._update(dash)
        except Exception as exc:  # noqa: BLE001 - the whole point of this method
            self._degrade(exc)

    def _update(self, dash) -> None:
        raise NotImplementedError

    def selected_phase(self) -> str | None:
        """The phase under the cursor, for the app's global ``w``/``r``/``t``/``f``."""
        return None

    # -- rendering --------------------------------------------------------
    def sync(self, rows: list, keys: list[str], cells) -> None:
        """Put ``rows`` on screen, touching as few widgets as possible.

        Three paths, cheapest first: identical cells (do nothing at all), same
        rows with different values (patch just those cells, cursor untouched),
        different rows (rebuild, then put the cursor back on the *same key*).
        The middle path is what makes a ticking elapsed column affordable.
        """
        self.rows = rows
        keys = unique_keys(keys)
        rendered = [tuple(str(value) for value in cells(row)) for row in rows]
        table = self.table

        if keys == self._keys:
            if rendered == self._cells:
                return
            for row_index, (new, old) in enumerate(zip(rendered, self._cells)):
                for col_index, (new_cell, old_cell) in enumerate(zip(new, old)):
                    if new_cell != old_cell:
                        table.update_cell_at(
                            Coordinate(row_index, col_index), new_cell, update_width=False
                        )
            self._cells = rendered
            return

        previous_index = table.cursor_row
        previous_key = (
            self._keys[previous_index] if 0 <= previous_index < len(self._keys) else None
        )
        table.clear()
        for key, row_cells in zip(keys, rendered):
            table.add_row(*row_cells, key=key)
        self._keys, self._cells = keys, rendered
        if keys:
            if previous_key in keys:
                target = keys.index(previous_key)
            else:
                target = min(max(previous_index, 0), len(keys) - 1)
            table.move_cursor(row=target)

    def set_head(self, text: str) -> None:
        set_text(self.query_one(".tab-head", Body), text)

    def update_detail(self, dash=None) -> None:
        """Repaint only the detail pane — what moving the cursor costs."""
        dash = dash if dash is not None else self._dash
        try:
            body = self.query_one(".detail-body", Body)
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        try:
            text = self.EMPTY_DETAIL if dash is None else self.detail_text(dash)
        except Exception as exc:  # noqa: BLE001
            text = paint(f"detail unavailable: {escape(str(exc))}", BAD)
        set_text(body, text)

    EMPTY_DETAIL = ""

    def detail_text(self, dash) -> str:
        return ""

    def _degrade(self, exc: Exception) -> None:
        """Show the failure in this tab instead of taking the app down with it."""
        try:
            set_text(
                self.query_one(".detail-body", Body),
                paint(f"this panel failed to render: {escape(str(exc))}", BAD),
            )
        except Exception:  # noqa: BLE001 - nothing left to do but stay alive
            pass

    # -- selection + filtering -------------------------------------------
    @property
    def selected(self):
        try:
            index = self.table.cursor_row
        except Exception:  # noqa: BLE001 - not mounted
            return None
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def focus_row(self, phase: str) -> None:
        """Put the cursor on ``phase``, for a drill-in from another screen.

        A filter in force can hide the row being asked for, so clear it rather
        than silently landing the cursor somewhere else.
        """
        if self.filter and not any(getattr(r, "phase", None) == phase for r in self.rows):
            self.set_filter("")
        for index, row in enumerate(self.rows):
            if getattr(row, "phase", None) == phase:
                try:
                    self.table.move_cursor(row=index)
                    self.table.focus()
                except Exception:  # noqa: BLE001 - not mounted yet
                    pass
                return

    def set_filter(self, needle: str) -> None:
        self.filter = needle or ""
        self._keys = []  # the row set changes; force the rebuild path
        if self._dash is not None:
            self.update(self._dash)

    def head_suffix(self) -> str:
        return f"  ·  [{COLOR[WARN]}]/{escape(self.filter)}[/]" if self.filter else ""

    # -- keys -------------------------------------------------------------
    def on_data_table_row_highlighted(self) -> None:
        # Only ever repaints the Static. Rebuilding here would move the cursor,
        # which fires this again — an event storm that wedges the UI.
        self.update_detail()

    def action_cursor_down(self) -> None:
        self.table.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.table.action_cursor_up()

    def action_clear_filter(self) -> None:
        if self.filter:
            self.set_filter("")

    def action_open_detail(self) -> None:
        if self._dash is None or self.selected is None:
            return
        try:
            body = self.detail_text(self._dash)
        except Exception as exc:  # noqa: BLE001
            body = paint(f"detail unavailable: {escape(str(exc))}", BAD)
        self.post_message(OpenDetail(self.DETAIL_TITLE, body, self.selected_phase()))


# -- workers tab -----------------------------------------------------------
class Workers(TableTab):
    """One row per slot: what is in it, how far along, and whether it is alive.

    The ``live`` column is the reason this is a table. It joins three sources the
    owner otherwise has to reconcile by hand — ``state.json`` (is the slot
    claimed), ``claude agents`` (is the agent busy, idle or waiting on an answer)
    and ``tmux`` (is anything running in the pane at all) — and a disagreement
    between them is exactly what a stalled run looks like.
    """

    COLUMNS = WORKER_COLUMNS
    DETAIL_TITLE = "slot"
    EMPTY_DETAIL = "no slots yet — has `swarm up` run?"

    def _update(self, dash) -> None:
        rows = list(dash.slot_rows())
        gone = [r for r in rows if unpack_slot_row(r)[1] == GONE]
        busy = [r for r in rows if unpack_slot_row(r)[0].busy]
        waiting = [r for r in rows if unpack_slot_row(r)[1] == "waiting"]

        self.sync(
            rows,
            [worker_key(r) for r in rows],
            lambda r: worker_row(r, (dash.repos or {}).get(unpack_slot_row(r)[0].phase or "")),
        )

        head = [f"{len(busy)}/{len(rows)} slots busy"]
        if waiting:
            head.append(paint(f"{len(waiting)} waiting on you", WARN))
        if gone:
            head.append(paint(f"{len(gone)} PANE GONE", BAD))
        self.set_head("  ·  ".join(head) + self.head_suffix())
        # The border is the part visible without reading anything: a dead pane
        # turns the whole panel red from across the room.
        panel = self.detail_panel
        panel.set_class(bool(gone), "-bad")
        panel.set_class(bool(waiting) and not gone, "-warn")
        set_border(panel, title=f"slot — {len(gone)} gone" if gone else "slot")
        self.update_detail(dash)

    def detail_text(self, dash) -> str:
        row = self.selected
        return paint(self.EMPTY_DETAIL, MUTED) if row is None else worker_detail(row, dash)

    def selected_phase(self) -> str | None:
        row = self.selected
        return None if row is None else unpack_slot_row(row)[0].phase


# -- history tab -----------------------------------------------------------
class History(TableTab):
    """Every phase ever run, newest first — the "what did that worker do" tab.

    The sentinel notes it reads have been written by every worker since the
    project started and were never once read back; they were kept only so a
    restart could tell a finished phase from an interrupted one. They are the
    worker's own account of its run, which is precisely what is wanted here.
    """

    COLUMNS = HISTORY_COLUMNS
    DETAIL_TITLE = "phase"
    EMPTY_DETAIL = "no phase history yet — nothing has run in this state dir"

    def _update(self, dash) -> None:
        history = list(dash.history or [])
        rows = [run for run in history if run.matches(self.filter)]
        self.sync(rows, [history_key(run) for run in rows], history_row)

        failed = sum(1 for run in history if run.status == "fail")
        running = sum(1 for run in history if run.running)
        head = [
            f"{len(rows)} of {len(history)} run(s)" if self.filter else f"{len(history)} run(s)"
        ]
        if running:
            head.append(paint(f"{running} in flight", INFO))
        if failed:
            head.append(paint(f"{failed} failed", BAD))
        self.set_head("  ·  ".join(head) + self.head_suffix())
        self.update_detail(dash)

    def detail_text(self, dash) -> str:
        run = self.selected
        if run is None:
            hint = f" matching /{self.filter}" if self.filter else ""
            return paint(escape(self.EMPTY_DETAIL + hint), MUTED)
        return history_detail(run, dash)

    def selected_phase(self) -> str | None:
        run = self.selected
        return None if run is None else run.phase


# -- notifications tab -----------------------------------------------------
class Notifications(TableTab):
    """The "why did I get pinged" ledger, and — the point — which pings never landed.

    A telegram that failed to send is invisible everywhere else in the system:
    the sender logs it and moves on, and an unattended run then sits waiting for
    an owner who was never told. That is why a failed delivery is loud here and
    why ``F`` filters straight to it.
    """

    COLUMNS = NOTIFICATION_COLUMNS
    DETAIL_TITLE = "notification"
    MODES = NOTIFICATION_MODES

    BINDINGS = [Binding("F", "cycle_mode", "filter delivered", show=False)]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.mode = "all"

    def cycle_mode(self) -> str:
        self.mode = self.MODES[(self.MODES.index(self.mode) + 1) % len(self.MODES)]
        self._keys = []  # the row set changes wholesale
        return self.mode

    def action_cycle_mode(self) -> None:
        self.cycle_mode()
        if self._dash is not None:
            self.update(self._dash)

    def _update(self, dash) -> None:
        everything = list(dash.notifications or [])
        pairs = list(enumerate(everything))
        if self.mode == "failed":
            pairs = [(i, n) for i, n in pairs if not n.delivered]
        elif self.mode == "delivered":
            pairs = [(i, n) for i, n in pairs if n.delivered]
        pairs = [(i, n) for i, n in pairs if notification_matches(n, self.filter)]
        pairs.reverse()  # newest first; the file is append-only

        self.sync(
            [note for _, note in pairs],
            [notification_key(index) for index, _ in pairs],
            notification_row,
        )

        dropped = sum(1 for note in everything if not note.delivered)
        head = [f"{len(pairs)} of {len(everything)} ping(s)"]
        head.append(f"showing [{COLOR[ACCENT]}]{self.mode}[/] (F)")
        if dropped:
            head.append(paint(f"{dropped} NOT DELIVERED", BAD))
        self.set_head("  ·  ".join(head) + self.head_suffix())
        panel = self.detail_panel
        panel.set_class(bool(dropped), "-bad")
        self.update_detail(dash)

    def detail_text(self, dash) -> str:
        note = self.selected
        if note is not None:
            return notification_detail(note)
        try:
            exists = dash.notifications_path.exists()
        except Exception:  # noqa: BLE001
            exists = False
        if not exists:
            return paint(
                "no notifications.jsonl — nothing has pinged you from this run, "
                "or this build does not record pings.",
                MUTED,
            )
        return paint(f"nothing matches (showing {self.mode})", MUTED)

    def selected_phase(self) -> str | None:
        note = self.selected
        return None if note is None else note.phase
