"""A project's own command, run by the swarm in a checkout it names.

Not everything a repo's gates read is in git: the dependencies installed beside
a checkout are not. A worker keeps its own mirror's in step with what it
commits; nobody does that for a checkout the swarm itself works in. So a project
names a command per repo, and the swarm runs it where its table says:

- ``[lanes].check``: in a phase's worktree, before the phase lands beside a
  sibling (:mod:`landing`);
- ``[lanes].prepare``: in that worktree, before the check
  (:func:`landing._make_ready`);
- ``[git].post_merge``: in a repo's main checkout, after a merge landed there
  and before the push (:func:`gitq._post_merge`).

Every table is keyed by the repo's name, ``.`` for the project's own and
``"*"`` for every repo without an entry (:func:`command`), and :func:`run` is
the one way such a command runs: in the checkout it is given, through the build
gate like any build, with a timeout, its output in the caller's log file. It
says how it ended (:class:`Outcome`); what a failure means is the caller's.

The last two put right what git does not hold beside a checkout, so they share
two rules. Most runs would find nothing to do, so each has a quick test beside
it (``prepare_if``, ``post_merge_if``) that is asked first, at once and in no
queue (:func:`wanted`). And the command works beside the tree, not in it: one
that leaves a tracked file changed has failed (:func:`run_clean`).

**A light command takes no build slot.** The command is classified as ``swarm
build`` classifies one (:mod:`buildclass`, with ``[build].heavy`` and
``light``): heavy unless it is known to be light or the project declares it so.
A light one runs at once, beside whatever build holds the slots; every other
one queues.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import buildclass
from . import buildlog
from . import buildsem
from .config import Config

#: The tail of a run's output worth showing the owner (:attr:`Outcome.reason`).
TAIL_LINES = 3
TAIL_CHARS = 300
_TAIL_BYTES = 8192
_KILL_GRACE_S = 10.0
#: A quick test (:func:`wanted`): asked before every run of its command.
IF_TIMEOUT_S = 30.0
_GIT_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Outcome:
    """How one :func:`run` ended. ``reason`` is set on a failure only: the last
    lines of the run's output, which are the command's own, or the runner's
    when the command never started or was stopped."""

    ok: bool
    reason: str = ""
    wait_s: float = 0.0  # queued for a build slot
    run_s: float = 0.0


def command(cfg: Config, table: dict[str, str], repo: Path) -> str:
    """``table[R]`` by the repo's name (``.`` for the project's own), else
    ``table["*"]``, else ``""`` (nothing to run)."""
    table = table or {}
    name = "." if repo.resolve() == cfg.project_dir.resolve() else repo.name
    return table.get(name) or table.get("*") or ""


def wanted(test: str, cwd: Path) -> bool:
    """Whether the quick test ``test`` says its command has work to do in the
    checkout ``cwd``: it exits 0. It is asked at once, outside the build gate.
    No test is a yes. One that cannot be asked raises ``OSError`` or
    ``subprocess.TimeoutExpired``."""
    if not test:
        return True
    asked = subprocess.run(test, shell=True, cwd=cwd, capture_output=True, text=True,
                           timeout=IF_TIMEOUT_S)
    return asked.returncode == 0


def _light(cfg: Config, cmd: str, cwd: Path) -> str | None:
    """Why ``cmd`` needs no build slot, by the rules ``swarm build`` goes by, or
    None: it queues."""
    try:
        verdict = buildclass.classify(
            ["sh", "-c", cmd], str(cwd), cfg.build_heavy, cfg.build_light)
    except Exception:  # noqa: BLE001 -- a classifier bug must not stop a command
        return None
    return verdict.why if verdict.cls == buildclass.LIGHT else None


def _run_light(cfg: Config, cmd: str, cwd: Path, phase: str | None, fh,
               timeout: float) -> bool:
    """Run a light command now, beside any build, on the build log as a bypass."""
    start = time.time()
    said = {"id": uuid.uuid4().hex[:12], "phase": phase, "pid": os.getpid(), "slot": None,
            "cls": buildclass.LIGHT, "argv": cmd, "cwd": str(cwd)}
    buildlog.event(cfg, "bypass", **said, wait_s=0.0, ts=start)
    ok = _spawn(cmd, cwd, fh, timeout)
    now = time.time()
    buildlog.event(cfg, "end", **said, run_s=now - start, exit=0 if ok else 1, ts=now)
    return ok


def _spawn(cmd: str, cwd: Path, fh, timeout: float) -> bool:
    if not cmd:
        return True
    from . import launch  # lazy: launch builds on gitq, which builds on this module

    try:
        # The workers' cargo settings: the command builds into the same shared
        # target, and any other profile would write a second copy of every unit.
        env = {**os.environ, **launch.cargo_env()}
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, env=env,
                                stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:  # the checkout went: nothing ran
        fh.write(f"\n# could not start: {exc}\n")
        return False
    try:
        return proc.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
                proc.wait(timeout=_KILL_GRACE_S)
                break
            except (ProcessLookupError, subprocess.TimeoutExpired):
                continue
        fh.write(f"\n# timed out after {timeout}s\n")
        return False


def _mark(fh) -> int:
    """Where the next byte written to ``fh`` goes."""
    fh.flush()
    return os.lseek(fh.fileno(), 0, os.SEEK_CUR)


def _tail(fh, mark: int) -> str:
    """The last few lines written to ``fh`` since ``mark``, capped for a phone."""
    fh.flush()
    try:
        with open(fh.name, "rb") as raw:
            raw.seek(max(mark, os.fstat(raw.fileno()).st_size - _TAIL_BYTES))
            text = raw.read().decode("utf-8", "replace")
    except OSError:
        text = ""
    lines = [ln.strip().lstrip("# ") for ln in text.splitlines() if ln.strip()]
    reason = " ".join(lines[-TAIL_LINES:])
    if len(reason) > TAIL_CHARS:
        reason = reason[: TAIL_CHARS - 3].rstrip() + "..."
    return reason or "failed with no output"


def run(cfg: Config, cmd: str, cwd: Path, fh, *, timeout_s: float,
        phase: str | None = None, wait_s: float | None = None) -> Outcome:
    """Run ``cmd`` in the checkout ``cwd``, its output appended to the open log
    ``fh``. It goes through the build semaphore unless the command is light
    (:func:`_light`), and is stopped ``timeout_s`` after it started.

    ``wait_s`` is the longest it may queue for a build slot, for a caller that
    can do nothing else meanwhile: past it nothing ran, and that is the failure.
    None waits for as long as the queue takes."""
    mark = _mark(fh)
    queued = start = time.time()
    light = _light(cfg, cmd, cwd)
    if light is not None:
        fh.write(f"# light command ({light}): not queued for a build slot\n")
        mark = _mark(fh)
        ok = _run_light(cfg, cmd, cwd, phase, fh, timeout_s)
    else:
        try:
            with buildsem.slot(cfg, cmd, cwd, phase, wait_s=wait_s) as held:
                start = time.time()
                ok = _spawn(cmd, cwd, fh, timeout_s)
                held.exit = 0 if ok else 1
        except buildsem.Busy as exc:
            start = time.time()
            fh.write(f"\n# {exc}: nothing ran\n")
            ok = False
    now = time.time()
    return Outcome(ok, "" if ok else _tail(fh, mark), start - queued, now - start)


def _changed(cwd: Path) -> list[str]:
    """The tracked files of the checkout ``cwd`` with uncommitted changes."""
    try:
        out = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=cwd,
                             capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return out.stdout.splitlines()


def run_clean(cfg: Config, cmd: str, cwd: Path, fh, *, timeout_s: float,
              phase: str | None = None, wait_s: float | None = None) -> Outcome:
    """:func:`run`, for a command that must leave the checkout's tracked files
    as git has them. One that leaves any changed has failed, whatever it exited
    with."""
    ran = run(cfg, cmd, cwd, fh, timeout_s=timeout_s, phase=phase, wait_s=wait_s)
    left = _changed(cwd) if ran.ok else []
    if not left:
        return ran
    why = f"the command left uncommitted changes: {' '.join(left[:5])}"
    return Outcome(False, why, ran.wait_s, ran.run_s)
