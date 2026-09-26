"""Backup pushes: every piece of unmerged swarm work, copied to the repo's origin.

Work on ``swarm/<phase>`` lives only on this machine until it merges, which can
be hours. A pass copies it to the ``origin`` each repo already pushes its main to,
under names the swarm owns:

* ``swarm/<phase>``      — the phase branch, when it holds commits main lacks;
* ``swarm-wip/<phase>``  — what its worktree has not committed yet, as a snapshot
  commit on top of the branch head, built in a throwaway index so the worker's
  own index and files are never touched;
* ``swarm-attic/<phase>-<stamp>`` — each kept ``refs/swarm-attic/<phase>/<stamp>``,
  as a branch, because a hosting service is only sure to keep branches.

Everything is pushed with ``--force-with-lease`` against what ``ls-remote`` just
reported, and ``--no-verify``: a repo's pre-push checks judge work bound for main,
and a backup is not that. A remote backup whose work is now on main is deleted.
A failure is logged and reported, never raised: a backup must not block anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gitq
from .config import Config
from .logutil import Log

BRANCH = "refs/heads/swarm/"
WIP = "refs/heads/swarm-wip/"
ATTIC_BRANCH = "refs/heads/swarm-attic/"
ATTIC_REFS = "refs/swarm-attic/"
OURS = (BRANCH, WIP, ATTIC_BRANCH)

#: A backup call gives up sooner than an integration's: nothing waits on it, and
#: ``swarm down`` should not sit out a dead network for long.
TIMEOUT_S = 60.0
#: How long ``swarm down`` gives a whole pass before it stops starting new repos.
DOWN_BUDGET_S = 300.0

_WIP_MESSAGE = "swarm: backup of uncommitted work on {phase}"


@dataclass
class Result:
    """What one pass did, per ref (``<repo>:<branch>``)."""

    pushed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def line(self) -> str:
        parts = [f"{len(self.pushed)} pushed", f"{len(self.deleted)} deleted"]
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return ", ".join(parts)


def _git(cwd: Path, *args: str, env: dict | None = None, timeout: float = TIMEOUT_S):
    """``git -C cwd args`` that never raises: a timeout reads as a failed call."""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **gitq._GIT_ENV, **(env or {})},
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(args, 1, "", str(exc))


def _out(cwd: Path, *args: str, env: dict | None = None) -> str | None:
    proc = _git(cwd, *args, env=env)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _refs(repo: Path, prefix: str) -> dict[str, str]:
    """Local ``{refname: sha}`` under ``prefix``."""
    out = _out(repo, "for-each-ref", "--format=%(refname) %(objectname)", prefix) or ""
    return dict(ln.split(" ", 1) for ln in out.splitlines() if " " in ln)


def _remote_refs(repo: Path) -> dict[str, str] | None:
    """The origin's backup branches, ``{refname: sha}``; None when unreachable."""
    proc = _git(repo, "ls-remote", "--heads", "origin")
    if proc.returncode != 0:
        return None
    found: dict[str, str] = {}
    for ln in proc.stdout.splitlines():
        sha, _, ref = ln.partition("\t")
        if ref.startswith(OURS):
            found[ref] = sha
    return found


def _on_main(repo: Path, main: str, rev: str) -> bool:
    """``rev`` is on ``main``. An object this repo lacks counts as not on it."""
    return _git(repo, "merge-base", "--is-ancestor", rev, main).returncode == 0


def _nested(cfg: Config, repo: Path) -> list[str]:
    """Component repos under ``repo``, relative to it: separate checkouts that
    this repo's ``git add`` must never pick up as embedded repositories."""
    root = cfg.project_dir.resolve()
    here = repo.resolve().relative_to(root)
    out = []
    for other in gitq.discovered_repos(cfg):
        rel = other.resolve().relative_to(root)
        if rel != here and (here == Path(".") or here in rel.parents):
            out.append(str(rel.relative_to(here)))
    return out


def snapshot(cfg: Config, repo: Path, phase: str, log: Log) -> str | None:
    """A commit of ``phase``'s uncommitted work in ``repo``, or None when clean.

    Built in a copy of the worktree's index (``GIT_INDEX_FILE``): ``add -A``,
    ``write-tree``, then ``commit-tree`` with the branch head as parent. The
    worker's own index, files and branch are left exactly as they were.
    """
    wt = gitq._wt_for(cfg, repo, phase)
    if not (wt / ".git").exists():
        return None
    spec = ["--", ".", *(f":(exclude){p}" for p in _nested(cfg, repo))]
    changes = _out(wt, "status", "--porcelain", *spec)
    head = _out(wt, "rev-parse", "--verify", "HEAD")
    if not changes or not head:
        return None
    index = _out(wt, "rev-parse", "--path-format=absolute", "--git-path", "index")
    with tempfile.TemporaryDirectory(prefix="swarm-backup-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if index and Path(index).is_file():
            shutil.copyfile(index, env["GIT_INDEX_FILE"])  # its stat cache: fast add
        elif _git(wt, "read-tree", "HEAD", env=env).returncode != 0:
            return None
        added = _git(wt, "add", "-A", *spec, env=env)
        tree = _out(wt, "write-tree", env=env) if added.returncode == 0 else None
    if tree is None:
        log.line(f"BACKUP-SNAPSHOT-FAILED {phase} {repo.name}: {added.stderr.strip()[:200]}")
        return None
    if tree == _out(wt, "rev-parse", "HEAD^{tree}"):
        return None
    commit = _out(wt, "commit-tree", tree, "-p", head, "-m", _WIP_MESSAGE.format(phase=phase))
    if commit is None:
        log.line(f"BACKUP-SNAPSHOT-FAILED {phase} {repo.name}: commit-tree")
    return commit


def _attic_branch(ref: str) -> str:
    """``refs/swarm-attic/<phase>/<stamp>`` -> ``refs/heads/swarm-attic/<phase>-<stamp>``."""
    rest = ref[len(ATTIC_REFS):]
    head, _, stamp = rest.rpartition("/")
    return ATTIC_BRANCH + (f"{head}-{stamp}" if head else stamp)


def _wanted(cfg: Config, repo: Path, main: str, log: Log) -> dict[str, str]:
    """``{remote ref: sha}`` this repo's backup should hold right now."""
    want: dict[str, str] = {}
    for ref, sha in _refs(repo, "refs/heads/swarm/").items():
        phase = ref[len(BRANCH):]
        if not _on_main(repo, main, sha):
            want[ref] = sha
        snap = snapshot(cfg, repo, phase, log)
        if snap:
            want[WIP + phase] = snap
    for ref, sha in _refs(repo, ATTIC_REFS).items():
        want[_attic_branch(ref)] = sha
    return want


def _stale(repo: Path, main: str, ref: str, sha: str) -> bool:
    """A remote backup that holds nothing main lacks and no live branch owns."""
    if ref.startswith(WIP):
        phase = ref[len(WIP):]
        if gitq._branch_exists(repo, f"swarm/{phase}"):
            return False  # its phase is still unmerged; the next snapshot replaces it
        return _on_main(repo, main, f"{sha}^")
    if ref.startswith(BRANCH) and gitq._branch_exists(repo, ref[len("refs/heads/"):]):
        return False
    return _on_main(repo, main, sha)


def _push_repo(cfg: Config, repo: Path, main: str, log: Log, res: Result) -> None:
    if _out(repo, "remote", "get-url", "--push", "origin") is None:
        return  # the project pushes this repo nowhere, so neither does its backup
    remote = _remote_refs(repo)
    if remote is None:
        res.failed.append(f"{repo.name}: origin unreachable")
        log.line(f"BACKUP-FAILED {repo.name} ls-remote")
        return
    want = _wanted(cfg, repo, main, log)
    specs: list[str] = []
    leases: list[str] = []
    for ref, sha in want.items():
        if remote.get(ref) != sha:
            specs.append(f"{sha}:{ref}")
            leases.append(f"--force-with-lease={ref}:{remote.get(ref, '')}")
    for ref, sha in remote.items():
        if ref not in want and _stale(repo, main, ref, sha):
            specs.append(f":{ref}")
            leases.append(f"--force-with-lease={ref}:{sha}")
    if not specs:
        return
    proc = _git(repo, "push", "--porcelain", "--no-verify", *leases, "origin", *specs)
    seen = set()
    for ln in proc.stdout.splitlines():
        flag, _, rest = ln.partition("\t")
        target = rest.split("\t", 1)[0].rpartition(":")[2]
        if not target.startswith(OURS):
            continue
        seen.add(target)
        name = f"{repo.name}:{target[len('refs/heads/'):]}"
        if flag == "!":
            res.failed.append(name)
        elif flag == "-":
            res.deleted.append(name)
        elif flag != "=":
            res.pushed.append(name)
    for spec in specs:
        target = spec.rpartition(":")[2]
        if target not in seen:  # the push never got as far as reporting it
            res.failed.append(f"{repo.name}:{target[len('refs/heads/'):]}")
    if proc.returncode != 0:
        log.line(f"BACKUP-PUSH-FAILED {repo.name} {gitq._push_reason(proc.stdout, proc.stderr)}")


def run(cfg: Config, log: Log, budget_s: float | None = None) -> Result:
    """One backup pass over every repo a phase spans. Never raises.

    ``budget_s`` bounds the pass: once spent, no further repo is started (the
    one under way finishes). Worktree isolation only: without it there are no
    ``swarm/*`` branches, and the canonical checkout is the owner's own.
    """
    res = Result()
    if cfg.git_isolation != "worktree":
        return res
    deadline = time.monotonic() + budget_s if budget_s else None
    try:
        repos = gitq._repos(cfg)
    except (gitq.GitError, OSError) as exc:
        log.line(f"BACKUP-FAILED {exc}")
        res.failed.append(str(exc))
        return res
    for repo, main in repos:
        if deadline is not None and time.monotonic() > deadline:
            res.failed.append(f"{repo.name}: out of time")
            continue
        try:
            _push_repo(cfg, repo, main, log, res)
        except Exception as exc:  # noqa: BLE001 - a backup reports, it never breaks its caller
            res.failed.append(f"{repo.name}: {exc}")
            log.line(f"BACKUP-FAILED {repo.name} {exc!r}")
    if res.pushed or res.deleted or res.failed:
        log.line(f"BACKUP {res.line()}"
                 + (f" failed={' '.join(res.failed[:10])}" if res.failed else ""))
    return res
