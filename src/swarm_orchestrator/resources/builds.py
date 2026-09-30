"""Which build holds each gate slot, and what it cost.

Two sources, in order of preference:

1. **The gate's event log**, ``<state>/buildsem/events.jsonl``: one JSON object
   per line — ``queued``, ``start`` (with the pid that runs the build, its slot,
   class, argv, cwd and how long it waited), ``end`` (run time and exit code),
   ``bypass`` and ``preflight_fail``. A build killed with SIGKILL never writes
   its ``end``, so a ``start`` whose process is gone counts as ended.
2. **The slot's lock holder**, when there is no event log: each slot is a
   ``flock`` on ``buildsem/slotN``, and ``/proc/locks`` names the pid holding
   it (matched on the slot file's device and inode). That pid is the build.

Either way the build is a pid; its *tree* (the pid and every descendant) is
what is measured each sample (:mod:`.ptree`). A :class:`Build` keeps the running
figures; when it ends, :meth:`Build.summary` is the row ``meters/builds.jsonl``
gets.
"""

from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import ptree

EVENTS = "events.jsonl"
#: On a (re)start the event log is read from at most this far back: enough to
#: find the builds still running, without parsing months of history.
TAIL_BYTES = 1 << 20
#: A build running this long with its whole tree under :data:`IDLE_CORES` of CPU
#: is an idle holder: it keeps every other build queued while doing nothing.
IDLE_CORES = 0.01
_PSI = ("cpu", "mem", "memf", "io", "iof")


class EventTail:
    """Reads new lines of the gate's event log, from where it left off."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset: int | None = None
        self._part = b""

    def exists(self) -> bool:
        return self.path.is_file()

    def poll(self) -> list[dict]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if self.offset is None or size < self.offset:  # first read, or rotated
            self.offset, self._part = max(0, size - TAIL_BYTES), b""
            skip_partial = self.offset > 0
        else:
            skip_partial = False
        if size == self.offset:
            return []
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                data = fh.read(size - self.offset)
        except OSError:
            return []
        self.offset += len(data)
        data = self._part + data
        lines = data.split(b"\n")
        self._part = lines.pop()
        if skip_partial and lines:
            lines = lines[1:]
        out = []
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and isinstance(ev.get("event"), str):
                out.append(ev)
        return out


def slot_files(buildsem: Path) -> dict[tuple[int, int, int], int]:
    """``{(major, minor, inode): slot}`` for every ``slotN`` file."""
    out: dict[tuple[int, int, int], int] = {}
    try:
        entries = list(buildsem.iterdir())
    except OSError:
        return out
    for entry in entries:
        name = entry.name
        if not (name.startswith("slot") and name[4:].isdigit()):
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        out[(os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)] = int(name[4:])
    return out


def flock_holders(locks_text: str, slots: dict[tuple[int, int, int], int]) -> dict[int, int]:
    """``{slot: pid}`` from ``/proc/locks``: who holds each slot's ``flock``.

    A line reads ``N: FLOCK ADVISORY WRITE <pid> <maj>:<min>:<inode> 0 EOF``
    (device numbers in hex); a waiter's line carries ``->`` and holds nothing."""
    out: dict[int, int] = {}
    for line in locks_text.splitlines():
        parts = line.split()
        if len(parts) < 6 or "->" in parts or parts[1] != "FLOCK":
            continue
        try:
            pid = int(parts[4])
            major, minor, inode = parts[5].split(":")
            key = (int(major, 16), int(minor, 16), int(inode))
        except ValueError:
            continue
        slot = slots.get(key)
        if slot is not None and pid > 0:
            out[slot] = pid
    return out


@dataclass
class Build:
    """One gated build, from its start (or first sighting) to its end."""

    id: str
    pid: int
    started: float
    source: str  # "events" | "flock"
    phase: str | None = None
    slot: int | None = None
    cls: str = "heavy"
    argv: str = ""
    cwd: str = ""
    wait_s: float | None = None
    jobs: int | None = None
    start_ticks: int | None = None
    partial: bool = False  # first seen already running: the early part is missing
    # running figures
    base: tuple[float, int, int] | None = None
    cpu_s: float = 0.0
    rd: int = 0
    wr: int = 0
    peak_cores: float = 0.0
    peak_anon_mb: float = 0.0
    peak_rss_mb: float = 0.0
    min_avail_mb: float | None = None
    psi_max: dict = field(default_factory=dict)
    samples: int = 0
    last_ts: float | None = None
    idle_flagged: bool = False
    # the end, once known
    ended: float | None = None
    run_s: float | None = None
    exit: int | None = None
    ended_by: str | None = None
    history: deque = field(default_factory=deque)  # (ts, cpu_s) for idle detection
    cores: float = 0.0
    anon_mb: float = 0.0
    tree: set = field(default_factory=set)

    def observe(self, now: float, use: ptree.Usage, host_row: dict | None,
                fresh: bool, idle_s: float) -> None:
        """Fold one sample of the build's tree into the running figures.

        ``fresh``: the root process started with the build, so everything its
        tree ever used is the build's (baseline zero); otherwise the tree's
        totals at first sight are the baseline."""
        if self.base is None:
            self.base = (0.0, 0, 0) if fresh else (use.cpu_s, use.rd_bytes, use.wr_bytes)
        cpu = max(self.cpu_s, use.cpu_s - self.base[0])
        if self.last_ts is not None and now > self.last_ts:
            self.cores = max(0.0, cpu - self.cpu_s) / (now - self.last_ts)
            self.peak_cores = max(self.peak_cores, self.cores)
        self.cpu_s = cpu
        self.rd = max(self.rd, use.rd_bytes - self.base[1])
        self.wr = max(self.wr, use.wr_bytes - self.base[2])
        self.anon_mb = use.anon_mb
        self.peak_anon_mb = max(self.peak_anon_mb, use.anon_mb)
        self.peak_rss_mb = max(self.peak_rss_mb, use.rss_mb)
        if host_row:
            avail = host_row.get("avail_mb")
            if avail is not None:
                self.min_avail_mb = avail if self.min_avail_mb is None else min(
                    self.min_avail_mb, avail)
            for key, val in (host_row.get("psi") or {}).items():
                self.psi_max[key] = max(self.psi_max.get(key, 0.0), val)
        self.samples += 1
        self.last_ts = now
        self.history.append((now, cpu))
        while len(self.history) > 2 and self.history[1][0] <= now - idle_s:
            self.history.popleft()

    def idle_for(self, now: float, idle_s: float) -> bool:
        """Has the whole tree stayed under :data:`IDLE_CORES` for ``idle_s``?"""
        if now - self.started < idle_s or not self.history:
            return False
        old_ts, old_cpu = self.history[0]
        if old_ts > now - idle_s:
            return False  # not watched long enough (sampler restarted, say)
        return self.cpu_s - old_cpu < IDLE_CORES * (now - old_ts)

    def summary(self) -> dict:
        end = self.ended if self.ended is not None else self.last_ts or self.started
        run = self.run_s if self.run_s is not None else max(0.0, end - self.started)
        return {
            "id": self.id, "phase": self.phase, "argv": self.argv, "cwd": self.cwd,
            "slot": self.slot, "cls": self.cls, "source": self.source, "pid": self.pid,
            "jobs": self.jobs, "started": round(self.started, 3), "ended": round(end, 3),
            "wait_s": _r(self.wait_s), "run_s": _r(run), "exit": self.exit,
            "ended_by": self.ended_by, "partial": self.partial,
            "cpu_s": _r(self.cpu_s), "avg_cores": _r(self.cpu_s / run) if run > 0 else None,
            "peak_cores": _r(self.peak_cores), "peak_anon_mb": _r(self.peak_anon_mb),
            "peak_rss_mb": _r(self.peak_rss_mb), "min_avail_mb": self.min_avail_mb,
            "psi_max": {k: self.psi_max[k] for k in _PSI if k in self.psi_max},
            "rd_mb": _r(self.rd / 2**20), "wr_mb": _r(self.wr / 2**20),
            "samples": self.samples, "idle_flagged": self.idle_flagged,
        }

    def now_row(self, now: float, idle_s: float) -> dict:
        return {
            "id": self.id, "phase": self.phase, "pid": self.pid, "slot": self.slot,
            "argv": self.argv[:200], "age_s": round(now - self.started, 1),
            "cores": round(self.cores, 2), "anon_mb": round(self.anon_mb, 1),
            "peak_anon_mb": round(self.peak_anon_mb, 1), "cpu_s": round(self.cpu_s, 1),
            "procs": len(self.tree), "idle": self.idle_for(now, idle_s),
            "source": self.source,
        }


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 2)


class BuildBook:
    """The builds in flight, fed by whichever source the gate provides."""

    def __init__(self, buildsem: Path, env_marker: str = "SWARM_PHASE",
                 proc_root: Path = ptree.PROC, done_ids: set[str] | None = None) -> None:
        self.buildsem = buildsem
        self.env_marker = env_marker
        self.proc = proc_root
        self.tail = EventTail(buildsem / EVENTS)
        self.active: dict[str, Build] = {}
        self.queued: dict[str, dict] = {}
        self.finished: list[Build] = []
        self.done_ids = set(done_ids or ())
        self._boot = ptree.boot_time(proc_root)
        self._first = True

    @property
    def source(self) -> str:
        return "events" if self.tail.exists() else "flock"

    # -- discovery ----------------------------------------------------------
    def poll(self, now: float, table: dict[int, ptree.Proc] | None = None) -> bool:
        """Pick up builds that started or ended. True when anything changed."""
        before = (set(self.active), len(self.finished))
        if self.tail.exists():
            for ev in self.tail.poll():
                self._event(ev, now, table)
        else:
            self._flock(now, table)
        self._first = False
        return before != (set(self.active), len(self.finished))

    def _proc_start_epoch(self, ticks: int | None) -> float | None:
        if ticks is None or not self._boot:
            return None
        return self._boot + ticks / ptree.TICK

    def _identity(self, pid: int, table: dict[int, ptree.Proc] | None) -> int | None:
        if table is not None:
            proc = table.get(pid)
            return proc.start if proc is not None and proc.state not in ("Z", "X") else None
        try:
            text = (self.proc / str(pid) / "stat").read_text()
        except OSError:
            return None
        proc = ptree.parse_stat(pid, text)
        return proc.start if proc is not None and proc.state not in ("Z", "X") else None

    def _event(self, ev: dict, now: float, table: dict[int, ptree.Proc] | None) -> None:
        kind, bid = ev.get("event"), str(ev.get("id") or "")
        if not bid:
            return
        if kind == "queued":
            self.queued[bid] = ev
        elif kind in ("bypass", "preflight_fail"):
            self.queued.pop(bid, None)
        elif kind == "start":
            self.queued.pop(bid, None)
            if ev.get("cls", "heavy") != "heavy" or bid in self.done_ids or bid in self.active:
                return
            self._start_from_event(ev, bid, now, table)
        elif kind == "end":
            self.queued.pop(bid, None)
            b = self.active.get(bid)
            if b is not None:
                b.ended = float(ev.get("ts") or now)
                b.run_s = _num(ev.get("run_s"))
                b.exit = ev.get("exit") if isinstance(ev.get("exit"), int) else None
                b.ended_by = "end"

    def _start_from_event(self, ev: dict, bid: str, now: float,
                          table: dict[int, ptree.Proc] | None) -> None:
        try:
            pid = int(ev.get("pid") or 0)
        except (TypeError, ValueError):
            return
        ts = _num(ev.get("ts")) or now
        wait = _num(ev.get("wait_s"))
        ticks = self._identity(pid, table) if pid > 0 else None
        began = self._proc_start_epoch(ticks)
        if ticks is None or (began is not None and began > ts + 5):
            ticks = None  # gone already, or the pid now names another process
        b = Build(id=bid, pid=pid, started=ts, source="events", phase=ev.get("phase"),
                  slot=ev.get("slot"), cls=ev.get("cls", "heavy"), argv=str(ev.get("argv") or ""),
                  cwd=str(ev.get("cwd") or ""), wait_s=wait, start_ticks=ticks)
        self._environ(b)
        self.active[bid] = b

    def _flock(self, now: float, table: dict[int, ptree.Proc] | None) -> None:
        try:
            locks = (self.proc / "locks").read_text()
        except OSError:
            locks = ""
        holders = flock_holders(locks, slot_files(self.buildsem))
        held = {}
        for slot, pid in holders.items():
            ticks = self._identity(pid, table)
            if ticks is None:
                continue
            bid = f"lock-{pid}-{ticks}"
            held[bid] = (slot, pid, ticks)
        for bid, (slot, pid, ticks) in held.items():
            if bid in self.active or bid in self.done_ids:
                continue
            began = self._proc_start_epoch(ticks)
            started = now
            if self._first and began is not None:
                started = began  # already running when we looked: count from its start
            b = Build(id=bid, pid=pid, started=started, source="flock", slot=slot,
                      argv=ptree.cmdline(pid, self.proc), cwd=ptree.cwd(pid, self.proc),
                      start_ticks=ticks, partial=self._first)
            self._environ(b)
            self.active[bid] = b
        for bid, b in self.active.items():
            if b.source == "flock" and bid not in held and b.ended is None:
                b.ended, b.ended_by = (b.last_ts or now), "released"

    def _environ(self, b: Build) -> None:
        env = ptree.environ(b.pid, self.proc) if b.start_ticks is not None else {}
        b.phase = b.phase or env.get(self.env_marker) or None
        try:
            b.jobs = int(env["CARGO_BUILD_JOBS"]) if env.get("CARGO_BUILD_JOBS") else None
        except ValueError:
            b.jobs = None

    # -- measurement --------------------------------------------------------
    def measure(self, now: float, table: dict[int, ptree.Proc],
                kids: dict[int, list[int]], host_row: dict | None, idle_s: float) -> None:
        """Sample every active build's tree; retire the ones that are over."""
        for bid in list(self.active):
            b = self.active[bid]
            proc = table.get(b.pid)
            alive = (b.start_ticks is not None and proc is not None
                     and proc.start == b.start_ticks and proc.state not in ("Z", "X"))
            if alive and b.ended is None:
                b.tree = ptree.tree(kids, {b.pid})
                use = ptree.usage(table, b.tree, self.proc)
                began = self._proc_start_epoch(b.start_ticks)
                slack = (b.wait_s or 0.0) + 5.0
                fresh = began is None or began >= b.started - slack or b.partial
                b.observe(now, use, host_row, fresh, idle_s)
                continue
            if b.ended is None:
                b.ended, b.ended_by = (b.last_ts or now), "gone"
            self._retire(bid)

    def _retire(self, bid: str) -> None:
        b = self.active.pop(bid)
        b.tree = set()
        self.done_ids.add(bid)
        self.finished.append(b)

    def take_finished(self) -> list[Build]:
        out, self.finished = self.finished, []
        return out

    def busy(self) -> bool:
        return bool(self.active)


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
