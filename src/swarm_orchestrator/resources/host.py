"""The host, read from ``/proc``: CPU, load, pressure, memory, swap, disk IO.

Every reader takes the ``/proc`` root as a parameter so the tests can point it at
small fixture files. The parsers are pure functions of the file's text.

Two measurement choices matter more than they look:

- **Memory is split into anon and page cache.** ``memory.peak`` and ``docker
  stats`` count page cache, which the kernel hands back the moment anything
  needs it; sizing a gate on them overstates the need several times over. What
  a build *needs* is anonymous memory; what the host has *left* is
  ``MemAvailable``. Page cache is reported, separately, as what it is.
- **Pressure comes from the counters, not the averages.** ``/proc/pressure/*``
  carries ``avg10`` (a 10-second decaying average, which smooths away exactly
  the burst a build is) and ``total`` (microseconds stalled, monotonic). The
  sampler differences ``total`` over its own interval, so a one-second sample
  says what that second was.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PROC = Path("/proc")
_MB = 1024 * 1024

#: Whole disks in ``/proc/diskstats``; partitions and ``dm-*`` would count the
#: same bytes twice, ``loop``/``ram``/``zram`` are not storage.
_WHOLE_DISK = re.compile(r"^(sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")
_SECTOR = 512


def _read(root: Path, name: str) -> str:
    try:
        return (root / name).read_text()
    except OSError:
        return ""


def parse_cpu(text: str) -> tuple[int, int] | None:
    """``(busy, total)`` jiffies across all CPUs from ``/proc/stat``.

    Busy is everything but ``idle`` and ``iowait``; ``guest`` time is already
    inside ``user`` and is not added twice."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            vals = [int(v) for v in line.split()[1:]]
            total = sum(vals[:8])  # user nice system idle iowait irq softirq steal
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return total - idle, total
    return None


def parse_loadavg(text: str) -> float | None:
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


def parse_pressure(text: str) -> dict[str, int]:
    """``{"some": us, "full": us}``: the monotonic stall totals of one resource."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] not in ("some", "full"):
            continue
        for item in parts[1:]:
            if item.startswith("total="):
                try:
                    out[parts[0]] = int(item[6:])
                except ValueError:
                    pass
    return out


def parse_meminfo(text: str) -> dict[str, int]:
    """``/proc/meminfo`` in bytes, by field name."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[name.strip()] = int(parts[0]) * (1024 if len(parts) > 1 else 1)
    return out


def memory(info: dict[str, int]) -> dict[str, float]:
    """The memory split a capacity decision needs, in MiB.

    ``cache`` is file-backed page cache (``Buffers + Cached - Shmem``): the
    kernel reclaims it on demand, so it is not memory anything needs."""
    def mb(v: int) -> float:
        return round(v / _MB, 1)

    cache = info.get("Buffers", 0) + info.get("Cached", 0) - info.get("Shmem", 0)
    return {
        "avail_mb": mb(info.get("MemAvailable", 0)),
        "anon_mb": mb(info.get("AnonPages", 0)),
        "cache_mb": mb(max(0, cache)),
        "shmem_mb": mb(info.get("Shmem", 0)),
        "swap_mb": mb(max(0, info.get("SwapTotal", 0) - info.get("SwapFree", 0))),
    }


def parse_diskstats(text: str) -> tuple[int, int]:
    """``(read_bytes, written_bytes)`` summed over whole disks."""
    rd = wr = 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 10 or not _WHOLE_DISK.match(parts[2]):
            continue
        try:
            rd += int(parts[5]) * _SECTOR
            wr += int(parts[9]) * _SECTOR
        except ValueError:
            continue
    return rd, wr


def static(root: Path = PROC) -> dict:
    """What does not change while the host runs: cores, RAM, swap size."""
    info = parse_meminfo(_read(root, "meminfo"))
    return {
        "ncpu": os.cpu_count() or 1,
        "mem_total_mb": round(info.get("MemTotal", 0) / _MB, 1),
        "swap_total_mb": round(info.get("SwapTotal", 0) / _MB, 1),
    }


class HostReader:
    """Reads the host each :meth:`sample` and turns counters into rates.

    The first sample of a reader has no previous counters, so its rates are
    ``None`` (never a made-up zero)."""

    def __init__(self, root: Path = PROC) -> None:
        self.root = root
        self._prev: dict | None = None

    def _raw(self, now: float) -> dict:
        pressure = {
            res: parse_pressure(_read(self.root, f"pressure/{res}"))
            for res in ("cpu", "memory", "io")
        }
        return {
            "ts": now,
            "cpu": parse_cpu(_read(self.root, "stat")),
            "psi": pressure,
            "disk": parse_diskstats(_read(self.root, "diskstats")),
        }

    def sample(self, now: float) -> dict:
        raw = self._raw(now)
        row: dict = {"load": parse_loadavg(_read(self.root, "loadavg"))}
        row.update(memory(parse_meminfo(_read(self.root, "meminfo"))))
        prev, self._prev = self._prev, raw
        dt = now - prev["ts"] if prev else 0.0
        if prev is None or dt <= 0:
            row.update(cpu=None, psi=None, rd_mbs=None, wr_mbs=None)
            return row
        row["cpu"] = _cpu_pct(prev["cpu"], raw["cpu"])
        row["psi"] = _psi_pct(prev["psi"], raw["psi"], dt)
        row["rd_mbs"] = round(max(0, raw["disk"][0] - prev["disk"][0]) / _MB / dt, 2)
        row["wr_mbs"] = round(max(0, raw["disk"][1] - prev["disk"][1]) / _MB / dt, 2)
        return row


def _cpu_pct(a: tuple[int, int] | None, b: tuple[int, int] | None) -> float | None:
    if not a or not b or b[1] <= a[1]:
        return None
    return round(100.0 * max(0, b[0] - a[0]) / (b[1] - a[1]), 1)


#: Row keys for the stall shares: ``some`` = at least one task stalled,
#: ``full`` = every non-idle task stalled. CPU ``full`` is meaningless system-wide.
_PSI_KEYS = (("cpu", "some", "cpu"), ("memory", "some", "mem"), ("memory", "full", "memf"),
             ("io", "some", "io"), ("io", "full", "iof"))


def _psi_pct(a: dict, b: dict, dt: float) -> dict[str, float] | None:
    out: dict[str, float] = {}
    for res, kind, key in _PSI_KEYS:
        x, y = a.get(res, {}).get(kind), b.get(res, {}).get(kind)
        if x is not None and y is not None:
            out[key] = round(min(100.0, 100.0 * max(0, y - x) / (dt * 1e6)), 2)
    return out or None
