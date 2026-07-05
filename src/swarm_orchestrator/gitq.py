"""Git worktrees + a serialized merge-queue for ``isolation = "worktree"``.

Each phase builds against a **full, isolated mirror of the whole workspace**: a
worktree of the umbrella (project) repo on branch ``swarm/<phase>``, with a
worktree of every component repo nested inside it at its real path, each on the
same branch. The worker's cwd is the umbrella worktree, so the mirror looks
exactly like the real project (``cd pricing`` just works) but nothing it does
touches the canonical repos or another phase's mirror.

Which component repos are mirrored is set by ``[git].repos`` globs (default: every
independent git repo that is a direct child of the project root). A single-repo
project matches none and gets a one-repo mirror.

On ``swarm done ok`` a single serialized integrator merges every repo the phase
actually changed into its main and pushes; repos it didn't touch are 0 commits
ahead and are dropped without any network. On ``fail`` (or a launch failure) all
of the phase's worktrees and branches are removed with no merge — a clean
rollback. Every repo mutation is serialized by an ``flock`` keyed per repo.

Statuses: :data:`MERGED` (all repos clean, pushed, pruned), :data:`CONFLICT` (a
repo left mid-merge for a resolver), :data:`DIRTY` (a repo's canonical tree had
uncommitted changes — held), :data:`PUSH_FAILED` (merged locally, push failed —
retryable).
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Config
from .logutil import Log

MERGED = "merged"
CONFLICT = "conflict"
DIRTY = "dirty"
PUSH_FAILED = "push_failed"

_GIT_TIMEOUT_S = 120.0
_PUSH_ATTEMPTS = 5

# git env that makes remote ops fail fast instead of blocking on an interactive
# credential / host-key prompt (which capture_output can never answer).
_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -oBatchMode=yes"),
}


class GitError(RuntimeError):
    """A git subprocess failed unexpectedly (not an expected merge conflict)."""


def _git(
    repo: Path, *args: str, check: bool = True, timeout: float = _GIT_TIMEOUT_S
) -> subprocess.CompletedProcess:
    """Run ``git -C <repo> <args>``; raise :class:`GitError` on failure if check.

    A subprocess timeout (a hung / credential-prompting remote) is converted to
    a :class:`GitError` — never allowed to escape as ``TimeoutExpired`` — so a
    single ``except GitError`` at the call sites covers every git failure and the
    sole supervisor loop can never be killed by an uncaught timeout.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **_GIT_ENV},
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} @ {repo}: timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} @ {repo}: {proc.stderr.strip()}")
    return proc


def _slug(repo: Path) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in repo.name]
    return "".join(keep).strip("-") or "repo"


@contextmanager
def repo_lock(cfg: Config, repo: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` for one repo. Never nest on one repo."""
    cfg.git_lock_dir.mkdir(parents=True, exist_ok=True)
    lock = cfg.git_lock_dir / f"{_slug(repo)}.lock"
    with lock.open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# -- repo predicates ------------------------------------------------------
def _has_remote(repo: Path) -> bool:
    return bool(_git(repo, "remote", check=False).stdout.strip())


def _branch_exists(repo: Path, branch: str) -> bool:
    return (
        _git(repo, "rev-parse", "--verify", "--quiet", branch, check=False).returncode
        == 0
    )


def _ref_exists(repo: Path, ref: str) -> bool:
    return (
        _git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0
    )


def _commits_ahead(repo: Path, base: str, tip: str) -> int:
    out = _git(repo, "rev-list", "--count", f"{base}..{tip}", check=False)
    try:
        return int(out.stdout.strip() or "0")
    except ValueError:
        return 0


def _merge_in_progress(repo: Path) -> bool:
    return _ref_exists(repo, "MERGE_HEAD")


def _rebase_in_progress(repo: Path) -> bool:
    gd = _git(repo, "rev-parse", "--git-dir", check=False).stdout.strip()
    if not gd:
        return False
    base = Path(gd) if os.path.isabs(gd) else repo / gd
    return (base / "rebase-merge").exists() or (base / "rebase-apply").exists()


def _dirty(repo: Path) -> bool:
    """True if the canonical tree has uncommitted changes to TRACKED files.

    Untracked files are ignored on purpose: they don't block a clean merge (a
    merge that would actually overwrite one fails on its own and surfaces as a
    CONFLICT), so a stray untracked file — e.g. an uncommitted ``.swarm.toml`` at
    the project root — must not falsely hold the whole merge-queue as DIRTY.
    """
    return bool(
        _git(
            repo, "status", "--porcelain", "--untracked-files=no", check=False
        ).stdout.strip()
    )


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()


def _local_ahead_of_origin(repo: Path, main: str) -> bool:
    """True if local ``main`` has commit(s) that ``origin/main`` lacks."""
    if not _has_remote(repo) or not _ref_exists(repo, f"origin/{main}"):
        return False
    return _commits_ahead(repo, f"origin/{main}", main) > 0


# -- workspace repo discovery ---------------------------------------------
def discovered_repos(cfg: Config) -> list[Path]:
    """Component repos to mirror, per ``[git].repos`` globs (default direct
    children). Independent git repos under the project root, excluding the
    umbrella itself; sorted and deduped. ``glob`` skips dot-names, so ``.git`` /
    ``.venv`` are never swept in by the default ``*``.
    """
    if cfg.git_isolation != "worktree":
        return []
    project = cfg.project_dir.resolve()
    found: dict[str, Path] = {}
    for pattern in cfg.git_repos:
        try:
            matches = cfg.project_dir.glob(pattern)
        except (ValueError, OSError):
            continue
        for p in matches:
            rp = p.resolve()
            if p.is_dir() and rp != project and (p / ".git").exists():
                found[str(rp)] = p
    return [found[k] for k in sorted(found)]


def _repo_main(cfg: Config, repo: Path) -> str:
    """A repo's integration branch — the configured main if present, else the
    branch it currently has checked out."""
    if repo.resolve() == cfg.project_dir.resolve():
        return cfg.git_main_branch
    if _branch_exists(repo, cfg.git_main_branch):
        return cfg.git_main_branch
    cur = _current_branch(repo)
    return cur if cur and cur != "HEAD" else cfg.git_main_branch


def _repos(cfg: Config) -> list[tuple[Path, str]]:
    """All repos a phase spans: components first, umbrella last. That order lands
    code before the umbrella tick that announces it, and (for cleanup) removes
    nested worktrees before their parent."""
    repos = [(r, _repo_main(cfg, r)) for r in discovered_repos(cfg)]
    repos.append((cfg.project_dir, cfg.git_main_branch))
    return repos


def _wt_for(cfg: Config, repo: Path, phase: str) -> Path:
    """Where a repo's per-phase worktree lives — nested under the umbrella
    worktree at the repo's real relative path, so the mirror matches the project
    layout."""
    umbrella_wt = cfg.wt_dir / phase
    if repo.resolve() == cfg.project_dir.resolve():
        return umbrella_wt
    rel = repo.resolve().relative_to(cfg.project_dir.resolve())
    return umbrella_wt / rel


# -- cleanup --------------------------------------------------------------
def _gc(cfg: Config, repo: Path, phase: str, log: Log) -> None:
    """Remove one repo's ``swarm/<phase>`` worktree then delete its branch.
    Caller holds the repo lock. Uniform across umbrella and components (every
    repo has a per-phase worktree now)."""
    branch = f"swarm/{phase}"
    wt = _wt_for(cfg, repo, phase)
    if wt.exists():
        _git(repo, "worktree", "remove", "--force", str(wt), check=False)
    _git(repo, "worktree", "prune", check=False)
    if _branch_exists(repo, branch):
        _git(repo, "branch", "-D", branch, check=False)


def _rmtree_mirror(cfg: Config, phase: str) -> None:
    """Best-effort remove the phase's umbrella worktree directory shell (any
    empty nesting dirs a component worktree left behind)."""
    shutil.rmtree(cfg.wt_dir / phase, ignore_errors=True)


def discard(cfg: Config, phase: str, log: Log) -> None:
    """Drop a phase's work everywhere with NO merge (launch failure / failed
    build). Components first (nested), umbrella last, then wipe the mirror dir —
    a clean rollback that leaves no half-built worktree or branch behind."""
    for repo, _main in _repos(cfg):
        with repo_lock(cfg, repo):
            _gc(cfg, repo, phase, log)
    _rmtree_mirror(cfg, phase)
    log.line(f"WORKTREE-DISCARD {phase}")


# -- worktree creation ----------------------------------------------------
def _mirror_base(repo: Path, main: str) -> str:
    """The commit to seed a phase mirror from: local ``main`` unless the remote
    strictly fast-forwards it.

    Preferring ``origin/<main>`` picks up work integrated on another machine, but
    only when it *fast-forwards* local ``main`` (local fully contained in origin).
    If local ``main`` is ahead or has diverged — e.g. an owner commit not yet
    pushed (a ``/prime`` patch, a ``.swarm.toml``) — seeding from the remote would
    silently drop it from every worktree, so we seed from local ``main`` instead.
    """
    if not _has_remote(repo):
        return main
    _git(repo, "fetch", "origin", check=False)  # best effort
    om = f"origin/{main}"
    if not _ref_exists(repo, om):
        return main
    # origin strictly ahead  <=>  local has no commit origin lacks (fully
    # contained) AND origin has >=1 commit local lacks. Any other shape (equal,
    # local ahead, diverged) keeps local `main` so unpushed owner work survives.
    if _commits_ahead(repo, om, main) == 0 and _commits_ahead(repo, main, om) > 0:
        return om
    return main


def worktree_add(cfg: Config, phase: str, log: Log) -> Path:
    """Build the phase's full-workspace mirror; return the umbrella worktree
    (the worker's cwd).

    The umbrella worktree is created first (its directory must exist before
    component worktrees nest inside it), then each component. Each branches off
    :func:`_mirror_base` — local ``main`` unless ``origin/<main>`` strictly
    fast-forwards it — so unpushed owner commits are never dropped from the
    mirror. A stale leftover from a prior run is GC'd first.
    """
    branch = f"swarm/{phase}"
    umbrella = (cfg.project_dir, cfg.git_main_branch)
    ordered = [umbrella] + [
        (r, m) for (r, m) in _repos(cfg) if r.resolve() != cfg.project_dir.resolve()
    ]
    cfg.wt_dir.mkdir(parents=True, exist_ok=True)
    for repo, main in ordered:
        wt = _wt_for(cfg, repo, phase)
        with repo_lock(cfg, repo):
            base = _mirror_base(repo, main)
            if _branch_exists(repo, branch) or wt.exists():
                _gc(cfg, repo, phase, log)  # stale leftover -> start clean
            wt.parent.mkdir(parents=True, exist_ok=True)
            _git(repo, "worktree", "add", str(wt), "-b", branch, base)
    log.line(f"WORKTREE-ADD {phase} {cfg.wt_dir / phase}")
    return cfg.wt_dir / phase


# -- push (optimistic, merge-based reconcile — never rebases a merge) ------
def _push(repo: Path, main: str, log: Log) -> str:
    """Push local ``main``; on a non-ff rejection, MERGE origin/main in and retry.

    Reconciling by **merge** (never rebase) means a concurrent external commit is
    integrated without flattening our merge commit, and a reconcile that itself
    conflicts leaves a *real* mid-merge state a resolver can finish — instead of a
    clean-tree wedge. A push that fails for a non-rejection reason (unreachable
    remote, auth) returns :data:`PUSH_FAILED` (retryable), not a conflict.
    """
    if not _has_remote(repo):
        return MERGED
    for attempt in range(1, _PUSH_ATTEMPTS + 1):
        push = _git(repo, "push", "origin", main, check=False)
        if push.returncode == 0:
            return MERGED
        err = (push.stderr or "").lower()
        rejected = any(
            k in err
            for k in ("non-fast-forward", "fetch first", "rejected", "failed to push some refs")
        )
        if not rejected:
            log.line(f"PUSH-FAIL {main} {push.stderr.strip()[:120]}")
            return PUSH_FAILED
        _git(repo, "fetch", "origin", check=False)
        if _ref_exists(repo, f"origin/{main}"):
            merge = _git(repo, "merge", "--no-edit", f"origin/{main}", check=False)
            if merge.returncode != 0:
                log.line(f"PUSH-RECONCILE-CONFLICT {main}")
                return CONFLICT
        log.line(f"PUSH-RETRY {attempt} {main}")
    return PUSH_FAILED


def push_with_retry(repo: Path, main: str, log: Log) -> bool:
    """Compat wrapper: push ``main`` with optimistic merge-reconcile. True == pushed."""
    return _push(repo, main, log) == MERGED


# -- integration ----------------------------------------------------------
def _integrate_one(cfg: Config, repo: Path, main: str, phase: str, log: Log) -> str:
    """Land ``swarm/<phase>`` into ``main`` for a single repo. Serialized.

    Untouched-by-this-phase repos are the common case in a big workspace: the
    branch is 0 commits ahead of a not-behind main, so they are pruned with **no
    network** (no fetch/merge/push). Only repos the phase actually changed do the
    full merge+push. Idempotent/resumable: a dirty canonical tree is held
    (:data:`DIRTY`), a merge/push conflict returns :data:`CONFLICT` leaving a
    resolvable mid-merge, a pure push failure returns :data:`PUSH_FAILED`.
    """
    branch = f"swarm/{phase}"
    with repo_lock(cfg, repo):
        has_branch = _branch_exists(repo, branch)
        ahead_origin = _local_ahead_of_origin(repo, main)
        if not has_branch and not ahead_origin:
            return MERGED  # nothing to integrate here
        # Untouched: branch adds nothing to a main that is level with origin.
        if has_branch and not ahead_origin and _commits_ahead(repo, main, branch) == 0:
            _gc(cfg, repo, phase, log)
            return MERGED
        if _merge_in_progress(repo) or _rebase_in_progress(repo):
            return CONFLICT  # a prior op is still mid-resolution
        if _dirty(repo):
            log.line(f"INTEGRATE-DIRTY {phase} {repo.name}")
            return DIRTY
        _git(repo, "checkout", main)
        if _has_remote(repo):
            _git(repo, "fetch", "origin", check=False)
            if _ref_exists(repo, f"origin/{main}") and _commits_ahead(
                repo, main, f"origin/{main}"
            ) > 0:
                base = _git(repo, "merge", "--no-edit", f"origin/{main}", check=False)
                if base.returncode != 0:
                    log.line(f"INTEGRATE-BASE-CONFLICT {phase} {repo.name}")
                    return CONFLICT
        if has_branch and _commits_ahead(repo, main, branch) > 0:
            merge = _git(repo, "merge", "--no-ff", "--no-edit", branch, check=False)
            if merge.returncode != 0:
                log.line(f"INTEGRATE-CONFLICT {phase} {repo.name}")
                return CONFLICT
        if _has_remote(repo):
            pushed = _push(repo, main, log)
            if pushed != MERGED:
                log.line(f"INTEGRATE-PUSH-{pushed.upper()} {phase} {repo.name}")
                return pushed
        _gc(cfg, repo, phase, log)
        log.line(f"INTEGRATE-MERGED {phase} {repo.name}")
        return MERGED


def integrate(cfg: Config, phase: str, log: Log) -> str:
    """Land ``swarm/<phase>`` across every repo the phase mirrored, components
    first then umbrella. Returns :data:`MERGED` only when all repos are clean;
    the first non-merged repo's status short-circuits and is returned so the
    supervisor can hold the queue and (for a conflict) point a resolver at the
    exact repo (:func:`blocked_repo`)."""
    for repo, main in _repos(cfg):
        result = _integrate_one(cfg, repo, main, phase, log)
        if result != MERGED:
            return result
    _rmtree_mirror(cfg, phase)  # all repos merged -> drop the empty mirror shell
    return MERGED


def blocked_repo(cfg: Config, phase: str) -> Path | None:
    """The repo currently mid-merge / mid-rebase / dirty for ``phase``, if any.
    A :data:`CONFLICT` or :data:`DIRTY` leaves one; a :data:`PUSH_FAILED` does not."""
    for repo, _main in _repos(cfg):
        if _merge_in_progress(repo) or _rebase_in_progress(repo) or _dirty(repo):
            return repo
    return None


def resolve_ready(cfg: Config, repo: Path) -> bool:
    """True once a held repo is safe to re-integrate: no merge/rebase in progress
    and a clean tree. A premature ``swarm resolved`` fails this."""
    if repo is None:
        return True
    with repo_lock(cfg, repo):
        return not (
            _merge_in_progress(repo) or _rebase_in_progress(repo) or _dirty(repo)
        )


# -- restart reconcile (sentinel-driven, never topology-driven) -----------
def sentinel_done(cfg: Config) -> dict[str, str]:
    """Rehydrate the ``{phase: status}`` map from durable ``swarm done`` sentinels.

    ``swarm done`` writes ``<state>/done/<phase>.<status>`` before it pokes the
    FIFO, so the sentinels — not transient in-memory state — are the durable
    record of which phases actually finished. Reading them back on restart lets
    reconcile tell a *finished* phase from an *interrupted* one, instead of
    guessing from branch topology.
    """
    out: dict[str, str] = {}
    if not cfg.done_dir.is_dir():
        return out
    for entry in cfg.done_dir.iterdir():
        if entry.name.startswith(".") or "." not in entry.name:
            continue
        phase, _, status = entry.name.rpartition(".")
        if phase and status in ("ok", "fail", "skip"):
            if out.get(phase) != "ok":  # a completed build wins over fail/skip
                out[phase] = status
    return out


def _swarm_branches(repo: Path) -> list[str]:
    out = _git(
        repo, "branch", "--list", "swarm/*", "--format=%(refname:short)", check=False
    )
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def _all_swarm_phases(cfg: Config) -> set[str]:
    phases: set[str] = set()
    for repo in [cfg.project_dir, *discovered_repos(cfg)]:
        for branch in _swarm_branches(repo):
            phases.add(branch[len("swarm/") :])
    return phases


def reconcile_orphans(
    cfg: Config, done_phases: dict[str, str], log: Log
) -> list[str]:
    """Reconcile leftover ``swarm/*`` branches at ``swarm up`` (sentinel-driven).

    A phase already recorded done is cleaned up. A phase with an ``ok`` sentinel
    (worker finished, supervisor died before integrating) has its integration
    completed. A leftover branch with **no** ``ok`` sentinel was interrupted
    mid-build — discarded and left NOT done so the master rebuilds it, never
    declared complete off branch topology. Returns the phases integrated.
    """
    sentinels = sentinel_done(cfg)
    integrated: list[str] = []
    for phase in sorted(_all_swarm_phases(cfg)):
        if phase in done_phases:
            discard(cfg, phase, log)
            log.line(f"RECONCILE-GC {phase} already-recorded")
        elif sentinels.get(phase) == "ok":
            result = integrate(cfg, phase, log)
            if result == MERGED:
                integrated.append(phase)
                log.line(f"RECONCILE-INTEGRATED {phase}")
            else:
                log.line(f"RECONCILE-HELD {phase} {result}")
        else:
            discard(cfg, phase, log)
            log.line(f"RECONCILE-DISCARD {phase} interrupted")
    return integrated
