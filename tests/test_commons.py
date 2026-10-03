"""``[lanes] commons``: a touch that matches it is never a lane.

Rows name their repo's changelog and status page in ``touches:``, because those
are files they edit. Commons used to be read at landing only, so the scheduler
kept two such rows apart over a file the config calls shared, and ``swarm why``
named that file as what a row waited for. :func:`lanes.owned` decides it now, and
these tests ask each caller that says "these rows overlap" or "this row waits for
that one": the pick, the context, ``swarm why``, doctor's line, the launch
backstop, a reshape of a phase in flight and ``swarm widen``. A row may still
name a commons file, and its snapshot keeps it.
"""

from __future__ import annotations

import pytest
from test_deps import _lanes_cfg

from swarm_orchestrator.lanes import Touch, Wait, collide, legacy, owned, parse_touch, pick

KNOWN = {"frontend", "billing", ".", "@live-box"}
COMMONS = ["./docs/PHASE-LEDGER.md", "./docs/phases/**", "*/CHANGELOG.md", "*/docs/STATUS.md"]
LOG = "frontend/CHANGELOG.md"


def t(text: str) -> Touch:
    return parse_touch(text, KNOWN)


def lane(*texts: str) -> frozenset[Touch]:
    return frozenset(t(x) for x in texts)


# --- the one decision ----------------------------------------------------------


@pytest.mark.parametrize("touch, held", [
    ("frontend/CHANGELOG.md", False),
    ("billing/docs/STATUS.md", False),
    ("./docs/PHASE-LEDGER.md", False),
    ("./docs/phases/ui.md", False),
    ("./docs/phases/ui-*.md", False),   # every file it names is under the glob
    ("./docs/phases/**", False),
    ("frontend/src/a.ts", True),
    ("frontend/README.md", True),       # not listed: still a lane
    ("frontend/**", True),              # the whole repo, the changelog under it or not
    ("frontend/docs/**", True),
    ("frontend/*.md", True),
    ("./docs/**", True),
    ("@live-box", True),
])
def test_a_touch_is_held_unless_it_matches_a_commons_glob(touch, held):
    assert owned(lane(touch), COMMONS) == (lane(touch) if held else frozenset())


def test_a_resource_is_held_whatever_the_globs_say():
    assert owned(lane("@live-box", LOG), ["*"]) == lane("@live-box")


def test_with_no_commons_every_touch_is_held():
    both = lane("frontend/src/a.ts", LOG)
    assert owned(both) == both and owned(both, []) == both


def test_collide_never_pairs_a_commons_touch():
    a, b = lane("frontend/src/a.ts", LOG), lane("frontend/src/b.ts", LOG)
    assert collide(a, b) == (t(LOG), t(LOG))
    assert collide(a, b, COMMONS) is None
    assert collide(a, lane("frontend/**"), COMMONS) == (t("frontend/src/a.ts"), t("frontend/**"))
    assert collide(lane(LOG), lane("frontend/**"), COMMONS) is None


# --- the scheduler -------------------------------------------------------------


def test_rows_that_share_only_a_commons_file_launch_together_up_to_per_repo():
    lanes = {p: lane(f"frontend/src/{p}.ts", LOG, "frontend/docs/STATUS.md") for p in "ABC"}
    launch, waits = pick("ABC", "ABC", {}, lanes, 2, COMMONS)
    assert launch == ["A", "B"]
    assert waits == {"C": Wait("A", Touch("frontend", ("**",)), "per_repo")}
    # the same rows with nothing shared take turns over the changelog
    launch, waits = pick("ABC", "ABC", {}, lanes, 2)
    assert launch == ["A"] and waits["B"] == Wait("A", t(LOG), "held")


def test_a_phase_in_flight_holds_no_commons_file():
    held = {"run": lane("frontend/src/a.ts", LOG)}
    lanes = {"B": lane("frontend/src/b.ts", LOG), "same": lane("frontend/src/a.ts", LOG)}
    launch, waits = pick(lanes, ["B", "same"], held, lanes, 3, COMMONS)
    assert launch == ["B"]
    assert waits == {"same": Wait("run", t("frontend/src/a.ts"), "held")}


def test_a_waiting_row_reserves_no_commons_file():
    held = {"run": lane("frontend/src/**")}
    lanes = {"behind": lane("frontend/src/a.ts", LOG), "docs": lane("frontend/docs/x.md", LOG)}
    launch, waits = pick(lanes, ["behind", "docs"], held, lanes, 3, COMMONS)
    assert launch == ["docs"]
    assert waits == {"behind": Wait("run", t("frontend/src/**"), "held")}


def test_a_row_whose_touches_are_all_commons_launches_and_owns_nothing():
    held = {"run": legacy(["frontend"])}
    lanes = {"log": lane(LOG, "frontend/docs/STATUS.md"), "src": lane("frontend/src/a.ts")}
    launch, waits = pick(lanes, ["log", "src"], held, lanes, 1, COMMONS)
    assert launch == ["log"]  # beside a row that owns the whole repo, at per_repo 1
    assert waits == {"src": Wait("run", Touch("frontend", ("**",)), "held")}
    # in flight it holds nothing and fills no place in its repo
    assert pick(["src"], ["src"], {"log": lanes["log"]}, lanes, 1, COMMONS) == (["src"], {})


# --- every caller, over a real config ------------------------------------------

ROWS_MD = (
    "- [ ] `ui-W1` · dir:`frontend` · needs:— · touches:`frontend/src/a.ts`"
    " `frontend/CHANGELOG.md` `frontend/docs/STATUS.md` · **a**\n"
    "- [ ] `ui-W2` · dir:`frontend` · needs:— · touches:`frontend/src/b.ts`"
    " `frontend/CHANGELOG.md` · **b**\n"
    "- [ ] `ui-W3` · dir:`frontend` · needs:— · touches:`frontend/src/c.ts`"
    " `frontend/CHANGELOG.md` · **c**\n"
    "- [ ] `ui-W4` · dir:`frontend` · needs:— · touches:`frontend/CHANGELOG.md`"
    " · **only the changelog**\n"
    "- [ ] `ui-W5` · dir:`frontend` · needs:— · touches:`frontend/src/a.ts`"
    " `frontend/CHANGELOG.md` · **the same file as a**\n"
)
EXTRA = 'commons = ["*/CHANGELOG.md", "*/docs/STATUS.md"]\n'
SNAPSHOT = ["frontend/CHANGELOG.md", "frontend/docs/STATUS.md", "frontend/src/a.ts"]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from swarm_orchestrator import state as state_mod

    cfg = _lanes_cfg(tmp_path, monkeypatch, ROWS_MD, extra=EXTRA)
    assert cfg.lanes_commons == ["*/CHANGELOG.md", "*/docs/STATUS.md"]
    state_mod.init_state(cfg)
    return cfg


def _in_flight(cfg, phase: str = "ui-W1", snapshot: list[str] = SNAPSHOT) -> None:
    from swarm_orchestrator import state as state_mod

    with state_mod.transaction(cfg) as st:
        st.waiting[phase] = 9e9
        st.lanes[phase] = list(snapshot)


def _ctx(cfg) -> dict:
    from swarm_orchestrator import master
    from swarm_orchestrator import state as state_mod

    return master.build_context(cfg, state_mod.read(cfg))


def test_the_context_picks_rows_that_share_only_commons(cfg):
    lanes = _ctx(cfg)["lanes"]
    assert lanes["picked"] == ["ui-W1", "ui-W2", "ui-W4"]
    assert lanes["waits"] == {
        "ui-W3": {"holder": "ui-W1", "touch": "frontend/**", "why": "per_repo"},
        "ui-W5": {"holder": "ui-W1", "touch": "frontend/src/a.ts", "why": "running"},
    }


def test_a_phase_in_flight_is_shown_holding_only_its_lane(cfg):
    _in_flight(cfg)
    _in_flight(cfg, "ui-W4", ["frontend/CHANGELOG.md"])
    lanes = _ctx(cfg)["lanes"]
    assert lanes["held"] == {"ui-W1": ["frontend/src/a.ts"], "ui-W4": []}
    assert lanes["picked"] == ["ui-W2"]
    assert lanes["waits"]["ui-W3"]["why"] == "per_repo"


def test_why_never_names_a_commons_file_as_the_holder(cfg):
    from swarm_orchestrator import why

    _in_flight(cfg)
    said = {p: why.explain(cfg, p).detail for p in ("ui-W2", "ui-W3", "ui-W4", "ui-W5")}
    assert "nothing is blocking it" in said["ui-W2"]
    assert "waits for its repo: `frontend` already has 2 phases in flight" in said["ui-W3"]
    assert "nothing is blocking it" in said["ui-W4"]
    assert "waits for its lane: `frontend/src/a.ts` is held by `ui-W1`" in said["ui-W5"]
    for detail in said.values():
        assert "CHANGELOG.md" not in detail and "STATUS.md" not in detail


def test_doctor_names_no_commons_file_among_the_lane_waits(cfg):
    from swarm_orchestrator import doctor

    _in_flight(cfg)
    rows, held = doctor._startable(_ctx(cfg))
    assert rows == ["ui-W2", "ui-W4"]
    line = doctor._lane_waits(held)
    assert line.startswith("2 ready row(s) wait on a lane")
    assert "CHANGELOG.md" not in line and "STATUS.md" not in line


def test_the_launch_backstop_lets_them_run_and_records_what_each_row_names(cfg):
    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator import state as state_mod
    from swarm_orchestrator.logutil import Log

    log = Log(cfg.supervisor_log)
    try:
        for phase in ("ui-W1", "ui-W2", "ui-W4"):
            assert launch_mod.launch_outcome(cfg, phase, log, quiet=True) == launch_mod.LAUNCHED
        assert launch_mod.launch_outcome(cfg, "ui-W5", log, quiet=True) == launch_mod.DENIED
    finally:
        log.close()
    assert "LAUNCH-DENIED ui-W5 lane-busy [ui-W1 frontend/src/a.ts]" \
        in cfg.supervisor_log.read_text(encoding="utf-8")
    # the snapshot is the row as written: a worker is told every file it names
    assert state_mod.read(cfg).lanes == {
        "ui-W1": SNAPSHOT,
        "ui-W2": ["frontend/CHANGELOG.md", "frontend/src/b.ts"],
        "ui-W4": ["frontend/CHANGELOG.md"],
    }
    assert launch_mod._worker_env(cfg, "ui-W1")["SWARM_TOUCHES"] == " ".join(SNAPSHOT)
    assert _ctx(cfg)["lanes"]["held"] == {
        "ui-W1": ["frontend/src/a.ts"], "ui-W2": ["frontend/src/b.ts"], "ui-W4": []}


def test_a_follow_up_may_name_only_commons(cfg):
    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator import ledgerw

    ledgerw.check_touches("ui-W9", ["frontend/CHANGELOG.md"], ["frontend"],
                          launch_mod.known_lanes(cfg))


def test_a_reshape_may_drop_a_commons_touch_whose_changes_cannot_be_read(cfg):
    """Isolation ``none``: one shared checkout, so nothing says what the phase
    changed, and a touch it holds may only be kept. A commons touch is not held."""
    from swarm_orchestrator import ledgerw

    _in_flight(cfg)
    with pytest.raises(ledgerw.ReportError, match=r"frontend/src/a\.ts \(its changes there"):
        ledgerw.file_reshape(cfg, "overseer", "ui-W1", "narrow", touches=["frontend/src/z.ts"])
    ledgerw.file_reshape(cfg, "overseer", "ui-W1", "the changelog is shared anyway",
                         touches=["frontend/src/a.ts"])
    (op,) = (o for data in ledgerw.pending(cfg).values() for o in data["ops"])
    assert op["touches"] == ["frontend/src/a.ts"] and op["held"] == SNAPSHOT


def test_widen_names_no_holder_over_a_commons_file(cfg, capsys):
    from swarm_orchestrator import cli
    from swarm_orchestrator import state as state_mod

    _in_flight(cfg)
    _in_flight(cfg, "ui-W2", ["frontend/src/b.ts"])
    assert cli.cmd_widen(cfg, "ui-W2", ["frontend/CHANGELOG.md"]) == 0
    assert capsys.readouterr().out == ""
    assert state_mod.read(cfg).lanes["ui-W2"] == ["frontend/CHANGELOG.md", "frontend/src/b.ts"]
    assert cli.cmd_widen(cfg, "ui-W2", ["frontend/src/a.ts"]) == 0
    assert "ui-W1 holds frontend/src/a.ts" in capsys.readouterr().out
