"""The open ledger as the swarm will schedule it, and what waits on the owner.

A forecast can only time rows the swarm will get to by itself. Anything whose
next step is the owner's — an owner-run row (``[tasks].exclude``), a worker
that asked a question and waits for the answer, a row that failed and waits for
``swarm retry`` — has no finish time anyone can compute, and neither has every
row that needs one of them, however indirectly. Those are left out of the
forecast and listed instead (:class:`Stuck`), each with the rows stuck behind
it: *what your answer is worth* is the honest thing to show there, not a date.

Everything else is a :class:`Row` the simulator schedules: rows running now
(with how long they have worked), rows whose worker finished and are merging,
and rows still to start, in ledger order, which is the order the supervisor
picks them in. A row with an ``after:`` date waits for the start of that day,
as the launcher reads it (UTC, :func:`ledger.deferred`).
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass, field

from .. import ledger as ledger_mod
from .. import statuses
from ..tui.campaign import campaign_of
from .model import Meta, kind_of

#: Why a row waits on the owner, in the words every view prints.
OWNER_RUN = "yours to do"
ASKED = "asked you a question"
FAILED = "failed; `swarm retry` runs it again"
CYCLE = "in a dependency cycle; the ledger needs fixing"


@dataclass(frozen=True)
class Row:
    """One row the forecast schedules."""

    id: str
    meta: Meta
    #: The open rows it needs, in the schedule (landed ones are already met).
    needs: frozenset[str]


@dataclass(frozen=True)
class Stuck:
    """A row whose next step is the owner's, and what is stuck behind it."""

    row: str
    why: str
    behind: tuple[str, ...] = ()


@dataclass(frozen=True)
class Count:
    """One campaign's rows, counted the way :mod:`tui.campaign` counts them."""

    done: int = 0
    total: int = 0
    running: tuple[str, ...] = ()
    ready: tuple[str, ...] = ()
    #: row -> the date it waits for.
    gated: dict[str, str] = field(default_factory=dict)
    #: Open rows left out of the forecast because they wait on the owner.
    behind: int = 0

    @property
    def left(self) -> int:
        return max(0, self.total - self.done)


@dataclass(frozen=True)
class Plan:
    """Everything the simulator needs about the ledger and the run right now."""

    now: float
    #: Every row the forecast schedules, in ledger order (running ones included).
    rows: dict[str, Row] = field(default_factory=dict)
    #: row -> seconds its worker has worked so far.
    running: dict[str, float] = field(default_factory=dict)
    #: Rows whose worker finished and whose work is being merged.
    finishing: tuple[str, ...] = ()
    #: row -> when its ``after:`` date opens (epoch seconds).
    gates: dict[str, float] = field(default_factory=dict)
    stuck: tuple[Stuck, ...] = ()
    #: Open rows out of the forecast: the stuck rows and everything behind them.
    held: frozenset[str] = frozenset()
    books: dict[str, Count] = field(default_factory=dict)
    workers: int = 1
    #: Concurrent heavy builds (``[build].max_concurrent``); 0 = no gate.
    build_slots: int = 0
    park_after: float = 900.0
    #: Every open row -> the open rows it needs, as the ledger says, in ledger order.
    needs: dict[str, frozenset[str]] = field(default_factory=dict)
    #: Every open row -> its campaign.
    campaigns: dict[str, str] = field(default_factory=dict)


def meta_for(phase: str, dirs: dict[str, list[str]]) -> Meta:
    """A row's kind, repo and campaign. The repo is the first ``dir:``; a row
    with none works in the repo its id is named for, as the ledger header says."""
    camp = campaign_of(phase)
    repo = (dirs.get(phase) or [camp])[0]
    return Meta(kind=kind_of(phase), repo=repo, campaign=camp)


def day_start(date: str) -> float:
    """The epoch the launcher starts reading ``date`` as today (UTC midnight)."""
    return float(calendar.timegm(time.strptime(date, "%Y-%m-%d")))


def build(graph: dict[str, set[str]], text: str, landed: dict[str, str], *,
          now: float, busy: dict[str, float], asking: set[str] | frozenset[str] = frozenset(),
          merging: set[str] | frozenset[str] = frozenset(),
          excluded: set[str] | frozenset[str] = frozenset(), workers: int = 1,
          build_slots: int = 0, park_after: float = 900.0) -> Plan:
    """The plan for ``graph`` (:func:`ledger.parse`) and ledger ``text``.

    ``landed`` is the launcher's done view (:func:`ledger.with_ticked`); ``busy``
    maps each running row to when its worker started; ``asking`` are the rows
    whose worker waits on the owner (``waiting`` or ``parked``); ``merging`` the
    rows whose worker finished and are in the merge queue.
    """
    dirs = ledger_mod.dirs(text)
    satisfied = {p for p, s in landed.items() if s in statuses.SATISFIES_DEPS}
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    dated = ledger_mod.deferred(text, today)
    open_rows = [p for p in graph if p not in satisfied]
    live = set(open_rows)
    roots: dict[str, str] = {}
    for p in open_rows:
        if p in asking:
            roots[p] = ASKED
        elif p in busy or p in merging:
            continue
        elif p in excluded:
            roots[p] = OWNER_RUN
        elif p in landed:  # attempted, and it released nothing: a `fail`
            roots[p] = FAILED
    blockers = _blockers(graph, live, roots)
    order = _schedulable(graph, [p for p in open_rows if not blockers[p]])
    for p in open_rows:
        if not blockers[p] and p not in order:
            roots[p] = CYCLE
    if any(r == CYCLE for r in roots.values()):
        blockers = _blockers(graph, live, roots)
    held = {p for p in open_rows if blockers[p]}
    behind: dict[str, list[str]] = {r: [] for r in roots}
    for p in open_rows:
        for r in blockers[p]:
            if r != p:
                behind[r].append(p)
    stuck = tuple(Stuck(r, roots[r], tuple(behind[r])) for r in open_rows if r in roots)
    rows = {p: Row(p, meta_for(p, dirs), frozenset(d for d in graph[p] if d in live))
            for p in open_rows if p not in held}
    return Plan(
        now=now,
        rows=rows,
        running={p: max(0.0, now - t) for p, t in busy.items() if p in rows},
        finishing=tuple(p for p in open_rows if p in merging and p in rows),
        gates={p: day_start(d) for p, d in dated.items() if p in rows},
        stuck=stuck,
        held=frozenset(held),
        books=_books(graph, landed, satisfied, held, set(busy), set(merging), excluded, dated),
        workers=max(1, workers),
        build_slots=max(0, build_slots),
        park_after=park_after,
        needs={p: frozenset(d for d in graph[p] if d in live) for p in open_rows},
        campaigns={p: campaign_of(p) for p in open_rows},
    )


def _blockers(graph, live: set[str], roots: dict[str, str]) -> dict[str, frozenset[str]]:
    """Each open row -> the owner-bound rows it waits on (itself, if it is one)."""
    memo: dict[str, frozenset[str]] = {}
    for start in graph:
        if start not in live or start in memo:
            continue
        stack = [(start, iter(sorted(graph[start])))]
        visiting = {start}
        while stack:
            node, deps = stack[-1]
            dep = next(deps, None)
            if dep is None:
                stack.pop()
                visiting.discard(node)
                got = {node} if node in roots else set()
                for d in graph[node]:
                    got |= memo.get(d, frozenset())
                memo[node] = frozenset(got)
            elif dep in live and dep not in memo and dep not in visiting:
                visiting.add(dep)
                stack.append((dep, iter(sorted(graph[dep]))))
    return memo


def _schedulable(graph, rows: list[str]) -> set[str]:
    """The rows in ``rows`` that can ever start: a cycle's members never do."""
    members = set(rows)
    waiting = {p: {d for d in graph[p] if d in members} for p in rows}
    free = [p for p, d in waiting.items() if not d]
    dependents: dict[str, list[str]] = {}
    for p, deps in waiting.items():
        for d in deps:
            dependents.setdefault(d, []).append(p)
    out = set()
    while free:
        p = free.pop()
        out.add(p)
        for q in dependents.get(p, ()):
            waiting[q].discard(p)
            if not waiting[q]:
                free.append(q)
    return out


def _books(graph, landed, satisfied, held, busy, merging, excluded, dated) -> dict[str, Count]:
    """Per campaign: done, total, running, ready, gated and behind-the-owner."""
    ready = set(ledger_mod.ready(graph, landed, busy | merging | held,
                                 set(excluded) | set(dated)))
    acc: dict[str, dict] = {}
    for p in graph:
        c = acc.setdefault(campaign_of(p), {"done": 0, "total": 0, "running": [], "ready": [],
                                            "gated": {}, "behind": 0})
        if p in satisfied:
            c["done"] += 1
            c["total"] += 1
            continue
        if p in excluded:
            continue  # owner-run and open: not the swarm's to count
        c["total"] += 1
        if p in held:
            c["behind"] += 1
        elif p in busy:
            c["running"].append(p)
        elif p in dated and graph[p] <= satisfied:
            c["gated"][p] = dated[p]
        elif p in ready:
            c["ready"].append(p)
    return {name: Count(done=c["done"], total=c["total"], running=tuple(c["running"]),
                        ready=tuple(c["ready"]), gated=c["gated"], behind=c["behind"])
            for name, c in acc.items()}
