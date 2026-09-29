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


def test_dirs_reads_each_rows_repos_as_written():
    text = ("- [ ] `a-W1` · dir:`frontend` · needs:—\n"
            "- [ ] `a-W2` · dir:`orders+frontend` · needs:`a-W1`\n"
            "- [ ] `a-W3` · dir:`contract` `webhooks` · needs:—\n"
            "- [ ] `a-W4` · needs:—\n")
    assert ledger.dirs(text) == {"a-W1": ["frontend"], "a-W2": ["orders", "frontend"],
                                 "a-W3": ["contract", "webhooks"]}


# -- lanes: the ledger's touches, the context, the backstop ------
LANES_MD = (
    "- [ ] `ui-W1` · dir:`frontend` · needs:— · touches:`frontend/src/a.ts` · **a**\n"
    "- [ ] `ui-W2` · dir:`frontend` · needs:— · touches:`frontend/src/b.ts` · **b**\n"
    "- [ ] `ui-W3` · dir:`frontend` · needs:— · touches:`frontend/src/**` · **broad**\n"
    "- [ ] `ops-W1` · dir:`.` · needs:— · touches:`./ci/x.sh` `@live-box` · **op**\n"
    "- [ ] `ui-W4` · dir:`frontend` · needs:— · **legacy**\n"
    "- [ ] `payments-P9` · needs:— · **no dir**\n"
    "- [ ] `bad-W1` · dir:`frontend` · needs:— · touches:`frontend/../x` · **bad**\n"
    "- [ ] `bad-W2` · dir:`nowhere` · needs:— · touches:`nowhere/x` · **unknown**\n"
)


def _lanes_cfg(tmp_path, monkeypatch, text=LANES_MD, *, enabled=True, extra=""):
    (tmp_path / "frontend" / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".swarm.toml").write_text(
        f'[lanes]\nresources = ["live-box"]\n{extra}', encoding="utf-8")
    if enabled:
        monkeypatch.setenv("SWARM_LANES", "1")
    else:
        monkeypatch.delenv("SWARM_LANES", raising=False)
    return _bare_cfg(tmp_path, monkeypatch, text)


def test_ledger_lanes_reads_touches_legacy_and_refusals():
    from swarm_orchestrator.lanes import Touch

    issues: list[str] = []
    got = ledger.lanes(LANES_MD, {"frontend", ".", "@live-box"}, issues)
    assert got["ui-W1"] == {Touch("frontend", ("src", "a.ts"))}
    assert got["ops-W1"] == {Touch(".", ("ci", "x.sh")), Touch("@live-box")}
    # no touches: the row owns its dir: repo; no dir: either, its id prefix repo.
    assert got["ui-W4"] == {Touch("frontend", ("**",))}
    assert got["payments-P9"] == {Touch("payments", ("**",))}
    # a bad touch leaves the row lane-less and says so; it never crashes.
    assert "bad-W1" not in got and "bad-W2" not in got
    assert [i.split(":")[0] for i in issues] == ["bad-W1", "bad-W2"]
    # a bare-format ledger has no fields: every row owns its home repo.
    assert ledger.lanes("P0\nP1 needs:P0\n", {"."}) == {
        "P0": {Touch("P0", ("**",))}, "P1": {Touch("P1", ("**",))}}


def test_context_with_lanes_off_is_todays_plus_a_disabled_block(tmp_path, monkeypatch):
    from swarm_orchestrator import master
    from swarm_orchestrator import state as state_mod

    cfg = _lanes_cfg(tmp_path, monkeypatch, enabled=False)
    state_mod.init_state(cfg)
    ctx = master.build_context(cfg, state_mod.read(cfg))
    graph = ledger.parse(LANES_MD)
    today = ledger.ready(graph, {}, set(), set())
    assert ctx["ready"] == today
    assert ctx["launchable"] == today[: len(ctx["free_slots"])]
    assert ctx["lanes"] == {"enabled": False}
    assert ctx["ledger_issues"] == ledger.validate(graph)  # no lane issues when off


def test_context_with_lanes_on_runs_disjoint_rows_and_says_who_waits(tmp_path, monkeypatch):
    from swarm_orchestrator import master
    from swarm_orchestrator import state as state_mod

    cfg = _lanes_cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    ctx = master.build_context(cfg, state_mod.read(cfg))
    lanes = ctx["lanes"]
    # two disjoint frontend rows and the umbrella row go; frontend is then at per_repo=2.
    assert lanes["picked"] == ["ui-W1", "ui-W2", "ops-W1", "payments-P9"]
    assert ctx["launchable"] == lanes["picked"][: len(ctx["free_slots"])]
    assert lanes["waits"]["ui-W3"] == {"holder": "ui-W1", "touch": "frontend/src/a.ts",
                                       "why": "running"}
    assert lanes["waits"]["ui-W4"]["why"] in ("running", "reserved")
    assert "bad-W1" not in lanes["picked"] and "bad-W1" not in lanes["waits"]
    assert any(i.startswith("bad-W1: bad touches") for i in ctx["ledger_issues"])
    assert lanes["held"] == {}


def test_waiting_parked_and_merging_phases_hold_their_lanes(tmp_path, monkeypatch):
    from swarm_orchestrator import master
    from swarm_orchestrator import state as state_mod

    for where in ("waiting", "parked", "integrating"):
        cfg = _lanes_cfg(tmp_path / where, monkeypatch)
        state_mod.init_state(cfg)
        with state_mod.transaction(cfg) as st:
            if where == "waiting":
                st.waiting["ui-W3"] = 9e9
            elif where == "parked":
                st.parked.append("ui-W3")
            else:
                st.integ_push("ui-W3", "ok")  # no snapshot: read from the ledger
        ctx = master.build_context(cfg, state_mod.read(cfg))
        assert ctx["lanes"]["held"] == {"ui-W3": ["frontend/src/**"]}, where
        assert ctx["lanes"]["waits"]["ui-W1"]["holder"] == "ui-W3", where
        assert "ui-W1" not in ctx["lanes"]["picked"], where


def test_a_snapshot_wins_over_the_ledger_for_a_phase_in_flight(tmp_path, monkeypatch):
    from swarm_orchestrator import master
    from swarm_orchestrator import state as state_mod

    cfg = _lanes_cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.parked.append("ui-W1")
        st.lanes["ui-W1"] = ["frontend/src/a.ts", "frontend/docs/**"]  # widened at runtime
    ctx = master.build_context(cfg, state_mod.read(cfg))
    assert ctx["lanes"]["held"]["ui-W1"] == ["frontend/docs/**", "frontend/src/a.ts"]


def test_lane_backstop_denies_an_overlapping_launch_raced_past_the_scheduler(
        tmp_path, monkeypatch):
    import json

    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator import state as state_mod
    from swarm_orchestrator.logutil import Log

    cfg = _lanes_cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        assert launch_mod.launch_outcome(cfg, "ui-W3", log, quiet=True) == launch_mod.LAUNCHED
        assert state_mod.read(cfg).lanes == {"ui-W3": ["frontend/src/**"]}
        # ui-W1 overlaps the running broad row: refused, and its claim undone.
        assert launch_mod.launch_outcome(cfg, "ui-W1", log, quiet=True) == launch_mod.DENIED
        st = state_mod.read(cfg)
        assert [s.phase for s in st.busy_slots()] == ["ui-W3"]
        assert "ui-W1" not in st.lanes
        # a disjoint row still launches beside it; a lane-less row never does.
        assert launch_mod.launch_outcome(cfg, "ops-W1", log, quiet=True) == launch_mod.LAUNCHED
        assert launch_mod.launch_outcome(cfg, "bad-W1", log, quiet=True) == launch_mod.DENIED
    finally:
        log.close()
    text = cfg.supervisor_log.read_text(encoding="utf-8")
    assert "LAUNCH-DENIED ui-W1 lane-busy [ui-W3 frontend/src/**]" in text
    assert "LAUNCH-DENIED bad-W1 lane-invalid" in text
    on_disk = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert on_disk["lanes"] == {"ui-W3": ["frontend/src/**"], "ops-W1": ["./ci/x.sh", "@live-box"]}


def test_the_lane_snapshot_is_released_on_merge_discard_skip_and_free(tmp_path, monkeypatch):
    from swarm_orchestrator import cli
    from swarm_orchestrator import state as state_mod

    cfg = _lanes_cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.lanes = {p: ["frontend/src/a.ts"] for p in ("merged", "discarded", "skipped", "freed")}
        st.integ_push("merged", "ok")
        assert st.integ_pop("merged")  # the integrator's MERGED
        st.mark_done("discarded", "fail")  # a failed build's discard records its status
    assert cli.cmd_skip(cfg, "skipped") == 0
    with state_mod.transaction(cfg) as st:
        st.clear_phase("freed")  # `swarm free`, `swarm retry`
    assert state_mod.read(cfg).lanes == {}
    assert "lanes" not in cfg.state_path.read_text(encoding="utf-8")


def test_an_old_state_file_loads_and_a_run_with_lanes_off_writes_no_lanes(
        tmp_path, monkeypatch):
    import json

    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator import state as state_mod
    from swarm_orchestrator.logutil import Log

    cfg = _lanes_cfg(tmp_path, monkeypatch, enabled=False)
    state_mod.init_state(cfg)
    old = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert "lanes" not in old
    cfg.state_path.write_text(json.dumps(old), encoding="utf-8")
    assert state_mod.read(cfg).lanes == {}
    log = Log(cfg.supervisor_log)
    try:
        assert launch_mod.launch(cfg, "ui-W1", log) is True
        assert launch_mod.launch(cfg, "ui-W3", log) is True  # lanes off: no lane gate
    finally:
        log.close()
    assert "lanes" not in json.loads(cfg.state_path.read_text(encoding="utf-8"))


def test_a_worker_is_told_its_lane(tmp_path, monkeypatch):
    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator import state as state_mod

    cfg = _lanes_cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.lanes["ops-W1"] = ["./ci/x.sh", "@live-box"]
    assert launch_mod._worker_env(cfg, "ops-W1")["SWARM_TOUCHES"] == "./ci/x.sh @live-box"
    off = _lanes_cfg(tmp_path / "off", monkeypatch, enabled=False)
    assert "SWARM_TOUCHES" not in launch_mod._worker_env(off, "ops-W1")


WHY_MD = (
    "- [ ] `w-1` · dir:`frontend` · needs:— · touches:`frontend/src/a.ts` · **held**\n"
    "- [ ] `w-2` · dir:`frontend` · needs:— · touches:`frontend/src/**` · **broad**\n"
    "- [ ] `w-3` · dir:`frontend` · needs:— · touches:`frontend/src/c.ts` · **behind broad**\n"
    "- [ ] `w-4` · dir:`frontend` · needs:— · touches:`frontend/docs/x.md` · **disjoint**\n"
    "- [ ] `w-5` · dir:`frontend` · needs:— · touches:`frontend/tests/y.ts` · **third**\n"
    "- [ ] `w-6` · dir:`frontend` · needs:— · touches:`frontend/../y` · **bad**\n"
)


def test_why_names_the_lane_holder(tmp_path, monkeypatch):
    from swarm_orchestrator import state as state_mod
    from swarm_orchestrator import why

    cfg = _lanes_cfg(tmp_path, monkeypatch, WHY_MD)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.waiting["w-1"] = 9e9
        st.lanes["w-1"] = ["frontend/src/a.ts"]

    def detail(phase):
        exp = why.explain(cfg, phase)
        assert exp.reason == why.READY, phase
        return exp.detail

    assert "waits for its lane: `frontend/src/a.ts` is held by `w-1` (waiting on the owner)" \
        in detail("w-2")
    assert "waits for its lane: `frontend/src/**` is held by `w-2` (reserved)" in detail("w-3")
    assert "nothing is blocking it" in detail("w-4")
    assert "waits for its repo: `frontend` already has 2 phases in flight" in detail("w-5")
    assert "never launches" in why.explain(cfg, "w-6").detail
    with state_mod.transaction(cfg) as st:
        st.waiting.pop("w-1")
        st.integ_push("w-1", "ok")
    assert "is held by `w-1` (merging)" in detail("w-2")


def test_the_digest_counts_lane_waits_under_their_holder():
    from swarm_orchestrator.ovdigest import lane_blockers

    blockers = [{"phase": "ui-W3", "kind": "ready", "blocks": 2, "examples": ["x", "y"]}]
    waits = {"ui-W3": {"holder": "ui-W1", "touch": "frontend/src/a.ts", "why": "running"},
             "ui-W2": {"holder": "ui-W3", "touch": "frontend/src/**", "why": "reserved"}}
    got = lane_blockers(blockers, waits)
    assert got[0] == {"phase": "ui-W1", "kind": "lane", "blocks": 3, "examples": ["ui-W3"]}
    assert {"phase": "ui-W3", "kind": "lane", "blocks": 1, "examples": ["ui-W2"]} in got
    assert lane_blockers(blockers, {}) == blockers
