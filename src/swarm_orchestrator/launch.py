"""Worker launch and the ``swarm done`` completion signal.

``launch`` claims a slot check-and-set, then either respawns the slot's tmux
pane (real driver) or spawns a detached process (bare driver used by the
hermetic logic tests). Readiness is *detected* (polling ``capture-pane`` for the
idle marker) rather than slept for. ``done`` is the only thing a worker does to
signal completion: write a durable sentinel, then a best-effort non-blocking
FIFO poke that must never hang the worker if the supervisor is down.
"""

from __future__ import annotations

import errno
import os
import shlex
import subprocess
import time
from pathlib import Path

from . import gitq
from . import state as state_mod
from . import telegram, tmux
from .config import Config, ready_needle
from .logutil import Log

READY_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 0.25
TRUST_PROMPT = "Do you trust the files"


def _worker_shell(cfg: Config, phase: str, cwd: Path) -> str:
    cmd = cfg.worker_cmd.format(phase=phase)
    if cfg.worker_settings:
        # Force in-process teammates: a worker's own subagents then never open
        # extra tmux panes in the workers window. Merges over the user's
        # settings, so bypassPermissions etc. are preserved.
        cmd += f" --settings {shlex.quote(cfg.worker_settings)}"
    return f"cd {shlex.quote(str(cwd))} && exec {cmd}"


def _worker_env(
    cfg: Config, phase: str, worktree: Path | None = None
) -> dict[str, str]:
    """Env vars a worker (and its ``swarm done``) need to find this run.

    Under ``isolation = worktree`` the worker also gets ``SWARM_WORKTREE`` (its
    cwd — a full isolated mirror of the whole workspace on branch
    ``swarm/<phase>``, umbrella + every component repo at its real path),
    ``SWARM_MAIN`` (the integration target branch) and ``SWARM_PROJECT`` (the
    canonical project path). The worker just works inside the mirror as if it
    were the real project; the integrator merges whatever repos it changed.
    """
    env = {cfg.env_marker: phase, "SWARM_STATE_DIR": str(cfg.state_dir)}
    for key in ("SWARM_SLUG", "SWARM_TG_SINK", "SWARM_BIN", "SWARM_DRIVER"):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    if worktree is not None:
        env["SWARM_WORKTREE"] = str(worktree)
        env["SWARM_MAIN"] = cfg.git_main_branch
        env["SWARM_PROJECT"] = str(cfg.project_dir)
    return env


def launch(cfg: Config, phase: str, log: Log) -> bool:
    """Claim a slot and start a worker for ``phase``. Returns success.

    Under ``isolation = worktree`` a full-workspace mirror on ``swarm/<phase>``
    is created before the pane is respawned; the branch and umbrella worktree
    path (both deterministic from the phase) are recorded on the slot *in the
    claiming transaction*, so a crash between claim and worktree creation can
    still be reconciled/freed. Concurrent phases in the same repo are fine —
    each has its own worktree/branch — so there is no per-repo launch gate.
    """
    with state_mod.transaction(cfg) as st:
        if st.paused:
            log.line(f"LAUNCH-DENIED {phase} paused")
            return False
        slot = st.claim_slot(phase)
        if slot is None:
            log.line(f"LAUNCH-DENIED {phase} no-free-slot")
            return False
        sid, pane = slot.id, slot.pane_id
        if cfg.git_isolation == "worktree":
            slot.branch = f"swarm/{phase}"
            slot.worktree = str(cfg.wt_dir / phase)
    log.line(f"CLAIM {phase} slot={sid}")

    worktree: Path | None = None
    if cfg.git_isolation == "worktree":
        try:
            worktree = gitq.worktree_add(cfg, phase, log)
        except gitq.GitError as exc:
            with state_mod.transaction(cfg) as st:
                st.free_slot_for(phase)
            telegram.notify(cfg.telegram_notify, f"swarm: worktree {phase} failed: {exc}")
            log.line(f"WORKTREE-FAIL {phase} {exc}")
            return False

    if cfg.driver == "bare":
        ok = _launch_bare(cfg, phase, worktree, log)
    else:
        ok = _launch_tmux(cfg, phase, pane, worktree, log)

    if not ok:
        # Kill the half-started claude BEFORE dropping its worktree, so we never
        # yank the cwd out from under a live process (which would strand an
        # orphaned claude in a deleted directory).
        if cfg.driver != "bare" and pane is not None:
            tmux.respawn_pane(pane, "exec sleep infinity")
        with state_mod.transaction(cfg) as st:
            st.free_slot_for(phase)
        if worktree is not None:
            gitq.discard(cfg, phase, log)  # don't leak the worktree on start failure
        telegram.notify(cfg.telegram_notify, f"swarm: worker {phase} failed to start")
        log.line(f"LAUNCH-FAIL {phase} slot={sid}")
        return False
    log.line(f"LAUNCH {phase} slot={sid}")
    return True


def _launch_bare(cfg: Config, phase: str, worktree: Path | None, log: Log) -> bool:
    """Spawn a detached worker process (no tmux)."""
    cwd = worktree or cfg.project_dir
    env = {**os.environ, **_worker_env(cfg, phase, worktree)}
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", _worker_shell(cfg, phase, cwd)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError as exc:
        log.line(f"LAUNCH-SPAWN-ERROR {phase} {exc}")
        return False


def _launch_tmux(
    cfg: Config, phase: str, pane: str | None, worktree: Path | None, log: Log
) -> bool:
    """Respawn the slot pane, detect readiness, drive the worker command."""
    if pane is None:
        log.line(f"LAUNCH-FAIL {phase} no-pane")
        return False
    cwd = worktree or cfg.project_dir
    tmux.respawn_pane(
        pane, _worker_shell(cfg, phase, cwd), env=_worker_env(cfg, phase, worktree)
    )
    if not _await_ready(cfg, pane, log):
        return False
    command = cfg.command_template.format(phase=phase)
    tmux.send_submit(pane, command)
    return True


def await_ready(cfg: Config, pane: str, log: Log) -> bool:
    """Wait until claude has booted (its version banner shows); dismiss a
    first-run folder-trust prompt if one appears. Shared by worker, master, and
    resolver pane launches so all three honour the trust prompt and the timeout.
    Returns False on timeout (the caller treats it as a launch failure)."""
    needle = ready_needle(cfg)
    deadline = time.monotonic() + READY_TIMEOUT_S
    dismissed = False
    while time.monotonic() < deadline:
        text = tmux.capture(pane)
        if TRUST_PROMPT in text and not dismissed:
            tmux.send_enter(pane)
            dismissed = True
            time.sleep(POLL_INTERVAL_S)
            continue
        if needle in text:
            return True
        time.sleep(POLL_INTERVAL_S)
    log.line(f"READY-TIMEOUT pane={pane}")
    return False


# Backwards-compatible internal alias.
_await_ready = await_ready


def _write_sentinel(cfg: Config, phase: str, status: str, note: str) -> None:
    cfg.done_dir.mkdir(parents=True, exist_ok=True)
    dest = cfg.done_dir / f"{phase}.{status}"
    tmp = cfg.done_dir / f".{phase}.{status}.tmp"
    tmp.write_text(f"{phase} {status} {note}\n", encoding="utf-8")
    os.replace(tmp, dest)


def _log_poke_drop(cfg: Config, detail: str) -> None:
    """Record a dropped FIFO poke centrally (best-effort; never raises)."""
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        with cfg.supervisor_log.open("a", encoding="utf-8") as fh:
            fh.write(f"POKE-DROP {detail}\n")
    except OSError:
        pass


def _poke_fifo(cfg: Config, line: str) -> bool:
    """Non-blocking one-line FIFO poke. Never hangs the caller.

    A missing FIFO or ``ENXIO`` (no reader attached) is the expected
    supervisor-down case and is skipped silently. Any *other* write error is
    unexpected and logged to the supervisor log (no retry, no backstop) rather
    than swallowed, so a genuinely dropped event is diagnosable.
    """
    if not cfg.fifo_path.exists():
        return False
    try:
        fd = os.open(cfg.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ENXIO:  # no reader attached
            return False
        _log_poke_drop(cfg, f"open {line.strip()}: {exc}")
        return False
    try:
        os.write(fd, line.encode("utf-8"))
        return True
    except OSError as exc:
        _log_poke_drop(cfg, f"write {line.strip()}: {exc}")
        return False
    finally:
        os.close(fd)


def done(cfg: Config, phase: str, status: str, note: str = "") -> None:
    """Signal phase completion: durable sentinel first, then best-effort poke."""
    _write_sentinel(cfg, phase, status, note)
    _poke_fifo(cfg, f"done {phase} {status}\n")
