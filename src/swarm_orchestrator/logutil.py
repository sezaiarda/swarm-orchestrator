"""Minimal structured logger.

Appends single, greppable lines to the supervisor log. Tests assert on these
lines (``ACTION spawn-master``, ``ACTION inject-master``, ...), so the *message*
is the stable part; only the timestamp prefix changed.

Format: ``<iso-local> <mono> <line>`` — e.g.
``2026-08-27 14:31:07.412 163262.783 EVENT done dash-W10 ok``.

Both stamps are kept on purpose. The wall clock is what correlates a line to a
commit, a telegram or a transcript, and it is what survives a reboot: the log is
opened ``"a"`` and the state dir outlives the machine, so a monotonic-only file
restarts near ``0.0`` beneath six-figure lines and any "last line is latest"
assumption silently inverts. The monotonic value is retained because every
process on a host shares the CLOCK_MONOTONIC epoch, which makes intervals
between a supervisor line and a separate ``swarm done`` line exact and immune to
NTP steps.

:func:`parse_ts` reads both this format and the legacy monotonic-only one, so an
existing log stays readable across the change.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

#: Size at which the supervisor rotates its log, and how many old files it keeps
#: (``supervisor.log.1`` newest … ``.3`` oldest). The log grows by tens of KB a
#: day on a busy campaign, so this bounds it without ever touching a live run's
#: history, and every reader of that history reads the old files too
#: (:func:`read_all`).
ROTATE_BYTES = 16 * 1024 * 1024
ROTATE_KEEP = 3


def generations(path: Path) -> list[Path]:
    """The rotated files of ``path`` that exist, oldest first."""
    out = []
    for n in range(ROTATE_KEEP, 0, -1):
        old = path.with_name(f"{path.name}.{n}")
        if old.is_file():
            out.append(old)
    return out


def read_all(path: Path, keep: int | None = None, current: bool = True) -> str:
    """``path`` with its rotated files before it, oldest first: the whole history.

    ``keep`` limits how many rotated files are read (the newest ones);
    ``current=False`` reads only the rotated ones. A missing or unreadable file
    contributes nothing.
    """
    olds = generations(path)
    if keep is not None:
        olds = olds[len(olds) - keep:] if keep > 0 else []
    parts = []
    for p in [*olds, *([path] if current else [])]:
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "".join(parts)


def rotate(path: Path, keep: int = ROTATE_KEEP) -> None:
    """``path`` -> ``path.1`` -> … -> ``path.<keep>``; the oldest is dropped."""
    for n in range(keep, 0, -1):
        src = path if n == 1 else path.with_name(f"{path.name}.{n - 1}")
        if src.exists():
            os.replace(src, path.with_name(f"{path.name}.{n}"))


class Log:
    """Append-only line logger backed by a file (with stderr fallback).

    ``max_bytes`` (the supervisor's, :data:`ROTATE_BYTES`) rotates the file once
    it reaches that size. Short-lived writers (every CLI call) leave it at 0: a
    line one of them appends during a rotation lands in ``.1``, which every
    history reader reads anyway.
    """

    def __init__(self, path: Path, echo: bool = False, max_bytes: int = 0) -> None:
        self.path = path
        self.echo = echo
        self.max_bytes = max_bytes
        # The supervisor launches workers on background threads, and a launch
        # builds its worktrees on a pool, all writing to this one handle. A line
        # is the unit every reader greps for, so it must never interleave.
        self._lock = threading.Lock()
        self._fh = self._open()

    def _open(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return self.path.open("a", encoding="utf-8", buffering=1)
        except OSError as exc:
            print(f"log-open-failed {self.path}: {exc}", file=sys.stderr)
            return None

    def line(self, message: str) -> None:
        """Write one timestamped line."""
        record = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
            f"{time.monotonic():.3f} {message}\n"
        )
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.write(record)
                    if self.max_bytes and os.fstat(self._fh.fileno()).st_size >= self.max_bytes:
                        self._fh.close()
                        rotate(self.path)
                        self._fh = self._open()
                except (OSError, ValueError) as exc:  # ValueError: closed under us
                    print(f"log-write-failed: {exc}", file=sys.stderr)
            if self.echo or self._fh is None:
                sys.stderr.write(record)

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


#: The line that closes a phase run which ended without ``swarm done``. The phase
#: history pairs ``CLAIM``/``LAUNCH`` with ``EVENT done``; a claim that ended any
#: other way (``swarm up`` rebuilt the slots, the watchdog reaped a dead pane,
#: ``swarm free``, a worker that never started) wrote nothing, so the dashboard
#: said "running" for days about a worker that was long gone.
RUN_ENDED = "RUN-ENDED"


def run_ended(log, phase: str, reason: str) -> None:
    """Record that ``phase``'s claim ended without a report. ``reason`` is one token."""
    log.line(f"{RUN_ENDED} {phase} reason={reason}")


def _boot_epoch() -> float | None:
    """Wall-clock time of this boot, or None if unavailable (non-Linux)."""
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            return time.time() - float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def parse_ts(line: str) -> tuple[float | None, str]:
    """Split a log line into ``(epoch_seconds | None, message)``.

    Handles both formats:

    * new — ``2026-08-27 14:31:07.412 163262.783 EVENT …``
    * legacy — ``163262.783 EVENT …`` (monotonic only)

    A legacy line is decoded against *this* boot's epoch, which is only valid
    while its value is below the current uptime. A larger value is from an
    earlier boot and is undecodable, so ``None`` is returned rather than a
    confidently wrong wall clock.
    """
    parts = line.split(" ", 3)
    if len(parts) >= 4 and "-" in parts[0] and ":" in parts[1]:
        try:
            dt = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M:%S.%f")
            float(parts[2])  # the monotonic field; presence confirms the format
            return dt.timestamp(), parts[3].rstrip("\n")
        except ValueError:
            pass
    parts = line.split(" ", 1)
    if len(parts) == 2:
        try:
            mono = float(parts[0])
        except ValueError:
            return None, line.rstrip("\n")
        boot = _boot_epoch()
        if boot is not None and mono <= time.monotonic():
            return boot + mono, parts[1].rstrip("\n")
        return None, parts[1].rstrip("\n")
    return None, line.rstrip("\n")
