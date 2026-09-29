"""What the ETA engine learns from history: work times, and the rarer delays.

The fit is small arithmetic on logged runs, so it is tested on samples whose
answer is known: a log-normal laid out quantile by quantile, groups too small
to trust and groups big enough to, rows still running, and ledger filings.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import pytest

from swarm_orchestrator.eta import hazards, model, record
from swarm_orchestrator.eta.record import Attempt, Record, Sample
from swarm_orchestrator.tui.data import Event, PhaseRun

MIN = 60.0
NOW = 1_790_000_000.0


def lognormal(n: int, median_s: float, sigma: float, phase: str = "x-W", end=NOW):
    """``n`` samples at evenly spaced quantiles: an exact log-normal, no noise."""
    std = NormalDist()
    return [Sample(f"{phase}{i}", median_s * math.exp(sigma * std.inv_cdf((i + 0.5) / n)),
                   end) for i in range(n)]


def meta(phase: str) -> model.Meta:
    return model.Meta(kind=model.kind_of(phase), repo=phase.split("-")[0],
                      campaign=phase.split("-")[0])


# -- the work-time model -----------------------------------------------------------
def test_a_fit_recovers_median_and_spread_pulled_a_little_to_the_research_prior():
    got = model.fit(lognormal(600, 40 * MIN, 1.2), meta)
    assert math.exp(got.mu) == pytest.approx(40 * MIN, rel=0.05)
    assert got.sigma == pytest.approx(1.2, abs=0.05)
    assert got.n == 600


def test_no_history_is_the_research_figures():
    got = model.fit([], meta)
    assert (got.mu, got.sigma) == (model.DEFAULT_MU, model.DEFAULT_SIGMA)
    assert math.exp(got.mu) == pytest.approx(24 * MIN)


def test_a_first_day_of_log_does_not_set_a_two_hour_median():
    """A small first batch of long runs moved the fit, not replaced it."""
    got = model.fit(lognormal(22, 120 * MIN, 1.7), meta)
    assert 24 * MIN < math.exp(got.mu) < 120 * MIN


def test_a_group_is_used_only_when_it_is_big_enough_and_then_shrunk():
    common = lognormal(200, 20 * MIN, 0.8, "a-W")
    small = lognormal(model.MIN_GROUP - 1, 200 * MIN, 0.1, "b-W")
    big = lognormal(40, 200 * MIN, 0.1, "c-W")
    got = model.fit(common + small + big, meta)
    assert ("campaign", "b") not in got.offsets
    raw = math.log(200 / 20)
    assert 0 < got.offsets[("campaign", "c")] + got.offsets.get(("repo", "c"), 0.0) < raw
    assert got.median_s(meta("c-W1")) > got.median_s(meta("a-W1"))


def test_a_row_still_running_counts_as_at_least_its_age():
    done = lognormal(100, 20 * MIN, 0.8)
    long_runners = [Sample(f"r-W{i}", 300 * MIN, NOW, censored=True) for i in range(20)]
    assert model.fit(done + long_runners, meta).mu > model.fit(done, meta).mu


def test_a_row_that_has_run_long_is_expected_to_run_longer_still():
    """Heavy tails: "median minus elapsed" would promise the stragglers too early."""
    d = model.Durations(mu=math.log(30 * MIN), sigma=1.0, shared=0.0)
    m = meta("a-W1")
    us = [(i + 0.5) / 200 for i in range(200)]
    young = sum(d.remaining(u, m, 0.0, 5 * MIN) for u in us) / len(us)
    old = sum(d.remaining(u, m, 0.0, 180 * MIN) for u in us) / len(us)
    assert old > young > 0
    assert all(d.remaining(u, m, 0.0, 180 * MIN) >= 0 for u in us)


def test_the_shared_slowdown_keeps_one_row_s_spread():
    d = model.Durations(sigma=1.0, shared=0.25)
    assert d.shared_sigma ** 2 + d.own_sigma ** 2 == pytest.approx(1.0)


# -- what the log says ---------------------------------------------------------------
def ev(ts, kind, phase):
    return Event(ts, kind, phase, None, {}, f"{kind} {phase}")


def test_owner_waits_come_out_of_work_time_and_lost_runs_are_not_samples():
    runs = [
        PhaseRun("a-W1", "ok", started_at=0.0, ended_at=3600.0),
        PhaseRun("a-W2", "lost", started_at=0.0, ended_at=600.0, why="reaped"),
        PhaseRun("a-W3", None, started_at=NOW - 1200.0),
    ]
    events = [ev(1000.0, "waiting", "a-W1"), ev(1600.0, "resumed", "a-W1")]
    atts = record.attempts(runs, events)
    assert [a.phase for a in atts] == ["a-W1", "a-W2", "a-W3"]
    assert atts[0].waited_s == 600.0 and not atts[1].done and atts[2].end is None
    got = record.samples(Record(attempts=atts), NOW)
    assert [(s.phase, s.seconds, s.censored) for s in got] == [
        ("a-W1", 3000.0, False), ("a-W3", 1200.0, True)]


# -- the rarer delays ------------------------------------------------------------------
def test_rates_start_from_the_research_figures_and_move_with_the_record():
    assert hazards.fit(Record(), NOW, lambda p: p.split("-")[0]).owner_p == hazards.OWNER_P
    asked = [Attempt(f"a-W{i}", 0.0, 100.0, True, 50.0 if i < 30 else 0.0) for i in range(40)]
    got = hazards.fit(Record(attempts=tuple(asked)), NOW, lambda p: p.split("-")[0])
    assert got.owner_p == pytest.approx((30 + hazards.PRIOR_N * hazards.OWNER_P)
                                        / (40 + hazards.PRIOR_N))
    assert got.owner_waits == tuple([50.0] * 30)


def test_growth_counts_rows_filed_into_a_running_campaign_but_not_a_design_pass():
    day = 86400.0
    ticks = {f"g-W{i}": (NOW - 5 * day + i * 3600, 1) for i in range(10)}
    added = {f"g-W{i}": (NOW - 10 * day, 20) for i in range(10)}          # the design pass
    added |= {f"g-F{i}": (NOW - 2 * day, 1) for i in range(5)}            # follow-ups
    added |= {f"g-W{i}": (NOW - 2 * day, 30) for i in range(20, 40)}      # a second pass
    got = hazards.fit(Record(ticks=ticks, added=added), NOW, lambda p: p.split("-")[0])
    pooled_all = hazards._pooled(5, 10, hazards.FOLLOW)
    assert got.follow_all == pytest.approx(pooled_all)
    assert got.follow_rate("g") == pytest.approx(hazards._pooled(5, 10, pooled_all))
    assert got.follow_rate("new") == got.follow_all
