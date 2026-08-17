"""`swarm why` — the stall explainer.

Hermetic: builds a Config against the demo project with an isolated state dir,
writes a State by hand, and captures stdout. No supervisor, no tmux server, no
fake claude — `cmd_why` is a pure read of state + ledger, and that is exactly
what makes it safe to run at any moment during a live swarm.

What is under test is not formatting but *coverage of the stall reasons*: every
condition that can stop a run must produce a finding that names the remedy,
because the pure-injection design has no auto-retry and the owner is therefore
the recovery path.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from swarm_orchestrator import cli
from swarm_orchestrator import config as config_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.state import Slot, State

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "examples" / "demo"


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    shutil.copytree(DEMO, project)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "whytest")
    monkeypatch.setenv("SWARM_DRIVER", "none")
    c = config_mod.load(project_dir=str(project))
    c.state_dir.mkdir(parents=True, exist_ok=True)
    return c


def _why(cfg, st: State, capsys) -> str:
    state_mod._save(cfg, st)
    assert cli.cmd_why(cfg) == 0
    return capsys.readouterr().out


def _base(**kw) -> State:
    kw.setdefault("supervisor_pid", None)
    return State(slots=[Slot(id=0), Slot(id=1)], **kw)


def test_why_reports_dead_supervisor(cfg, capsys):
    out = _why(cfg, _base(), capsys)
    assert "STALLED" in out
    assert "swarm up" in out


def test_why_reports_paused(cfg, capsys):
    out = _why(cfg, _base(paused=True, supervisor_pid=os.getpid()), capsys)
    assert "PAUSED" in out
    assert "swarm resume" in out


def test_why_leads_with_a_blocked_integration(cfg, capsys):
    st = _base(supervisor_pid=os.getpid())
    st.integ_blocked = "P1"
    st.integ_blocked_kind = "conflict"
    st.integ_blocked_repo = str(cfg.project_dir)
    out = _why(cfg, st, capsys)
    assert "BLOCKED" in out
    # The remedy must be spelled out — a hold nobody knows how to clear is the
    # failure this command exists to prevent.
    assert "swarm resolved P1" in out
    # A conflict with no resolver pane recorded must say so, not stay silent.
    assert "resolver: none open" in out


def test_why_names_a_missing_resolver_pane(cfg, capsys):
    st = _base(supervisor_pid=os.getpid())
    st.integ_blocked = "P1"
    st.integ_blocked_kind = "conflict"
    st.integ_blocked_repo = str(cfg.project_dir)
    st.windows["resolve:P1"] = "%999"  # never existed / already died
    out = _why(cfg, st, capsys)
    assert "GONE" in out


def test_why_surfaces_a_worker_waiting_on_the_owner(cfg, capsys):
    st = _base(supervisor_pid=os.getpid())
    st.waiting["P1"] = 0.0
    out = _why(cfg, st, capsys)
    assert "WAITING" in out
    assert "P1" in out


def test_why_does_not_claim_a_parked_phase_is_still_unanswered(cfg, capsys):
    """Parked means off-grid, NOT unanswered.

    `swarm resumed` cancels the park timer, but a phase that was already parked
    stays in `parked` until it reports done. Calling that "waiting on YOU" sends
    the owner to answer a question they may have answered an hour ago while the
    worker is busy building — the same cry-wolf failure as the DECIDING case.
    """
    st = _base(supervisor_pid=os.getpid(), master_alive=True)
    st.parked = ["P1"]  # answered: no longer in `waiting`
    out = _why(cfg, st, capsys)
    assert "PARKED" in out and "P1" in out
    assert "waiting on YOU" not in out
    assert "WAITING" not in out


def test_why_flags_the_lost_nudge_race(cfg, capsys):
    """Free slots + ready phases + NO master is the one accepted race."""
    st = _base(supervisor_pid=os.getpid(), master_alive=False)
    out = _why(cfg, st, capsys)
    assert "IDLE" in out
    assert "swarm launch" in out


def test_why_does_not_cry_wolf_while_a_master_is_deciding(cfg, capsys):
    """A live master with free slots is mid-decision, not a lost nudge.

    Regression: the first cut of `why` omitted the `master_alive` check and
    reported IDLE during the ~30 s between a resume poke and the master's claim.
    That sent the owner to `swarm launch`, whose launch was then refused by the
    atomic slot claim — correct behaviour that looks like a broken swarm. A
    diagnostic that cries wolf during normal operation is worse than none.
    """
    st = _base(supervisor_pid=os.getpid(), master_alive=True)
    out = _why(cfg, st, capsys)
    assert "DECIDING" in out
    assert "IDLE" not in out
    assert "swarm launch" not in out


def test_why_shows_running_work_while_a_master_fills_the_other_slot(cfg, capsys):
    """DECIDING must ADD to the picture, not replace it.

    One slot building and one being filled is the normal mid-campaign shape; a
    reader who is told only "master is deciding" has lost the more useful fact,
    which is what is currently building.
    """
    # Advance past the root so more than one phase is eligible at once, then put
    # exactly one of them in a slot — leaving a free slot AND a ready phase.
    root = cli.build_context(cfg, _base())["ready"][0]
    st = _base(supervisor_pid=os.getpid(), master_alive=True)
    st.done[root] = "ok"
    unlocked = cli.build_context(cfg, st)["ready"]
    if len(unlocked) < 2:
        pytest.skip("demo ledger has no two-wide frontier to exercise this shape")
    st.slots[0].busy = True
    st.slots[0].phase = unlocked[0]
    out = _why(cfg, st, capsys)
    assert "WORKING" in out and unlocked[0] in out
    assert "DECIDING" in out


def test_why_explains_a_healthy_serial_run(cfg, capsys):
    """No findings: say what it is waiting ON, never just 'fine'."""
    graph_root = next(iter(sorted(cli.build_context(cfg, _base()).get("ready", []))), None)
    st = _base(supervisor_pid=os.getpid())
    st.slots[0].busy = True
    st.slots[0].phase = graph_root
    out = _why(cfg, st, capsys)
    assert "WORKING" in out
    assert graph_root in out
    # Idle-but-not-stuck must be explained, not left to look like a stall.
    assert "depends on one still building" in out or "ready" in out


def test_why_never_mutates_state(cfg, capsys):
    st = _base(supervisor_pid=os.getpid())
    _why(cfg, st, capsys)
    before = state_mod.read(cfg)
    cli.cmd_why(cfg)
    capsys.readouterr()
    after = state_mod.read(cfg)
    assert before.done == after.done
    assert before.integ_queue == after.integ_queue
    assert [s.phase for s in before.slots] == [s.phase for s in after.slots]
