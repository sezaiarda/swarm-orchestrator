"""Git worktrees + a serialized merge-queue for ``isolation = "worktree"``.

Each worker builds on branch ``swarm/<phase>`` in an isolated worktree of the
project repo; a single serialized integrator (the supervisor) merges that branch
into the latest ``main`` and pushes. Pure subprocess over ``git``. Every
project-repo mutation is serialized by an ``flock`` on
``<state>/git/<repo-slug>.lock`` so the (single-threaded) supervisor and a
``swarm up`` orphan-reconcile never race the same repo.

Statuses returned to the caller: :data:`MERGED` (clean, pushed, pruned) or
:data:`CONFLICT` (left mid-merge in the project tree — a resolver must finish).
"""

from __future__ import annotations

import fcntl
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Config
from .logutil import Log

MERGED = "merged"
CONFLICT = "conflict"

_GIT_TIMEOUT_S = 120.0
_PUSH_ATTEMPTS = 5


class GitError(RuntimeError):
    """A git subprocess failed unexpectedly (not an expected merge conflict)."""


def _git(
    repo: Path, *args: str, check: bool = True, timeout: float = _GIT_TIMEOUT_S
) -> subprocess.CompletedProcess:
    """Run ``git -C <repo> <args>``; raise :class:`GitError` on failure if check."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} @ {repo}: {proc.stderr.strip()}")
    return proc


def _slug(repo: Path) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in repo.name]
    return "".join(keep).strip("-") or "repo"


@contextmanager
def repo_lock(cfg: Config, repo: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` for one project repo. Never nest on one repo."""
    cfg.git_lock_dir.mkdir(parents=True, exist_ok=True)
    lock = cfg.git_lock_dir / f"{_slug(repo)}.lock"
    with lock.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _has_remote(repo: Path) -> bool:
    return bool(_git(repo, "remote", check=False).stdout.strip())


def _branch_exists(repo: Path, branch: str) -> bool:
    return (
        _git(repo, "rev-parse", "--verify", "--quiet", branch, check=False).returncode
        == 0
    )


def _commits_ahead(repo: Path, base: str, branch: str) -> int:
    out = _git(repo, "rev-list", "--count", f"{base}..{branch}", check=False)
    try:
        return int(out.stdout.strip() or "0")
    except ValueError:
        return 0


def _merge_in_progress(repo: Path) -> bool:
    return (
        _git(repo, "rev-parse", "--verify", "--quiet", "MERGE_HEAD", check=False).returncode
        == 0
    )


def _dirty(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain", check=False).stdout.strip())


def _worktree_path(cfg: Config, phase: str) -> Path:
    return cfg.wt_dir / phase


def _gc(cfg: Config, phase: str, log: Log) -> None:
    """Remove the phase worktree then delete its branch. Caller holds the lock."""
    repo = cfg.project_dir
    branch = f"swarm/{phase}"
    wt = _worktree_path(cfg, phase)
    if wt.exists():
        _git(repo, "worktree", "remove", "--force", str(wt), check=False)
    _git(repo, "worktree", "prune", check=False)
    if _branch_exists(repo, branch):
        _git(repo, "branch", "-D", branch, check=False)


def discard(cfg: Config, phase: str, log: Log) -> None:
    """Drop a phase's worktree + branch (launch failure / failed build)."""
    with repo_lock(cfg, cfg.project_dir):
        _gc(cfg, phase, log)
    log.line(f"WORKTREE-DISCARD {phase}")


def worktree_add(cfg: Config, phase: str, log: Log) -> Path:
    """Create ``swarm/<phase>`` off ``main`` in an isolated worktree; return path."""
    repo = cfg.project_dir
    branch = f"swarm/{phase}"
    wt = _worktree_path(cfg, phase)
    with repo_lock(cfg, repo):
        cfg.wt_dir.mkdir(parents=True, exist_ok=True)
        if _has_remote(repo):
            _git(repo, "fetch", "origin", check=False)  # best effort
        if _branch_exists(repo, branch) or wt.exists():
            _gc(cfg, phase, log)  # stale leftover -> start clean
        _git(repo, "worktree", "add", str(wt), "-b", branch, cfg.git_main_branch)
    log.line(f"WORKTREE-ADD {phase} {wt}")
    return wt


def push_with_retry(repo: Path, main: str, log: Log) -> bool:
    """Push ``main``; on rejection re-``pull --rebase`` and retry (bounded).

    A rejected push means someone integrated meanwhile — rebasing our commits on
    top and pushing again is the optimistic-concurrency path. A rebase that
    itself conflicts is aborted and reported (False) rather than papered over.
    """
    for attempt in range(1, _PUSH_ATTEMPTS + 1):
        if _git(repo, "push", "origin", main, check=False).returncode == 0:
            return True
        pull = _git(repo, "pull", "--rebase", "origin", main, check=False)
        if pull.returncode != 0:
            _git(repo, "rebase", "--abort", check=False)
            log.line(f"PUSH-REBASE-CONFLICT {main}")
            return False
        log.line(f"PUSH-RETRY {attempt} {main}")
    return False


def integrate(cfg: Config, phase: str, log: Log) -> str:
    """Merge ``swarm/<phase>`` into the latest ``main`` (serialized). Push + prune.

    Returns :data:`MERGED` (clean) or :data:`CONFLICT` (project tree left
    mid-merge for a resolver). Idempotent: an already-merged / absent branch is a
    :data:`MERGED` no-op.
    """
    repo = cfg.project_dir
    main = cfg.git_main_branch
    branch = f"swarm/{phase}"
    with repo_lock(cfg, repo):
        if not _branch_exists(repo, branch):
            log.line(f"INTEGRATE-NOOP {phase} no-branch")
            return MERGED
        _git(repo, "checkout", main)
        if _has_remote(repo):
            _git(repo, "pull", "--rebase", "origin", main, check=False)
        if _commits_ahead(repo, main, branch) == 0:
            _gc(cfg, phase, log)
            log.line(f"INTEGRATE-NOOP {phase} already-merged")
            return MERGED
        merge = _git(repo, "merge", "--no-ff", "--no-edit", branch, check=False)
        if merge.returncode != 0:
            log.line(f"INTEGRATE-CONFLICT {phase}")
            return CONFLICT
        if _has_remote(repo) and not push_with_retry(repo, main, log):
            log.line(f"INTEGRATE-PUSH-FAIL {phase}")
            return CONFLICT
        _gc(cfg, phase, log)
        log.line(f"INTEGRATE-MERGED {phase}")
        return MERGED


def finish_conflict(cfg: Config, phase: str, log: Log) -> str:
    """After a resolver committed the merge: push + prune. Serialized.

    Verifies the resolver actually finished (no ``MERGE_HEAD``, clean tree)
    before declaring success — a premature ``resolved`` keeps the queue blocked.
    """
    repo = cfg.project_dir
    main = cfg.git_main_branch
    with repo_lock(cfg, repo):
        if _merge_in_progress(repo) or _dirty(repo):
            log.line(f"RESOLVE-INCOMPLETE {phase}")
            return CONFLICT
        if _has_remote(repo) and not push_with_retry(repo, main, log):
            log.line(f"RESOLVE-PUSH-FAIL {phase}")
            return CONFLICT
        _gc(cfg, phase, log)
        log.line(f"INTEGRATE-MERGED {phase} resolved")
        return MERGED


def _swarm_branches(repo: Path) -> list[str]:
    out = _git(
        repo, "branch", "--list", "swarm/*", "--format=%(refname:short)", check=False
    )
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def reconcile_orphans(
    cfg: Config, done_phases: dict[str, str], log: Log
) -> list[str]:
    """Reconcile leftover ``swarm/*`` branches at ``swarm up`` (git-state-derived).

    A branch whose phase is already ``done`` is GC'd (its integration already
    happened). Any other branch is an orphan (a worker committed but the
    supervisor died before ``done``) and is integrated. An orphan that conflicts
    is aborted and left for the owner (no resolver runs before the supervisor is
    up). Returns the phases integrated so the caller can mark them done.
    """
    integrated: list[str] = []
    for branch in _swarm_branches(cfg.project_dir):
        phase = branch[len("swarm/") :]
        if phase in done_phases:
            with repo_lock(cfg, cfg.project_dir):
                _gc(cfg, phase, log)
            log.line(f"RECONCILE-GC {phase}")
            continue
        if integrate(cfg, phase, log) == MERGED:
            integrated.append(phase)
            log.line(f"RECONCILE-INTEGRATED {phase}")
        else:
            with repo_lock(cfg, cfg.project_dir):
                _git(cfg.project_dir, "merge", "--abort", check=False)
            log.line(f"RECONCILE-CONFLICT {phase} left-for-owner")
    return integrated
