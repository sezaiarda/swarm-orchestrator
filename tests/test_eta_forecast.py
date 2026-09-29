"""From the ledger to what every view says: the plan, the calendar, the forecast.

* **the plan** keeps out what waits on the owner — an owner-run row, a worker's
  question, a failed row, a cycle — and everything behind it, and says what;
* **the calendar** measures availability from delivered rows and turns the usage
  rules (and the account's own 100%) into caps;
* **the forecast** reads ranges off replays, falls back to a floor that is never
  late, and survives the JSON cache every process shares;
* **the words** are one set of functions for the TUI, ``swarm status`` and the
  web board;
* **the engine** computes on its own thread and shares its answer on disk.
"""

from __future__ import annotations

import math
import time
from types import SimpleNamespace

import pytest

from swarm_orchestrator import ledger
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.eta import engine, forecast, hazards, holds, model, plan, sim
from swarm_orchestrator.tui import books

H = 3600.0
NOW = 1_790_000_000.0
HOUR = model.Durations(mu=math.log(H), sigma=1e-9, shared=0.0)

LEDGER = (
    "- [x] `a-W0` · dir:`frontend` · needs:—\n"
    "- [ ] `a-W1` · dir:`frontend` · needs:`a-W0`\n"
    "- [ ] `a-W2` · dir:`frontend` · needs:`a-W1`\n"
    "- [ ] `o-W1` · needs:—\n"
    "- [ ] `o-W2` · needs:`o-W1`\n"
    "- [ ] `b-W1` · needs:`o-W2`\n"
    "- [ ] `q-W1` · needs:—\n"
    "- [ ] `q-W2` · needs:`q-W1`\n"
    "- [ ] `d-W1` · needs:— · after:`{date}`\n"
)
#: Two days after NOW: inside a forecast's horizon.
SOON = "2026-09-23"


def build(text=LEDGER, **kw) -> plan.Plan:
    text = text.replace("{date}", SOON)
    graph = ledger.parse(text)
    landed = ledger.with_ticked(kw.pop("done", {}), ledger.ticked(text))
    return plan.build(graph, text, landed, now=NOW, **({"busy": {}, "workers": 2} | kw))


# -- the plan --------------------------------------------------------------------------
def test_what_waits_on_the_owner_is_left_out_and_says_what_is_behind_it():
    p = build(excluded={"o-W1"}, asking={"q-W1"}, busy={"q-W1": NOW - H})
    stuck = {s.row: s for s in p.stuck}
    assert stuck["o-W1"].why == plan.OWNER_RUN and stuck["o-W1"].behind == ("o-W2", "b-W1")
    assert stuck["q-W1"].why == plan.ASKED and stuck["q-W1"].behind == ("q-W2",)
    assert set(p.rows) == {"a-W1", "a-W2", "d-W1"}
    assert p.held == {"o-W1", "o-W2", "b-W1", "q-W1", "q-W2"}


def test_a_failed_row_and_a_cycle_wait_on_the_owner_too():
    text = LEDGER + "- [ ] `c-W1` · needs:`c-W2`\n- [ ] `c-W2` · needs:`c-W1`\n"
    p = build(text, done={"a-W1": "fail"})
    why = {s.row: s.why for s in p.stuck}
    assert why["a-W1"] == plan.FAILED and "a-W2" in p.held
    assert why["c-W1"] == why["c-W2"] == plan.CYCLE


def test_a_rows_repo_is_its_first_dir_else_the_repo_its_id_names():
    assert plan.meta_for("a-W1", {"a-W1": ["orders", "frontend"]}).repo == "orders"
    assert plan.meta_for("billing-P3", {}) == model.Meta("P", "billing", "billing")


def test_the_counts_keep_a_dated_row_out_of_ready():
    p = build()
    assert p.books["d"].gated == {"d-W1": SOON} and p.books["d"].ready == ()
    assert p.books["a"].ready == ("a-W1",) and (p.books["a"].done, p.books["a"].total) == (1, 3)
    assert p.gates["d-W1"] == plan.day_start(SOON)


# -- the calendar ---------------------------------------------------------------------
def test_availability_is_what_was_delivered_over_what_the_workers_could_have():
    ticks = [NOW - i * 7200.0 for i in range(1, 37)]  # one row every two hours, three days
    got = holds.availability(ticks, [(0.0, 2)], NOW, mean_work_s=H)
    assert got.share == pytest.approx(36 / (72 * 2), rel=0.02)
    assert holds.availability([NOW - 600], [(0.0, 2)], NOW, H).share == 1.0  # too little history


def test_each_future_draws_its_own_availability_around_the_measured_one():
    a = holds.Availability(share=0.3)
    draws = [a.draw((i + 0.5) / 100) for i in range(100)]
    assert min(draws) < 0.3 < max(draws) and 0 < min(draws) and max(draws) < 1
    assert sorted(draws) == draws  # a quantile draw, so futures are ordered by it
    assert holds.Availability().draw(0.01) == 1.0


def test_caps_come_from_the_rules_and_the_accounts_own_limit():
    from swarm_orchestrator.usage import Sample

    sample = [Sample(NOW - 60, week_pct=80.0, week_resets_at=NOW + 2 * 86400,
                     five_pct=10.0, five_resets_at=NOW + H)]
    rules = [{"window": "week", "at": 90.0, "action": "pause"},
             {"window": "week", "at": 95.0, "action": "down"}]
    st = state_mod.State()
    got = {c.window: c for c in holds.from_state(st, rules, sample, {"week": 0.5}, NOW).caps}
    assert (got["week"].at, got["week"].pct, got["week"].burn) == (90.0, 80.0, 0.5)
    assert got["five_hour"].at == holds.LIMIT  # no rule: 100% still stops every worker
    held = state_mod.State(usage_hold={"week": {"pct": 91, "at": 90, "resets_at": NOW + H}})
    calendar = holds.from_state(held, rules, sample, {}, NOW)
    assert calendar.held_until == NOW + H
    paused = holds.from_state(state_mod.State(paused=True, pause_at=NOW - 5), [], [], {}, NOW)
    assert paused.stopped == holds.PAUSED and paused.pause_at == NOW


# -- the forecast -----------------------------------------------------------------------
def test_ranges_are_read_off_the_replays():
    r = forecast.Range.of([float(i) for i in range(1, 101)])
    assert (r.p50, r.p85, r.p95) == (50.0, 85.0, 95.0)


def test_campaigns_finish_with_their_last_row_soonest_first():
    p = build(workers=4, excluded={"o-W1"})
    futures = sim.simulate(p, HOUR, hazards.Hazards(follow_all=0.0), holds.Holds(),
                           sim.Options(runs=5))
    fc = forecast.summarise(p, holds.Holds(), futures, NOW)
    names = [b.name for b in fc.books]
    assert names[:2] == ["q", "a"] and names[-2:] == ["b", "o"]  # they wait on you
    a = next(b for b in fc.books if b.name == "a")
    assert a.finish.p50 == pytest.approx(NOW + 2 * H, abs=1)
    assert fc.books[-1].finish is None and fc.critical[-1] == "d-W1"
    assert [r.state for r in a.rows] == ["ready", "blocked"]


def test_the_floor_is_the_longest_chain_or_the_work_over_the_workers():
    one = build(workers=1, excluded={"o-W1"})
    fc = forecast.floor(one, HOUR, holds.Holds(), NOW)
    assert fc.floor and fc.runs == 0
    # Each campaign: its own chain, or its own work over the one worker (2 h each);
    # overall, all five hours of work or d-W1's date and its hour, whichever is later.
    q = next(b for b in fc.books if b.name == "q")
    assert q.finish.p50 == pytest.approx(NOW + 2 * H, abs=1)
    assert fc.overall.p50 == pytest.approx(plan.day_start(SOON) + H, abs=1)


def test_a_forecast_survives_the_shared_cache_infinities_and_all():
    p = build(workers=2, excluded={"o-W1"})
    fc = forecast.summarise(p, holds.Holds(pause_at=NOW + 0.5 * H),
                            sim.simulate(p, HOUR, hazards.Hazards(), holds.Holds(
                                pause_at=NOW + 0.5 * H), sim.Options(runs=4)), NOW)
    assert any(not math.isfinite(b.finish.p50) for b in fc.books if b.finish)
    back = forecast.Forecast.from_json(fc.to_json())
    assert back == fc


# -- the words --------------------------------------------------------------------------
def words_forecast(**kw) -> forecast.Forecast:
    p = build(workers=2, excluded={"o-W1"})
    futures = sim.simulate(p, HOUR, hazards.Hazards(follow_all=0.0), holds.Holds(),
                           sim.Options(runs=5))
    fc = forecast.summarise(p, holds.Holds(), futures, NOW)
    return forecast.Forecast(**(fc.__dict__ | kw))


def test_every_view_says_the_forecast_in_the_same_words():
    fc = words_forecast(caps=(("week", 90.0, 76.0),), working=0.46)
    assert books.overall_line(fc, NOW).startswith("5 rows left · done ~")
    basis = books.basis_line(fc, NOW)
    assert basis == ("simulated 5× · 2 workers · working 46% of the time, as lately"
                     " · weekly cap pauses at 90% (76% now)")
    assert books.waiting_line(fc) == "waiting on you: o-W1 holds 2 rows (o, b)"
    a = next(b for b in fc.books if b.name == "a")
    line = books.book_line(a, fc, NOW, 90)
    assert line.startswith("a            1/3") and "2 left · 1 ready" in line
    status = "\n".join(books.status_lines(fc, NOW))
    assert f"critical path: d-W1 (waits until {SOON})" in status
    detail = books.detail(a, fc, NOW)
    assert "almost surely by" in detail and "a-W2  blocked by a-W1" in detail


def test_a_floor_a_pause_and_a_row_past_the_pause_say_so():
    fc = words_forecast()
    floor = forecast.Forecast(**(fc.__dict__ | {"runs": 0}))
    assert "(simulating…)" in books.overall_line(floor, NOW)
    assert books.basis_line(floor, NOW).startswith("a floor")
    paused = forecast.Forecast(**(fc.__dict__ | {"stopped": holds.PAUSED}))
    assert "paused · resume now and it is done ~" in books.overall_line(paused, NOW)
    past = forecast.Forecast(**(fc.__dict__ | {"pause_at": NOW + H}))
    assert books.when(math.inf, past, NOW) == "after the scheduled pause"
    assert books.when(math.inf, fc, NOW) == "not in sight"


# -- the engine -----------------------------------------------------------------------
@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / ".swarm.toml").write_text('[swarm]\ndriver = "bare"\nmax_workers = 2\n'
                                         '[web]\nenabled = false\n[tasks]\n'
                                         'exclude = ["o-W1"]\n', encoding="utf-8")
    (project / "docs" / "PHASE-LEDGER.md").write_text(LEDGER.replace("{date}", "2999-01-01"),
                                                      encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_DRIVER", raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


def test_one_forecast_is_made_and_every_reader_after_shares_it(cfg):
    st = state_mod.read(cfg)
    first = engine.current(cfg, engine.from_files(cfg, st), runs=20)
    assert first.runs == 20 and engine.cache_path(cfg).is_file()
    again = engine.current(cfg, engine.from_files(cfg, st), runs=999)
    assert again == first  # read from the cache, not simulated again
    (cfg.project_dir / cfg.ledger).write_text(LEDGER.replace("{date}", "2999-01-01")
                                              + "- [ ] `a-W3` · needs:`a-W2`\n")
    moved = engine.current(cfg, engine.from_files(cfg, st), runs=20)
    assert moved != first and "a-W3" in {r.id for b in moved.books for r in b.rows}


def test_the_worker_publishes_off_the_callers_thread_and_ignores_a_repeat(cfg):
    st = state_mod.read(cfg)
    eng = engine.Engine(cfg, runs=10)
    asked = []

    def make():
        asked.append(time.time())
        return engine.from_files(cfg, st)

    eng.request(("same",), make)
    assert eng.wait(30) and eng.result is not None and eng.result.runs == 10
    eng.request(("same",), make)
    assert eng.wait(30) and len(asked) == 1


def test_swarm_status_prints_the_forecast(cfg, capsys):
    from swarm_orchestrator import cli

    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert "eta: 5 rows left · done ~" in out and "waiting on you: o-W1 holds 2 rows" in out
    assert "phases:" in out and "1 waiting for a date" in out
    assert cli.cmd_status(cfg, as_json=True) == 0


def test_a_broken_forecast_never_breaks_status(cfg, capsys, monkeypatch):
    from swarm_orchestrator import cli

    monkeypatch.setattr(engine, "from_files", SimpleNamespace)  # raises on the state arg
    assert cli.cmd_status(cfg) == 0
    assert "eta: unavailable (TypeError" in capsys.readouterr().out
