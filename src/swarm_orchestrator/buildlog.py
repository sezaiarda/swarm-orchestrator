"""The build gate's event log, holder records and run-time history.

``<state>/buildsem/events.jsonl`` is append-only, one JSON object per line, and
is a contract other tools read. Every line has exactly these keys::

    {"ts": <unix float>,
     "event": "queued"|"start"|"end"|"bypass"|"preflight_fail"|"yield"|"unyield",
     "id": "<one per swarm build call>", "phase": <$SWARM_PHASE or null>,
     "pid": <int>, "slot": <int or null>, "cls": "heavy"|"light",
     "argv": "<command, at most 300 chars>", "cwd": "<path>",
     "wait_s": <float on start, else null>,
     "run_s": <float on end, yield and unyield, else null>,
     "exit": <int or null on end, else null>,
     "idle_s": <float on yield and unyield, else null>,
     "hold": <true|false on start, else null>}

- ``queued``: a heavy command joined the queue. ``pid`` is the waiting
  ``swarm build`` process (the build does not exist yet).
- ``start``: it got slot ``slot``; ``pid`` is the build process itself (its whole
  process tree holds the slot), ``wait_s`` how long it queued. ``hold`` is true
  for a build started with ``swarm build --hold``: it keeps its slot however
  idle it looks, and never gets a ``yield``.
- ``bypass``: a light command (or any command while the gate is off) started
  without queueing; ``pid`` is the command's process, ``slot`` null.
- ``end``: the command finished; ``run_s`` since its start, ``exit`` its exit code
  (a signal N is ``128+N``; ``124`` is ``--timeout``). ``exit`` is **null** for a
  synthetic end the gate writes when it finds a holder that died without
  logging one (``swarm build`` itself was SIGKILLed); then ``run_s`` is measured
  up to when that was noticed.
- ``preflight_fail``: the command was refused before queueing (missing program,
  directory or file); nothing ran.
- ``yield``: a running build was set aside as idle (:mod:`buildidle`): it keeps
  running but no longer counts against ``max_concurrent``. Same ``id``, ``pid``
  and ``slot`` as its ``start``; ``idle_s`` is how long its whole process tree
  had been quiet, ``run_s`` how long it had run. Written by the waiter that
  measured it.
- ``unyield``: a set-aside build is working again and counts again; ``idle_s``
  is how long it was set aside. A build that ends while set aside gets no
  ``unyield``: its ``end`` closes the stretch.

A ``queued`` with no ``start`` for the same ``id`` gave up (killed) while
waiting. A ``start`` with no ``end`` whose ``pid`` is gone died unrecorded; the
gate writes the synthetic ``end`` as soon as it notices.

Each line is one ``write(2)`` on an ``O_APPEND`` descriptor, so concurrent writers
never interleave. At 20 MB the file is renamed to ``events.jsonl.1`` (replacing
the previous one) and a new one is started.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import statistics
import time
from pathlib import Path

from .config import Config

EVENTS = "events.jsonl"
ROTATE_BYTES = 20 * 1024 * 1024
ARGV_MAX = 300
_TAIL_BYTES = 2 * 1024 * 1024
_HISTORY_N = 20
_DEFAULT_RUN_S = 120.0
_WRAPPERS = frozenset({"timeout", "env", "nice", "nohup", "time", "stdbuf", "ionice", "exec"})


def events_path(cfg: Config) -> Path:
    return cfg.buildsem_dir / EVENTS


def argv_text(argv: list[str] | str) -> str:
    text = argv if isinstance(argv, str) else shlex.join(argv)
    return text[:ARGV_MAX]


def event(cfg: Config, kind: str, *, id: str, phase: str | None, pid: int,
          slot: int | None, cls: str, argv: list[str] | str, cwd: str,
          wait_s: float | None = None, run_s: float | None = None,
          exit: int | None = None, idle_s: float | None = None,
          hold: bool | None = None, ts: float | None = None) -> None:
    """Append one event line. Never raises: the log must not break a build."""
    rec = {
        "ts": round(ts if ts is not None else time.time(), 3), "event": kind, "id": id,
        "phase": phase, "pid": pid, "slot": slot, "cls": cls, "argv": argv_text(argv),
        "cwd": str(cwd),
        "wait_s": round(wait_s, 3) if wait_s is not None else None,
        "run_s": round(run_s, 3) if run_s is not None else None,
        "exit": exit,
        "idle_s": round(idle_s, 3) if idle_s is not None else None,
        "hold": hold,
    }
    line = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
    path = events_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def _maybe_rotate(path: Path) -> None:
    try:
        if path.stat().st_size < ROTATE_BYTES:
            return
    except OSError:
        return
    lock = os.open(path.with_name(EVENTS + ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:  # someone else may have rotated while we waited
            if path.stat().st_size >= ROTATE_BYTES:
                os.replace(path, path.with_name(EVENTS + ".1"))
        except OSError:
            pass
    finally:
        os.close(lock)


def _tail_lines(path: Path, limit: int) -> tuple[list[str], int]:
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - limit)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return [], 0
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start and lines:
        lines = lines[1:]  # the first line was cut in the middle
    return lines, len(data)


def read_events(cfg: Config, limit: int = _TAIL_BYTES) -> list[dict]:
    """The most recent events (about ``limit`` bytes' worth), oldest first."""
    path = events_path(cfg)
    lines, got = _tail_lines(path, limit)
    if got < limit:
        older, _ = _tail_lines(path.with_name(EVENTS + ".1"), limit - got)
        lines = older + lines
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


# -- run-time history -------------------------------------------------------
def where(cfg: Config, cwd: str) -> str:
    """Where a command ran, comparable across phases: the path inside a phase's
    worktree (or inside the project), so ``wt/A/lib`` and ``wt/B/lib`` match."""
    p = Path(cwd)
    for root, skip in ((cfg.wt_dir, 1), (cfg.project_dir, 0)):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts[skip:]
        return "/".join(parts) or "."
    return p.name


def family(text: str) -> str:
    """The command's shape: its first three words that are not options,
    assignments or wrapper arguments (``cargo nextest run``)."""
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    out: list[str] = []
    for w in words:
        if not out and (w in _WRAPPERS or "=" in w or w[:1].isdigit()):
            continue
        if w.startswith("-") or "=" in w:
            continue
        out.append(os.path.basename(w) if not out else w)
        if len(out) == 3:
            break
    return " ".join(out)


class History:
    """Past heavy run times, by exact command and by command shape."""

    def __init__(self, cfg: Config, events: list[dict] | None = None):
        self.cfg = cfg
        self.exact: dict[tuple[str, str], list[float]] = {}
        self.shape: dict[tuple[str, str], list[float]] = {}
        self.all: list[float] = []
        for e in events if events is not None else read_events(cfg):
            if e.get("event") != "end" or e.get("cls") != "heavy" or e.get("exit") is None:
                continue
            run = e.get("run_s")
            if not isinstance(run, (int, float)):
                continue
            loc = where(cfg, str(e.get("cwd", "")))
            text = str(e.get("argv", ""))
            self.exact.setdefault((loc, text), []).append(float(run))
            self.shape.setdefault((loc, family(text)), []).append(float(run))
            self.all.append(float(run))

    def predict(self, argv: list[str] | str, cwd: str) -> float | None:
        """The usual run time of this command here, or ``None`` if unknown: the
        median of its own last runs (2+), else of its shape's (3+)."""
        text = argv_text(argv)
        loc = where(self.cfg, cwd)
        runs = self.exact.get((loc, text), [])[-_HISTORY_N:]
        if len(runs) >= 2:
            return statistics.median(runs)
        runs = self.shape.get((loc, family(text)), [])[-_HISTORY_N:]
        if len(runs) >= 3:
            return statistics.median(runs)
        return None

    @property
    def default(self) -> float:
        """What to assume for a command with no history."""
        runs = self.all[-200:]
        return statistics.median(runs) if runs else _DEFAULT_RUN_S


def fmt_s(seconds: float | None) -> str:
    """``42s``, ``3m05s``, ``1h12m``."""
    if seconds is None:
        return "?"
    s = int(max(0.0, seconds) + 0.5)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def short_cmd(text: str, width: int = 60) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
