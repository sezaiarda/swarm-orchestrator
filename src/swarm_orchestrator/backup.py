"""Backup pushes: every piece of unmerged swarm work, copied to the repo's origin.

Work on ``swarm/<phase>`` lives only on this machine until it merges, which can
be hours. A pass copies it to the ``origin`` each repo already pushes its main to,
under names the swarm owns:

* ``swarm/<phase>``      — the phase branch, when it holds commits main lacks
  (also for a phase that finished ``later``: its branch is gone and its work
  waits for its date under ``refs/swarm-later/<phase>``);
* ``swarm-wip/<phase>``  — what its worktree has not committed yet, as a snapshot
  commit on top of the branch head, built in a throwaway index so the worker's
  own index and files are never touched;
* ``swarm-attic/<phase>-<stamp>`` — each kept ``refs/swarm-attic/<phase>/<stamp>``,
  as a branch, because a hosting service is only sure to keep branches.

Everything is pushed with ``--force-with-lease`` against what ``ls-remote`` just
reported, and ``--no-verify``: a repo's pre-push checks judge work bound for main,
and a backup is not that. A remote backup whose work is now on main is deleted,
and an attic backup goes when gc prunes its local ref (:func:`drop_attic`).
A failure is logged and reported, never raised: a backup must not block anything.
Each pass's outcome is kept in ``<state>/backup.json`` (:func:`last`) for
``swarm doctor``, which says when uncommitted work could not be snapshotted.
"""

from __future__ import annotations

import json
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

#: The last pass's outcome, in the state dir, for ``swarm doctor``.
RECORD = "backup.json"
#: At most this many names are kept per list in it; the counts stay exact.
RECORD_KEEP = 20


@dataclass
class Result:
    """What one pass did, per ref (``<repo>:<branch>``)."""

    pushed: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    #: Uncommitted work no snapshot could be made of, and why. Each is counted in
    #: ``failed`` too: it did not reach origin, whatever else the pass pushed.
    snapshots: dict[str, str] = field(default_factory=dict)

    def line(self) -> str:
        parts = [f"{len(self.pushed)} pushed", f"{len(self.deleted)} deleted"]
        if self.failed:
            n = len(self.snapshots)
            parts.append(f"{len(self.failed)} failed"
                         + (f" ({n} snapshot{'s' if n != 1 else ''})" if n else ""))
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


def _excludes(cfg: Config, repo: Path, wt: Path) -> list[str]:
    """Pathspecs that keep the nested repos out of a snapshot of ``repo``.

    A nested repo that ``repo``'s own ignore rules cover gets none. ``add -A``
    skips an ignored path anyway, and git refuses a pathspec that names one (or
    a path under one), inside ``:(exclude)`` too: "The following paths are
    ignored by one of your .gitignore files", exit 1. An umbrella that
    gitignores its component repos lost every snapshot to that.
    """
    return [f":(exclude){p}" for p in _nested(cfg, repo)
            if _git(wt, "check-ignore", "-q", "--", p).returncode != 0]


def _why(step: str, proc: subprocess.CompletedProcess) -> str:
    """``step`` and what git said about it, on one line."""
    said = " ".join((proc.stderr or proc.stdout or "").split())
    return f"{step}: {said or f'exit {proc.returncode}'}"[:200]


def _snapshot(cfg: Config, repo: Path, phase: str) -> tuple[str | None, str | None]:
    """``(commit, None)``; ``(None, None)`` when there is nothing to save; or
    ``(None, why)`` when there is, or may be, and no snapshot could be made."""
    wt = gitq._wt_for(cfg, repo, phase)
    if not (wt / ".git").exists():
        return None, None
    spec = ["--", ".", *_excludes(cfg, repo, wt)]
    status = _git(wt, "status", "--porcelain", *spec)
    if status.returncode != 0:
        return None, _why("status", status)
    if not status.stdout.strip():
        return None, None
    head = _out(wt, "rev-parse", "--verify", "HEAD")
    if not head:
        return None, "rev-parse: no commit to put the snapshot on"
    index = _out(wt, "rev-parse", "--path-format=absolute", "--git-path", "index")
    with tempfile.TemporaryDirectory(prefix="swarm-backup-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        steps = [("add", "-A", *spec), ("write-tree",)]
        if index and Path(index).is_file():
            shutil.copyfile(index, env["GIT_INDEX_FILE"])  # its stat cache: fast add
        else:
            steps.insert(0, ("read-tree", "HEAD"))
        for step in steps:
            done = _git(wt, *step, env=env)
            if done.returncode != 0:
                return None, _why(step[0], done)
    tree = done.stdout.strip()
    if tree == _out(wt, "rev-parse", "HEAD^{tree}"):
        return None, None
    made = _git(wt, "commit-tree", tree, "-p", head, "-m", _WIP_MESSAGE.format(phase=phase))
    if made.returncode != 0:
        return None, _why("commit-tree", made)
    return made.stdout.strip(), None


def snapshot(cfg: Config, repo: Path, phase: str, log: Log) -> str | None:
    """A commit of ``phase``'s uncommitted work in ``repo``, or None when clean.

    Built in a copy of the worktree's index (``GIT_INDEX_FILE``): ``add -A``,
    ``write-tree``, then ``commit-tree`` with the branch head as parent. The
    worker's own index, files and branch are left exactly as they were. None
    also when it could not be made, which is logged (``BACKUP-SNAPSHOT-FAILED``).
    """
    commit, why = _snapshot(cfg, repo, phase)
    if why:
        log.line(f"BACKUP-SNAPSHOT-FAILED {phase} {repo.name}: {why}")
    return commit


def _attic_branch(ref: str) -> str:
    """``refs/swarm-attic/<phase>/<stamp>`` -> ``refs/heads/swarm-attic/<phase>-<stamp>``."""
    rest = ref[len(ATTIC_REFS):]
    head, _, stamp = rest.rpartition("/")
    return ATTIC_BRANCH + (f"{head}-{stamp}" if head else stamp)


def drop_attic(repo: Path, ref: str, log: Log) -> None:
    """Delete the origin's backup of attic ref ``ref``, which gc has just pruned.

    A pass deletes a remote attic branch only once its work is on main, and set-
    aside work mostly never gets there, so without this every one would stay on
    the host for good. Best-effort and logged, like every backup call."""
    if _out(repo, "remote", "get-url", "--push", "origin") is None:
        return
    branch = _attic_branch(ref)
    name = f"{repo.name}:{branch[len('refs/heads/'):]}"
    proc = _git(repo, "ls-remote", "origin", branch)
    if proc.returncode == 0:
        if not proc.stdout.strip():
            return  # never backed up, or already gone
        proc = _git(repo, "push", "--no-verify", "origin", f":{branch}")
    if proc.returncode == 0:
        log.line(f"BACKUP-ATTIC-DELETED {name}")
    else:
        log.line(f"BACKUP-ATTIC-DELETE-FAILED {name} "
                 f"{gitq._push_reason(proc.stdout, proc.stderr)}")


def _wanted(cfg: Config, repo: Path, main: str, log: Log, res: Result) -> dict[str, str]:
    """``{remote ref: sha}`` this repo's backup should hold right now. A snapshot
    that could not be made is entered in ``res``: those edits were not copied."""
    want: dict[str, str] = {}
    for ref, sha in _refs(repo, "refs/heads/swarm/").items():
        phase = ref[len(BRANCH):]
        if not _on_main(repo, main, sha):
            want[ref] = sha
        snap, why = _snapshot(cfg, repo, phase)
        if snap:
            want[WIP + phase] = snap
        elif why:
            name = f"{repo.name}:swarm-wip/{phase}"
            res.snapshots[name] = why
            res.failed.append(f"{name} (no snapshot)")
            log.line(f"BACKUP-SNAPSHOT-FAILED {phase} {repo.name}: {why}")
    for ref, sha in _refs(repo, ATTIC_REFS).items():
        want[_attic_branch(ref)] = sha
    # Work kept for a `later` phase's date has no branch until the relaunch.
    for ref, sha in _refs(repo, gitq.LATER + "/").items():
        want.setdefault(BRANCH + ref[len(gitq.LATER) + 1:], sha)
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
    want = _wanted(cfg, repo, main, log, res)
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
        _record(cfg, res, log)
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
    _record(cfg, res, log)
    return res


def last(cfg: Config) -> dict | None:
    """What the last pass did, as :func:`_record` wrote it; None when no pass has
    been recorded (or the record does not read)."""
    try:
        data = json.loads((cfg.state_dir / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _record(cfg: Config, res: Result, log: Log) -> None:
    """Keep the pass's outcome for ``swarm doctor``, which runs in another
    process and long after the log line has scrolled away. Failed snapshots carry
    how many passes running they have failed, and since when."""
    now = time.time()
    prior = last(cfg) or {}
    data = {
        "ts": now,
        "pushed": len(res.pushed),
        "deleted": len(res.deleted),
        "failed": res.failed[:RECORD_KEEP],
        "failed_n": len(res.failed),
        "snapshots": dict(list(res.snapshots.items())[:RECORD_KEEP]),
        "snapshots_n": len(res.snapshots),
        "unsaved_passes": int(prior.get("unsaved_passes") or 0) + 1 if res.snapshots else 0,
        "unsaved_since": (prior.get("unsaved_since") or now) if res.snapshots else None,
    }
    path = cfg.state_dir / RECORD
    tmp = path.with_name(f"{RECORD}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as exc:
        log.line(f"BACKUP-RECORD-FAILED {exc!r}")
