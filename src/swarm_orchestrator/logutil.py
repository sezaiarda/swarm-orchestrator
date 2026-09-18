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

import sys
import time
from datetime import datetime
from pathlib import Path



class Log:
    """Append-only line logger backed by a file (with stderr fallback)."""

    def __init__(self, path: Path, echo: bool = False) -> None:
        self.path = path
        self.echo = echo
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8", buffering=1)
        except OSError as exc:
            print(f"log-open-failed {path}: {exc}", file=sys.stderr)
            self._fh = None

    def line(self, message: str) -> None:
        """Write one timestamped line."""
        record = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} "
            f"{time.monotonic():.3f} {message}\n"
        )
        if self._fh is not None:
            try:
                self._fh.write(record)
            except OSError as exc:
                print(f"log-write-failed: {exc}", file=sys.stderr)
        if self.echo or self._fh is None:
            sys.stderr.write(record)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


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
