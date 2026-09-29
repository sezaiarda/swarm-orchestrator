"""The rarer things that happen to a phase, and how often.

Three, each fitted offline against recorded phase histories
and refitted from this project's own record where it has enough of it:

* **an owner question.** Some phases ask the owner; the wait is heavy
  tailed (median in minutes, P90 in hours). It delays that phase's finish, but after
  ``park_after`` the worker moves to its own window and the seat is free, so it
  costs the swarm little and the phase a lot;
* **a lost attempt.** Some launches crash, are relaunched or restarted
  under, losing time before the attempt that finished;
* **follow-ups.** Campaigns keep growing while they run: rows filed into a
  campaign after its first tick, per row it finished, pooled toward the
  project-wide rate. A design pass that files a whole campaign at once
  (more than :data:`FILING` rows in one commit) is not growth.

Every rate is a count pooled with the research figure as a prior of
:data:`PRIOR_N` observations, so a young project starts from the measured
behaviour and moves to its own as the record grows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import NormalDist

from .record import Record

#: Research figures: the prior every rate starts from.
OWNER_P = 0.13
OWNER_MEDIAN_S = 24 * 60
OWNER_SIGMA = math.log(300 / 24) / 1.2816  # P90 of 5 h
FAIL_P = 0.05
LOST_MEDIAN_S = 19 * 60
LOST_SIGMA = 1.0
FOLLOW = 0.12

#: A prior counts as this many observations.
PRIOR_N = 8
#: At least this many observed waits (or lost attempts) replace the research shape.
MIN_OBSERVED = 8
#: Growth is measured over this recent a stretch of the ledger's history.
GROWTH_WINDOW_S = 21 * 86400
#: A commit that files more rows than this is a design pass, not growth.
FILING = 15
#: No campaign is expected to file more follow-ups than this per finished row;
#: at one or more a campaign never ends, which is a stream, not a forecast.
FOLLOW_MAX = 0.6

_STD = NormalDist()


def _quantile(values: tuple[float, ...], u: float) -> float:
    i = min(len(values) - 1, max(0, int(u * len(values))))
    return values[i]


def _lognormal(median: float, sigma: float, u: float) -> float:
    return median * math.exp(sigma * _STD.inv_cdf(min(max(u, 1e-9), 1 - 1e-9)))


@dataclass(frozen=True)
class Hazards:
    """How often each delay happens, and how long it is."""

    owner_p: float = OWNER_P
    #: Observed owner waits in seconds, sorted; empty = the research shape.
    owner_waits: tuple[float, ...] = ()
    fail_p: float = FAIL_P
    lost: tuple[float, ...] = ()
    #: campaign -> follow-ups per finished row.
    follow: dict[str, float] = field(default_factory=dict)
    follow_all: float = FOLLOW

    def owner_wait(self, u: float) -> float:
        if self.owner_waits:
            return _quantile(self.owner_waits, u)
        return _lognormal(OWNER_MEDIAN_S, OWNER_SIGMA, u)

    def lost_time(self, u: float) -> float:
        if self.lost:
            return _quantile(self.lost, u)
        return _lognormal(LOST_MEDIAN_S, LOST_SIGMA, u)

    def follow_rate(self, campaign: str) -> float:
        return self.follow.get(campaign, self.follow_all)


def _pooled(hits: float, n: float, prior: float) -> float:
    return (hits + PRIOR_N * prior) / (n + PRIOR_N)


def fit(record: Record, now: float, campaign_of) -> Hazards:
    """The rates as of ``now``, from ``record`` (nothing after ``now`` is read)."""
    ended = [a for a in record.attempts if a.end is not None and a.end <= now]
    done = [a for a in ended if a.done]
    lost = [a for a in ended if not a.done]
    waits = tuple(sorted(a.waited_s for a in done if a.waited_s > 0))
    lost_s = tuple(sorted(max(0.0, a.end - a.start) for a in lost))
    follow, follow_all = _growth(record, now, campaign_of)
    return Hazards(
        owner_p=_pooled(len(waits), len(done), OWNER_P),
        owner_waits=waits if len(waits) >= MIN_OBSERVED else (),
        fail_p=_pooled(len(lost), len(ended), FAIL_P),
        lost=lost_s if len(lost_s) >= MIN_OBSERVED else (),
        follow=follow,
        follow_all=follow_all,
    )


def _growth(record: Record, now: float, campaign_of) -> tuple[dict[str, float], float]:
    """Per campaign, rows filed after its first tick per row it finished."""
    first: dict[str, float] = {}
    for phase, (t, _) in record.ticks.items():
        if t <= now:
            c = campaign_of(phase)
            first[c] = min(first.get(c, t), t)
    since = now - GROWTH_WINDOW_S
    grew: dict[str, int] = {}
    finished: dict[str, int] = {}
    for phase, (t, n) in record.added.items():
        c = campaign_of(phase)
        if since <= t <= now and n <= FILING and c in first and t > first[c]:
            grew[c] = grew.get(c, 0) + 1
    for phase, (t, n) in record.ticks.items():
        if since <= t <= now and n <= 3:
            c = campaign_of(phase)
            finished[c] = finished.get(c, 0) + 1
    total = sum(finished.values())
    overall = min(FOLLOW_MAX, _pooled(sum(grew.values()), total, FOLLOW))
    rates = {c: min(FOLLOW_MAX, _pooled(grew.get(c, 0), n, overall))
             for c, n in finished.items()}
    return rates, overall
