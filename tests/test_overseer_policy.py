"""The Overseer's trigger policy and starvation map (pure: no supervisor, no session).

When a pass runs is a policy question with a few sharp edges: the first look at a
long-lived run must not report its whole history, one condition must be one
reason, a pass never overlaps another, non-urgent reasons respect the gap while
urgent ones only wait for the running pass, and the memory survives a restart.
"""

from __future__ import annotations

import pytest

from swarm_orchestrator import overseer as ov
from swarm_orchestrator.config import load
from swarm_orchestrator.state import Slot, State

T0 = 1_800_000_000.0


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_OVERSEER", "1")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".swarm.toml").write_text(
        "[worker]\npark_after = 120\n", encoding="utf-8"
    )
    c = load(project_dir=str(project))
    c.ensure_dirs()
    return c


def _st(done=None, **kw) -> State:
    st = State(slots=[Slot(id=0), Slot(id=1)], done=dict(done or {}))
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def _keys(pol: ov.Policy) -> list[str]:
    return [r.key for r in pol.pending]


def test_defaults_match_the_plan(cfg):
    assert cfg.overseer_enabled is True
    assert (cfg.overseer_min_gap_s, cfg.overseer_every_finished, cfg.overseer_every_s) == (600, 3, 10800)
    assert (cfg.overseer_owner_wait_s, cfg.overseer_starve_s, cfg.overseer_timeout_s) == (3600, 600, 2700)
    assert cfg.overseer_hold_wait_s == 600


def test_the_first_look_baselines_history_instead_of_reporting_it(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st({"A": "ok", "B": "fail", "C": "ok", "D": "ok"}), T0)
    assert pol.pending == []
    assert pol.mem.finished_since == 0


def test_a_new_failure_is_a_reason_and_due_at_once_when_no_pass_ran_yet(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st({"A": "ok"}), T0)
    pol.observe(_st({"A": "ok", "B": "fail"}), T0 + 5)
    assert _keys(pol) == ["fail:B"]
    assert pol.pending[0].urgent is False
    assert pol.due(T0 + 5)


def test_every_n_finished_counts_builds_and_failures_but_not_skips(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st({}), T0)
    pol.observe(_st({"A": "ok", "S": "skip"}), T0 + 1)
    pol.observe(_st({"A": "ok", "S": "skip", "B": "operator"}), T0 + 2)
    assert ov.FINISHED not in _keys(pol)
    pol.observe(_st({"A": "ok", "S": "skip", "B": "operator", "C": "fail"}), T0 + 3)
    assert ov.FINISHED in _keys(pol)
    assert pol.mem.finished_since == 3


def test_a_pass_takes_every_reason_and_resets_the_counter(cfg):
    pol = ov.Policy(cfg)
    pol.request("a", "one", now=T0)
    pol.request("b", "two", now=T0)
    pol.mem.finished_since = 2
    taken = pol.begin(T0 + 1)
    assert [r.key for r in taken] == ["a", "b"]
    assert pol.pending == [] and pol.mem.finished_since == 0
    assert not pol.due(T0 + 2)


def test_one_condition_is_one_reason(cfg):
    pol = ov.Policy(cfg)
    assert pol.request("hold:P", "held", now=T0) is True
    assert pol.request("hold:P", "held again", now=T0 + 1) is False
    assert len(pol.pending) == 1


def test_min_gap_holds_ordinary_reasons_but_not_urgent_ones(cfg):
    pol = ov.Policy(cfg)
    pol.begin(T0)
    pol.request("fail:X", "X failed", now=T0 + 60)
    assert not pol.due(T0 + 60)
    assert not pol.due(T0 + 599)
    assert pol.due(T0 + 600)  # start to start
    pol.begin(T0 + 600)
    pol.request("hold:Y", "held", urgent=True, now=T0 + 610)
    assert pol.due(T0 + 610)


def test_never_two_passes_at_once_and_reasons_wait_for_the_running_one(cfg):
    pol = ov.Policy(cfg)
    pol.request(ov.MANUAL, "now", urgent=True, now=T0)
    assert not pol.due(T0, running=True)
    assert pol.due(T0, running=False)


def test_a_repeat_can_make_a_pending_reason_urgent(cfg):
    pol = ov.Policy(cfg)
    pol.begin(T0)
    pol.request("k", "x", now=T0 + 1)
    assert not pol.due(T0 + 2)
    pol.request("k", "x", urgent=True, now=T0 + 3)
    assert pol.due(T0 + 3)


def test_a_hold_fires_once_per_hold_and_again_after_it_clears(cfg):
    pol = ov.Policy(cfg)
    held = _st(integ_blocked="P2", integ_blocked_kind="conflict")
    pol.observe(held, T0)
    assert _keys(pol) == ["hold:P2"] and pol.pending[0].urgent
    pol.begin(T0 + 1)
    pol.observe(held, T0 + 2)
    assert pol.pending == []
    pol.observe(_st(), T0 + 3)
    pol.observe(held, T0 + 4)
    assert _keys(pol) == ["hold:P2"]


def _resolving(phase="P2", **kw) -> State:
    return _st(integ_blocked=phase, integ_blocked_kind="conflict",
               windows={f"resolve:{phase}": "@7"}, **kw)


def test_a_hold_the_resolver_is_on_waits_for_hold_wait_s(cfg):
    assert cfg.overseer_hold_wait_s == 600
    pol = ov.Policy(cfg)
    pol.observe(_resolving(), T0)
    assert pol.pending == []
    assert pol.next_deadline(T0) == T0 + 600  # the supervisor wakes for it
    pol.observe(_resolving(), T0 + 599)
    assert pol.pending == []
    pol.observe(_resolving(), T0 + 600)
    assert _keys(pol) == ["hold:P2"] and pol.pending[0].urgent
    assert "not cleared it in 10m" in pol.pending[0].text


def test_a_hold_the_resolver_cleared_never_triggers(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_resolving(), T0)
    pol.observe(_st(), T0 + 40)
    pol.observe(_st(), T0 + 4000)
    assert pol.pending == []


def test_a_resolver_that_gives_up_triggers_at_once(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_resolving(), T0)
    pol.resolver_escalated("P2")
    pol.observe(_resolving(), T0 + 30)
    assert _keys(pol) == ["hold:P2"]
    assert "could not fix it" in pol.pending[0].text


def test_the_escalation_belongs_to_one_hold(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_resolving(), T0)
    pol.resolver_escalated("P2")
    pol.observe(_resolving(), T0 + 1)
    pol.begin(T0 + 2)
    pol.observe(_st(), T0 + 3)
    pol.observe(_resolving(), T0 + 4)  # a new hold on the same phase
    assert pol.pending == []


def test_a_pending_hold_reason_is_dropped_once_the_hold_clears(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st(integ_blocked="P2", integ_blocked_kind="dirty"), T0)
    pol.request("fail:X", "x", now=T0)
    assert _keys(pol) == ["hold:P2", "fail:X"]
    assert pol.recheck(_st(), T0 + 5) == ["hold:P2"]
    assert _keys(pol) == ["fail:X"]
    assert pol.recheck(_st(), T0 + 6) == []


def test_recheck_keeps_a_hold_reason_that_still_applies(cfg):
    pol = ov.Policy(cfg)
    held = _st(integ_blocked="P2", integ_blocked_kind="dirty")
    pol.observe(held, T0)
    assert pol.recheck(held, T0 + 5) == []
    assert _keys(pol) == ["hold:P2"]


def test_a_newly_owed_push_fires_once(cfg):
    pol = ov.Policy(cfg)
    owed = {"/p/frontend": {"phase": "coral-W1", "reason": "pre-push hook refused"}}
    pol.observe(_st(push_owed=owed), T0)
    assert _keys(pol) == ["push:frontend"]
    pol.begin(T0 + 1)
    pol.observe(_st(push_owed=owed), T0 + 2)
    assert pol.pending == []


def test_a_phase_on_the_owner_too_long_fires_once_per_wait(cfg):
    pol = ov.Policy(cfg)
    # `waiting` holds the PARK deadline: asked = deadline - park_after (120).
    asked = T0 - 3000
    st = _st(waiting={"P1": asked + 120})
    pol.observe(st, T0)
    assert pol.pending == []
    pol.observe(st, asked + 3600)
    assert _keys(pol) == ["owner:P1"]
    pol.begin(asked + 3601)
    pol.observe(_st(parked=["P1"]), asked + 4000)  # parked now: same wait, no refire
    assert pol.pending == []
    assert pol.mem.owner_since["P1"] == asked


def test_a_parked_phase_counts_from_its_first_sighting(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st(parked=["Q"]), T0)
    pol.observe(_st(parked=["Q"]), T0 + 3599)
    assert pol.pending == []
    pol.observe(_st(parked=["Q"]), T0 + 3600)
    assert _keys(pol) == ["owner:Q"]
    pol.observe(_st(), T0 + 3700)  # answered: forgotten
    assert "Q" not in pol.mem.owner_since and "Q" not in pol.mem.owner_fired


def test_starvation_must_be_sustained_and_fires_once_per_episode(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st(), T0, starving=True)
    pol.observe(_st(), T0 + 599, starving=True)
    assert pol.pending == []
    pol.observe(_st(), T0 + 600, starving=True)
    assert _keys(pol) == [ov.STARVE] and pol.pending[0].urgent
    pol.begin(T0 + 601)
    pol.observe(_st(), T0 + 2000, starving=True)
    assert pol.pending == []  # same episode
    pol.observe(_st(), T0 + 2001, starving=False)
    pol.observe(_st(), T0 + 2002, starving=True)
    pol.observe(_st(), T0 + 2602, starving=True)
    assert _keys(pol) == [ov.STARVE]


def test_an_unjudged_starvation_leaves_the_episode_alone(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st(), T0, starving=True)
    pol.observe(_st(), T0 + 300, starving=None)
    assert pol.mem.starving_since == T0


def test_a_doctor_fail_fires_once_per_episode_and_an_unprobed_wake_changes_nothing(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st(), T0, doctor_fails={"ledger": "cycle A -> B"})
    assert _keys(pol) == ["doctor:ledger"] and pol.pending[0].urgent
    pol.begin(T0 + 1)
    pol.observe(_st(), T0 + 2, doctor_fails=None)
    pol.observe(_st(), T0 + 3, doctor_fails={"ledger": "cycle A -> B"})
    assert pol.pending == []
    pol.observe(_st(), T0 + 4, doctor_fails={})
    pol.observe(_st(), T0 + 5, doctor_fails={"ledger": "cycle A -> B"})
    assert _keys(pol) == ["doctor:ledger"]


def test_the_cadence_runs_from_the_last_pass_or_the_first_look(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st(), T0)
    pol.observe(_st(), T0 + 10799)
    assert pol.pending == []
    pol.observe(_st(), T0 + 10800)
    assert _keys(pol) == [ov.EVERY]
    pol.begin(T0 + 10801)
    pol.observe(_st(), T0 + 20000)
    assert pol.pending == []


def test_nothing_is_requested_while_disabled(cfg):
    cfg.overseer_enabled = False
    pol = ov.Policy(cfg)
    pol.observe(_st(), T0)
    pol.observe(_st({"B": "fail"}, integ_blocked="B", integ_blocked_kind="dirty"), T0 + 1)
    assert pol.pending == [] and not pol.due(T0 + 2)
    assert pol.next_deadline(T0) is None


def test_the_memory_survives_a_restart(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st({"A": "ok"}), T0)
    pol.observe(_st({"A": "ok", "B": "fail"}), T0 + 1)
    again = ov.Policy(cfg)
    assert _keys(again) == ["fail:B"]
    assert again.mem.seen_done == {"A": "ok", "B": "fail"}
    again.observe(_st({"A": "ok", "B": "fail"}), T0 + 2)
    assert len(again.pending) == 1  # the restart did not re-fire it


def test_a_failed_start_puts_its_reasons_back_as_ordinary(cfg):
    pol = ov.Policy(cfg)
    pol.request("hold:P", "held", urgent=True, now=T0)
    taken = pol.begin(T0)
    pol.requeue(taken)
    assert _keys(pol) == ["hold:P"] and not pol.pending[0].urgent
    assert not pol.due(T0 + 1)  # waits out the gap instead of spinning


def test_next_deadline_is_only_ever_in_the_future(cfg):
    pol = ov.Policy(cfg)
    pol.observe(_st(), T0, starving=True)
    assert pol.next_deadline(T0) == T0 + 600  # the starvation threshold
    pol.observe(_st(), T0 + 1, starving=False)
    pol.begin(T0 + 1)
    pol.request("fail:X", "x", now=T0 + 2)
    assert pol.next_deadline(T0 + 2) == T0 + 1 + 600  # the gap
    assert pol.next_deadline(T0 + 99_999) is None


def test_every_trigger_is_logged_with_its_reason(cfg, tmp_path):
    from swarm_orchestrator.logutil import Log

    log = Log(tmp_path / "sup.log")
    try:
        pol = ov.Policy(cfg, log)
        pol.request("hold:P9", "integration held on P9", urgent=True, now=T0)
    finally:
        log.close()
    text = (tmp_path / "sup.log").read_text()
    assert "OVERSEER-TRIGGER hold:P9 urgent — integration held on P9" in text


# -- the starvation map ------------------------------------------------------
GRAPH = {
    "A": set(),
    "B": {"A"},          # failed
    "C": {"B"},
    "D": {"C"},
    "E": set(),          # excluded
    "F": {"E"},
    "G": {"F", "B"},     # behind both the excluded and the failed root
    "H": set(),          # ready, not launched
    "I": {"H"},
    "J": set(),          # building
    "K": {"J"},
    "L": {"M"},          # M is not in the ledger
    "P": set(),          # parked on the owner
    "Q": {"P"},
    "X": {"Y"},          # a cycle
    "Y": {"X"},
    "E2": {"B"},         # excluded, and itself behind the failed B
    "Z": {"E2"},         # counts under E2 only: the walk stops at a root
}
DONE = {"A": "ok", "B": "fail"}
EXCLUDED = {"E", "E2"}
IN_FLIGHT = {"J": "building", "P": "parked"}


def _by_root(m: dict) -> dict[str, tuple[str, int]]:
    return {b["phase"]: (b["kind"], b["blocks"]) for b in m["blockers"]}


def test_the_map_names_each_root_and_how_much_stands_behind_it():
    m = ov.starvation_map(GRAPH, DONE, EXCLUDED, IN_FLIGHT)
    assert _by_root(m) == {
        "B": ("failed", 3),       # C, D, G (E2 is a root of its own)
        "E": ("excluded", 2),     # F, G
        "E2": ("excluded", 1),    # Z
        "H": ("ready", 1),        # I
        "J": ("building", 1),     # K
        "M": ("unknown", 1),      # L
        "P": ("parked", 1),       # Q
    }
    assert m["blockers"][0]["phase"] == "B"
    assert m["blockers"][0]["examples"] == ["C", "D", "G"]  # ledger order


def test_the_map_counts_the_backlog_and_reports_a_cycle():
    m = ov.starvation_map(GRAPH, DONE, EXCLUDED, IN_FLIGHT)
    assert m["ready"] == ["H"]
    assert m["cycle"] == ["X", "Y"]
    # open, not excluded, not attempted, not in flight
    assert m["backlog"] == len(["C", "D", "F", "G", "H", "I", "K", "L", "Q", "X", "Y", "Z"])
    assert m["blocked"] == m["backlog"] - 1


def test_a_landed_dependency_blocks_nothing():
    m = ov.starvation_map({"A": set(), "B": {"A"}}, {"A": "skip"}, set(), {})
    assert m["blockers"] == []  # a root nothing waits on is not worth a line
    assert m["ready"] == ["B"] and m["blocked"] == 0


def test_a_long_serial_chain_does_not_hit_the_recursion_limit():
    n = 5000
    graph = {"c0": set(), **{f"c{i}": {f"c{i - 1}"} for i in range(1, n)}}
    m = ov.starvation_map(graph, {}, set(), {"c0": "building"})
    assert _by_root(m) == {"c0": ("building", n - 1)}


def test_backlog_is_what_the_swarm_could_still_start():
    assert ov.backlog(GRAPH, DONE, EXCLUDED, IN_FLIGHT) == [
        p for p in GRAPH if p not in ("A", "B", "E", "E2", "J", "P")
    ]
