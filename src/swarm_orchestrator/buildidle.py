"""Idle yield: a build that holds a slot but does no work stops counting.

A command can sit in a build slot for half an hour at 0% CPU (a script waiting
on a timeout, a test runner waiting on a server that never comes up) while
every other build queues behind it. Nothing here stops or signals such a
holder; it ends when it ends. It is *set aside*: it no longer counts against
``[build].max_concurrent``, so the next waiter starts beside it.

**Who measures.** The waiters. Whoever is queued reads ``/proc`` for each
holder's processes every :data:`SAMPLE_S` and keeps the running figures in
``<state>/buildsem/idle.json``, under ``queue.lock`` like every other decision
of the gate. So it needs no daemon and no particular supervisor: when nobody
waits nobody needs the answer. A waiter that dies leaves its last sample for
the next one, and the counters it compares are the kernel's cumulative ones,
so a pause in sampling loses nothing. One more process measures, for the
record's sake only: the ``swarm build`` of a holder that *is* set aside keeps
sampling while nobody waits, so its wake-up is logged when it happens. No
decision depends on it; a build never starts beside a set-aside holder
without the starter's own fresh sample.

**What is measured.** The holder's process tree: the build's root and every
descendant, plus every process that still has the holder's seat file open
(a child that detached keeps the inherited descriptor). CPU is
``utime+stime+cutime+cstime`` summed over it, which counts reaped children
once; disk IO is ``read_bytes+write_bytes`` of ``/proc/<pid>/io``.

**The rule.**

- *Quiet*: over one sample the tree used under :data:`IDLE_CORES` of a core and
  under :data:`IDLE_IO_BPS` of disk IO. Any other sample restarts the quiet
  clock, so a pause between compile and test, behind a lock or while linking
  never adds up to a window.
- *Yield*: quiet for ``[build].idle_yield_s`` in a row, and fewer than
  ``[build].idle_yield_max`` holders set aside already.
- *Wake*: a set-aside holder that uses :data:`WAKE_CORES` of a core (or
  :data:`WAKE_IO_BPS`) over a sample counts again from that sample; no new
  build starts while the builds that count fill the slots, and a build that
  started beside it keeps running. The quiet clock restarts, so the earliest
  it can be set aside again is a full window later. Between the two thresholds
  nothing changes, so a holder hovering at the edge does not flip.

**Work that happens elsewhere is never idle.** ``docker build`` does its work
in the docker daemon; its client waits at 0% CPU. A command the classifier
reads as such (:func:`buildclass.daemon_side`) never yields, and neither does
any holder while one of :data:`buildclass.DAEMON_CLIENTS` is alive in its
tree, which covers a script whose contents could not be read. A tree with a
process whose IO cannot be read (another user's) is never idle either.

**Nothing can leak.** The yield mark names a build id and is only honoured for
a holder whose seat lock is still held, and only while the sample behind it is
fresh (:data:`FRESH_S` before a build starts beside it). A dead holder's seat
is free and its mark is dropped; a mark nobody refreshed is re-measured before
it is used. Whatever the marks say, the seats cap the builds alive at
``max_concurrent + idle_yield_max`` (see :mod:`buildsem`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import buildclass
from .config import Config
from .resources import ptree

STATE = "idle.json"
#: A waiter measures the holders this often; faster when ``idle_yield_s`` is
#: short, so a window always holds several samples.
SAMPLE_S = 5.0
#: A build starts beside a set-aside holder only on a measurement this fresh.
FRESH_S = 1.0
#: Under this much of one core (and :data:`IDLE_IO_BPS`) a sample is quiet.
IDLE_CORES = 0.05
#: A set-aside holder using this much counts again.
WAKE_CORES = 0.5
#: Bytes read from or written to storage per second: quiet below, awake above.
IDLE_IO_BPS = 256 * 1024
WAKE_IO_BPS = 4 * 1024 * 1024


def enabled(cfg: Config) -> bool:
    return cfg.build_idle_yield_s > 0 and cfg.build_idle_yield_max > 0


def sample_every(cfg: Config) -> float:
    return min(SAMPLE_S, max(0.1, cfg.build_idle_yield_s / 6))


def fresh_s(cfg: Config) -> float:
    return min(FRESH_S, sample_every(cfg))


# -- measuring one holder --------------------------------------------------
@dataclass
class Sample:
    """A holder's tree at one instant: cumulative CPU seconds and IO bytes."""

    cpu_s: float = 0.0
    io: int = 0
    procs: int = 0
    busy: str | None = None  # why it is not idle whatever the counters say


def _has_open(pid: int, target: str, root: Path) -> bool:
    try:
        with os.scandir(root / str(pid) / "fd") as fds:
            for fd in fds:
                try:
                    if os.readlink(fd.path) == target:
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def members(rec: dict, seat: Path | None, table: dict[int, ptree.Proc],
            kids: dict[int, list[int]], root: Path = ptree.PROC) -> set[int]:
    """The holder's processes: its build's tree, whoever still has its seat
    file open, and their descendants. The ``swarm build`` process that only
    waits for the build is left out (its own CPU is the queue's, not the build's)."""
    pid, gate = rec.get("pid"), rec.get("gate_pid")
    roots: set[int] = set()
    proc = table.get(pid) if isinstance(pid, int) else None
    if proc is not None and proc.start == rec.get("pid_start"):
        roots.add(pid)
    found = ptree.tree(kids, roots)
    if seat is not None:
        target = os.path.realpath(seat)
        since = rec.get("gate_start")
        for p in table.values():  # whoever inherited the seat is younger than the gate
            if p.pid in found or p.state in ("Z", "X"):
                continue
            if isinstance(since, int) and p.start < since:
                continue
            if _has_open(p.pid, target, root):
                roots.add(p.pid)
        found = ptree.tree(kids, roots)
    if isinstance(gate, int) and gate != pid:
        found.discard(gate)
    return found


def measure(rec: dict, seat: Path | None, table: dict[int, ptree.Proc],
            kids: dict[int, list[int]], root: Path = ptree.PROC) -> Sample:
    pids = members(rec, seat, table, kids, root)
    s = Sample(procs=len(pids))
    if not pids:
        s.busy = "its processes cannot be seen"
        return s
    for pid in pids:
        proc = table.get(pid)
        if proc is None:
            continue
        s.cpu_s += proc.cpu / ptree.TICK
        if proc.state in ("Z", "X"):
            continue
        name = ptree.comm(pid, root)
        if name in buildclass.DAEMON_CLIENTS:
            s.busy = f"{name} is running (its work happens in a daemon)"
        try:
            text = (root / str(pid) / "io").read_text()
        except PermissionError:
            s.busy = s.busy or "a process in its tree cannot be measured"
            continue
        except OSError:
            continue  # gone between the scan and the read
        rd, wr = ptree.parse_io(text)
        s.io += rd + wr
    return s


# -- the running figures ---------------------------------------------------
def _path(cfg: Config) -> Path:
    return cfg.buildsem_dir / STATE


def load(cfg: Config) -> dict:
    """``{"ts": last sample, "h": {build id: entry}}``; empty if none or unreadable."""
    try:
        data = json.loads(_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("h"), dict):
        return {"ts": 0.0, "h": {}}
    return data


def _save(cfg: Config, st: dict) -> None:
    path = _path(cfg)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    try:
        tmp.write_text(json.dumps(st, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


@dataclass
class Change:
    kind: str  # "yield" | "unyield"
    rec: dict  # the holder's record
    idle_s: float  # yield: how long it was quiet; unyield: how long it was set aside
    why: str = ""


def advance(entry: dict | None, rec: dict, s: Sample, now: float, window: float,
            every: float, room: bool) -> tuple[dict, Change | None]:
    """Fold one sample into a holder's entry. ``room``: may one more holder be
    set aside. The entry keeps the last counters (``t``, ``cpu``, ``io``), when
    the quiet stretch began (``quiet``), when it was set aside (``yielded``),
    and ``ok``: whether the last sample could tell what it is doing *now*."""
    start = float(rec.get("start_ts") or now)
    if entry is None:
        # A build starts with nothing used: its start is a sample nobody took.
        entry = {"t": start, "cpu": 0.0, "io": 0, "quiet": start, "yielded": None, "ok": True}
    dt = now - float(entry["t"])
    if dt <= 0:
        return entry, None
    span = min(dt, 2 * every)
    dcpu, dio = s.cpu_s - float(entry["cpu"]), s.io - int(entry["io"])
    change = None
    if entry.get("yielded") is None:
        # A long gap gets no bigger allowance than a short one, so a stretch
        # nobody watched only counts as quiet if next to nothing happened in it.
        quiet = (s.busy is None and abs(dcpu) < IDLE_CORES * span
                 and abs(dio) < IDLE_IO_BPS * span)
        if not quiet:
            entry["quiet"] = now
        elif room and now - float(entry["quiet"]) >= window:
            entry["yielded"], entry["idle_s"] = now, round(now - float(entry["quiet"]), 1)
            change = Change("yield", rec, now - float(entry["quiet"]))
        entry["ok"] = True
    else:
        still = abs(dcpu) < IDLE_CORES * span and abs(dio) < IDLE_IO_BPS * span
        if dt <= 2 * every:
            woke = dcpu >= WAKE_CORES * span or dio >= WAKE_IO_BPS * span
        else:  # nobody watched: only a gap busy on average is surely awake
            woke = dcpu >= WAKE_CORES * dt or dio >= WAKE_IO_BPS * dt
        if s.busy is not None or woke:
            why = s.busy or ("it is using CPU again" if dcpu >= WAKE_CORES * span
                             or dcpu >= WAKE_CORES * dt else "it is doing disk IO again")
            change = Change("unyield", rec, now - float(entry["yielded"]), why)
            entry.update(yielded=None, quiet=now, ok=True)
            entry.pop("idle_s", None)
        else:
            entry["ok"] = still or dt <= 2 * every
    entry.update(t=round(now, 3), cpu=round(s.cpu_s, 3), io=s.io, busy=s.busy,
                 procs=s.procs)
    return entry, change


def update(cfg: Config, holders: list[dict], now: float, force: bool = False,
           root: Path = ptree.PROC) -> tuple[dict, list[Change]]:
    """Measure the holders if a sample is due (or ``force``d and the last one is
    older than :func:`fresh_s`); returns the state and what changed. Call under
    ``queue.lock``. ``holders`` are live seat records, each with ``seat_path``."""
    if not enabled(cfg):
        return {"ts": 0.0, "h": {}}, []
    st = load(cfg)
    every = sample_every(cfg)
    age = now - float(st.get("ts") or 0.0)
    old = st["h"]
    live = {h["id"]: h for h in holders if h.get("id")}
    unsure = any(not e.get("ok", True) for bid, e in old.items() if bid in live)
    if 0 <= age < every and not ((force or unsure) and age >= fresh_s(cfg)):
        return st, []
    table = ptree.scan(root)
    kids = ptree.children(table)
    changes: list[Change] = []
    out: dict[str, dict] = {}
    set_aside = sum(1 for bid in live if (old.get(bid) or {}).get("yielded"))
    for bid, rec in sorted(live.items(), key=lambda kv: kv[1].get("start_ts") or 0.0):
        if rec.get("noyield"):
            continue
        entry = old.get(bid)
        was = bool(entry and entry.get("yielded"))
        sample = measure(rec, rec.get("seat_path"), table, kids, root)
        entry, change = advance(entry, rec, sample, now, float(cfg.build_idle_yield_s),
                                every, set_aside < cfg.build_idle_yield_max)
        set_aside += bool(entry.get("yielded")) - was
        if change is not None:
            changes.append(change)
        out[bid] = entry
    st = {"ts": round(now, 3), "h": out}
    _save(cfg, st)
    return st, changes


def set_aside(cfg: Config, st: dict, holders: list[dict], now: float) -> list[dict]:
    """The holders that do not count now: marked, still alive, their last
    sample fresh enough to trust, and at most ``idle_yield_max`` of them."""
    if not enabled(cfg):
        return []
    limit = 2 * sample_every(cfg) + 1.0
    out = []
    for h in holders:
        e = st["h"].get(h.get("id") or "")
        if e and e.get("yielded") and e.get("ok", True) and now - float(e["t"]) <= limit \
                and not h.get("noyield"):
            out.append(dict(h, yielded_ts=e["yielded"], idle_s=e.get("idle_s")))
    out.sort(key=lambda h: h["yielded_ts"])
    return out[: cfg.build_idle_yield_max]


def describe(cfg: Config, st: dict, rec: dict, now: float) -> dict:
    """What the last sample says about one holder, for ``--status`` and the
    waiting line: ``yielded_s``/``idle_s`` once set aside, else ``quiet_s`` (how
    long it has been quiet) and ``busy_why`` (what rules idleness out), or
    ``noyield`` (why it never yields)."""
    out: dict = {}
    if rec.get("noyield"):
        out["noyield"] = rec["noyield"]
        return out
    e = st["h"].get(rec.get("id") or "") if enabled(cfg) else None
    if not e:
        return out
    if e.get("yielded"):
        out["yielded_s"] = max(0.0, now - float(e["yielded"]))
        out["idle_s"] = e.get("idle_s")
    else:
        out["quiet_s"] = max(0.0, float(e["t"]) - float(e["quiet"]))
        if e.get("busy"):
            out["busy_why"] = e["busy"]
    out["sampled_s"] = max(0.0, now - float(e["t"]))
    return out
