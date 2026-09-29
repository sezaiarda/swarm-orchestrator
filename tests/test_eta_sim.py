"""The ETA simulator on tiny ledgers whose answers are known by hand.

Every row here takes exactly one hour (a log-normal with no spread), nothing
files follow-ups and the swarm works every hour, unless a test says otherwise,
so a finish time is arithmetic: the tests pin that the replay honours what the
swarm honours — ``needs``, the worker count, ledger order, ``after:`` dates, a
scheduled pause, the build slot and a usage cap — and nothing it does not.
"""

from __future__ import annotations

import math

from swarm_orchestrator import ledger
from swarm_orchestrator.eta import hazards, holds, model, plan, sim

H = 3600.0
NOW = 1_790_000_000.0
#: One hour of work for every row, to the second.
HOUR = model.Durations(mu=math.log(H), sigma=1e-9, shared=0.0)
QUIET = hazards.Hazards(follow_all=0.0)
RUNS = sim.Options(runs=3)


def plan_of(text: str, workers: int = 2, busy: dict | None = None, build_slots: int = 0,
            now: float = NOW, excluded=()) -> plan.Plan:
    graph = ledger.parse(text)
    landed = ledger.with_ticked({}, ledger.ticked(text), set(busy or ()))
    return plan.build(graph, text, landed, now=now, busy=busy or {}, workers=workers,
                      build_slots=build_slots, excluded=set(excluded))


def finishes(p: plan.Plan, calendar: holds.Holds = holds.Holds(), opts=RUNS,
             durations=HOUR, hz=QUIET) -> dict[str, float]:
    """Hours after ``now`` each row finishes (to a second), the same in every replay."""
    futures = sim.simulate(p, durations, hz, calendar, opts)
    hours = [{r: round((t - p.now) / H, 4) if math.isfinite(t) else t
              for r, t in f.finish.items()} for f in futures]
    assert all(h == hours[0] for h in hours)
    return hours[0]


def near(got: dict, want: dict) -> bool:
    return all(math.isclose(got[k], v, abs_tol=1e-3) for k, v in want.items())


CHAIN = ("- [ ] `a-W1` · needs:—\n- [ ] `a-W2` · needs:`a-W1`\n"
         "- [ ] `a-W3` · needs:`a-W2`\n")
FAN = "".join(f"- [ ] `f-W{i}` · dir:`r{i}` · needs:—\n" for i in range(1, 5))


def test_a_serial_chain_finishes_one_row_after_another():
    got = finishes(plan_of(CHAIN, workers=4))
    assert near(got, {"a-W1": 1, "a-W2": 2, "a-W3": 3})


def test_a_fan_out_is_as_fast_as_the_workers_allow():
    assert max(finishes(plan_of(FAN, workers=2)).values()) == 2.0
    assert max(finishes(plan_of(FAN, workers=4)).values()) == 1.0
    assert max(finishes(plan_of(FAN, workers=8)).values()) == 1.0


def test_same_repo_rows_serialize_through_their_needs_chain():
    """The supervisor has no per-repo lock: the ledger's same-repo gate makes two
    rows in one repo a ``needs`` chain, and the replay honours the chain."""
    chained = ("- [ ] `b-W1` · dir:`frontend` · needs:—\n"
               "- [ ] `b-W2` · dir:`frontend` · needs:`b-W1`\n")
    assert near(finishes(plan_of(chained, workers=4)), {"b-W1": 1, "b-W2": 2})
    apart = ("- [ ] `b-W1` · dir:`frontend` · needs:—\n"
             "- [ ] `e-W1` · dir:`payments` · needs:—\n")
    assert near(finishes(plan_of(apart, workers=4)), {"b-W1": 1, "e-W1": 1})


def test_free_seats_go_to_ready_rows_in_ledger_order():
    text = ("- [ ] `z-W1` · needs:—\n- [ ] `a-W1` · needs:—\n- [ ] `m-W1` · needs:—\n")
    assert near(finishes(plan_of(text, workers=1)), {"z-W1": 1, "a-W1": 2, "m-W1": 3})


def test_an_after_date_holds_a_row_until_that_day_starts():
    gate = plan.day_start("2099-01-02")
    text = "- [ ] `p-F1` · needs:— · after:`2099-01-02`\n- [ ] `p-F2` · needs:—\n"
    got = finishes(plan_of(text, now=gate - 5 * H))
    assert near(got, {"p-F2": 1, "p-F1": 6})


def test_a_scheduled_pause_stops_launches_and_lets_running_rows_finish():
    busy = {"a-W1": NOW - 0.5 * H}
    one = plan_of(CHAIN, workers=1, busy=busy)
    got = finishes(one, holds.Holds(pause_at=NOW + 0.25 * H))
    assert near(got, {"a-W1": 0.5}) and got["a-W2"] == math.inf == got["a-W3"]
    later = finishes(one, holds.Holds(pause_at=NOW + 1.25 * H))
    assert near(later, {"a-W1": 0.5, "a-W2": 1.5}) and later["a-W3"] == math.inf


def test_the_build_slot_serializes_builds_across_workers():
    all_build = sim.Options(runs=3, build_share=1.0)
    two = "- [ ] `a-W1` · needs:—\n- [ ] `b-W1` · needs:—\n"
    assert max(finishes(plan_of(two, workers=2, build_slots=1), opts=all_build).values()) == 2.0
    assert max(finishes(plan_of(two, workers=2, build_slots=2), opts=all_build).values()) == 1.0
    # Switched off (the default), the slot is not a constraint at all.
    assert max(finishes(plan_of(two, workers=2, build_slots=1)).values()) == 1.0


def test_a_usage_cap_holds_launches_until_its_window_resets():
    """89% with 2 points per busy worker-hour crosses 90% half an hour in; the
    next row waits for the reset ten hours out, like the swarm will."""
    cap = holds.Cap("five_hour", pct=89.0, at=90.0, resets_at=NOW + 10 * H, burn=2.0)
    got = finishes(plan_of(CHAIN, workers=1), holds.Holds(caps=(cap,)))
    assert near(got, {"a-W1": 1, "a-W2": 11, "a-W3": 12})
    assert near(finishes(plan_of(CHAIN, workers=1), holds.Holds(caps=(cap,)),
                         opts=sim.Options(runs=3, caps=False)), {"a-W3": 3})


def test_a_swarm_that_works_half_its_hours_finishes_later():
    ten = "".join(f"- [ ] `s-W{i}` · needs:{'`s-W%d`' % (i - 1) if i > 1 else '—'}\n"
                  for i in range(1, 11))
    p = plan_of(ten, workers=1)
    half = holds.Holds(availability=holds.Availability(share=0.5))
    full = sim.simulate(p, HOUR, QUIET, holds.Holds(), sim.Options(runs=40))
    slow = sim.simulate(p, HOUR, QUIET, half, sim.Options(runs=40))
    median = sorted(f.finish["s-W10"] for f in slow)[20]
    assert all(math.isclose(f.finish["s-W10"], NOW + 10 * H, abs_tol=1) for f in full)
    assert median > NOW + 14 * H


def test_a_running_row_carries_on_from_its_age():
    busy = {"a-W1": NOW - 0.25 * H}
    got = finishes(plan_of(CHAIN, workers=1, busy=busy))
    assert near(got, {"a-W1": 0.75, "a-W2": 1.75, "a-W3": 2.75})


def test_the_same_seed_gives_the_same_forecast_and_an_unrelated_row_moves_nothing():
    spread = model.Durations(mu=math.log(H), sigma=1.0, shared=0.25)
    base = plan_of(CHAIN, workers=4)
    more = plan_of(CHAIN + "- [ ] `x-W1` · needs:—\n", workers=4)
    one = sim.simulate(base, spread, QUIET, holds.Holds(), sim.Options(runs=20))
    two = sim.simulate(base, spread, QUIET, holds.Holds(), sim.Options(runs=20))
    three = sim.simulate(more, spread, QUIET, holds.Holds(), sim.Options(runs=20))
    assert [f.finish for f in one] == [f.finish for f in two]
    assert [f.finish["a-W3"] for f in one] == [f.finish["a-W3"] for f in three]


def test_follow_ups_extend_a_campaign_and_stop_three_deep():
    busy_campaign = hazards.Hazards(follow={"a": 5.0}, follow_all=0.0)
    futures = sim.simulate(plan_of(CHAIN, workers=4), HOUR, busy_campaign, holds.Holds(),
                           sim.Options(runs=5))
    for f in futures:
        assert f.grown["a"] >= f.finish["a-W1"] + H - 1
        # a replay files at most MAX_GROWTH times the rows it started with
        assert f.filed["a"] == int(sim.MAX_GROWTH * 3)
    none = sim.simulate(plan_of(CHAIN, workers=4), HOUR, busy_campaign, holds.Holds(),
                        sim.Options(runs=5, growth=False))
    assert all(not f.grown for f in none)


def test_the_critical_chain_walks_back_from_the_last_finish():
    (future,) = sim.simulate(plan_of(CHAIN + FAN, workers=8), HOUR, QUIET, holds.Holds(),
                             sim.Options(runs=1))
    assert future.chain == ("a-W1", "a-W2", "a-W3")


def test_rows_behind_the_owner_are_never_scheduled():
    text = CHAIN + "- [ ] `o-W1` · needs:—\n- [ ] `o-W2` · needs:`o-W1`\n"
    p = plan_of(text, workers=4, excluded={"o-W1"})
    assert "o-W1" not in p.rows and "o-W2" not in p.rows
    assert set(finishes(p)) == {"a-W1", "a-W2", "a-W3"}
