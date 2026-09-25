"""Markdown-ledger dependency capture + the launch-time dependency backstop.

The markdown parser must capture each phase's ``needs:`` deps (scoped to that one
field), filter them to known phase ids (dropping git tags / the ``needs:—`` root
/ prose words) while keeping cross-repo phase deps, and ``ready`` must then gate
on them. The backstop refuses a ``swarm launch`` whose deps aren't all in
``st.done`` yet. These tests would all FAIL on the pre-fix code, where markdown
deps were dropped and every phase parsed as deps-free (hence trivially ready).
"""

from __future__ import annotations

from swarm_orchestrator import ledger

# A typical markdown ledger: `·`-separated fields, `needs:` deps in
# back-ticks, git-tag / dir: / TAG: back-ticks that must NOT be read as deps.
MYPROJECT_MD = "\n".join(
    [
        "# Myproject — Phase Ledger",
        "> legend prose that must never become a phase",
        "## Family Phase 0",
        "- [x] `contract-P0` · dir:`contract` · needs:— · wire types · **TAG:`bundle-v0.1.0`**",
        "- [x] `inventory-P0` · needs:`nothing` · skeleton",
        "- [x] `inventory-P2` · needs:`inventory-P0` · markov (lazy-on-hover)",
        "- [x] `inventory-P3` · needs:`inventory-P2` · co-occurrence batch",
        "- [ ] `inventory-P4` · needs:`inventory-P3` · lifecycle, governance & observability hardening",
        "- [x] `payments-P4` · needs:— · transient cache + head-only prewarm",
        "- [x] `billing-P4` · dir:`billing` · needs:— · pin lifecycle",
        "- [x] `frontend-P2` · needs:— · detail view + click to open",
        "- [ ] `I4` · needs:`inventory-P0` `payments-P4` `billing-P4` `frontend-P2` `bundle-v0.3.0` `bundle-v1.0.0` · prewarm + batch",
    ]
) + "\n"


def test_markdown_captures_needs_and_filters_to_known_phases():
    graph = ledger.parse(MYPROJECT_MD)
    # only checklist items are phases; prose/legend/headings never leak in.
    assert set(graph) == {
        "contract-P0", "inventory-P0", "inventory-P2", "inventory-P3", "inventory-P4",
        "payments-P4", "billing-P4", "frontend-P2", "I4",
    }
    # the confirmed-bug line: needs:`inventory-P3` must be captured, not dropped.
    assert graph["inventory-P4"] == {"inventory-P3"}
    assert graph["inventory-P3"] == {"inventory-P2"}
    # a multi-dep line keeps every *phase* dep and drops the git tags.
    assert graph["I4"] == {"inventory-P0", "payments-P4", "billing-P4", "frontend-P2"}
    assert "bundle-v0.3.0" not in graph["I4"] and "bundle-v1.0.0" not in graph["I4"]
    # em-dash root, the `nothing` word, and the dir:/TAG: back-ticks all drop.
    assert graph["contract-P0"] == set()  # needs:— · TAG:`bundle-v0.1.0` not a dep
    assert "bundle-v0.1.0" not in graph["contract-P0"]  # sibling TAG: field
    assert "contract" not in graph["contract-P0"]  # sibling dir: field
    assert graph["inventory-P0"] == set()  # `nothing` is not a known phase
    # deps all resolve to known phases -> no phantom unknown/cycle warnings.
    assert ledger.validate(graph) == []


def test_ready_gates_on_captured_markdown_deps():
    graph = ledger.parse(
        "- [x] `inventory-P2` · needs:— · base\n"
        "- [ ] `inventory-P3` · needs:`inventory-P2` · mid\n"
        "- [ ] `inventory-P4` · needs:`inventory-P3` · leaf\n"
    )
    # nothing done -> only the root is ready; the gated phases are excluded.
    assert ledger.ready(graph, {}, set(), set()) == ["inventory-P2"]
    assert "inventory-P3" not in ledger.ready(graph, {}, set(), set())
    assert "inventory-P4" not in ledger.ready(graph, {}, set(), set())
    # dep done -> the dependent becomes ready.
    assert ledger.ready(graph, {"inventory-P2": "ok"}, set(), set()) == ["inventory-P3"]
    assert "inventory-P4" in ledger.ready(
        graph, {"inventory-P2": "ok", "inventory-P3": "ok"}, set(), set()
    )


# -- launch-time dependency backstop (in-process bare driver) -------------
def _bare_cfg(tmp_path, monkeypatch, ledger_text: str):
    """A bare-driver Config over a throwaway project with the given ledger."""
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_WORKER_CMD", "true")  # trivial worker, never claude
    monkeypatch.setenv("SWARM_WORKER_SETTINGS", "")  # no --settings appended
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)  # isolation=none
    from swarm_orchestrator.config import load

    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(ledger_text, encoding="utf-8")
    return load(project_dir=str(tmp_path))


def test_launch_backstop_denies_then_allows(tmp_path, monkeypatch):
    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator import state as state_mod
    from swarm_orchestrator.logutil import Log

    cfg = _bare_cfg(
        tmp_path,
        monkeypatch,
        "- [x] `P0` · needs:—\n"
        "- [ ] `P1` · needs:`P0`\n"
        "- [ ] `P2` · needs:`P0`\n"
        "- [ ] `P4` · needs:`P1` `P2`\n",
    )
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        # P4's deps (P1, P2) are not in st.done -> denied, and the just-claimed
        # slot is undone (never stranded).
        assert launch_mod.launch(cfg, "P4", log) is False
        assert state_mod.read(cfg).busy_slots() == []

        # once the deps are recorded done, the same launch is allowed.
        with state_mod.transaction(cfg) as st:
            st.done = {"P0": "ok", "P1": "ok", "P2": "ok"}
        assert launch_mod.launch(cfg, "P4", log) is True
        assert [s.phase for s in state_mod.read(cfg).busy_slots()] == ["P4"]
    finally:
        log.close()

    text = cfg.supervisor_log.read_text(encoding="utf-8")
    assert "LAUNCH-DENIED P4 unmet-deps" in text
    assert "P1" in text and "P2" in text


def test_launch_backstop_is_best_effort_for_unknown_phase(tmp_path, monkeypatch):
    """A phase the ledger doesn't declare is never blocked (best-effort gate)."""
    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator import state as state_mod
    from swarm_orchestrator.logutil import Log

    cfg = _bare_cfg(tmp_path, monkeypatch, "- [ ] `P0` · needs:—\n")
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        # Zzz is not in the ledger -> the backstop abstains, launch proceeds.
        assert launch_mod.launch(cfg, "Zzz", log) is True
        assert [s.phase for s in state_mod.read(cfg).busy_slots()] == ["Zzz"]
    finally:
        log.close()


# -- a ledger `[x]` row counts as landed (the web board's reading) --------
TICKED_MD = (
    "- [x] `P0` · needs:—\n"
    "- [X] `P1` · needs:`P0`\n"
    "- [ ] `P2` · needs:`P1`\n"
    "- [ ] `P3` · needs:—\n"
)


def test_ticked_reads_only_checked_rows():
    assert ledger.ticked(TICKED_MD) == {"P0", "P1"}
    assert ledger.ticked("P0\nP1 needs:P0\n") == set()  # the bare format has no boxes


def test_with_ticked_never_masks_a_record():
    view = ledger.with_ticked({"P1": "fail", "P9": "ok"}, {"P0", "P1"})
    assert view == {"P0": "ledger", "P1": "fail", "P9": "ok"}
    # a phase in flight is its worker's to finish: its tick releases nothing yet
    assert "P0" not in ledger.with_ticked({}, {"P0"}, in_flight={"P0"})


def test_context_treats_ticked_rows_as_done(tmp_path, monkeypatch):
    """The launcher's `ready` skips a ticked row and releases its dependents,
    without any done record being written for it."""
    from swarm_orchestrator import master as master_mod
    from swarm_orchestrator import state as state_mod

    cfg = _bare_cfg(tmp_path, monkeypatch, TICKED_MD)
    state_mod.init_state(cfg)
    st = state_mod.read(cfg)
    ctx = master_mod.build_context(cfg, st)
    assert ctx["ready"] == ["P2", "P3"]
    assert ctx["done"] == {}  # a view, never a record
    assert state_mod.read(cfg).done == {}


def test_context_keeps_a_ticked_fail_failed(tmp_path, monkeypatch):
    """A ticked row the swarm recorded `fail` stays failed: not re-offered, and
    its dependents stay blocked until `swarm retry`."""
    from swarm_orchestrator import master as master_mod
    from swarm_orchestrator import state as state_mod

    cfg = _bare_cfg(tmp_path, monkeypatch, TICKED_MD)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.done = {"P1": "fail"}
    ctx = master_mod.build_context(cfg, state_mod.read(cfg))
    assert ctx["ready"] == ["P3"]


def test_launch_backstop_accepts_a_ticked_dep(tmp_path, monkeypatch):
    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator import state as state_mod
    from swarm_orchestrator.logutil import Log

    cfg = _bare_cfg(tmp_path, monkeypatch, TICKED_MD)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        assert launch_mod.launch(cfg, "P2", log) is True
    finally:
        log.close()


def test_why_says_a_ticked_row_is_done(tmp_path, monkeypatch):
    from swarm_orchestrator import state as state_mod
    from swarm_orchestrator import why

    cfg = _bare_cfg(tmp_path, monkeypatch, TICKED_MD)
    state_mod.init_state(cfg)
    exp = why.explain(cfg, "P1")
    assert exp.reason == why.DONE and "ticked" in exp.detail
    assert why.explain(cfg, "P2").reason == why.READY
