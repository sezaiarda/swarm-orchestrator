"""The phase books: every campaign's forecast, in the words every view uses.

The home screen lists them, ``swarm status`` prints them and the web board
shows them, all from one :class:`eta.forecast.Forecast` and all through these
functions, so the three can never say different things about the same run.

Pure text: no Textual, no I/O. ``now`` is always passed in, so a line is a
function of the forecast and the clock only.
"""

from __future__ import annotations

import math

from ..eta.forecast import Book, Forecast, Range
from ..eta.plan import OWNER_RUN
from .campaign import campaign_of
from .data import bar, fmt_range, fmt_when, fmt_when_range

#: The book name column, and the waiting-on-you line's rows before "+n more".
NAME_W = 10
WAITING_NAMED = 3

#: How a row's state reads before what it waits for.
_STATE = {"running": "running", "merging": "merging", "ready": "ready to start",
          "waits": "waits", "blocked": "blocked by", "yours": "", "behind": "waits on you, behind"}


def when(ts: float, fc: Forecast, now: float) -> str:
    """A finish time, or why it has none: past a scheduled pause, or not in sight."""
    if math.isfinite(ts):
        return fmt_when(ts, now)
    return "after the scheduled pause" if fc.pause_at else "not in sight"


def span(r: Range | None, fc: Forecast, now: float) -> str:
    """P50–P85 as ``Wed 05:30–21:10``; a floor reads ``at least Wed 05:30``."""
    if r is None:
        return "waits on you"
    if fc.floor:
        return f"at least {when(r.p50, fc, now)}"
    if not math.isfinite(r.p85):
        return f"{when(r.p50, fc, now)} – {when(r.p85, fc, now)}" if math.isfinite(r.p50) \
            else when(r.p50, fc, now)
    return fmt_when_range(r.p50, r.p85, now)


def phrase(r: Range | None, fc: Forecast, now: float) -> str:
    """A finish as a phrase of its own: ``done ~Wed 05:30–21:10``."""
    if r is None or fc.floor:
        return span(r, fc, now)
    return f"done ~{span(r, fc, now)}"


def overall_line(fc: Forecast, now: float) -> str:
    """The headline: how many rows the swarm can do, when the last one lands,
    and how many more wait on the owner (they are not in the time)."""
    left = sum(b.left - b.behind for b in fc.books)
    held = sum(b.behind for b in fc.books)
    yours = f" · {held} more wait on you" if held else ""
    r = fc.overall
    if r is None:
        return f"nothing the swarm can do by itself is left{yours}"
    if fc.stopped and not math.isfinite(r.p50):
        tail = fc.stopped
    elif fc.stopped:
        # Work, not a clock: "done Tue" goes staler by the hour it stays paused.
        work = fmt_range(r.p50 - fc.made_at,
                         r.p85 - fc.made_at if math.isfinite(r.p85) else None)
        tail = f"{fc.stopped} · {work} of work once it runs again"
    elif fc.floor:
        tail = f"done {span(r, fc, now)} (simulating…)"
    else:
        tail = f"done ~{span(r, fc, now)}"
    return f"{left} row{'' if left == 1 else 's'} left · {tail}{yours}"


def basis_line(fc: Forecast, now: float) -> str:
    """What the forecast was made on, in words: its small print."""
    if fc.floor:
        return (f"a floor: the longest chain of rows, or the work over {fc.workers} "
                f"worker{'' if fc.workers == 1 else 's'}, whichever is longer")
    parts = [f"simulated {fc.runs}×", f"{fc.workers} worker{'' if fc.workers == 1 else 's'}"]
    if fc.build_slots:
        parts.append(f"{fc.build_slots} build slot{'' if fc.build_slots == 1 else 's'}")
    if fc.working < 0.995:
        parts.append(f"working {fc.working:.0%} of the time, as lately")
    for window, at, pct in fc.caps:
        name = "weekly" if window == "week" else "5-hour"
        parts.append(f"{name} cap pauses at {at:g}% ({pct:.0f}% now)")
    if fc.held_until:
        parts.append(f"held by the cap until {fmt_when(fc.held_until, now)}")
    if fc.pause_at:
        parts.append(f"pauses {fmt_when(fc.pause_at, now)}")
    return " · ".join(parts)


def waiting_line(fc: Forecast) -> str:
    """What waits on the owner and what is stuck behind it; "" when nothing is.

    ``waiting on you: name-W7 holds 25 rows (name, readme), lilac-P1 holds 7
    (vault) · 20 more yours to do``. A reason is said only when it is not the
    usual one, an owner-run row."""
    holding = sorted((s for s in fc.stuck if s.behind), key=lambda s: -len(s.behind))
    idle = [s for s in fc.stuck if not s.behind]
    if not holding and not idle:
        return ""
    named = []
    for s in holding[:WAITING_NAMED]:
        camps = list(dict.fromkeys(campaign_of(r) for r in s.behind))
        where = ", ".join(camps[:2]) + (", …" if len(camps) > 2 else "")
        why = "" if s.why == OWNER_RUN else f" ({s.why.split(';')[0]})"
        named.append(f"{s.row}{why} holds {len(s.behind)} row{'' if len(s.behind) == 1 else 's'}"
                     f" ({where})")
    rest = holding[WAITING_NAMED:]
    if rest:
        named.append(f"{len(rest)} more hold {sum(len(s.behind) for s in rest)}")
    line = "waiting on you: " + ", ".join(named) if named else "waiting on you:"
    if idle:
        line += f"{' ·' if named else ''} {len(idle)} {'more ' if named else ''}yours to do"
    return line


def book_line(b: Book, fc: Forecast, now: float, width: int = 76) -> str:
    """One phase book in a line: name, done/total, bar, left, running/ready, P50–P85."""
    count = f"{b.done:>3}/{b.total:<3}"
    doing = []
    if b.running:
        doing.append(f"{b.running} running")
    if b.ready:
        doing.append(f"{b.ready} ready")
    if b.dated:
        doing.append(f"{b.dated} wait{'s' if b.dated == 1 else ''} for a date")
    if b.behind:
        doing.append(f"{b.behind} wait on you")
    tail = f"{b.left:>3} left · {', '.join(doing) or 'blocked'} · {span(b.finish, fc, now)}"
    room = max(4, width - NAME_W - len(count) - len(tail) - 4)
    return (f"{b.name[:NAME_W]:<{NAME_W}} {count} {bar(b.done, max(1, b.total), min(12, room))}"
            f"  {tail}")


def detail(b: Book, fc: Forecast, now: float) -> str:
    """Everything about one phase book, for its detail view."""
    lines = [f"{b.name}: {b.done} of {b.total} done, {b.left} left"
             + (f", {b.behind} of them waiting on you" if b.behind else "")]
    if b.finish is not None and not fc.floor:
        r = b.finish
        lines.append(f"finishes ~{when(r.p50, fc, now)} (half the time sooner), likely by "
                     f"{when(r.p85, fc, now)}, almost surely by {when(r.p95, fc, now)}")
    elif b.finish is not None:
        lines.append(f"finishes {span(b.finish, fc, now)} (still simulating)")
    else:
        lines.append("nothing in it can move until you act")
    if b.grows:
        lines.append(f"expects about {b.grows:g} follow-up row{'' if b.grows == 1 else 's'} "
                     "to be filed on the way, as its campaigns lately have")
    lines += ["", "rows left, in ledger order:"]
    width = max((len(r.id) for r in b.rows), default=8)
    for r in b.rows:
        what = f"{_STATE.get(r.state, r.state)} {r.why}".strip()
        eta = ""
        if r.finish is not None and not fc.floor:
            eta = f"  ~{when(r.finish.p50, fc, now)}, by {when(r.finish.p95, fc, now)} at worst"
        lines.append(f"  {r.id:<{width}}  {what}{eta}")
    return "\n".join(lines)


def critical_line(fc: Forecast) -> str:
    """The chain that sets the overall finish, and why its first row starts when
    it does when that is a date: ``billing-P3 (waits until 2026-10-01)``."""
    views = {r.id: r for b in fc.books for r in b.rows}
    first = views.get(fc.critical[0]) if fc.critical else None
    why = f" ({_STATE['waits']} {first.why})" if first is not None and first.state == "waits" \
        else ""
    return " → ".join(fc.critical) + why


def status_lines(fc: Forecast, now: float) -> list[str]:
    """``swarm status``'s forecast: the headline, its basis, what waits on the
    owner, the critical chain and one line per phase book."""
    out = [f"eta: {overall_line(fc, now)}", f"  basis: {basis_line(fc, now)}"]
    waiting = waiting_line(fc)
    if waiting:
        out.append(f"  {waiting}")
    if fc.critical and not fc.floor:
        out.append(f"  critical path: {critical_line(fc)}")
    out += [f"  {book_line(b, fc, now, 96)}" for b in fc.books]
    return out
