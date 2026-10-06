"""A machine service: one detached process for every swarm on the machine.

Some helpers belong to no swarm: the web board shows all of them on one
address. Such a process is started by whichever swarm comes up first, left
alone by every ``swarm up`` after it, and stopped when the last running swarm
goes down. This module is that lifecycle and nothing else, so each such helper
is a :class:`Service` declaration and three calls:

* :func:`start` at ``swarm up`` (and by hand): start it if it is absent.
* :func:`stop_with_last` at ``swarm down``: stop it unless another swarm runs.
* :func:`stop` by hand, and :func:`running` for whoever asks.

Its pid file and its log are in the machine directory
(:func:`machine.directory`), under the service's name. The one who starts it
writes the pid file, under a lock that :func:`stop_with_last` takes too, so two
``swarm up`` at the same moment start one process, and a ``down`` racing an
``up`` never takes the helper away from the swarm that has just come up.

The process is started with no swarm's environment: every ``SWARM_*`` variable
is left behind, because one typed for a single project (``SWARM_STATE_DIR``,
``SWARM_SLUG``) would otherwise decide what the helper does for all of them.
What it needs to know goes on its command line.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from . import freezer, machine, procs

#: Our package on a command line: what makes a pid one of ours.
_PACKAGE = b"swarm_orchestrator"


@dataclass(frozen=True)
class Service:
    """One machine service.

    ``name`` names its pid file, log and lock in the machine directory.
    ``args`` is what follows ``python -m swarm_orchestrator`` on its command
    line, and ``mark`` the leading words of it that say which helper a process
    is (``("web", "serve")``): a pid whose command line does not carry them is
    not this service, whatever the pid file says. ``answers``, when given, says
    whether one is already serving that the pid file does not know of (started
    by hand, in the foreground).
    """

    name: str
    args: tuple[str, ...]
    mark: tuple[str, ...]
    answers: Callable[[], bool] | None = None

    def argv(self) -> list[str]:
        """The command line. This interpreter, not whatever ``swarm`` is first
        on PATH: the helper must be the same install as the command starting it."""
        return [sys.executable, "-m", "swarm_orchestrator", *self.args]


def pidfile(svc: Service, mdir: Path) -> Path:
    return Path(mdir) / f"{svc.name}.pid"


def logfile(svc: Service, mdir: Path) -> Path:
    return Path(mdir) / f"{svc.name}.log"


def is_process(svc: Service, pid: int) -> bool:
    """Is ``pid`` a live process of this service, and not a pid that has since
    been given to something else?"""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    mark = [word.encode() for word in svc.mark]
    return any(_PACKAGE in word for word in cmd) and any(
        cmd[i:i + len(mark)] == mark for i in range(len(cmd)))


def running(svc: Service, mdir: Path) -> int | None:
    """The pid of the service :func:`start` started, if it is still alive."""
    try:
        pid = int(pidfile(svc, mdir).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 and is_process(svc, pid) else None


@contextmanager
def _turn(svc: Service, mdir: Path) -> Iterator[None]:
    """One starter or stopper of this service at a time, across every swarm."""
    mdir = Path(mdir)
    mdir.mkdir(parents=True, exist_ok=True)
    with (mdir / f"{svc.name}.lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def clean_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """``env`` without any swarm's own settings: what a machine service runs in."""
    env = dict(os.environ if env is None else env)
    return {k: v for k, v in env.items() if not k.startswith("SWARM_")}


def start(svc: Service, mdir: Path) -> tuple[int | None, bool]:
    """Start the service unless one is there. Returns ``(pid, started)``:
    the pid when this module knows it, and whether this call started it.

    Detached, in a session of its own, and in a systemd scope of its own where
    one can be made (:func:`freezer.scoped`), so it goes on answering while a
    swarm's sessions are frozen. Raises ``OSError`` when it cannot be started.
    """
    with _turn(svc, mdir):
        pid = running(svc, mdir)
        if pid is not None:
            return pid, False
        if svc.answers is not None and svc.answers():
            return None, False
        with logfile(svc, mdir).open("ab") as log:
            proc = subprocess.Popen(
                freezer.scoped(svc.argv()), cwd=str(mdir), env=clean_env(),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            )
        pidfile(svc, mdir).write_text(f"{proc.pid}\n", encoding="utf-8")
        return proc.pid, True


def _end(svc: Service, pid: int, timeout: float) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and is_process(svc, pid):
        time.sleep(0.05)
    if is_process(svc, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _stop(svc: Service, mdir: Path, also: int | None, timeout: float) -> bool:
    pids = {pid for pid in (running(svc, mdir), also)
            if pid is not None and is_process(svc, pid)}
    for pid in pids:
        _end(svc, pid, timeout)
    try:
        pidfile(svc, mdir).unlink()
    except OSError:
        pass
    return bool(pids)


def stop(svc: Service, mdir: Path, also: int | None = None, timeout: float = 5.0) -> bool:
    """Stop the service. Returns whether one was running.

    ``also`` is a pid the caller found serving without a pid file (one started
    by hand); it is stopped too, if it is a process of this service."""
    with _turn(svc, mdir):
        return _stop(svc, mdir, also, timeout)


def others_running(mdir: Path, leaving: Path | None = None) -> list[Path]:
    """The state dirs of the swarms that are up on this machine, ``leaving``
    aside: a supervisor holds its control FIFO open for as long as it lives."""
    gone = Path(leaving).resolve() if leaving is not None else None
    return [d for d in machine.state_dirs(Path(mdir).parent)
            if d.resolve() != gone and procs.fifo_has_reader(d / "control.fifo")]


def stop_with_last(svc: Service, mdir: Path, leaving: Path | None = None,
                   also: Callable[[], int | None] | None = None,
                   timeout: float = 5.0) -> bool:
    """``swarm down``'s half: stop the service unless a swarm other than the one
    in ``leaving`` is still up. Returns whether it was stopped.

    ``also`` is asked, only when the service is to stop, for the pid of one
    serving without a pid file."""
    with _turn(svc, mdir):
        if others_running(mdir, leaving):
            return False
        return _stop(svc, mdir, also() if also is not None else None, timeout)
