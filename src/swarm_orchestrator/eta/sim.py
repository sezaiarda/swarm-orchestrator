"""Many seeded replays of the swarm working through the plan.

Each replay is one possible future. Every row gets a work time from
:mod:`.model` (a running row: what is left given how long it has run) under one
slowdown shared by the whole future, and may file follow-up rows
(:mod:`.hazards`). Then the swarm is replayed event by event: whenever a worker
frees, a row lands, a date opens or a hold lifts, the free seats are filled
with **the supervisor's own pick** — :func:`ledger.ready` over the rows not
started yet, in ledger order, the first as many as seats are free — so a
campaign waits behind another exactly as it will on the box.

Constraints the replay honours, each the way the swarm does it:

* ``max_workers`` seats; with owner questions switched on, a worker that waits
  on the owner past ``park_after`` gives its seat back and finishes on its own;
  a row already at work outside the slots (:attr:`Plan.outside`) takes none;
* ``[build].max_concurrent`` heavy builds at once, when switched on: a phase
  spends :data:`BUILD_SHARE` of its work in ``swarm build``, split into
  :data:`BUILD_CHUNKS` builds that queue first come, first served;
* ``after:`` dates, a scheduled pause, the usage caps and the hours the swarm
  is not working at all (:mod:`.holds`, in blocks of :data:`IDLE_BLOCK_H`): no
  launch while held, running rows carry on and keep burning usage.

Draws come from a hash of (seed, replay, row, purpose) rather than one random
stream, so a row finishing elsewhere in the ledger does not reshuffle every
other row's draws: the forecast moves when the plan does, not by chance.
"""

from __future__ import annotations

import heapq
import math
import zlib
from collections import deque
from dataclasses import dataclass, field
from statistics import NormalDist

from .. import ledger as ledger_mod
from .hazards import Hazards
from .holds import Holds
from .model import Durations, Meta
from .plan import Plan

#: Share of a phase's work spent in ``swarm build`` (findings.md §3).
BUILD_SHARE = 0.3
#: How many builds a phase's build time is split into.
BUILD_CHUNKS = 2
#: Follow-ups of follow-ups go this deep and no deeper.
DEPTH = 3
#: A replay stops here; whatever has not finished by then has no finish time.
HORIZON_S = 60 * 86400.0
#: A future works or idles in whole blocks of this many hours (the backtest's choice).
IDLE_BLOCK_H = 2
#: No replay files more follow-up rows than this many times the rows it started with.
MAX_GROWTH = 2.0

_SEAT, _FIN, _THINK, _BUILT, _WAKE = range(5)
_DUR, _OWNER, _WAIT, _FAIL, _LOST, _FOLLOW, _SLOW, _STOP = range(1, 9)
_MASK = (1 << 64) - 1
_STD = NormalDist()


def _mix(x: int) -> int:
    """splitmix64's finaliser: a good 64-bit hash of one integer."""
    x = (x + 0x9E3779B97F4A7C15) & _MASK
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _MASK
    return x ^ (x >> 31)


def uniform(*parts: int) -> float:
    """A uniform in (0, 1) fixed by ``parts``."""
    h = 0
    for p in parts:
        h = _mix(h ^ (p & _MASK))
    return ((h >> 11) + 0.5) / float(1 << 53)


def row_key(row: str) -> int:
    return zlib.crc32(row.encode("utf-8"))


def poisson(rate: float, u: float) -> int:
    """How many events at ``rate`` for quantile ``u``."""
    if rate <= 0:
        return 0
    k, p = 0, math.exp(-rate)
    total = p
    while u > total and k < 50:
        k += 1
        p *= rate / k
        total += p
    return k


@dataclass(frozen=True)
class Options:
    """Which parts of the model a simulation uses.

    The defaults are the offline backtest's verdict. Owner questions, lost attempts and the build slot are
    measured and implemented, but each made the forecast worse: the swarm's
    measured availability already carries what they cost, so adding them
    counted it twice. They stay switchable so the backtest can ask again once
    the history holds more than a few days at four workers, where the build
    slot is the one expected to start to bind.
    """

    runs: int = 500
    seed: int = 20260929
    build_share: float = 0.0
    owner: bool = False
    failures: bool = False
    growth: bool = True
    caps: bool = True
    availability: bool = True


@dataclass
class Future:
    """One replay's outcome."""

    #: Every planned row -> when it finished (``inf``: not within the horizon).
    finish: dict[str, float] = field(default_factory=dict)
    #: campaign -> when its last follow-up row finished, and how many were filed.
    grown: dict[str, float] = field(default_factory=dict)
    filed: dict[str, int] = field(default_factory=dict)
    #: The chain of rows that set the last finish, first to last.
    chain: tuple[str, ...] = ()


def simulate(plan: Plan, model: Durations, hazards: Hazards, holds: Holds,
             opts: Options = Options()) -> list[Future]:
    """``opts.runs`` replays of ``plan``."""
    return [_Replay(plan, model, hazards, holds, opts, i).run() for i in range(opts.runs)]


class _Cap:
    """A usage window as it runs in one replay."""

    __slots__ = ("pct", "at", "resets_at", "period", "burn", "blocked")

    def __init__(self, cap) -> None:
        self.pct, self.at, self.resets_at = cap.pct, cap.at, cap.resets_at
        self.period, self.burn, self.blocked = cap.period_s, cap.burn, cap.held

    def advance(self, t0: float, t1: float, busy: int) -> None:
        """Burn from ``t0`` to ``t1`` at ``busy`` workers; a reset starts from zero."""
        while self.resets_at <= t1:
            t0 = max(t0, self.resets_at)
            self.pct, self.blocked = 0.0, False
            self.resets_at += self.period
        self.pct += self.burn * busy * max(0.0, t1 - t0) / 3600.0
        if self.pct >= self.at - 1e-6:
            self.blocked = True

    def next_change(self, t: float, busy: int) -> float:
        if self.blocked:
            return self.resets_at
        if busy and self.burn > 0:
            return min(self.resets_at, t + (self.at - self.pct) / (self.burn * busy) * 3600.0)
        return self.resets_at


class _Replay:
    """One future, replayed event by event."""

    def __init__(self, plan: Plan, model: Durations, hazards: Hazards, holds: Holds,
                 opts: Options, index: int) -> None:
        self.plan, self.model, self.hz, self.holds, self.opts = plan, model, hazards, holds, opts
        self.run_key = _mix(opts.seed) ^ _mix(index + 1)
        self.slow = model.shared_sigma * _STD.inv_cdf(uniform(self.run_key, _SLOW))
        self.t = plan.now
        self.end = plan.now + HORIZON_S
        self.heap: list = []
        self.seq = 0
        self.pending: dict[str, frozenset[str]] = {}
        #: Every row's needs, the follow-ups' (their parent) included.
        self.needs: dict[str, frozenset[str]] = {}
        self.meta: dict[str, Meta] = {}
        self.depth: dict[str, int] = {}
        self.done: dict[str, str] = {}
        #: :func:`ledger.ready` over :attr:`pending`, kept current as rows land.
        self.picks: ledger_mod.ReadySet | None = None
        self.finish: dict[str, float] = {}
        self.cause: dict[str, str | None] = {}
        self.work: dict[str, list] = {}
        self.wait: dict[str, float] = {}
        self.seats = 0
        #: Rows at work that hold no seat (:attr:`Plan.outside`), until each ends.
        self.away: set[str] = set()
        builds = plan.build_slots if opts.build_share > 0 else 0
        self.build_free = builds if builds > 0 else None
        self.build_queue: deque = deque()
        self.caps = [_Cap(c) for c in holds.caps] if opts.caps else []
        self.caps_at = plan.now
        self.woken: set[float] = set()
        self.share = holds.availability.draw(uniform(self.run_key, _STOP, -1)) \
            if opts.availability else 1.0
        self.hours: dict[int, bool] = {}
        self.trigger: str | None = None
        self.budget = int(MAX_GROWTH * max(1, len(plan.rows)))
        self.future = Future()

    # -- the replay ------------------------------------------------------------
    def run(self) -> Future:
        plan = self.plan
        for row in plan.rows.values():
            self.meta[row.id] = row.meta
            self.needs[row.id] = row.needs
            self.depth[row.id] = 0
            if row.id in plan.finishing:
                self.done[row.id] = "ok"
                self.finish[row.id] = plan.now
            elif row.id not in plan.running:
                self.pending[row.id] = row.needs
        for row, age in plan.running.items():
            self.cause[row] = None
            if row in plan.outside:
                self.away.add(row)
            self._start(row, age)
        for gate in plan.gates.values():
            if gate > plan.now:
                self._push(gate, _WAKE, "")
        self.picks = ledger_mod.ReadySet(self.pending, self.done)
        self._fill()
        while self.heap:
            t = self.heap[0][0]
            if t > self.end:
                break
            self._advance_caps(t)
            self.t = t
            self.trigger = None
            while self.heap and self.heap[0][0] == t:
                _, _, kind, row = heapq.heappop(self.heap)
                self._handle(kind, row)
            self._fill()
        for row in plan.rows:
            self.future.finish[row] = self.finish.get(row, math.inf)
        self.future.chain = self._chain()
        return self.future

    def _push(self, t: float, kind: int, row: str) -> None:
        self.seq += 1
        heapq.heappush(self.heap, (t, self.seq, kind, row))

    def _handle(self, kind: int, row: str) -> None:
        if kind == _SEAT:
            self.seats -= 1
            self.trigger = row
        elif kind == _FIN:
            self.done[row] = "ok"
            self.picks.land(row, "ok")
            self.finish[row] = self.t
            self.trigger = row
            if row not in self.plan.rows:
                camp = self.meta[row].campaign
                self.future.grown[camp] = max(self.future.grown.get(camp, 0.0), self.t)
            self._follow_ups(row)
        elif kind == _THINK:
            self._request_build(row)
        elif kind == _BUILT:
            if self.build_free is not None:
                if self.build_queue:
                    nxt, span = self.build_queue.popleft()
                    self._push(self.t + span, _BUILT, nxt)
                else:
                    self.build_free += 1
            self._next_segment(row)

    # -- launching -------------------------------------------------------------
    def _held_until(self) -> float:
        """When launching may resume: ``now`` if it may launch now, ``inf`` if
        only the owner can say."""
        h = self.holds
        if h.pause_at and self.t >= h.pause_at:
            return math.inf
        until = self.t
        if self.share < 1.0:
            hour = int((self.t - self.plan.now) // 3600)
            first = hour
            while not self._working(hour) and hour - first < 24 * 30:
                hour += 1
            if hour > first:
                until = self.plan.now + hour * 3600.0
        for cap in self.caps:
            if cap.blocked:
                until = max(until, cap.resets_at)
        return until

    def _working(self, hour: int) -> bool:
        """Whether this future's swarm works in its ``hour``-th hour."""
        block = hour // IDLE_BLOCK_H
        got = self.hours.get(block)
        if got is None:
            got = self.hours[block] = uniform(self.run_key, _STOP, block) < self.share
        return got

    def _fill(self) -> None:
        """Fill the free seats the way the supervisor does, then arrange to wake
        when a hold lifts or a cap will change what may launch."""
        free = self.plan.workers - self.seats
        if free > 0 and self.pending:
            until = self._held_until()
            if until <= self.t:
                gated = {r for r, g in self.plan.gates.items()
                         if g > self.t and r in self.pending}
                for row in self.picks.ready(gated)[:free]:
                    del self.pending[row]
                    self.picks.take(row)
                    self.cause[row] = self._why_now(row)
                    self._start(row, 0.0)
            else:
                self._wake(until)
        for cap in self.caps:
            self._wake(cap.next_change(self.t, self._busy()))

    def _busy(self) -> int:
        """Workers burning usage now: the seats taken, and the rows at work in a
        window of their own."""
        return self.seats + len(self.away)

    def _wake(self, at: float) -> None:
        if self.t < at <= self.end and at not in self.woken:
            self.woken.add(at)
            self._push(at, _WAKE, "")

    def _why_now(self, row: str) -> str | None:
        """The row whose finish (or freed seat) let ``row`` start now."""
        last = max((d for d in self.needs[row] if d in self.finish),
                   key=lambda d: self.finish[d], default=None)
        if last is not None and self.finish[last] >= self.t - 1.0:
            return last
        return self.trigger

    def _advance_caps(self, t: float) -> None:
        for cap in self.caps:
            cap.advance(self.caps_at, t, self._busy())
        self.caps_at = t

    # -- one row's life ----------------------------------------------------------
    def _start(self, row: str, age: float) -> None:
        key = self.run_key ^ row_key(row)
        meta = self.meta[row]
        if age > 0:
            work = self.model.remaining(uniform(key, _DUR), meta, self.slow, age)
            chunks = 1
        else:
            work = self.model.draw(uniform(key, _DUR), meta, self.slow)
            chunks = BUILD_CHUNKS
        lost = 0.0
        if self.opts.failures and uniform(key, _FAIL) < self.hz.fail_p:
            lost = self.hz.lost_time(uniform(key, _LOST))
        wait = 0.0
        if self.opts.owner and uniform(key, _OWNER) < self.hz.owner_p:
            wait = self.hz.owner_wait(uniform(key, _WAIT))
        self.wait[row] = wait
        if row not in self.away:
            self.seats += 1
        if self.build_free is None:
            self.work[row] = []
            self._push(self.t + lost + work, _THINK, row)
            return
        build = self.opts.build_share * work / chunks
        think = (1.0 - self.opts.build_share) * work / chunks
        self.work[row] = [(think, build) for _ in range(chunks)]
        self.work[row][0] = (think + lost, build)
        self._next_segment(row)

    def _next_segment(self, row: str) -> None:
        segs = self.work[row]
        if segs:
            think, _ = segs[0]
            self._push(self.t + think, _THINK, row)
            return
        wait = self.wait[row]
        if row in self.away:
            self.away.discard(row)  # its work is over, and it has no seat to give back
        else:
            self._push(self.t + min(wait, self.plan.park_after), _SEAT, row)
        self._push(self.t + wait, _FIN, row)

    def _request_build(self, row: str) -> None:
        segs = self.work[row]
        if not segs:  # no build gate: the work is simply over
            self._next_segment(row)
            return
        _, build = segs.pop(0)
        if self.build_free is None or self.build_free > 0:
            if self.build_free is not None:
                self.build_free -= 1
            self._push(self.t + build, _BUILT, row)
        else:
            self.build_queue.append((row, build))

    def _follow_ups(self, row: str) -> None:
        if not self.opts.growth or self.depth[row] >= DEPTH or self.budget <= 0:
            return
        meta = self.meta[row]
        n = poisson(self.hz.follow_rate(meta.campaign), uniform(self.run_key ^ row_key(row),
                                                                  _FOLLOW))
        for k in range(min(n, self.budget)):
            child = f"{row}+{k + 1}"
            self.meta[child] = Meta("F", meta.repo, meta.campaign)
            self.depth[child] = self.depth[row] + 1
            self.needs[child] = frozenset({row})
            self.pending[child] = frozenset()
            self.picks.add(child, ())
            self.future.filed[meta.campaign] = self.future.filed.get(meta.campaign, 0) + 1
            self.budget -= 1

    def _chain(self) -> tuple[str, ...]:
        """The rows that set the last planned finish, walked back from it."""
        done = [(t, r) for r, t in self.finish.items() if r in self.plan.rows]
        if not done:
            return ()
        _, row = max(done)
        chain = []
        seen = set()
        while row is not None and row not in seen and len(chain) < 200:
            seen.add(row)
            chain.append(row)
            row = self.cause.get(row)
        return tuple(reversed(chain))
