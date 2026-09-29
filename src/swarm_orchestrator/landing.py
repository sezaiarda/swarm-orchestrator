"""Landing under lanes: re-test a phase against what landed beside it.

With lanes on, two phases can build in one repo at once, so the second to finish
branched from a main that no longer exists. Its worker's gates tested a tree that
will never land. For each repo R the phase changed, :func:`integrate` asks whether
R's main gained anything but commons since the branch was cut:

- **No:** land exactly as :func:`gitq._integrate_one` always has.
- **Yes:** take R's landing lock, merge main into the phase's own worktree of R
  (``auto_resolve`` applies there), and run R's ``[lanes] check`` in that
  worktree, detached (``swarm _lane-check``). The queue lands other phases while
  it runs. Green: merge into main, which cannot conflict, so the tree that lands
  is the tree that was tested. Red, or a text conflict at the catch-up: a
  resolver opens **on the worktree**. The owner's checkout is never mid-merge.

**The landing lock.** A phase's entry for R in ``State.landing`` is its hold on
R: nobody else lands in R until the entry goes, at the canonical merge (or when
the phase leaves the queue). The entry, not the process, is what survives a
supervisor restart; the flock at ``<state>/landing/<repo-slug>.lock`` is taken
beside it for as long as this process holds the entry, and around every lanes
merge into R, so no second process can move R's main meanwhile.

**Lock order is landing -> build semaphore -> family lock**, and nothing takes a
landing lock while holding either of the others. The integrator holds landing
locks and never a build slot; ``swarm _lane-check`` takes a build slot and never
a landing lock; the family lock is taken inside a check's push gate. So no cycle
can form.

**Outside the lane (D6).** At landing, the files the phase changed that are
neither in its snapshot lane (``State.lanes``) nor commons are listed: a history
note on the phase, a line in the next Overseer digest. It never holds a merge.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import time
from fnmatch import fnmatch
from pathlib import Path

from . import buildsem
from . import gitq
from . import launch as launch_mod
from . import ledgerw
from . import state as state_mod
from .config import Config
from .lanes import LaneError, Touch, overlaps, parse_touch
from .logutil import Log

# Stages of a phase's entry for one repo.
CHECKING = "checking"  # the detached check is running
PASSED = "ok"  # the check passed; land once the branch still contains main
RED = "red"  # the check failed: a semantic-conflict resolver is on the worktree
CONFLICT = "conflict"  # the catch-up merge conflicted: a resolver is on the worktree
AGAIN = "again"  # `swarm resolved`: merge main again and re-check, moved or not

_KILL_GRACE_S = 10.0
_MERGED_BRANCH = re.compile(r"swarm/([A-Za-z0-9._-]+)")


# -- where things are ------------------------------------------------------
def landing_dir(cfg: Config) -> Path:
    """``<state>/landing``: locks, check logs and results, the undeclared log."""
    return cfg.state_dir / "landing"


def lane_name(cfg: Config, repo: Path) -> str:
    """A repo's lane: its directory name, ``.`` for the umbrella."""
    return "." if repo.resolve() == cfg.project_dir.resolve() else repo.name


def repo_for(cfg: Config, lane: str) -> tuple[Path, str] | None:
    """The repo and main branch behind a lane name, or None for an unknown one."""
    return next(((r, m) for r, m in gitq._repos(cfg) if lane_name(cfg, r) == lane), None)


def check_log(cfg: Config, phase: str, repo: Path) -> Path:
    """Where ``swarm _lane-check`` writes the check's output."""
    return landing_dir(cfg) / f"{phase}.{gitq._slug(repo)}.log"


def _result_path(cfg: Config, phase: str, repo: Path) -> Path:
    return landing_dir(cfg) / f"{phase}.{gitq._slug(repo)}.result"


def check_cmd(cfg: Config, repo: Path) -> str:
    """``[lanes].check[R]``, else ``check["*"]``, else ``""`` (nothing to run)."""
    table = cfg.lanes_check or {}
    return table.get(lane_name(cfg, repo)) or table.get("*") or ""


def _commons(cfg: Config, lane: str, paths: list[str]) -> list[str]:
    """``paths`` of ``lane`` minus those matching ``[lanes] commons``, matched as
    ``<lane>/<path>`` (``./<path>`` in the umbrella)."""
    globs = cfg.lanes_commons or []
    return [p for p in paths if not any(fnmatch(f"{lane}/{p}", g) for g in globs)]


def _names(repo: Path, *args: str) -> list[str]:
    out = gitq._git(repo, "diff", "--name-only", *args).stdout
    return [p for p in out.splitlines() if p.strip()]


def _moved(cfg: Config, repo: Path, main: str, branch: str) -> list[str]:
    """What R's main gained since ``branch`` was cut, commons left out."""
    base = gitq._git(repo, "merge-base", main, branch).stdout.strip()
    return _commons(cfg, lane_name(cfg, repo), _names(repo, f"{base}..{main}"))


def _contains(repo: Path, main: str, branch: str) -> bool:
    return gitq._git(
        repo, "merge-base", "--is-ancestor", main, branch, check=False
    ).returncode == 0


# -- the landing lock ------------------------------------------------------
#: This process's flocks: repo slug -> (phase, fd). A flock lives on its open
#: file description, so a second open in this same process conflicts too.
_FDS: dict[str, tuple[str, int]] = {}


def _holder(st: state_mod.State, repo: Path, phase: str) -> str | None:
    """The other phase whose landing holds ``repo``, if any."""
    key = str(repo)
    return next((p for p, repos in sorted(st.landing.items())
                 if p != phase and key in repos), None)


def _flock(cfg: Config, repo: Path, phase: str, st: state_mod.State) -> bool:
    """Hold ``repo``'s landing flock for ``phase``. False: someone else has it."""
    slug = gitq._slug(repo)
    held = _FDS.get(slug)
    if held is not None:
        owner, fd = held
        if owner == phase:
            return True
        if str(repo) in st.landing.get(owner, {}):
            return False
        _unflock(repo)  # its entry went (merged, dropped): a stale fd
    landing_dir(cfg).mkdir(parents=True, exist_ok=True)
    fd = os.open(landing_dir(cfg) / f"{slug}.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    _FDS[slug] = (phase, fd)
    return True


def _unflock(repo: Path) -> None:
    held = _FDS.pop(gitq._slug(repo), None)
    if held is not None:
        os.close(held[1])


def _set(cfg: Config, phase: str, repo: Path, **entry) -> None:
    with state_mod.transaction(cfg) as st:
        st.landing.setdefault(phase, {})[str(repo)] = {**entry, "at": time.time()}


def _release(cfg: Config, phase: str, repo: Path) -> None:
    """Give up ``phase``'s hold on ``repo``: its merge landed."""
    with state_mod.transaction(cfg) as st:
        mine = st.landing.get(phase, {})
        mine.pop(str(repo), None)
        if not mine:
            st.landing.pop(phase, None)
    if _FDS.get(gitq._slug(repo), ("",))[0] == phase:
        _unflock(repo)


# -- integrate -------------------------------------------------------------
def integrate(
    cfg: Config, phase: str, log: Log, pushes: dict[Path, gitq.PushResult] | None = None
) -> str:
    """:func:`gitq.integrate` with lanes on. Besides its statuses, returns
    :data:`gitq.LANE_CHECKING` (a check runs; land others meanwhile),
    :data:`gitq.LANE_WAITING` (another phase's landing holds a repo this one
    changed), :data:`gitq.LANE_CONFLICT` and :data:`gitq.LANE_RED` (a resolver's
    job, on the worktree :func:`blocked` names).

    Nothing lands until every changed repo is ready, so a phase that spans two
    repos never lands one half while the other half's check is red."""
    branch = f"swarm/{phase}"
    repos = gitq._repos(cfg)
    changed = [(r, m) for r, m in repos
               if gitq._branch_exists(r, branch) and gitq._commits_ahead(r, m, branch) > 0]
    st = state_mod.read(cfg)
    for repo, _main in changed:
        other = _holder(st, repo, phase)
        if other is not None or not _flock(cfg, repo, phase, st):
            log.line(f"LANE-WAIT {phase} {repo.name} holder={other or 'another process'}")
            _drop_idle_flocks(cfg, phase, changed)
            return gitq.LANE_WAITING
    mine = st.landing.get(phase, {})
    pending = False
    for repo, main in changed:
        status = _prepare(cfg, phase, repo, main, mine.get(str(repo)) or {}, log)
        if status == gitq.LANE_CHECKING:
            pending = True
        elif status != gitq.MERGED:
            return status
    if pending:
        return gitq.LANE_CHECKING
    _flag_undeclared(cfg, phase, changed, log)
    for repo, main in repos:
        result = gitq._integrate_one(cfg, repo, main, phase, log, pushes)
        if result != gitq.MERGED:
            return result
        if (repo, main) in changed:
            _release(cfg, phase, repo)
    gitq._rmtree_mirror(cfg, phase)
    return gitq.MERGED


def _drop_idle_flocks(cfg: Config, phase: str, changed: list[tuple[Path, str]]) -> None:
    """A waiting phase keeps no flock it holds no entry for."""
    st = state_mod.read(cfg)
    for repo, _main in changed:
        if str(repo) not in st.landing.get(phase, {}) and _FDS.get(
            gitq._slug(repo), ("",)
        )[0] == phase:
            _unflock(repo)


def _prepare(cfg: Config, phase: str, repo: Path, main: str, entry: dict, log: Log) -> str:
    """Bring one changed repo to ready: :data:`gitq.MERGED` means *may land now*."""
    branch = f"swarm/{phase}"
    stage = entry.get("stage")
    if stage == CHECKING:
        stage = _collect(cfg, phase, repo, entry, log)
        if stage == CHECKING:
            return gitq.LANE_CHECKING
    if stage == RED:
        return gitq.LANE_RED
    if stage == CONFLICT:
        return gitq.LANE_CONFLICT
    if stage == PASSED:
        if _contains(repo, main, branch):
            return gitq.MERGED
        log.line(f"LANE-MAIN-MOVED {phase} {repo.name}")  # someone outside the swarm
    elif stage != AGAIN:
        moved = _moved(cfg, repo, main, branch)
        if not moved:
            return gitq.MERGED
        log.line(f"LANE-CATCH-UP {phase} {repo.name} main gained {len(moved)} file(s)")
    return _catch_up(cfg, phase, repo, main, entry.get("base"), log)


def _catch_up(
    cfg: Config, phase: str, repo: Path, main: str, base: str | None, log: Log
) -> str:
    """Merge main into the phase's worktree of ``repo``, then start the check."""
    branch = f"swarm/{phase}"
    base = base or gitq._git(repo, "merge-base", main, branch).stdout.strip()
    wt = gitq._wt_for(cfg, repo, phase)
    merged = gitq._git(wt, "merge", "--no-edit", main, check=False)
    if merged.returncode != 0 and not gitq._auto_resolve(cfg, wt, phase, log):
        _set(cfg, phase, repo, stage=CONFLICT, base=base)
        log.line(f"LANE-CONFLICT {phase} {repo.name} in {wt}")
        return gitq.LANE_CONFLICT
    cmd = check_cmd(cfg, repo)
    if not cmd:
        _set(cfg, phase, repo, stage=PASSED, base=base)
        log.line(f"LANE-CHECK-NONE {phase} {repo.name}")
        return gitq.MERGED
    _result_path(cfg, phase, repo).unlink(missing_ok=True)
    pid = _spawn_check(cfg, phase, repo)
    _set(cfg, phase, repo, stage=CHECKING, base=base, pid=pid)
    log.line(f"LANE-CHECK-START {phase} {repo.name} pid={pid}")
    return gitq.LANE_CHECKING


def _spawn_check(cfg: Config, phase: str, repo: Path) -> int | None:
    bin_ = os.environ.get("SWARM_BIN", "swarm")  # the test seam, as for `_poke-done`
    argv = f"exec {bin_} _lane-check {shlex.quote(phase)} {shlex.quote(lane_name(cfg, repo))}"
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", argv], cwd=str(cfg.project_dir),
            env=launch_mod.detached_env(cfg), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except (OSError, ValueError):
        return None
    return proc.pid


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if os.waitpid(pid, os.WNOHANG) != (0, 0):
            return False  # our child, and it has exited
    except ChildProcessError:
        pass  # started by another process (`swarm up`): ask the kernel
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _collect(cfg: Config, phase: str, repo: Path, entry: dict, log: Log) -> str:
    """The stage a running check has reached: its result, or a check that died
    without one counted red."""
    try:
        result = _result_path(cfg, phase, repo).read_text(encoding="utf-8").strip()
    except OSError:
        result = ""
    if not result:
        if _alive(entry.get("pid")):
            return CHECKING
        result = "fail"
        log.line(f"LANE-CHECK-LOST {phase} {repo.name} pid={entry.get('pid')}")
    stage = PASSED if result == "ok" else RED
    _set(cfg, phase, repo, **{**entry, "stage": stage})
    log.line(f"LANE-CHECKED {phase} {repo.name} {result}")
    return stage


# -- the check (``swarm _lane-check``) ------------------------------------
def run_check(cfg: Config, phase: str, lane: str) -> int:
    """Run ``lane``'s check in ``phase``'s worktree through the build semaphore,
    write its log and result, and poke ``lane-checked``. A timeout is red."""
    found = repo_for(cfg, lane)
    if found is None:
        return 2
    repo, _main = found
    wt = gitq._wt_for(cfg, repo, phase)
    cmd = check_cmd(cfg, repo)
    landing_dir(cfg).mkdir(parents=True, exist_ok=True)
    ok = False
    with check_log(cfg, phase, repo).open("w", encoding="utf-8") as fh:
        fh.write(f"# swarm _lane-check {phase} {lane}: `{cmd}` in {wt}\n")
        fh.flush()
        with buildsem.slot(cfg):
            ok = _run(cmd, wt, fh, cfg.lanes_check_timeout_s)
        fh.write(f"# result: {'ok' if ok else 'fail'}\n")
    result = _result_path(cfg, phase, repo)
    tmp = result.with_suffix(".tmp")
    tmp.write_text("ok\n" if ok else "fail\n", encoding="utf-8")
    tmp.replace(result)
    launch_mod._poke_fifo(cfg, f"lane-checked {phase} {lane} {'ok' if ok else 'fail'}\n")
    return 0 if ok else 1


def _run(cmd: str, wt: Path, fh, timeout: float) -> bool:
    if not cmd:
        return True
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=wt, stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as exc:  # the worktree went: nothing was tested, so red
        fh.write(f"\n# could not start: {exc}\n")
        return False
    try:
        return proc.wait(timeout=timeout) == 0
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
                proc.wait(timeout=_KILL_GRACE_S)
                break
            except (ProcessLookupError, subprocess.TimeoutExpired):
                continue
        fh.write(f"\n# timed out after {timeout}s: counted red\n")
        return False


# -- resolving -------------------------------------------------------------
def blocked(cfg: Config, phase: str) -> tuple[Path, Path, str] | None:
    """``(repo, worktree, stage)`` of the repo whose landing waits on a resolver."""
    for key, entry in sorted(state_mod.read(cfg).landing.get(phase, {}).items()):
        if entry.get("stage") in (RED, CONFLICT):
            repo = Path(key)
            return repo, gitq._wt_for(cfg, repo, phase), entry["stage"]
    return None


def resolve_ready(wt: Path) -> bool:
    """A resolver finished on ``wt``: no merge in progress, nothing uncommitted."""
    return not (gitq._merge_in_progress(wt) or gitq._dirty(wt))


def retest(cfg: Config, phase: str) -> None:
    """``swarm resolved``: every held repo merges main again and is re-checked."""
    with state_mod.transaction(cfg) as st:
        for entry in st.landing.get(phase, {}).values():
            if entry.get("stage") in (RED, CONFLICT):
                entry["stage"] = AGAIN


def landed_since(repo: Path, main: str, base: str | None) -> list[str]:
    """The phases merged into ``main`` since ``base``, from ``swarm/<phase>`` merges."""
    if not base:
        return []
    out = gitq._git(repo, "log", "--merges", "--format=%s", f"{base}..{main}", check=False)
    seen = [m.group(1) for m in map(_MERGED_BRANCH.search, out.stdout.splitlines()) if m]
    return list(dict.fromkeys(reversed(seen)))


def resolver_brief(cfg: Config, phase: str) -> tuple[Path, str] | None:
    """The worktree a lane resolver works in, and its instruction line."""
    hit = blocked(cfg, phase)
    if hit is None:
        return None
    repo, wt, stage = hit
    main = gitq._repo_main(cfg, repo)
    entry = state_mod.read(cfg).landing[phase][str(repo)]
    landed = ", ".join(landed_since(repo, main, entry.get("base"))) or "none recorded"
    prompt = _prompt_file()
    head = (f"Read {prompt} and follow it exactly, in its lane mode. You work in the"
            f" worktree {wt}, on the branch swarm/{phase}, before it lands in"
            f" {repo.name}'s {main}. Work only in {wt}; never touch {repo}, the"
            f" canonical checkout.")
    if stage == CONFLICT:
        return wt, (f"{head} Mode: catch-up conflict. Merging {main} into swarm/{phase}"
                    f" conflicted. Phases landed in {repo.name} since this branch was"
                    f" cut: {landed}. Resolve the merge keeping both sides, commit it on"
                    f" the branch, then run `swarm resolved {phase}`.")
    rerun = f"cd {shlex.quote(str(wt))} && {check_cmd(cfg, repo)}"
    return wt, (f"{head} Mode: semantic conflict. {main} merged cleanly, but the"
                f" combined tree fails the lane check. Its log: {check_log(cfg, phase, repo)}."
                f" Phases landed in {repo.name} since this branch was cut: {landed}."
                f" Re-run the check with: {rerun}. Make the combined tree pass without"
                f" dropping either phase's behaviour, commit on the branch, then run"
                f" `swarm resolved {phase}`.")


def _prompt_file() -> Path:
    from . import resolver  # the resolver imports launch too; keep this lazy
    return resolver.prompt_path("resolver.md")


# -- outside the lane (D6) -------------------------------------------------
def _undeclared_log(cfg: Config) -> Path:
    return landing_dir(cfg) / "undeclared.jsonl"


def _snapshot(snap: list[str]) -> frozenset[Touch] | None:
    try:
        return frozenset(parse_touch(t, {t.split("/", 1)[0]}) for t in snap)
    except LaneError:
        return None


def _flag_undeclared(
    cfg: Config, phase: str, changed: list[tuple[Path, str]], log: Log
) -> None:
    """Note the files ``phase`` changed outside its lane and the commons."""
    lane = _snapshot(state_mod.read(cfg).lanes.get(phase) or [])
    if not lane:
        log.line(f"LANE-UNDECLARED-SKIP {phase} no lane snapshot")
        return
    outside: list[str] = []
    for repo, main in changed:
        name = lane_name(cfg, repo)
        paths = _commons(cfg, name, _names(repo, f"{main}...swarm/{phase}"))
        outside += [f"{name}/{p}" for p in paths
                    if not any(overlaps(Touch(name, tuple(p.split("/"))), t) for t in lane)]
    if not outside or _flagged(cfg, phase, outside):
        return
    note = f"lane: touched outside its declaration: {' '.join(outside)}"
    ledgerw.queue(cfg, phase, {"kind": "record", "phase": phase, "outcome": "note",
                               "note": note, "by": "the swarm, at landing"})
    landing_dir(cfg).mkdir(parents=True, exist_ok=True)
    with _undeclared_log(cfg).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "phase": phase, "paths": outside}) + "\n")
    log.line(f"LANE-UNDECLARED {phase} {' '.join(outside)}"[:600])


def undeclared_since(cfg: Config, since: float) -> list[dict]:
    """Every undeclared-touch flag raised since ``since``, oldest first."""
    try:
        lines = _undeclared_log(cfg).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and float(rec.get("ts") or 0) >= since:
            out.append(rec)
    return out


def _flagged(cfg: Config, phase: str, paths: list[str]) -> bool:
    """Already noted: a landing retried after a hold must not note it twice."""
    return any(r.get("phase") == phase and r.get("paths") == paths
               for r in undeclared_since(cfg, 0.0))
