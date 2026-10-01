"""The transient merge-conflict resolver pane (tmux driver only).

When the integrator hits a merge conflict it opens a ``claude`` session in a
dedicated ``resolve-<phase>`` window, pointed at ``prompts/resolver.md`` and at
the **specific repo** that conflicted (the umbrella or a sibling — a phase's code
lives in a sibling). That session resolves the conflict in that repo's working
tree, commits, then runs ``swarm resolved <phase>`` — which unblocks the
merge-queue. The bare driver (hermetic tests) has no pane: resolution is faked
and signalled straight on the FIFO.

Under lanes the target is the phase's own **worktree** instead: a
catch-up merge of main into the branch conflicted, or the combined tree failed
the lane check. The owner's checkout stays clean on main, and the instruction
line comes from :func:`landing.resolver_brief`.

A session that never takes its brief resolves nothing, and the merge queue is
held for as long as it sits there. So :func:`spawn` reports a window only once
the brief was delivered: a session that did not take it is closed and one more
is opened, and when that one fails too the caller is told no resolver started.
"""

from __future__ import annotations

import os
import threading
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


#: Sessions one hold may open before :func:`spawn` gives up. The second is a
#: fresh window, never a second try at typing into the first: a box that may
#: already hold the brief would submit it twice.
SPAWN_ATTEMPTS = 2

#: The reaper of each phase's last resolver, while it may still be looking.
_REAPING: dict[str, threading.Thread] = {}


def brief_path(cfg: Config, phase: str) -> Path:
    """``<state>/resolver/<phase>.brief.md``: what the pane's one typed line
    points at."""
    return cfg.state_dir / "resolver" / f"{phase}.brief.md"


def _await_reap(phase: str) -> None:
    """Wait out the reaper of ``phase``'s last resolver.

    It finds that session's processes by the marker ``resolver:<phase>``, a
    moment after the window closed (:data:`session.REAP_GRACE_S`). The next
    session for the phase carries the same marker, so started before that look
    it would be ended by it."""
    reaping = _REAPING.pop(phase, None)
    if reaping is not None:
        reaping.join()


def spawn(
    cfg: Config, phase: str, repo: Path, log: Log, line: str | None = None
) -> str | None:
    """Open the resolver pane in ``repo``; return its window id (tmux), or
    ``None`` when no resolver is on the hold.

    ``repo`` is a canonical checkout, or under lanes a phase's worktree, with
    ``line`` the lane-mode instruction that replaces the default one."""
    if cfg.driver != "tmux":
        log.line(f"RESOLVER-SPAWN skipped driver={cfg.driver} {phase}")
        return None
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
    model = f" --model {cfg.resolver_model}" if cfg.resolver_model else ""
    name = launch_mod.with_name(f"claude{model}", launch_mod.session_name("resolver", phase))
    cmd = cfg.resolver_cmd or f"cd {repo} && exec {name}"
    for attempt in range(1, SPAWN_ATTEMPTS + 1):
        _await_reap(phase)
        win = tmux.new_window(cfg.session, f"resolve-{phase}")
        panes = tmux.list_panes(win)
        if not panes:
            log.line(f"RESOLVER-SPAWN-FAIL {phase} no-pane")
            return None
        tmux.respawn_pane(panes[0], cmd, env=_resolver_env(cfg, phase))
        if cfg.resolver_cmd or _deliver(cfg, panes[0], phase, repo, log, line):
            log.line(f"RESOLVER-SPAWN {phase} repo={repo.name} win={win}")
            return win
        # It booted without its brief (or never booted): left open it would sit
        # idle on a held queue while the supervisor waits for `swarm resolved`.
        close(cfg, win, log, phase)
        if attempt < SPAWN_ATTEMPTS:
            log.line(f"RESOLVER-RETRY {phase} attempt={attempt + 1}")
    log.line(f"RESOLVER-SPAWN-FAIL {phase} brief-not-delivered")
    return None


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


def _deliver(
    cfg: Config, pane: str, phase: str, repo: Path, log: Log, lane: str | None = None
) -> bool:
    """Point a freshly launched session at its brief; False if it did not take it.

    The brief goes to a file and the pane gets one short line naming it. A lane
    brief runs past a thousand characters (five long paths), and claude folds a
    typed line that long into ``[Pasted text #1]``: an Enter arriving with it is
    swallowed into the paste, and the brief sits in the box unsent."""
    prompt_file = prompt_path("resolver.md")
    if not prompt_file.is_file():
        log.line(f"RESOLVER-PROMPT-MISSING {prompt_file}")
        return False
    if not launch_mod.await_ready(cfg, pane, log):
        log.line(f"RESOLVER-READY-TIMEOUT {phase}")
        return False
    brief = lane or (
        f"Read {prompt_file} and follow it exactly. You are resolving a git merge "
        f"conflict in the repo {repo} while merging swarm/{phase} into "
        f"{cfg.git_main_branch}. Work only in {repo}. When done, run "
        f"`swarm resolved {phase}`."
    )
    checks = cfg.git_auto_resolve_check
    if checks:
        named = "; ".join(f"{glob}: `{cmd}`" for glob, cmd in checks.items())
        brief += f" The project's checks for resolved files: {named}."
    brief_file = brief_path(cfg, phase)
    try:
        brief_file.parent.mkdir(parents=True, exist_ok=True)
        brief_file.write_text(brief + "\n", encoding="utf-8")
    except OSError as exc:
        log.line(f"RESOLVER-BRIEF-WRITE-FAILED {phase} {exc}")
        return False
    line = (
        f"You are the swarm's merge resolver for {phase}. Read your full brief in"
        f" {brief_file} first, then do exactly what it says."
    )
    sent = tmux.send_submit_ex(pane, line)
    if sent not in tmux.SUBMIT_OK:
        log.line(f"RESOLVER-SUBMIT-LOST {phase} {sent}")
        return False
    return True


def close(cfg: Config, win: str, log: Log, phase: str | None = None) -> None:
    """Close a resolver window (idempotent; no-op on the bare driver), and end
    every process the resolver session for ``phase`` started."""
    if cfg.driver == "tmux":
        tmux.kill_window(win)
    if phase:
        brief_path(cfg, phase).unlink(missing_ok=True)
        reaping = session_mod.reap_session(cfg, "resolver", phase, log)
        if reaping is not None:
            _REAPING[phase] = reaping
    log.line(f"RESOLVER-CLOSE win={win}")
