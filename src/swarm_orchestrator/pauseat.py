"""``swarm pause --in 12h`` / ``--at 03:00``: a pause that happens later.

The schedule is one timestamp in ``state.json`` (``State.pause_at``, 0 = none),
so it survives ``swarm down`` / ``swarm up``. When it comes, the supervisor does
what ``swarm pause`` does — sets ``paused``, so no new worker launches and the
running ones finish — and drops the schedule. Its next wake-up counts the moment
in, so it fires on time; one that passed while the supervisor was down fires on
its first wake. ``swarm resume`` and ``swarm pause --cancel`` drop it, and a new
one replaces it: there is only ever one.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

from .usage import _clock

_DURATION = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?")
_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


def parse_in(text: str) -> float:
    """Seconds in ``12h``, ``90m``, ``1h30m``, ``2d`` (days, hours, minutes, in
    that order). Raises ``ValueError`` in words the owner can act on."""
    m = _DURATION.fullmatch(text.strip().lower())
    if not m or not any(m.groups()):
        raise ValueError(f"{text!r} is not a duration; give one like 12h, 90m, 1h30m or 2d")
    days, hours, minutes = (int(g or 0) for g in m.groups())
    seconds = days * 86400 + hours * 3600 + minutes * 60
    if not seconds:
        raise ValueError(f"{text!r} is no time at all; for a pause now, run `swarm pause`")
    return float(seconds)


def next_at(text: str, now: float) -> float:
    """The next time the local clock reads ``HH:MM``: today if still ahead, else tomorrow."""
    m = _HHMM.fullmatch(text.strip())
    hour, minute = (int(g) for g in m.groups()) if m else (-1, -1)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"{text!r} is not a time of day; give one like 03:00 or 23:30")
    at = datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at.timestamp() <= now:
        at += timedelta(days=1)
    return at.timestamp()


def left(seconds: float) -> str:
    """``11h 58m``, ``1d 2h``, ``7m``: how far off, to the minute."""
    minutes = int(max(0.0, seconds) // 60)
    days, rem = divmod(minutes, 1440)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else "under a minute"


def when(pause_at: float, now: float) -> str:
    """``03:12 (in 11h 58m)``; a moment already past reads as due now."""
    due = "due now" if pause_at <= now else f"in {left(pause_at - now)}"
    return f"{_clock(pause_at, now)} ({due})"


def line(pause_at: float, now: float | None = None) -> str:
    """The schedule in plain English, for ``swarm status`` and the dashboard."""
    if not pause_at:
        return ""
    now = time.time() if now is None else now
    return f"pause scheduled: {when(pause_at, now)} — swarm pause --cancel to drop it"


def stamp(ts: float) -> str:
    """The moment as the supervisor log writes it."""
    return time.strftime("%Y-%m-%dT%H:%M", time.localtime(ts))
