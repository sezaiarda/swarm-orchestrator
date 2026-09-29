"""``swarm build`` — a cross-worker gate for heavy build commands.

Every worker builds in its own isolated worktree, so N workers compiling in
parallel means N independent full builds, each fanning ``cargo`` out across every
core. On a memory-capped host (e.g. WSL) that is exactly how the box OOM-thrashes.

``swarm build <cmd...>`` fixes it by making the build pass through a **counting
semaphore** shared by the whole swarm: at most ``[build].max_concurrent`` heavy
builds run at once; the rest wait. It is implemented with one ``flock`` per slot
and then *execs* the command, so **the build process itself holds the lock** —
if the worker's bash-tool timeout SIGKILLs the build, the slot is released
automatically. No daemon, no counter file to leak, no manual cleanup.

It also caps per-build parallelism (``CARGO_BUILD_JOBS``) so a single ``cargo``
can't saturate every core, compounding the memory spike.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Config

_POLL_S = 0.5


def _try_once(cfg: Config) -> int | None:
    """One non-blocking sweep for a free slot. Returns a held (locked, inheritable)
    fd on success, else ``None``. The fd is kept OPEN on purpose: the flock lives
    on the open file description and is released only when this fd closes — i.e.
    when the (exec'd) build process dies, however it dies."""
    cfg.buildsem_dir.mkdir(parents=True, exist_ok=True)
    for i in range(cfg.build_max_concurrent):
        fd = os.open(cfg.buildsem_dir / f"slot{i}", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        os.set_inheritable(fd, True)  # survive the exec so the build holds the lock
        return fd
    return None


def _acquire(cfg: Config) -> int:
    """Block (polling) until a slot is free; return its held fd."""
    announced = False
    while True:
        fd = _try_once(cfg)
        if fd is not None:
            return fd
        if not announced:
            print(
                f"swarm build: all {cfg.build_max_concurrent} build slots busy —"
                " waiting for one to free…",
                file=sys.stderr,
                flush=True,
            )
            announced = True
        time.sleep(_POLL_S)


def _build_env(cfg: Config, argv: list[str]) -> dict[str, str]:
    env = dict(os.environ)
    if cfg.build_jobs and argv and Path(argv[0]).name == "cargo":
        # Cap codegen fan-out so one build can't grab every core (memory spike);
        # honour an explicit override the caller already set.
        env.setdefault("CARGO_BUILD_JOBS", str(cfg.build_jobs))
    return env


def run(cfg: Config, argv: list[str]) -> int:
    """Acquire a build slot, then exec ``argv`` (the build) in this process.

    Returns an exit code only on the error paths (nothing to run / cannot exec);
    on success it never returns — ``execvpe`` replaces the process image, and the
    slot's flock rides along on the inherited fd until the build exits or is
    killed.
    """
    if not argv:
        print("swarm build: no command given", file=sys.stderr)
        return 2
    if cfg.build_max_concurrent < 1:
        _exec(cfg, argv)  # gate disabled -> run straight through, still job-capped
        return 127
    _acquire(cfg)  # held via the inherited fd; released when the exec'd build dies
    _exec(cfg, argv)
    return 127


@contextmanager
def slot(cfg: Config) -> Iterator[None]:
    """Hold one build slot for the body, in this process: for a build the swarm
    runs itself and waits on (a landing's lane check), not one it execs. The fd
    is not inherited, so a daemon the build leaves behind cannot keep the slot."""
    if cfg.build_max_concurrent < 1:
        yield
        return
    fd = _acquire(cfg)
    os.set_inheritable(fd, False)
    try:
        yield
    finally:
        os.close(fd)


def _exec(cfg: Config, argv: list[str]) -> None:
    try:
        os.execvpe(argv[0], argv, _build_env(cfg, argv))
    except OSError as exc:
        print(f"swarm build: cannot run {argv[0]!r}: {exc}", file=sys.stderr)
        raise SystemExit(127) from exc
