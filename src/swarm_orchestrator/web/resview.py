"""The Resources tab: what ``swarm resources`` prints, as the board's JSON.

Four views, each cached and served with an ETag like the rest of the board:

``now``       the sampler's latest snapshot (host, disk, the builds running, the
              sessions) and the build gate's queue with its waits;
``window``    one history window from :mod:`.reshist`, with the builds that ran
              in it marked on the time axis and how many were queued;
``table``     the finished builds, filtered and sorted here (the page holds one
              screen of them, so "worst of the month" must be picked server-side);
``capacity``  the scenarios and their arithmetic, from
              :func:`resources.capacity.analyse` itself, so the figures are the
              ones ``swarm resources`` prints.

**Cheap by construction.** The snapshot is a 3 kB file. History and the builds
are read once and then only as far as they grew (:class:`reshist.Tail`). The
gate is asked at most every :data:`GATE_EVERY_S`. Capacity is the one view that
needs the month's samples, so it is worked out at most every
:data:`CAPACITY_EVERY_S`, from rows streamed off the files rather than loaded,
and only while somebody has the tab open.

**What leaves the box.** A build's command line is shown, because it is what
tells one build from another, but every string from the sampler's files has its
absolute paths shortened (the state dir, the project and the home directory
become ``<state>``, ``<project>`` and ``~``), a command is cut to
:data:`CMD_MAX` characters, a build's working directory is reduced to its place
inside the worktree, and all of it passes :func:`redact.deep` like every other
payload.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterator

from .. import buildlog, buildstatus
from ..resources import capacity, host, store, view
from . import reshist
from .redact import deep, redact

CMD_MAX = 96
GATE_EVERY_S = 15.0
CAPACITY_EVERY_S = 600.0
CAPACITY_DAYS = 30.0
#: The builds table: every window of the charts, and everything recorded.
ALL = "all"
TABLE_WINDOWS = (*reshist.WINDOWS, ALL)
#: Columns the table sorts by (a key of a served row).
SORTS = ("ended", "phase", "run_s", "wait_s", "cpu_s", "avg_cores", "peak_cores",
         "peak_anon_mb", "min_avail_mb", "psi_memf", "psi_iof", "exit", "cmd")
LIMIT = 50
MAX_LIMIT = 500
PHASE_RE = re.compile(r"^[A-Za-z0-9._/:-]{0,80}$")
_MAX_VIEWS = 24
_BUILD_NUMBERS = ("jobs", "started", "ended", "wait_s", "run_s", "exit", "cpu_s", "avg_cores",
                  "peak_cores", "peak_anon_mb", "min_avail_mb", "yielded_s")


def _n(v) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def tidy(text, cfg, width: int | None = None) -> str:
    """``text`` with this machine's absolute paths shortened and credentials out."""
    text = str(text or "")
    for root, name in ((cfg.state_dir, "<state>"), (cfg.project_dir, "<project>"),
                       (Path.home(), "~")):
        root = str(root).rstrip("/")
        if len(root) > 1:
            text = text.replace(root, name)
    text = redact(text)
    return buildlog.short_cmd(text, width) if width else text


def scrub(value: Any, cfg) -> Any:
    """:func:`tidy` on every string inside a JSON-shaped value: a field the
    sampler grows later is shortened too, without this module knowing its name."""
    if isinstance(value, str):
        return tidy(value, cfg)
    if isinstance(value, dict):
        return {k: scrub(v, cfg) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, cfg) for v in value]
    return value


# -- now -------------------------------------------------------------------------
def _running(row: dict, cfg, at: float) -> dict:
    """One running build as the page shows it: its command shortened, its start
    as a time (so the page's own clock counts it), no working directory."""
    out = {k: scrub(v, cfg) for k, v in row.items() if k not in ("argv", "cwd", "age_s")}
    out["cmd"] = tidy(row.get("argv"), cfg, CMD_MAX)
    age = _n(row.get("age_s"))
    out["since"] = round(at - age, 1) if age is not None else None
    return out


def sample(snap: dict, cfg) -> dict:
    """The sampler's snapshot, shaped for the page."""
    at = _n(snap.get("ts")) or 0.0
    live = snap.get("host") if isinstance(snap.get("host"), dict) else {}
    builds = [_running(b, cfg, at) for b in snap.get("builds") or [] if isinstance(b, dict)]
    return {
        "ts": at,
        "source": scrub(snap.get("source"), cfg),
        "static": scrub(snap.get("static") or {}, cfg),
        # The per-build and per-session pairs ("b", "w", "x", "o") are the rows
        # below, already; only the host's own figures go out here.
        "host": {k: scrub(v, cfg) for k, v in live.items() if k not in ("b", "w", "x", "o", "k")},
        "disk": scrub(snap.get("disk") or {}, cfg),
        "dirs": scrub(snap.get("dirs"), cfg) if isinstance(snap.get("dirs"), dict) else None,
        "builds": builds,
        "queued": _n(snap.get("queued")) or 0,
        "workers": scrub([w for w in snap.get("workers") or [] if isinstance(w, dict)], cfg),
        "infra": scrub(snap.get("infra"), cfg),
        "console": scrub(snap.get("console"), cfg),
        "idle": [b.get("id") for b in snap.get("idle_holders") or [] if isinstance(b, dict)],
        "idle_s": _n(snap.get("idle_s")),
        "sampler": scrub(snap.get("sampler") or {}, cfg),
    }


def whose(rec: dict) -> dict:
    """Whose a build is, as its record says: ``swarm`` (the slug), ``swarm_name``
    and ``mine`` (whether that is the swarm reading). The gate is the machine's,
    so every view of it holds every swarm's builds; a record that names no
    swarm is the reader's own."""
    return {"swarm": rec.get("swarm") or None, "swarm_name": rec.get("swarm_name") or None,
            "mine": rec.get("mine") is not False}


def gate(cfg) -> dict:
    """The build gate now: each slot's holder and the queue in the order it would
    start, from :func:`buildstatus.snapshot` (what ``swarm build --status`` prints).
    It is the machine's one gate, so the holders and waiters are every swarm's,
    each saying whose it is (:func:`whose`) and whether its swarm stands frozen.
    Every "for how long" is turned into a time, so the page counts it itself."""
    if cfg.build_max_concurrent < 1:
        return {"on": False}
    at = time.time()
    try:
        snap = buildstatus.snapshot(cfg, n_recent=1)
    except Exception:  # noqa: BLE001 - the gate's files are not this page's to trust
        return {"on": True, "error": True, "max_concurrent": cfg.build_max_concurrent,
                "slots": [], "queue": []}

    def at_plus(seconds) -> float | None:
        return round(at + seconds, 1) if _n(seconds) is not None else None

    slots = []
    for h in snap.get("slots") or []:
        run = _n(h.get("running_s"))
        slots.append({
            "slot": h.get("slot"), "busy": bool(h.get("busy")), "unknown": bool(h.get("unknown")),
            "gc": bool(h.get("gc")), "id": h.get("id"), "phase": h.get("phase"),
            **whose(h), "frozen": bool(h.get("frozen")),
            "cmd": tidy(h.get("argv"), cfg, CMD_MAX) if h.get("argv") else "",
            "since": round(at - run, 1) if run is not None else None,
            "usual_s": _n(h.get("pred_s")),
        })
    queue = []
    for t in snap.get("queue") or []:
        wait = _n(t.get("waiting_s"))
        queue.append({
            "id": t.get("id"), "phase": t.get("phase"),
            **whose(t), "frozen": bool(t.get("frozen")),
            "cmd": tidy(t.get("argv"), cfg, CMD_MAX),
            "queued_at": round(at - wait, 1) if wait is not None else None,
            "usual_s": _n(t.get("pred_s")), "starts_at": at_plus(t.get("starts_in_s")),
            "passed": _n(t.get("passed")) or 0, "stale": bool(t.get("stale")),
        })
    return {"on": True, "max_concurrent": snap.get("max_concurrent"),
            "overtake": snap.get("overtake"), "short_s": snap.get("short_s"),
            "slots": slots, "queue": queue}


def now_payload(cfg, gate_view: dict, now: float) -> dict:
    snap, age = view.fresh_now(cfg.state_dir, now)
    return {
        "at": round(now, 1),
        "enabled": bool(getattr(cfg, "resources_enabled", True)),
        "age_s": None if age is None else round(age, 1),
        "stale": snap is None or (age or 0) > view.STALE_S,
        "sample": sample(snap, cfg) if snap is not None else None,
        "gate": gate_view,
        "windows": list(reshist.WINDOWS),
    }


# -- finished builds -------------------------------------------------------------------
def build_row(row: dict, cfg) -> dict:
    """One line of ``builds.jsonl`` as the table shows it. Fields this does not
    know are left out; a missing one is ``None``."""
    out: dict = {"id": str(row.get("id") or ""), "phase": row.get("phase") or None,
                 **whose(row),
                 "cmd": tidy(row.get("argv"), cfg, CMD_MAX),
                 "where": tidy(buildlog.where(cfg, str(row.get("cwd") or "")), cfg, 60)
                 if row.get("cwd") else "",
                 "partial": bool(row.get("partial")), "idle": bool(row.get("idle_flagged"))}
    for key in _BUILD_NUMBERS:
        out[key] = _n(row.get(key))
    psi = row.get("psi_max") if isinstance(row.get("psi_max"), dict) else {}
    out["psi_memf"] = _n(psi.get("memf"))
    out["psi_iof"] = _n(psi.get("iof"))
    return out


class BuildRows:
    """Every finished build, shaped once, kept current from ``builds.jsonl``."""

    def __init__(self, state_dir: Path | str) -> None:
        self._tail = reshist.Tail(store.meters_dir(state_dir) / store.BUILDS)
        self.rows: list[dict] = []

    def update(self, cfg) -> None:
        lines = list(self._tail.lines())
        if self._tail.reset:
            self.rows = []  # trimmed (the oldest half dropped): shape what is left
        for line in lines:
            row = reshist.parse(line)
            if row is not None:
                self.rows.append(build_row(row, cfg))


def table(rows: list[dict], *, now: float, window: str = ALL, sort: str = "ended",
          desc: bool = True, phase: str = "", limit: int = LIMIT) -> dict:
    """The builds table: one window's builds, one phase's, sorted, the first ``limit``."""
    span = reshist.WINDOWS.get(window)
    since = now - span[0] if span else None
    inside = [r for r in rows if since is None or (r.get("ended") or 0) >= since]
    phases = Counter(r["phase"] for r in inside if r.get("phase"))
    want = phase.lower()
    hit = [r for r in inside if want in (r.get("phase") or "").lower()] if want else inside
    known = [r for r in hit if r.get(sort) is not None]
    rest = [r for r in hit if r.get(sort) is None]
    known.sort(key=lambda r: r[sort].lower() if isinstance(r[sort], str) else r[sort],
               reverse=desc)

    def total(key: str) -> float:
        return round(sum(r[key] for r in hit if r.get(key) is not None), 1)

    return {
        "window": window, "sort": sort, "dir": "desc" if desc else "asc", "phase": phase,
        "limit": limit, "total": len(inside), "matched": len(hit),
        "rows": (known + rest)[:limit],
        "phases": [[name, n] for name, n in phases.most_common(300)],
        "sum": {"run_s": total("run_s"), "wait_s": total("wait_s"), "cpu_s": total("cpu_s"),
                "failed": sum(1 for r in hit if r.get("exit") not in (0, None))},
    }


# -- capacity ----------------------------------------------------------------------------
class Rows:
    """The rows :func:`store.history` returns, streamed off the files.

    The same rows in the same order — minute rows older than the first full
    sample, then the full samples — but one at a time: a month of samples is tens
    of megabytes as dicts, and the board lives inside the dashboard's process.
    """

    def __init__(self, state_dir: Path | str, since: float) -> None:
        self._root = store.meters_dir(state_dir)
        self._since = since

    def _read(self, name: str, kind: str) -> Iterator[dict]:
        try:
            fh = (self._root / name).open("rb")
        except OSError:
            return
        with fh:
            for line in fh:
                ts = reshist.ts_of(line)
                if ts is not None and ts < self._since:
                    continue
                row = reshist.parse(line)
                if (row is not None and row.get("k") == kind
                        and (row.get("ts") or 0) >= self._since):
                    yield row

    def __iter__(self) -> Iterator[dict]:
        head = self._read(store.FULL, "s")
        first = next(head, None)
        head.close()
        for row in self._read(store.AGG, "m"):
            if first is None or row["ts"] < first["ts"]:
                yield row
        yield from self._read(store.FULL, "s")


def capacity_payload(cfg, now: float, days: float = CAPACITY_DAYS) -> dict:
    """:func:`capacity.analyse` over the same inputs ``swarm resources`` gives it."""
    since = now - days * 86400
    snap = store.read_now(cfg.state_dir)
    static = (snap or {}).get("static") or host.static()
    out = capacity.analyse(store.builds(cfg.state_dir, since), Rows(cfg.state_dir, since),
                           static, cfg.build_max_concurrent, cfg.build_jobs, cfg.max_workers,
                           cfg.build_pair)
    out = dict(out)
    out.update(days=days, asof=round(now), min_builds=capacity.MIN_BUILDS,
               min_worker_h=capacity.MIN_WORKER_H)
    return out


# -- the cached views ----------------------------------------------------------------
class Resources:
    """The four views, each built when its stamp moves and gzipped once.

    Its own lock, not the feed's: working capacity out takes a second, and the
    board, the graph and the usage chart must not wait behind it.
    """

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.history = reshist.History(self.state_dir)
        self.builds = BuildRows(self.state_dir)
        self._lock = threading.Lock()
        self._views: dict[tuple, tuple] = {}
        self._gate: tuple[tuple, dict] | None = None

    def _view(self, key: tuple, stamp: tuple, make) -> tuple[bytes, bytes, str]:
        hit = self._views.get(key)
        if hit is not None and hit[0] == stamp:
            return hit[1], hit[2], hit[3]
        body = json.dumps(deep(make()), separators=(",", ":"), default=str).encode()
        etag = f'"{hashlib.sha1(body).hexdigest()[:16]}"'
        self._views.pop(key, None)
        self._views[key] = (stamp, body, gzip.compress(body, compresslevel=6), etag)
        while len(self._views) > _MAX_VIEWS:
            self._views.pop(next(iter(self._views)))
        return self._views[key][1:]

    def _gate_view(self, cfg) -> tuple[tuple, dict]:
        """The gate, asked again only when its files moved or it has aged."""
        if cfg.build_max_concurrent < 1:
            return ("off",), {"on": False}
        sem = Path(cfg.buildsem_dir)
        stamp = (_stamp(sem / buildlog.EVENTS), _stamp(sem / "queue"),
                 _stamp(sem / "queue.json"), int(time.time() // GATE_EVERY_S),
                 cfg.build_max_concurrent)
        if self._gate is None or self._gate[0] != stamp:
            self._gate = (stamp, gate(cfg))
        return self._gate

    def now(self, cfg, now: float | None = None) -> tuple[bytes, bytes, str]:
        now = time.time() if now is None else now
        with self._lock:
            gate_stamp, gate_view = self._gate_view(cfg)
            stamp = (_stamp(store.now_path(self.state_dir)), gate_stamp)
            return self._view(("now",), stamp, lambda: now_payload(cfg, gate_view, now))

    def window(self, cfg, name: str, now: float | None = None) -> tuple[bytes, bytes, str]:
        now = time.time() if now is None else now
        step = reshist.WINDOWS[name][1]
        every = min(max(step / 2, 5.0), 300.0)
        with self._lock:
            return self._view(("window", name), (int(now // every),),
                              lambda: self._window(cfg, name, now))

    def _window(self, cfg, name: str, now: float) -> dict:
        self.builds.update(cfg)
        data = self.history.window(name, now)
        t0, step, n = data["t0"], data["step"], data["n"]
        # A neighbour's build is named with its swarm in front, as the gate names it.
        marks = [{"a": r["started"], "b": r["ended"], "phase": buildlog.who(r, "").strip() or None,
                  "cmd": r["cmd"], "exit": r["exit"]}
                 for r in self.builds.rows if r["started"] is not None and r["ended"] is not None]
        waits = [(r["started"] - r["wait_s"], r["started"]) for r in self.builds.rows
                 if r["started"] is not None and r["wait_s"]]
        snap, age = view.fresh_now(self.state_dir, now)
        if snap is not None and (age or 0) <= view.STALE_S:
            at = _n(snap.get("ts")) or now
            for b in snap.get("builds") or []:
                if isinstance(b, dict) and _n(b.get("age_s")) is not None:
                    marks.append({"a": at - b["age_s"], "b": now,
                                  "phase": buildlog.who(b, "").strip() or None,
                                  "cmd": tidy(b.get("argv"), cfg, CMD_MAX), "exit": None,
                                  "live": True})
            for t in self._gate_view(cfg)[1].get("queue") or []:
                if t.get("queued_at") is not None:
                    waits.append((t["queued_at"], now))
        data["queued"] = reshist.level_max(waits, t0, step, n, now)
        data["spans"] = reshist.spans(marks, t0, min(now, t0 + n * step), step)
        data["static"] = scrub((snap or {}).get("static") or {}, cfg)
        data["now"] = round(now, 1)
        return data

    def table(self, cfg, *, window: str = ALL, sort: str = "ended", desc: bool = True,
              phase: str = "", limit: int = LIMIT,
              now: float | None = None) -> tuple[bytes, bytes, str]:
        now = time.time() if now is None else now
        path = store.meters_dir(self.state_dir) / store.BUILDS
        with self._lock:
            stamp = (_stamp(path), 0 if window == ALL else int(now // 60))

            def make() -> dict:
                self.builds.update(cfg)
                return table(self.builds.rows, now=now, window=window, sort=sort, desc=desc,
                             phase=phase, limit=limit)

            return self._view(("table", window, sort, desc, phase, limit), stamp, make)

    def capacity(self, cfg, now: float | None = None) -> tuple[bytes, bytes, str]:
        now = time.time() if now is None else now
        with self._lock:
            stamp = (int(now // CAPACITY_EVERY_S), cfg.build_max_concurrent, cfg.build_jobs,
                     cfg.max_workers)
            return self._view(("capacity",), stamp, lambda: capacity_payload(cfg, now))


def _stamp(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
    except OSError:
        return (0, -1)
    return (st.st_mtime_ns, st.st_size)
