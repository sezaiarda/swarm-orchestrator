"""The disk tab: what the swarm is actually eating, and where.

A long run's disk cost is invisible until it isn't. ``swarm doctor`` warns once
the state dir crosses a threshold and ``swarm gc`` prices what it could reclaim,
but neither answers the question the owner actually asks — *what uses what?* On
a long run the honest answer is often a large shared build cache next to a
small set of worktrees: a lopsided split that no threshold alarm conveys and
that changes what you delete.

So this measures, per thing, in plain English: each phase's worktree mirror, the
shared cargo target cache every worktree's ``target/`` symlinks into, the git
object stores those worktrees are cut from, and each part of the state dir. Every
byte is attributed exactly once — the cache and the worktrees live *inside* the
state dir, so "state dir" here means whatever is left after the two big things
above it have been counted.

**It never scans on a timer.** This is a directory walk over a large tree,
and the host it runs on may be a small-memory box whose "freezes" can be the OOM
killer arriving during parallel builds. A dashboard that quietly walks the
build cache every minute is a dashboard that can take the swarm down. The scan
happens when the tab is opened and when the owner asks for it again, on a
niced worker thread, and the panel says how old the number is. A stale figure
you can see the age of beats a fresh one that cost a build.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical

from .data import fmt_ago
from .theme import (
    BAD,
    COLOR,
    INFO,
    MUTED,
    OK,
    Body,
    Panel,
    bar,
    field,
    meter_state,
    paint,
)

#: Wall-clock ceiling for one scan. A warm walk of a large cache measures in
#: under a second; anything near this bound means the tree is pathological (or
#: the page cache is cold under load) and a floor is a better answer than a hang.
DEADLINE_S = 20.0

#: Nothing legitimate here is this deep. A cap means a symlink-free loop (bind
#: mount, recursive junction) costs a bounded walk instead of the whole scan.
MAX_DEPTH = 24

#: How often the walk looks at the clock. Per-entry would double the cost of the
#: cheapest loop in the module; per-directory alone misses a single huge dir.
_CLOCK_EVERY = 2048


# -- measuring ------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    """One thing on disk, what it cost, and what it is in plain English."""

    label: str
    path: str
    size: int = 0
    what: str = ""
    partial: bool = False  # the walk was cut short: this is a floor, not a total


@dataclass(frozen=True)
class Volume:
    """The filesystem the state dir sits on."""

    total: int = 0
    used: int = 0
    free: int = 0

    @property
    def pct_used(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0


@dataclass(frozen=True)
class DiskReport:
    """One scan: every category, largest first, plus the volume it lives on."""

    entries: tuple[Entry, ...] = ()
    volume: Volume = Volume()
    scanned_at: float = 0.0
    elapsed: float = 0.0

    @property
    def total(self) -> int:
        return sum(e.size for e in self.entries)

    @property
    def partial(self) -> bool:
        return any(e.partial for e in self.entries)

    @property
    def largest(self) -> Entry | None:
        return self.entries[0] if self.entries else None


class _Budget:
    """The wall-clock bound shared by every walk in one scan."""

    def __init__(self, seconds: float) -> None:
        self.until = time.monotonic() + max(0.0, seconds)
        self.spent = False

    def expired(self) -> bool:
        if not self.spent and time.monotonic() >= self.until:
            self.spent = True
        return self.spent


def _charge(info, seen: set) -> int:
    """Bytes to bill for one stat, or 0 if this inode was already billed.

    Allocation (``st_blocks``), not apparent size: the same choice
    :func:`swarm_orchestrator.gc.du` makes, and for the same measured reason —
    sparse fixtures reported a repo's cache as many times its real size.

    Only *files* with more than one link go in ``seen``. Every directory has
    ``st_nlink >= 2`` by construction, so tracking those would put the whole tree
    in a set to prove something that cannot happen.
    """
    blocks = getattr(info, "st_blocks", None)
    size = info.st_size if blocks is None else blocks * 512
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        key = (info.st_dev, info.st_ino)
        if key in seen:
            return 0
        seen.add(key)
    return size


def _du(path: Path, budget: _Budget, seen: set, skip: frozenset = frozenset()):
    """``(bytes, partial)`` under ``path``. Never raises, never leaves the tree.

    Symlinks are billed as links and never followed: a worktree's ``target`` is a
    symlink into the shared cache, so following it would both double-count the
    cache and invite the walk out of the subtree it was asked about.

    ``skip`` holds paths already attributed to another category — that is how the
    state dir can be measured *after* the worktrees and the cache that live
    inside it without counting either twice.
    """
    try:
        info = path.lstat()
    except OSError:
        return 0, False
    if not stat.S_ISDIR(info.st_mode):
        return _charge(info, seen), False

    total = _charge(info, seen)
    partial = False
    checked = 0
    stack = [(str(path), 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_DEPTH or budget.expired():
            partial = True
            continue
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.path in skip:
                        continue
                    checked += 1
                    if checked % _CLOCK_EVERY == 0 and budget.expired():
                        return total, True
                    try:
                        sub = entry.stat(follow_symlinks=False)
                    except OSError:
                        partial = True
                        continue
                    total += _charge(sub, seen)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, depth + 1))
        except OSError:
            # Unreadable or vanished mid-walk (a worktree being discarded is
            # normal here). The number becomes a floor, not an error.
            partial = True
    return total, partial


def volume_of(path: Path) -> Volume:
    """Total/used/free for the filesystem holding ``path``.

    One ``statvfs``, no walk — cheap enough for the home line to call directly.
    Climbs to the nearest existing parent, so a state dir that has not been
    created yet still reports the volume it will land on.
    """
    current = Path(path)
    for candidate in (current, *current.parents):
        try:
            usage = shutil.disk_usage(candidate)
        except OSError:
            continue
        return Volume(total=usage.total, used=usage.used, free=usage.free)
    return Volume()


def _repos(cfg) -> list[Path]:
    """The umbrella plus every component repo the ``[git].repos`` globs match.

    Deliberately not :func:`gitq.discovered_repos`, which returns nothing unless
    ``isolation = worktree``: the object stores are on disk either way, and a run
    switched back to in-place mode still has them.
    """
    project = Path(cfg.project_dir)
    found = {str(project.resolve()): project}
    for pattern in getattr(cfg, "git_repos", []) or []:
        try:
            matches = list(project.glob(pattern))
        except (ValueError, OSError):
            continue
        for repo in matches:
            if repo.is_dir() and (repo / ".git").exists():
                found[str(repo.resolve())] = repo
    return [found[k] for k in sorted(found)]


def scan(cfg, deadline_s: float = DEADLINE_S) -> DiskReport:
    """Measure everything this swarm put on disk. Slow, thread-safe, never raises.

    Cheap categories are measured first so a scan that runs out of budget still
    answers the small questions, and the entries come back largest-first because
    the question is always "what is eating the disk", never "what is on it".
    """
    started = time.time()
    budget = _Budget(deadline_s)
    seen: set = set()
    entries: list[Entry] = []
    claimed: set[str] = set()

    def measure(label: str, path: Path, what: str, *, claim: bool = True) -> None:
        size, partial = _du(path, budget, seen)
        if claim:
            claimed.add(str(path))
        entries.append(Entry(label, str(path), size, what, partial))

    state_dir = Path(cfg.state_dir)
    measure(
        "state.json",
        Path(cfg.state_path),
        "the live run: which slot holds which phase, and what is done",
    )
    measure(
        "done/",
        Path(cfg.done_dir),
        "one sentinel per finished phase — the only durable record of what completed",
    )
    measure("recaps/", state_dir / "recaps", "the wrap-up each worker wrote when it finished")
    measure("notes/", state_dir / "notes", "worker notes and the questions they asked the owner")
    measure(
        "notifications",
        state_dir / "notifications.jsonl",
        "the alert feed behind the alerts tab",
    )
    measure(
        "supervisor log",
        Path(cfg.supervisor_log),
        "every event the run produced, appended forever",
    )

    wt_dir = Path(cfg.wt_dir)
    try:
        phases = sorted(p for p in wt_dir.iterdir() if p.is_dir())
    except OSError:
        phases = []
    for wt in phases:
        measure(
            f"worktree {wt.name}",
            wt,
            f"the isolated workspace mirror phase {wt.name} builds in "
            "(its target/ is a symlink, counted under the build cache)",
        )

    cache_dir = Path(cfg.build_cache_dir)
    try:
        repo_caches = sum(1 for p in cache_dir.iterdir() if p.is_dir())
    except OSError:
        repo_caches = 0
    measure(
        "build cache",
        cache_dir,
        f"shared cargo target dir ({repo_caches} repo(s)) — every worktree's target/ "
        "points here so parallel builds don't each cold-compile",
    )

    git_size, git_partial = 0, False
    repos = _repos(cfg)
    biggest_repo, biggest_size = "", 0
    for repo in repos:
        size, partial = _du(repo / ".git", budget, seen)
        git_size += size
        git_partial = git_partial or partial
        if size > biggest_size:
            biggest_repo, biggest_size = repo.name, size
    entries.append(
        Entry(
            "git objects",
            str(Path(cfg.project_dir)),
            git_size,
            f"the .git stores of {len(repos)} repo(s) the worktrees are cut from"
            + (f", largest {biggest_repo}" if biggest_repo else "")
            + " — shared with your own checkout",
            git_partial,
        )
    )

    # Last, and with everything above excluded by path: locks, the FIFO, the
    # build semaphore, turns, config — plus anything a future version drops in
    # the state dir, which will show up here instead of going unmeasured.
    rest, rest_partial = _du(state_dir, budget, seen, skip=frozenset(claimed))
    entries.append(
        Entry(
            "state · rest",
            str(state_dir),
            rest,
            "the rest of the state dir: locks, the control fifo, the build "
            "semaphore, turns (the worktrees and cache above are not counted again)",
            rest_partial,
        )
    )

    entries.sort(key=lambda e: e.size, reverse=True)
    return DiskReport(
        entries=tuple(entries),
        volume=volume_of(state_dir),
        scanned_at=started,
        elapsed=time.time() - started,
    )


# -- rendering ------------------------------------------------------------
def fmt_bytes(n: int | None) -> str:
    """Bytes as a column-width size: ``694G``, ``12.4G``, ``840M``, ``12K``.

    Not :func:`swarm_orchestrator.gc.human`, which renders ``12.4 GiB`` for a CLI
    table; on a bar row the space and the ``iB`` are three columns that say
    nothing. A tenth is shown only for gigabytes and up and only below 100, where
    it is worth 100 MiB of real difference — a tenth of a megabyte is noise.
    """
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "K", "M", "G", "T", "P"):
        # 1023.5, not 1024: the rounding below would otherwise render a hair
        # under a gigabyte as the nonsense "1024M".
        if abs(size) < 1023.5 or unit == "P":
            if unit in ("B", "K", "M"):
                return f"{size:.0f}{unit}"
            return f"{size:.1f}{unit}" if abs(size) < 100 else f"{size:.0f}{unit}"
        size /= 1024.0
    return f"{size:.0f}P"


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def summary(report: DiskReport | None, cfg=None, width: int = 64) -> str:
    """The one-line disk fact for the home screen. Measures nothing.

    Renders the *cached* report the disk tab produced. With no scan yet it falls
    back to the free-space figure alone — one ``statvfs``, no walk — and says
    nothing about swarm usage rather than guessing at it, because the only honest
    alternative would be to walk the build cache from the home screen.
    """
    if report is None or not report.entries:
        vol = volume_of(Path(cfg.state_dir)) if cfg is not None else Volume()
        if not vol.total:
            return "disk not measured"
        return _fit(f"{fmt_bytes(vol.free)} free · swarm usage not measured", width)

    head = f"{fmt_bytes(report.total)} used by this swarm"
    if report.volume.total:
        head += f" · {fmt_bytes(report.volume.free)} free"
    top = report.largest
    full = f"{head} · biggest {top.label} {fmt_bytes(top.size)}" if top and top.size else head
    return _fit(full if len(full) <= width else head, width)


class Disk(Vertical):
    """What the swarm is eating, measured on demand and never on a timer."""

    DEFAULT_CSS = """
    Disk { height: 1fr; }
    Disk Panel { height: 1fr; overflow-y: auto; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # `_disk_`-prefixed for the reason doctor.py spells out: MessagePump and
        # Widget own a pile of short attribute names, and shadowing one makes the
        # framework read ours and fail somewhere that looks unrelated.
        self._disk_report: DiskReport | None = None
        self._disk_error: str = ""
        self._disk_busy = False
        self._disk_shown = False
        # Whether a scan has been *attempted*, not whether one succeeded. A
        # failed scan must not turn the first-show trigger into a retry loop —
        # that is the timer this module exists to not have.
        self._disk_tried = False

    def compose(self) -> ComposeResult:
        with Panel("disk", id="disk-panel"):
            yield Body(id="disk-body")

    def on_mount(self) -> None:
        self._repaint()

    def on_show(self) -> None:
        """First look at the tab is the one free scan. After that it is manual."""
        self._disk_shown = True
        if self._disk_tried:
            self._repaint()
        else:
            self.rescan()

    def on_hide(self) -> None:
        self._disk_shown = False

    # The app calls this every tick; it repaints from cache and never scans.
    def update(self, dash) -> None:  # noqa: D102 - tab contract
        if not self._disk_tried and self.is_on_screen:
            # Belt for the first paint: `Show` is posted on a layout pass, and if
            # this tab is the one the app opens on we may already be visible.
            self.rescan()
            return
        self._repaint()

    def selected_phase(self) -> str | None:  # noqa: D102 - tab contract
        return None

    @property
    def report(self) -> DiskReport | None:
        """The last completed scan — what the home line renders from."""
        return self._disk_report

    def rescan(self) -> None:
        """Ask for a fresh measurement. The only other trigger is the first show."""
        if self._disk_busy:
            return
        self._disk_busy = True
        self._disk_tried = True
        self._repaint()
        self._spawn()

    @work(thread=True, exclusive=True, group="disk")
    def _spawn(self) -> None:
        try:
            # Linux's nice() applies to the calling thread, so this yields to the
            # workers' compilers rather than competing with them for a host that
            # has been OOM-killed doing exactly this before. Best effort: a
            # sandbox may refuse, and the scan is still worth running.
            os.nice(10)
        except OSError:
            pass
        try:
            report, err = scan(self.app.cfg), ""
        except Exception as exc:  # noqa: BLE001 - a probe must never take the app down
            report, err = None, f"{type(exc).__name__}: {exc}"
        self.app.call_from_thread(self._finish, report, err)

    def _finish(self, report: DiskReport | None, err: str) -> None:
        self._disk_busy = False
        self._disk_error = err
        if report is not None:
            self._disk_report = report
        self._repaint()

    def _repaint(self) -> None:
        """Redraw from the cached report. Pure formatting: no I/O on this path.

        NOT named ``_render`` — that is Textual's own method, and shadowing it
        takes the tab down with ``'NoneType' object has no attribute
        'render_strips'``.
        """
        try:
            body = self.query_one("#disk-body", Body)
            panel = self.query_one("#disk-panel", Panel)
        except Exception:  # noqa: BLE001 - not mounted yet
            return

        for cls in ("-ok", "-warn", "-bad"):
            panel.remove_class(cls)

        if self._disk_busy:
            body.update(
                paint("  measuring…", MUTED)
                + "\n\n"
                + paint(
                    "  walking the worktrees, the shared cargo cache and the\n"
                    "  state dir. this is niced, so a building swarm wins.",
                    MUTED,
                )
            )
            panel.set_title("disk", "scanning")
            return

        report = self._disk_report
        if report is None:
            body.update(
                paint("  " + (self._disk_error or "not measured yet"),
                      BAD if self._disk_error else MUTED)
                + "\n\n"
                + paint("  press ", MUTED) + paint("r", OK)
                + paint(" to measure. it is a real directory walk over tens of\n"
                        "  gigabytes, so it never runs on a timer — only when asked.", MUTED)
            )
            panel.set_title("disk", "not measured")
            return

        vol = report.volume
        vol_state = meter_state(vol.pct_used) if vol.total else MUTED
        vol_line = (
            f"{fmt_bytes(vol.free)} free of {fmt_bytes(vol.total)} · "
            f"{vol.pct_used:.0f}% used {bar(vol.used, vol.total, 12)}"
            if vol.total
            else "unavailable"
        )
        lines = [
            field("total", f"{fmt_bytes(report.total)} used by this swarm",
                  state=INFO, width=10),
            field("volume", vol_line, state=vol_state, width=10),
            "",
        ]

        top = report.entries[0].size if report.entries else 0
        shown = 0
        for entry in report.entries:
            if not entry.size:
                continue
            shown += 1
            share = 100.0 * entry.size / report.total if report.total else 0.0
            lines.append(
                f"[b]{entry.label:<16}[/b] {bar(entry.size, top, 12)} "
                # One decimal: with a 47G cache in the list, every other row
                # rounds to a flat 0% and the ranking stops saying anything.
                f"{fmt_bytes(entry.size):>6} {share:5.1f}%"
                + (paint("  partial", MUTED) if entry.partial else "")
            )
            lines.append(f"  [{COLOR[MUTED]}]{entry.what}[/]")
        if not shown:
            lines.append(paint("  nothing measurable on disk yet", MUTED))

        body.update("\n".join(lines))
        if vol.total:
            panel.add_class(f"-{vol_state}")
        panel.set_title(
            "disk",
            f"measured {fmt_ago(report.scanned_at)} · r to rescan"
            + (" · partial" if report.partial else ""),
        )
