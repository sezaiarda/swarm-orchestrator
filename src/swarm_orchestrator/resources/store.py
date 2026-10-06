"""Where the samples live, and how they age.

All under ``<state>/meters/``:

- ``resources.jsonl`` — every sample, full resolution (one a second while a
  build runs, one every :data:`sampler.SLOW_S` otherwise), for
  :data:`FULL_KEEP_S` (a day). Rows are ``{"ts", "k": "s", ...}`` (a sample),
  ``"k": "dirs"`` (directory sizes) and ``"k": "host"`` (cores, RAM, swap;
  written when the sampler starts). A sample's builds are the machine's, every
  swarm's: ``nb`` how many, ``b`` each one's ``[cores, anon MiB]`` by id, and
  ``bo`` the ids among them that are another swarm's (absent when none is).
  Its sessions (``w``, ``x``, ``o``) are this swarm's alone.
- ``resources-1m.jsonl`` — what ages out of the first file, folded into one row
  a minute: every figure as ``[min, avg, max]``, per build and per worker the
  average cores and the peak anon memory, and ``bo`` as every other swarm's
  build of that minute. Kept :data:`AGG_KEEP_S` (30 days).
- ``builds.jsonl`` — one summary per finished heavy build on the machine's
  gate, with ``swarm``, ``swarm_name`` and ``mine`` saying whose it was.
- ``<state>/resources-now.json`` (beside ``state.json``) — the latest sample,
  the builds running on the machine (each with ``swarm``, ``swarm_name`` and
  ``mine``), how many wait (``queued``, and ``queued_mine`` of them this
  swarm's), this swarm's workers and idle holders and the sampler's own cost:
  what ``swarm status``, ``swarm doctor`` and the dashboards read.

Every file is bounded twice: by age, and by a byte cap (:data:`MAX_FULL_BYTES`,
:data:`MAX_AGG_BYTES`, :data:`MAX_BUILDS_BYTES`) that wins when a busy host
would outrun the age limit. Compaction rewrites a file to a temp name and
renames it over the old one, so a reader never sees half a file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

METERS = "meters"
FULL = "resources.jsonl"
AGG = "resources-1m.jsonl"
BUILDS = "builds.jsonl"
NOW = "resources-now.json"

FULL_KEEP_S = 24 * 3600.0
AGG_KEEP_S = 30 * 86400.0
MAX_FULL_BYTES = 48 * 2**20
MAX_AGG_BYTES = 32 * 2**20
MAX_BUILDS_BYTES = 8 * 2**20
#: How often the sampler compacts (and checks the caps).
COMPACT_EVERY_S = 3600.0

#: Scalar figures of a sample; each becomes ``[min, avg, max]`` in a minute row.
SCALARS = ("cpu", "load", "avail_mb", "anon_mb", "cache_mb", "shmem_mb", "swap_mb",
           "rd_mbs", "wr_mbs", "headroom_gb", "nb")


def meters_dir(state_dir: Path | str) -> Path:
    return Path(state_dir) / METERS


def append(path: Path, row: dict) -> int:
    """Append one JSON line; returns the bytes written."""
    line = (json.dumps(row, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    return len(line)


def read_rows(path: Path, since: float | None = None) -> list[dict]:
    rows: list[dict] = []
    try:
        fh = path.open("rb")
    except OSError:
        return rows
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and (since is None or (row.get("ts") or 0) >= since):
                rows.append(row)
    return rows


def _rewrite(path: Path, rows: list[dict]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def now_path(state_dir: Path | str) -> Path:
    """The snapshot sits beside ``state.json``, not in ``meters/``: it is replaced
    every few seconds, and a rename in ``meters/`` would make every dashboard
    that watches that directory reload each worker's meter file."""
    return Path(state_dir) / NOW


def write_now(state_dir: Path | str, data: dict) -> None:
    path = now_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{NOW}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def read_now(state_dir: Path | str) -> dict | None:
    try:
        data = json.loads(now_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# -- downsampling -------------------------------------------------------------
def _stat(vals: list[float]) -> list[float]:
    return [round(min(vals), 2), round(sum(vals) / len(vals), 2), round(max(vals), 2)]


def aggregate(rows: list[dict]) -> list[dict]:
    """Fold sample rows into one row per minute; other rows pass through.

    A minute row keeps, for each scalar, ``[min, avg, max]``; for pressure the
    same per resource; for each build and worker ``[avg cores, max anon MiB]``
    (the figures a capacity estimate uses); and ``bo``, the builds of the
    minute that were another swarm's."""
    minutes: dict[int, list[dict]] = {}
    out: list[dict] = []
    for row in rows:
        if row.get("k") != "s":
            out.append(row)
            continue
        minutes.setdefault(int(row["ts"] // 60), []).append(row)
    for minute in sorted(minutes):
        group = minutes[minute]
        agg: dict = {"ts": minute * 60.0, "k": "m", "n": len(group)}
        for key in SCALARS:
            vals = [r[key] for r in group if isinstance(r.get(key), (int, float))]
            if vals:
                agg[key] = _stat(vals)
        psi: dict[str, list[float]] = {}
        for r in group:
            for k, v in (r.get("psi") or {}).items():
                psi.setdefault(k, []).append(v)
        if psi:
            agg["psi"] = {k: _stat(v) for k, v in psi.items()}
        for key in ("b", "w"):
            per: dict[str, list[list[float]]] = {}
            for r in group:
                for name, pair in (r.get(key) or {}).items():
                    per.setdefault(name, []).append(pair)
            if per:
                agg[key] = {
                    name: [round(sum(p[0] for p in pairs) / len(pairs), 2),
                           round(max(p[1] for p in pairs), 1)]
                    for name, pairs in per.items()
                }
        others = list(dict.fromkeys(bid for r in group for bid in r.get("bo") or []))
        if others:
            agg["bo"] = others
        for key in ("x", "o"):
            xs = [r[key] for r in group if isinstance(r.get(key), list)]
            if xs:
                agg[key] = [round(sum(x[0] for x in xs) / len(xs), 2),
                            round(max(x[1] for x in xs), 1)]
        out.append(agg)
    out.sort(key=lambda r: r.get("ts") or 0)
    return out


def compact(state_dir: Path | str, now: float) -> dict:
    """Age the full-resolution file into minute rows and enforce every bound.

    Returns what it did (for the sampler's log line and the tests)."""
    root = meters_dir(state_dir)
    full, agg_path = root / FULL, root / AGG
    cutoff = now - FULL_KEEP_S
    first = _first_ts(full)
    if first is not None and first >= cutoff and _size(full) <= MAX_FULL_BYTES:
        rows = []  # nothing has aged yet: no need to parse a day of samples
    else:
        rows = read_rows(full)
    if _size(full) > MAX_FULL_BYTES and rows:
        # Busier than the cap allows for a whole day: keep the newest half.
        cutoff = max(cutoff, rows[len(rows) // 2].get("ts") or cutoff)
    old = [r for r in rows if (r.get("ts") or 0) < cutoff]
    keep = [r for r in rows if (r.get("ts") or 0) >= cutoff]
    moved = aggregate(old) if old else []
    for row in moved:
        append(agg_path, row)
    if old:
        _rewrite(full, keep)
    dropped = _trim(agg_path, now - AGG_KEEP_S, MAX_AGG_BYTES)
    dropped_builds = _trim(root / BUILDS, None, MAX_BUILDS_BYTES)
    return {"aged": len(old), "minute_rows": len(moved), "kept": len(keep),
            "dropped": dropped, "dropped_builds": dropped_builds}


def _first_ts(path: Path) -> float | None:
    try:
        with path.open("rb") as fh:
            row = json.loads(fh.readline())
    except (OSError, ValueError):
        return None
    return row.get("ts") if isinstance(row, dict) else None


def _trim(path: Path, older_than: float | None, max_bytes: int) -> int:
    """Drop rows older than ``older_than``, then the oldest half while over ``max_bytes``."""
    size = _size(path)
    if not size:
        return 0
    if size <= max_bytes:
        first = _first_ts(path) if older_than is not None else None
        if older_than is None or first is None or first >= older_than:
            return 0  # within both bounds: nothing to parse
    rows = read_rows(path)
    kept = rows if older_than is None else [r for r in rows if (r.get("ts") or r.get("ended") or 0) >= older_than]
    over = size > max_bytes
    if over:
        kept = kept[len(kept) // 2:]
    if len(kept) == len(rows) and not over:
        return 0
    _rewrite(path, kept)
    return len(rows) - len(kept)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def sizes(state_dir: Path | str) -> dict[str, int]:
    root = meters_dir(state_dir)
    out = {name: _size(root / name) for name in (FULL, AGG, BUILDS)}
    out[NOW] = _size(now_path(state_dir))
    return out


# -- reading back -------------------------------------------------------------
def history(state_dir: Path | str, since: float) -> list[dict]:
    """Every sample-like row since ``since``, oldest first: minute rows from the
    aggregate file, then full rows. Use :func:`val` to read a figure from either."""
    root = meters_dir(state_dir)
    rows = [r for r in read_rows(root / AGG, since) if r.get("k") == "m"]
    first_full = None
    full = [r for r in read_rows(root / FULL, since) if r.get("k") == "s"]
    if full:
        first_full = full[0]["ts"]
        rows = [r for r in rows if r["ts"] < first_full]
    return rows + full


def val(row: dict, key: str, how: str = "avg") -> float | None:
    """A figure of a full row (a number) or a minute row (``[min, avg, max]``)."""
    v = row.get(key)
    return _pick(v, how)


def psi_val(row: dict, key: str, how: str = "max") -> float | None:
    return _pick((row.get("psi") or {}).get(key), how)


def _pick(v, how: str) -> float | None:
    if isinstance(v, list) and len(v) == 3:
        return v[{"min": 0, "avg": 1, "max": 2}[how]]
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def dirs_rows(state_dir: Path | str, since: float) -> list[dict]:
    root = meters_dir(state_dir)
    rows = read_rows(root / AGG, since) + read_rows(root / FULL, since)
    return sorted((r for r in rows if r.get("k") == "dirs"), key=lambda r: r["ts"])


def builds(state_dir: Path | str, since: float | None = None) -> list[dict]:
    rows = read_rows(meters_dir(state_dir) / BUILDS)
    if since is not None:
        rows = [r for r in rows if (r.get("ended") or 0) >= since]
    return rows


def recent_build_ids(state_dir: Path | str, limit: int = 2000) -> set[str]:
    rows = read_rows(meters_dir(state_dir) / BUILDS)
    return {str(r.get("id")) for r in rows[-limit:] if r.get("id")}
