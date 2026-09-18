"""``swarm gc`` — reclaim the disk a long-running swarm quietly eats.

Nothing in this codebase has ever pruned anything. :func:`gitq.discard` removes a
phase's *worktree link* and branch; the shared per-repo cargo cache that
:func:`gitq._link_target_cache` points every worktree's ``target/`` at is never
touched, by design — reusing it is the whole reason parallel worktree builds do
not each cold-compile the dependency graph. It just grows, forever. On the live
run ``cache/target/`` can reach tens of GiB and keeps climbing, a large
share of it ``debug/incremental`` and much of the rest reclaimable by
``cargo sweep --time 1``; the worktrees themselves stay small.

Two rules shape this module.

**Nothing is deleted unless you ask.** :func:`plan_gc` measures and estimates and
returns a :class:`GcPlan`; only :func:`apply` deletes, and only when
``opts.yes`` is set. ``--dry-run`` is not a mode you opt into — it is what
happens by default.

**The protected set is not negotiable.** ``done/`` is the only durable record of
which phases finished (:func:`gitq.sentinel_done` rebuilds the whole ``done`` map
from it on restart, so removing one sentinel silently re-runs or mis-integrates a
phase); ``state.json``, the FIFO and the two lock dirs are live IPC; a busy or
parked phase's worktree has a worker inside it; a ``.fingerprint`` directory
deleted *in isolation* desynchronises cargo and converts the next build into a
cold compile of the entire dependency graph — precisely the memory blow-up
:mod:`buildsem` exists to prevent. And nothing under the canonical project is
touched at all without an explicit ``--canonical``.

**Build-safety interlock.** GC must not race a build that is mid-write in a
target dir. :mod:`buildsem` takes ONE slot and then ``exec``s; GC needs the
inverse — it acquires *every* ``[build].max_concurrent`` slot, works, and releases
them. Acquisition is in **ascending** slot order, the same order
:func:`buildsem._try_once` scans, so GC can never deadlock against a waiting
builder (a builder holds at most one slot and never blocks while holding one). If
the gate is disabled (``max_concurrent = 0``) it proves nothing, so GC refuses
without ``--force``. As a backstop for a bare ``cargo`` that never went through
``swarm build``, :func:`live_builders` also looks for live cargo/rustc processes
with a cwd inside the worktree root.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import gitq
from . import state as state_mod
from .config import Config
from .state import State

_POLL_S = 0.5
_SWEEP_TIMEOUT_S = 600.0
_GIT_TIMEOUT_S = 60.0

# Build artefacts `--aggressive` will drop from a repo's shared target cache.
# `debug/incremental` is a pure recompile accelerator that cargo rebuilds from
# nothing; `release`, `doc` and `semver-checks` are whole profiles a phase build
# does not use. `debug/` itself is NEVER listed — that is the live profile.
AGGRESSIVE_SUBDIRS = ("debug/incremental", "release", "doc", "semver-checks")

# Directories whose deletion in isolation breaks the next build rather than
# merely slowing it: cargo reads these to decide what NOT to rebuild.
NEVER_DELETE_NAMES = (".fingerprint",)

_SIZE_RE = re.compile(r"([\d.]+)\s*(B|[KMGT]iB)")
_UNITS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30, "TiB": 1 << 40}


class GcRefused(RuntimeError):
    """GC will not run: a safety precondition is not met."""


@dataclass
class GcOptions:
    """What the caller asked for. Every destructive knob defaults to off."""

    yes: bool = False  # actually delete; the default is a dry run
    sweep_days: int | None = None  # `cargo sweep --time N` per repo cache
    aggressive: bool = False  # also drop AGGRESSIVE_SUBDIRS
    transcripts: bool = False  # also drop orphaned ~/.claude/projects dirs
    branches: bool = False  # also delete merged swarm/* branches + prune
    canonical: bool = False  # allow touching anything under the project dir
    force: bool = False  # proceed despite a disabled gate / live builders
    gate_timeout_s: float = 300.0


@dataclass
class Target:
    """One thing GC would reclaim, and what it measured."""

    kind: str
    label: str
    op: str  # rmtree | sweep | branch | prune
    detail: str
    path: str | None = None
    repo: str | None = None
    before: int = 0  # bytes on disk when the plan was made
    estimate: int = 0  # bytes the plan expects to reclaim
    after: int | None = None  # measured by apply()
    reclaimed: int | None = None  # measured by apply()
    error: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "op": self.op,
            "detail": self.detail,
            "path": self.path,
            "repo": self.repo,
            "before": self.before,
            "estimate": self.estimate,
            "after": self.after,
            "reclaimed": self.reclaimed,
            "error": self.error,
            "extra": self.extra,
        }


@dataclass
class GcPlan:
    """The full proposal: what would go, what is protected, what blocks it."""

    cfg: Config
    opts: GcOptions
    targets: list[Target] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(t.estimate for t in self.targets)

    @property
    def reclaimed_bytes(self) -> int:
        return sum(t.reclaimed or 0 for t in self.targets)

    def to_dict(self) -> dict:
        """JSON view. ``cfg`` is deliberately omitted — it is a live object, not
        a finding, and dumping it would leak every resolved path into the log."""
        return {
            "applied": self.applied,
            "dry_run": not self.opts.yes,
            "targets": [t.to_dict() for t in self.targets],
            "protected": self.protected,
            "blockers": self.blockers,
            "total_bytes": self.total_bytes,
            "reclaimed_bytes": self.reclaimed_bytes,
        }


# -- planning -------------------------------------------------------------
def plan_gc(cfg: Config, opts: GcOptions, st: State | None = None) -> GcPlan:
    """Measure what could be reclaimed. Deletes nothing, ever.

    Every tier is opt-in past the first: an orphaned cache directory belongs to a
    repo that no longer exists and is pure dead weight, so it is always proposed;
    a sweep, the aggressive profiles, transcripts and branches each need their own
    flag because each trades a future rebuild (or a session history) for space.
    """
    if st is None:
        st = state_mod.read(cfg)
    plan = GcPlan(cfg=cfg, opts=opts)
    plan.blockers = _blockers(cfg, opts)
    live = _live_phases(st)

    # Tiers must not overlap, or the total is a fiction. An orphan cache goes
    # whole, so its sub-profiles must not be proposed again underneath it; and
    # `--aggressive` removes the very profiles an age-based sweep would prune, so
    # a cache it covers is left out of the sweep entirely (the note below says to
    # re-run --sweep afterwards for what is left in the live `debug/` profile).
    orphans = _plan_orphan_caches(plan, cfg, live)
    swept_by_aggressive: set[Path] = set()
    if opts.aggressive:
        swept_by_aggressive = _plan_aggressive(plan, cfg, orphans)
    if opts.sweep_days is not None:
        _plan_sweep(plan, cfg, opts.sweep_days, orphans, swept_by_aggressive)
    if opts.transcripts:
        _plan_transcripts(plan, cfg)
    if opts.branches:
        _plan_branches(plan, cfg, live)

    _note_protected(plan, cfg, live)
    return plan


def _blockers(cfg: Config, opts: GcOptions) -> list[str]:
    """Preconditions :func:`apply` will refuse on. Surfaced at plan time so a dry
    run tells you *before* you type ``--yes``."""
    out: list[str] = []
    if cfg.build_max_concurrent < 1 and not opts.force:
        out.append(
            "[build].max_concurrent is 0 — the build gate is disabled, so"
            " acquiring it proves nothing about whether a build is running."
            " Set it, stop the swarm, or pass --force."
        )
    builders = live_builders(cfg, opts)
    if builders and not opts.force:
        out.append(
            f"{len(builders)} build process(es) are live under {cfg.wt_dir}:"
            f" {'; '.join(builders[:3])}"
        )
    return out


def _live_phases(st: State) -> set[str]:
    """Phases whose worktree must not be touched: in a slot, parked (a worker is
    alive off-grid on its branch), waiting, or queued for integration."""
    return (
        {s.phase for s in st.busy_slots() if s.phase}
        | set(st.parked)
        | set(st.waiting)
        | set(st.integ_queue)
        | ({st.integ_blocked} if st.integ_blocked else set())
    )


def _plan_orphan_caches(plan: GcPlan, cfg: Config, live: set[str]) -> set[Path]:
    """Per-repo target caches whose repo is gone.

    The cache is keyed by :func:`gitq._slug` of the repo directory name, so a repo
    that was renamed, removed, or dropped from ``[git].repos`` leaves a cache
    nobody will ever read again. Two independent signals must agree before one is
    proposed: the slug matches no repo in the workspace *and* no live worktree's
    ``target`` symlink resolves into it. The second check is what keeps a cache
    that is merely idle between phases — the normal, valuable state — safe.
    """
    orphans: set[Path] = set()
    root = cfg.build_cache_dir
    if not root.is_dir():
        return orphans
    known = {gitq._slug(repo) for repo in _workspace_repos(cfg)}
    linked = _target_links(cfg)
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name in known:
            continue
        if entry.resolve() in linked:
            plan.protected.append(
                f"{entry} — a live worktree's target/ still points at it"
            )
            continue
        size = du(entry)
        orphans.add(entry)
        plan.targets.append(
            Target(
                kind="orphan-cache",
                label=f"cache/target/{entry.name}",
                op="rmtree",
                detail=f"no repo named {entry.name!r} in the workspace any more",
                path=str(entry),
                before=size,
                estimate=size,
            )
        )
    return orphans


def _plan_sweep(
    plan: GcPlan, cfg: Config, days: int, orphans: set[Path], aggressive: set[Path]
) -> None:
    """``cargo sweep --time <days>`` over every repo's shared target cache.

    ``cargo-sweep`` only understands a *cargo project* (it reads ``cargo
    metadata`` to find the target directory), and our caches are bare target
    directories with no manifest above them. So each one is swept through a
    throwaway shim project whose ``target`` is a symlink to the cache — which is
    exactly the shape a real worktree has, and the shape cargo-sweep was written
    against. The shim carries its own ``[workspace]`` table so ``cargo metadata``
    cannot wander into a surrounding workspace.
    """
    root = cfg.build_cache_dir
    binary = _cargo_sweep()
    if not root.is_dir():
        return
    if binary is None:
        plan.blockers.append(
            "cargo-sweep is not installed (cargo install cargo-sweep) — cannot"
            " plan --sweep"
        )
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.is_symlink() or entry in orphans:
            continue
        if entry in aggressive:
            plan.protected.append(
                f"{entry} — not swept: --aggressive already drops the profiles a"
                " sweep would prune; re-run with --sweep alone afterwards for"
                " what is left in debug/"
            )
            continue
        before = du(entry)
        estimate = _sweep(binary, entry, days, dry_run=True)
        plan.targets.append(
            Target(
                kind="cargo-sweep",
                label=f"cache/target/{entry.name}",
                op="sweep",
                detail=f"drop build artefacts unused for >{days}d",
                path=str(entry),
                repo=entry.name,
                before=before,
                estimate=max(0, estimate),
                extra={"days": days},
            )
        )


def _plan_aggressive(plan: GcPlan, cfg: Config, skip: set[Path]) -> set[Path]:
    """Whole rebuildable profiles under each repo cache (:data:`AGGRESSIVE_SUBDIRS`).

    These cost a full cold rebuild of what they held, which is why they are behind
    a flag — but ``debug/incremental`` alone can reach tens of GiB in a long run, and
    nothing in a phase build reads ``release/``, ``doc/`` or ``semver-checks/``.
    """
    touched: set[Path] = set()
    root = cfg.build_cache_dir
    if not root.is_dir():
        return touched
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.is_symlink() or entry in skip:
            continue
        for sub in AGGRESSIVE_SUBDIRS:
            path = entry / sub
            if not path.is_dir() or path.is_symlink():
                continue
            size = du(path)
            touched.add(entry)
            plan.targets.append(
                Target(
                    kind="aggressive",
                    label=f"cache/target/{entry.name}/{sub}",
                    op="rmtree",
                    detail="rebuildable build artefacts (cold rebuild next time)",
                    path=str(path),
                    repo=entry.name,
                    before=size,
                    estimate=size,
                )
            )
    return touched


def _plan_transcripts(plan: GcPlan, cfg: Config) -> None:
    """``~/.claude/projects`` session directories for worktrees that are gone.

    Claude keys a session directory off the cwd with ``/`` and ``.`` both mapped to
    ``-``, so a worker's transcripts land under the encoding of its worktree path
    and stay there after :func:`gitq.discard` removes the worktree. Liveness is
    decided from the *forward* mapping — encode each existing worktree and keep
    anything matching — because the encoding is lossy and cannot be inverted. Only
    names under the worktree root are ever considered, so the owner's own project
    session can never be selected.
    """
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return
    prefix = transcript_name(cfg.wt_dir) + "-"
    alive = {
        transcript_name(p)
        for p in (cfg.wt_dir.iterdir() if cfg.wt_dir.is_dir() else [])
        if p.is_dir()
    }
    for entry in sorted(projects.iterdir()):
        if not entry.is_dir() or not entry.name.startswith(prefix):
            continue
        if any(entry.name == a or entry.name.startswith(a + "-") for a in alive):
            plan.protected.append(f"{entry} — its worktree is still live")
            continue
        size = du(entry)
        plan.targets.append(
            Target(
                kind="transcript",
                label=f"~/.claude/projects/{entry.name}",
                op="rmtree",
                detail="session history for a worktree that no longer exists",
                path=str(entry),
                before=size,
                estimate=size,
            )
        )


def _plan_branches(plan: GcPlan, cfg: Config, live: set[str]) -> None:
    """Leftover ``swarm/*`` branches whose work is already on main, plus a prune.

    A branch is only proposed when its tip is an *ancestor* of the repo's
    integration branch — i.e. the merge already happened and deleting it loses
    nothing. An unmerged branch is left alone and reported as protected: it is
    either a phase still in flight or work that never landed, and both are things
    an owner needs to see rather than have swept up.
    """
    for repo in _workspace_repos(cfg):
        main = gitq._repo_main(cfg, repo)
        for branch in gitq._swarm_branches(repo):
            phase = branch[len("swarm/") :]
            if phase in live:
                plan.protected.append(f"{repo.name}:{branch} — {phase} is in flight")
                continue
            if (cfg.wt_dir / phase).exists():
                plan.protected.append(
                    f"{repo.name}:{branch} — its worktree still exists"
                )
                continue
            if not _merged(repo, branch, main):
                plan.protected.append(
                    f"{repo.name}:{branch} — not merged into {main}; its commits"
                    " would be lost"
                )
                continue
            plan.targets.append(
                Target(
                    kind="branch",
                    label=f"{repo.name}:{branch}",
                    op="branch",
                    detail=f"already merged into {main}",
                    repo=str(repo),
                    extra={"branch": branch},
                )
            )
        plan.targets.append(
            Target(
                kind="worktree-prune",
                label=f"{repo.name}: git worktree prune",
                op="prune",
                detail="drop administrative entries for worktrees already gone",
                repo=str(repo),
            )
        )


def _note_protected(plan: GcPlan, cfg: Config, live: set[str]) -> None:
    """Record the things GC deliberately will not consider, so the dry run is a
    statement of intent rather than a silent omission."""
    plan.protected.append(
        f"{cfg.done_dir} — the sentinels are the only durable record of which"
        " phases finished"
    )
    plan.protected.append(f"{cfg.state_path}, {cfg.fifo_path} — live run state")
    plan.protected.append(
        f"{cfg.git_lock_dir}, {cfg.buildsem_dir} — live locks"
    )
    for phase in sorted(live):
        wt = cfg.wt_dir / phase
        if wt.exists():
            plan.protected.append(f"{wt} — {phase} is in flight")
    if not plan.opts.canonical:
        plan.protected.append(
            f"{cfg.project_dir} — the canonical workspace (pass --canonical)"
        )


# -- applying -------------------------------------------------------------
def apply(plan: GcPlan) -> GcPlan:
    """Execute the plan under the build gate. Refuses unless ``opts.yes``.

    Blockers are re-evaluated here, not trusted from plan time: a build can start
    between planning and applying, and the whole point of the gate is that the
    check and the work happen inside the same lock.
    """
    cfg, opts = plan.cfg, plan.opts
    if not opts.yes:
        raise GcRefused("refusing to delete without --yes (this was a dry run)")
    blockers = _blockers(cfg, opts)
    if blockers:
        raise GcRefused("; ".join(blockers))

    with build_gate(cfg, opts):
        # Re-read INSIDE the gate: a phase can have started between planning and
        # here, and its worktree/branch must then be off limits.
        live = _live_phases(state_mod.read(cfg))
        for target in plan.targets:
            reason = _protected_reason(cfg, target, opts, live)
            if reason is not None:
                target.error = f"skipped: {reason}"
                target.reclaimed = 0
                continue
            try:
                _execute(cfg, target)
            except (OSError, subprocess.SubprocessError, gitq.GitError) as exc:
                target.error = str(exc)
                target.reclaimed = 0
    plan.applied = True
    return plan


def _execute(cfg: Config, target: Target) -> None:
    """Run one target's action and measure what it actually reclaimed."""
    if target.op == "rmtree":
        path = Path(target.path or "")
        shutil.rmtree(path, ignore_errors=False)
        target.after = du(path)
    elif target.op == "sweep":
        path = Path(target.path or "")
        binary = _cargo_sweep()
        if binary is None:
            raise OSError("cargo-sweep disappeared between plan and apply")
        _sweep(binary, path, int(target.extra.get("days", 1)), dry_run=False)
        target.after = du(path)
    elif target.op == "branch":
        repo = Path(target.repo or "")
        with gitq.repo_lock(cfg, repo):
            gitq._git(repo, "branch", "-D", target.extra["branch"], check=False)
        target.after = 0
    elif target.op == "prune":
        repo = Path(target.repo or "")
        with gitq.repo_lock(cfg, repo):
            gitq._git(repo, "worktree", "prune", check=False)
        target.after = 0
    else:  # pragma: no cover - the op set is closed
        raise OSError(f"unknown gc op {target.op!r}")
    target.reclaimed = max(0, target.before - (target.after or 0))


def _protected_reason(
    cfg: Config, target: Target, opts: GcOptions, live: set[str]
) -> str | None:
    """The last line of defence, re-checked for every target at delete time.

    Duplicating the planner's judgement is deliberate: a plan can be minutes old,
    hand-edited, or replayed, and none of the rules below are ones we want to
    depend on an earlier function having remembered.
    """
    if target.op in ("branch", "prune"):
        phase = str(target.extra.get("branch", ""))[len("swarm/") :]
        if phase and phase in live:
            return f"{phase} started again and is in flight"
        return None
    if target.path is None:
        return None
    path = Path(target.path)
    if path == path.parent or path == Path.home():
        return "refusing to delete a filesystem root / home directory"
    if path.name in NEVER_DELETE_NAMES:
        return (
            f"{path.name} in isolation desynchronises cargo — the next build would"
            " cold-compile the whole dependency graph"
        )
    if path.is_symlink():
        return "it is a symlink; deleting through one could escape the state dir"

    protected_exact = {
        cfg.state_dir,
        cfg.done_dir,
        cfg.operator_dir,
        cfg.state_path,
        cfg.lock_path,
        cfg.fifo_path,
        cfg.git_lock_dir,
        cfg.buildsem_dir,
        cfg.log_dir,
        cfg.wt_dir,
        cfg.build_cache_dir,
    }
    if path in protected_exact:
        return "it is a live state directory or file"
    # `operator/` sits beside `done/` for the same reason: an item in it is work
    # the swarm has recorded and not yet carried out, recoverable from nowhere
    # else once its sentinel has been swept.
    for guard in (cfg.done_dir, cfg.operator_dir, cfg.git_lock_dir, cfg.buildsem_dir):
        if _under(path, guard):
            return f"it lives under {guard}"

    for phase in live:
        wt = cfg.wt_dir / phase
        if path == wt or _under(path, wt):
            return f"{phase} is in flight and its worker is inside {wt}"

    if _under(path, cfg.project_dir) and not _under(path, cfg.state_dir):
        if not opts.canonical:
            return "it is inside the canonical project (pass --canonical)"
    return None


# -- the build gate -------------------------------------------------------
@contextmanager
def build_gate(cfg: Config, opts: GcOptions):
    """Hold EVERY ``swarm build`` slot for the duration of the block.

    :func:`buildsem._try_once` scans slots ``0..N-1`` with ``LOCK_NB`` and closes
    the fd the moment one is taken, so a builder never blocks while holding a
    lock. Acquiring in the same ascending order therefore cannot deadlock against
    one — the worst case is that builds queue behind GC, which is the intent. A
    timeout releases everything rather than holding the swarm's builds hostage
    forever.
    """
    if cfg.build_max_concurrent < 1:
        if not opts.force:
            raise GcRefused(
                "[build].max_concurrent is 0: there is no gate to acquire, so GC"
                " cannot prove no build is running (pass --force)"
            )
        yield []
        return

    cfg.buildsem_dir.mkdir(parents=True, exist_ok=True)
    fds: list[int] = []
    deadline = time.monotonic() + opts.gate_timeout_s
    try:
        for i in range(cfg.build_max_concurrent):  # ascending: see the docstring
            fd = os.open(cfg.buildsem_dir / f"slot{i}", os.O_CREAT | os.O_RDWR, 0o644)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        os.close(fd)
                        raise GcRefused(
                            f"build slot {i} was still busy after"
                            f" {opts.gate_timeout_s:.0f}s — a build is running;"
                            " try again later"
                        )
                    time.sleep(_POLL_S)
            fds.append(fd)
        yield fds
    finally:
        for fd in reversed(fds):
            os.close(fd)  # closing releases the flock


def live_builders(cfg: Config, opts: GcOptions | None = None) -> list[str]:
    """Live cargo/rustc processes building inside the swarm's trees.

    The build gate only sees builds routed through ``swarm build``. A worker that
    types ``cargo build`` directly holds no slot at all, so this walks ``/proc``
    for the compiler processes themselves and matches on their cwd. Best-effort by
    construction: a process we cannot read is skipped rather than assumed idle,
    which is why the gate — not this — is the real interlock.
    """
    roots = [cfg.wt_dir]
    if opts is not None and opts.canonical:
        roots.append(cfg.project_dir)
    out: list[str] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if comm not in ("cargo", "rustc", "rustdoc"):
            continue
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        if any(_under(cwd, root) for root in roots):
            out.append(f"pid {entry.name} {comm} in {cwd}")
    return out


# -- helpers --------------------------------------------------------------
def du(path: Path) -> int:
    """Bytes on disk under ``path``, never following a symlink.

    Symlinks are counted as links, not as their targets: a worktree's ``target``
    is a symlink into the shared cache, and following it would both double-count
    the cache and — far worse — invite a walker to wander out of the tree it was
    asked about.

    Size is *allocation* (``st_blocks``), never apparent size, and a zero-block
    file therefore contributes zero. That is not a detail: a live
    cache can hold large sparse fixtures whose blocks are all holes, and reading
    their ``st_size`` can report a cache as several times larger than it occupies.
    A GC that overstates what it can reclaim is worse than one that finds
    nothing. ``st_size`` is used only where the platform has no ``st_blocks``.
    """
    if path.is_symlink() or not path.exists():
        return 0
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    blocks = getattr(info, "st_blocks", None)
                    total += info.st_size if blocks is None else blocks * 512
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except OSError:
            continue
    return total


def transcript_name(path: Path) -> str:
    """The ``~/.claude/projects`` directory name for a cwd: ``/`` and ``.`` to ``-``."""
    return str(path).replace("/", "-").replace(".", "-")


def _under(child: Path, parent: Path) -> bool:
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def _workspace_repos(cfg: Config) -> list[Path]:
    """The umbrella plus every component repo the ``[git].repos`` globs match.

    Deliberately *not* :func:`gitq.discovered_repos`, which returns nothing unless
    ``isolation = worktree``: GC has to answer "which repos exist" even for a run
    that has since been switched back to in-place mode, or every one of its caches
    would look orphaned.
    """
    project = cfg.project_dir.resolve()
    found: dict[str, Path] = {str(project): cfg.project_dir}
    for pattern in cfg.git_repos:
        try:
            matches = cfg.project_dir.glob(pattern)
        except (ValueError, OSError):
            continue
        for p in matches:
            if p.is_dir() and (p / ".git").exists():
                found[str(p.resolve())] = p
    return [found[k] for k in sorted(found)]


def _target_links(cfg: Config, max_depth: int = 3) -> set[Path]:
    """Cache directories a live worktree's ``target`` symlink currently resolves to.

    Scanned only a few levels down: a mirror nests component worktrees at their
    real relative path (``wt/<phase>/pricing/target``), and a bounded walk keeps
    this from descending into a worktree's own ``node_modules``.
    """
    out: set[Path] = set()
    if not cfg.wt_dir.is_dir():
        return out
    stack = [(cfg.wt_dir, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            continue
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            if entry.name == "target" and entry.is_symlink():
                try:
                    out.add(path.resolve())
                except OSError:
                    continue
            elif entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                stack.append((path, depth + 1))
    return out


def _cargo_sweep() -> str | None:
    """Path to ``cargo-sweep``, preferring one on PATH then the cargo bin dir."""
    found = shutil.which("cargo-sweep")
    if found:
        return found
    fallback = Path.home() / ".cargo" / "bin" / "cargo-sweep"
    return str(fallback) if fallback.is_file() else None


def _sweep(binary: str, cache: Path, days: int, dry_run: bool) -> int:
    """Sweep one bare target cache; return the bytes reported reclaim(ed|able).

    See :func:`_plan_sweep` for why a shim project is needed. The shim lives in
    the system temp dir (never inside the state dir, so a crashed run cannot leave
    debris where a later GC would have to reason about it) and its ``target`` link
    is unlinked explicitly before cleanup — belt and braces on top of
    ``rmtree`` already not following directory symlinks.
    """
    shim = Path(tempfile.mkdtemp(prefix="swarm-gc-sweep-"))
    link = shim / "target"
    try:
        (shim / "src").mkdir()
        (shim / "src" / "lib.rs").write_text("", encoding="utf-8")
        (shim / "Cargo.toml").write_text(
            '[package]\nname = "swarm-gc-shim"\nversion = "0.0.0"\n'
            'edition = "2021"\n\n[lib]\npath = "src/lib.rs"\n\n[workspace]\n',
            encoding="utf-8",
        )
        link.symlink_to(cache)
        argv = [binary, "sweep", "--time", str(days)]
        if dry_run:
            argv.append("--dry-run")
        argv.append(str(shim))
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_SWEEP_TIMEOUT_S
        )
        return _parse_size(f"{proc.stdout}\n{proc.stderr}")
    except (OSError, subprocess.SubprocessError):
        return 0
    finally:
        if link.is_symlink():
            link.unlink()
        shutil.rmtree(shim, ignore_errors=True)


def _parse_size(text: str) -> int:
    """Bytes out of a ``cargo-sweep`` report line (``Would clean: 21.43 MiB …``)."""
    match = _SIZE_RE.search(text)
    if not match:
        return 0
    try:
        return int(float(match.group(1)) * _UNITS[match.group(2)])
    except (ValueError, KeyError):
        return 0


def _merged(repo: Path, branch: str, main: str) -> bool:
    """True when ``branch``'s tip is already an ancestor of ``main``."""
    return (
        gitq._git(
            repo,
            "merge-base",
            "--is-ancestor",
            branch,
            main,
            check=False,
            timeout=_GIT_TIMEOUT_S,
        ).returncode
        == 0
    )


def human(n: int | None) -> str:
    """Bytes as a human size (``4.2 GiB``). ``None`` renders as ``-``."""
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"


# -- rendering ------------------------------------------------------------
def render(plan: GcPlan, verbose: bool = False) -> str:
    """The before → after → reclaimed table, for a dry run or an applied plan."""
    out: list[str] = []
    if plan.applied:
        out.append(f"swarm gc — APPLIED, reclaimed {human(plan.reclaimed_bytes)}")
    else:
        out.append(
            "swarm gc — DRY RUN, nothing deleted."
            " Re-run with --yes to reclaim"
            f" {human(plan.total_bytes)}."
        )
    for blocker in plan.blockers:
        out.append(f"  BLOCKED: {blocker}")

    if not plan.targets:
        out.append("  nothing to reclaim")
    else:
        header = ("kind", "target", "before", "after", "reclaimed")
        rows = [
            (
                t.kind,
                t.label,
                human(t.before) if t.before else "-",
                human(t.after) if t.after is not None else "-",
                t.error or human(t.reclaimed if plan.applied else t.estimate),
            )
            for t in plan.targets
        ]
        widths = [max(len(str(r[i])) for r in (header, *rows)) for i in range(5)]
        out.append("")
        out.append("  ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip())
        for row in rows:
            out.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())
        out.append("")
        label = "reclaimed" if plan.applied else "reclaimable"
        total = plan.reclaimed_bytes if plan.applied else plan.total_bytes
        out.append(f"total {label}: {human(total)}")

    if plan.protected and (verbose or not plan.applied):
        out.append("")
        out.append(f"protected ({len(plan.protected)}, never touched):")
        for item in plan.protected:
            out.append(f"  {item}")
    return "\n".join(out)
