"""One ask for a burst of ``blocked`` outcomes.

A ``blocked`` phase stopped on something outside it, and one outside cause tends
to stop many phases at once: when a remote box refuses this host's ssh key, every
phase that deploys hits it within the hour. An ask each would tell the owner the
same thing over and over. So a ``blocked`` outcome that would ask (one past the
Overseer's retry, or any with the Overseer off) is gathered here instead (logged,
never lost), and the supervisor sends one ask :data:`GATHER_S` after the first
one of the burst, naming the phases.

Each gathered row keeps its reason — the recap's first sentence with the phase's
own id taken out — in the log beside the ask; the ask itself is too short to
carry them, and ``swarm report`` has every recap in full.
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

from . import state as state_mod
from . import telegram
from .config import Config
from .logutil import Log

#: How long a burst gathers before its one ping goes out.
GATHER_S = 900.0
PENDING = "blocked-pings.jsonl"
REASON_CHARS = 200
HELD = "gathered into one ask for the phases blocked with it"

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


def shift(cfg: Config, delta: float, now: float | None = None) -> None:
    """Move the gathered burst ``delta`` seconds along for a thaw: a burst
    gathers while workers run, and none ran (:func:`freezer.rebase`)."""
    now = time.time() if now is None else now
    try:
        with _locked(cfg):
            rows = _read(cfg)
            if not rows:
                return
            for row in rows:
                row["ts"] = state_mod.moved(row.get("ts") or 0.0, delta, now)
            tmp = _path(cfg).with_suffix(".tmp")
            tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            os.replace(tmp, _path(cfg))
    except OSError:
        pass


def reasons(rows: list[dict]) -> str:
    """Each distinct reason once, with the phases it stopped: the long form the
    log keeps beside the ask."""
    groups: dict[str, list[str]] = {}
    for r in rows:
        phases = groups.setdefault(r.get("reason") or "no reason given", [])
        if r["phase"] not in phases:
            phases.append(r["phase"])
    return "; ".join(f"{reason} ({', '.join(phases)})" for reason, phases in groups.items())


def message(cfg: Config, rows: list[dict]) -> str:
    """The ask: what to clear and for which phases, then why it is the owner's."""
    phases = list(dict.fromkeys(r["phase"] for r in rows))
    if len(phases) == 1:
        (phase,) = phases
        return (f"Clear what blocks {phase}, then run `swarm retry {phase}`: it stopped"
                " on something outside its own work that the swarm cannot fix."
                " `swarm report` has its recap.")
    return telegram.fitted(
        cfg, f"Clear what blocks {len(phases)} phases (", telegram.names(phases),
        "), then `swarm retry` each: they stopped on something outside their own work"
        " that the swarm cannot fix. `swarm report` has their recaps.")


def flush(cfg: Config, log: Log, now: float | None = None) -> bool:
    """Send the gathered burst once it is due. True when the ask went out."""
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
    sent = telegram.ask(
        cfg,
        message(cfg, rows),
        kind="blocked",
        phase=phases[0] if len(phases) == 1 else None,
        source="blockedping.flush",
        detail=reasons(rows),
    ).delivered
    log.line(f"BLOCKED-PING {'sent' if sent else 'failed'} {' '.join(phases)}")
    return sent
