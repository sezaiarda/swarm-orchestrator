"""The data tabs: Workers, History, Notifications, Runs and Shells.

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

from .. import statuses
from ..doctor import Gone
from . import data
from .campaign import campaign_of
from .dash import ParkedWorker
from .data import (
    CONTEXT_BUDGET,
    Notification,
    PhaseRun,
    SlotView,
    fmt_ago,
    fmt_clock,
    fmt_duration,
    eta_runs_of,
    fmt_phase_eta,
    fmt_stamp,
    load_attempts,
    load_notes,
    phase_eta,
)
from .theme import (
    ACCENT,
    BAD,
    COLOR,
    INFO,
    MUTED,
    OK,
    WARN,
    YOU,
    Body,
    Panel,
    bar,
    field,
    glyph,
    meter_state,
    paint,
    rows as join_rows,
    section,
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


# -- fitting columns to the width ------------------------------------------
#: Cells ``DataTable`` spends around every column (one padding cell each side).
CELL_PAD = 2
#: Kept back for the vertical scrollbar, so a full table never scrolls sideways.
SCROLLBAR = 2


def fit_columns(widths, priorities, avail: int, flex: int | None = None,
                flex_min: int = 16, pad: int = CELL_PAD) -> tuple[tuple[int, ...], int | None]:
    """``(shown column indexes, flex column width)`` for ``avail`` cells.

    Horizontal scrolling in a terminal table is where information goes to die:
    the columns off the right edge are never looked at, and the one that says
    what happened is usually the last. So columns are *dropped*, least useful
    first — a higher ``priorities`` number goes earlier, ``0`` never goes, and
    among equals the rightmost goes first — until the rest fit. ``flex`` (the
    prose column) is then given every cell left over, never fewer than
    ``flex_min``: at 80 columns a recap should get 30 cells, not be cut to the
    58 it was designed for at 140 and pushed off screen.
    """
    widths = list(widths)
    prios = list(priorities) + [0] * (len(widths) - len(priorities))
    shown = list(range(len(widths)))

    def need(cols) -> int:
        return sum((flex_min if i == flex else widths[i]) + pad for i in cols)

    for i in sorted((i for i in shown if prios[i] > 0), key=lambda i: (-prios[i], -i)):
        if need(shown) <= avail:
            break
        shown.remove(i)
    flex_w = None
    if flex is not None and flex in shown:
        fixed = sum(widths[i] + pad for i in shown if i != flex)
        flex_w = max(flex_min, avail - fixed - pad)
    return tuple(shown), flex_w


# -- workers ---------------------------------------------------------------
WORKER_COLUMNS: tuple[tuple[str, int], ...] = (
    ("", 2),
    ("slot", 4),
    ("phase", 20),
    ("live", 9),
    ("model", 8),
    ("elapsed", 8),
    ("eta", 12),
    ("context", 15),
    ("+", 4),
    ("~", 4),
    ("branch", 26),
)
#: What goes first when the terminal narrows: the branch name (it is the phase
#: name again), then the git counts, the ETA and the elapsed clock. Slot, phase,
#: liveness and context are the row.
WORKER_PRIORITY = (0, 0, 0, 0, 1, 2, 3, 1, 4, 4, 5)

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


def parked_of(entry) -> ParkedWorker | None:
    """The parked worker a Workers row is of (a :meth:`Dash.parked_rows` entry,
    or the worker alone), or ``None`` for a slot's row."""
    if isinstance(entry, (list, tuple)) and entry:
        entry = entry[0]
    return entry if isinstance(entry, ParkedWorker) else None


def unpack_parked_row(entry) -> tuple[ParkedWorker, str, str, float | None, object]:
    """Normalise one :meth:`Dash.parked_rows` entry, as :func:`unpack_slot_row`
    does a slot's. A worker alone is one no probe has looked at yet."""
    items = tuple(entry) if isinstance(entry, (list, tuple)) else (entry,)
    items = items + (None,) * 5
    worker, status, waiting, ctx, pane = items[:5]
    if not isinstance(ctx, (int, float)) or isinstance(ctx, bool):
        ctx = None
    return worker, str(status or "unknown"), str(waiting or ""), ctx, pane


def worker_key(entry) -> str:
    """Stable row key: a slot's identity is its number, whatever is in it. A
    parked worker holds no slot, and is its phase."""
    worker = parked_of(entry)
    if worker is not None:
        return f"parked-{worker.phase}"
    slot, *_ = unpack_slot_row(entry)
    return f"slot-{slot.id}"


def worker_phase(entry) -> str | None:
    """The phase a Workers row shows, in a slot or in a window of its own."""
    worker = parked_of(entry)
    if worker is not None:
        return worker.phase
    return unpack_slot_row(entry)[0].phase


def worker_status(entry) -> str:
    """The ``live`` status of a Workers row, a slot's or a parked worker's."""
    if parked_of(entry) is not None:
        return unpack_parked_row(entry)[1]
    return unpack_slot_row(entry)[1]


def parked_workers(dash) -> list[ParkedWorker]:
    """The parked workers at work on the owner's answer (``Dash.working_parked``)."""
    return list(getattr(dash, "working_parked", None) or ())


def parked_rows(dash) -> list[tuple]:
    """Every parked worker, asking or at work, joined with the live probes
    (:meth:`Dash.parked_rows`)."""
    rows = getattr(dash, "parked_rows", None)
    return list(rows()) if callable(rows) else []


def parked_gone(dash, entry) -> Gone | None:
    """What the dashboard's probe found missing of a parked worker's session
    (``Dash.parked_gone``): ``None`` while it is there, and for a slot's row."""
    worker = parked_of(entry)
    if worker is None:
        return None
    return (getattr(dash, "parked_gone", None) or {}).get(worker.phase)


def parked_where(worker: ParkedWorker) -> str:
    """Where a parked worker is, in the one sentence every detail says."""
    doing = "works on your answer" if worker.answered else "waits on your answer"
    return f"{doing} in tmux window {worker.window}, and holds no slot"


def parked_line(worker: ParkedWorker, gone: Gone | None = None) -> str:
    """A parked worker in the one line every detail says: where it works, or,
    of one that is gone, what is missing and what settles it.

    The sweep is named only where the running supervisor's settles the session
    by itself (``gone.kept`` is ``None``). Otherwise the reason nothing does is
    repeated as it was found, and nothing more is promised.
    """
    if gone is None:
        return paint(escape(f"parked: it {parked_where(worker)}"), INFO)
    if gone.kept is None:
        after = ("The supervisor's sweep settles it on its second sighting: its work is kept,"
                 " and the phase is started again by the usual rules.")
    else:
        after = f"Nothing settles it: {gone.kept}."
    return paint(
        escape(f"✖ WORKER GONE — {gone.what}. The swarm still counts {worker.phase} as"
               f" {'running' if worker.answered else 'asking you'}, and it does no work"
               f" until this is settled. {after}"),
        BAD,
    )


def context_cell(ctx: float | None, m=None) -> str:
    """Context as tokens against the ~300K budget when measured, else the pane's %."""
    tokens = getattr(m, "context_tokens", None)
    if tokens:
        share = tokens / CONTEXT_BUDGET
        state = BAD if share >= 1 else (WARN if share >= 0.8 else OK)
        return paint(f"{tokens / 1000:>4.0f}k {bar(min(1.0, share), 1, 8)}", state)
    if ctx is None:
        return paint("—", MUTED)
    return paint(f"{ctx:3.0f}% {bar(ctx, 100, 8)}", meter_state(ctx))


def model_cell(model: str | None, own: bool = True) -> str:
    """The model a worker runs on: muted when it is the swarm's own, marked
    when the row named another (it may hand the phase back)."""
    if not model:
        return paint("—", MUTED)
    return cell(model, 8, MUTED if own else INFO)


def worker_row(entry, repo=None, meter=None, history=None, gone=None,
               model: tuple[str, bool] | None = None) -> tuple[str, ...]:
    """One Workers row, one cell per :data:`WORKER_COLUMNS` entry.

    ``model`` is ``(name, whether it is the swarm's own)`` for the row's phase.

    ``gone`` gets three separate tells — the marker glyph, the phase turning red
    and the word in caps — because it is the failure mode that otherwise looks
    identical to a healthy run and the one this tab exists to surface.

    A parked worker, asking or at work on the owner's answer, is a row like a
    slot's worker's, with no slot number. One whose session is gone (``gone``,
    from :func:`parked_gone`) gets the same three tells.
    """
    worker = parked_of(entry)
    if worker is not None:
        _, status, _, ctx, _ = unpack_parked_row(entry)
        live = "GONE" if gone else status
        state = BAD if gone else token(live)
        over = phase_eta(history or [], worker.elapsed_s)[1]
        commits = getattr(repo, "commits", None)
        dirty = getattr(repo, "dirty", 0) or 0
        return (
            paint("✖", BAD) if gone else glyph(live),
            "—",
            cell(worker.phase, 20, BAD if gone else None),
            paint(live, state),
            model_cell(*(model or (None,))),
            cell(fmt_duration(worker.elapsed_s), 8, elapsed_state(worker.elapsed_s)),
            cell(fmt_phase_eta(history or [], worker.elapsed_s), 12, WARN if over else MUTED),
            context_cell(ctx, meter),
            paint("—" if commits is None else str(commits), MUTED if not commits else OK),
            paint(str(dirty) if dirty else "—", WARN if dirty else MUTED),
            cell(worker.branch or "—", 26, MUTED),
        )
    slot, status, waiting, ctx, pane = unpack_slot_row(entry)
    gone = status == GONE
    live = status
    if gone:
        live = "GONE"
    elif status == "waiting" and waiting:
        live = "waiting"
    state = token(live)

    context = context_cell(ctx, meter)
    over = phase_eta(history or [], slot.elapsed_s if slot.busy else None)[1]
    left = fmt_phase_eta(history or [], slot.elapsed_s) if slot.busy else "—"

    commits = getattr(repo, "commits", None)
    dirty = getattr(repo, "dirty", 0) or 0
    return (
        paint("✖", state) if gone else glyph(live),
        str(slot.id) if slot.id >= 0 else "—",
        cell(slot.label or "—", 20, BAD if gone else (None if slot.busy else MUTED)),
        paint(live, state),
        model_cell(*(model or (None,))) if slot.busy else paint("—", MUTED),
        cell(fmt_duration(slot.elapsed_s) if slot.busy else "—", 8,
             elapsed_state(slot.elapsed_s if slot.busy else None)),
        cell(left, 12, WARN if over else MUTED),
        context,
        paint("—" if commits is None else str(commits), MUTED if not commits else OK),
        paint(str(dirty) if dirty else "—", WARN if dirty else MUTED),
        cell(slot.branch or "—", 26, MUTED),
    )


def worker_model(dash, phase: str | None) -> tuple[str, bool] | None:
    """``(model, whether it is the swarm's own)`` for ``phase``'s worker, from
    what the dashboard read of the ledger (``Dash.row_models``, ``Dash.own_model``)."""
    if not phase:
        return None
    named = (getattr(dash, "row_models", None) or {}).get(phase)
    if named:
        return named, False
    own = getattr(dash, "own_model", "") or ""
    return (own, True) if own else None


OWNER_DECISION = "owner_decision"


def _note_block(notes: list, limit: int, width: int, stamp) -> list[str]:
    """A phase's recorded calls: the owner's answers first, then its own.

    Two headings because they are two different things — what the owner decided
    when asked, and what the worker decided without asking — and the history is
    only worth reading if the two never blur.
    """
    out: list[str] = []
    owner = [n for n in notes if n.kind == OWNER_DECISION]
    own = [n for n in notes if n.kind != OWNER_DECISION]
    for title, group, head, colour in (
        ("owner decisions", owner, YOU, lambda k: YOU),
        ("decisions it made on its own", own, None,
         lambda k: WARN if k in ("risk", "assumption") else INFO),
    ):
        if not group:
            continue
        out.append(section(title, len(group), head))
        for note in group[-limit:]:
            label = "you" if note.kind == OWNER_DECISION else note.kind
            out.append(
                f"  [{COLOR[MUTED]}]{stamp(note.ts)}[/]  "
                f"{paint(f'{escape(label):<10}', colour(note.kind))} {escape(clip(note.text, width))}"
            )
    return out


def _meter_lines(dash, phase: str | None, ctx: float | None) -> list[str]:
    """A worker's context, spend and effort, from its meters file (else the pane's %)."""
    lines: list[str] = []
    m = (getattr(dash, "meters", None) or {}).get(phase or "")
    if m is not None and m.context_tokens:
        w = m.context_window or 0
        window = f" of {w / 1e6:.1f}M" if w >= 1e6 else (f" of {w / 1000:.0f}k" if w else "")
        lines.append(field("context", f"{m.context_tokens / 1000:.0f}k{window}",
                           state=BAD if m.context_tokens >= CONTEXT_BUDGET else None))
        if m.peak_tokens:
            note = (" — past the ~300k budget: this phase may be too big for one worker"
                    if m.peak_tokens >= CONTEXT_BUDGET else "")
            lines.append(field("peak", f"{m.peak_tokens / 1000:.0f}k{note}",
                               state=WARN if note else None))
    elif ctx is not None:
        lines.append(
            field("context", f"{ctx:.0f}% {bar(ctx, 100, 16)}", state=meter_state(ctx))
        )
    if m is not None and m.cost_usd is not None:
        burn = f" · ≈${m.burn_per_h:.2f}/h" if m.burn_per_h is not None else ""
        lines.append(field("spend", f"≈${m.cost_usd:.2f} API-equivalent{burn}"))
    if m is not None and m.effort:
        lines.append(field("effort", m.effort))
    return lines


def _record_lines(dash, phase: str | None) -> list[str]:
    """What a live worker has said so far: its recap, then its recorded calls."""
    lines: list[str] = []
    recap = (dash.recaps or {}).get(phase or "")
    if recap is not None and recap.summary:
        lines.append(section("recap"))
        lines.append(escape(clip(recap.summary, 600)))
    notes = (dash.notes or {}).get(phase or "") or []
    lines.extend(_note_block(notes, 6, 140, fmt_clock))
    return lines


def _work_lines(dash, phase: str | None, elapsed_s: float | None, ctx: float | None,
                waiting: str, pane_id: str | None) -> list[str]:
    """A live worker past where it is: its eta and meters, what it waits for,
    its branch work, what it has said, and the last lines in its pane."""
    lines: list[str] = []
    history = eta_runs_of(dash)
    left_s, over = phase_eta(history, elapsed_s)
    if left_s is not None:
        lines.append(field("eta", fmt_phase_eta(history, elapsed_s) + " vs the typical phase",
                           state=WARN if over else None))
    lines.extend(_meter_lines(dash, phase, ctx))
    if waiting:
        lines.append(field("waiting for", escape(clip(waiting, 160)), state=WARN))

    repo = (dash.repos or {}).get(phase or "")
    if repo is not None:
        ahead = "—" if repo.commits is None else str(repo.commits)
        lines.append(field("branch work", f"{ahead} commit(s), {repo.dirty} dirty file(s)"))

    lines.extend(_record_lines(dash, phase))

    tail = (dash.tails or {}).get(pane_id or "", "")
    body = [ln for ln in tail.splitlines() if ln.strip()][-6:]
    if body:
        lines.append(section("last lines in its pane"))
        lines.extend(f"  [{COLOR[MUTED]}]{escape(clip(ln, 160))}[/]" for ln in body)
    return lines


def parked_detail(entry, dash) -> str:
    """A parked worker in full, as a slot's: the window it is in and why, then
    the pane there, what it decided and what it just printed. Of one whose
    session is gone: what is missing, and what settles it (:func:`parked_line`)."""
    worker, status, waiting, ctx, pane = unpack_parked_row(entry)
    gone = parked_gone(dash, worker)
    live = "GONE" if gone else status
    lines = [
        f"{glyph(GONE if gone else status)} [bold]{escape(worker.phase)}[/]  "
        f"{paint(live, BAD if gone else token(status))}  [{COLOR[MUTED]}]no slot[/]",
        parked_line(worker, gone),
        field("pane", escape(getattr(pane, "pane_id", "") or "—")),
        field("window", escape(worker.window)),
        field("branch", escape(worker.branch or "—")),
        field("worktree", escape(worker.worktree or "—")),
        field("started", fmt_ago(worker.started_at)),
    ]
    lines.extend(_work_lines(dash, worker.phase, worker.elapsed_s, ctx, waiting,
                             getattr(pane, "pane_id", None)))
    return join_rows(*lines)


def worker_detail(entry, dash) -> str:
    """The selected slot in full: where it is, what it decided, what it just printed.

    The pane tail is the only place a worker's *current* thought is visible; the
    recap and the notes are the only places its past ones are. Together they are
    the answer to "what is this thing actually doing", which used to require
    switching to its tmux pane and reading.

    A parked worker has no slot: its detail says which window it is in instead
    (:func:`parked_detail`).
    """
    if parked_of(entry) is not None:
        return parked_detail(entry, dash)
    slot, status, waiting, ctx, pane = unpack_slot_row(entry)
    gone = status == GONE
    head = (
        f"{glyph(status)} [bold]{escape(slot.phase or 'free')}[/]  "
        f"{paint(status, token(status))}  [{COLOR[MUTED]}]slot {slot.id}[/]"
    )
    lines = [head]
    if gone:
        lines.append(
            paint(
                "✖ PANE GONE — the swarm still counts this slot as busy, but its "
                "worker is no longer running, so the slot does no work until it is freed.",
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
    if len(slot.batch) > 1:
        lines.append(field("batch", escape(data.batch_line(
            slot.batch, getattr(dash.snapshot, "batch_done", None) or {}))))
    lines.append(field("started", fmt_ago(slot.started_at)))
    lines.extend(_work_lines(dash, slot.phase, slot.elapsed_s, ctx, waiting, slot.pane_id))
    return join_rows(*lines)


# -- history ---------------------------------------------------------------
HISTORY_COLUMNS: tuple[tuple[str, int], ...] = (
    ("", 2),
    ("phase", 20),
    ("campaign", 12),
    ("status", 14),
    ("started", 13),
    ("took", 8),
    ("what it did", 58),
)
#: The recap is the reason this tab exists, so it is the column that flexes and
#: never goes; the campaign (it is the phase's prefix) and the start time go first.
HISTORY_PRIORITY = (0, 0, 3, 0, 2, 1, 0)
HISTORY_FLEX = 6


def history_key(run: PhaseRun) -> str:
    """Stable across refreshes: a run is its phase plus when it started.

    Not the row index — History is newest-first, so every completion shifts the
    whole table down and an index-restored cursor would land on a different run
    than the one the owner selected.
    """
    stamp = run.started_at if run.started_at is not None else run.ended_at
    return f"{run.phase}@{0.0 if stamp is None else stamp:.0f}"


def history_status(run: PhaseRun) -> str:
    """``running`` only while its worker is at work: a busy slot holds the phase,
    or it is parked and at work on the owner's answer in a window of its own.
    Else how it is held or how it ended, in words (``lost`` reads "worker gone")."""
    if run.running:
        return "running"
    return run.hold or data.run_word(run.status) or "?"


def history_row(run: PhaseRun, recap_w: int = 58) -> tuple[str, ...]:
    """One History row. The last column is why this tab exists: the recap."""
    status = history_status(run)
    state = INFO if status == "running" else token(status)
    recap = run.summary or run.note
    return (
        glyph("running" if status == "running" else status),
        cell(run.phase, 20),
        cell(campaign_of(run.phase or ""), 12, MUTED),
        paint(clip(status, 14), state),
        cell(fmt_stamp(run.started_at), 13, MUTED),
        cell(fmt_duration(run.duration_s), 8),
        cell(recap or "— no recap recorded —", recap_w, None if recap else MUTED),
    )


def _ended_line(run: PhaseRun) -> str:
    """Why a run is not running although it never said ``swarm done``."""
    why = data.ENDED_WHY.get(run.why, run.why)
    if run.status == statuses.LEDGER:
        tail = f" (here, {why})" if why else ""
        return ("the ledger has ticked it since — it was finished on another machine"
                f" or by hand, and this swarm holds no report of it{tail}")
    if run.status == data.LOST:
        return f"its worker ended without a report: {why or 'no slot holds it'}"
    return ""


def history_detail(run: PhaseRun, dash) -> str:
    """Everything on disk about one run — the "what did that worker do" answer.

    Four independent sources are joined here because each holds something the
    others do not: the generated recap (what it says it did), the sentinel note
    (what it wrote at the moment it finished), ``swarm note`` entries (calls it
    made without asking) and ``done/<phase>.jsonl`` (that it took four goes).
    """
    status = history_status(run)
    lines = [
        f"{glyph(status)} [bold]{escape(run.phase)}[/]  {paint(status, token(status))}"
        f"  [{COLOR[MUTED]}]took[/] {fmt_duration(run.duration_s)}",
    ]
    camp = campaign_of(run.phase or "")
    # Running, but in no slot: parked, and at work on the owner's answer.
    where = next((w for w in parked_workers(dash) if run.running and w.phase == run.phase), None)
    meta = [f"{fmt_stamp(run.started_at)} → {fmt_stamp(run.ended_at)}", f"campaign {camp}"]
    if run.slot and where is None:
        meta.append(f"slot {run.slot}")
    if run.parked:
        meta.append("parked")
    lines.append(paint(escape(" · ".join(meta)), MUTED))
    if where is not None:
        lines.append(parked_line(where, parked_gone(dash, where)))
    ended = _ended_line(run)
    if ended:
        lines.append(paint(escape(ended), WARN if run.status == data.LOST else MUTED))
    what = (getattr(dash, "campaign_what", None) or {}).get(camp)
    if what:
        lines.append(paint(escape(clip(what, 100)), MUTED))

    if run.summary:
        lines.append(section("recap"))
        lines.append(escape(run.summary.strip()))
    if run.note:
        lines.append(section("what it wrote when it finished"))
        lines.append(escape(run.note.strip()))
    if not run.summary and not run.note:
        lines.append("\n" + paint("no recap on disk — press r to ask a live worker for one", MUTED))

    try:
        notes = load_notes(dash.notes_dir, run.phase)
    except Exception:  # noqa: BLE001 - a detail pane never costs more than itself
        notes = []
    lines.extend(_note_block(notes, 10, 200, fmt_stamp))

    try:
        attempts = load_attempts(dash.cfg.done_dir, run.phase)
    except Exception:  # noqa: BLE001
        attempts = []
    if attempts:
        lines.append(section("attempts", len(attempts)))
        for att in attempts[-8:]:
            status_ = str(att.get("status") or "?")
            body = str(att.get("note") or att.get("summary") or "")
            lines.append(
                f"  [{COLOR[MUTED]}]{fmt_stamp(data.coerce_ts(att.get('ts')))}[/]  "
                f"{glyph(status_)} {paint(f'{escape(status_):<9}', token(status_))} "
                f"{escape(clip(body, 160))}"
            )
    return join_rows(*lines)


# -- notifications ---------------------------------------------------------
NOTIFICATION_COLUMNS: tuple[tuple[str, int], ...] = (
    ("", 2),
    ("time", 13),
    ("kind", 14),
    ("phase", 16),
    ("message", 56),
)
NOTIFICATION_PRIORITY = (0, 1, 2, 0, 0)
NOTIFICATION_FLEX = 4

#: Cycled with `F`. "failed" is first after "all" because a dropped ping is the
#: reason to ever open this tab: it is how an unattended run goes wrong silently.
NOTIFICATION_MODES = ("all", "failed", "delivered")


def notification_key(index: int) -> str:
    """File order. The log is append-only, so an existing line's index never moves."""
    return f"n{index}"


def notification_row(note: Notification, message_w: int = 56) -> tuple[str, ...]:
    """One Notifications row. A failed delivery is red in three columns; a
    message the swarm chose not to send is a muted ``·``. Which code sent it
    stays in the log: the owner reads what happened, not where."""
    bad = BAD if note.dropped else None
    glyph = "·" if note.suppressed else ("✓" if note.delivered else "✗")
    return (
        paint(glyph, MUTED if note.suppressed else (OK if note.delivered else BAD)),
        cell(fmt_stamp(note.ts), 13, MUTED),
        cell(note.kind or "—", 14, bad),
        cell(note.phase or "—", 16),
        cell(note.text or "—", message_w, bad),
    )


def notification_detail(note: Notification) -> str:
    """One ping in full. A failure leads with the error, not with the message."""
    if note.suppressed:
        head = paint(f"· not sent to your phone — {escape(note.suppressed)}", MUTED)
    elif note.delivered:
        head = paint("✓ delivered", OK)
    else:
        head = paint("✗ NOT DELIVERED — this ping never reached you", BAD)
    lines = [
        head,
        field("kind", escape(note.kind or "—")),
        field("phase", escape(note.phase or "—")),
        field("sent", f"{fmt_stamp(note.ts)}  ({fmt_ago(note.ts)})"),
    ]
    if note.error:
        lines.append(section("error", state=BAD))
        lines.append(paint(escape(note.error), BAD))
    lines.append(section("message"))
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
    #: Per column: ``0`` never dropped, higher numbers dropped first as the tab
    #: narrows (:func:`fit_columns`). Empty means every column stays.
    PRIORITY: tuple[int, ...] = ()
    #: The prose column that takes whatever width is left, or ``None``.
    FLEX: int | None = None
    FLEX_MIN = 16
    DETAIL_TITLE = "detail"

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("enter", "open_detail", "detail"),
    ]

    # The table sizes to its rows and the detail takes the rest. It used to be
    # the other way round, so four workers sat in a screen-tall table over a
    # detail pane squeezed to a scrolling slit — while the detail is the part
    # with the words in it.
    DEFAULT_CSS = """
    TableTab { layout: vertical; height: 1fr; }
    TableTab > .tab-head { height: 1; padding: 0 1; color: #8b949e; }
    TableTab > DataTable {
        height: auto; max-height: 50%; min-height: 3;
        background: #161b22; margin: 0 1;
    }
    TableTab > DataTable > .datatable--header { background: #1c2330; color: #e6edf3; }
    TableTab > DataTable > .datatable--even-row { background: #182029; }
    TableTab > .detail-pane { height: 1fr; min-height: 5; overflow-y: auto; margin: 1 1 0 1; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rows: list = []
        self._keys: list[str] = []
        self._cells: list[tuple[str, ...]] = []
        self._dash = None
        self._shown: tuple[int, ...] = tuple(range(len(self.COLUMNS)))
        self._flex_w: int | None = None

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

    # -- width ------------------------------------------------------------
    @property
    def flex_width(self) -> int:
        """The prose column's width right now (its designed width until laid out)."""
        if self._flex_w is not None:
            return self._flex_w
        return self.COLUMNS[self.FLEX][1] if self.FLEX is not None else 0

    def on_resize(self, event) -> None:
        self.relayout(event.size.width)

    def relayout(self, width: int) -> None:
        """Re-pick the columns for ``width``; rebuild the table only if they moved.

        A hidden tab has no width, and laying it out at zero would drop every
        droppable column until it is next shown — so zero is ignored.
        """
        if width <= 0 or not self.COLUMNS:
            return
        widths = [w for _, w in self.COLUMNS]
        # The table's 1-cell side margins, then the scrollbar.
        shown, flex_w = fit_columns(widths, self.PRIORITY or (), width - 2 - SCROLLBAR,
                                    self.FLEX, self.FLEX_MIN)
        if (shown, flex_w) == (self._shown, self._flex_w):
            return
        self._shown, self._flex_w = shown, flex_w
        try:
            table = self.table
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        # Clearing the columns resets the cursor, and a tab's first resize comes
        # *after* a drill-in has put it on a row — so carry the row across.
        row = table.cursor_row
        keep = self._keys[row] if 0 <= row < len(self._keys) else None
        table.clear(columns=True)
        for i in shown:
            label, w = self.COLUMNS[i]
            table.add_column(label, width=flex_w if i == self.FLEX and flex_w else w)
        self._keys, self._cells = [], []  # force the rebuild path
        if self._dash is not None:
            self.update(self._dash)
        if keep in self._keys:
            table.move_cursor(row=self._keys.index(keep))

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
        shown = self._shown
        rendered = []
        for row in rows:
            full = tuple(str(value) for value in cells(row))
            rendered.append(tuple(full[i] for i in shown if i < len(full)))
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

    def detail_title(self) -> str:
        """What the selected row is called, on the detail it opens."""
        return self.DETAIL_TITLE

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
        """Put the cursor on ``phase``, for a drill-in from another screen."""
        for index, row in enumerate(self.rows):
            if getattr(row, "phase", None) == phase:
                try:
                    self.table.move_cursor(row=index)
                    self.table.focus()
                except Exception:  # noqa: BLE001 - not mounted yet
                    pass
                return

    # -- keys -------------------------------------------------------------
    def on_data_table_row_highlighted(self) -> None:
        # Only ever repaints the Static. Rebuilding here would move the cursor,
        # which fires this again — an event storm that wedges the UI.
        self.update_detail()

    def action_cursor_down(self) -> None:
        self.table.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.table.action_cursor_up()

    def action_open_detail(self) -> None:
        if self._dash is None or self.selected is None:
            return
        try:
            body = self.detail_text(self._dash)
        except Exception as exc:  # noqa: BLE001
            body = paint(f"detail unavailable: {escape(str(exc))}", BAD)
        self.post_message(OpenDetail(self.detail_title(), body, self.selected_phase()))


# -- workers tab -----------------------------------------------------------
class Workers(TableTab):
    """One row per slot: what is in it, how far along, and whether it is alive.
    Then one per parked worker, asking the owner or at work on the answer, which
    runs in a window of its own and in no slot.

    The ``live`` column is the reason this is a table. It joins three sources the
    owner otherwise has to reconcile by hand — ``state.json`` (is the slot
    claimed), ``claude agents`` (is the agent busy, idle or waiting on an answer)
    and ``tmux`` (is anything running in the pane at all) — and a disagreement
    between them is exactly what a stalled run looks like. A parked worker's are
    the pane in its own window; whether its session is still there is what the
    dashboard's probe found of it (``Dash.parked_gone``).
    """

    COLUMNS = WORKER_COLUMNS
    PRIORITY = WORKER_PRIORITY
    DETAIL_TITLE = "slot"
    #: What the detail calls a parked worker's row: it holds no slot.
    PARKED_TITLE = "parked worker"
    EMPTY_DETAIL = "no workers yet — `swarm up` starts them"
    #: How many slots have a dead pane, and how many parked workers are gone.
    _gone: tuple[int, int] = (0, 0)

    def _update(self, dash) -> None:
        slots = list(dash.slot_rows())
        parked = parked_rows(dash)
        rows = slots + parked
        gone = [r for r in slots if unpack_slot_row(r)[1] == GONE]
        lost = [w for w in parked if parked_gone(dash, w)]
        busy = [r for r in slots if unpack_slot_row(r)[0].busy]
        waiting = [r for r in rows if worker_status(r) == "waiting"]

        self.sync(
            rows,
            [worker_key(r) for r in rows],
            lambda r: worker_row(
                r,
                (dash.repos or {}).get(worker_phase(r) or ""),
                (getattr(dash, "meters", None) or {}).get(worker_phase(r) or ""),
                eta_runs_of(dash),
                parked_gone(dash, r),
                worker_model(dash, worker_phase(r)),
            ),
        )

        head = [f"{len(busy)}/{len(slots)} slots busy"]
        if len(parked) > len(lost):
            head.append(paint(f"{len(parked) - len(lost)} in own window", INFO))
        if waiting:
            head.append(paint(f"{len(waiting)} waiting on you", WARN))
        if gone:
            head.append(paint(f"{len(gone)} PANE GONE", BAD))
        if lost:
            head.append(paint(f"{len(lost)} PARKED WORKER GONE", BAD))
        self.set_head("  ·  ".join(head))
        # The border is the part visible without reading anything: a dead pane,
        # or a parked worker that is gone, turns the whole panel red from across
        # the room.
        panel = self.detail_panel
        panel.set_class(bool(gone or lost), "-bad")
        panel.set_class(bool(waiting) and not (gone or lost), "-warn")
        self._gone = (len(gone), len(lost))
        self.update_detail(dash)

    def update_detail(self, dash=None) -> None:
        """The detail pane and its border. The border says what the selected row
        is, so it is set here, where the cursor's moves arrive too. Then how many
        rows are gone: the selected kind's count reads bare, as a slot's always
        did, and the other kind is named."""
        super().update_detail(dash)
        try:
            panel = self.detail_panel
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        panes, parked = self._gone
        if parked_of(self.selected) is not None:
            counts = ((parked, "gone"), (panes, "pane gone"))
        else:
            counts = ((panes, "gone"), (parked, "parked gone"))
        said = ", ".join(f"{n} {what}" for n, what in counts if n)
        title = self.detail_title()
        set_border(panel, title=f"{title} — {said}" if said else title)

    def detail_title(self) -> str:
        """A slot's row is a slot. A parked worker holds none, and is called
        what it is."""
        return self.PARKED_TITLE if parked_of(self.selected) is not None else self.DETAIL_TITLE

    def detail_text(self, dash) -> str:
        row = self.selected
        return paint(self.EMPTY_DETAIL, MUTED) if row is None else worker_detail(row, dash)

    def selected_phase(self) -> str | None:
        row = self.selected
        return None if row is None else worker_phase(row)


# -- history tab -----------------------------------------------------------
class History(TableTab):
    """Every phase ever run, newest first — the "what did that worker do" tab.

    The sentinel notes it reads have been written by every worker since the
    project started and were never once read back; they were kept only so a
    restart could tell a finished phase from an interrupted one. They are the
    worker's own account of its run, which is precisely what is wanted here.
    """

    COLUMNS = HISTORY_COLUMNS
    PRIORITY = HISTORY_PRIORITY
    FLEX = HISTORY_FLEX
    DETAIL_TITLE = "phase"
    EMPTY_DETAIL = "no phase history yet — nothing has run in this state dir"

    def _update(self, dash) -> None:
        history = list(dash.history or [])
        rows = history
        width = self.flex_width
        self.sync(rows, [history_key(run) for run in rows],
                  lambda run: history_row(run, width))

        failed = sum(1 for run in history if run.status == "fail")
        running = sum(1 for run in history if run.running)
        lost = sum(1 for run in history if run.status == data.LOST)
        head = [f"{len(history)} run(s)"]
        if running:
            head.append(paint(f"{running} in flight", INFO))
        if failed:
            head.append(paint(f"{failed} failed", BAD))
        if lost:
            head.append(paint(f"{lost} ended without a report", MUTED))
        self.set_head("  ·  ".join(head))
        self.update_detail(dash)

    def detail_text(self, dash) -> str:
        run = self.selected
        if run is None:
            return paint(escape(self.EMPTY_DETAIL), MUTED)
        return history_detail(run, dash)

    def selected_phase(self) -> str | None:
        run = self.selected
        return None if run is None else run.phase


# -- runs tab --------------------------------------------------------------
RUN_COLUMNS: tuple[tuple[str, int], ...] = (
    ("", 2),
    ("run", 17),
    ("started", 13),
    ("hours", 6),
    ("wk", 3),
    ("isolation", 9),
    ("done", 5),
    ("fail", 5),
    ("5h %/h", 7),
    ("win", 4),
    ("wk %/h", 7),
    ("$/h", 7),
)
#: Identity, size and outcome stay; the per-hour rates and config go first.
RUN_PRIORITY = (0, 0, 2, 0, 3, 4, 0, 1, 2, 5, 3, 3)


def _rate(value, width: int) -> str:
    return cell("—" if value is None else f"{value:.2f}", width, None if value is not None else MUTED)


def run_key(summary: dict) -> str:
    return str(summary.get("run_id") or "legacy")


def run_row(summary: dict) -> tuple[str, ...]:
    """One run: how long, at what config, and what it used per hour."""
    live = bool(summary.get("live") or summary.get("legacy"))
    return (
        paint("●", INFO if live else OK),
        cell(summary.get("run_id") or "legacy", 17, None if summary.get("run_id") else MUTED),
        cell(fmt_stamp(summary.get("start")), 13, MUTED),
        cell(f"{summary.get('hours') or 0:.1f}", 6),
        cell(str(summary.get("max_workers") or "?"), 3),
        cell(str(summary.get("isolation") or "?"), 9, MUTED),
        cell(str(summary.get("phases_finished", 0)), 5),
        cell(str(summary.get("phases_failed", 0)), 5, BAD if summary.get("phases_failed") else MUTED),
        _rate(summary.get("five_pct_per_h"), 7),
        cell(str(summary.get("five_windows", 0)), 4, MUTED),
        _rate(summary.get("week_pct_per_h"), 7),
        _rate(summary.get("usd_per_h"), 7),
    )


def run_detail(summary: dict) -> str:
    """The selected run in full, split per worker count if a reload moved it."""
    def rate(v) -> str:
        return "—" if v is None else f"{v:.2f}"

    head = "open run" if summary.get("live") else (
        "before runs were recorded (since the last supervisor start)" if summary.get("legacy")
        else f"closed by {summary.get('closed_by') or '?'}")
    lines = [
        field("run", escape(str(summary.get("run_id") or "legacy")) + "  " + paint(escape(head), MUTED)),
        field("span", f"{fmt_stamp(summary.get('start'))} → "
                      f"{'now' if summary.get('live') else fmt_stamp(summary.get('end'))}"
                      f"  ({summary.get('hours') or 0:.1f} h)"),
        field("5-hour", f"{rate(summary.get('five_pct_per_h'))} %/h · "
                        f"{summary.get('five_used') or 0:.0f} pts over {summary.get('five_windows', 0)} window(s)"),
        field("weekly", f"{rate(summary.get('week_pct_per_h'))} %/h · {summary.get('week_used') or 0:.0f} pts"),
        field("phases", f"{summary.get('phases_finished', 0)} finished · "
                        f"{summary.get('phases_failed', 0)} failed · {rate(summary.get('phases_per_h'))}/h"),
        field("cost", "—" if summary.get("usd") is None else
              f"${summary['usd']:.2f} · ${rate(summary.get('usd_per_h'))}/h (API-equivalent)"),
    ]
    for seg in summary.get("segments") or []:
        lines.append(field(
            f"{seg.get('max_workers')}w {seg.get('isolation')}",
            f"{seg.get('hours', 0):.1f} h · 5h {rate(seg.get('five_pct_per_h'))} %/h · "
            f"wk {rate(seg.get('week_pct_per_h'))} %/h · {seg.get('phases_finished', 0)} phase(s)"))
    lines.append(paint("account-wide figures: other sessions on the account count too", MUTED))
    return "\n".join(lines)


class Runs(TableTab):
    """Every run (``swarm up`` → ``down``, or a ``reset``) with its per-hour averages.

    The open run is on top and live; ``R`` anywhere closes it and starts another.
    """

    COLUMNS = RUN_COLUMNS
    PRIORITY = RUN_PRIORITY
    DETAIL_TITLE = "run"
    EMPTY_DETAIL = "no runs recorded yet — the next `swarm up` or `R` starts one"

    def _update(self, dash) -> None:
        cur = getattr(dash, "usage", None)
        cur = [cur | {"live": bool(getattr(dash, "run", None))}] if cur else []
        rows = cur + list(getattr(dash, "past_runs", None) or [])
        self.sync(rows, [run_key(r) for r in rows], run_row)
        self.set_head(f"{len(rows)} run(s)  ·  R resets the open run")
        self.update_detail(dash)

    def detail_text(self, dash) -> str:
        summary = self.selected
        if summary is None:
            return paint(escape(self.EMPTY_DETAIL), MUTED)
        return run_detail(summary)


# -- shells tab ------------------------------------------------------------
SHELL_COLUMNS: tuple[tuple[str, int], ...] = (
    ("", 2),
    ("name", 16),
    ("what it is for", 44),
    ("state", 6),
    ("pid", 8),
    ("age", 6),
    ("by", 20),
    ("stop it with", 30),
)
#: The name and its one line are the point; the pid goes first, then who and how.
SHELL_PRIORITY = (0, 0, 0, 0, 4, 1, 3, 2)
SHELL_FLEX = 2


def shell_row(row: dict, why_w: int = 44) -> tuple[str, ...]:
    """One kept process. Alive is green; dead, or alive past a week, is amber."""
    alive, stale = row.get("alive"), row.get("stale")
    state = WARN if stale or not alive else OK
    return (
        paint("●", state),
        cell(row.get("name"), 16),
        cell(row.get("why") or "— no why recorded —", why_w, None if row.get("why") else MUTED),
        cell(row.get("state"), 6, state),
        cell(row.get("pid") if alive else "—", 8, MUTED),
        cell(row.get("age"), 6, WARN if stale else None),
        cell(row.get("by"), 20, MUTED),
        cell(row.get("stop"), 30, ACCENT),
    )


def shell_detail(row: dict) -> str:
    """The selected kept process in full: what it runs, where, and where it logs."""
    alive = row.get("alive")
    state = f"alive, pid {row.get('pid')}" if alive else "dead — its record is all that is left"
    lines = [
        field("name", escape(str(row.get("name")))),
        field("for", escape(str(row.get("why") or "—"))),
        field("state", escape(state), state=OK if alive else WARN),
        field("age", escape(f"{row.get('age')} (since {fmt_stamp(row.get('started_at'))})"),
              state=WARN if row.get("stale") else None),
        field("started by", escape(str(row.get("by") or "?"))),
        field("command", escape(str(row.get("command") or "—"))),
        field("cwd", escape(str(row.get("cwd") or "—"))),
        field("log", escape(str(row.get("log") or "—"))),
        field("stop", paint(escape(str(row.get("stop"))), ACCENT)
              + paint("   x here (asks first)" if alive else "   x here clears the record", MUTED)),
    ]
    if row.get("stale"):
        lines.append(paint("alive for over a week — still wanted?", WARN))
    return "\n".join(lines)


class StopKept(Message):
    """``x`` on a shell: stop it — the app hands it to the command centre."""

    def __init__(self, kept: str) -> None:
        super().__init__()
        self.kept = kept


class Shells(TableTab):
    """What ``swarm keep`` left running, and the one line saying why.

    Everything else a session starts is reaped when the session ends, so these
    are the only processes the swarm leaves behind on purpose — and the only ones
    the owner has to be able to see, understand and stop without hunting through
    ``ps``. ``x`` runs ``swarm keep --stop`` through the command centre, which
    confirms first and keeps the output.
    """

    COLUMNS = SHELL_COLUMNS
    PRIORITY = SHELL_PRIORITY
    FLEX = SHELL_FLEX
    DETAIL_TITLE = "kept process"
    EMPTY_DETAIL = "nothing kept — `swarm keep --name N --why '...' -- <command>` leaves one running"

    BINDINGS = [Binding("x", "stop_kept", "stop it")]

    def _update(self, dash) -> None:
        rows = data.kept_rows(getattr(dash, "kept", None) or [])
        width = self.flex_width
        self.sync(rows, [r["name"] for r in rows], lambda r: shell_row(r, width))
        alive = sum(1 for r in rows if r["alive"])
        stale = sum(1 for r in rows if r["stale"])
        head = [f"{alive} kept process(es) running"]
        if len(rows) > alive:
            head.append(paint(f"{len(rows) - alive} dead record(s)", WARN))
        if stale:
            head.append(paint(f"{stale} older than a week", WARN))
        head.append("x stops the selected")
        self.set_head("  ·  ".join(head))
        self.update_detail(dash)

    def detail_text(self, dash) -> str:
        row = self.selected
        return paint(escape(self.EMPTY_DETAIL), MUTED) if row is None else shell_detail(row)

    def action_stop_kept(self) -> None:
        row = self.selected
        if row is not None:
            self.post_message(StopKept(row["name"]))


# -- notifications tab -----------------------------------------------------
class AckPings(Message):
    """``x`` on the alerts tab: the owner has seen the pings that never arrived."""


class Notifications(TableTab):
    """The "why did I get pinged" ledger, and — the point — which pings never landed.

    A telegram that failed to send is invisible everywhere else in the system:
    the sender logs it and moves on, and an unattended run then sits waiting for
    an owner who was never told. That is why a failed delivery is loud here and
    why ``F`` filters straight to it.
    """

    COLUMNS = NOTIFICATION_COLUMNS
    PRIORITY = NOTIFICATION_PRIORITY
    FLEX = NOTIFICATION_FLEX
    DETAIL_TITLE = "notification"
    MODES = NOTIFICATION_MODES

    BINDINGS = [
        Binding("F", "cycle_mode", "filter delivered", show=False),
        Binding("x", "ack_drops", "clear not-delivered"),
    ]

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
            pairs = [(i, n) for i, n in pairs if n.dropped]
        elif self.mode == "delivered":
            pairs = [(i, n) for i, n in pairs if n.delivered]
        pairs.reverse()  # newest first; the file is append-only

        width = self.flex_width
        self.sync(
            [note for _, note in pairs],
            [notification_key(index) for index, _ in pairs],
            lambda note: notification_row(note, width),
        )

        dropped = len(data.open_drops(everything, getattr(dash, "pings_acked_at", 0.0)))
        seen = sum(1 for note in everything if note.dropped) - dropped
        head = [f"{len(pairs)} of {len(everything)} ping(s)"]
        head.append(f"showing [{COLOR[ACCENT]}]{self.mode}[/] (F)")
        if dropped:
            head.append(paint(f"{dropped} NOT DELIVERED (x clears)", BAD))
        if seen:
            head.append(paint(f"{seen} earlier not delivered, acknowledged", MUTED))
        self.set_head("  ·  ".join(head))
        panel = self.detail_panel
        panel.set_class(bool(dropped), "-bad")
        self.update_detail(dash)

    def action_ack_drops(self) -> None:
        self.post_message(AckPings())

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
