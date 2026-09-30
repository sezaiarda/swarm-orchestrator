"""Process trees from ``/proc``: who is running, and what a tree of them costs.

One :func:`scan` reads ``/proc/<pid>/stat`` for every process (about a
millisecond for a few hundred), which is enough for the tree shape, start times
and CPU time. The costlier per-process files (``statm``, ``io``) are read only
for the processes a tree actually covers (:func:`usage`).

**CPU and IO of a tree include its finished children.** A build is thousands of
short compiler processes; by the time a sample looks, most are gone. The kernel
folds a reaped child's CPU time into its parent's ``cutime``/``cstime`` and its
IO into the parent's ``/proc/<pid>/io``, so summing ``utime+stime+cutime+cstime``
and the IO counters over the *live* members of a tree counts every process that
ever ran in it exactly once (a live child is not yet in its parent's totals).
The sum can dip for a moment (a child exited but is not yet reaped), so callers
keep the running maximum.

Memory is **anon** resident memory (``statm`` resident minus shared), the part
that page cache and shared libraries do not inflate; total RSS rides along.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROC = Path("/proc")
TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_MB = 1024 * 1024


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    state: str
    start: int  # clock ticks since boot
    cpu: int  # utime + stime + cutime + cstime, in clock ticks
    own: int = 0  # utime + stime alone: this process, not its reaped children


@dataclass
class Usage:
    """What a set of processes is using at one instant (CPU and IO cumulative)."""

    cpu_s: float = 0.0
    anon_mb: float = 0.0
    rss_mb: float = 0.0
    rd_bytes: int = 0
    wr_bytes: int = 0
    procs: int = 0


def parse_stat(pid: int, text: str) -> Proc | None:
    """One ``/proc/<pid>/stat`` line; the command name may hold spaces and parens."""
    rest = text[text.rfind(")") + 2:].split()
    if len(rest) < 20:
        return None
    try:
        own = int(rest[11]) + int(rest[12])
        cpu = own + int(rest[13]) + int(rest[14])
        return Proc(pid, int(rest[1]), rest[0], int(rest[19]), cpu, own)
    except ValueError:
        return None


def parse_statm(text: str) -> tuple[float, float] | None:
    """``(anon_mb, rss_mb)`` from ``/proc/<pid>/statm``."""
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        resident, shared = int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return max(0, resident - shared) * PAGE / _MB, resident * PAGE / _MB


def parse_io(text: str) -> tuple[int, int]:
    """``(read_bytes, write_bytes)``: bytes that reached (or were fetched from) storage."""
    vals: dict[str, int] = {}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        if key in ("read_bytes", "write_bytes"):
            try:
                vals[key] = int(val)
            except ValueError:
                pass
    return vals.get("read_bytes", 0), vals.get("write_bytes", 0)


def scan(root: Path = PROC) -> dict[int, Proc]:
    """Every process (zombies too: their CPU time is still in their stat)."""
    out: dict[int, Proc] = {}
    try:
        names = [e.name for e in os.scandir(root) if e.name.isdigit()]
    except OSError:
        return out
    for name in names:
        try:
            with open(root / name / "stat", "rb") as fh:
                text = fh.read().decode(errors="replace")
        except OSError:
            continue
        proc = parse_stat(int(name), text)
        if proc is not None:
            out[proc.pid] = proc
    return out


def children(table: dict[int, Proc]) -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for p in table.values():
        kids.setdefault(p.ppid, []).append(p.pid)
    return kids


def tree(kids: dict[int, list[int]], roots: set[int] | list[int]) -> set[int]:
    """``roots`` and every descendant of them."""
    found = set(roots)
    stack = list(found)
    while stack:
        for child in kids.get(stack.pop(), ()):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def usage(table: dict[int, Proc], pids: set[int], root: Path = PROC) -> Usage:
    """Sum CPU seconds, anon/total RSS and storage IO over ``pids``."""
    u = Usage()
    for pid in pids:
        proc = table.get(pid)
        if proc is None:
            continue
        u.procs += 1
        u.cpu_s += proc.cpu / TICK
        if proc.state in ("Z", "X"):
            continue
        try:
            mem = parse_statm((root / str(pid) / "statm").read_text())
        except OSError:
            mem = None
        if mem is not None:
            u.anon_mb += mem[0]
            u.rss_mb += mem[1]
        try:
            rd, wr = parse_io((root / str(pid) / "io").read_text())
        except OSError:
            continue
        u.rd_bytes += rd
        u.wr_bytes += wr
    return u


def boot_time(root: Path = PROC) -> float:
    """Boot time as epoch seconds (``btime`` in ``/proc/stat``); 0 if unknown."""
    try:
        for line in (root / "stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def environ(pid: int, root: Path = PROC) -> dict[str, str]:
    try:
        raw = (root / str(pid) / "environ").read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        key, sep, val = entry.partition(b"=")
        if sep:
            out[key.decode(errors="replace")] = val.decode(errors="replace")
    return out


def cmdline(pid: int, root: Path = PROC) -> str:
    try:
        raw = (root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(a.decode(errors="replace") for a in raw.split(b"\0") if a)


def comm(pid: int, root: Path = PROC) -> str:
    try:
        return (root / str(pid) / "comm").read_text().strip()
    except OSError:
        return ""


def cwd(pid: int, root: Path = PROC) -> str:
    try:
        return os.readlink(root / str(pid) / "cwd")
    except OSError:
        return ""


#: The environment every swarm session carries (see :mod:`swarm_orchestrator.procs`).
SESSION_ENV = "SWARM_SESSION_ID"
STATE_ENV = "SWARM_STATE_DIR"
#: The label of a process of this run that belongs to no session (the
#: supervisor, the dashboard, the board): the swarm's own overhead.
INFRA = "swarm"


class Attributor:
    """Which swarm session each process belongs to, remembered per process.

    A process is labelled once, when first seen, from its environment: this
    run's ``SWARM_STATE_DIR`` plus a ``SWARM_SESSION_ID`` (``worker:<phase>``,
    ``operator:<job>``…) names the session; the state dir alone is the swarm's
    own overhead (:data:`INFRA`). A process whose environment cannot be read
    (or was cleared) inherits its parent's label. A pid is remembered with its
    start time, so a reused pid is labelled afresh.
    """

    def __init__(self, state_dir: Path | str, root: Path = PROC) -> None:
        self.state_dir = str(state_dir)
        self.root = root
        self._seen: dict[int, tuple[int, str | None]] = {}

    def label(self, table: dict[int, Proc]) -> dict[int, str]:
        """``{pid: label}`` for the processes of this run; others are left out."""
        out: dict[int, str] = {}
        seen: dict[int, tuple[int, str | None]] = {}
        for pid in sorted(table, key=lambda p: table[p].start):  # parents first
            proc = table[pid]
            cached = self._seen.get(pid)
            if cached is not None and cached[0] == proc.start:
                lab = cached[1]
            else:
                lab = self._from_env(pid)
                if lab is None:
                    parent = out.get(proc.ppid)
                    lab = parent if parent not in (None, INFRA) else None
            seen[pid] = (proc.start, lab)
            if lab is not None:
                out[pid] = lab
        self._seen = seen
        return out

    def _from_env(self, pid: int) -> str | None:
        env = environ(pid, self.root)
        if env.get(STATE_ENV) != self.state_dir:
            return None
        if comm(pid, self.root).startswith("tmux"):
            return None  # a tmux server started from a swarm shell hosts other sessions
        return env.get(SESSION_ENV) or INFRA
