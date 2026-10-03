"""How fast the swarm finishes phases, read from the project's own git history.

The dashboard used to time everything off this machine's supervisor log and
``done/`` sentinels. Those only know what ran *here*: phases built on another
machine and pulled back would be missing from the chart, and the ETA would be
timed on stale phases. The ledger's git
history knows every phase wherever it was built — the commit that turned its row
``[x]`` — so that is the record every forecast is made from.

Two histories are read, both from the project repo:

* the ledger's, for when each row was ticked (and by how big a commit: one that
  ticks thirty rows at once is bookkeeping, not throughput), and when each row
  was filed (the ETA engine's measure of how campaigns grow while they run). A
  merge commit counts for the lines neither of its parents had: a phase's tick
  is written into the merge that lands it;
* ``.swarm.toml``'s, for how many workers the swarm had at the time, so a pace
  measured at two workers is not promised to one.

``git log -p`` over a ledger with hundreds of commits takes seconds, so the
result is cached in the state dir by the repo's ``HEAD`` and extended
incrementally (``<cached>..HEAD``) when ``HEAD`` moves. Nothing here raises: a
project that is not a git repo simply has no history, and the callers fall back
to what the local records know.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CACHE_NAME = "ledger-history.json"
_CACHE_VERSION = 3

#: A commit that ticks more rows than this closed a campaign by hand or seeded
#: the ledger: the rows are done, but they did not take the time between them.
BULK = 3

#: Only this recent a pace predicts the next phase.
RECENT_S = 7 * 86400
#: At most this many of the latest finishes make the pace…
WINDOW = 20
#: …and fewer than this is not a pace, it is an anecdote.
MIN_PHASES = 5

GIT_TIMEOUT_S = 60.0

_TICK_RE = re.compile(r"^\+\s*[-*]\s+\[([ xX])\]\s+`([^`]+)`")
_WORKERS_RE = re.compile(r"^\+\s*max_workers\s*=\s*(\d+)")
#: A merge's diff is headed ``diff --cc <path>`` and has a column per parent.
_DIFF_RE = re.compile(r"^diff --(?:git a/\S+ b/|(cc) )(\S+)")


@dataclass(frozen=True)
class History:
    """What the project's git history says about finished phases."""

    head: str = ""
    #: phase -> ``(commit time its row last turned [x], rows that commit ticked)``.
    ticks: dict[str, tuple[float, int]] = field(default_factory=dict)
    #: ``(commit time, max_workers)`` each time ``.swarm.toml`` set it, oldest first.
    workers: list[tuple[float, int]] = field(default_factory=list)
    #: phase -> ``(commit time its row first appeared, rows that commit filed)``.
    added: dict[str, tuple[float, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class Pace:
    """The swarm's recent throughput."""

    phases: int
    #: Hours the swarm could work across the sample: wall clock between its
    #: first and last finish, less the holds the local log recorded.
    hours: float
    per_hour: float
    #: Mean ``max_workers`` over the sample, or ``None`` when no history says.
    workers: float | None


# -- reading git ------------------------------------------------------------
def fold(log_text: str, ticks: dict, workers: list, ledger: str, config: str,
         added: dict | None = None) -> None:
    """Apply ``git log --reverse -p --cc -U0`` output to ``ticks``/``workers`` in place.

    A row counts from the commit that turned it ``[x]``: an edit to an already
    ticked row keeps its time, a row turned back to ``[ ]`` loses it. ``added``,
    when given, gets each row's first appearance; a later edit or move of the
    row is not a filing. Of a merge only the lines neither parent had count
    (``++``): the rest was read in the commits it came from.
    """
    ts = 0.0
    path = ""
    merge = False
    new: list[str] = []
    filed: list[str] = []
    added = {} if added is None else added

    def close() -> None:
        for phase in new:
            ticks[phase] = (ts, len(new))
        for phase in filed:
            added[phase] = (ts, len(filed))
        new.clear()
        filed.clear()

    for line in log_text.splitlines():
        if line.startswith("@@@") and not line.startswith("@@@ -"):  # not a merge's hunk
            close()
            try:
                ts = float(line.split()[1])
            except (IndexError, ValueError):
                ts = 0.0
            continue
        m = _DIFF_RE.match(line)
        if m:
            merge, path = bool(m.group(1)), m.group(2)
            continue
        if merge:
            if not line.startswith("++"):
                continue
            line = line[1:]
        if path == ledger:
            m = _TICK_RE.match(line)
            if m is None:
                continue
            box, phase = m.groups()
            if phase not in added and phase not in filed:
                filed.append(phase)
            if box == " ":
                ticks.pop(phase, None)
                if phase in new:
                    new.remove(phase)
            elif phase not in ticks and phase not in new:
                new.append(phase)
        elif path == config:
            m = _WORKERS_RE.match(line)
            if m:
                workers.append((ts, int(m.group(1))))
    close()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _log(repo: Path, rev: str, paths: list[str]) -> str | None:
    # --cc: a plain -p shows nothing for a merge commit, and a phase's tick is
    # written into the merge that lands it.
    proc = _git(repo, "log", "--date-order", "--reverse", "--no-renames", "--no-color",
                "--format=@@@ %ct", "-p", "--cc", "-U0", rev, "--", *paths)
    return proc.stdout if proc is not None and proc.returncode == 0 else None


def _repo_path(repo: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return ""


def load(cfg) -> History:
    """The project's history, from the cache when ``HEAD`` has not moved."""
    project = Path(cfg.project_dir)
    top = _git(project, "rev-parse", "--show-toplevel", "HEAD")
    lines = top.stdout.splitlines() if top is not None and top.returncode == 0 else []
    if len(lines) != 2:
        return History()
    # Diff paths are the repo root's, which the project dir need not be.
    repo, head = Path(lines[0]), lines[1]
    ledger = _repo_path(repo, project / cfg.ledger)
    config = _repo_path(repo, project / ".swarm.toml")
    if not ledger:
        return History()
    paths = [p for p in (ledger, config) if p]
    state_dir = getattr(cfg, "state_dir", None)
    cache = Path(state_dir) / "cache" / CACHE_NAME if state_dir else None
    old = _read_cache(cache, ledger)
    if old is not None and old.head == head:
        return old
    ticks = dict(old.ticks) if old else {}
    workers = list(old.workers) if old else []
    added = dict(old.added) if old else {}
    text = None
    if old is not None and old.head:
        base = _git(repo, "merge-base", "--is-ancestor", old.head, head)
        if base is not None and base.returncode == 0:
            text = _log(repo, f"{old.head}..{head}", paths)
    if text is None:  # first read, or history was rewritten under the cache
        ticks, workers, added = {}, [], {}
        text = _log(repo, head, paths)
        if text is None:
            return History()
    fold(text, ticks, workers, ledger, config, added)
    got = History(head=head, ticks=ticks, workers=workers, added=added)
    _write_cache(cache, ledger, got)
    return got


def _read_cache(path: Path | None, ledger: str) -> History | None:
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != _CACHE_VERSION or raw.get("ledger") != ledger:
            return None
        return History(
            head=str(raw["head"]),
            ticks={str(p): (float(t), int(n)) for p, (t, n) in raw["ticks"].items()},
            workers=[(float(t), int(n)) for t, n in raw["workers"]],
            added={str(p): (float(t), int(n)) for p, (t, n) in raw["added"].items()},
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _write_cache(path: Path | None, ledger: str, got: History) -> None:
    """tmp + ``os.replace``; a lost write only costs the next reader a rebuild."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({
            "version": _CACHE_VERSION, "ledger": ledger, "head": got.head,
            "ticks": {p: list(v) for p, v in got.ticks.items()},
            "workers": [list(w) for w in got.workers],
            "added": {p: list(v) for p, v in got.added.items()},
        }), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# -- the pace -----------------------------------------------------------------
def workers_at(history: list[tuple[float, int]], ts: float) -> int | None:
    """``max_workers`` as the config set it at ``ts``; ``None`` before any record."""
    got = None
    for when, n in history:
        if when > ts:
            break
        got = n
    return got


def _overlap(spans: list[tuple[float, float]], t0: float, t1: float) -> float:
    return sum(max(0.0, min(b, t1) - max(a, t0)) for a, b in spans)


def measure(finished: dict[str, float], bulk: set[str] | frozenset[str],
            idle: list[tuple[float, float]], workers: list[tuple[float, int]],
            now: float) -> Pace | None:
    """The pace of the latest :data:`WINDOW` finishes, or ``None`` when too few.

    ``finished`` is phase -> when it finished, ``bulk`` the rows a bookkeeping
    commit ticked (done, but not throughput). ``idle`` are the spans this
    machine's log says the swarm could not work — paused, held by a usage cap,
    its supervisor down. A span in which a phase finished anyway was not a hold
    for the swarm as a whole (it was working elsewhere), so it is not taken out.
    """
    times = sorted(t for p, t in finished.items()
                   if p not in bulk and now - RECENT_S <= t <= now)[-WINDOW:]
    if len(times) < MIN_PHASES:
        return None
    every = sorted(finished.values())
    held = [(a, b) for a, b in idle if not any(a < t < b for t in every)]
    work = 0.0
    weighted = 0.0
    known = True
    for t0, t1 in zip(times, times[1:]):
        span = max(0.0, (t1 - t0) - _overlap(held, t0, t1))
        n = workers_at(workers, (t0 + t1) / 2)
        known = known and n is not None
        work += span
        weighted += span * (n or 0)
    if work <= 0:
        return None
    hours = work / 3600.0
    return Pace(phases=len(times), hours=hours, per_hour=(len(times) - 1) / hours,
                workers=weighted / work if known else None)


def outlook(pace: Pace, remaining: int, workers: int) -> tuple[float, float]:
    """``(soonest, latest)`` seconds for ``remaining`` phases at ``workers`` now.

    The pace was measured at ``pace.workers``. Throughput is somewhere between
    unchanged by the worker count (a shared bottleneck: one build at a time, the
    merge queue) and proportional to it (every worker independent), so the two
    ends of that are the range. No more workers can help than phases are left.
    """
    rate = pace.per_hour / 3600.0
    same = remaining / rate
    if not pace.workers:
        return same, same
    now = max(1, min(workers or 1, remaining))
    scaled = remaining / (rate * now / pace.workers)
    return min(same, scaled), max(same, scaled)
