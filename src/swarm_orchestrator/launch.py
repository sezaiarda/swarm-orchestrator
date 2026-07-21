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
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from . import gitq
from . import ledger as ledger_mod
from . import state as state_mod
from . import telegram, tmux
from .config import Config, ready_needle
from .logutil import Log

READY_TIMEOUT_S = 30.0
POLL_INTERVAL_S = 0.25
# Substrings that identify claude's workspace-trust dialog (matched
# case-insensitively against joined pane text, so wrapping never hides them).
TRUST_MARKERS = ("do you trust", "trust the files", "trust the authors")


def _claude_config_path() -> Path:
    """Where claude stores per-directory trust (`~/.claude.json`).

    Overridable via ``SWARM_CLAUDE_CONFIG`` so tests never touch the real file.
    """
    override = os.environ.get("SWARM_CLAUDE_CONFIG")
    return Path(override).expanduser() if override else Path.home() / ".claude.json"


def pretrust_dir(path: Path, log: Log) -> None:
    """Mark ``path`` trusted in claude's config so no folder-trust dialog appears.

    Worktree mode launches every worker in a brand-new directory claude has never
    seen, which would otherwise pop the "Do you trust the files in this folder?"
    dialog for *every* phase. We pre-seed
    ``projects[<path>].hasTrustDialogAccepted = true`` before the pane starts.
    Best-effort and additive (never drops other projects), written atomically so a
    concurrent claude write can't see a half file. A no-op if already trusted.
    """
    cfg_path = _claude_config_path()
    key = str(path.resolve())
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        projects, data["projects"] = {}, {}
    entry = projects.setdefault(key, {})
    if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
        return  # already trusted -> don't rewrite (avoids racing a live claude)
    if not isinstance(entry, dict):
        entry = projects[key] = {}
    entry["hasTrustDialogAccepted"] = True
    entry.setdefault("hasCompletedProjectOnboarding", True)
    tmp = cfg_path.with_name(cfg_path.name + ".swarm-tmp")
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, cfg_path)
        log.line(f"PRETRUST {key}")
    except OSError as exc:
        log.line(f"PRETRUST-FAIL {key} {exc}")


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
        # Worktree mode only (the parallel-build OOM problem is worktree-specific):
        # carry the build-gate config so `swarm build` honours it from a worktree
        # cwd (no .swarm.toml there), and cap raw `cargo` fan-out as a backstop for
        # any build not routed through `swarm build`. In-place (`isolation="none"`)
        # workers keep their full build parallelism untouched.
        env["SWARM_BUILD_MAX"] = str(cfg.build_max_concurrent)
        env["SWARM_BUILD_JOBS"] = str(cfg.build_jobs)
        if cfg.build_jobs:
            env["CARGO_BUILD_JOBS"] = str(cfg.build_jobs)
    return env


def _unmet_deps(cfg: Config, phase: str, done: dict[str, str]) -> list[str]:
    """Known-phase deps of ``phase`` not yet in ``done`` (launch-time backstop).

    Resolves the same ledger the master reasons over (``cfg.project_dir /
    cfg.ledger``). Best-effort: an empty list — never a block — is returned when
    the ledger can't be loaded or ``phase`` isn't a declared phase, so a project
    with no machine ledger (or a phase the resolver doesn't know) launches exactly
    as before.
    """
    graph = ledger_mod.load(cfg.project_dir / cfg.ledger)
    if phase not in graph:
        return []
    return sorted(d for d in graph[phase] if d not in done)


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
        # Dependency backstop: st.done is written only after a phase's worktree is
        # merged, so gating on it means a phase can never start before every dep it
        # depends on has merged — even if the LLM master mis-reasons over the prose
        # ledger and asks to launch it out of order. Undo the just-claimed slot
        # under the same flock so it isn't stranded.
        missing = _unmet_deps(cfg, phase, st.done)
        if missing:
            st.free_slot_for(phase)
            detail = " ".join(missing)
            log.line(f"LAUNCH-DENIED {phase} unmet-deps [{detail}]")
            print(f"LAUNCH-DENIED {phase}: unmet deps [{detail}]")
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
        # Pre-trust the fresh worktree so claude never pops the folder-trust dialog.
        pretrust_dir(worktree, log)

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
    if not tmux.send_submit(pane, command):
        log.line(f"SUBMIT-LOST {phase} pane={pane}")
        return False
    return True


def _trust_prompt_showing(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in TRUST_MARKERS)


def await_ready(cfg: Config, pane: str, log: Log) -> bool:
    """Wait until claude has booted (its version banner shows), dismissing the
    folder-trust dialog if it appears. Shared by worker, master, and resolver
    pane launches. Returns False on timeout (a launch failure to the caller).

    The trust dialog is checked on **every** poll (not latched) and re-accepted
    with Enter each time it shows, and readiness is never declared while it is up
    — so a dialog that renders late, re-prompts, or wraps can't be mistaken for a
    ready pane. (Belt-and-suspenders: :func:`pretrust_dir` normally stops the
    dialog from ever appearing.)"""
    needle = ready_needle(cfg)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        text = tmux.capture_joined(pane)
        if _trust_prompt_showing(text):
            tmux.send_enter(pane)  # accept (default = trust); re-sent if it lingers
            time.sleep(POLL_INTERVAL_S)
            continue  # never 'ready' while the dialog is up
        if needle in text:
            return True
        time.sleep(POLL_INTERVAL_S)
    log.line(f"READY-TIMEOUT pane={pane}")
    return False


# Backwards-compatible aliases.
_await_ready = await_ready
TRUST_PROMPT = TRUST_MARKERS[1]


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


def _completion_ping(phase: str, status: str, note: str) -> str | None:
    """The owner telegram for a finishing worker, or ``None`` for a clean success.

    The worker self-classifies its outcome (the ``status`` it passes to ``swarm
    done``): ``ok`` is a silent success (return ``None`` — no ping); ``needs-owner``
    integrates like ``ok`` but the owner should look at something; ``fail`` rolled
    back. The last two ping with the worker's one-line ``note`` recap so the owner
    sees *why* without opening the pane; an empty/whitespace recap just drops the
    trailing dash.
    """
    if status == "ok":
        return None
    recap = " ".join(note.split())  # trim + collapse the free-text recap
    tail = f" — {recap}" if recap else ""
    if status == "needs-owner":
        return f"swarm: {phase} needs you{tail}"
    return f"swarm: {phase} FAILED{tail}"


def done(cfg: Config, phase: str, status: str, note: str = "") -> None:
    """Signal phase completion.

    Order matters: (1) write the durable sentinel; (2) telegram the owner *from
    the worker itself* — but ONLY when the outcome is not a clean success:
    ``needs-owner`` and ``fail`` ping with the recap, ``ok`` stays silent; (3) hold
    the slot for ``done_grace_s`` so the worker has a buffer to flush any last work
    before the supervisor reclaims it; (4) best-effort poke. The grace sleeps in
    the worker's own process, so the single-threaded supervisor loop is never
    blocked; ``done_grace_s = 0`` (the default) keeps the immediate path.
    """
    _write_sentinel(cfg, phase, status, note)
    ping = _completion_ping(phase, status, note)
    if ping is not None:
        telegram.notify(cfg.telegram_notify, ping)
    if cfg.done_grace_s > 0:
        time.sleep(cfg.done_grace_s)
    _poke_fifo(cfg, f"done {phase} {status}\n")


def waiting(cfg: Config, phase: str, note: str = "") -> None:
    """Signal that the worker for ``phase`` is blocked on the owner.

    Mirror of :func:`done`'s ping path: telegram the owner the question *from the
    worker itself* (``note`` in hand, so they can answer in the pane), then a
    best-effort non-blocking FIFO poke asking the supervisor to arm the park timer.
    Never hangs the worker if the supervisor is down. The note is NOT sent over the
    FIFO — only ``waiting <phase>`` — since parking keys on the phase alone.
    """
    recap = " ".join(note.split())  # trim + collapse the free-text question
    tail = f" — {recap}" if recap else ""
    telegram.notify(cfg.telegram_notify, f"swarm: {phase} is waiting on you{tail}")
    _poke_fifo(cfg, f"waiting {phase}\n")
