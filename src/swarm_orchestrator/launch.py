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

from . import state as state_mod
from . import telegram, tmux
from .config import Config
from .logutil import Log

READY_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 0.25
TRUST_PROMPT = "Do you trust the files"


def _worker_shell(cfg: Config, phase: str) -> str:
    cmd = cfg.worker_cmd.format(phase=phase)
    return f"cd {shlex.quote(str(cfg.project_dir))} && exec {cmd}"


def _worker_env(cfg: Config, phase: str) -> dict[str, str]:
    """Env vars a worker (and its ``swarm done``) need to find this run."""
    env = {cfg.env_marker: phase, "SWARM_STATE_DIR": str(cfg.state_dir)}
    for key in ("SWARM_SLUG", "SWARM_TG_SINK", "SWARM_BIN", "SWARM_DRIVER"):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


def launch(cfg: Config, phase: str, log: Log) -> bool:
    """Claim a slot and start a worker for ``phase``. Returns success."""
    with state_mod.transaction(cfg) as st:
        slot = st.claim_slot(phase)
        if slot is None:
            log.line(f"LAUNCH-DENIED {phase} no-free-slot")
            return False
        sid, pane = slot.id, slot.pane_id
    log.line(f"CLAIM {phase} slot={sid}")

    if cfg.driver == "bare":
        ok = _launch_bare(cfg, phase, log)
    else:
        ok = _launch_tmux(cfg, phase, pane, log)

    if not ok:
        with state_mod.transaction(cfg) as st:
            st.free_slot_for(phase)
        telegram.notify(cfg.telegram_notify, f"swarm: worker {phase} failed to start")
        log.line(f"LAUNCH-FAIL {phase} slot={sid}")
        return False
    log.line(f"LAUNCH {phase} slot={sid}")
    return True


def _launch_bare(cfg: Config, phase: str, log: Log) -> bool:
    """Spawn a detached worker process (no tmux)."""
    env = {**os.environ, **_worker_env(cfg, phase)}
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", _worker_shell(cfg, phase)],
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


def _launch_tmux(cfg: Config, phase: str, pane: str | None, log: Log) -> bool:
    """Respawn the slot pane, detect readiness, drive the worker command."""
    if pane is None:
        log.line(f"LAUNCH-FAIL {phase} no-pane")
        return False
    tmux.respawn_pane(pane, _worker_shell(cfg, phase), env=_worker_env(cfg, phase))
    if not _await_ready(cfg, pane, log):
        return False
    command = cfg.command_template.format(phase=phase)
    if tmux.submit_line(pane, command):
        return True
    log.line(f"SUBMIT-FAIL {phase}")
    return False


def _await_ready(cfg: Config, pane: str, log: Log) -> bool:
    """Poll capture-pane for the readiness marker; dismiss a trust prompt."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    dismissed = False
    while time.monotonic() < deadline:
        text = tmux.capture(pane)
        if TRUST_PROMPT in text and not dismissed:
            tmux.send_enter(pane)
            dismissed = True
            time.sleep(POLL_INTERVAL_S)
            continue
        if cfg.ready_marker in text:
            return True
        time.sleep(POLL_INTERVAL_S)
    log.line(f"READY-TIMEOUT pane={pane}")
    return False


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
