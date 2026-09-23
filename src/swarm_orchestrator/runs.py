"""Runs: one ``swarm up`` → ``swarm down`` (or ``swarm reset``), on disk.

Every number the dashboard derives over time — the ETA's typical phase, the
usage pace — used to be computed from files that are never rotated: the
append-only supervisor log and ``meters/limits.jsonl``. So a ``swarm down`` then
``swarm up`` kept yesterday's phases in today's ETA and yesterday's samples in
today's pace, and there was no way to say "start counting from now". A run is
that "from now": an id and an epoch every live figure is measured since.

Layout (small files, kept forever)::

    <state>/history/current.json          {"run_id", "epoch_ts"} of the open run
    <state>/history/runs/<run_id>/run.json the record, + "summary" once closed

``current.json`` is the pointer every reader uses, not a ``state.json`` field:
a supervisor running older code rewrites ``state.json`` from its own dataclass
and would silently drop a key it does not know. The state carries a copy for
``swarm status``-style readers, and loses nothing if it is dropped.

Stdlib only: the meters tap imports this module on the path that runs per
status-line render.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

HISTORY_DIR = "history"
RUNS_DIR = "runs"
CURRENT = "current.json"
RUN_FILE = "run.json"


def history_dir(state_dir: str | Path) -> Path:
    return Path(state_dir) / HISTORY_DIR


def runs_dir(state_dir: str | Path) -> Path:
    return history_dir(state_dir) / RUNS_DIR


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def _locked(state_dir: str | Path) -> Iterator[None]:
    """One writer at a time: ``up``, ``down``, ``reset`` and a reload can race."""
    root = history_dir(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def current_id(state_dir: str | Path) -> str | None:
    """The open run's id, from the pointer alone — the tap's cheap read."""
    ptr = _read(history_dir(state_dir) / CURRENT)
    rid = ptr.get("run_id") if ptr else None
    return rid if isinstance(rid, str) and rid else None


def load(state_dir: str | Path, run_id: str) -> dict | None:
    rec = _read(runs_dir(state_dir) / run_id / RUN_FILE)
    return rec if rec and isinstance(rec.get("epoch_ts"), (int, float)) else None


def current(state_dir: str | Path) -> dict | None:
    """The open run's record, or ``None`` (no run yet: the legacy period)."""
    rid = current_id(state_dir)
    rec = load(state_dir, rid) if rid else None
    return rec if rec and rec.get("end_ts") is None else None


def list_runs(state_dir: str | Path) -> list[dict]:
    """Every recorded run, newest first. A torn ``run.json`` is skipped, not fatal."""
    try:
        dirs = [p for p in runs_dir(state_dir).iterdir() if p.is_dir()]
    except OSError:
        return []
    out = [r for r in (load(state_dir, p.name) for p in dirs) if r is not None]
    out.sort(key=lambda r: r["epoch_ts"], reverse=True)
    return out


def _new_id(state_dir: str | Path, now: float) -> str:
    """Readable and sortable: the start time, suffixed if two starts share a second."""
    base = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    rid, n = base, 1
    while (runs_dir(state_dir) / rid).exists():
        n += 1
        rid = f"{base}-{n}"
    return rid


def _close(state_dir, rec: dict, end: float, reason: str,
           summarize: Callable[[dict, float], dict] | None) -> dict:
    rec["end_ts"] = end
    rec["closed_by"] = reason
    if summarize is not None:
        try:
            rec["summary"] = summarize(rec, end)
        except Exception as exc:  # noqa: BLE001 - a failed summary must not keep a run open
            rec["summary_error"] = str(exc)
    _write(runs_dir(state_dir) / rec["run_id"] / RUN_FILE, rec)
    return rec


def close(state_dir: str | Path, reason: str = "down", now: float | None = None,
          summarize: Callable[[dict, float], dict] | None = None,
          end: Callable[[dict], float] | None = None) -> dict | None:
    """Close the open run (``end_ts`` + summary); ``None`` if none was open.

    ``end`` picks the end time from the record when "now" would be a lie — a run
    left open by a crash is closed by the next ``up`` hours later, and stretching
    it over the dead time would dilute every per-hour average it reports.
    """
    now = time.time() if now is None else now
    with _locked(state_dir):
        rec = current(state_dir)
        if rec is None:
            return None
        rec = _close(state_dir, rec, end(rec) if end else now, reason, summarize)
        try:
            (history_dir(state_dir) / CURRENT).unlink()
        except OSError:
            pass
        return rec


def start(state_dir: str | Path, max_workers: int, isolation: str, reason: str = "up",
          now: float | None = None,
          summarize: Callable[[dict, float], dict] | None = None,
          end: Callable[[dict], float] | None = None) -> tuple[dict, dict | None]:
    """Open a new run, closing any still-open one first. Returns ``(new, closed)``."""
    now = time.time() if now is None else now
    with _locked(state_dir):
        closed = None
        prev = current(state_dir)
        if prev is not None:
            closed = _close(state_dir, prev, end(prev) if end else now,
                            f"{reason} (was still open)", summarize)
        rid = _new_id(state_dir, now)
        rec = {
            "run_id": rid,
            "epoch_ts": now,
            "end_ts": None,
            "started_by": reason,
            "max_workers": int(max_workers),
            "isolation": str(isolation),
            # Mid-run config moves, oldest first: {"ts", "max_workers", "isolation"}.
            "changes": [],
        }
        _write(runs_dir(state_dir) / rid / RUN_FILE, rec)
        _write(history_dir(state_dir) / CURRENT, {"run_id": rid, "epoch_ts": now})
        return rec, closed


def note_config(state_dir: str | Path, max_workers: int, isolation: str,
                now: float | None = None) -> bool:
    """Record a reload that changed the worker count or isolation mid-run.

    A 12-hour measurement at one worker is only readable if a reload to four is
    visible in it: the summary splits the averages at every recorded change.
    """
    now = time.time() if now is None else now
    with _locked(state_dir):
        rec = current(state_dir)
        if rec is None:
            return False
        workers, iso = config_at(rec, now)
        if (workers, iso) == (int(max_workers), str(isolation)):
            return False
        rec.setdefault("changes", []).append(
            {"ts": now, "max_workers": int(max_workers), "isolation": str(isolation)})
        _write(runs_dir(state_dir) / rec["run_id"] / RUN_FILE, rec)
        return True


def config_at(rec: dict, ts: float) -> tuple[int, str]:
    """The ``(max_workers, isolation)`` in force at ``ts``."""
    workers, iso = int(rec.get("max_workers") or 0), str(rec.get("isolation") or "?")
    for ch in rec.get("changes") or []:
        if isinstance(ch, dict) and float(ch.get("ts") or 0) <= ts:
            workers = int(ch.get("max_workers") or workers)
            iso = str(ch.get("isolation") or iso)
    return workers, iso


def segments(rec: dict, end: float) -> list[tuple[float, float, int, str]]:
    """``(from, to, max_workers, isolation)`` spans the run held one config for."""
    start = float(rec["epoch_ts"])
    cuts = sorted(float(c.get("ts") or 0) for c in rec.get("changes") or []
                  if isinstance(c, dict) and start < float(c.get("ts") or 0) < end)
    bounds = [start, *cuts, end]
    out = []
    for t0, t1 in zip(bounds, bounds[1:]):
        workers, iso = config_at(rec, t0)
        if out and (out[-1][2], out[-1][3]) == (workers, iso):
            out[-1] = (out[-1][0], t1, workers, iso)
        else:
            out.append((t0, t1, workers, iso))
    return out
