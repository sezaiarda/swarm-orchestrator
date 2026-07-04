"""The transient merge-conflict resolver pane (tmux driver only).

When the integrator hits a merge conflict it opens a ``claude`` session in a
dedicated ``resolve-<phase>`` window pointed at ``prompts/resolver.md``. That
session resolves the conflict in the canonical project tree, commits, then runs
``swarm resolved <phase>`` — which unblocks the merge-queue. The bare driver
(hermetic tests) has no pane: resolution is faked and signalled straight on the
FIFO.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import tmux
from .config import Config, ready_needle
from .logutil import Log

_READY_TIMEOUT_S = 30.0
_FORWARD_ENV = (
    "SWARM_STATE_DIR",
    "SWARM_SLUG",
    "SWARM_DRIVER",
    "SWARM_BIN",
    "SWARM_TG_SINK",
    "SWARM_READY_MARKER",
)


def _resolver_env(cfg: Config) -> dict[str, str]:
    env = {"SWARM_STATE_DIR": str(cfg.state_dir)}
    for key in _FORWARD_ENV:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


def spawn(cfg: Config, phase: str, log: Log) -> str | None:
    """Open the resolver pane; return its window id (tmux) or ``None`` (bare)."""
    if cfg.driver != "tmux":
        log.line(f"RESOLVER-SPAWN skipped driver={cfg.driver} {phase}")
        return None
    win = tmux.new_window(cfg.session, f"resolve-{phase}")
    panes = tmux.list_panes(win)
    if not panes:
        log.line(f"RESOLVER-SPAWN-FAIL {phase} no-pane")
        return None
    pane = panes[0]
    cmd = cfg.master_cmd or f"cd {cfg.project_dir} && exec claude"
    tmux.respawn_pane(pane, cmd, env=_resolver_env(cfg))
    if not cfg.master_cmd:
        _deliver(cfg, pane, phase, log)
    log.line(f"RESOLVER-SPAWN {phase} win={win}")
    return win


def _deliver(cfg: Config, pane: str, phase: str, log: Log) -> None:
    prompt_file = Path(__file__).resolve().parent.parent.parent / "prompts" / "resolver.md"
    if not prompt_file.is_file():
        log.line(f"RESOLVER-PROMPT-MISSING {prompt_file}")
        return
    tmux.await_text(pane, ready_needle(cfg), _READY_TIMEOUT_S)
    line = (
        f"Read {prompt_file} and follow it exactly. You are resolving a git merge "
        f"conflict in {cfg.project_dir} while merging swarm/{phase} into "
        f"{cfg.git_main_branch}. When done, run `swarm resolved {phase}`."
    )
    tmux.send_submit(pane, line)


def close(cfg: Config, win: str, log: Log) -> None:
    """Close a resolver window (idempotent; no-op on the bare driver)."""
    if cfg.driver == "tmux":
        tmux.kill_window(win)
    log.line(f"RESOLVER-CLOSE win={win}")
