"""`swarm widen`: a phase in flight adds touches to the lane it holds.

The scheduler reads held lanes from the ``State.lanes`` snapshot, so widening
must grow that snapshot (never shrink it), keep a newly overlapping ready row
waiting, name any phase in flight that already holds an overlapping touch, and
refuse a bad touch or a phase not in flight without recording anything.
"""

from __future__ import annotations

from swarm_orchestrator import master
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import main as cli_main

LANES_MD = (
    "- [ ] `ui-W1` · dir:`frontend` · needs:— · touches:`frontend/src/a.ts` · **a**\n"
    "- [ ] `ui-W2` · dir:`frontend` · needs:— · touches:`frontend/src/b.ts` · **b**\n"
    "- [ ] `ui-W3` · dir:`frontend` · needs:— · touches:`frontend/src/**` · **broad**\n"
    "- [ ] `ops-W1` · dir:`.` · needs:— · touches:`./ci/x.sh` `@live-box` · **op**\n"
)


def _cfg(tmp_path, monkeypatch):
    """A lanes-on, bare-driver Config over a throwaway project with LANES_MD."""
    (tmp_path / "frontend" / ".git").mkdir(parents=True)
    (tmp_path / ".swarm.toml").write_text('[lanes]\nresources = ["live-box"]\n',
                                          encoding="utf-8")
    monkeypatch.setenv("SWARM_LANES", "1")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_WORKER_CMD", "true")
    monkeypatch.setenv("SWARM_WORKER_SETTINGS", "")
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)
    from swarm_orchestrator.config import load

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(LANES_MD, encoding="utf-8")
    cfg = load(project_dir=str(tmp_path))
    state_mod.init_state(cfg)
    return cfg


def _widen(cfg, *args: str) -> int:
    return cli_main(["--project-dir", str(cfg.project_dir), "widen", *args])


def test_widen_extends_the_snapshot_and_never_shrinks_it(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    with state_mod.transaction(cfg) as st:
        st.parked += ["ui-W1", "ops-W1"]
        st.lanes["ops-W1"] = ["./ci/x.sh", "@live-box"]
    # no snapshot yet: it starts from the row's lane, then adds the new touch.
    assert _widen(cfg, "ui-W1", "frontend/docs/**") == 0
    # a snapshot: old + new, sorted, a repeat recorded once.
    assert _widen(cfg, "ops-W1", "./docs/x.md", "./ci/x.sh") == 0
    assert state_mod.read(cfg).lanes == {
        "ui-W1": ["frontend/docs/**", "frontend/src/a.ts"],
        "ops-W1": ["./ci/x.sh", "./docs/x.md", "@live-box"],
    }


def test_the_scheduler_then_keeps_an_overlapping_ready_row_waiting(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    with state_mod.transaction(cfg) as st:
        st.parked.append("ui-W1")
    before = master.build_context(cfg, state_mod.read(cfg))["lanes"]
    assert "ui-W2" in before["picked"]
    assert _widen(cfg, "ui-W1", "frontend/src/b.ts") == 0
    after = master.build_context(cfg, state_mod.read(cfg))["lanes"]
    assert "ui-W2" not in after["picked"]
    assert after["waits"]["ui-W2"]["holder"] == "ui-W1"
    assert after["waits"]["ui-W2"]["touch"] == "frontend/src/b.ts"


def test_it_names_each_phase_in_flight_holding_an_overlapping_touch(
        tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    with state_mod.transaction(cfg) as st:
        st.parked += ["ui-W1", "ui-W3", "ops-W1"]
    assert _widen(cfg, "ui-W1", "frontend/src/b.ts") == 0  # an overlap never refuses
    out = capsys.readouterr().out.splitlines()
    line = ("ui-W3 holds frontend/src/**: your merge will be re-tested against it"
            " and may need a resolver")
    assert out == [line]
    assert state_mod.read(cfg).lanes["ui-W1"] == ["frontend/src/a.ts", "frontend/src/b.ts"]


def test_a_phase_not_in_flight_or_a_bad_touch_exits_2_and_records_nothing(
        tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path, monkeypatch)
    with state_mod.transaction(cfg) as st:
        st.parked.append("ui-W1")
    assert _widen(cfg, "ui-W2", "frontend/src/c.ts") == 2  # a ready row, not in flight
    assert "ui-W2 is not in flight" in capsys.readouterr().err
    # one bad touch among good ones: none of them is recorded.
    assert _widen(cfg, "ui-W1", "frontend/src/c.ts", "frontend/../x") == 2
    assert _widen(cfg, "ui-W1", "nowhere/x") == 2
    err = capsys.readouterr().err
    assert "'..' segments are not allowed" in err and "unknown lane 'nowhere'" in err
    assert state_mod.read(cfg).lanes == {}
