"""The home screen's alerts & notifications box, and its shells box.

This box is one place for everything that wants attention: what waits on
them, what is wrong right now, and every ping the swarm sent or held, newest
first, one line each, with a mark that says which is which:

* ``◆`` needs you (a worker's question, a parked phase, a held merge…);
* ``✗`` / ``▲`` wrong now (the footer's problems, a failing doctor check);
* ``✓`` a ping that reached the phone, ``·`` one the swarm chose to hold,
  ``✗`` one that never arrived (grey once acknowledged with ``x``).

Everything here is computed from what ``Dash`` already holds; the problems are
the same list the footer draws from (:func:`problems`), so the two cannot
disagree. The alerts tab keeps the full log with its filter and detail.
"""

from __future__ import annotations

import time

from rich.markup import escape

from .. import telegram
from ..drain import line as drain_line
from ..pauseat import line as pause_line
from .. import restart as restart_mod
from .data import fmt_clock, fmt_duration, fmt_stamp, held_merge, open_drops
from .shell import STALE_BAD_S, STALE_WARN_S
from .theme import BAD, BRIGHT, GLYPH, MUTED, OK, SOFT, WARN, YOU, paint


#: Ping kinds that ask something of the owner, and kinds that report a fault.
ASKS = frozenset({"waiting", "park", "question", "needs-owner", "operator-ask",
                  "operator-todo", "operator-abandoned", "integrate-hold", "blocked"})
FAULTS = frozenset({"spawn-fail", "master-timeout", "usage-cap", "web-board", "push-owed",
                    "lane-unprepared"})

#: Rows of the ping log the box keeps; the alerts tab holds every one.
NOTE_ROWS = 60
KIND_W = 15
NAME_W = 12


def clip(text: str, width: int) -> str:
    flat = " ".join((text or "").split())
    if width <= 1:
        return ""
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def when(ts: float | None, now: float) -> str:
    """``HH:MM`` today, ``MM-DD`` past a day — five cells either way."""
    if ts is None:
        return "  —  "
    return fmt_clock(ts)[:5] if now - ts < 20 * 3600 else fmt_stamp(ts)[:5]


# -- what is wrong right now ---------------------------------------------------
def problems(dash, now: float | None = None) -> list[tuple[str, str]]:
    """``(text, state)`` per thing wrong with the run right now; ``[]`` when nothing is.

    Staleness only escalates while a slot is busy — a finished or idle swarm is
    *supposed* to be quiet. A scheduled pause is not a problem but a hold the
    owner set for later, so it is said until it happens.
    """
    now = time.time() if now is None else now
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
    dropped = open_drops(dash.notifications or [], getattr(dash, "pings_acked_at", 0.0))
    if dropped:
        parts.append((f"{len(dropped)} ping(s) never reached your phone (x clears)", BAD))
    busy = any(s.busy for s in snap.slots)
    age = None if snap.last_event_at is None else max(0.0, now - snap.last_event_at)
    if busy and age is not None and age > STALE_BAD_S:
        parts.append((f"nothing has happened for {fmt_duration(age)}", BAD))
    elif busy and age is not None and age > STALE_WARN_S:
        parts.append((f"quiet for {fmt_duration(age)}", WARN))
    if snap.ok and snap.pause_at:
        parts.append((pause_line(snap.pause_at, now), WARN))
    plan = getattr(dash, "restart", None) or {}
    if snap.ok and plan:
        # Planned or under way is a hold the owner (or a session) set; a failed
        # one is a fault, and says how the swarm was left.
        text = restart_mod.line(
            plan, *restart_mod.counts_of(getattr(dash, "_state", None) or {}), now)
        if text:
            parts.append((escape(text), BAD if plan.get("stage") == restart_mod.FAILED else WARN))
    return parts


def problem_line(text: str, state: str, width: int) -> str:
    """`` now  ✗ merging stopped: …`` — a condition, not an event: it has no time."""
    shape = GLYPH.get(state, "·")
    return f"{paint(' now ', MUTED)} {paint(shape, state)} {paint(escape(clip(text, width - 9)), state)}"


def columns(width: int) -> tuple[int, int, int]:
    """``(kind_w, name_w, text_w)`` for a row ``width`` wide.

    A narrow box drops the kind column first (the mark and its colour already
    say which sort of row it is), then shortens the name; the text keeps the rest.
    """
    kind_w = KIND_W if width >= 64 else 0
    name_w = NAME_W if width >= 44 else 8
    lead = 1 + 5 + 1 + 1 + 1  # mark, time, space, glyph, space
    used = lead + (kind_w + 1 if kind_w else 0) + name_w + 1
    return kind_w, name_w, max(6, width - used)


def _row(mark: str, ts, now: float, shape: str, shape_state: str, kind: str,
         kind_state: str, name: str, text: str, text_state: str, width: int,
         bold: bool = False) -> str:
    kind_w, name_w, text_w = columns(width)
    cell = f"{escape(clip(name or '—', name_w)):<{name_w}}"
    out = f"{mark}{paint(when(ts, now), MUTED)} {paint(shape, shape_state)} "
    if kind_w:
        out += paint(f"{clip(kind, kind_w):<{kind_w}}", kind_state) + " "
    out += (f"[b]{cell}[/b] " if bold else f"{cell} ")
    return out + paint(escape(clip(text, text_w)), text_state)


def doctor_line(check: dict, ran_at: float | None, width: int, now: float) -> str:
    """A failing or warning ``swarm doctor`` check from the last time it ran."""
    state = BAD if str(check.get("status", "")).lower() == "fail" else WARN
    return _row(" ", ran_at, now, GLYPH[state], state, "doctor", state,
                str(check.get("name") or "?"), str(check.get("detail") or ""), SOFT, width)


# -- what needs you --------------------------------------------------------------
def need_line(need, width: int, now: float, selected: bool = False) -> str:
    """``14:05 ◆ parked       P2           which schema should P2 read? (waited 3h)``"""
    name = need.ref if need.phase is None else (need.ref or need.phase)
    facts = []
    if need.since is not None:
        facts.append(f"waited {fmt_duration(max(0.0, now - need.since))}")
    if need.blocks:
        facts.append(f"blocks {need.blocks}")
    tail = f" ({', '.join(facts)})" if facts else ""
    _, _, text_w = columns(width)
    question = clip(need.question or "no question text was captured",
                    max(6, text_w - len(tail))) + tail
    return _row("▸" if selected else " ", need.since, now, GLYPH[YOU], YOU, need.kind, YOU,
                name or "?", question, BRIGHT, width, bold=True)


# -- what was sent ---------------------------------------------------------------
def note_style(note, acked: float) -> tuple[str, str]:
    """``(glyph, state)`` for one ping: sent, held, or never arrived."""
    if note.suppressed:
        return "·", MUTED
    if note.delivered:
        return GLYPH[OK], OK
    return GLYPH[BAD], BAD if telegram.unacknowledged(note.ts, acked) else MUTED


def note_line(note, width: int, now: float, acked: float = 0.0,
              selected: bool = False) -> str:
    """``14:05 ✓ waiting         P2           which schema should P2 read?``"""
    shape, state = note_style(note, acked)
    kind = note.kind or "ping"
    kind_state = YOU if kind in ASKS else WARN if kind in FAULTS else MUTED
    body_state = MUTED if note.suppressed else (BRIGHT if kind in ASKS else SOFT)
    return _row("▸" if selected else " ", note.ts, now, shape, state, kind, kind_state,
                note.phase or "—", note.text or "—", body_state, width)


def note_key(index: int) -> str:
    """File order, as the alerts tab keys it: an appended line never moves."""
    return f"n{index}"


def recent_notes(notifications, limit: int = NOTE_ROWS) -> list[tuple[str, object]]:
    """``(key, note)`` newest first, at most ``limit``."""
    pairs = list(enumerate(notifications or []))[-limit:]
    return [(note_key(i), n) for i, n in reversed(pairs)]


# -- shells ----------------------------------------------------------------------
def shell_lines(rows: list[dict], width: int) -> list[str]:
    """One line per kept process: ``● name  what it is for  age``."""
    if not rows:
        return [paint(clip("nothing kept running — `swarm keep` leaves one when a phase must",
                           width), MUTED)]
    name_w = max(8, min(18, max(len(str(r.get("name") or "")) for r in rows)))
    out = []
    for r in rows:
        alive, stale = r.get("alive"), r.get("stale")
        state = WARN if stale or not alive else OK
        age = str(r.get("age") or "")
        tail = f"  {age}" + ("" if alive else "  dead")
        room = max(6, width - 2 - name_w - 2 - len(tail))
        why = r.get("why") or "— no why recorded —"
        out.append(f"{paint('●', state)} {escape(clip(str(r.get('name') or '?'), name_w)):<{name_w}}  "
                   + paint(escape(clip(why, room)), SOFT if r.get("why") else MUTED)
                   + " " * max(0, room - len(clip(why, room)))
                   + paint(tail, WARN if stale or not alive else MUTED))
    return out
