"""The transient merge-conflict resolver pane (tmux driver only).

When the integrator hits a merge conflict it opens a ``claude`` session in a
dedicated ``resolve-<phase>`` window, pointed at ``prompts/resolver.md`` and at
the **specific repo** that conflicted (the umbrella or a sibling — a phase's code
lives in a sibling). That session resolves the conflict in that repo's working
tree, commits, then runs ``swarm resolved <phase>`` — which unblocks the
merge-queue. The bare driver (hermetic tests) has no pane: resolution is faked
and signalled straight on the FIFO.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import launch as launch_mod
from . import session as session_mod
from . import tmux
from .config import Config
from .logutil import Log
from .procs import SESSION_ENV

_FORWARD_ENV = (
    "SWARM_STATE_DIR",
    "SWARM_SLUG",
    "SWARM_DRIVER",
    "SWARM_BIN",
    "SWARM_TG_SINK",
    "SWARM_GIT_ISOLATION",
    "SWARM_GIT_MAIN",
    "SWARM_GIT_REPOS",
    "SWARM_READY_MARKER",
)


def _resolver_env(cfg: Config, phase: str) -> dict[str, str]:
    """The run's variables plus ``TMPDIR``. The resolver finishes ``phase``'s
    integration, so it shares that phase's temp dir, which goes when the merge
    lands (``gitq._rmtree_mirror``) — no second lifetime to track."""
    env = {"SWARM_STATE_DIR": str(cfg.state_dir), SESSION_ENV: f"resolver:{phase}",
           **launch_mod.tmp_env(cfg, phase)}
    for key in _FORWARD_ENV:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


def spawn(cfg: Config, phase: str, repo: Path, log: Log) -> str | None:
    """Open the resolver pane in ``repo``; return its window id (tmux) or ``None``."""
    if cfg.driver != "tmux":
        log.line(f"RESOLVER-SPAWN skipped driver={cfg.driver} {phase}")
        return None
    win = tmux.new_window(cfg.session, f"resolve-{phase}")
    panes = tmux.list_panes(win)
    if not panes:
        log.line(f"RESOLVER-SPAWN-FAIL {phase} no-pane")
        return None
    pane = panes[0]
    # Pre-accept claude's folder-trust dialog for THIS repo before the pane starts.
    #
    # Workers never hit this because they run inside a per-phase worktree that
    # `launch.pretrust_dir` already seeds. The resolver is the one pane that runs
    # in a CANONICAL repo directory, and those are trusted only if the owner has
    # personally opened claude there before — so a resolver spawned
    # in a repo, the trust dialog swallowed the injected prompt, and the whole
    # integration queue sat blocked behind a dialog nobody was watching.
    launch_mod.pretrust_dir(repo, log)
    # [swarm].resolver_cmd, NOT master_cmd. Reusing master_cmd here meant any
    # custom master silently became the resolver too -- and the second branch
    # then skipped priming entirely, leaving an unprompted claude staring at a
    # conflicted tree while the merge queue stayed held -- and a launch script
    # that no-ops on resolve-* windows would leave every merge conflict silently
    # landing on the owner to fix by hand.
    cmd = cfg.resolver_cmd or f"cd {repo} && exec claude"
    tmux.respawn_pane(pane, cmd, env=_resolver_env(cfg, phase))
    if not cfg.resolver_cmd:
        _deliver(cfg, pane, phase, repo, log)
    log.line(f"RESOLVER-SPAWN {phase} repo={repo.name} win={win}")
    return win


def prompt_path(name: str) -> Path:
    """Locate a prompt file, packaged copy first.

    ``master.py`` and this module both resolved prompts by walking three
    directories up from ``__file__``, which only works under an editable install.
    A normal install has no ``prompts/`` beside the package, so delivery failed,
    spawn returned False and the run stalled on one log line with no telegram.
    """
    packaged = Path(__file__).resolve().parent / "prompts" / name
    if packaged.is_file():
        return packaged
    return Path(__file__).resolve().parent.parent.parent / "prompts" / name


def _deliver(cfg: Config, pane: str, phase: str, repo: Path, log: Log) -> None:
    prompt_file = prompt_path("resolver.md")
    if not prompt_file.is_file():
        log.line(f"RESOLVER-PROMPT-MISSING {prompt_file}")
        return
    if not launch_mod.await_ready(cfg, pane, log):
        log.line(f"RESOLVER-READY-TIMEOUT {phase}")
        return
    line = (
        f"Read {prompt_file} and follow it exactly. You are resolving a git merge "
        f"conflict in the repo {repo} while merging swarm/{phase} into "
        f"{cfg.git_main_branch}. Work only in {repo}. When done, run "
        f"`swarm resolved {phase}`."
    )
    if not tmux.send_submit(pane, line):
        log.line(f"RESOLVER-SUBMIT-LOST {phase}")


def close(cfg: Config, win: str, log: Log, phase: str | None = None) -> None:
    """Close a resolver window (idempotent; no-op on the bare driver), and end
    every process the resolver session for ``phase`` started."""
    if cfg.driver == "tmux":
        tmux.kill_window(win)
    if phase:
        session_mod.reap_session(cfg, "resolver", phase, log)
    log.line(f"RESOLVER-CLOSE win={win}")
