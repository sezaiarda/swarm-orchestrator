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
target dir. A build holds a seat and shares one slot; GC needs the inverse — it
holds *every* build slot exclusively while it works, which the kernel grants
only while no build is alive on any of them (set aside as idle or not, started
by an older ``swarm build`` or not). The slots are the machine's, so that is no
build of any swarm: this one's output is all a GC deletes, but the disk the
builds strain is one. It never takes them one at a time: with
two slots, a GC that took slot 0 and then waited ten minutes for a long build
on slot 1 kept slot 0 from every build for those ten minutes, each interval,
and still did not run. :func:`build_gate` instead waits in the build queue
like a build (:func:`buildsem.whole`), holding nothing, and takes all the slots
in one step when the gate is empty; a wait that runs out leaves the queue and
is retried later. If the gate is disabled (``max_concurrent = 0``) it proves
nothing, so GC refuses without ``--force``. As a backstop for a bare ``cargo``
that never went through ``swarm build``, :func:`live_builders` also looks for
live cargo/rustc processes with a cwd inside the worktree root.

**Symlinked caches are the normal case, not an exception.** In a live run every
``cache/target/<repo>`` is a symlink into the canonical repo's own ``target/``
(so a worktree build and an owner build share one cache), and at first
every planner here skipped symlinks — ``swarm gc`` pruned nothing while the
canonical targets grew without bound. A cache entry is now resolved to its real
directory and deduplicated by that path, so a target several links share is
swept once. Resolution is the ONLY way GC reaches into the canonical project: a
resolved root there must be a directory literally named ``target``, and nothing
outside such a root is touched without ``--canonical``.

**It also runs by itself** (:func:`auto`, from the supervisor): at most once per
``[gc].every_s`` and once per long idle stretch, only with the whole build gate
to itself, and never with the opt-in tiers (``--aggressive``,
``--transcripts``, ``--branches``). What it removes is dead by construction:
superseded cargo units (:func:`superseded`), build output unused for
``[gc].keep_days``, ``incremental/`` (workers run ``CARGO_INCREMENTAL=0``),
mirrors and ``tmp/`` dirs no live session owns, and set-aside work under
``refs/swarm-attic`` older than ``[gc].attic_days``.

**Superseded units are what actually fill the disk.** ``cargo sweep --time``
can reclaim nothing on a very large cache, because every byte of it may be hours
old: each phase leaves a full generation of units (many test binaries,
the crate in each feature set, and every contract crate the phase repinned),
and none is ever read again once the phase lands.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import backup as backup_mod
from . import buildsem, gitq
from . import ledger as ledger_mod
from . import operator as operator_mod
from . import ovrecord
from . import state as state_mod
from .config import Config
from .state import State

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

# Profiles whose `incremental/` is dead weight: every swarm session builds with
# CARGO_INCREMENTAL=0, so nothing reads it again — it only ages until swept.
INCREMENTAL_PROFILES = ("debug", "release")

#: A cargo unit touched this recently may belong to a build that is about to
#: link against it, so :func:`superseded` never removes one.
SUPERSEDED_GRACE_S = 600

# A cargo output or fingerprint name: `[lib]<stem>-<16 hex>[.<ext>]`.
_UNIT_RE = re.compile(r"^(?:lib)?(.+)-([0-9a-f]{16})(\.[^/]*)?$")

#: A ``/tmp`` leftover smaller than this is not worth a line in the report.
TMP_REPORT_MIN_BYTES = 256 << 20

#: The supervisor's record of its last automatic gc (read by doctor).
AUTO_RECORD = "gc-auto.json"

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
    gate_timeout_s: float = 300.0  # how long to wait in the build queue
    # Of that wait, the last seconds that hold new builds back (None: [gc].hold_s).
    gate_hold_s: float | None = None
    # Ask cargo-sweep for a dry-run estimate at plan time. The automatic gc skips
    # it: that is a second full walk of every target just to print a number the
    # applied run measures anyway.
    estimate: bool = True


@dataclass
class Target:
    """One thing GC would reclaim, and what it measured."""

    kind: str
    label: str
    op: str  # rmtree | supersede | sweep | branch | prune | discard | attic
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
    # Things worth the owner's eye that GC will never delete itself.
    notes: list[str] = field(default_factory=list)
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
            "notes": self.notes,
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
    live = _live_names(cfg, st)

    # Tiers must not overlap, or the total is a fiction. An orphan cache goes
    # whole, so its sub-profiles must not be proposed again underneath it; and
    # `--aggressive` removes the very profiles an age-based sweep would prune, so
    # a cache it covers is left out of the sweep entirely (the note below says to
    # re-run --sweep afterwards for what is left in the live `debug/` profile).
    orphans = _plan_orphan_caches(plan, cfg, live)
    # An orphan cache goes whole (above), so it is never also swept or pruned.
    gone = {o.resolve() for o in orphans}
    roots = {r: n for r, n in cache_roots(cfg).items() if r not in gone}
    swept_by_aggressive: set[Path] = set()
    if opts.aggressive:
        swept_by_aggressive = _plan_aggressive(plan, roots)
    # Incremental and superseded units go before the sweep in the target list,
    # so the sweep's before/after measures what is left once they are gone.
    planned = _plan_incremental(plan, roots, swept_by_aggressive)
    for root, size in _plan_superseded(plan, cfg, roots, swept_by_aggressive, live).items():
        planned[root] = planned.get(root, 0) + size
    if opts.sweep_days is not None:
        _plan_sweep(plan, roots, opts, swept_by_aggressive, planned)
    if opts.transcripts:
        _plan_transcripts(plan, cfg)
    if opts.branches:
        _plan_branches(plan, cfg, live)
    _plan_mirrors(plan, cfg, st, live)
    _plan_attic(plan, cfg)
    _plan_tmp(plan, cfg, live)
    _report_tmp(plan)

    _note_protected(plan, cfg, live)
    return plan


def _blockers(cfg: Config, opts: GcOptions) -> list[str]:
    """Preconditions :func:`apply` will refuse on. Surfaced at plan time so a dry
    run tells you *before* you type ``--yes``."""
    out: list[str] = []
    if cfg.build_max_concurrent < 1 and not opts.force:
        out.append(
            "[build].max_concurrent is 0 in machine.toml — the build gate is"
            " disabled, so acquiring it proves nothing about whether a build is"
            " running. Set it, stop the swarms, or pass --force."
        )
    builders = live_builders(cfg, opts)
    if builders and not opts.force:
        out.append(
            f"{len(builders)} build process(es) are live under {cfg.wt_dir}:"
            f" {'; '.join(builders[:3])}"
        )
    return out


def _live_names(cfg: Config, st: State) -> set[str]:
    """Every name (phase, ``op-<job>``, ``ovs-<pass>``) whose mirror or ``tmp/``
    dir a live session may be using, or that the swarm still means to land.

    Beyond :func:`_live_phases`: the operator job holding the lease, every
    operator mirror :func:`operator.mirror_plan` keeps or will integrate, and the
    Overseer pass running now.
    """
    names = _live_phases(st)
    if st.operator_phase:
        names |= {st.operator_phase, operator_mod.mirror_name(st.operator_phase)}
    try:
        names |= set(operator_mod.mirror_plan(cfg))
    except (OSError, ValueError):
        pass
    if st.overseer_pass:
        names.add(ovrecord.mirror_name(st.overseer_pass))
    from . import bigpic  # not at the top: bigpic -> ovdigest -> doctor -> gc

    if bigpic.load(cfg).live:
        names.add(bigpic.WINDOW)  # the big-picture session's TMPDIR
    return names


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


def cache_roots(cfg: Config) -> dict[Path, list[str]]:
    """Real target directories behind ``cache/target/*``, each with the cache
    entry names that reach it.

    A symlinked entry is resolved and deduplicated by real path, so a target that
    several entries share is planned — and swept — exactly once. A resolved root
    inside the canonical project is accepted only when it is a directory named
    ``target`` below the project root: that is what :func:`gitq._link_target_cache`
    creates, and it is the guarantee that a mis-pointed link can never aim GC at
    source. A dangling link, or one resolving to a non-directory, is skipped.
    """
    roots: dict[Path, list[str]] = {}
    base = cfg.build_cache_dir
    if not base.is_dir():
        return roots
    project = cfg.project_dir.resolve()
    for entry in sorted(base.iterdir()):
        try:
            real = entry.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not real.is_dir():
            continue
        if entry.is_symlink() and real.is_relative_to(project):
            if real.name != "target" or real.parent == real or real == project:
                continue
        roots.setdefault(real, []).append(entry.name)
    return roots


def _root_label(names: list[str]) -> str:
    return f"cache/target/{'+'.join(names)}"


def _plan_incremental(
    plan: GcPlan, roots: dict[Path, list[str]], skip: set[Path]
) -> dict[Path, int]:
    """``<profile>/incremental`` in every real target: always dead weight here.

    Every swarm session builds with ``CARGO_INCREMENTAL=0`` (see
    :func:`launch.session_env`), so nothing reads these again; any already on
    disk predates that setting or came from an owner build. A root
    ``--aggressive`` already empties is skipped, so nothing is counted twice.
    Returns the bytes planned per root, which the sweep subtracts.
    """
    planned: dict[Path, int] = {}
    for root, names in roots.items():
        if root in skip:
            continue
        for profile in INCREMENTAL_PROFILES:
            path = root / profile / "incremental"
            if not path.is_dir() or path.is_symlink():
                continue
            size = du(path)
            planned[root] = planned.get(root, 0) + size
            plan.targets.append(
                Target(
                    kind="incremental",
                    label=f"{_root_label(names)}/{profile}/incremental",
                    op="rmtree",
                    detail="incremental state; every session builds with CARGO_INCREMENTAL=0",
                    path=str(path),
                    repo="+".join(names),
                    before=size,
                    estimate=size,
                )
            )
    return planned


def _plan_superseded(
    plan: GcPlan,
    cfg: Config,
    roots: dict[Path, list[str]],
    aggressive: set[Path],
    live: set[str],
) -> dict[Path, int]:
    """Superseded cargo units in every profile of every real target (:func:`superseded`).

    Always planned, like ``incremental/``: what it removes can never be read by
    a build again. A profile ``--aggressive`` drops whole is skipped so nothing
    is counted twice. Returns the bytes planned per root, which the sweep
    subtracts.
    """
    planned: dict[Path, int] = {}
    for root, names in roots.items():
        for profile in _profiles(root):
            if root in aggressive and profile.name in AGGRESSIVE_SUBDIRS:
                continue
            dead = superseded(cfg, profile, live)
            if not dead:
                continue
            estimate = sum(_freed(p) for p in dead)
            planned[root] = planned.get(root, 0) + estimate
            before = max(0, du(profile) - du(profile / "incremental"))
            plan.targets.append(
                Target(
                    kind="superseded",
                    label=f"{_root_label(names)}/{profile.name}",
                    op="supersede",
                    detail=f"{len(dead)} superseded build unit file(s); only the live generation stays",
                    path=str(profile),
                    repo="+".join(names),
                    before=before,
                    estimate=min(estimate, before),
                )
            )
    return planned


def _profiles(root: Path) -> list[Path]:
    """Profile directories (``debug``, ``release`` ...) directly under a target."""
    try:
        return sorted(
            p for p in root.iterdir()
            if not p.is_symlink() and (p / ".fingerprint").is_dir()
        )
    except OSError:
        return []


def superseded(
    cfg: Config, profile: Path, live: set[str], now: float | None = None
) -> list[Path]:
    """Paths of the cargo units in one profile dir no build will read again.

    A shared cache piles up one *generation* of units per phase and never drops
    the old ones: every phase edits its own crate (each crate unit and all 100+
    test binaries re-link) and most repin the shared contract to a fresh git
    tag, which changes the hash of the contract crates and so of everything
    built on them. The worktree path is *not* in the hash, so a third-party
    unit one phase built is reused by the next.

    A unit (``.fingerprint/<pkg>-<hash>``, its ``deps/*-<hash>*`` outputs and
    ``build/<pkg>-<hash>``) is kept when it is, or is a dependency of, a unit
    that is

    - touched within :data:`SUPERSEDED_GRACE_S`, or has no readable fingerprint;
    - built by a live phase (its ``deps/*.d`` names ``wt/<live>/``);
    - the newest of its kind (package, target, features, profile, flags) — but
      never a workspace crate a finished phase built (its sources are relative
      in the ``.d``): cargo decides freshness of path crates by mtime, so a
      fresh worktree always rebuilds them and that copy is dead by construction.

    Everything else goes as a whole unit, fingerprint and outputs together, so
    cargo simply rebuilds it if asked. Output files whose fingerprint is gone
    are dead too once past the grace window.
    """
    now = time.time() if now is None else now
    fresh = now - SUPERSEDED_GRACE_S
    units: dict[str, Path] = {}
    outputs: dict[str, list[Path]] = {}
    newest: dict[str, float] = {}
    for sub in (".fingerprint", "deps", "build", "examples"):
        for entry in _scan(profile / sub):
            match = _UNIT_RE.match(entry.name)
            if match is None:
                continue
            h = match.group(2)
            if sub == ".fingerprint":
                if entry.is_dir(follow_symlinks=False):
                    units[h] = Path(entry.path)
                continue
            outputs.setdefault(h, []).append(Path(entry.path))
            newest[h] = max(newest.get(h, 0.0), _mtime(entry))

    wt = [str(cfg.wt_dir) + os.sep, str(cfg.wt_dir.resolve()) + os.sep]
    by_fp: dict[str, str] = {}
    deps: dict[str, list[int]] = {}
    roots: set[str] = set()
    kinds: dict[tuple, tuple[float, str]] = {}
    for h, fp in units.items():
        stamp, key, unit_deps, ok = newest.get(h, 0.0), [fp.name.rsplit("-", 1)[0]], [], False
        for entry in _scan(fp):
            stamp = max(stamp, _mtime(entry))
            if entry.name.endswith(".json"):
                try:
                    data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
                    unit_deps += [int(d[-1]) for d in data.get("deps", [])]
                except (OSError, ValueError, TypeError, IndexError, AttributeError):
                    continue
                ok = True
                key.append(entry.name)
                key += [json.dumps(data.get(k), sort_keys=True) for k in (
                    "features", "profile", "target", "compile_kind", "rustflags")]
            elif not entry.name.startswith(("dep-", "invoked.")):
                try:
                    by_fp[Path(entry.path).read_text(encoding="utf-8").strip()] = h
                except OSError:
                    pass
        deps[h] = unit_deps
        if stamp >= fresh or not ok:
            roots.add(h)
        if not ok:
            continue
        out, local = _dep_info(outputs.get(h, []))
        built_by = next(
            (out[len(p):].split(os.sep, 1)[0] for p in wt if out.startswith(p)), None
        )
        if built_by is not None and built_by in live:
            roots.add(h)
        elif not (local and built_by is not None):
            kind = tuple(key)
            if kind not in kinds or stamp > kinds[kind][0]:
                kinds[kind] = (stamp, h)
    roots |= {h for _, h in kinds.values()}

    keep: set[str] = set()
    stack = list(roots)
    while stack:
        h = stack.pop()
        if h in keep:
            continue
        keep.add(h)
        for fp_hash in deps.get(h, []):
            dep = by_fp.get((fp_hash & (2**64 - 1)).to_bytes(8, "little").hex())
            if dep is not None:
                stack.append(dep)

    dead: list[Path] = []
    for h, fp in units.items():
        if h not in keep:
            dead += [fp, *outputs.get(h, [])]
    for h, paths in outputs.items():
        if h not in units and newest.get(h, now) < fresh:
            dead += paths
    return sorted(dead)


def _scan(path: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _mtime(entry: os.DirEntry) -> float:
    try:
        return entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return 0.0


def _dep_info(paths: list[Path]) -> tuple[str, bool]:
    """``(output path, sources are relative)`` from a unit's ``deps/*.d``.

    rustc writes the output path as cargo gave it — through the building
    worktree's ``target`` link — and a workspace crate's sources relative to its
    root, where a registry or git dependency's are absolute.
    """
    for path in paths:
        if path.suffix != ".d":
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                line = fh.readline(65536)
        except OSError:
            continue
        out, sep, rest = line.partition(": ")
        if not sep:
            continue
        srcs = rest.split()
        return out, bool(srcs) and not srcs[0].startswith(os.sep)
    return "", False


def _freed(path: Path) -> int:
    """Bytes removing ``path`` gives back; a still-linked file gives back none."""
    try:
        info = path.lstat()
    except OSError:
        return 0
    if path.is_dir() and not path.is_symlink():
        return du(path) + info.st_blocks * 512
    return info.st_blocks * 512 if info.st_nlink <= 1 else 0


def _plan_sweep(
    plan: GcPlan,
    roots: dict[Path, list[str]],
    opts: GcOptions,
    aggressive: set[Path],
    planned: dict[Path, int],
) -> None:
    """``cargo sweep --time <days>`` over every real target directory, once each.

    ``cargo-sweep`` only understands a *cargo project* (it reads ``cargo
    metadata`` to find the target directory), and our caches are bare target
    directories with no manifest above them. So each one is swept through a
    throwaway shim project whose ``target`` is a symlink to the cache — which is
    exactly the shape a real worktree has, and the shape cargo-sweep was written
    against. The shim carries its own ``[workspace]`` table so ``cargo metadata``
    cannot wander into a surrounding workspace.

    ``before`` excludes the incremental dirs and superseded units planned ahead
    of it, because they are gone by the time the sweep runs; the estimate is
    capped by the same figure, since cargo-sweep's own count includes them.
    """
    days = int(opts.sweep_days or 0)
    binary = _cargo_sweep()
    if not roots:
        return
    if binary is None:
        plan.blockers.append(
            "cargo-sweep is not installed (cargo install cargo-sweep) — cannot"
            " plan --sweep"
        )
        return
    for root, names in roots.items():
        if root in aggressive:
            plan.protected.append(
                f"{root} — not swept: --aggressive already drops the profiles a"
                " sweep would prune; re-run with --sweep alone afterwards for"
                " what is left in debug/"
            )
            continue
        before = max(0, du(root) - planned.get(root, 0))
        if before == 0:
            continue  # an empty cache (a repo with no Rust built yet): nothing to sweep
        estimate = _sweep(binary, root, days, dry_run=True) if opts.estimate else 0
        plan.targets.append(
            Target(
                kind="cargo-sweep",
                label=_root_label(names),
                op="sweep",
                detail=f"drop build artefacts unused for >{days}d",
                path=str(root),
                repo="+".join(names),
                before=before,
                estimate=min(max(0, estimate), before),
                extra={"days": days},
            )
        )


def _plan_aggressive(plan: GcPlan, roots: dict[Path, list[str]]) -> set[Path]:
    """Whole rebuildable profiles under each real target (:data:`AGGRESSIVE_SUBDIRS`).

    These cost a full cold rebuild of what they held, which is why they are behind
    a flag — but ``debug/incremental`` alone can reach tens of GiB in a long run, and
    nothing in a phase build reads ``release/``, ``doc/`` or ``semver-checks/``.
    """
    touched: set[Path] = set()
    for root, names in roots.items():
        for sub in AGGRESSIVE_SUBDIRS:
            path = root / sub
            if not path.is_dir() or path.is_symlink():
                continue
            size = du(path)
            touched.add(root)
            plan.targets.append(
                Target(
                    kind="aggressive",
                    label=f"{_root_label(names)}/{sub}",
                    op="rmtree",
                    detail="rebuildable build artefacts (cold rebuild next time)",
                    path=str(path),
                    repo="+".join(names),
                    before=size,
                    estimate=size,
                )
            )
    return touched


def _plan_mirrors(plan: GcPlan, cfg: Config, st: State, live: set[str]) -> None:
    """``wt/<name>`` mirrors no live phase, job or pass owns.

    A mirror normally goes with its merge or discard; one is left behind when a
    removal timed out (``gitq._gc`` never raises) or a run was killed. The rule
    for which may go is exactly :func:`gitq.reconcile`'s, so GC never decides
    anything ``swarm up`` would not: a name already in ``done`` is discarded, and
    so is one with no completion sentinel (an interrupted build, an abandoned
    job, a stale pass). A finished-but-unmerged phase is left for reconcile to
    land, and an interrupted ledger phase's mirror that holds work is left for
    its next launch to resume. Removal goes through :func:`gitq.discard` — the
    same worktree-remove, branch-delete and prune the integrator uses, with its
    generous timeout, and it archives anything not on main.
    """
    if not cfg.wt_dir.is_dir():
        return
    sentinels = gitq.sentinel_done(cfg)
    try:
        phases = set(ledger_mod.load(cfg.project_dir / cfg.ledger))
    except (OSError, ValueError):
        phases = set()
    for entry in sorted(cfg.wt_dir.iterdir()):
        name = entry.name
        if not entry.is_dir() or entry.is_symlink() or name in live:
            continue
        if name not in st.done and sentinels.get(name) in gitq.DONE_INTEGRATE:
            plan.protected.append(
                f"{entry} — {name} finished but is not merged yet; `swarm up` lands it"
            )
            continue
        if name in phases and name not in st.done and gitq.holds_work(cfg, name):
            plan.protected.append(
                f"{entry} — {name} was interrupted; its next launch resumes this work"
            )
            continue
        size = du(entry)
        plan.targets.append(
            Target(
                kind="orphan-mirror",
                label=f"wt/{name}",
                op="discard",
                detail="no live phase, job or pass owns this mirror",
                path=str(entry),
                before=size,
                estimate=size,
                extra={"name": name},
            )
        )


def _plan_attic(plan: GcPlan, cfg: Config) -> None:
    """``refs/swarm-attic`` refs older than ``[gc].attic_days``.

    The attic is where discarded work goes instead of being deleted; after the
    grace period nobody is coming back for it. Dropping the ref frees no disk by
    itself (git's own gc collects the objects later), so nothing is estimated.
    """
    cutoff = time.time() - cfg.gc_attic_days * 86400
    for repo in _workspace_repos(cfg):
        for ref, made in gitq.attic_refs(repo):
            if made >= cutoff:
                continue
            plan.targets.append(
                Target(
                    kind="attic",
                    label=f"{repo.name}:{ref}",
                    op="attic",
                    detail=f"set aside more than {cfg.gc_attic_days} days ago",
                    repo=str(repo),
                    extra={"ref": ref},
                )
            )


def _plan_tmp(plan: GcPlan, cfg: Config, live: set[str]) -> None:
    """``tmp/<name>`` session temp dirs whose session is over.

    Each is normally dropped when its session's work lands; this catches the
    ones a crash, a reaped pane or an in-place run left behind, and any plain
    file a session wrote beside its TMPDIR. A symlink is never planned.
    """
    if not cfg.tmp_dir.is_dir():
        return
    for entry in sorted(cfg.tmp_dir.iterdir()):
        if entry.is_symlink() or entry.name in live:
            continue
        size = du(entry) if entry.is_dir() else entry.lstat().st_blocks * 512
        plan.targets.append(
            Target(
                kind="stale-tmp",
                label=f"tmp/{entry.name}",
                op="rmtree",
                detail="TMPDIR of a session that is no longer running",
                path=str(entry),
                before=size,
                estimate=size,
            )
        )


def tmp_offenders(
    root: Path = Path("/tmp"), min_bytes: int = TMP_REPORT_MIN_BYTES
) -> list[tuple[Path, int, str]]:
    """Big ``/tmp`` entries of ours that look swarm-made, largest first.

    Report-only, for GC and doctor alike: ``/tmp`` is shared with the owner's own
    sessions and tools, so nothing here is ever deleted automatically. "Looks
    swarm-made" is deliberately narrow — a cargo target (``CACHEDIR.TAG``, which
    cargo writes into every target dir, or a ``debug/`` profile), a GC sweep shim,
    or Claude Code's per-user dir (every session's diff cache lands there when no
    ``TMPDIR`` is set).
    """
    out: list[tuple[Path, int, str]] = []
    uid = os.getuid()
    try:
        entries = list(root.iterdir())
    except OSError:
        return out
    for entry in entries:
        try:
            info = entry.lstat()
        except OSError:
            continue
        if info.st_uid != uid or not entry.is_dir() or entry.is_symlink():
            continue
        name = entry.name
        if name == f"claude-{uid}":
            why = "Claude Code's temp dir (bash output, edit diffs)"
        elif name.startswith("swarm-"):
            why = "a swarm helper's scratch dir"
        elif (entry / "CACHEDIR.TAG").is_file() or (entry / "debug" / ".fingerprint").is_dir():
            why = "a cargo target dir (CARGO_TARGET_DIR under /tmp)"
        else:
            continue
        size = du(entry)
        if size >= min_bytes:
            out.append((entry, size, why))
    return sorted(out, key=lambda t: t[1], reverse=True)


def _report_tmp(plan: GcPlan) -> None:
    for path, size, why in tmp_offenders():
        plan.notes.append(f"{path} — {human(size)}, {why}; not removed (shared /tmp)")


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
        tmp = cfg.session_tmp(phase)
        if tmp is not None and tmp.exists():
            plan.protected.append(f"{tmp} — {phase}'s session TMPDIR")
    if not plan.opts.canonical:
        plan.protected.append(
            f"{cfg.project_dir} — the canonical workspace (pass --canonical)"
        )


# -- applying -------------------------------------------------------------
class _NoLog:
    """Stand-in for a :class:`~.logutil.Log` when ``swarm gc`` runs by hand."""

    def line(self, text: str) -> None:
        pass


def apply(plan: GcPlan, log=None) -> GcPlan:
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
        _apply_in_gate(plan, log)
    _drop_remote_attic(plan, log)
    return plan


def _drop_remote_attic(plan: GcPlan, log=None) -> None:
    """Delete the origin backup of every attic ref the plan dropped. After the
    build gate is released: each is a network call, and no build should wait on
    a slow or dead remote."""
    if not plan.applied:
        return
    for target in plan.targets:
        if target.op == "attic" and not target.error:
            backup_mod.drop_attic(Path(target.repo or ""), target.extra["ref"], log or _NoLog())


def _apply_in_gate(plan: GcPlan, log=None) -> None:
    """The deleting half of :func:`apply`; the caller holds the build gate."""
    cfg, opts = plan.cfg, plan.opts
    log = log or _NoLog()
    # Re-read INSIDE the gate: a phase can have started between planning and
    # here, and its worktree/branch must then be off limits.
    live = _live_names(cfg, state_mod.read(cfg))
    roots = set(cache_roots(cfg))
    for target in plan.targets:
        reason = _protected_reason(cfg, target, opts, live, roots)
        if reason is not None:
            target.error = f"skipped: {reason}"
            target.reclaimed = 0
            continue
        try:
            _execute(cfg, target, log, live)
        except (OSError, subprocess.SubprocessError, gitq.GitError) as exc:
            target.error = str(exc)
            target.reclaimed = 0
    plan.applied = True


def _execute(cfg: Config, target: Target, log=None, live: set[str] | None = None) -> None:
    """Run one target's action and measure what it actually reclaimed."""
    if target.op == "rmtree":
        path = Path(target.path or "")
        # A session can leave a plain file beside its TMPDIR, and rmtree raises
        # on one. A symlink still goes to rmtree, which refuses it.
        if path.is_symlink() or path.is_dir():
            shutil.rmtree(path, ignore_errors=False)
        else:
            path.unlink()
        target.after = du(path)
    elif target.op == "supersede":
        # Re-derived inside the gate, never replayed from the plan: a build
        # since planning may have made a planned unit live again.
        path = Path(target.path or "")
        if live is None:
            live = _live_names(cfg, state_mod.read(cfg))
        for dead in superseded(cfg, path, live):
            if dead.is_dir() and not dead.is_symlink():
                shutil.rmtree(dead, ignore_errors=True)
            else:
                dead.unlink(missing_ok=True)
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
    elif target.op == "attic":
        repo = Path(target.repo or "")
        with gitq.repo_lock(cfg, repo):
            gitq._git(repo, "update-ref", "-d", target.extra["ref"])
        target.after = 0
    elif target.op == "discard":
        # Worktrees, branches and the mirror dir, the way a failed phase is
        # rolled back — never a bare rmtree, which would strand git metadata.
        gitq.discard(cfg, str(target.extra["name"]), log or _NoLog())
        target.after = du(Path(target.path or ""))
    else:  # pragma: no cover - the op set is closed
        raise OSError(f"unknown gc op {target.op!r}")
    target.reclaimed = max(0, target.before - (target.after or 0))


def _protected_reason(
    cfg: Config,
    target: Target,
    opts: GcOptions,
    live: set[str],
    roots: set[Path] | None = None,
) -> str | None:
    """The last line of defence, re-checked for every target at delete time.

    Duplicating the planner's judgement is deliberate: a plan can be minutes old,
    hand-edited, or replayed, and none of the rules below are ones we want to
    depend on an earlier function having remembered.
    """
    if target.op == "attic":
        if not str(target.extra.get("ref", "")).startswith(gitq.ATTIC + "/"):
            return "not a swarm-attic ref"
        return None
    if target.op in ("branch", "prune"):
        phase = str(target.extra.get("branch", ""))[len("swarm/") :]
        if phase and phase in live:
            return f"{phase} started again and is in flight"
        return None
    if target.op == "discard":
        name = str(target.extra.get("name", ""))
        if cfg.session_tmp(name) is None or target.path != str(cfg.wt_dir / name):
            return "not a mirror under wt/"
        if name in live:
            return f"{name} is live again"
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
    if target.op == "supersede":
        real = cache_roots(cfg).keys() if roots is None else roots
        if path.parent.resolve() not in real or not (path / ".fingerprint").is_dir():
            return "not a cargo profile directory of a build cache"

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
        cfg.tmp_dir,
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
        tmp = cfg.session_tmp(phase)
        if tmp is not None and (path == tmp or _under(path, tmp)):
            return f"{phase} is live and {tmp} is its TMPDIR"

    if _under(path, cfg.project_dir) and not _under(path, cfg.state_dir):
        # The one way in without --canonical: a real target dir the build cache
        # links to *right now* (re-resolved at apply time, never trusted from the
        # plan), and never the target dir itself — only what is inside it, or a
        # sweep, which prunes files within it.
        roots = cache_roots(cfg).keys() if roots is None else roots
        in_cache = any(
            (path == r and target.op == "sweep") or (path != r and _under(path, r))
            for r in roots
        )
        if not opts.canonical and not in_cache:
            return "it is inside the canonical project (pass --canonical)"
    return None


# -- the automatic run ----------------------------------------------------
#: :func:`auto` outcomes. ``busy`` is the only one worth retrying soon.
AUTO_DONE, AUTO_BUSY, AUTO_FAILED = "done", "busy", "failed"


@dataclass
class AutoResult:
    """What one automatic gc did."""

    outcome: str
    freed: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    errors: int = 0
    detail: str = ""


def auto(cfg: Config, log=None) -> AutoResult:
    """One unattended gc: the dead-weight tiers only, never during a build.

    The build gate is waited for up to ``[gc].wait_s`` and held for the whole
    run, planning included — so the sizes it measures are the sizes it deletes,
    and no build can start mid-sweep. Waiting is what lets it run at all on a
    busy swarm: its place in the build queue gets it the gate the moment no
    build is alive, and for the last ``[gc].hold_s`` of the wait no build
    queued after it starts, so the gate does empty; the next build queues
    behind one short sweep. It holds no slot while it waits. A gate still busy
    after the wait, or a live cargo/rustc under a tree it would touch, is
    ``busy``: the caller retries later. The opt-in tiers are never used,
    and neither is ``--canonical``; the canonical project is reached only through
    the build-cache links (:func:`cache_roots`).
    """
    opts = GcOptions(
        yes=True, sweep_days=cfg.gc_keep_days, gate_timeout_s=float(cfg.gc_wait_s),
        estimate=False,
    )
    if cfg.build_max_concurrent < 1:
        return AutoResult(AUTO_FAILED,
                          detail="[build].max_concurrent is 0 in machine.toml — no gate to hold")
    try:
        with build_gate(cfg, opts):
            busy = live_builders(cfg, opts)
            if busy:
                return AutoResult(AUTO_BUSY, detail=busy[0])
            plan = plan_gc(cfg, opts)
            _apply_in_gate(plan, log)
    except GcRefused as exc:
        return AutoResult(AUTO_BUSY, detail=str(exc))
    _drop_remote_attic(plan, log)
    kinds: dict[str, int] = {}
    for t in plan.targets:
        kinds[t.kind] = kinds.get(t.kind, 0) + (t.reclaimed or 0)
    # Every failing target by name, in the log and in the record: a bare count
    # cannot tell a standing failure from a new one without a second run.
    errors = [f"{t.label}: {t.error}" for t in plan.targets if t.error]
    for line in errors:
        (log or _NoLog()).line(f"GC-AUTO-ERROR {line}")
    return AutoResult(
        AUTO_DONE,
        freed=plan.reclaimed_bytes,
        by_kind=kinds,
        errors=len(errors),
        detail="; ".join(errors),
    )


def write_record(cfg: Config, result: AutoResult, reason: str, now: float) -> None:
    """Persist the last automatic run (doctor reads it; the scheduler resumes
    its clock from it after a restart)."""
    data = {
        "ts": now,
        "reason": reason,
        "outcome": result.outcome,
        "freed": result.freed,
        "by_kind": result.by_kind,
        "errors": result.errors,
        "detail": result.detail,
    }
    path = cfg.state_dir / AUTO_RECORD
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_record(cfg: Config) -> dict | None:
    """The last automatic run's record, or ``None`` if it never ran."""
    try:
        data = json.loads((cfg.state_dir / AUTO_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# -- the build gate -------------------------------------------------------
@contextmanager
def build_gate(cfg: Config, opts: GcOptions):
    """Hold EVERY ``swarm build`` slot for the duration of the block: the
    machine's, so no build of any swarm runs while this one's output is swept.

    The wait is a place in the build queue (:func:`buildsem.whole`), not a hold
    on the slots already free: builds keep using those until the gate is empty,
    and every slot is then taken in one step. For ``opts.gate_timeout_s`` in
    all, of which the last ``opts.gate_hold_s`` (``[gc].hold_s``) hold back the
    builds queued behind it; after that it leaves the queue rather than keep
    the machine's builds waiting on it.
    """
    if cfg.build_max_concurrent < 1:
        if not opts.force:
            raise GcRefused(
                "[build].max_concurrent is 0 in machine.toml: there is no gate to acquire, so GC"
                " cannot prove no build is running (pass --force)"
            )
        yield []
        return

    hold_s = cfg.gc_hold_s if opts.gate_hold_s is None else opts.gate_hold_s
    try:
        with buildsem.whole(cfg, opts.gate_timeout_s, float(hold_s)) as fds:
            yield fds
    except buildsem.Busy as exc:  # raised on the way in only: nothing was held
        raise GcRefused(str(exc)) from None


def live_builders(cfg: Config, opts: GcOptions | None = None) -> list[str]:
    """Live cargo/rustc processes building inside the swarm's trees.

    The build gate only sees builds routed through ``swarm build``. A worker that
    types ``cargo build`` directly holds no slot at all, so this walks ``/proc``
    for the compiler processes themselves and matches on their cwd. Best-effort by
    construction: a process we cannot read is skipped rather than assumed idle,
    which is why the gate — not this — is the real interlock.

    A cache that resolves into a canonical repo's ``target/`` is written by the
    owner's own builds there too, which no gate sees — so that repo is watched
    as well whenever GC could sweep its target.
    """
    roots = [cfg.wt_dir]
    if opts is not None and opts.canonical:
        roots.append(cfg.project_dir)
    roots.extend(r.parent for r in cache_roots(cfg) if not _under(r, cfg.state_dir))
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

    if plan.notes:
        out.append("")
        out.append(f"left for you ({len(plan.notes)}, report only):")
        for item in plan.notes:
            out.append(f"  {item}")

    if plan.protected and (verbose or not plan.applied):
        out.append("")
        out.append(f"protected ({len(plan.protected)}, never touched):")
        for item in plan.protected:
            out.append(f"  {item}")
    return "\n".join(out)
