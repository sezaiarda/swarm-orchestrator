"""What the replays add up to: a finish range per campaign, and overall.

A campaign ("phase book") finishes when its last row does, follow-ups it files
on the way included, in the shared schedule: its range is read off the
replays, so it already carries every other campaign competing for the same
workers. Percentiles are per campaign; two campaigns' P85s are not a joint
statement and are never added up.

Until the first replay finishes, :func:`floor` stands in: the longer of the
critical path (median work along the longest chain of needs) and the work left
spread over every seat the build gate lets work at once. It can only be early,
and it is labelled that way.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import asdict, dataclass

from .holds import Holds
from .model import Durations
from .plan import Plan, Stuck
from .sim import BUILD_SHARE, Future, Options


@dataclass(frozen=True)
class Range:
    """Finish times in epoch seconds; ``inf`` = not within the horizon."""

    p50: float
    p85: float
    p95: float

    @classmethod
    def of(cls, values: list[float]) -> "Range":
        got = sorted(values)
        n = len(got)
        return cls(*(got[min(n - 1, max(0, math.ceil(q * n) - 1))] for q in (0.5, 0.85, 0.95)))


@dataclass(frozen=True)
class RowView:
    """One open row of a campaign, for its detail."""

    id: str
    #: ``running``, ``merging``, ``ready``, ``waits`` (for a date), ``blocked``
    #: (by rows), ``yours`` (the owner's to do) or ``behind`` (a row that is).
    state: str
    #: What it waits for: rows, a date, or the owner's reason.
    why: str = ""
    finish: Range | None = None


@dataclass(frozen=True)
class Book:
    """One campaign's standing and forecast."""

    name: str
    done: int
    total: int
    running: int
    ready: int
    #: Open rows the forecast leaves out because they wait on the owner.
    behind: int = 0
    finish: Range | None = None
    #: Open rows waiting for their ``after:`` date.
    dated: int = 0
    #: Follow-up rows the replays expect it to file, on average.
    grows: float = 0.0
    rows: tuple[RowView, ...] = ()

    @property
    def left(self) -> int:
        return max(0, self.total - self.done)


@dataclass(frozen=True)
class Forecast:
    """Everything every view shows about when the ledger finishes."""

    made_at: float
    #: Replays behind it; 0 = the cheap floor, before the first simulation.
    runs: int = 0
    workers: int = 1
    build_slots: int = 0
    overall: Range | None = None
    books: tuple[Book, ...] = ()
    stuck: tuple[Stuck, ...] = ()
    #: The chain of rows that sets the overall finish in the median replay.
    critical: tuple[str, ...] = ()
    #: Why nothing may launch until the owner acts ("" = nothing stops it).
    stopped: str = ""
    held_until: float = 0.0
    pause_at: float = 0.0
    #: The pause thresholds the replays honoured: ``(window, at, now)``.
    caps: tuple[tuple[str, float, float], ...] = ()
    #: Share of its hours the swarm was simulated working (its measured availability).
    working: float = 1.0
    #: How far the upper band was stretched past the replays (:mod:`.calibrate`;
    #: 1 = as simulated), and the finished campaigns that measured it (0 = the default).
    stretch: float = 1.0
    calibrated_on: int = 0

    @property
    def floor(self) -> bool:
        return self.runs == 0

    def to_json(self) -> dict:
        return _jsonable(asdict(self))

    @classmethod
    def from_json(cls, data: dict) -> "Forecast":
        def rng(d):
            return None if d is None else Range(*(_num(d[k]) for k in ("p50", "p85", "p95")))

        books = tuple(
            Book(**{**b, "finish": rng(b["finish"]),
                    "rows": tuple(RowView(**{**r, "finish": rng(r["finish"])})
                                  for r in b["rows"])})
            for b in data["books"])
        return cls(**{**data, "overall": rng(data["overall"]), "books": books,
                      "stuck": tuple(Stuck(s["row"], s["why"], tuple(s["behind"]))
                                     for s in data["stuck"]),
                      "critical": tuple(data["critical"]),
                      "caps": tuple(tuple(c) for c in data["caps"])})


def _jsonable(value):
    if isinstance(value, float) and math.isinf(value):
        return None
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _num(value) -> float:
    return math.inf if value is None else float(value)


# -- from replays -------------------------------------------------------------------
def summarise(plan: Plan, holds: Holds, futures: list[Future], made_at: float,
              opts: Options = Options()) -> Forecast:
    """The forecast the replays make."""
    by_camp: dict[str, list[str]] = {}
    for row in plan.rows.values():
        by_camp.setdefault(row.meta.campaign, []).append(row.id)
    row_range = {r: Range.of([f.finish[r] for f in futures]) for r in plan.rows}
    books = []
    for name, count in plan.books.items():
        if count.left <= 0:
            continue
        mine = by_camp.get(name, [])
        finish = None
        if mine:
            finish = Range.of([max([f.finish[r] for r in mine] + [f.grown.get(name, 0.0)])
                               for f in futures])
        grows = sum(f.filed.get(name, 0) for f in futures) / max(1, len(futures))
        books.append(Book(name, count.done, count.total, len(count.running), len(count.ready),
                          count.behind, finish, len(count.gated), round(grows, 1),
                          _rows(plan, name, row_range)))
    ends = [max([max(f.finish.values(), default=plan.now)] + list(f.grown.values()))
            for f in futures]
    overall = Range.of(ends) if plan.rows else None
    median = sorted(range(len(futures)), key=lambda i: ends[i])[len(futures) // 2] \
        if futures else None
    return Forecast(
        made_at=made_at, runs=len(futures), workers=plan.workers,
        build_slots=plan.build_slots if opts.build_share > 0 else 0,
        overall=overall, books=_ordered(books), stuck=plan.stuck,
        critical=futures[median].chain if median is not None else (),
        stopped=holds.stopped, held_until=holds.held_until, pause_at=holds.pause_at,
        caps=tuple((c.window, c.at, c.pct) for c in holds.caps) if opts.caps else (),
        working=holds.availability.share if opts.availability else 1.0)


def _ordered(books: list[Book]) -> tuple[Book, ...]:
    """Soonest P50 first; a campaign with nothing the forecast can time last."""
    return tuple(sorted(books, key=lambda b: (b.finish is None, b.finish.p50 if b.finish
                                              else 0.0, b.name)))


def _rows(plan: Plan, name: str, ranges: dict[str, Range]) -> tuple[RowView, ...]:
    """The campaign's open rows, in ledger order, each with what holds it."""
    stuck = {s.row: s.why for s in plan.stuck}
    count = plan.books[name]
    out = []
    for row, needs in plan.needs.items():
        if plan.campaigns.get(row) != name or (row not in plan.rows and row not in plan.held):
            continue
        open_needs = sorted(d for d in needs if d in plan.rows or d in plan.held)
        if row in stuck:
            view = RowView(row, "yours", stuck[row])
        elif row in plan.held:
            view = RowView(row, "behind", ", ".join(d for d in open_needs if d in plan.held))
        elif row in plan.running:
            view = RowView(row, "running", "", ranges.get(row))
        elif row in plan.finishing:
            view = RowView(row, "merging", "", ranges.get(row))
        elif row in count.gated:
            view = RowView(row, "waits", f"until {count.gated[row]}", ranges.get(row))
        elif row in count.ready:
            view = RowView(row, "ready", "", ranges.get(row))
        else:
            view = RowView(row, "blocked", ", ".join(open_needs), ranges.get(row))
        out.append(view)
    return tuple(out)


# -- before the first replay ------------------------------------------------------------
def floor(plan: Plan, model: Durations, holds: Holds, made_at: float,
          build_share: float = BUILD_SHARE) -> Forecast:
    """The cheap lower bound, per campaign and overall: never later than the truth."""
    work = {r: model.median_s(row.meta) for r, row in plan.rows.items()}
    for r, age in plan.running.items():
        work[r] = max(0.0, work[r] - age)
    for r in plan.finishing:
        work[r] = 0.0
    start = max(plan.now, holds.held_until)
    ends: dict[str, float] = {}
    for r in _topo(plan):
        after = max([ends[d] for d in plan.rows[r].needs if d in ends]
                    + [plan.gates.get(r, 0.0), start if r not in plan.running else plan.now])
        ends[r] = after + work[r]
    seats = plan.workers
    if plan.build_slots and build_share > 0:
        seats = min(seats, plan.build_slots / build_share)
    by_camp: dict[str, list[str]] = {}
    for r, row in plan.rows.items():
        by_camp.setdefault(row.meta.campaign, []).append(r)
    total = start + sum(work.values()) / max(1e-9, seats)
    books = []
    for name, count in plan.books.items():
        if count.left <= 0:
            continue
        mine = by_camp.get(name, [])
        finish = None
        if mine:
            t = max(max(ends[r] for r in mine),
                    start + sum(work[r] for r in mine) / max(1e-9, seats))
            finish = Range(t, t, t)
        books.append(Book(name, count.done, count.total, len(count.running), len(count.ready),
                          count.behind, finish, len(count.gated), 0.0, _rows(plan, name, {})))
    last = max([total] + list(ends.values())) if plan.rows else None
    return Forecast(
        made_at=made_at, runs=0, workers=plan.workers,
        build_slots=plan.build_slots if build_share > 0 else 0,
        overall=Range(last, last, last) if last is not None else None,
        books=_ordered(books), stuck=plan.stuck, stopped=holds.stopped,
        held_until=holds.held_until, pause_at=holds.pause_at,
        caps=tuple((c.window, c.at, c.pct) for c in holds.caps))


def _topo(plan: Plan) -> list[str]:
    """The planned rows, each after every row it needs."""
    indeg = Counter()
    dependents: dict[str, list[str]] = {}
    for r, row in plan.rows.items():
        for d in row.needs:
            if d in plan.rows:
                indeg[r] += 1
                dependents.setdefault(d, []).append(r)
    order = [r for r in plan.rows if not indeg[r]]
    for r in order:
        for q in dependents.get(r, ()):
            indeg[q] -= 1
            if not indeg[q]:
                order.append(q)
    return order
