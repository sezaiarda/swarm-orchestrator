"""How long a row's work takes: log-normal, pooled from the whole history down.

Fitted offline against recorded phase histories: work time is
log-normal (ln-sigma about 1), and almost nothing known before a
launch predicts it. Campaign explains some of the variance, repo a little,
row kind less. So the location of ``ln(seconds)`` is the global mean plus an
offset for the row's kind, then its repo, then its campaign, each estimated on
what the levels above left over and **shrunk toward zero** by ``n / (n + K)``
(partial pooling): a group with a handful of samples barely moves the answer,
one with dozens mostly speaks for itself, and a group with fewer than
:data:`MIN_GROUP` samples is not used at all. The global median and spread are
pooled the same way with the research figures, worth :data:`PRIOR_N` samples,
so a project's first day of log does not set a two-hour median.

Rows still running are right-censored samples, not missing ones: dropping them
would fit only the phases that finished quickly. They enter as their expected
value given that they have run at least as long as they have (two passes).

The spread that is left, ``sigma``, is split in two for simulation: a
**slowdown shared by every row in one simulated future** (a slow day, a model
having a bad week) and the row's own. The marginal spread of one row stays
``sigma``; the shared part is what keeps a campaign's range from pretending its
rows' delays cancel out.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from statistics import NormalDist

from . import tiers
from .record import Sample

#: A group needs this many samples before its own offset is used at all…
MIN_GROUP = 8
#: …and is shrunk by ``n / (n + K)``: at ``n = K`` it moves half way.
SHRINK_K = 8.0
#: Share of ``sigma`` (as variance) that one simulated future shares across rows.
SHARED = 0.25
#: The research figures (findings.md §1-2): median 24 min, ln-sigma 0.95…
DEFAULT_MU = math.log(24 * 60)
DEFAULT_SIGMA = 0.95
#: …worth this many samples in the pooled global median and spread.
PRIOR_N = 30

#: The pooling levels, outermost first.
LEVELS = ("kind", "repo", "campaign")

_KIND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*?-([A-Za-z]+)\d")
_STD = NormalDist()


@dataclass(frozen=True)
class Meta:
    """What is known about a row before it runs."""

    kind: str
    repo: str
    campaign: str
    #: The model its worker runs on when that is not the swarm's own ("" = it is).
    model: str = ""


def kind_of(phase: str) -> str:
    """``W`` wave, ``F`` fix, ``C``, ``P``…, or ``other`` for an unnumbered id."""
    m = _KIND_RE.match(phase)
    return m.group(1) if m else "other"


@dataclass(frozen=True)
class Durations:
    """The fitted work-time model."""

    mu: float = DEFAULT_MU
    sigma: float = DEFAULT_SIGMA
    #: ``(level, value) -> offset`` on ``ln(seconds)``.
    offsets: dict[tuple[str, str], float] = field(default_factory=dict)
    n: int = 0
    shared: float = SHARED

    def loc(self, meta: Meta) -> float:
        """The row's median, as ``ln(seconds)``."""
        return (self.mu + sum(self.offsets.get((lvl, getattr(meta, lvl)), 0.0)
                              for lvl in LEVELS)
                + (self.model_offset(meta.model) if meta.model else 0.0))

    def model_offset(self, model: str) -> float:
        """How much shorter or longer (on ``ln(seconds)``) a row on ``model``
        works than one on the swarm's own: measured, or the prior."""
        return self.offsets.get(("model", model), tiers.TIME_PRIOR)

    def median_s(self, meta: Meta) -> float:
        return math.exp(self.loc(meta))

    @property
    def mean_s(self) -> float:
        """The mean work time of a row with no group of its own."""
        return math.exp(self.mu + self.sigma ** 2 / 2)

    @property
    def shared_sigma(self) -> float:
        """The spread of the one slowdown every row of a simulated future shares."""
        return self.sigma * math.sqrt(self.shared)

    @property
    def own_sigma(self) -> float:
        """What is left of ``sigma`` for each row on its own."""
        return self.sigma * math.sqrt(1.0 - self.shared)

    def draw(self, u: float, meta: Meta, slow: float) -> float:
        """Seconds of work for quantile ``u`` of the row's own spread, in a
        future whose shared slowdown is ``slow`` (on the log scale)."""
        z = _STD.inv_cdf(min(max(u, 1e-9), 1 - 1e-9))
        return math.exp(self.loc(meta) + slow + self.own_sigma * z)

    def remaining(self, u: float, meta: Meta, slow: float, age_s: float) -> float:
        """Seconds still to go for a row that has worked ``age_s`` already.

        Drawn from the work time *given that it is longer than the age*: under
        a heavy tail a phase that has run long is expected to run longer still,
        so "median minus elapsed" would promise the stragglers too early.
        """
        loc = self.loc(meta) + slow
        if age_s <= 0:
            return self.draw(u, meta, slow)
        floor = _STD.cdf((math.log(age_s) - loc) / self.own_sigma)
        if floor >= 1 - 1e-9:
            floor = 1 - 1e-9
        q = floor + (1 - floor) * min(max(u, 1e-9), 1 - 1e-9)
        total = math.exp(loc + self.own_sigma * _STD.inv_cdf(min(q, 1 - 1e-12)))
        return max(0.0, total - age_s)


def fit(samples: list[Sample], meta_of, shared: float = SHARED) -> Durations:
    """Fit :class:`Durations` on ``samples``; ``meta_of(phase)`` gives each row's
    :class:`Meta`. With no samples at all the research figures are returned."""
    rows = [(s, meta_of(s.phase)) for s in samples if s.seconds > 0]
    if not rows:
        return Durations(shared=shared)
    observed = [math.log(s.seconds) for s, _ in rows]
    model = _fit_once(rows, observed, shared)
    for _ in range(2):  # a running row is worth its expected value past its age
        ys = [_censored_mean(model, m, y) if s.censored else y
              for (s, m), y in zip(rows, observed)]
        model = _fit_once(rows, ys, shared)
    return model


def _censored_mean(model: Durations, meta: Meta, y_min: float) -> float:
    """E[ln t | ln t > y_min] under the current fit (the inverse Mills ratio)."""
    loc, sd = model.loc(meta), model.sigma
    a = (y_min - loc) / sd
    tail = 1.0 - _STD.cdf(a)
    if tail < 1e-9:
        return y_min
    return max(y_min, loc + sd * _STD.pdf(a) / tail)


def _fit_once(rows, ys: list[float], shared: float) -> Durations:
    # A model of a row's own shifts its rows' times. Its offset starts at the
    # prior, and the rest of the fit is made on times with that shift taken out,
    # so a mix of models never moves the swarm's own median; then the offset is
    # measured against that fit, and the two are settled in a few rounds.
    shift: dict[str, float] = {}
    mine = [meta.model for _, meta in rows]
    for _ in range(4 if any(mine) else 1):
        plain = [y - (shift.get(m, tiers.TIME_PRIOR) if m else 0.0) for m, y in zip(mine, ys)]
        mu, offsets, resid = _fit_levels(rows, plain)
        sums: dict[str, list[float]] = {}
        for m, r in zip(mine, resid):
            if m:
                acc = sums.setdefault(m, [0.0, 0])
                acc[0] += r + shift.get(m, tiers.TIME_PRIOR)
                acc[1] += 1
        shift = {m: (total + tiers.TIME_PRIOR_N * tiers.TIME_PRIOR) / (count + tiers.TIME_PRIOR_N)
                 for m, (total, count) in sums.items()}
    n = len(rows)
    resid = [y - mu - sum(offsets.get((lvl, getattr(meta, lvl)), 0.0) for lvl in LEVELS)
             - (shift[m] if m else 0.0) for (_, meta), m, y in zip(rows, mine, ys)]
    offsets.update({("model", m): v for m, v in shift.items()})
    var = (sum(r * r for r in resid) + PRIOR_N * DEFAULT_SIGMA ** 2) / (n + PRIOR_N)
    return Durations(mu=mu, sigma=math.sqrt(var), offsets=offsets, n=n, shared=shared)


def _fit_levels(rows, ys: list[float]) -> tuple[float, dict[tuple[str, str], float], list[float]]:
    """The pooled median, each level's offsets, and what is left of every row."""
    n = len(rows)
    mu = (sum(ys) + PRIOR_N * DEFAULT_MU) / (n + PRIOR_N)
    resid = [y - mu for y in ys]
    offsets: dict[tuple[str, str], float] = {}
    for level in LEVELS:
        sums: dict[str, list[float]] = {}
        for (_, meta), r in zip(rows, resid):
            acc = sums.setdefault(getattr(meta, level), [0.0, 0])
            acc[0] += r
            acc[1] += 1
        for key, (total, count) in sums.items():
            if count >= MIN_GROUP:
                offsets[(level, key)] = total / (count + SHRINK_K)
        resid = [r - offsets.get((level, getattr(meta, level)), 0.0)
                 for (_, meta), r in zip(rows, resid)]
    return mu, offsets, resid
