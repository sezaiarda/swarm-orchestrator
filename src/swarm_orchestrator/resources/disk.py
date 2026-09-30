"""Disk: real free space (WSL-aware) and the size of the swarm's big directories.

**Free space under WSL is not what ``df /`` says.** The distro's ext4 volume is a
virtual disk (``ext4.vhdx``) stored on a Windows drive. ``df /`` reports the
virtual disk's own ceiling, which is fiction: the file grows on demand until the
Windows drive is full, and deleting inside the distro never shrinks it — freed
blocks become slack the guest reuses. So the real headroom is::

    headroom = (vhdx file size - bytes used inside the distro)   # reusable slack
             + free space on the Windows drive holding the vhdx   # growth room

The vhdx is found once (the largest ``ext4.vhdx`` under the Windows user
profiles' WSL folders, or ``[resources].vhdx`` when set) and re-found hourly.
Outside WSL the headroom is plain ``statvfs`` free space of the state dir's disk.

**Directory sizes are expensive** — a shared build cache is hundreds of
thousands of files — so they are measured rarely (every
:data:`DIRS_EVERY_S`), by ``du`` at idle CPU and IO priority, each under a time
budget, on a thread of their own. A size that ran out of budget is reported as
unknown, never as a partial figure that looks like a real one.
"""

from __future__ import annotations

import glob
import os
import subprocess
import time
from pathlib import Path

#: How often directory sizes are measured, and how long one ``du`` may take.
DIRS_EVERY_S = 600.0
DU_BUDGET_S = 60.0
#: How often the headroom is re-read, and the vhdx re-found.
HEADROOM_EVERY_S = 60.0
VHDX_REFIND_S = 3600.0
_GB = 1024 ** 3

#: Where WSL keeps a distro's virtual disk on the Windows side.
VHDX_GLOBS = (
    "/mnt/*/Users/*/AppData/Local/wsl/*/ext4.vhdx",
    "/mnt/*/Users/*/AppData/Local/Packages/*/LocalState/ext4.vhdx",
)


def is_wsl(proc_version: str | None = None) -> bool:
    if proc_version is None:
        try:
            proc_version = Path("/proc/version").read_text()
        except OSError:
            return False
    return "microsoft" in proc_version.lower()


def find_vhdx(override: str = "", globs: tuple[str, ...] = VHDX_GLOBS) -> Path | None:
    """The distro's virtual disk: ``override`` if it exists, else the largest match."""
    if override:
        p = Path(override).expanduser()
        return p if p.is_file() else None
    best: tuple[int, str] | None = None
    for pattern in globs:
        for path in glob.glob(pattern):
            try:
                size = os.stat(path).st_size
            except OSError:
                continue
            if best is None or size > best[0]:
                best = (size, path)
    return Path(best[1]) if best else None


def _used(stat: os.statvfs_result) -> int:
    return (stat.f_blocks - stat.f_bfree) * stat.f_frsize


def _free(stat: os.statvfs_result) -> int:
    return stat.f_bavail * stat.f_frsize


def wsl_headroom(vhdx_size: int, used_inside: int, host_free: int) -> dict:
    """The headroom arithmetic, in GiB (see the module docstring)."""
    slack = max(0, vhdx_size - used_inside)
    return {
        "headroom_gb": round((slack + host_free) / _GB, 1),
        "vhdx_gb": round(vhdx_size / _GB, 1),
        "used_gb": round(used_inside / _GB, 1),
        "slack_gb": round(slack / _GB, 1),
        "host_free_gb": round(host_free / _GB, 1),
        "wsl": True,
    }


class Headroom:
    """Reads the headroom; remembers where the vhdx is between reads."""

    def __init__(self, state_dir: Path, vhdx: str = "", wsl: bool | None = None) -> None:
        self.state_dir = state_dir
        self.override = vhdx
        self.wsl = is_wsl() if wsl is None else wsl
        self._vhdx: Path | None = None
        self._found_at = 0.0

    def read(self, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        try:
            if self.wsl:
                if self._vhdx is None or now - self._found_at >= VHDX_REFIND_S:
                    self._vhdx, self._found_at = find_vhdx(self.override), now
                if self._vhdx is not None:
                    size = os.stat(self._vhdx).st_size
                    host = os.statvfs(_mount_of(self._vhdx))
                    return wsl_headroom(size, _used(os.statvfs("/")), _free(host))
            stat = os.statvfs(self.state_dir if self.state_dir.exists() else "/")
        except OSError:
            return None
        return {"headroom_gb": round(_free(stat) / _GB, 1),
                "used_gb": round(_used(stat) / _GB, 1), "wsl": False}


def _mount_of(path: Path) -> Path:
    """The Windows drive mount a vhdx lives on (``/mnt/c``); statvfs of any path
    on it answers the same, so the file's own directory would do too."""
    parts = path.parts
    return Path(*parts[:3]) if len(parts) > 3 and parts[1] == "mnt" else path.parent


def du(path: Path, budget: float = DU_BUDGET_S) -> tuple[int | None, float]:
    """``(bytes on disk, CPU seconds du spent)``; bytes is ``None`` past ``budget``.

    ``du -sxB1`` at nice 19 and idle IO class. The CPU cost comes from ``wait4``'s
    rusage, so the sampler's stated overhead includes it."""
    if not path.exists():
        return 0, 0.0
    cmd = ["du", "-sxB1", str(path)]
    if _which("ionice"):
        cmd = ["ionice", "-c3", *cmd]
    if _which("nice"):
        cmd = ["nice", "-n", "19", *cmd]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL)
    except OSError:
        return None, 0.0
    deadline = time.monotonic() + budget
    killed = False
    while True:
        pid, status, rusage = os.wait4(proc.pid, os.WNOHANG)
        if pid:
            break
        if time.monotonic() >= deadline:
            proc.kill()
            killed = True
            _, status, rusage = os.wait4(proc.pid, 0)
            break
        time.sleep(0.2)
    code = os.waitstatus_to_exitcode(status)
    proc.returncode = code  # reaped here, so Popen must not wait for it again
    cpu = rusage.ru_utime + rusage.ru_stime
    assert proc.stdout is not None
    with proc.stdout:
        out = proc.stdout.read().decode(errors="replace")
    # du exits 1 when a file vanished mid-walk; its total is still a real total.
    if killed or code not in (0, 1):
        return None, cpu
    try:
        return int(out.split()[0]), cpu
    except (IndexError, ValueError):
        return None, cpu


def _which(name: str) -> bool:
    return any(os.access(os.path.join(d, name), os.X_OK)
               for d in os.environ.get("PATH", "").split(os.pathsep) if d)


def growth_gb_per_h(prev: dict | None, cur: dict, key: str) -> float | None:
    """How fast ``key`` (a size in GiB) grew between two dirs rows, in GiB per hour."""
    if not prev or prev.get(key) is None or cur.get(key) is None:
        return None
    dt = cur["ts"] - prev["ts"]
    if dt < 60:
        return None
    return round((cur[key] - prev[key]) / (dt / 3600), 2)
