"""The upper band's calibration: its score, its stretch, its fit and its record.

* **the stretch** leaves the median alone and moves P85 and P95 out in log time;
* **the fit** is the conformal 85% quantile of the scores, read through
  Kaplan-Meier when campaigns are still open, and falls back to the caller's
  default when there is too little to say;
* **the runtime** logs its own forecasts, scores them against the ledger's ticks,
  and applies the default until enough of them have finished.
"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest

from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.eta import calibrate, engine, forecast
from swarm_orchestrator.tui import books

H = 3600.0
NOW = 1_790_000_000.0


# -- the score and the stretch ------------------------------------------------------------
def test_the_score_is_the_truths_distance_above_the_median_in_p85s():
    assert calibrate.score(10.0, 20.0, 10.0) == pytest.approx(0.0)
    assert calibrate.score(10.0, 20.0, 20.0) == pytest.approx(1.0)
    assert calibrate.score(10.0, 20.0, 40.0) == pytest.approx(2.0)
    assert calibrate.score(10.0, 20.0, 5.0) < 0
    # A collapsed spread is measured against the floor, not divided by nothing.
    assert calibrate.score(10.0, 10.0, 12.0) == pytest.approx(1.0)


def test_the_stretch_keeps_the_median_and_moves_the_tail_in_log_time():
    assert calibrate.stretch_one(20.0, 10.0, 2.0) == pytest.approx(40.0)
    assert calibrate.stretch_one(8.0, 10.0, 2.0) == 8.0  # below the median: untouched
    assert calibrate.stretch_one(math.inf, 10.0, 2.0) == math.inf
    assert calibrate.stretch_one(20.0, 10.0, 1.0) == 20.0
    r = forecast.Range(NOW + 10 * H, NOW + 20 * H, NOW + 30 * H)
    got = calibrate.stretch(r, NOW, 2.0)
    assert got.p50 == r.p50
    assert got.p85 == pytest.approx(NOW + 40 * H) and got.p95 == pytest.approx(NOW + 90 * H)
    # A longer forecast with the same shape widens by more hours.
    long = calibrate.stretch(forecast.Range(NOW + 100 * H, NOW + 200 * H, NOW + 300 * H),
                             NOW, 2.0)
    assert long.p85 - long.p50 > got.p85 - got.p50
    assert calibrate.stretch(None, NOW, 2.0) is None
    never = forecast.Range(math.inf, math.inf, math.inf)
    assert calibrate.stretch(never, NOW, 2.0) == never


def test_every_range_a_forecast_shows_is_stretched_but_a_floor_is_not():
    rng = forecast.Range(NOW + 10 * H, NOW + 20 * H, NOW + 30 * H)
    row = forecast.RowView("a-W1", "ready", "", rng)
    book = forecast.Book("a", 0, 1, 0, 1, finish=rng, rows=(row,))
    fc = forecast.Forecast(made_at=NOW, runs=100, overall=rng, books=(book,))
    got = calibrate.apply(fc, calibrate.Calibration(2.0, 7))
    stretched = calibrate.stretch(rng, NOW, 2.0)
    assert got.overall == stretched and got.books[0].finish == stretched
    assert got.books[0].rows[0].finish == stretched
    assert (got.stretch, got.calibrated_on) == (2.0, 7)
    assert forecast.Forecast.from_json(got.to_json()) == got
    assert books.basis_line(got, NOW).endswith(
        "upper band stretched k=2.00 (from 7 finished campaigns)")
    default = calibrate.apply(fc, calibrate.Calibration(2.75, 0))
    assert "the backtest's, too little history here yet" in books.basis_line(default, NOW)
    assert "upper band" not in books.basis_line(fc, NOW)
    floor = forecast.Forecast(made_at=NOW, runs=0, overall=rng, books=(book,))
    assert calibrate.apply(floor, calibrate.Calibration(2.0, 7)).overall == rng


# -- the fit ----------------------------------------------------------------------------
def test_k_is_the_conformal_quantile_of_the_finished_scores():
    pairs = [(i / 10, True) for i in range(1, 21)]  # 0.1 … 2.0
    # n = 20: the ⌈21·0.85⌉ = 18th smallest.
    assert calibrate.fit(pairs).k == pytest.approx(1.8)
    assert calibrate.fit(pairs).n == 20


def test_k_never_narrows_the_replays():
    assert calibrate.fit([(0.1, True)] * 10).k == 1.0


def test_an_open_campaign_counts_as_at_least_what_has_passed():
    done = [(0.5, True)] * 8
    # Two still open past 3: the quantile has to come from above them, and
    # nothing finished there, so the estimate never reaches 85%.
    assert calibrate.km_quantile(done + [(3.0, False)] * 2, 0.85) is None
    # Open ones below the finished scores leave the estimate on the finished ones.
    assert calibrate.km_quantile(done + [(0.1, False)] * 2, 0.85) == 0.5
    # Kaplan-Meier, not the plain sort (which says 6): the three open at 3.5 are
    # somewhere above it, so their weight moves up onto the scores that finished there.
    pairs = [(float(v), True) for v in range(1, 8)] + [(3.5, False)] * 3
    assert calibrate.km_quantile(pairs, 0.85) == 7.0


def test_too_little_history_says_the_default():
    few = [(2.0, True)] * (calibrate.MIN_EVENTS - 1) + [(0.0, False)] * 20
    assert calibrate.fit(few) == calibrate.Calibration(1.0, 0)
    assert calibrate.fit(few, default=calibrate.DEFAULT_K).k == calibrate.DEFAULT_K
    open_heavy = [(0.5, True)] * calibrate.MIN_EVENTS + [(5.0, False)] * 10
    assert calibrate.fit(open_heavy, default=2.5) == calibrate.Calibration(2.5, 0)


# -- the runtime's record ---------------------------------------------------------------------
def test_a_forecast_is_logged_once_per_interval_and_old_ones_are_dropped(tmp_path):
    path = tmp_path / calibrate.LOG_NAME
    rng = forecast.Range(NOW + 10 * H, NOW + 20 * H, NOW + 30 * H)
    ranges = {"a": (rng, ["a-W1"]), "never": (forecast.Range(*[math.inf] * 3), ["n-W1"])}
    assert calibrate.record(path, NOW, ranges)
    assert not calibrate.record(path, NOW + calibrate.LOG_EVERY_S - 1, ranges)
    assert calibrate.record(path, NOW + calibrate.LOG_EVERY_S, ranges)
    entries = calibrate.load(path)
    assert [e["at"] for e in entries] == [NOW, NOW + calibrate.LOG_EVERY_S]
    assert set(entries[0]["books"]) == {"a"}  # nothing to score without a finite P85
    later = NOW + calibrate.KEEP_S + calibrate.LOG_EVERY_S + 1
    assert calibrate.record(path, later, {"a": (forecast.Range(later + H, later + 2 * H,
                                                               later + 3 * H), ["a-W1"])})
    assert [e["at"] for e in calibrate.load(path)] == [later]
    path.write_text(path.read_text() + "torn{\n")
    assert len(calibrate.load(path)) == 1


def test_logged_forecasts_are_scored_against_the_ledgers_ticks():
    entries = [{"at": NOW, "books": {"a": [NOW + 10 * H, NOW + 20 * H, ["a-W1", "a-W2"]],
                                     "b": [NOW + 10 * H, NOW + 20 * H, ["b-W1"]]}}]
    ticks = {"a-W1": (NOW + 5 * H, 1), "a-W2": (NOW + 40 * H, 1)}
    got = calibrate.pairs_from_log(entries, ticks, NOW + 50 * H)
    assert got == [(pytest.approx(calibrate.score(10, 20, 40)), True),
                   (pytest.approx(calibrate.score(10, 20, 50)), False)]


# -- the engine ------------------------------------------------------------------------------
@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / ".swarm.toml").write_text('[swarm]\ndriver = "bare"\nmax_workers = 2\n'
                                         '[web]\nenabled = false\n', encoding="utf-8")
    (project / "docs" / "PHASE-LEDGER.md").write_text(
        "- [ ] `a-W1` · needs:—\n- [ ] `a-W2` · needs:`a-W1`\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_DRIVER", raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


def test_with_no_history_the_engine_uses_the_default_and_starts_its_record(cfg):
    st = state_mod.read(cfg)
    fc = engine.current(cfg, engine.from_files(cfg, st), runs=20)
    assert (fc.stretch, fc.calibrated_on) == (calibrate.DEFAULT_K, 0)
    a = next(b for b in fc.books if b.name == "a")
    assert a.finish.p85 - a.finish.p50 > 0
    logged = calibrate.load(calibrate.log_path(cfg))
    assert len(logged) == 1 and logged[0]["books"]["a"][2] == ["a-W1", "a-W2"]
    raw = json.loads(calibrate.log_path(cfg).read_text())["books"]["a"]
    assert raw[1] <= a.finish.p85  # the record keeps what the replays said, never a stretch


def test_a_paused_swarms_forecast_is_not_logged(cfg):
    st = state_mod.read(cfg)
    st.paused = True
    engine.compute(engine.from_files(cfg, st), runs=10, log=calibrate.log_path(cfg))
    assert calibrate.load(calibrate.log_path(cfg)) == []


def test_enough_finished_history_replaces_the_default(cfg, monkeypatch):
    st = state_mod.read(cfg)
    inputs = engine.from_files(cfg, st)
    entries = [{"at": NOW + i * H, "books": {"a": [NOW + (i + 10) * H, NOW + (i + 20) * H,
                                                   [f"x-W{i}"]]}} for i in range(10)]
    ticks = {f"x-W{i}": (NOW + (i + 5) * H, 1) for i in range(10)}  # all well inside P85
    history = SimpleNamespace(**{**inputs.ledger_history.__dict__, "ticks": ticks})
    got = engine.fit(engine.Inputs(**{**inputs.__dict__, "forecasts": tuple(entries),
                                      "ledger_history": history, "now": NOW + 30 * H}))
    assert got.calibration == calibrate.Calibration(1.0, 10)
