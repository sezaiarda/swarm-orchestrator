"""``swarm why <phase>`` — one answer per phase, and the root cause behind it.

``swarm context`` reports ``ready: []`` with no explanation, which is how a run
that is actually stuck looks identical to one that has simply finished. These
tests pin the classification order (the order a human asks the questions in) and,
for a blocked phase, the walk to the **root cause** — the deepest unmet ancestor
that has nothing unmet itself.

The dependency semantics matter more than the prose: a dependency is satisfied
only when its status is in :data:`ledger.SATISFIES_DEPS`. A ``fail`` had its
branch discarded, so a dependent built on top of it would be built against a main
that provably lacks it — and a whole subtree stalling on one ``fail`` is exactly
the case ``swarm why`` exists to name.
"""

from __future__ import annotations

from pathlib import Path

from swarm_orchestrator import state as state_mod
from swarm_orchestrator import why as why_mod
from swarm_orchestrator.config import load

CHAIN = (
    "- [ ] `P0` · needs:—\n"
    "- [ ] `P1` · needs:`P0`\n"
    "- [ ] `P2` · needs:`P1`\n"
    "- [ ] `P3` · needs:`P1` `P2`\n"
    "- [ ] `solo` · needs:—\n"
)


def _cfg(tmp_path: Path, monkeypatch, ledger: str = CHAIN, toml: str = ""):
    """A bare-driver Config over a throwaway project with the given ledger."""
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True, exist_ok=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text(ledger, encoding="utf-8")
    if toml:
        (project / ".swarm.toml").write_text(toml, encoding="utf-8")
    return load(project_dir=str(project))


def _state(cfg, slots=2, **kw):
    """Reset the run to a clean ``slots``-wide state with the given overrides."""
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(state_mod.State.fresh(slots).__dict__)
        for key, val in kw.items():
            setattr(st, key, val)
        return st


# -- classification --------------------------------------------------------
def test_unknown_phase_names_the_ledger_it_looked_in(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg)

    exp = why_mod.explain(cfg, "P9")

    assert exp.reason == why_mod.UNKNOWN
    assert "PHASE-LEDGER.md" in exp.detail and "5 phases" in exp.detail


def test_excluded_phase_quotes_the_config_comment(tmp_path, monkeypatch):
    # `exclude = ["P0"]` on its own says nothing; the reason is written beside it
    # as a comment, and that comment IS the answer to "why isn't P0 running".
    cfg = _cfg(
        tmp_path, monkeypatch,
        toml='[tasks]\nexclude = [\n  "P0",  # owner is doing this one by hand\n]\n',
    )
    _state(cfg)

    exp = why_mod.explain(cfg, "P0")

    assert exp.reason == why_mod.EXCLUDED
    assert "owner is doing this one by hand" in exp.detail


def test_excluded_falls_back_to_the_comment_block_above(tmp_path, monkeypatch):
    cfg = _cfg(
        tmp_path, monkeypatch,
        toml='[tasks]\n# waiting on the upstream release\nexclude = ["P0"]\n',
    )
    _state(cfg)
    assert "waiting on the upstream release" in why_mod.explain(cfg, "P0").detail


def test_the_comment_block_is_narrowed_to_the_phase_it_names(tmp_path, monkeypatch):
    # A real block covers every excluded phase at once (a multi-phase entry names three);
    # handing all of it back as the answer for one phase buries that phase's line.
    cfg = _cfg(
        tmp_path, monkeypatch,
        toml=(
            "[tasks]\n"
            "# Phases the swarm must never launch:\n"
            "#   P0 — owner-run: it needs the live stack and a long session.\n"
            "#   P1 — standing additive-only policy, never a scheduled phase.\n"
            'exclude = ["P0", "P1"]\n'
        ),
    )
    _state(cfg)

    detail = why_mod.explain(cfg, "P0").detail

    assert "owner-run: it needs the live stack" in detail
    assert "additive-only" not in detail  # P1's line stays with P1


def test_a_wrapped_comment_entry_keeps_its_continuation_lines(tmp_path, monkeypatch):
    # A ledger wraps each entry over several `#` lines; stopping at the first one
    # cuts the reason off mid-sentence, which is worse than saying nothing.
    cfg = _cfg(
        tmp_path, monkeypatch,
        toml=(
            "[tasks]\n"
            "#   P0 — owner-run: it needs the live stack\n"
            "#        and a real day to prove out.\n"
            "#   P1 — standing additive-only policy.\n"
            'exclude = ["P0", "P1"]\n'
        ),
    )
    _state(cfg)

    detail = why_mod.explain(cfg, "P0").detail

    assert "and a real day to prove out." in detail
    assert "additive-only" not in detail


def test_a_cross_reference_to_another_excluded_phase_does_not_truncate(
    tmp_path, monkeypatch
):
    # A ledger entry ends "exactly like <another phase>", which is itself
    # excluded. Matching a bare mention would cut the reason off mid-sentence, so
    # entry boundaries are anchored to the START of a comment line.
    cfg = _cfg(
        tmp_path, monkeypatch,
        toml=(
            "[tasks]\n"
            "#   P0 — owner-run, exactly like P1.\n"
            "#        Added so no master launches it.\n"
            "#   P1 — standing additive-only policy.\n"
            'exclude = ["P0", "P1"]\n'
        ),
    )
    _state(cfg)

    detail = why_mod.explain(cfg, "P0").detail

    assert "exactly like P1" in detail
    assert "Added so no master launches it." in detail
    assert "additive-only" not in detail


def test_a_very_long_comment_is_clipped(tmp_path, monkeypatch):
    cfg = _cfg(
        tmp_path, monkeypatch,
        toml=f'[tasks]\n# {"blah " * 200}\nexclude = ["P0"]\n',
    )
    _state(cfg)

    assert len(why_mod.explain(cfg, "P0").detail) < 320


def test_excluded_with_no_comment_still_answers(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, toml='[tasks]\nexclude = ["P0"]\n')
    _state(cfg)
    exp = why_mod.explain(cfg, "P0")
    assert exp.reason == why_mod.EXCLUDED and "says:" not in exp.detail


def test_busy_phase_is_not_stuck(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, slots=2)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")

    exp = why_mod.explain(cfg, "P0")

    assert exp.reason == why_mod.BUSY and "slot 0" in exp.detail


def test_parked_and_waiting_point_at_the_owner(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, parked=["P0"], waiting={"solo": 9e9})

    assert why_mod.explain(cfg, "P0").reason == why_mod.PARKED
    waiting = why_mod.explain(cfg, "solo")
    assert waiting.reason == why_mod.WAITING and "waiting on YOU" in waiting.detail


def test_merge_queue_states_are_distinguished(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, integ_queue=["P0", "P1"], integ_blocked="P0",
           integ_blocked_kind="conflict", integ_blocked_repo="/repos/pricing")

    blocked = why_mod.explain(cfg, "P0")
    queued = why_mod.explain(cfg, "P1")

    assert blocked.reason == why_mod.INTEG_BLOCKED
    assert "conflict" in blocked.detail and "pricing" in blocked.detail
    assert queued.reason == why_mod.INTEGRATING and "1 ahead of it" in queued.detail


def test_done_ok_versus_done_fail(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, done={"P0": "ok", "solo": "fail"})

    ok = why_mod.explain(cfg, "P0")
    failed = why_mod.explain(cfg, "solo")

    assert ok.reason == why_mod.DONE and ok.status == "ok"
    assert failed.reason == why_mod.DONE and failed.status == "fail"
    assert "not re-offered" in failed.detail  # it will never come back on its own


def test_ready_reports_pause_and_slot_pressure(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)

    _state(cfg, slots=2)
    assert "ready NOW" in why_mod.explain(cfg, "P0").detail

    _state(cfg, slots=2, paused=True)
    assert "PAUSED" in why_mod.explain(cfg, "P0").detail

    _state(cfg, slots=1)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("solo")
    assert "all 1 slots are busy" in why_mod.explain(cfg, "P0").detail


# -- the root cause --------------------------------------------------------
def test_a_fail_at_the_root_stalls_the_whole_subtree(tmp_path, monkeypatch):
    # The case this exists to name: `fail` is in the done map, so a resolver
    # that tests bare membership calls P1 ready and builds it on a main that
    # never got P0.
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, done={"P0": "fail"})

    exp = why_mod.explain(cfg, "P3")

    assert exp.reason == why_mod.BLOCKED
    assert exp.root_cause == "P0"
    assert exp.roots == ["P0"]
    assert "`fail`" in exp.root_detail
    text = why_mod.render(exp, show_tree=True)
    assert "root cause: P0" in text
    assert "everything below waits on one phase." in text


def test_skip_and_needs_owner_do_satisfy_a_dependency(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, done={"P0": "skip", "P1": "needs-owner"})

    assert why_mod.explain(cfg, "P2").reason == why_mod.READY


def test_root_cause_is_the_deepest_unmet_ancestor(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, slots=1)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")  # P0 is building; P1/P2/P3 all wait on it

    exp = why_mod.explain(cfg, "P3")

    assert exp.root_cause == "P0"
    assert exp.unmet == ["P1", "P2"]  # its DIRECT unmet deps, not the root
    assert "slot 0" in exp.root_detail


def test_multiple_independent_roots_are_all_reported(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, "- [ ] `A` · needs:—\n"
                                      "- [ ] `B` · needs:—\n"
                                      "- [ ] `C` · needs:`A` `B`\n")
    _state(cfg)

    exp = why_mod.explain(cfg, "C")

    assert exp.roots == ["A", "B"]
    assert exp.root_cause is None  # there is no single phase to point at
    assert "root causes: A, B" in why_mod.render(exp)


def test_a_missing_dependency_surfaces_as_its_own_root(tmp_path, monkeypatch):
    # A typo'd `needs:` is a dep no phase will ever satisfy. It has no deps of its
    # own, so the walk stops there -- which is precisely the diagnosis.
    cfg = _cfg(tmp_path, monkeypatch, "- [ ] `A` · needs:`ghost`\n")
    _state(cfg)

    graph = {"A": {"ghost"}}
    assert why_mod._roots(graph, {}, "A", {"A"}) == ["ghost"]


def test_a_dependency_cycle_is_named_not_mistaken_for_a_root(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, "A needs:B\nB needs:A\n")
    _state(cfg)

    exp = why_mod.explain(cfg, "A")

    assert exp.roots == []  # nothing in a cycle is actionable
    assert "dependency cycle" in exp.detail
    assert any("dependency cycle" in i for i in exp.issues)


def test_the_tree_expands_only_what_is_still_owed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, done={"P0": "ok"})

    exp = why_mod.explain(cfg, "P3")
    labels = {n.phase: n.state for n in exp.tree.children}

    assert labels["P1"] == why_mod.READY  # P0 landed, so P1 can start
    assert labels["P2"] == why_mod.BLOCKED  # still owed: expanded
    # P0 already landed: one satisfied leaf, not another level of history.
    p0 = next(n for n in exp.tree.children[0].children if n.phase == "P0")
    assert p0.state == why_mod._SATISFIED and p0.children == []


def test_a_done_node_is_labelled_with_its_status(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, done={"P0": "fail"})

    exp = why_mod.explain(cfg, "P1")
    assert exp.tree.children[0].state == "done:fail"


def test_explanation_is_json_serialisable(tmp_path, monkeypatch):
    import json

    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, done={"P0": "fail"})

    payload = json.loads(json.dumps(why_mod.explain(cfg, "P3").to_dict()))

    assert payload["reason"] == why_mod.BLOCKED
    assert payload["root_cause"] == "P0"
    assert payload["tree"]["phase"] == "P3"


def test_render_without_tree_stays_one_short_answer(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg)

    assert why_mod.render(why_mod.explain(cfg, "solo")) == (
        "solo: ready NOW — nothing is blocking it (`swarm launch solo`)"
    )
