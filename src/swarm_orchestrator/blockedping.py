"""One ping for a burst of ``blocked`` outcomes, grouped by reason.

A ``blocked`` phase stopped on something outside it, and one outside cause tends
to stop many phases at once: when a remote box refuses this host's ssh key, every
phase that deploys hits it within the hour. A ping each told the owner the same
thing over and over. So under ``[telegram].pings = "necessary"`` a ``blocked``
outcome that would ping is gathered here instead (logged as held, never lost),
and the supervisor sends one ping :data:`GATHER_S` after the first one of the
burst, listing the phases under each distinct reason. ``"all"`` pings each one,
as before.

A reason is the recap's first sentence with the phase's own id taken out, so two
workers that hit the same wall and say so the same way share a line.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import telegram
from .config import Config
from .logutil import Log

#: How long a burst gathers before its one ping goes out.
GATHER_S = 900.0
PENDING = "blocked-pings.jsonl"
REASON_CHARS = 200
HELD = "gathered into one ping for the phases blocked with it"

_SENTENCE = re.compile(r"(?<=[.!?;])\s")


def _path(cfg: Config) -> Path:
    return Path(cfg.state_dir) / PENDING


@contextmanager
def _locked(cfg: Config) -> Iterator[None]:
    """Worker ``swarm done`` calls append while the supervisor flushes."""
    lock = _path(cfg).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def reason_of(phase: str, note: str) -> str:
    """The part of a recap that names the wall: its first sentence, without the
    phase's own id, collapsed and cut to one line."""
    text = " ".join(note.replace(phase, " ").split()).strip(" -—:·,")
    text = _SENTENCE.split(text, maxsplit=1)[0] if text else ""
    if len(text) > REASON_CHARS:
        text = text[:REASON_CHARS].rstrip() + " …"
    return text or "no reason given"


def gather(cfg: Config, phase: str, note: str, now: float | None = None) -> None:
    """Queue ``phase``'s blocked ping for the next grouped send. Never raises."""
    row = {"ts": now or time.time(), "phase": phase, "reason": reason_of(phase, note)}
    try:
        with _locked(cfg), _path(cfg).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _read(cfg: Config) -> list[dict]:
    try:
        lines = _path(cfg).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("phase"):
            rows.append(row)
    return rows


def deadline(cfg: Config) -> float | None:
    """When the gathered burst is due to go out, or ``None`` with nothing gathered."""
    rows = _read(cfg)
    if not rows:
        return None
    return min(float(r.get("ts") or 0.0) for r in rows) + GATHER_S


def message(rows: list[dict]) -> str:
    """Plain English: how many phases are blocked, and on what, each reason once."""
    groups: dict[str, list[str]] = {}
    for r in rows:
        phases = groups.setdefault(r.get("reason") or "no reason given", [])
        if r["phase"] not in phases:
            phases.append(r["phase"])
    count = len({r["phase"] for r in rows})
    head = (f"swarm: {rows[0]['phase']} is blocked and needs you" if count == 1
            else f"swarm: {count} phases are blocked and need you")
    lines = [head + f", on something outside {'its' if count == 1 else 'their'} own work:"]
    for reason, phases in groups.items():
        lines.append(f"- {reason} ({', '.join(phases)})")
    lines.append("Once the cause is fixed, `swarm retry <phase>` puts each one back in play.")
    return "\n".join(lines)


def flush(cfg: Config, log: Log, now: float | None = None) -> bool:
    """Send the gathered burst once it is due. True when a ping went out."""
    now = now or time.time()
    with _locked(cfg):
        rows = _read(cfg)
        if not rows or now < min(float(r.get("ts") or 0.0) for r in rows) + GATHER_S:
            return False
        try:
            os.unlink(_path(cfg))
        except OSError:
            pass
    phases = sorted({r["phase"] for r in rows})
    sent = telegram.notify(
        cfg.telegram_notify,
        message(rows),
        kind="blocked",
        phase=phases[0] if len(phases) == 1 else None,
        source="blockedping.flush",
        state_dir=cfg.state_dir,
    )
    log.line(f"BLOCKED-PING {'sent' if sent else 'failed'} {' '.join(phases)}")
    return sent
