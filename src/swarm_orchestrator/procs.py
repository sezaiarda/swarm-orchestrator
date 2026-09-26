"""Reading ``/proc``: the process table, liveness and a process's identity.

Shared by the reaper (:mod:`session`) and ``swarm keep`` (:mod:`keep`). A pid is
reused by the kernel, so "the same process" means the same pid AND the same start
time (``/proc/<pid>/stat`` field 22, in clock ticks since boot).
"""

from __future__ import annotations

from pathlib import Path

#: Every session the swarm spawns carries ``SWARM_SESSION_ID=<kind>:<id>`` (a
#: worker ``worker:<phase>``, the operator ``operator:<job>``, an Overseer pass
#: ``overseer:<pass>``, and a resolver ``resolver:<phase>``), and
#: hands it to everything it starts. When the session ends, whatever still carries it is ended too.
#: (Not ``SWARM_SESSION``: that one names the tmux session.)
SESSION_ENV = "SWARM_SESSION_ID"


def _stat_fields(pid: int) -> list[str] | None:
    """``/proc/<pid>/stat`` from field 3 (the state) on; the name may hold spaces."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat[stat.rfind(")") + 2 :].split()


def table() -> dict[int, int]:
    """``{pid: ppid}`` for every live process (zombies are already gone)."""
    out: dict[int, int] = {}
    try:
        entries = [e for e in Path("/proc").iterdir() if e.name.isdigit()]
    except OSError:
        return out
    for entry in entries:
        rest = _stat_fields(int(entry.name))
        if rest and len(rest) > 1 and rest[0] not in ("Z", "X"):
            out[int(entry.name)] = int(rest[1])
    return out


def alive(pid: int) -> bool:
    rest = _stat_fields(pid)
    return bool(rest) and rest[0] not in ("Z", "X")


def start_ticks(pid: int) -> int | None:
    """When ``pid`` started, in clock ticks since boot; ``None`` if it is gone."""
    rest = _stat_fields(pid)
    if not rest or len(rest) < 20 or rest[0] in ("Z", "X"):
        return None
    return int(rest[19])


def same(pid: int, ticks: int | None) -> bool:
    """Is ``pid`` still the process that started at ``ticks``?"""
    return ticks is not None and pid > 0 and start_ticks(pid) == ticks


def environ(pid: int) -> list[bytes]:
    """``pid``'s environment as ``KEY=VALUE`` entries (empty if unreadable)."""
    try:
        return Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return []


def cmdline(pid: int) -> list[str]:
    """``pid``'s argv (empty if unreadable)."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def cwd(pid: int) -> Path | None:
    try:
        return Path(f"/proc/{pid}/cwd").resolve(strict=True)
    except OSError:
        return None


def comm(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""
