"""The Resources tab's history: the sampler's files, folded into what a chart draws.

``meters/resources.jsonl`` grows by a line a second while a build runs and is
tens of megabytes within a day; a phone polling the board must never cause it to
be read again. So the file is read **once**, then only its new lines
(:class:`Tail`), and every line is folded on arrival into one fixed set of
buckets per window (:data:`WINDOWS`): a request is answered from a few hundred
buckets already in memory, whatever the file holds.

Each bucket keeps, per figure, the minimum, the time-weighted average and the
maximum of the samples that fell in it, so a one-second burst inside a two-hour
bucket is still on the chart (as the top of the band) and is not averaged away.
The minute aggregates (``resources-1m.jsonl``, what ages out of the full file)
are read once too, at load, for the stretch the full file no longer covers:
after that every sample passes through here before it ages, so the aggregate
file is not needed again. Compaction rewrites the full file under a new inode;
the lines already folded are skipped by their timestamp, and only a real gap
(nobody looked for longer than the file keeps) starts over.

The row format is the store's (:mod:`swarm_orchestrator.resources.store`): a
figure is a number in a sample row and ``[min, avg, max]`` in a minute row.
Unknown keys are ignored and missing ones leave their bucket empty.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from ..resources import store

#: ``window -> (seconds it spans, seconds per bucket)``: 336 to 360 buckets each.
WINDOWS: dict[str, tuple[float, float]] = {
    "1h": (3600.0, 10.0),
    "6h": (6 * 3600.0, 60.0),
    "24h": (24 * 3600.0, 240.0),
    "7d": (7 * 86400.0, 1800.0),
    "30d": (30 * 86400.0, 7200.0),
}
DEFAULT_WINDOW = "24h"
#: ``(name, row key, key inside "psi" or None, decimals served)``.
METRICS: tuple[tuple[str, str, str | None, int], ...] = (
    ("cpu", "cpu", None, 1),
    ("avail_mb", "avail_mb", None, 0),
    ("anon_mb", "anon_mb", None, 0),
    ("wr_mbs", "wr_mbs", None, 1),
    ("nb", "nb", None, 0),
    ("psi_mem", "psi", "mem", 2),
    ("psi_memf", "psi", "memf", 2),
    ("psi_io", "psi", "io", 2),
    ("psi_iof", "psi", "iof", 2),
)
#: A sample stands for the seconds since the one before it, up to this; a longer
#: silence is the sampler not running, not one long sample.
MAX_WEIGHT_S = 30.0
MINUTE_S = 60.0
_INF = float("inf")
_EMPTY = [_INF, 0.0, 0.0, -_INF] * len(METRICS)
_TS_PREFIX = b'{"ts":'


class Tail:
    """The complete new lines of an append-only file since the last call.

    :attr:`reset` says the last :meth:`lines` started from the top: the file is
    new, was replaced (another inode) or shrank. A line still being written (no
    newline yet) is left for the next call.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.reset = False
        self._ino: int | None = None
        self._off = 0

    def lines(self) -> Iterator[bytes]:
        try:
            fh = self.path.open("rb")
        except OSError:
            self.reset = self._ino is not None
            self._ino, self._off = None, 0
            return
        with fh:
            st = os.fstat(fh.fileno())
            self.reset = st.st_ino != self._ino or st.st_size < self._off
            if self.reset:
                self._ino, self._off = st.st_ino, 0
            if st.st_size == self._off:
                return
            fh.seek(self._off)
            for line in fh:
                if not line.endswith(b"\n"):
                    break
                self._off += len(line)
                yield line


def ts_of(line: bytes) -> float | None:
    """A row's ``ts`` without parsing the row, when it leads the line (it does in
    every sample row); ``None`` leaves the caller to parse it."""
    if line.startswith(_TS_PREFIX):
        end = line.find(b",", 6)
        if end > 0:
            try:
                return float(line[6:end])
            except ValueError:
                return None
    return None


def parse(line: bytes) -> dict | None:
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _triple(v) -> tuple[float, float, float] | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v, v, v
    if isinstance(v, list) and len(v) == 3 and all(
            isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
        return v[0], v[1], v[2]
    return None


def decode(row: dict) -> list[tuple[float, float, float] | None]:
    """One ``(min, avg, max)`` per :data:`METRICS` entry, ``None`` where the row has none."""
    psi = row.get("psi")
    psi = psi if isinstance(psi, dict) else {}
    return [_triple(psi.get(sub) if sub else row.get(key)) for _, key, sub, _ in METRICS]


class Ring:
    """One window's buckets: ``index -> [min, weighted sum, weight, max] per metric``."""

    def __init__(self, span_s: float, step_s: float) -> None:
        self.span_s = span_s
        self.step_s = step_s
        self.buckets: dict[int, list[float]] = {}

    def add(self, ts: float, vals: list, weight: float) -> None:
        idx = int(ts // self.step_s)
        b = self.buckets.get(idx)
        if b is None:
            b = self.buckets[idx] = list(_EMPTY)
        i = 0
        for v in vals:
            if v is not None:
                if v[0] < b[i]:
                    b[i] = v[0]
                b[i + 1] += v[1] * weight
                b[i + 2] += weight
                if v[2] > b[i + 3]:
                    b[i + 3] = v[2]
            i += 4

    def bounds(self, now: float) -> tuple[int, int]:
        """``(first, last)`` bucket index of the window ending at ``now``."""
        last = int(now // self.step_s)
        return last - int(round(self.span_s / self.step_s)) + 1, last

    def prune(self, now: float) -> None:
        """Drop what left the window (a window nobody opens must not grow)."""
        first, _ = self.bounds(now)
        for idx in [i for i in self.buckets if i < first]:
            del self.buckets[idx]

    def series(self, now: float) -> dict[str, dict]:
        """``{metric: {"min": [...], "avg": [...], "max": [...], "all": [lo, avg, hi]}}``:
        one entry a bucket (``None`` where nothing was sampled) and the whole
        window's own minimum, time-weighted average and maximum."""
        first, last = self.bounds(now)
        out: dict[str, dict] = {}
        cells = [self.buckets.get(i) for i in range(first, last + 1)]
        for m, (name, _, _, digits) in enumerate(METRICS):
            lo: list = []
            avg: list = []
            hi: list = []
            j = m * 4
            total = weight = 0.0
            for b in cells:
                if b is None or not b[j + 2]:
                    lo.append(None)
                    avg.append(None)
                    hi.append(None)
                    continue
                total += b[j + 1]
                weight += b[j + 2]
                lo.append(_num(b[j], digits))
                avg.append(_num(b[j + 1] / b[j + 2], digits))
                hi.append(_num(b[j + 3], digits))
            known = [v for v in lo if v is not None]
            whole = [min(known), _num(total / weight, digits),
                     max(v for v in hi if v is not None)] if known else None
            out[name] = {"min": lo, "avg": avg, "max": hi, "all": whole}
        return out


def _num(v: float, digits: int) -> float | int:
    return round(v, digits) if digits else int(round(v))


class History:
    """Every window's :class:`Ring`, kept current from the sampler's files."""

    def __init__(self, state_dir: Path | str) -> None:
        root = store.meters_dir(state_dir)
        self._agg = root / store.AGG
        self._tail = Tail(root / store.FULL)
        self.rings = {name: Ring(span, step) for name, (span, step) in WINDOWS.items()}
        self._loaded = False
        self._last_ts: float | None = None
        #: Samples folded since the start: what the tests (and a curious reader)
        #: check to see that a poll did not read the file again.
        self.folded = 0
        self.loads = 0

    # -- reading ---------------------------------------------------------------
    def update(self, now: float) -> None:
        """Fold whatever the sampler wrote since the last call."""
        if self._loaded:
            self._catch_up(now)
        else:
            self._load(now)
        for ring in self.rings.values():
            ring.prune(now)

    def _catch_up(self, now: float) -> None:
        seen = self._last_ts
        for line in self._tail.lines():
            ts = ts_of(line)
            if self._tail.reset and seen is not None:
                if ts is None:
                    ts = (parse(line) or {}).get("ts")
                if isinstance(ts, (int, float)) and ts <= seen:
                    continue  # compaction kept this line; it was folded when it was new
            self._sample(line, now)
        if self._tail.reset and not self._continuous(seen):
            self._load(now)

    def _continuous(self, seen: float | None) -> bool:
        """After a rewrite: does the new file still start at or before what was
        folded? If not, samples aged out unseen and only a reload finds them."""
        if seen is None:
            return True
        first = _first_sample_ts(self._tail.path)
        return first is None or first <= seen

    def _load(self, now: float) -> None:
        for ring in self.rings.values():
            ring.buckets.clear()
        self._tail = Tail(self._tail.path)
        self._last_ts = None
        self._loaded = True
        self.loads += 1
        first: float | None = None
        for line in self._tail.lines():
            ts = self._sample(line, now)
            if first is None:
                first = ts
        oldest = now - max(span for span, _ in WINDOWS.values()) - MINUTE_S
        try:
            fh = self._agg.open("rb")
        except OSError:
            return
        with fh:
            for line in fh:
                ts = ts_of(line)
                if ts is not None and (ts < oldest or (first is not None and ts >= first)):
                    continue
                row = parse(line)
                if row is None or row.get("k") != "m":
                    continue
                ts = row.get("ts")
                if not isinstance(ts, (int, float)) or ts < oldest:
                    continue
                if first is not None and ts >= first:
                    continue
                self._fold(ts, decode(row), MINUTE_S, now)

    def _sample(self, line: bytes, now: float) -> float | None:
        """Fold one line of the full file if it is a sample; returns its ``ts``."""
        if b'"s"' not in line:
            return None
        row = parse(line)
        if row is None or row.get("k") != "s":
            return None
        ts = row.get("ts")
        if not isinstance(ts, (int, float)):
            return None
        prev = self._last_ts
        gap = ts - prev if prev is not None else 0.0
        self._last_ts = ts if prev is None else max(prev, ts)
        self._fold(ts, decode(row), gap if 0.0 < gap <= MAX_WEIGHT_S else 1.0, now)
        self.folded += 1
        return ts

    def _fold(self, ts: float, vals: list, weight: float, now: float) -> None:
        for ring in self.rings.values():
            if ts >= now - ring.span_s - ring.step_s:
                ring.add(ts, vals, weight)

    # -- serving ---------------------------------------------------------------
    def window(self, name: str, now: float) -> dict:
        """One window: where it starts, its bucket width and every metric's series."""
        self.update(now)
        ring = self.rings[name]
        first, last = ring.bounds(now)
        return {"window": name, "t0": first * ring.step_s, "step": ring.step_s,
                "n": last - first + 1, "series": ring.series(now)}


def _first_sample_ts(path: Path) -> float | None:
    try:
        fh = path.open("rb")
    except OSError:
        return None
    with fh:
        for n, line in enumerate(fh):
            row = parse(line) if b'"s"' in line else None
            if row is not None and row.get("k") == "s" and isinstance(row.get("ts"), (int, float)):
                return row["ts"]
            if n > 200:
                break
    return None


# -- builds on the time axis ------------------------------------------------------
def level_max(intervals: list[tuple[float, float]], t0: float, step: float, n: int,
              until: float) -> list[int | None]:
    """How many of ``intervals`` were open at once, at most, in each bucket.

    An exact sweep, not a count of the intervals that touch a bucket: ten builds
    that queued one after another inside a two-hour bucket are a peak of one.
    Buckets that start after ``until`` (the future) are ``None``.
    """
    events = sorted([(a, 1) for a, b in intervals if b > a]
                    + [(b, -1) for a, b in intervals if b > a])
    out: list[int | None] = []
    level = 0
    j = 0
    while j < len(events) and events[j][0] < t0:
        level += events[j][1]
        j += 1
    for i in range(n):
        start = t0 + i * step
        if start > until:
            out.append(None)
            continue
        peak = level
        end = start + step
        while j < len(events) and events[j][0] < end:
            level += events[j][1]
            peak = max(peak, level)
            j += 1
        out.append(peak)
    return out


def spans(builds: list[dict], t0: float, t1: float, step: float) -> list[dict]:
    """The builds that ran between ``t0`` and ``t1``, as marks for the time axis.

    Builds that start within one bucket of each other cannot be told apart at
    this zoom, so they share one band that says how many it holds and which
    phases; a build longer than a bucket keeps a band of its own, even when the
    next one starts the moment it ends (the gate runs them back to back).
    ``builds`` are ``{"a", "b", "phase", "cmd", "exit", "live"}``; at most one
    band a bucket comes back.
    """
    out: list[dict] = []
    for b in sorted((b for b in builds if b["b"] >= t0 and b["a"] <= t1), key=lambda b: b["a"]):
        a, end = max(b["a"], t0), min(b["b"], t1)
        cur = out[-1] if out else None
        if cur is not None and a < cur["a"] + step and a - cur["b"] < step:
            cur["b"] = max(cur["b"], end)
            cur["n"] += 1
            cur["live"] = cur["live"] or bool(b.get("live"))
            cur["bad"] += 1 if _failed(b) else 0
            if b.get("phase") and b["phase"] not in cur["phases"] and len(cur["phases"]) < 4:
                cur["phases"].append(b["phase"])
            cur.pop("cmd", None)
            continue
        out.append({"a": a, "b": end, "n": 1, "phases": [b["phase"]] if b.get("phase") else [],
                    "cmd": b.get("cmd") or "", "live": bool(b.get("live")),
                    "bad": 1 if _failed(b) else 0})
    for s in out:
        s["a"], s["b"] = round(s["a"], 1), round(s["b"], 1)
    return out


def _failed(b: dict) -> bool:
    return b.get("exit") not in (0, None)
