"""``swarm keep``: the one sanctioned way to leave a process running.

Everything a session starts dies when the session ends (:func:`session.reap_session`),
and ``swarm down`` ends everything the run started. No shell is left lying around:
when a worker is done, all of its shells are gone too.
The exception is something the owner needs after the session is gone, such as a
page to open: ``swarm keep --name N --why "<one line>" -- <command…>`` starts it
fully detached, with the session's markers stripped from its environment, and
records it under ``<state>/keep/<name>.json`` so it is listed (``keep --list``,
``status``, ``doctor``, the dashboard) and can be stopped (``keep --stop N``).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import procs
from .config import Config

#: ``doctor`` warns about a kept process alive longer than this.
STALE_S = 7 * 86400
#: The one-line ``--why`` cap: it has to fit a table row and a phone screen.
WHY_MAX = 120
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: How long ``start`` watches the new process for an immediate exit.
START_GRACE_S = 0.5


class KeepError(Exception):
    """A keep request refused: a bad name or why, a live name, a command that won't start."""


@dataclass
class Kept:
    name: str
    pid: int
    start_ticks: int | None
    started_at: float
    argv: list[str]
    cwd: str
    by: str
    why: str
    log: str
    alive: bool = field(default=False, compare=False)

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.started_at)

    @property
    def stop_cmd(self) -> str:
        return stop_command(self.name)

    def to_json(self) -> dict:
        return asdict(self)


def stop_command(name: str) -> str:
    return f"swarm keep --stop {name}"


def keep_dir(cfg: Config) -> Path:
    return Path(cfg.state_dir) / "keep"


def _path(cfg: Config, name: str) -> Path:
    return keep_dir(cfg) / f"{name}.json"


def _read(path: Path) -> Kept | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rec = Kept(**{k: v for k, v in data.items() if k != "alive"})
    except (OSError, ValueError, TypeError):
        return None
    rec.alive = procs.same(rec.pid, rec.start_ticks)
    return rec


def get(cfg: Config, name: str) -> Kept | None:
    return _read(_path(cfg, name))


def load_all(cfg: Config) -> list[Kept]:
    """Every recorded kept process, alive or dead, by name."""
    try:
        paths = sorted(keep_dir(cfg).glob("*.json"))
    except OSError:
        return []
    return [rec for rec in (_read(p) for p in paths) if rec is not None]


def live_pids(cfg: Config) -> set[int]:
    """Pids of kept processes that are still the process that was kept."""
    return {rec.pid for rec in load_all(cfg) if rec.alive}


def who() -> str:
    """Who is asking: the session's own marker, else the phase, else the owner."""
    return (os.environ.get(procs.SESSION_ENV) or
            (f"worker:{os.environ['SWARM_PHASE']}" if os.environ.get("SWARM_PHASE") else "owner"))


def clean_env(cfg: Config) -> dict[str, str]:
    """This environment minus everything that ties a process to a session or the run.

    Without the markers no reaper matches it, and a ``TMPDIR`` under the state dir
    is the session's own temp dir, deleted when the session's work lands."""
    drop = {procs.SESSION_ENV, "SWARM_SESSION", "SWARM_STATE_DIR", "SWARM_PHASE", cfg.env_marker,
            "SWARM_OPERATOR_JOB", "SWARM_OVERSEER_PASS"}
    state = str(Path(cfg.state_dir).resolve())
    env = {k: v for k, v in os.environ.items() if k not in drop}
    for key in ("TMPDIR", "TMP", "TEMP"):
        val = env.get(key)
        if val and str(Path(val).resolve()).startswith(state):
            env.pop(key)
    return env


def check_why(why: str) -> str:
    why = " ".join((why or "").split())
    if not why:
        raise KeepError("--why is required: one plain line saying what it is for")
    if len(why) > WHY_MAX:
        raise KeepError(f"--why is {len(why)} characters; keep it to one line of {WHY_MAX}")
    return why


def start(cfg: Config, name: str, argv: list[str], why: str, cwd: str | None = None) -> Kept:
    """Start ``argv`` detached and record it. Raises :class:`KeepError`."""
    if not _NAME.match(name or ""):
        raise KeepError(f"bad name {name!r}: letters, digits, '.', '_' or '-' (64 at most)")
    why = check_why(why)
    if not argv:
        raise KeepError("no command: swarm keep --name N --why '...' -- <command...>")
    old = get(cfg, name)
    if old is not None and old.alive:
        raise KeepError(f"{name} is already running (pid {old.pid}); "
                        f"`{stop_command(name)}` first, or pick another name")
    where = str(Path(cwd or os.getcwd()).resolve())
    keep_dir(cfg).mkdir(parents=True, exist_ok=True)
    log = keep_dir(cfg) / f"{name}.log"
    try:
        with log.open("ab") as out:
            proc = subprocess.Popen(
                argv, cwd=where, env=clean_env(cfg), stdin=subprocess.DEVNULL,
                stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            )
    except OSError as exc:
        raise KeepError(f"could not start {argv[0]}: {exc}") from exc
    rec = Kept(name=name, pid=proc.pid, start_ticks=procs.start_ticks(proc.pid),
               started_at=time.time(), argv=list(argv), cwd=where, by=who(), why=why,
               log=str(log))
    tmp = _path(cfg, name).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec.to_json(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(_path(cfg, name))
    deadline = time.monotonic() + START_GRACE_S
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    rec.alive = proc.poll() is None
    return rec


def stop(cfg: Config, name: str, wait: float = 5.0) -> Kept | None:
    """TERM the kept process (its whole process group), then KILL; drop its record.

    Returns the record, or ``None`` when no process is kept under ``name``."""
    rec = get(cfg, name)
    if rec is None:
        return None
    if rec.alive:
        for sig, grace in ((signal.SIGTERM, wait), (signal.SIGKILL, 2.0)):
            try:
                os.killpg(rec.pid, sig)  # it leads its own session and group
            except OSError:
                try:
                    os.kill(rec.pid, sig)
                except OSError:
                    break
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and procs.same(rec.pid, rec.start_ticks):
                time.sleep(0.05)
            if not procs.same(rec.pid, rec.start_ticks):
                break
    rec.alive = procs.same(rec.pid, rec.start_ticks)
    if not rec.alive:
        _path(cfg, name).unlink(missing_ok=True)
    return rec


def age_text(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def line(rec: Kept, now: float | None = None) -> str:
    """One human line per kept process, shared by ``keep --list`` and ``status``."""
    state = f"alive pid {rec.pid}" if rec.alive else "dead"
    return (f"{rec.name}: {state}, {age_text(rec.age_s(now))} old, by {rec.by} — {rec.why}"
            + (f"  (stop: {rec.stop_cmd})" if rec.alive else ""))
