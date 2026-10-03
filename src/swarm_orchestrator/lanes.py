"""Lanes: what a ledger row edits, and a scheduler that never lets two rows edit it at once.

A row used to be scheduled by its ``dir:`` alone, so two rows in one repo were a
mutex even when they touched unrelated files, and a cap of one per repo starved
every busy repo. A **touch** names what a row edits instead:

    billing/src/http/bridge.rs     one file of a repo
    frontend/src/**                    everything under a dir (``frontend/**``: the repo)
    frontend/tests/unit/jade-*.test.ts  one ``*`` in the last segment
    ./docs/x.md                     the umbrella (lane ``.``; ``./**``: all of it)
    @live-box                       something that is not a file

A row's **lane** is its set of touches. Two lanes collide when any pair of their
touches may name the same file (D3, conservative: a ``**`` also matches zero
segments). :func:`pick` (D4) walks the ready rows in ledger order and launches
each one whose lane collides with nothing in flight; a row that cannot launch
*reserves* its lane, so a later overlapping row can never overtake it while a
later disjoint one still goes. A row with no touches keeps the old behaviour via
:func:`legacy`: ``<dir>/**``, a per-repo mutex.

A touch that matches a ``[lanes] commons`` glob is never part of a lane. A row
may name such a file, and it holds nothing there: :func:`owned` decides it, and
:func:`collide` and :func:`pick` ask it, so no caller compares commons itself.

Pure and stdlib-only: no I/O, no clock, deterministic output for a given input.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from itertools import zip_longest
from typing import NamedTuple

DEEP = "**"


class LaneError(ValueError):
    """A touch that does not follow the grammar, or names an unknown lane."""


@dataclass(frozen=True, order=True)
class Touch:
    """One thing a row edits: a lane (repo, ``.`` or ``@resource``) and a path in it."""

    lane: str
    segments: tuple[str, ...] = ()

    @property
    def is_resource(self) -> bool:
        """True for ``@name`` touches, which have no path."""
        return self.lane.startswith("@")

    def __str__(self) -> str:
        return self.lane if self.is_resource else "/".join((self.lane, *self.segments))


def parse_touch(text: str, known_lanes: Iterable[str]) -> Touch:
    """Parse one touch in its source form, or raise :class:`LaneError` saying why."""
    raw = text.strip().strip("`").strip()
    known = set(known_lanes)
    if raw.startswith("/"):
        raise LaneError(f"touch {raw!r} is an absolute path; start it with a repo or './'")
    lane, _, rest = raw.partition("/")
    if lane.startswith("@"):
        if "/" in raw:
            raise LaneError(f"touch {raw!r}: a resource takes no path")
        if lane not in known:
            raise LaneError(f"touch {raw!r}: unknown resource {lane!r}")
        return Touch(lane)
    if lane not in known:
        raise LaneError(f"touch {raw!r}: unknown lane {lane!r}")
    if not rest and "/" not in raw:
        raise LaneError(f"touch {raw!r} has no path; write '{lane}/**' for the whole lane")
    segments = tuple(rest.split("/"))
    for i, seg in enumerate(segments):
        _check_segment(raw, seg, last=i == len(segments) - 1)
    return Touch(lane, segments)


def _check_segment(raw: str, seg: str, *, last: bool) -> None:
    if not seg:
        raise LaneError(f"touch {raw!r} has an empty path segment")
    if seg in (".", ".."):
        raise LaneError(f"touch {raw!r}: {seg!r} segments are not allowed")
    if seg == DEEP:
        if not last:
            raise LaneError(f"touch {raw!r}: '**' may only be the last segment")
        return
    if DEEP in seg:
        raise LaneError(f"touch {raw!r}: '**' must be a whole segment, not {seg!r}")
    if "*" in seg and not last:
        raise LaneError(f"touch {raw!r}: '*' may only appear in the last segment")
    if seg.count("*") > 1:
        raise LaneError(f"touch {raw!r}: at most one '*' per segment, got {seg!r}")


def _segment_overlaps(p: str, q: str) -> bool:
    if "*" not in p and "*" not in q:
        return p == q
    if "*" not in p:
        p, q = q, p
    head, _, tail = p.partition("*")
    if "*" not in q:
        return q.startswith(head) and q.endswith(tail) and len(q) >= len(head) + len(tail)
    head2, _, tail2 = q.partition("*")
    return (head.startswith(head2) or head2.startswith(head)) and (
        tail.endswith(tail2) or tail2.endswith(tail))


def overlaps(a: Touch, b: Touch) -> bool:
    """Whether two touches may name the same file or resource."""
    if a.lane != b.lane:
        return False
    if a.is_resource:
        return True
    for p, q in zip_longest(a.segments, b.segments):
        if p == DEEP or q == DEEP:
            return True
        if p is None or q is None or not _segment_overlaps(p, q):
            return False
    return True


def owned(touches: Iterable[Touch], commons: Iterable[str] = ()) -> frozenset[Touch]:
    """The touches a row holds: ``touches`` without those matching a ``commons``
    glob. A touch is matched as written, ``<lane>/<path>`` (``./<path>`` in the
    umbrella), the way landing matches a changed path, so ``repo/**`` is held
    whole even with a commons file under it. A resource is always held."""
    globs = list(commons)
    return frozenset(t for t in touches
                     if t.is_resource or not any(fnmatch(str(t), g) for g in globs))


def collide(set_a: Iterable[Touch], set_b: Iterable[Touch],
            commons: Iterable[str] = ()) -> tuple[Touch, Touch] | None:
    """The first overlapping ``(from a, from b)`` pair in sorted order, or None.
    A touch matching ``commons`` is in no pair (:func:`owned`)."""
    globs = list(commons)
    right = sorted(owned(set_b, globs))
    for a in sorted(owned(set_a, globs)):
        for b in right:
            if overlaps(a, b):
                return a, b
    return None


def legacy(dirs: Iterable[str]) -> frozenset[Touch]:
    """The lane of a row without touches: the whole of each ``dir:`` repo."""
    return frozenset(Touch(d.rstrip("/") or ".", (DEEP,)) for d in dirs)


def repos_of(touches: Iterable[Touch]) -> frozenset[str]:
    """The repo lanes (``.`` included, resources not) a lane touches."""
    return frozenset(t.lane for t in touches if not t.is_resource)


class Wait(NamedTuple):
    """Why a ready row did not launch: who it waits on, over what."""

    holder: str
    touch: Touch | None
    why: str  # "held" | "reserved" | "per_repo"


def pick(ready: Iterable[str], order: Sequence[str], held: Mapping[str, frozenset[Touch]],
         lanes: Mapping[str, frozenset[Touch]], per_repo: int,
         commons: Iterable[str] = ()) -> tuple[list[str], dict[str, Wait]]:
    """Choose which ready rows launch now.

    Returns the launchable ids in walk order, and a :class:`Wait` for every other
    ready row that has a lane. ``per_repo`` below 1 is treated as 1. A touch
    matching ``commons`` holds nothing and counts toward no repo (:func:`owned`),
    so a row whose touches are all commons launches beside anything.
    """
    globs = list(commons)
    rank = {pid: i for i, pid in enumerate(order)}
    walk = sorted(set(ready), key=lambda p: (0, rank[p], "") if p in rank else (1, 0, p))
    running = {pid: owned(lane, globs) for pid, lane in held.items()}
    counts = Counter(repo for lane in running.values() for repo in repos_of(lane))
    cap = max(per_repo, 1)
    reserved: list[tuple[str, frozenset[Touch]]] = []
    launch: list[str] = []
    waits: dict[str, Wait] = {}
    for pid in walk:
        lane = lanes.get(pid)
        if lane is None:
            continue
        lane = owned(lane, globs)
        wait = (_first_hit(lane, sorted(running.items()), "held")
                or _first_hit(lane, reserved, "reserved")
                or _over_cap(lane, running, counts, cap))
        if wait is None:
            launch.append(pid)
            running[pid] = lane
            counts.update(repos_of(lane))
        else:
            waits[pid] = wait
            reserved.append((pid, lane))
    return launch, waits


def _first_hit(lane: frozenset[Touch], holders: Iterable[tuple[str, frozenset[Touch]]],
               why: str) -> Wait | None:
    for holder, theirs in holders:
        pair = collide(lane, theirs)
        if pair is not None:
            return Wait(holder, pair[1], why)
    return None


def _over_cap(lane: frozenset[Touch], running: Mapping[str, frozenset[Touch]],
              counts: Counter[str], cap: int) -> Wait | None:
    for repo in sorted(repos_of(lane)):
        if counts[repo] >= cap:
            holder = min(p for p, theirs in running.items() if repo in repos_of(theirs))
            return Wait(holder, Touch(repo, (DEEP,)), "per_repo")
    return None
