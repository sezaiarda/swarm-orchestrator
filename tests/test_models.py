"""A row's worker model: the ledger field, the launch, the hand-back, the forecast.

A row may name the model its worker runs on. The launcher swaps it into the
worker command and tells that worker how to hand the phase back; ``swarm
escalate`` then ends the session, drops the mirror without merging it and lets
the row start again on the swarm's own model. On real git for the hand-back,
through the supervisor's own handler.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import cli, gitq, ledgerw
from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import ledger as ledger_mod
from swarm_orchestrator import models as models_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.eta import hazards, model, plan as eta_plan, record, sim, tiers
from swarm_orchestrator.eta.holds import Cap, Holds
from swarm_orchestrator.supervisor import Supervisor
from swarm_orchestrator.tui import data as data_mod

LEDGER = """# Ledger

## Campaign A
- [x] `a-W1` · dir:`alpha` · needs:— · **the first wave**
- [ ] `a-W2` · dir:`alpha` · needs:`a-W1` · model:`sonnet` · **the second wave**
- [ ] `a-W3` · dir:`alpha` · needs:`a-W1` · **the third wave** · **TAG:`v1`**
- [ ] `a-W4` · dir:`alpha` · needs:`a-W1` · model:`opus` · **the fourth wave**
"""
LEDGER_FILE = "docs/PHASE-LEDGER.md"
WHY = "the cause is a race in the store's close path that I could not pin down in two tries"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def ws(tmp_path, monkeypatch):
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", "-b", "master", str(project)], check=True)
    for k, v in (("user.email", "swarm@test"), ("user.name", "swarm"), ("commit.gpgsign", "false")):
        _git(project, "config", k, v)
    (project / "docs").mkdir()
    (project / LEDGER_FILE).write_text(LEDGER)
    (project / ".swarm.toml").write_text(
        f'[swarm]\ndriver = "bare"\n[worker]\nworker_cmd = "claude --model opus"\n'
        f'[tasks]\nledger = "{LEDGER_FILE}"\n')
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_SLUG", "models")
    for leak in ("SWARM_SESSION_ID", "SWARM_PHASE", "SWARM_DRIVER", "SWARM_WORKER_CMD",
                 "SWARM_MODEL"):
        monkeypatch.delenv(leak, raising=False)
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)
    monkeypatch.setattr(sup, "_fill_slots", lambda *a, **k: [])
    yield cfg, project, sup
    sup.log.close()


# -- the ledger field -----------------------------------------------------------
def test_a_rows_model_is_read_and_kept_out_of_its_needs_and_prose():
    assert ledger_mod.models(LEDGER) == {"a-W2": "sonnet", "a-W4": "opus"}
    assert ledger_mod.parse(LEDGER)["a-W2"] == {"a-W1"}
    head = ledgerw.split_head(next(ln for ln in LEDGER.splitlines() if "`a-W2`" in ln))
    assert "model:`sonnet`" in head.meta and head.prose == ["**the second wave**"]


def test_setting_a_model_adds_changes_and_drops_the_field_where_the_gates_read_it():
    text, said = ledgerw.set_model(LEDGER, "a-W3", "sonnet")
    line = next(ln for ln in text.splitlines() if "`a-W3`" in ln)
    assert line == ("- [ ] `a-W3` · dir:`alpha` · needs:`a-W1` · model:`sonnet`"
                    " · **the third wave** · **TAG:`v1`**")
    assert said == "model default → sonnet"

    text, said = ledgerw.set_model(text, "a-W3", "opus")
    assert ledger_mod.models(text)["a-W3"] == "opus" and said == "model sonnet → opus"

    text, said = ledgerw.set_model(text, "a-W3", "")
    assert "a-W3" not in ledger_mod.models(text) and said == "model opus → default"
    assert next(ln for ln in text.splitlines() if "`a-W3`" in ln) \
        == next(ln for ln in LEDGER.splitlines() if "`a-W3`" in ln)


@pytest.mark.parametrize("phase, name, word", [
    ("a-W1", "sonnet", "ticked"), ("a-W9", "sonnet", "no ledger row"),
    ("a-W2", "sonnet", "already runs on"), ("a-W3", "son net", "not a model name"),
    ("a-W3", "", "already runs on"),
])
def test_a_model_edit_that_cannot_stand_is_refused(phase, name, word):
    with pytest.raises(ledgerw.ReportError, match=word):
        ledgerw.set_model(LEDGER, phase, name)


# -- the launch -------------------------------------------------------------------
def test_swapping_the_model_keeps_the_rest_of_the_command():
    assert models_mod.swap("claude --model opus", "sonnet") == "claude --model sonnet"
    assert models_mod.swap("claude --model=opus -x", "sonnet") == "claude --model=sonnet -x"
    assert models_mod.swap("claude", "sonnet") == "claude --model sonnet"


def test_a_row_on_its_own_model_launches_on_it_and_is_told_how_to_hand_back(ws):
    cfg, project, _sup = ws
    assert models_mod.default(cfg) == "opus"
    assert models_mod.overrides(cfg) == {"a-W2": "sonnet"}  # a-W4 names the default

    shell = launch_mod._worker_shell(cfg, "a-W2", project)
    assert " claude --model sonnet " in shell and "--model opus" not in shell
    assert "--append-system-prompt" in shell and "swarm escalate a-W2" in shell
    assert launch_mod._worker_env(cfg, "a-W2")[models_mod.ENV] == "sonnet"

    for phase in ("a-W3", "a-W4"):  # no field, and a field naming the default
        shell = launch_mod._worker_shell(cfg, phase, project)
        assert " claude --model opus " in shell and "--append-system-prompt" not in shell
        assert launch_mod._worker_env(cfg, phase)[models_mod.ENV] == ""


# -- the sweep --------------------------------------------------------------------
def test_one_ledger_write_sets_many_rows_and_notes_each_reason(ws, tmp_path, capsys):
    cfg, project, sup = ws
    picks = tmp_path / "picks.json"
    picks.write_text(json.dumps([
        {"row": "a-W3", "model": "sonnet", "why": "a mechanical sweep"},
        {"row": "a-W2", "model": "default", "why": "a root-cause hunt"}]))
    assert cli.cmd_model(cfg, "owner", "", [], "", str(picks)) == 0
    sup._flush_ledger({})

    text = (project / LEDGER_FILE).read_text()
    assert ledger_mod.models(text) == {"a-W3": "sonnet", "a-W4": "opus"}
    assert _git(project, "log", "-1", "--format=%s").strip() == "ledger: model of 2 rows"
    history = ledgerw.history_text(project, cfg.history_dir, "a-W3")
    assert "model default → sonnet; why: a mechanical sweep" in history

    # One bad row refuses the whole sweep before anything is queued.
    assert cli.cmd_model(cfg, "owner", "sonnet", ["a-W4", "a-W1"], "x", None) == 2
    assert "a-W1 is ticked" in capsys.readouterr().err
    assert not ledgerw.pending(cfg)


# -- the hand-back ------------------------------------------------------------------
def _build(cfg, sup, phase: str) -> Path:
    wt = gitq.worktree_add(cfg, phase, sup.log)
    (wt / "code.txt").write_text("half an attempt\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"{phase} work")
    with state_mod.transaction(cfg) as st:
        slot = st.claim_slot(phase)
        slot.branch, slot.worktree = f"swarm/{phase}", str(wt)
    return wt


def test_a_hand_back_ends_the_run_unmerged_and_the_row_starts_again_on_the_default(ws, capsys):
    cfg, project, sup = ws
    _build(cfg, sup, "a-W2")

    assert cli.cmd_escalate(cfg, "a-W2", WHY) == 0
    assert "handed up from sonnet to opus" in capsys.readouterr().out
    assert models_mod.override(cfg, "a-W2") == ""  # at once, before the ledger says so
    sup._on_escalate("a-W2")

    st = state_mod.read(cfg)
    assert not st.in_flight("a-W2") and "a-W2" not in st.done  # free, and ready again
    assert not (cfg.wt_dir / "a-W2").exists() and not (project / "code.txt").exists()
    assert ledger_mod.models((project / LEDGER_FILE).read_text())["a-W2"] == "opus"
    assert f"handed up by its sonnet worker: {WHY}" in ledgerw.history_text(
        project, cfg.history_dir, "a-W2")
    log = cfg.supervisor_log.read_text()
    assert "EVENT escalate a-W2 from=sonnet to=opus freed_slot=0" in log
    assert "RUN-ENDED a-W2 reason=escalated" in log
    assert " claude --model opus " in launch_mod._worker_shell(cfg, "a-W2", project)

    # Once: the next worker is on the swarm's own model, with nowhere to hand it.
    _build(cfg, sup, "a-W2")
    assert cli.cmd_escalate(cfg, "a-W2", WHY) == 2
    assert "already handed up once" in capsys.readouterr().err


@pytest.mark.parametrize("phase, why, word", [
    ("a-W3", WHY, "already runs on the swarm's own model"),
    ("a-W2", "too hard", "at least"),
])
def test_a_hand_back_with_no_other_model_or_no_reason_is_refused(ws, capsys, phase, why, word):
    cfg, _project, sup = ws
    _build(cfg, sup, phase)
    assert cli.cmd_escalate(cfg, phase, why) == 2
    assert word in capsys.readouterr().err
    assert models_mod.handup(cfg, phase) is None and not ledgerw.pending(cfg)


def test_a_hand_back_nobody_read_is_acted_on_when_the_supervisor_next_starts(ws):
    cfg, _project, sup = ws
    _build(cfg, sup, "a-W2")
    assert cli.cmd_escalate(cfg, "a-W2", WHY) == 0
    assert models_mod.unsettled(cfg) == ["a-W2"]
    sup._adopt()
    assert models_mod.unsettled(cfg) == [] and not state_mod.read(cfg).in_flight("a-W2")


# -- the forecast -------------------------------------------------------------------
def _meta(name: str = "") -> model.Meta:
    return model.Meta("W", "alpha", "a", name)


def test_a_row_on_its_own_model_is_timed_from_the_prior_then_from_what_was_measured():
    fresh = model.Durations()
    assert fresh.median_s(_meta("sonnet")) == pytest.approx(0.85 * fresh.median_s(_meta()))

    base = fresh.median_s(_meta())
    samples = [record.Sample(f"a-W{i}", base, 0.0) for i in range(40)] \
        + [record.Sample(f"s-W{i}", base / 2, 0.0) for i in range(40)]
    fitted = model.fit(samples, lambda p: _meta("sonnet" if p.startswith("s-") else ""))
    ratio = fitted.median_s(_meta("sonnet")) / fitted.median_s(_meta())
    assert 0.5 < ratio < 0.7  # pulled from the prior's 0.85 toward the measured half


def test_usage_weight_is_the_prior_until_both_sides_are_metered():
    rows = {f"s-W{i}": "sonnet" for i in range(8)}
    assert tiers.burn_weights({}, rows) == {"sonnet": tiers.BURN_PRIOR}
    meters = {f"a-W{i}": (10.0, 3600.0) for i in range(8)} \
        | {f"s-W{i}": (4.0, 3600.0) for i in range(8)}
    assert tiers.burn_weights(meters, rows) == {"sonnet": pytest.approx(0.4)}


def test_a_slot_counts_as_what_its_model_burns_and_a_hand_back_frees_it():
    log = "\n".join([
        "2026-01-01 00:00:00.000 1.0 LAUNCH a-W2 slot=0",
        "2026-01-01 00:00:00.000 1.0 LAUNCH a-W3 slot=1",
        "2026-01-01 01:00:00.000 2.0 EVENT escalate a-W2 from=sonnet to=opus freed_slot=0",
        "2026-01-01 02:00:00.000 3.0 EVENT done a-W3 ok freed_slot=1 parked=False"])
    events = data_mod.parse_events(log)
    plain = [v for _, v in data_mod.occupancy_series(events, 4).points]
    weighted = [v for _, v in data_mod.occupancy_series(
        events, 4, lambda p: 0.5 if p == "a-W2" else 1.0).points]
    assert plain == [1.0, 2.0, 1.0, 0.0] and weighted == [0.5, 1.5, 1.0, 0.0]


def _plan(rows: dict[str, str]) -> eta_plan.Plan:
    return eta_plan.Plan(
        now=0.0, workers=1,
        rows={r: eta_plan.Row(r, _meta(m), frozenset()) for r, m in rows.items()},
        burn_weight={"sonnet": 0.5})


def test_the_replays_burn_less_for_such_a_row_and_pay_for_its_hand_backs():
    durations = model.Durations(sigma=1e-6, shared=0.0)
    opts = sim.Options(runs=1, growth=False, availability=False)
    week = Cap("week", 0.0, 1000.0, 1e12, burn=10.0)

    def finish(name: str, handup_p: float) -> float:
        futures = sim.simulate(_plan({"a-W2": name}), durations,
                               hazards.Hazards(handup_p=handup_p), Holds(caps=(week,)), opts)
        return futures[0].finish["a-W2"], futures[0].handups

    own, _ = finish("", 0.0)
    cheap, ups = finish("sonnet", 0.0)
    assert cheap == pytest.approx(0.85 * own, rel=1e-3) and ups == 0
    # It works half of its own run, hands back, then runs in full on the default.
    paid, ups = finish("sonnet", 1.0)
    assert paid == pytest.approx(0.5 * cheap + own, rel=1e-3) and ups == 1

    # At 10 points per worker-hour a cap 1 point away stops a default worker
    # after 6 minutes, a half-weight one after 12.
    tight = Cap("week", 0.0, 1.0, 1e12, burn=10.0)
    replay = sim._Replay(_plan({"a-W2": "sonnet"}), durations, hazards.Hazards(handup_p=0.0),
                         Holds(caps=(tight,)), opts, 0)
    replay.run()
    assert replay.caps[0].blocked and replay.caps[0].pct >= 1.0


# -- what the owner sees ------------------------------------------------------------
def test_the_workers_table_names_each_workers_model_and_marks_a_rows_own():
    from types import SimpleNamespace

    from swarm_orchestrator.tui import tables

    dash = SimpleNamespace(row_models={"a-W2": "sonnet"}, own_model="opus")
    assert tables.worker_model(dash, "a-W2") == ("sonnet", False)
    assert tables.worker_model(dash, "a-W3") == ("opus", True)
    assert tables.worker_model(dash, None) is None
    assert len(tables.WORKER_COLUMNS) == len(tables.WORKER_PRIORITY)
    assert "sonnet" in tables.model_cell("sonnet", False) and "—" in tables.model_cell(None)


def test_the_board_counts_rows_by_the_model_that_builds_them(ws):
    from swarm_orchestrator.web import board, rows as web_rows

    cfg, _project, _sup = ws
    parsed, _ = web_rows.parse(LEDGER)
    assert parsed["a-W2"].model == "sonnet" and parsed["a-W3"].model == ""
    assert parsed["a-W2"].title == "the second wave"

    own, named, shown = board._row_models(cfg, parsed)
    assert (own, named, shown) == ("opus", {"a-W2": "sonnet"}, True)
    cards = {"a-W1": {"col": board.DONE, "m": "opus"}, "a-W2": {"col": board.BUILDING, "m": "sonnet"},
             "a-W3": {"col": board.READY, "m": "opus"}, "a-W4": {"col": board.READY, "m": "opus"}}
    assert board._model_counts(cfg, cards, own) == {
        "own": "opus", "handed_up": 0,
        "rows": [{"model": "opus", "done": 1, "running": 0, "left": 2},
                 {"model": "sonnet", "done": 0, "running": 1, "left": 0}]}

    # A row handed back is the swarm's own model's from then on.
    models_mod.record_handup(cfg, "a-W2", "sonnet", "opus", WHY)
    assert board._row_models(cfg, parsed)[1] == {}
    assert board._model_counts(cfg, cards, own)["handed_up"] == 1


def test_only_rows_launched_on_their_model_speak_for_it_in_the_forecast(ws):
    """A row given a model after an earlier attempt ran that attempt on the
    swarm's own: its history must not be read as the model's."""
    from swarm_orchestrator.eta import engine

    cfg, _project, _sup = ws
    st = state_mod.read(cfg)
    quiet = engine.gather(cfg, st, events=[], history=[], ledger_history=engine.pace_mod.History(),
                          usage=[])
    assert quiet.models == {"a-W2": "sonnet"} and quiet.ran_models == {}
    assert quiet.burn_weight == {"sonnet": tiers.BURN_PRIOR}

    events = data_mod.parse_events("2026-01-01 00:00:00.000 1.0 MODEL a-W2 model=sonnet")
    ran = engine.gather(cfg, st, events=events, history=[],
                        ledger_history=engine.pace_mod.History(), usage=[])
    assert ran.ran_models == {"a-W2": "sonnet"}
