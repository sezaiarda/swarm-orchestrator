"""Forecasts: finish clock, per-phase ETA, and whether the weekly limit lets the run finish."""

from __future__ import annotations

import json
from datetime import datetime

from swarm_orchestrator import meters
from swarm_orchestrator.pace import Pace
from swarm_orchestrator.tui.data import (
    TOO_FEW,
    Forecast,
    Limits,
    PhaseRun,
    eta,
    fmt_range,
    fmt_when,
    fmt_when_range,
    forecast,
    limit_outlook,
    load_limits,
    load_meters,
    phase_eta,
    week_pace,
)

NOW = datetime(2026, 9, 18, 12, 0, 0).timestamp()
H = 3600.0


def runs(*hours: float) -> list[PhaseRun]:
    return [PhaseRun(phase=f"P{i}", status="ok", started_at=0.0, ended_at=h * H) for i, h in enumerate(hours)]


# -- run and phase ETA -----------------------------------------------------------
def test_forecast_is_the_recent_pace_as_a_range_scaled_to_the_workers_now():
    p = Pace(phases=20, hours=12.0, per_hour=19 / 12.0, workers=2.0)
    fc = forecast(p, remaining=4, workers=1, running=0, ready=2)
    # 4 phases at 1.58/h is 2.5 h; at half the workers, up to twice that.
    assert (round(fc.soonest), round(fc.latest)) == (round(4 / (19 / 12) * H), round(8 / (19 / 12) * H))
    assert fc.label == "" and eta(p, 4, 1, ready=2) == "~3–5h left"
    same = forecast(p, remaining=4, workers=2, running=1)
    assert same.soonest == same.latest  # measured at the concurrency it runs at: one figure
    # More workers than phases left do not help: one phase at two workers is one phase.
    one = forecast(p, remaining=1, workers=2, running=1)
    assert one.latest == 2 * one.soonest


def test_forecast_refuses_rather_than_guesses():
    p = Pace(phases=20, hours=12.0, per_hour=1.5, workers=1.0)
    assert forecast(None, 3, 1, running=1) == Forecast(label=TOO_FEW)
    assert forecast(p, 3, 1) == Forecast(label="stalled")
    assert forecast(p, 0, 1) == Forecast(0.0, 0.0, "done")


def test_a_forecast_is_said_at_the_resolution_it_is_good_to():
    assert fmt_range(40 * 60) == "~40m"
    assert fmt_range(22 * 60, 43 * 60) == "~20–45m"
    assert fmt_range(2.4 * H, 4.6 * H) == "~2–5h"
    assert fmt_range(3 * H, 3.2 * H) == "~3h"
    assert fmt_range(30 * H, 60 * H) == "~1–2 days"
    assert fmt_range(20) == "~5m"  # never "0m"
    assert fmt_when_range(NOW + 2 * H, NOW + 4 * H, NOW) == "14:00–16:00"
    assert fmt_when_range(NOW + 2 * H, NOW + 2 * H + 60, NOW) == "14:00"
    tomorrow = datetime.fromtimestamp(NOW + 26 * H).strftime("%a")
    assert fmt_when_range(NOW + 26 * H, NOW + 27 * H, NOW) == f"{tomorrow} 14:00–15:00"
    assert fmt_when_range(NOW + 2 * H, NOW + 26 * H, NOW) == f"14:00 – {tomorrow} 14:00"


def test_a_phase_inside_the_median_has_time_left_and_past_it_is_over():
    assert phase_eta(runs(1, 1, 1), 0.25 * H) == (0.75 * H, False)
    assert phase_eta(runs(1, 1, 1), 1.5 * H) == (0.5 * H, True)
    assert phase_eta(runs(1, 1), 0.25 * H) == (None, False)


def test_a_finish_clock_carries_the_weekday_only_when_it_is_not_today():
    assert fmt_when(NOW + 2 * H, NOW) == "14:00"
    assert fmt_when(NOW + 26 * H, NOW) == datetime.fromtimestamp(NOW + 26 * H).strftime("%a %H:%M")
    assert fmt_when(None, NOW) == "—"


# -- weekly limit ------------------------------------------------------------------
def limits(pct: float, resets_in_h: float, samples=()) -> Limits:
    return Limits(observed_at=NOW, week_pct=pct, week_resets_at=NOW + resets_in_h * H,
                  samples=tuple(samples))


def test_the_pace_needs_a_real_span_of_recent_samples():
    assert week_pace([(NOW - 600, 40.0), (NOW, 41.0)], NOW) is None  # 10 minutes is noise
    assert week_pace([(NOW - 2 * H, 40.0), (NOW, 44.0)], NOW) == 2.0
    assert week_pace([(NOW - 30 * H, 10.0), (NOW - 29 * H, 12.0)], NOW) is None  # stale


def test_a_run_that_finishes_before_the_limit_says_so():
    lim = limits(40, resets_in_h=100, samples=[(NOW - 2 * H, 36.0), (NOW, 40.0)])  # 2%/h: full in 30h
    text, state = limit_outlook(lim, finish_in_s=10 * H, now=NOW)
    assert state == "ok" and "finishes" in text and "20h" in text


def test_a_run_the_limit_will_stop_is_red_with_the_time_it_hits():
    lim = limits(80, resets_in_h=100, samples=[(NOW - 2 * H, 72.0), (NOW, 80.0)])  # 4%/h: full in 5h
    text, state = limit_outlook(lim, finish_in_s=10 * H, now=NOW)
    assert state == "bad" and "limit in ~5h" in text


def test_a_pace_that_outlasts_the_window_is_fine_whatever_the_run_needs():
    lim = limits(80, resets_in_h=3, samples=[(NOW - 2 * H, 78.0), (NOW, 80.0)])  # full in 20h
    assert limit_outlook(lim, finish_in_s=50 * H, now=NOW)[1] == "ok"


def test_unknowns_are_said_not_guessed():
    assert limit_outlook(None, 5 * H, NOW)[1] == "muted"
    assert "pace unknown" in limit_outlook(limits(40, 50), 5 * H, NOW)[0]
    assert limit_outlook(limits(100, 50), 5 * H, NOW)[1] == "bad"


# -- from the tap's files to the dashboard -------------------------------------------
def payload(week, resets, session="s1"):
    return {"session_id": session, "context_window": {"total_input_tokens": 1000},
            "rate_limits": {"seven_day": {"used_percentage": week, "resets_at": resets}}}


def test_limits_come_from_the_freshest_worker_and_only_the_current_window(tmp_path):
    old_window, window = NOW - H, NOW + 50 * H
    meters.record(payload(90, old_window), tmp_path, "P0", now=NOW - 3 * H)  # already reset
    meters.record(payload(40, window), tmp_path, "P1", now=NOW - 2 * H)
    meters.record(payload(44, window), tmp_path, "P2", now=NOW)

    lim = load_limits(load_meters(tmp_path / "meters"), tmp_path / "meters" / "limits.jsonl", now=NOW)

    assert (lim.week_pct, lim.week_resets_at) == (44.0, window)
    assert lim.samples == ((NOW - 2 * H, 40.0), (NOW, 44.0))
    assert week_pace(lim.samples, NOW) == 2.0


def test_torn_meter_files_cost_one_worker_not_the_dashboard(tmp_path):
    d = tmp_path / "meters"
    d.mkdir()
    (d / "bad.json").write_text("{not json")
    (d / "odd.json").write_text(json.dumps(["a list"]))
    assert load_meters(d) == {}
    assert load_limits({}, d / "limits.jsonl") is None
