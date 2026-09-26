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

Nothing a worker made is ever destroyed. Before a worktree or branch holding
work that is not on main goes, its uncommitted edits are committed onto the
branch and the tip is kept under :data:`ATTIC` (``swarm gc`` prunes old ones).
An interrupted phase is not removed at all: :func:`set_aside` keeps its mirror
and its next launch resumes on the same branch.

Statuses: :data:`MERGED` (all repos clean, pushed, pruned), :data:`CONFLICT` (a
repo left mid-merge for a resolver), :data:`DIRTY` (a repo's canonical tree had
uncommitted changes — held), :data:`PUSH_FAILED` (merged locally, push failed —
retryable). A caller that passes ``pushes`` to :func:`integrate` never sees
:data:`PUSH_FAILED`: the merge is on local main, which is all the next worker
branches from, so the push is recorded as *owed* and the integration counts.
"""

from __future__ import annotations

import calendar
import fcntl
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import automerge
from . import statuses
from .config import Config
from .logutil import Log

MERGED = "merged"
CONFLICT = "conflict"
DIRTY = "dirty"
PUSH_FAILED = "push_failed"

# `swarm done` completion statuses that INTEGRATE (merge into main) rather than
# roll back. The owner-facing ones land exactly like ``ok``; they differ only in
# that the owner gets told (done worker-side in :func:`launch.done`). ``fail``
# (anything else) rolls back with no merge.
DONE_INTEGRATE = statuses.INTEGRATES

_GIT_TIMEOUT_S = 120.0
_PUSH_ATTEMPTS = 5

#: Where work is kept instead of deleted: ``refs/swarm-attic/<phase>/<utc-stamp>``.
#: Not a branch, so reconcile, launch and branch listings never see it.
ATTIC = "refs/swarm-attic"
ATTIC_STAMP = "%Y%m%dT%H%M%SZ"
_WIP_MESSAGE = "swarm: unfinished work on {phase}, saved before its worktree was set aside"

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


def branch_exists(project_dir: Path, branch: str) -> bool:
    """Whether ``branch`` exists in ``project_dir``. Public face of
    :func:`_branch_exists`, for callers that must verify a phase's branch is
    really gone (or really there) before acting on it."""
    return _branch_exists(project_dir, branch)


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


def _unmerged(repo: Path, main: str, branch: str) -> bool:
    """True when ``branch`` has a commit ``main`` lacks. An answer git cannot give
    (``main`` missing, a timeout) counts as True: the caller is deciding whether
    deleting ``branch`` loses anything, and "unknown" must mean "keep it"."""
    try:
        out = _git(repo, "rev-list", "--count", f"{main}..{branch}", check=False)
    except GitError:
        return True
    if out.returncode != 0:
        return True
    try:
        return int(out.stdout.strip() or "0") > 0
    except ValueError:
        return True


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


def _off_main(repo: Path, main: str) -> str | None:
    """The branch the owner's checkout is on when it is not ``main``, else None
    (also when git cannot say). Integration merges in that checkout and must
    never switch it for them."""
    cur = _current_branch(repo)
    if not cur or cur == main:
        return None
    return "a detached HEAD" if cur == "HEAD" else cur


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
#: Worktree removal walks the whole tree on disk, so it is IO-bound and scales
#: with the checkout — a large dependency tree is hundreds of thousands of
#: files. Under a loaded box (parallel builds) it can far exceed the
#: ordinary git timeout, so it gets its own, generous one.
_GC_TIMEOUT_S = 900.0


def _is_worktree(wt: Path) -> bool:
    """``wt`` is a checkout's own top level, not a directory inside another one.

    A component worktree nests inside the umbrella's, so ``git -C`` on a missing
    component path silently answers for the umbrella instead."""
    return (wt / ".git").exists()


def _nested_repos(cfg: Config, repo: Path) -> list[str]:
    """Component repos nested under ``repo``, relative to it: separate
    worktrees inside this one that its own ``git add`` must never pick up."""
    root = cfg.project_dir.resolve()
    here = repo.resolve().relative_to(root)
    out: list[str] = []
    for other in discovered_repos(cfg):
        rel = other.resolve().relative_to(root)
        if rel != here and (here == Path(".") or here in rel.parents):
            out.append(str(rel.relative_to(here)))
    return out


def _wt_changes(cfg: Config, repo: Path, wt: Path) -> str | None:
    """``git status --porcelain`` of one worktree, nested repos left out; None
    when it cannot be read."""
    excludes = [f":(exclude){p}" for p in _nested_repos(cfg, repo)]
    try:
        out = _git(wt, "status", "--porcelain", "--", ".", *excludes, check=False)
    except GitError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _save_wip(cfg: Config, repo: Path, phase: str, log: Log) -> bool:
    """Commit what the phase's worktree in ``repo`` has not committed yet onto
    its branch. Caller holds the repo lock.

    True when nothing uncommitted is left to lose (saved, already clean, or no
    worktree); False when the tree could not be read or the commit failed, and
    the caller must then leave the worktree alone. ``--no-verify`` because hooks
    judge finished work, and this is unfinished work being kept.
    """
    wt = _wt_for(cfg, repo, phase)
    if not _is_worktree(wt):
        return True
    changes = _wt_changes(cfg, repo, wt)
    if changes is None:
        log.line(f"WIP-SAVE-FAILED {phase} {repo.name}: status unreadable")
        return False
    if not changes:
        return True
    excludes = [f":(exclude){p}" for p in _nested_repos(cfg, repo)]
    try:
        _git(wt, "add", "-A", "--", ".", *excludes)
        staged = _git(wt, "diff", "--cached", "--quiet", check=False)
        if staged.returncode == 0:
            return True  # only nested repos differed; they save themselves
        commit = _git(
            wt, "commit", "--no-verify", "-q", "-m", _WIP_MESSAGE.format(phase=phase),
            check=False,
        )
    except GitError as exc:
        log.line(f"WIP-SAVE-FAILED {phase} {repo.name}: {exc}")
        return False
    if commit.returncode != 0:
        log.line(f"WIP-SAVE-FAILED {phase} {repo.name}: {commit.stderr.strip()[:200]}")
        return False
    log.line(f"WIP-SAVED {phase} {repo.name}")
    return True


def _archive(cfg: Config, repo: Path, phase: str, log: Log) -> bool:
    """Keep ``swarm/<phase>`` under :data:`ATTIC` if it holds commits the repo's
    main lacks. Caller holds the repo lock.

    True when deleting the branch now loses nothing (archived, or nothing to
    archive); False when the attic ref could not be written.
    """
    branch = f"swarm/{phase}"
    if not _branch_exists(repo, branch):
        return True
    main = _repo_main(cfg, repo)
    if not _unmerged(repo, main, branch):
        return True
    tip = _git(repo, "rev-parse", "--verify", "--quiet", branch, check=False).stdout.strip()
    stamp = time.strftime(ATTIC_STAMP, time.gmtime())
    ref, n = f"{ATTIC}/{phase}/{stamp}", 1
    while _ref_exists(repo, ref):
        n += 1
        ref = f"{ATTIC}/{phase}/{stamp}-{n}"
    made = _git(repo, "update-ref", ref, tip, check=False) if tip else None
    if made is None or made.returncode != 0:
        why = made.stderr.strip() if made is not None else "no tip"
        log.line(f"ATTIC-FAILED {phase} {repo.name}: {why}")
        return False
    log.line(f"ATTIC {phase} {repo.name} {ref} (not on {main})")
    return True


def _gc(cfg: Config, repo: Path, phase: str, log: Log) -> None:
    """Remove one repo's ``swarm/<phase>`` worktree then delete its branch.

    Caller holds the repo lock. Uniform across umbrella and components (every
    repo has a per-phase worktree now). Work not on main is saved first
    (:func:`_save_wip`, :func:`_archive`); if it cannot be, nothing is removed.

    **Never raises.** This is housekeeping that runs *after* a merge has already
    succeeded, so its failure must not fail the integration: a `worktree remove`
    that timed out once turned a merged, pushed phase into a blocked, dirty
    integration holding the whole queue. `check=False` was not enough: a timeout
    raises regardless of `check`. A leftover worktree is harmless and is
    reconciled on the next `swarm up`; a blocked queue is not.
    """
    branch = f"swarm/{phase}"
    wt = _wt_for(cfg, repo, phase)
    try:
        if not (_save_wip(cfg, repo, phase, log) and _archive(cfg, repo, phase, log)):
            log.line(f"WORKTREE-GC-KEPT {phase} {repo.name}: its work could not be saved")
            return
        if wt.exists():
            _git(
                repo,
                "worktree",
                "remove",
                "--force",
                str(wt),
                check=False,
                timeout=_GC_TIMEOUT_S,
            )
        _git(repo, "worktree", "prune", check=False, timeout=_GC_TIMEOUT_S)
        if _branch_exists(repo, branch):
            _git(repo, "branch", "-D", branch, check=False)
    except GitError as exc:
        # Loud in the log, invisible to the queue.
        log.line(f"WORKTREE-GC-FAILED {phase} {repo.name}: {exc}")


def _mirror_dir(cfg: Config, phase: str) -> Path | None:
    """``wt/<phase>``, or None for a name that would reach outside :attr:`wt_dir`
    (``..``, ``.``, a slash): the mirror dir is ``rmtree``'d."""
    if cfg.session_tmp(phase) is None:  # the same name rule
        return None
    path = cfg.wt_dir / phase
    try:
        inside = path.resolve().parent == cfg.wt_dir.resolve()
    except OSError:
        return None
    return path if inside else None


def _rmtree_mirror(cfg: Config, phase: str) -> None:
    """Best-effort remove the phase's umbrella worktree directory shell (any
    empty nesting dirs a component worktree left behind) and the session's
    ``TMPDIR`` (``launch.tmp_env``) — both end when the mirror's work has landed
    or been dropped, which is exactly when this runs (merged or discarded).

    A shell that still holds a checkout is left alone: its removal was refused
    because the work in it could not be saved."""
    path = _mirror_dir(cfg, phase)
    if path is not None and not any(
        _is_worktree(_wt_for(cfg, repo, phase)) for repo, _m in _repos(cfg)
    ):
        shutil.rmtree(path, ignore_errors=True)
    tmp = cfg.session_tmp(phase)
    if tmp is not None:
        shutil.rmtree(tmp, ignore_errors=True)


def discard(cfg: Config, phase: str, log: Log) -> None:
    """Drop a phase's mirror with NO merge (a failed build, a phase recorded
    done elsewhere). Components first (nested), umbrella last, then the mirror
    dir. Work not on main is archived first (:func:`_gc`), never lost."""
    if _mirror_dir(cfg, phase) is None:
        log.line(f"WORKTREE-DISCARD-REFUSED {phase!r} not a mirror name")
        return
    for repo, _main in _repos(cfg):
        with repo_lock(cfg, repo):
            _gc(cfg, repo, phase, log)
    _rmtree_mirror(cfg, phase)
    log.line(f"WORKTREE-DISCARD {phase}")


def set_aside(cfg: Config, phase: str, log: Log) -> bool:
    """Keep an unfinished phase's mirror for its next attempt. True when kept.

    Uncommitted edits become a commit on the branch, and the worktrees and
    branches stay, so the next :func:`worktree_add` for the phase resumes on
    them. A mirror with nothing in it is discarded instead: keeping it would
    only hand the next attempt a stale base.
    """
    saved = True
    for repo, _main in _repos(cfg):
        with repo_lock(cfg, repo):
            saved = _save_wip(cfg, repo, phase, log) and saved
    if saved and _mirror_empty(cfg, phase):
        discard(cfg, phase, log)
        return False
    log.line(f"WORKTREE-KEPT {phase}")
    return True


def attic_refs(repo: Path) -> list[tuple[str, float]]:
    """Every :data:`ATTIC` ref in ``repo`` with the UTC time it was made (from
    its name). A name that does not parse is left out, so it is never pruned."""
    out = _git(repo, "for-each-ref", "--format=%(refname)", ATTIC, check=False)
    refs: list[tuple[str, float]] = []
    for ref in out.stdout.split():
        stamp = ref.rsplit("/", 1)[-1].split("-", 1)[0]
        try:
            made = float(calendar.timegm(time.strptime(stamp, ATTIC_STAMP)))
        except ValueError:
            continue
        refs.append((ref, made))
    return refs


# -- worktree creation ----------------------------------------------------
def _link_target_cache(cfg: Config, wt: Path, repo: Path, log: Log) -> None:
    """Point a Rust worktree's ``target/`` at a shared, per-repo cache dir.

    Isolated worktrees otherwise each recompile the whole dependency graph from
    scratch. Symlinking ``<worktree>/target`` to one shared dir per repo lets
    ``cargo`` reuse unchanged crates across phases (only changed crates rebuild) —
    the single biggest cause of the parallel-build memory/CPU blow-up. A symlink
    (not ``CARGO_TARGET_DIR``) is used so gate scripts that read a *relative*
    ``target/release/<bin>`` still resolve.

    We create the link ONLY when the repo actually gitignores ``target`` (checked
    with ``git check-ignore``): that keeps the machine-local, absolute symlink out
    of the tree, the integration DIRTY check, and any ``git add -A`` — so it can
    never be committed onto ``swarm/<phase>`` and merged into canonical ``main``.
    Best-effort otherwise: a failure/decline just means a cold (still-correct) build.

    Trade-off: two phases building the *same* repo concurrently share one mutable
    ``target/``; ``cargo``'s build-dir lock serializes them and their divergent
    sources thrash each other's fingerprints. In practice the ledger's deps keep
    same-repo phases from running at once, so this is a net win; cross-repo phases
    (the common parallel case) use separate caches and don't interact.
    """
    if not cfg.build_cache or not (wt / "Cargo.toml").exists():
        return
    link = wt / "target"
    if link.exists() or link.is_symlink():
        return  # a fresh worktree shouldn't have one; don't clobber if it does
    if _git(wt, "check-ignore", "-q", "target", check=False).returncode != 0:
        # `target` isn't ignored here — linking would risk committing the symlink.
        log.line(f"TARGET-CACHE-SKIP {repo.name} target-not-ignored")
        return
    shared = cfg.build_cache_dir / _slug(repo)
    try:
        shared.mkdir(parents=True, exist_ok=True)
        link.symlink_to(shared)
    except OSError as exc:
        log.line(f"TARGET-CACHE-SKIP {repo.name} {exc}")


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


#: How many component worktrees :func:`worktree_add` builds at once. Each one is
#: a ``fetch`` plus a checkout, mostly waiting on the network and the disk, so a
#: serial walk over many repos is slow on every launch. Bounded rather than one
#: thread per repo because the checkouts do contend for the same disk.
WORKTREE_ADD_WORKERS = 6


def _holds_work(cfg: Config, repo: Path, phase: str, base: str) -> bool:
    """``swarm/<phase>`` in ``repo`` has commits past ``base``, or its worktree
    has edits: an earlier attempt's work, which the next attempt resumes."""
    if _unmerged(repo, base, f"swarm/{phase}"):
        return True
    wt = _wt_for(cfg, repo, phase)
    return _is_worktree(wt) and _wt_changes(cfg, repo, wt) != ""


def _resume_one(cfg: Config, repo: Path, phase: str, log: Log) -> None:
    """Put the existing ``swarm/<phase>`` back in its worktree (or keep the one
    that is there), so an earlier attempt's commits are where the worker starts."""
    branch = f"swarm/{phase}"
    wt = _wt_for(cfg, repo, phase)
    if _is_worktree(wt):
        head = _git(wt, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
        if head != branch:
            raise GitError(f"{wt} holds work but is on {head or 'a detached HEAD'}, not {branch}")
    else:
        _git(repo, "worktree", "prune", check=False)
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", str(wt), branch)
    _link_target_cache(cfg, wt, repo, log)
    log.line(f"WORKTREE-RESUME {phase} {repo.name}")


def _add_one(cfg: Config, repo: Path, main: str, phase: str, log: Log) -> None:
    """Create one repo's ``swarm/<phase>`` worktree under its own repo lock, or
    resume the one an earlier attempt left holding work."""
    branch = f"swarm/{phase}"
    wt = _wt_for(cfg, repo, phase)
    with repo_lock(cfg, repo):
        base = _mirror_base(repo, main)
        if _branch_exists(repo, branch) and _holds_work(cfg, repo, phase, base):
            _resume_one(cfg, repo, phase, log)
            return
        if _branch_exists(repo, branch) or wt.exists():
            _gc(cfg, repo, phase, log)  # empty leftover -> start from a fresh base
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", str(wt), "-b", branch, base)
        _link_target_cache(cfg, wt, repo, log)


def worktree_add(cfg: Config, phase: str, log: Log) -> Path:
    """Build the phase's full-workspace mirror; return the umbrella worktree
    (the worker's cwd).

    The umbrella worktree is created first (its directory must exist before
    component worktrees nest inside it), then the components, up to
    :data:`WORKTREE_ADD_WORKERS` at a time. Components are added one nesting
    depth at a time, so a repo configured *inside* another component's path never
    races its parent's checkout. Each branches off :func:`_mirror_base` — local
    ``main`` unless ``origin/<main>`` strictly fast-forwards it — so unpushed
    owner commits are never dropped from the mirror. A leftover from a prior
    attempt that holds work is resumed as it is; an empty one is GC'd first.

    All or nothing: if any repo fails, the phase's mirror is set aside
    (:func:`set_aside`: discarded unless an earlier attempt's work is in it) and
    the first failure is raised as :class:`GitError`. A half mirror is worse
    than none — a worker started in it would find some repos missing.
    """
    root = cfg.project_dir.resolve()
    components = [(r, m) for (r, m) in _repos(cfg) if r.resolve() != root]
    cfg.wt_dir.mkdir(parents=True, exist_ok=True)
    try:
        _add_one(cfg, cfg.project_dir, cfg.git_main_branch, phase, log)
        by_depth: dict[int, list[tuple[Path, str]]] = {}
        for repo, main in components:
            depth = len(repo.resolve().relative_to(root).parts)
            by_depth.setdefault(depth, []).append((repo, main))
        for depth in sorted(by_depth):
            batch = by_depth[depth]
            with ThreadPoolExecutor(max_workers=min(WORKTREE_ADD_WORKERS, len(batch))) as pool:
                futures = [pool.submit(_add_one, cfg, r, m, phase, log) for r, m in batch]
            for fut in futures:
                fut.result()  # re-raise the first failure, after all have settled
    except (GitError, OSError) as exc:
        log.line(f"WORKTREE-ADD-FAIL {phase} {exc}")
        set_aside(cfg, phase, log)
        if isinstance(exc, GitError):
            raise
        raise GitError(f"worktree add {phase}: {exc}") from exc
    log.line(f"WORKTREE-ADD {phase} {cfg.wt_dir / phase}")
    return cfg.wt_dir / phase


# -- push (optimistic, merge-based reconcile — never rebases a merge) ------
@dataclass(frozen=True)
class PushResult:
    """One push of a repo's main: how it ended, and in the push's own words why.

    ``refused`` separates a push something *decided* against — a pre-push hook
    (or a server-side one) said no — from one that could not happen at all
    (unreachable remote, auth, a timeout). Both leave the merge on local main;
    they differ in what the owner has to fix, so the reason travels with them.
    """

    status: str  # MERGED | CONFLICT | PUSH_FAILED
    reason: str = ""
    refused: bool = False


#: The tail of a push's own output worth showing the owner; git's
#: ``error: failed to push some refs`` and its hints say nothing a person needs.
_REASON_LINES = 3
_REASON_CHARS = 300


def _non_ff(err: str) -> bool:
    """A genuine non-fast-forward rejection, the only failure a merge can fix.

    Git reports it as ``! [rejected] main -> main (fetch first)`` or ``(non-
    fast-forward)``. A LOCAL pre-push hook that exits non-zero prints its own
    output and then the same ``error: failed to push some refs`` line — with NO
    ``[rejected]`` line (verified against git 2.53) — so matching on that error
    line, as this used to, read every hook refusal as a race with another pusher
    and fetched, merged and re-ran the hook five times for nothing.
    """
    return any(
        "[rejected]" in ln and ("non-fast-forward" in ln or "fetch first" in ln)
        for ln in err.splitlines()
    )


def _push_reason(out: str, err: str) -> str:
    """The last few meaningful lines of a failed push, capped for a phone.

    A hook's own stdout is preferred: that is where a check says *what* failed
    (a repo's gate: ``node_modules/dep is 0.16.0 but package.json pins
    0.17.0``), while its stderr tends to be the generic ``FAILED — the push was
    refused``. Git's own ``error:``/``hint:``/``To <url>`` lines are dropped.
    """

    def meaningful(text: str) -> list[str]:
        keep: list[str] = []
        for ln in (text or "").splitlines():
            ln = ln.strip()
            if ln.startswith("error:") and "failed to push" in ln:
                break  # git's own summary; everything after it is hints
            if not ln or ln.startswith(("hint:", "To ", "== ")):
                continue
            keep.append(ln)
        return keep

    lines = meaningful(out) or meaningful(err)
    reason = " ".join(lines[-_REASON_LINES:]) or (err or out or "").strip()
    if len(reason) > _REASON_CHARS:
        reason = reason[: _REASON_CHARS - 3].rstrip() + "..."
    return reason or "push failed with no output"


def _push_result(
    repo: Path, main: str, log: Log, *, reconcile: bool = True, abort_conflict: bool = False
) -> PushResult:
    """Push local ``main``; on a non-ff rejection, MERGE origin/main in and retry.

    Reconciling by **merge** (never rebase) means a concurrent external commit is
    integrated without flattening our merge commit, and a reconcile that itself
    conflicts leaves a *real* mid-merge state a resolver can finish — instead of a
    clean-tree wedge. ``abort_conflict`` is for a push that no phase owns (an
    owed push being retried): there is nobody to hold, so the merge is backed
    out and reported as a failure instead. ``reconcile=False`` pushes without
    ever merging, for a repo not sitting cleanly on ``main``.

    Only a genuine non-ff rejection is retried (:func:`_non_ff`). A hook refusal
    returns at once with the hook's reason: re-running a check that just failed
    against the same commits cannot pass. Anything else (unreachable remote,
    auth, a timeout) is :data:`PUSH_FAILED`, never a :class:`GitError`, so no
    push can hold the merge queue.
    """
    if not _has_remote(repo):
        return PushResult(MERGED)
    for attempt in range(1, _PUSH_ATTEMPTS + 1):
        try:
            push = _git(repo, "push", "origin", main, check=False)
        except GitError as exc:  # a timeout: a hung remote, or a hook that runs long
            log.line(f"PUSH-FAIL {main} {exc}")
            return PushResult(PUSH_FAILED, _push_reason("", str(exc)))
        if push.returncode == 0:
            return PushResult(MERGED)
        err = push.stderr or ""
        reason = _push_reason(push.stdout or "", err)
        if not _non_ff(err):
            refused = "failed to push some refs" in err
            log.line(f"PUSH-{'REFUSED' if refused else 'FAIL'} {main} {reason}")
            return PushResult(PUSH_FAILED, reason, refused=refused)
        if not reconcile:
            log.line(f"PUSH-FAIL {main} non-ff, not on a clean {main} to merge origin")
            return PushResult(
                PUSH_FAILED,
                f"origin/{main} has moved on and the repo is not on a clean {main}"
                " to merge it into",
            )
        _git(repo, "fetch", "origin", check=False)
        if _ref_exists(repo, f"origin/{main}"):
            merge = _git(repo, "merge", "--no-edit", f"origin/{main}", check=False)
            if merge.returncode != 0:
                log.line(f"PUSH-RECONCILE-CONFLICT {main}")
                if abort_conflict:
                    _git(repo, "merge", "--abort", check=False)
                    return PushResult(
                        PUSH_FAILED, f"merging origin/{main} conflicts; reconcile by hand"
                    )
                return PushResult(CONFLICT)
        log.line(f"PUSH-RETRY {attempt} {main}")
    return PushResult(PUSH_FAILED, f"still rejected after {_PUSH_ATTEMPTS} merge-and-retry rounds")


def _push(repo: Path, main: str, log: Log) -> str:
    """:func:`_push_result`'s status alone — the shape older callers expect."""
    return _push_result(repo, main, log).status


def push_with_retry(repo: Path, main: str, log: Log) -> bool:
    """Compat wrapper: push ``main`` with optimistic merge-reconcile. True == pushed."""
    return _push(repo, main, log) == MERGED


def _push_owed(repo: Path, main: str, log: Log) -> PushResult:
    """Push a ``main`` that is ahead of origin with no phase to hold. Never raises.

    Caller holds the repo lock. A merge-reconcile is only attempted when the repo
    sits cleanly on ``main`` — the owner may be mid-fix in exactly this checkout
    (fixing the check that refused the last push), and that tree must never be
    merged into or held as DIRTY on behalf of a phase that did not touch it.
    """
    try:
        _git(repo, "fetch", "origin", check=False)
        if not _local_ahead_of_origin(repo, main):
            return PushResult(MERGED)  # already on origin (pushed by hand, or level)
        clean = (
            _current_branch(repo) == main
            and not _merge_in_progress(repo)
            and not _rebase_in_progress(repo)
            and not _dirty(repo)
        )
        return _push_result(repo, main, log, reconcile=clean, abort_conflict=True)
    except GitError as exc:
        return PushResult(PUSH_FAILED, _push_reason("", str(exc)))


def retry_push(cfg: Config, repo: Path, log: Log) -> PushResult:
    """Retry an owed push of ``repo``'s main (see :func:`_push_owed`).

    :data:`MERGED` means origin now has everything local main has — whether this
    push did it or someone pushed by hand — so the debt is settled either way.
    """
    try:
        main = _repo_main(cfg, repo)
        with repo_lock(cfg, repo):
            return _push_owed(repo, main, log)
    except (GitError, OSError) as exc:
        return PushResult(PUSH_FAILED, _push_reason("", str(exc)))


# -- integration ----------------------------------------------------------
def _auto_resolve(cfg: Config, repo: Path, phase: str, log: Log) -> bool:
    """Try to settle a failed merge mechanically. True = resolved and committed.

    Runs inside the caller's per-repo flock, on the real mid-merge tree, so a
    refusal costs nothing: the index is left exactly as the failed merge left it
    and the resolver session takes over unchanged.

    All-or-nothing on purpose. If ANY conflicted file has no configured strategy,
    or a strategy declines (both sides edited the same record, both reordered),
    nothing is staged and this returns False. A partial mechanical resolve would
    hand the resolver a half-fixed tree, which is worse than handing it the
    original.

    Every conflict these campaigns produced was of exactly two shapes -- two
    phases ticking their own adjacent one-line ledger entry, and two phases
    prepending a dated block to a journal -- and both are settled here for free.
    Repeated resolver sessions burn many tokens to do that.
    """
    strategies = getattr(cfg, "git_auto_resolve", {}) or {}
    if not strategies:
        return False
    unmerged = _git(repo, "diff", "--name-only", "--diff-filter=U", check=False)
    paths = [p for p in unmerged.stdout.splitlines() if p.strip()]
    if not paths:
        return False
    # Settle every file in memory first: a decline on a later file must find the
    # earlier ones still exactly as the failed merge left them on disk.
    resolved: dict[str, str] = {}
    for rel in paths:
        how = automerge.strategy_for(rel, strategies)
        if how is None:
            log.line(f"AUTORESOLVE-SKIP {phase} {repo.name} {rel} no-strategy")
            return False
        stages = {}
        for num in (1, 2, 3):  # base, ours, theirs
            got = _git(repo, "show", f":{num}:{rel}", check=False)
            if got.returncode != 0:
                log.line(f"AUTORESOLVE-SKIP {phase} {repo.name} {rel} stage{num}-missing")
                return False
            stages[num] = got.stdout
        merged = automerge.resolve_text(stages[1], stages[2], stages[3], how)
        if merged is None:
            log.line(f"AUTORESOLVE-DECLINED {phase} {repo.name} {rel} {how}")
            return False
        resolved[rel] = merged
    staged: list[str] = []
    for rel, merged in resolved.items():
        try:
            (repo / rel).write_text(merged, encoding="utf-8")
        except OSError as exc:
            log.line(f"AUTORESOLVE-WRITE-FAIL {phase} {repo.name} {rel} {exc}")
            return False
        staged.append(rel)
    for rel in staged:
        _git(repo, "add", "--", rel)
    committed = _git(repo, "commit", "--no-edit", check=False)
    if committed.returncode != 0:
        log.line(f"AUTORESOLVE-COMMIT-FAIL {phase} {repo.name}")
        return False
    # Name every file the merge touched, not just the ones repaired. The one
    # thing a resolver session caught that no merge driver can was a file that
    # merged CLEANLY and was thereafter semantically false; that class stays
    # visible only if the whole merge is on the record.
    # --diff-merges=first-parent: a merge commit lists no files under a plain
    # --name-only, which silently emptied this line and defeated the whole point.
    touched = _git(
        repo, "log", "-1", "--name-only", "--format=",
        "--diff-merges=first-parent", check=False,
    )
    files = " ".join(t for t in touched.stdout.split() if t)[:400]
    log.line(f"INTEGRATE-AUTORESOLVED {phase} {repo.name} fixed={','.join(staged)}")
    log.line(f"AUTORESOLVE-TOUCHED {phase} {repo.name} {files}")
    return True


def _merge_failed(repo: Path, phase: str, tag: str, log: Log) -> str:
    """Classify a merge that failed and was not auto-resolved. A real conflict
    leaves a mid-merge for a resolver; a merge git refused to even start (an
    untracked file in the owner's checkout it would overwrite) leaves nothing to
    resolve, so it holds like a dirty tree."""
    if _merge_in_progress(repo):
        log.line(f"{tag} {phase} {repo.name}")
        return CONFLICT
    log.line(f"INTEGRATE-REFUSED {phase} {repo.name} files in the way")
    return DIRTY


def _integrate_one(
    cfg: Config,
    repo: Path,
    main: str,
    phase: str,
    log: Log,
    pushes: dict[Path, PushResult] | None = None,
) -> str:
    """Land ``swarm/<phase>`` into ``main`` for a single repo. Serialized.

    The merge happens in the owner's checkout, which is never switched to
    another branch: one that is not on ``main`` holds as :data:`DIRTY`.

    Untouched-by-this-phase repos are the common case in a big workspace: the
    branch is 0 commits ahead of a not-behind main, so they are pruned with **no
    network** (no fetch/merge/push). Only repos the phase actually changed do the
    full merge+push. Idempotent/resumable: a dirty canonical tree is held
    (:data:`DIRTY`), a merge/push conflict returns :data:`CONFLICT` leaving a
    resolvable mid-merge, a pure push failure returns :data:`PUSH_FAILED`.

    With ``pushes`` given, every push attempted is recorded there and a push
    failure is NOT a failure of the integration: the merge is already on local
    main, which is what every later worker branches from, so the repo is pruned
    and reported merged with its push owed. Holding the queue for it instead
    froze every later phase behind a check nobody downstream needed (a repo's
    pre-push gate that runs for hours while no worker launches). An untouched repo
    that is merely ahead of origin — an owed push, or an owner commit — gets a
    push attempt that can never hold (:func:`_push_owed`), rather than the full
    path's DIRTY check against a tree the owner may be fixing.
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
        if pushes is not None and not (
            has_branch and _commits_ahead(repo, main, branch) > 0
        ):
            if has_branch:
                _gc(cfg, repo, phase, log)
            pushes[repo] = _push_owed(repo, main, log)
            return MERGED
        if _merge_in_progress(repo) or _rebase_in_progress(repo):
            return CONFLICT  # a prior op is still mid-resolution
        if _dirty(repo):
            log.line(f"INTEGRATE-DIRTY {phase} {repo.name}")
            return DIRTY
        other = _off_main(repo, main)
        if other is not None:
            # The owner's checkout, on a branch they chose: never switched for them.
            log.line(f"INTEGRATE-OFF-MAIN {phase} {repo.name} on={other}")
            return DIRTY
        if _has_remote(repo):
            _git(repo, "fetch", "origin", check=False)
            if _ref_exists(repo, f"origin/{main}") and _commits_ahead(
                repo, main, f"origin/{main}"
            ) > 0:
                base = _git(repo, "merge", "--no-edit", f"origin/{main}", check=False)
                if base.returncode != 0 and not _auto_resolve(cfg, repo, phase, log):
                    return _merge_failed(repo, phase, "INTEGRATE-BASE-CONFLICT", log)
        if has_branch and _commits_ahead(repo, main, branch) > 0:
            merge = _git(repo, "merge", "--no-ff", "--no-edit", branch, check=False)
            if merge.returncode != 0 and not _auto_resolve(cfg, repo, phase, log):
                return _merge_failed(repo, phase, "INTEGRATE-CONFLICT", log)
        if _has_remote(repo):
            pushed = _push_result(repo, main, log)
            if pushes is not None and pushed.status != CONFLICT:
                pushes[repo] = pushed
                if pushed.status != MERGED:
                    log.line(f"INTEGRATE-PUSH-OWED {phase} {repo.name} {pushed.reason}")
            elif pushed.status != MERGED:
                log.line(f"INTEGRATE-PUSH-{pushed.status.upper()} {phase} {repo.name}")
                return pushed.status
        _gc(cfg, repo, phase, log)
        log.line(f"INTEGRATE-MERGED {phase} {repo.name}")
        return MERGED


def integrate(
    cfg: Config, phase: str, log: Log, pushes: dict[Path, PushResult] | None = None
) -> str:
    """Land ``swarm/<phase>`` across every repo the phase mirrored, components
    first then umbrella. Returns :data:`MERGED` only when all repos are clean;
    the first non-merged repo's status short-circuits and is returned so the
    supervisor can hold the queue and (for a conflict) point a resolver at the
    exact repo (:func:`blocked_repo`).

    ``pushes`` (when given) collects every push this attempted, keyed by repo; a
    failed one there is an owed push, not a hold (see :func:`_integrate_one`).
    It is filled even when a later repo conflicts — the earlier repos' merges are
    on local main regardless."""
    for repo, main in _repos(cfg):
        result = _integrate_one(cfg, repo, main, phase, log, pushes)
        if result != MERGED:
            return result
    _rmtree_mirror(cfg, phase)  # all repos merged -> drop the empty mirror shell
    return MERGED


def blocked_repo(cfg: Config, phase: str) -> Path | None:
    """The repo currently mid-merge / mid-rebase / dirty / off main for
    ``phase``, if any. A :data:`CONFLICT` or :data:`DIRTY` leaves one; a
    :data:`PUSH_FAILED` does not."""
    for repo, main in _repos(cfg):
        if (
            _merge_in_progress(repo)
            or _rebase_in_progress(repo)
            or _dirty(repo)
            or _off_main(repo, main) is not None
        ):
            return repo
    return None


def off_main_reason(cfg: Config, repo: Path | None, phase: str) -> str | None:
    """Plain words for a hold caused by the owner's checkout sitting on another
    branch, or None when that is not why ``repo`` is held."""
    if repo is None:
        return None
    main = _repo_main(cfg, repo)
    other = _off_main(repo, main)
    if other is None:
        return None
    return (
        f"swarm: {phase} is finished but not merged -- the {repo.name} checkout is on"
        f" {other}, not {main}, and the swarm will not switch it for you. Switch it"
        f" back to {main} when you are ready, then run `swarm resolved {phase}`."
    )


def resolve_ready(cfg: Config, repo: Path) -> bool:
    """True once a held repo is safe to re-integrate: no merge/rebase in
    progress, a clean tree, and back on main. A premature ``swarm resolved``
    fails this."""
    if repo is None:
        return True
    with repo_lock(cfg, repo):
        return not (
            _merge_in_progress(repo)
            or _rebase_in_progress(repo)
            or _dirty(repo)
            or _off_main(repo, _repo_main(cfg, repo)) is not None
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
        if not phase or status not in statuses.ALL:
            continue
        if out.get(phase) in DONE_INTEGRATE:
            continue  # a completed build (ok/needs-owner) wins over fail/skip
        out[phase] = status
    return out


def _swarm_branches(repo: Path) -> list[str]:
    out = _git(
        repo, "branch", "--list", "swarm/*", "--format=%(refname:short)", check=False
    )
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def _mirror_empty(cfg: Config, phase: str) -> bool:
    """Nothing in ``swarm/<phase>`` to lose: in no repo does the branch hold a
    commit past main, nor its worktree an edit (tracked or untracked). A status
    that cannot be read counts as not empty."""
    branch = f"swarm/{phase}"
    for repo, main in _repos(cfg):
        if _branch_exists(repo, branch) and _unmerged(repo, main, branch):
            return False
        wt = _wt_for(cfg, repo, phase)
        if wt.is_dir():
            status = _git(wt, "status", "--porcelain", check=False)
            if status.returncode != 0 or status.stdout.strip():
                return False
    return True


def holds_work(cfg: Config, phase: str) -> bool:
    """Whether ``swarm/<phase>`` holds anything not on main, in any repo."""
    return not _mirror_empty(cfg, phase)


def _all_swarm_phases(cfg: Config) -> set[str]:
    phases: set[str] = set()
    for repo in [cfg.project_dir, *discovered_repos(cfg)]:
        for branch in _swarm_branches(repo):
            phases.add(branch[len("swarm/") :])
    return phases


@dataclass(frozen=True)
class Held:
    """A phase whose restart-time integration did NOT complete."""

    phase: str
    kind: str  # MERGED is impossible here: CONFLICT | DIRTY | PUSH_FAILED
    repo: Path | None  # the repo to clear (None for PUSH_FAILED)


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of :func:`reconcile`: what landed, and what is still stuck."""

    integrated: list[str] = field(default_factory=list)
    held: list[Held] = field(default_factory=list)
    #: Operator-job mirrors landed here. Kept apart from ``integrated`` because
    #: the caller marks every name there done, and a job is not a ledger phase.
    operator_integrated: list[str] = field(default_factory=list)
    # phase -> the pushes its integration attempted; a failed one is owed.
    pushes: dict[str, dict[Path, PushResult]] = field(default_factory=dict)


def reconcile(
    cfg: Config,
    done_phases: dict[str, str],
    log: Log,
    operator: dict[str, str] | None = None,
) -> ReconcileResult:
    """Reconcile leftover ``swarm/*`` branches at ``swarm up`` (sentinel-driven).

    A phase already recorded done is cleaned up (anything unmerged archived). A
    phase with a completed sentinel (``ok``/``needs-owner`` — worker finished,
    supervisor died before integrating) has its integration completed. A leftover
    branch with **no** completed sentinel was interrupted mid-build: it is set
    aside (:func:`set_aside`) with its uncommitted edits saved as a commit, left
    NOT done, and its next launch resumes on it. Never declared complete off
    branch topology.

    Reports the HELD phases as well as the integrated ones, because they are not
    the same thing and the caller cannot tell them apart from the integrated list
    alone. ``swarm up`` seeds ``done`` from the sentinels regardless of whether the
    integration actually completed, so a phase held here was recorded done while
    its branch was still unmerged — with nothing blocked, no resolver and no ping.
    The NEXT ``swarm up`` then sees it in ``done_phases``, takes the branch below,
    and ``discard``s the completed work outright. Surfacing the hold is what lets
    the caller block/park/notify instead of silently destroying it.

    ``operator`` (``{mirror: "keep" | "integrate"}``, from
    :func:`operator.mirror_plan`) covers the one kind of ``swarm/*`` branch that
    has no sentinel by design: an operator job's mirror. Without it every such
    mirror reads as an interrupted phase and is discarded — commits and all. A
    live job's mirror is kept for its next attempt; a finished job's is landed.
    """
    sentinels = sentinel_done(cfg)
    operator = operator or {}
    integrated: list[str] = []
    operator_integrated: list[str] = []
    held: list[Held] = []
    pushes: dict[str, dict[Path, PushResult]] = {}
    for phase in sorted(_all_swarm_phases(cfg)):
        job = operator.get(phase)
        if job == "keep" and _mirror_empty(cfg, phase):
            # Kept so a retry finds the commits an attempt already made; with
            # none, keeping it only hands the next attempt a stale base.
            discard(cfg, phase, log)
            log.line(f"RECONCILE-DISCARD {phase} empty operator mirror")
        elif job == "keep":
            log.line(f"RECONCILE-KEEP {phase} operator-job")
        elif phase in done_phases:
            discard(cfg, phase, log)
            log.line(f"RECONCILE-GC {phase} already-recorded")
        elif job == "integrate" or sentinels.get(phase) in DONE_INTEGRATE:
            pushes[phase] = {}
            result = integrate(cfg, phase, log, pushes[phase])
            if result == MERGED:
                (operator_integrated if job else integrated).append(phase)
                log.line(f"RECONCILE-INTEGRATED {phase}")
            else:
                repo = blocked_repo(cfg, phase) if result in (CONFLICT, DIRTY) else None
                held.append(Held(phase=phase, kind=result, repo=repo))
                log.line(f"RECONCILE-HELD {phase} {result} repo={repo}")
        elif set_aside(cfg, phase, log):
            log.line(f"RECONCILE-KEEP {phase} interrupted; its next launch resumes it")
        else:
            log.line(f"RECONCILE-DISCARD {phase} interrupted with nothing to keep")
    return ReconcileResult(
        integrated=integrated,
        held=held,
        pushes={k: v for k, v in pushes.items() if v},
        operator_integrated=operator_integrated,
    )


def reconcile_orphans(cfg: Config, done_phases: dict[str, str], log: Log) -> list[str]:
    """The integrated phases only — :func:`reconcile`'s original return shape,
    kept for callers that do not (yet) act on held phases."""
    return reconcile(cfg, done_phases, log).integrated
