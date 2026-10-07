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
actually changed into its main and pushes, after the repo's ``[git].post_merge``
command if it has one; repos it didn't touch are 0 commits ahead and are dropped
without any network. On ``fail`` (or a launch failure) all
of the phase's worktrees and branches are removed with no merge — a clean
rollback. Every repo mutation is serialized by an ``flock`` keyed per repo.

Nothing a worker made is ever destroyed. Before a worktree or branch holding
work that is not on main goes, its uncommitted edits are committed onto the
branch and the tip is kept under :data:`ATTIC` (``swarm gc`` prunes old ones).
An interrupted phase is not removed at all: :func:`set_aside` keeps its mirror
and its next launch resumes on the same branch. A phase that finished ``later``
(it waits for a date) has its tip kept under :data:`LATER` instead of the attic
(:func:`keep_later`), and its next launch starts with that work merged onto the
day's main.

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
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import automerge
from . import repocmd
from . import statuses
from .config import Config
from .logutil import Log

MERGED = "merged"
CONFLICT = "conflict"
DIRTY = "dirty"
PUSH_FAILED = "push_failed"
# With ``[lanes] enabled`` only (:mod:`landing`): a check runs, another
# phase's landing holds a repo, or the phase's worktree could not be made ready
# for its check -- the queue lands others meanwhile; or a resolver's job on the
# phase's own worktree, never on the owner's checkout.
LANE_CHECKING = "lane-checking"
LANE_WAITING = "lane-waiting"
LANE_UNPREPARED = "lane-unprepared"
LANE_CONFLICT = "lane-conflict"
LANE_RED = "lane-red"
LANE_PENDING = (LANE_CHECKING, LANE_WAITING, LANE_UNPREPARED)
LANE_HOLDS = (LANE_CONFLICT, LANE_RED)

# `swarm done` completion statuses that INTEGRATE (merge into main) rather than
# roll back. The owner-facing ones land exactly like ``ok``; they differ only in
# that the owner gets told (done worker-side in :func:`launch.done`). ``fail``
# (anything else) rolls back with no merge.
DONE_INTEGRATE = statuses.INTEGRATES

_GIT_TIMEOUT_S = 120.0
_CHECK_TIMEOUT_S = 300.0  # an [git].auto_resolve_check run; it holds the repo lock
_PUSH_ATTEMPTS = 5
#: A push the remote turned away after the hook passed (a lock race with another
#: pusher) is pushed again this many times, this far apart, before it is owed.
_REMOTE_RETRIES = 2
_REMOTE_RETRY_PAUSE_S = 1.0
#: The fetch before an owed push runs in the ledger writer's turn, so every
#: write behind it waits for it: it is given this long, twice.
_OWED_FETCH_TIMEOUT_S = 30.0

#: Where work is kept instead of deleted: ``refs/swarm-attic/<phase>/<utc-stamp>``.
#: Not a branch, so reconcile, launch and branch listings never see it.
ATTIC = "refs/swarm-attic"
ATTIC_STAMP = "%Y%m%dT%H%M%SZ"
#: Where the work of a phase that finished ``later`` waits for its date:
#: ``refs/swarm-later/<phase>``, one per repo. Its next launch merges it into
#: the new branch and removes it, so ``swarm gc`` never prunes it.
LATER = "refs/swarm-later"
_LATER_MESSAGE = "swarm: {phase}'s work from before its date, brought onto {main}"
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
    nested worktrees before their parent. A component with a ``[git].post_merge``
    command comes after those without one: what the command installs is mostly a
    sibling's, and a phase that changed both has then landed the sibling first."""
    found = discovered_repos(cfg)
    if cfg.git_post_merge:
        found.sort(key=lambda r: bool(repocmd.command(cfg, cfg.git_post_merge, r)))
    repos = [(r, _repo_main(cfg, r)) for r in found]
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
    return _to_attic(repo, phase, tip, log, f"not on {main}")


def _to_attic(repo: Path, phase: str, tip: str, log: Log, why: str) -> bool:
    """Write ``tip`` under :data:`ATTIC` for ``phase``; False when git refused."""
    stamp = time.strftime(ATTIC_STAMP, time.gmtime())
    ref, n = f"{ATTIC}/{phase}/{stamp}", 1
    while _ref_exists(repo, ref):
        n += 1
        ref = f"{ATTIC}/{phase}/{stamp}-{n}"
    made = _git(repo, "update-ref", ref, tip, check=False) if tip else None
    if made is None or made.returncode != 0:
        reason = made.stderr.strip() if made is not None else "no tip"
        log.line(f"ATTIC-FAILED {phase} {repo.name}: {reason}")
        return False
    log.line(f"ATTIC {phase} {repo.name} {ref} ({why})")
    return True


def later_ref(phase: str) -> str:
    """The ref a ``later`` finish of ``phase`` is kept under, in each repo."""
    return f"{LATER}/{phase}"


def _tip(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()


def _contains(repo: Path, tip: str, commit: str) -> bool:
    """``commit`` is ``tip`` or one of its ancestors."""
    return _git(repo, "merge-base", "--is-ancestor", commit, tip, check=False).returncode == 0


def _keep_later(cfg: Config, repo: Path, phase: str, log: Log) -> bool:
    """Move ``swarm/<phase>``'s tip to :func:`later_ref` if it holds commits the
    repo's main lacks. Caller holds the repo lock. True when deleting the branch
    now loses nothing. Work an earlier ``later`` left there and this tip does not
    contain goes to the attic first."""
    branch, ref = f"swarm/{phase}", later_ref(phase)
    main = _repo_main(cfg, repo)
    if not _branch_exists(repo, branch) or not _unmerged(repo, main, branch):
        return True
    tip, old = _tip(repo, branch), _tip(repo, ref)
    if old and not _contains(repo, tip, old) and not _to_attic(
        repo, phase, old, log, "an earlier `later` finish"
    ):
        return False
    made = _git(repo, "update-ref", ref, tip, check=False) if tip else None
    if made is None or made.returncode != 0:
        why = made.stderr.strip() if made is not None else "no tip"
        log.line(f"LATER-KEEP-FAILED {phase} {repo.name}: {why}")
        return False
    log.line(f"LATER-KEPT {phase} {repo.name} {ref} (not on {main})")
    return True


def _restore_later(cfg: Config, repo: Path, main: str, phase: str, log: Log) -> None:
    """Bring the work a ``later`` finish left under :func:`later_ref` into the
    phase's fresh worktree, then drop the ref. Caller holds the repo lock.

    The branch was just made from today's main, so the work is merged into it:
    the worker starts with what it committed *and* everything that landed since.
    If the two no longer merge cleanly the branch is put back exactly where the
    work left it, like an interrupted attempt that is resumed, and the landing
    meets the conflict the way it does for any branch that fell behind. Either
    way the work is on ``swarm/<phase>`` afterwards, which the usual rules keep.
    """
    ref = later_ref(phase)
    tip = _tip(repo, ref)
    if not tip:
        return
    wt = _wt_for(cfg, repo, phase)
    merged = _git(
        wt, "merge", "--no-edit", "--no-verify", "-q",
        "-m", _LATER_MESSAGE.format(phase=phase, main=main), tip, check=False,
    )
    if merged.returncode == 0:
        how = f"onto {main}"
    else:
        _git(wt, "merge", "--abort", check=False)
        _git(wt, "reset", "--hard", "-q", tip)
        how = f"as it was: it no longer merges onto {main} cleanly"
    _git(repo, "update-ref", "-d", ref, check=False)
    log.line(f"LATER-RESTORED {phase} {repo.name} {how}")


def _gc(cfg: Config, repo: Path, phase: str, log: Log, *, later: bool = False) -> None:
    """Remove one repo's ``swarm/<phase>`` worktree then delete its branch.

    Caller holds the repo lock. Uniform across umbrella and components (every
    repo has a per-phase worktree now). Work not on main is saved first
    (:func:`_save_wip`, then :func:`_archive`, or :func:`_keep_later` when
    ``later``); if it cannot be, nothing is removed.

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
        keep = _keep_later if later else _archive
        if not (_save_wip(cfg, repo, phase, log) and keep(cfg, repo, phase, log)):
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
        if cfg.build_cache:
            # What this worktree built into the shared cache must not outlive it
            # pointing back here: other worktrees are running from that cache now.
            _drop_stale_executables(cfg.build_cache_dir / _slug(repo), repo, log)
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


def keep_later(cfg: Config, phase: str, log: Log) -> None:
    """Drop the mirror of a phase that finished ``later``, keeping its work for
    its date: like :func:`discard`, except that what main lacks goes under
    :func:`later_ref` (uncommitted edits as a commit first), where the phase's
    next :func:`worktree_add` finds it."""
    if _mirror_dir(cfg, phase) is None:
        log.line(f"WORKTREE-DISCARD-REFUSED {phase!r} not a mirror name")
        return
    for repo, _main in _repos(cfg):
        with repo_lock(cfg, repo):
            _gc(cfg, repo, phase, log, later=True)
    _rmtree_mirror(cfg, phase)
    log.line(f"WORKTREE-KEPT-FOR-DATE {phase}")


def kept_later(cfg: Config) -> set[str]:
    """Phases with work under :data:`LATER` in any repo."""
    phases: set[str] = set()
    for repo, _main in _repos(cfg):
        out = _git(repo, "for-each-ref", "--format=%(refname)", LATER, check=False)
        phases |= {ref[len(LATER) + 1:] for ref in out.stdout.split()}
    return phases


def unkeep_later(cfg: Config, phase: str, log: Log) -> bool:
    """A row closed some other way no longer waits for its kept work: it goes
    to the attic, so a rerun of the row never starts from it. Which rows those
    are is the caller's to say (:func:`reconcile` for a phase recorded done,
    :func:`ledgerw.release_kept` for a row closed in the ledger). True when
    nothing is kept for the phase any more; a ref the attic refused stays."""
    gone = True
    for repo, main in _repos(cfg):
        with repo_lock(cfg, repo):
            tip = _tip(repo, later_ref(phase))
            if not tip:
                continue
            if _contains(repo, main, tip) or _to_attic(repo, phase, tip, log, "its row is closed"):
                _git(repo, "update-ref", "-d", later_ref(phase), check=False)
            else:
                gone = False
    return gone


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


def shelve(cfg: Config, phase: str, log: Log) -> None:
    """Move what a phase's worktrees have not committed to the attic, and leave
    them clean on their branch: a batch whose session died lands the rows it
    reported, never the half-made one it was on. Kept as a commit under
    :data:`ATTIC` (``swarm: unfinished work``), as :func:`set_aside` would."""
    for repo, _main in _repos(cfg):
        with repo_lock(cfg, repo):
            wt = _wt_for(cfg, repo, phase)
            if not _is_worktree(wt):
                continue
            before = _tip(wt, "HEAD")
            if not before or not _save_wip(cfg, repo, phase, log):
                continue
            after = _tip(wt, "HEAD")
            if after != before and _to_attic(repo, phase, after, log, "unfinished, not landed"):
                _git(wt, "reset", "-q", "--hard", before, check=False)


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
    sources thrash each other's fingerprints. Under lanes two disjoint
    lanes in one repo do build at once, and share it; ``[lanes] per_repo`` bounds
    how many, and the cache saved is still worth more than the thrash. Cross-repo
    phases use separate caches and don't interact.

    A cache outlives the worktrees that build into it, so what one of them left
    there must not point back at it: see :func:`_pin_test_paths` and
    :func:`_drop_stale_executables`, which runs here for every worktree made or resumed.
    """
    if not cfg.build_cache or not (wt / "Cargo.toml").exists():
        return
    shared = cfg.build_cache_dir / _slug(repo)
    _drop_stale_executables(shared, repo, log)
    link = wt / "target"
    if link.exists() or link.is_symlink():
        return  # a fresh worktree shouldn't have one; don't clobber if it does
    if _git(wt, "check-ignore", "-q", "target", check=False).returncode != 0:
        # `target` isn't ignored here — linking would risk committing the symlink.
        log.line(f"TARGET-CACHE-SKIP {repo.name} target-not-ignored")
        return
    try:
        # resolve(): the cache entry may itself be a symlink to the main
        # checkout's target/, dangling once that target is cleaned or evicted.
        shared.resolve().mkdir(parents=True, exist_ok=True)
        link.symlink_to(shared)
    except OSError as exc:
        log.line(f"TARGET-CACHE-SKIP {repo.name} {exc}")


# -- the build paths a test compiles in -----------------------------------
# cargo hands a test target absolute paths at compile time, spelled through the
# directory it was started in: the package's own binaries
# (``CARGO_BIN_EXE_<name>``) and a scratch dir (``CARGO_TARGET_TMPDIR``), both
# under ``<worktree>/target/…``, and the package's sources
# (``CARGO_MANIFEST_DIR``). A test that reads one with ``env!`` keeps that
# spelling for good, and cargo does not rebuild it when another worktree would
# spell it differently. In a shared cache the next worktree therefore runs a
# test that starts the binary, or reads its fixtures, through the worktree that
# built it, which works only until that worktree is removed.

#: The rustc wrapper :func:`_pin_test_paths` installs: cargo runs it as
#: ``<wrapper> <rustc> <args…>``. It replaces each of the two paths under
#: ``target/`` by its real path (the cache itself), which is the same string in
#: every worktree and outlives all of them. ``-S -E``: it runs once per compiled
#: crate, so it loads nothing it does not need and ignores the session's
#: ``PYTHON*`` variables. Python by shebang and no shell in between: a binary's
#: name may hold a hyphen (``CARGO_BIN_EXE_my-svc``), and ``dash`` drops a
#: variable so named from the environment it passes on, which fails the compile.
_RUSTC_WRAP = '''\
#!{python} -SE
"""Installed by swarm-orchestrator as cargo's build.rustc-wrapper (see the
config.toml beside this file). Rewrites the build paths a test compiles in to
their real path, so a test built in one worktree of a shared target cache still
finds its binary after that worktree is removed. Everything else is rustc's."""
import os
import signal
import sys

for key, value in list(os.environ.items()):
    if (key.startswith("CARGO_BIN_EXE_") or key == "CARGO_TARGET_TMPDIR") and os.path.isabs(value):
        os.environ[key] = os.path.realpath(value)
for ignored_by_python in (signal.SIGPIPE, signal.SIGXFSZ):
    signal.signal(ignored_by_python, signal.SIG_DFL)  # the compiler gets them as cargo gave them
os.execvp(sys.argv[1], sys.argv[1:])
'''

#: Interpreters the wrapper may name before the tool's own, most lasting first.
#: The system's outlives a reinstall of this tool; while the named one is away
#: every build under the state dir fails to start its compiler.
_WRAP_PYTHONS = ("/usr/bin/python3",)

_CARGO_CONFIG = """\
# Written by swarm-orchestrator, and checked whenever a worktree mirror is made.
# cargo reads the config of every directory above the one it runs in, so this
# reaches every build under this state dir, whichever repo or directory it is
# started from. RUSTC_WRAPPER in the environment still wins over it.
[build]
rustc-wrapper = {wrapper}
"""

#: A dep-info line recording a path through the building worktree that rustc
#: compiled in: which variable, and the path.
_BAKED_PATH = re.compile(
    rb"^# env-dep:(CARGO_BIN_EXE_[^=\n]*|CARGO_TARGET_TMPDIR|CARGO_MANIFEST_DIR)=(.+)$", re.M
)


def _text(path: Path) -> str | None:
    """The file's text, or None when it is absent or not readable as text."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _wrapper_fault(script: Path, home: Path) -> str | None:
    """Why ``script`` cannot serve as the rustc wrapper, or None when it can: it
    is run the way cargo runs it, on a path it must rewrite."""
    probe = "import os; print(os.environ['CARGO_TARGET_TMPDIR'])"
    trial = subprocess.run(
        [str(script), sys.executable, "-c", probe],
        env={**os.environ, "CARGO_TARGET_TMPDIR": f"{home}{os.sep}."},
        capture_output=True, text=True, timeout=60, check=False,
    )
    if trial.returncode == 0 and trial.stdout.strip() == os.path.realpath(home):
        return None
    said = (trial.stderr.strip() or trial.stdout.strip())[:200]
    return f"its trial run exited {trial.returncode}: {said}"


def _install_wrapper(wrapper: Path, home: Path) -> None:
    """Put a working :data:`_RUSTC_WRAP` at ``wrapper``, or raise ``OSError``.

    One that is already there and still works is left untouched: a build may be
    running it. A new one is tried under another name first and renamed in only
    once it works, so no build ever starts a wrapper that was not seen to run."""
    fault = "no interpreter to run it"
    for python in dict.fromkeys((*_WRAP_PYTHONS, sys.executable)):
        text = _RUSTC_WRAP.format(python=python)
        fresh = wrapper.with_name(f"{wrapper.name}.{os.getpid()}.tmp")  # two processes may be here
        script = wrapper if _text(wrapper) == text else fresh
        try:
            if script is fresh:
                fresh.write_text(text, encoding="utf-8")
                fresh.chmod(0o755)
            fault = _wrapper_fault(script, home)
            if fault is None:
                if script is fresh:
                    os.replace(fresh, wrapper)
                return
        except (OSError, subprocess.SubprocessError) as exc:
            fault = str(exc)
        fresh.unlink(missing_ok=True)
    raise OSError(fault)


def _pin_test_paths(cfg: Config, log: Log) -> None:
    """Make every build under the state dir compile the same, lasting build
    paths into its tests, by a cargo config that names :data:`_RUSTC_WRAP`.

    The config sits in ``<state>/.cargo``, above every mirror and outside every
    checkout, because cargo looks for config upwards from the directory it runs
    in: it covers a build started in a repo and one started at the mirror root
    with ``--manifest-path`` alike, and puts no untracked file in any worktree.
    A per-repo ``target-dir`` cannot do that, and ``CARGO_TARGET_DIR`` is one
    value for a session that builds in many repos.

    The config names the wrapper only while the wrapper is seen to work
    (:func:`_install_wrapper`), and is removed otherwise and when the shared
    cache is off: a wrapper that cannot run would fail every build, where a
    missing one only leaves the paths as cargo spelled them. **Never raises**:
    this runs on the way to a worker's mirror.
    """
    home = cfg.state_dir / ".cargo"
    wrapper, config = home / "rustc-wrap", home / "config.toml"
    why: Exception | None = None
    try:
        if cfg.build_cache:
            home.mkdir(parents=True, exist_ok=True)
            _install_wrapper(wrapper, home)
            text = _CARGO_CONFIG.format(wrapper=json.dumps(str(wrapper)))
            if _text(config) != text:
                fresh = config.with_name(f"{config.name}.{os.getpid()}.tmp")
                fresh.write_text(text, encoding="utf-8")
                os.replace(fresh, config)
            return
    except (OSError, ValueError) as exc:
        why = exc
    try:
        config.unlink(missing_ok=True)
    except OSError as exc:
        why = why or exc
    if why is not None:
        log.line(f"RUSTC-WRAP-SKIP {why}")


def _drop_stale_executables(shared: Path, repo: Path, log: Log) -> None:
    """Delete the cache's executables that were built through a directory which
    is gone; cargo rebuilds a missing one, through a path that is there.

    These are tests, and binaries, that compiled in a path through a worktree
    since removed: its ``target/`` (built before the wrapper was in place, or
    around it: ``RUSTC_WRAPPER`` set, cargo run from outside the state dir), or
    its sources (``CARGO_MANIFEST_DIR``, which is the worktree's own and so
    cannot be pinned). Which paths a unit was built with is read from rustc's
    dep-info beside it (``deps/<name>-<hash>.d``). Only a path that no longer
    resolves counts: a test built through a worktree that still exists works,
    and may be running. **Never raises.**
    """
    try:
        root = shared.resolve()
    except (OSError, RuntimeError):  # a link that loops
        return
    dropped = 0
    for pattern in ("*/deps/*.d", "*/*/deps/*.d"):  # <profile>/ and <triple>/<profile>/
        for dep in root.glob(pattern):
            try:
                baked = _BAKED_PATH.findall(dep.read_bytes())
                if any(_gone(var, os.fsdecode(path)) for var, path in baked):
                    dep.with_suffix("").unlink()
                    dropped += 1
            except OSError:
                continue  # unreadable, or a library: it has no file of that name
    if dropped:
        log.line(
            f"TARGET-CACHE-STALE {repo.name} dropped {dropped} executable(s) "
            "built through a worktree that is gone"
        )


def _gone(var: bytes, path: str) -> bool:
    """The directory a compiled-in path runs through no longer exists: the
    sources themselves, or the directory a binary or the scratch dir sits in."""
    if not os.path.isabs(path):
        return False  # `cargo check` passes a placeholder, not a path
    return not os.path.isdir(path if var == b"CARGO_MANIFEST_DIR" else os.path.dirname(path))


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
        _restore_later(cfg, repo, main, phase, log)


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
    Work a ``later`` finish left for this launch is merged into the new branch
    (:func:`_restore_later`).

    All or nothing: if any repo fails, the phase's mirror is set aside
    (:func:`set_aside`: discarded unless an earlier attempt's work is in it) and
    the first failure is raised as :class:`GitError`. A half mirror is worse
    than none — a worker started in it would find some repos missing.
    """
    root = cfg.project_dir.resolve()
    components = [(r, m) for (r, m) in _repos(cfg) if r.resolve() != root]
    cfg.wt_dir.mkdir(parents=True, exist_ok=True)
    _pin_test_paths(cfg, log)
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

    ``refused`` separates a push the repo's own pre-push hook *decided* against
    from one that did not get through for any other reason (unreachable remote,
    auth, a timeout, a remote that turned the ref away after the hook passed).
    Both leave the merge on local main; they differ in what the owner has to
    fix, so the reason travels with them.
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


def _ref_status(err: str) -> bool:
    """Git printed its own verdict on the ref: a ``!`` line.

    `` ! [rejected] main -> main (fetch first)``, `` ! [remote rejected] main ->
    main (cannot lock ref ...)``, `` ! [rejected] main -> main (stale info)``.
    Git prints one only for a push that reached the remote, which is after the
    local pre-push hook passed: a hook that exits non-zero ends the push before
    any ref is sent. So a failed push with such a line was not refused by the
    hook, whatever the hook printed.
    """
    return any(ln.strip().startswith("! [") for ln in err.splitlines())


def _push_reason(out: str, err: str) -> str:
    """The last few meaningful lines of a failed push, capped for a phone.

    With a ref-status line (:func:`_ref_status`) the reason is that line and
    what the remote said before it; a hook that passed and printed ``ok`` is
    never the reason. Otherwise a hook's own stdout is preferred: that is where
    a check says *what* failed (a repo's gate: ``node_modules/dep is 0.16.0 but
    package.json pins 0.17.0``), while its stderr tends to be the generic
    ``FAILED — the push was refused``. Git's own ``error:``/``hint:``/``To
    <url>`` lines are dropped.
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

    if _ref_status(err):
        lines = [ln for ln in meaningful(err) if ln.startswith(("remote:", "! ["))]
    else:
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

    A genuine non-ff rejection is merged and retried (:func:`_non_ff`). Any
    other answer from the remote about the ref (:func:`_ref_status`: a lock race
    with another pusher, a server-side rejection) is pushed again as it is, up
    to :data:`_REMOTE_RETRIES` times: if origin moved meanwhile the next answer
    is a non-ff, and that one is merged. A hook refusal returns at once with the
    hook's reason: re-running a check that just failed against the same commits
    cannot pass. Anything else (unreachable remote, auth, a timeout) is
    :data:`PUSH_FAILED`, never a :class:`GitError`, so no push can hold the
    merge queue.
    """
    if not _has_remote(repo):
        return PushResult(MERGED)
    turned_away = 0
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
        if not _ref_status(err):
            refused = "failed to push some refs" in err
            log.line(f"PUSH-{'REFUSED' if refused else 'FAIL'} {main} {reason}")
            return PushResult(PUSH_FAILED, reason, refused=refused)
        if not _non_ff(err):
            if turned_away == _REMOTE_RETRIES:
                log.line(f"PUSH-FAIL {main} {reason}")
                return PushResult(PUSH_FAILED, reason)
            turned_away += 1
            log.line(f"PUSH-RETRY {attempt} {main} {reason}")
            time.sleep(_REMOTE_RETRY_PAUSE_S)
            continue
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


def _post_merge_log(cfg: Config, repo: Path) -> Path:
    """Where a repo's last post-merge command wrote its output."""
    return cfg.log_dir / f"post-merge.{_slug(repo)}.log"


def _post_merge(cfg: Config, repo: Path, phase: str | None, log: Log) -> str:
    """Run ``repo``'s ``[git].post_merge`` command in its main checkout, before a
    push of what was merged there. ``""``: push; else why not, in the command's
    own last lines.

    Caller holds the repo lock, on a clean ``main``. What the repo's pre-push
    check reads is not all in git: a merge that moves a dependency's pin leaves
    the main checkout's install where it was, since workers install in their
    mirrors, and the check then refuses every push until someone installs the
    pin by hand. The command is what that someone would run.

    Most merges need nothing, so ``[git].post_merge_if`` is asked first, at once
    and outside the build gate: only when it exits 0 does the command queue for
    a build slot (:func:`repocmd.run_clean`). The integrator waits on it, so the
    wait for a slot is bounded too (``post_merge_timeout_s``); behind a long
    build nothing runs and the push is owed, to be tried again like any owed
    push. A command that leaves the tree with uncommitted changes has failed,
    whatever it exited with: the next merge would be held as :data:`DIRTY` for
    them.
    """
    cmd = repocmd.command(cfg, cfg.git_post_merge, repo)
    if not cmd:
        return ""
    test = repocmd.command(cfg, cfg.git_post_merge_if, repo)
    try:
        if not repocmd.wanted(test, repo):
            return ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.line(f"POST-MERGE-IF-FAILED {repo.name} {test!r}: {exc}"[:300])
        return ""
    timeout = cfg.git_post_merge_timeout_s
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    with _post_merge_log(cfg, repo).open("w", encoding="utf-8") as fh:
        fh.write(f"# post-merge {phase or '(an owed push)'}: `{cmd}` in {repo}\n")
        ran = repocmd.run_clean(cfg, cmd, repo, fh, timeout_s=timeout, phase=phase,
                                wait_s=timeout)
        why = ran.reason
        fh.write(f"# result: {why or 'ok'}\n")
    if why:
        log.line(f"POST-MERGE-FAILED {repo.name} {why}")
    else:
        log.line(f"POST-MERGE {repo.name} ok queued={ran.wait_s:.0f}s ran={ran.run_s:.0f}s")
    return why


def _push_owed(repo: Path, main: str, log: Log, cfg: Config | None = None) -> PushResult:
    """Push a ``main`` that is ahead of origin with no phase to hold. Never raises.

    Caller holds the repo lock. A merge-reconcile is only attempted when the repo
    sits cleanly on ``main`` — the owner may be mid-fix in exactly this checkout
    (fixing the check that refused the last push), and that tree must never be
    merged into or held as DIRTY on behalf of a phase that did not touch it.
    Only there, and only for a caller that passes ``cfg`` (a retry; never the
    ledger writer, whose commit is no merge), does the repo's post-merge
    command run first (:func:`_post_merge`).

    The fetch is short and tried twice (:data:`_OWED_FETCH_TIMEOUT_S`): a fetch
    that hangs once can go through at once the second time, and a push is owed
    only when both hang.
    """
    try:
        try:
            _git(repo, "fetch", "origin", check=False, timeout=_OWED_FETCH_TIMEOUT_S)
        except GitError as exc:
            log.line(f"PUSH-FETCH-RETRY {main} {exc}")
            _git(repo, "fetch", "origin", check=False, timeout=_OWED_FETCH_TIMEOUT_S)
        if not _local_ahead_of_origin(repo, main):
            return PushResult(MERGED)  # already on origin (pushed by hand, or level)
        clean = (
            _current_branch(repo) == main
            and not _merge_in_progress(repo)
            and not _rebase_in_progress(repo)
            and not _dirty(repo)
        )
        if clean and cfg is not None:
            failed = _post_merge(cfg, repo, None, log)
            if failed:
                return PushResult(PUSH_FAILED, failed)
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
            return _push_owed(repo, main, log, cfg)
    except (GitError, OSError) as exc:
        return PushResult(PUSH_FAILED, _push_reason("", str(exc)))


def origin_has(cfg: Config, repo: Path) -> bool:
    """Whether origin has everything ``repo``'s main has, by the refs this clone
    holds: no fetch, no hook. A push made by hand from this checkout moves them,
    so the debt it settled need not wait for the next spaced retry."""
    try:
        main = _repo_main(cfg, repo)
        return _ref_exists(repo, f"origin/{main}") and not _local_ahead_of_origin(repo, main)
    except GitError:
        return False


# -- integration ----------------------------------------------------------
def _auto_resolve(cfg: Config, repo: Path, phase: str, log: Log) -> bool:
    """Try to settle a failed merge mechanically. True = resolved and committed.

    Runs inside the caller's per-repo flock, on the real mid-merge tree, so a
    refusal costs nothing: the index is left exactly as the failed merge left it
    and the resolver session takes over unchanged.

    All-or-nothing on purpose. If ANY conflicted file has no configured strategy,
    or a strategy declines (both sides rewrote the same words, both reordered),
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
    left: dict[str, str] = {}  # the failed merge's own text, to put back on a refusal
    for rel, merged in resolved.items():
        try:
            left[rel] = (repo / rel).read_text(encoding="utf-8")
            (repo / rel).write_text(merged, encoding="utf-8")
        except OSError as exc:
            log.line(f"AUTORESOLVE-WRITE-FAIL {phase} {repo.name} {rel} {exc}")
            _restore(repo, left)
            return False
    failed = _check_resolved(cfg, repo, list(resolved))
    if failed:
        log.line(f"AUTORESOLVE-CHECK-FAILED {phase} {repo.name} {failed}")
        _restore(repo, left)
        return False
    staged = list(resolved)
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


def _check_resolved(cfg: Config, repo: Path, paths: list[str]) -> str:
    """Run the ``[git].auto_resolve_check`` command of every resolved path, in
    ``repo`` with the merged text on disk. ``""`` = all passed, else what failed.

    A mechanical merge keeps both sides' words but cannot know what they mean
    together: two rows that merge cleanly can still make two phases on one repo
    runnable at once. A project that can check such a rule by script names it
    here, and a failure hands the conflict to the resolver instead."""
    table = getattr(cfg, "git_auto_resolve_check", {}) or {}
    cmds = dict.fromkeys(
        c for c in (automerge.strategy_for(rel, table) for rel in paths) if c
    )
    for cmd in cmds:
        try:
            ran = subprocess.run(
                cmd, shell=True, cwd=repo, capture_output=True, text=True,
                timeout=_CHECK_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"{cmd!r}: {exc}"[:300]
        if ran.returncode != 0:
            tail = (ran.stdout + ran.stderr).strip().splitlines()[-1:] or [""]
            return f"{cmd!r} exit {ran.returncode}: {tail[0]}"[:300]
    return ""


def _restore(repo: Path, texts: dict[str, str]) -> None:
    """Put back what the failed merge left in each file (markers and all), so
    the resolver finds the tree exactly as git left it. The index was never
    touched, so the paths are still unmerged."""
    for rel, text in texts.items():
        try:
            (repo / rel).write_text(text, encoding="utf-8")
        except OSError:
            pass


def _integrate_one(
    cfg: Config,
    repo: Path,
    main: str,
    phase: str,
    log: Log,
    pushes: dict[Path, PushResult] | None = None,
    ride: Callable[[], None] | None = None,
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

    Between the merge and the push the repo's ``[git].post_merge`` command runs
    (:func:`_post_merge`). One that fails leaves the push owed like a push that
    failed, with the command's last lines as the reason.

    ``ride`` is called once this has merged ``swarm/<phase>`` into the project
    checkout, before that command and the push, still under the repo lock: what
    it adds to the merge commit (:func:`amend_merge`) is then in the tree the
    command and the repo's pre-push check read, and in the one commit that is
    pushed. It is not called for a component repo, nor when nothing is merged
    here: a phase with no commit in the umbrella, or a merge that conflicted and
    was finished by a resolver.
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
            pushes[repo] = _push_owed(repo, main, log, cfg)
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
            if ride is not None and repo.resolve() == cfg.project_dir.resolve():
                ride()
        if _has_remote(repo):
            failed = _post_merge(cfg, repo, phase, log)
            pushed = PushResult(PUSH_FAILED, failed) if failed else _push_result(repo, main, log)
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
    cfg: Config, phase: str, log: Log, pushes: dict[Path, PushResult] | None = None,
    ride: Callable[[], None] | None = None,
) -> str:
    """Land ``swarm/<phase>`` across every repo the phase mirrored, components
    first then umbrella. Returns :data:`MERGED` only when all repos are clean;
    the first non-merged repo's status short-circuits and is returned so the
    supervisor can hold the queue and (for a conflict) point a resolver at the
    exact repo (:func:`blocked_repo`).

    ``pushes`` (when given) collects every push this attempted, keyed by repo; a
    failed one there is an owed push, not a hold (see :func:`_integrate_one`).
    It is filled even when a later repo conflicts — the earlier repos' merges are
    on local main regardless.

    ``ride`` (when given) is called between the umbrella's merge and its push
    (see :func:`_integrate_one`).

    With ``[lanes] enabled`` the landing re-tests the phase against siblings that
    landed in its repos meanwhile (:func:`landing.integrate`)."""
    if cfg.lanes_enabled:
        from . import landing  # lazy: landing builds on this module

        return landing.integrate(cfg, phase, log, pushes, ride)
    for repo, main in _repos(cfg):
        result = _integrate_one(cfg, repo, main, phase, log, pushes, ride)
        if result != MERGED:
            return result
    _rmtree_mirror(cfg, phase)  # all repos merged -> drop the empty mirror shell
    return MERGED


#: :func:`commit_to_target` outcomes besides :data:`MERGED`-style pushes.
COMMITTED = "committed"
UNCHANGED = "unchanged"
HELD = "held"
UNVERSIONED = "unversioned"


@dataclass
class TargetCommit:
    """What :func:`commit_to_target` did, and the push that followed a commit."""

    status: str  # COMMITTED | UNCHANGED | HELD | UNVERSIONED
    reason: str = ""
    push: PushResult | None = None


def _existing(repo: Path, paths: list[str]) -> list[str]:
    """The pathspecs that name something on disk or in the index: git refuses
    a pathspec that matches nothing, and a history dir may not exist yet."""
    tracked = _git(repo, "ls-files", "--", *paths, check=False).stdout.split("\n")
    return [p for p in paths if (repo / p).exists()
            or any(t == p or t.startswith(p.rstrip("/") + "/") for t in tracked if t)]


def _put_back(repo: Path, paths: list[str]) -> None:
    """Drop what a failed write left in ``paths``: staged, edited or new.

    One checkout per path: git refuses them all when one names nothing it
    tracks (a history directory whose first file was just written), and the
    ledger would then stay edited and hold every later write."""
    names = _existing(repo, paths)
    if names:
        _git(repo, "reset", "-q", "--", *names, check=False)
        for name in names:
            _git(repo, "checkout", "--", name, check=False)
        _git(repo, "clean", "-fdq", "--", *names, check=False)


def commit_to_target(cfg: Config, paths: list[str], write, message: str, log: Log) -> TargetCommit:
    """Write swarm-authored files on the target branch of the project checkout and commit them.

    ``write()`` edits files under ``cfg.project_dir``, inside ``paths`` only
    (project-relative files or directories). It runs under the umbrella's repo
    lock, so it is serialised with integration, on the configured main branch,
    and only when none of ``paths`` holds anyone else's uncommitted change: the
    commit takes exactly those paths (``git commit -- <paths>``), so other work
    in the tree, an in-place worker's included, is never swept in. Anything
    else is :data:`HELD` with the reason, untouched. A failed write or commit
    puts ``paths`` back as they were. The commit is pushed like an owed push;
    a push that fails is the caller's to record (:func:`pushowed.settle`).
    """
    repo = cfg.project_dir
    if not (repo / ".git").exists():
        write()
        return TargetCommit(UNVERSIONED)
    main = cfg.git_main_branch
    with repo_lock(cfg, repo):
        if _current_branch(repo) != main:
            return TargetCommit(HELD, f"the checkout is on {_current_branch(repo)}, not {main}")
        if _merge_in_progress(repo) or _rebase_in_progress(repo):
            return TargetCommit(HELD, "a merge or rebase is in progress")
        if _git(repo, "status", "--porcelain", "--", *paths, check=False).stdout.strip():
            return TargetCommit(HELD, f"uncommitted changes in {' '.join(paths)}")
        try:
            write()
            names = _existing(repo, paths)
            if names:
                _git(repo, "add", "-A", "--", *names)
            staged = _git(repo, "diff", "--cached", "--quiet", "--", *names, check=False) if names else None
            if staged is None or staged.returncode == 0:
                return TargetCommit(UNCHANGED)
            _git(repo, "commit", "-q", "-m", message, "--", *names)
        except BaseException:
            _put_back(repo, paths)
            raise
        log.line(f"TARGET-COMMIT {repo.name} {message}")
        push = _push_owed(repo, main, log) if _has_remote(repo) else None
        return TargetCommit(COMMITTED, push=push)


def amend_merge(
    cfg: Config, phase: str, paths: list[str], write, summary: str, log: Log
) -> TargetCommit:
    """:func:`commit_to_target` for a phase that is landing: ``write()``'s files
    go into the merge commit of ``swarm/<phase>`` instead of a commit of their own.

    For :func:`_integrate_one`'s ``ride``: the caller holds the umbrella's repo
    lock, on the configured main branch, and ``HEAD`` is the merge it just made.
    A ``HEAD`` that is not that merge is :data:`HELD`, and so are ``paths`` that
    hold anyone's uncommitted change. The merge is amended to its own tree plus
    exactly ``paths`` (``git commit --amend -- <paths>``), on the same two
    parents; its subject keeps what it says and gains ``summary`` after it. A
    failed write or amend puts ``paths`` back as they were and leaves the merge
    as it is, and so does a commit someone made here while ``write()`` ran: that
    one is theirs, never amended. Nothing is pushed here: the landing's own push
    takes the commit.
    """
    repo = cfg.project_dir
    head, merged = _tip(repo, "HEAD"), _tip(repo, "HEAD^2")
    if not merged or merged != _tip(repo, f"swarm/{phase}"):
        return TargetCommit(HELD, f"HEAD is not the merge of swarm/{phase}")
    if _git(repo, "status", "--porcelain", "--", *paths, check=False).stdout.strip():
        return TargetCommit(HELD, f"uncommitted changes in {' '.join(paths)}")
    subject, _, rest = _git(repo, "log", "-1", "--format=%B").stdout.partition("\n")
    message = f"{subject}: {summary}"
    try:
        write()
        names = _existing(repo, paths)
        if names:
            _git(repo, "add", "-A", "--", *names)
        staged = _git(repo, "diff", "--cached", "--quiet", "--", *names, check=False) if names else None
        if staged is None or staged.returncode == 0:
            return TargetCommit(UNCHANGED)
        if _tip(repo, "HEAD") != head:
            raise GitError(f"HEAD moved off the merge of swarm/{phase} meanwhile")
        _git(repo, "commit", "-q", "--amend", "-m", f"{message}\n{rest}", "--", *names)
    except BaseException:
        _put_back(repo, paths)
        raise
    log.line(f"TARGET-AMEND {repo.name} {message}")
    return TargetCommit(COMMITTED)


def committed_text(cfg: Config, rel: str) -> str:
    """A project file as committed on the target branch: what
    :func:`commit_to_target` writes onto, whatever the checkout holds right now
    (another branch, an edit nobody committed, a merge's conflict markers).
    The file on disk when the project is not under git; "" when the branch has
    no such file."""
    repo = cfg.project_dir
    if not (repo / ".git").exists():
        try:
            return (repo / rel).read_text(encoding="utf-8")
        except OSError:
            return ""
    got = _git(repo, "show", f"{cfg.git_main_branch}:{rel}", check=False)
    return got.stdout if got.returncode == 0 else ""


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
    """The ask for a hold caused by the owner's checkout sitting on another
    branch (what to do, then why), or None when that is not why ``repo`` is held."""
    if repo is None:
        return None
    main = _repo_main(cfg, repo)
    other = _off_main(repo, main)
    if other is None:
        return None
    return (
        f"Switch the {repo.name} checkout back to {main}, then run `swarm resolved"
        f" {phase}`: it is on {other}, and the swarm will not switch it for you, so"
        f" {phase} is finished but not merged and all merging waits on it."
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


#: The uncommitted paths :func:`unfinished` names before it counts the rest.
_UNFINISHED_SHOWN = 5


def unfinished(repo: Path) -> str:
    """What a premature ``swarm resolved`` found in ``repo``, in plain words: an
    unfinished merge or rebase, else the uncommitted changes :func:`_dirty`
    counts with the first paths named, so a reader can tell the output of a
    test run from work left half done."""
    if _merge_in_progress(repo):
        return "an unfinished merge"
    if _rebase_in_progress(repo):
        return "an unfinished rebase"
    out = _git(repo, "status", "--porcelain", "--untracked-files=no", check=False).stdout
    paths = [line[3:] for line in out.splitlines() if line.strip()]
    if not paths:  # the checkout is off main, or it was put right meanwhile
        return "an unfinished merge or uncommitted changes"
    shown = paths[:_UNFINISHED_SHOWN]
    if len(paths) > _UNFINISHED_SHOWN:
        shown.append(f"+{len(paths) - _UNFINISHED_SHOWN} more")
    return "uncommitted changes in " + ", ".join(shown)


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
    kind: str  # MERGED is impossible here: CONFLICT | DIRTY | PUSH_FAILED | LANE_HOLDS
    repo: Path | None  # the repo to clear (a lane hold's worktree; None for PUSH_FAILED)


@dataclass(frozen=True)
class ReconcileResult:
    """Outcome of :func:`reconcile`: what landed, and what is still stuck."""

    integrated: list[str] = field(default_factory=list)
    held: list[Held] = field(default_factory=list)
    #: Phases whose landing is under way under lanes (a check runs, or another
    #: landing holds a repo): queued for the supervisor, neither done nor held.
    landing: list[str] = field(default_factory=list)
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
    later: set[str] | frozenset[str] = frozenset(),
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

    ``operator`` (``{mirror: "keep" | "integrate" | "live"}``, from
    :func:`operator.mirror_plan`) covers the one kind of ``swarm/*`` branch that
    has no sentinel by design: an operator job's mirror. ``"live"`` is any mirror
    whose session is still running (a restart carried it across): left exactly
    as it is. Without it every such
    mirror reads as an interrupted phase and is discarded — commits and all. A
    live job's mirror is kept for its next attempt; a finished job's is landed.

    ``later`` are the phases that finished ``later`` and wait for a date. One
    whose branch is still here reported while no supervisor ran: its work is
    kept for its date (:func:`keep_later`), not sent to the attic. Work kept for
    a phase the done map has since recorded done (``swarm skip``, for one) goes
    to the attic. A row closed in the ledger (``swarm record <phase> done``, a
    tick by hand) leaves no record here: the caller runs
    :func:`ledgerw.release_kept` for those, as the supervisor's sweep does.
    """
    sentinels = sentinel_done(cfg)
    operator = operator or {}
    integrated: list[str] = []
    operator_integrated: list[str] = []
    held: list[Held] = []
    landing_: list[str] = []
    pushes: dict[str, dict[Path, PushResult]] = {}
    for phase in sorted(_all_swarm_phases(cfg)):
        job = operator.get(phase)
        if job == "live":
            # A session a restart carried across is working in it right now.
            log.line(f"RECONCILE-LIVE {phase} its session came through the restart")
        elif job == "keep" and _mirror_empty(cfg, phase):
            # Kept so a retry finds the commits an attempt already made; with
            # none, keeping it only hands the next attempt a stale base.
            discard(cfg, phase, log)
            log.line(f"RECONCILE-DISCARD {phase} empty operator mirror")
        elif job == "keep":
            log.line(f"RECONCILE-KEEP {phase} operator-job")
        elif phase in later and sentinels.get(phase) not in DONE_INTEGRATE:
            keep_later(cfg, phase, log)
            log.line(f"RECONCILE-KEPT-FOR-DATE {phase}")
        elif phase in done_phases:
            discard(cfg, phase, log)
            log.line(f"RECONCILE-GC {phase} already-recorded")
        elif job == "integrate" or sentinels.get(phase) in DONE_INTEGRATE:
            pushes[phase] = {}
            result = integrate(cfg, phase, log, pushes[phase])
            if result == MERGED:
                (operator_integrated if job else integrated).append(phase)
                log.line(f"RECONCILE-INTEGRATED {phase}")
            elif result in LANE_PENDING:
                landing_.append(phase)
                log.line(f"RECONCILE-LANDING {phase} {result}")
            elif result in LANE_HOLDS:
                from . import landing  # lazy: landing builds on this module

                hit = landing.blocked(cfg, phase)
                held.append(Held(phase=phase, kind=result, repo=hit[1] if hit else None))
                log.line(f"RECONCILE-HELD {phase} {result}")
            else:
                repo = blocked_repo(cfg, phase) if result in (CONFLICT, DIRTY) else None
                held.append(Held(phase=phase, kind=result, repo=repo))
                log.line(f"RECONCILE-HELD {phase} {result} repo={repo}")
        elif set_aside(cfg, phase, log):
            log.line(f"RECONCILE-KEEP {phase} interrupted; its next launch resumes it")
        else:
            log.line(f"RECONCILE-DISCARD {phase} interrupted with nothing to keep")
    for phase in sorted(kept_later(cfg) - set(later)):
        if done_phases.get(phase, statuses.FAIL) != statuses.FAIL:
            unkeep_later(cfg, phase, log)
    return ReconcileResult(
        integrated=integrated,
        held=held,
        landing=landing_,
        pushes={k: v for k, v in pushes.items() if v},
        operator_integrated=operator_integrated,
    )


def reconcile_orphans(cfg: Config, done_phases: dict[str, str], log: Log) -> list[str]:
    """The integrated phases only — :func:`reconcile`'s original return shape,
    kept for callers that do not (yet) act on held phases."""
    return reconcile(cfg, done_phases, log).integrated
