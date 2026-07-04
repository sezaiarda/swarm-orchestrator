"""Minimal structured logger.

Appends single, greppable lines to the supervisor log. Tests assert on these
lines (``ACTION spawn-master``, ``ACTION inject-master``, ...), so the format is
intentionally stable: ``<ts_monotonic> <line>``. No error is ever swallowed
silently — failures to open the log fall back to stderr.
"""

from __future__ import annotations

import sys
import time
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
        record = f"{time.monotonic():.3f} {message}\n"
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
