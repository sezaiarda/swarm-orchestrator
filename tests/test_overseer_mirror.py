"""The Overseer's own mirror under worktree isolation (real git, no claude).

A pass edits the ledger. Under ``isolation = "worktree"`` it does so in a mirror
of its own (``ovs-<id>``, branch ``swarm/ovs-<id>``), so a half-made edit never
touches the canonical tree the integrator merges into; when the pass ends its
commits land through the ordinary merge queue. What must never happen: the
mirror is discarded at ``swarm up`` as an interrupted phase, or a landed pass is
recorded ``done`` as though it were a ledger phase.
"""

from __future__ import annotations

import shutil

import pytest

from swarm_orchestrator import gitq, ovrecord
from swarm_orchestrator import overseer as ov
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import _mirror_plan
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor
from test_worktree import _cfg, _git, _make_project, _out, _tree

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@pytest.fixture
def env(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path)
    monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(tmp_path / "claude.json"))  # never the real one
    monkeypatch.setenv("SWARM_OVERSEER", "1")
    cfg = _cfg(monkeypatch, tmp_path, project, driver="bare")
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)
    sup._bootstrapped = True
    sup._doctor_probed = 1e18
    spawned = []
    monkeypatch.setattr(sup.master, "spawn", lambda kind, pane=None, **kw: spawned.append(kw) or True)
    monkeypatch.setattr(sup.master, "is_alive", lambda: False)
    monkeypatch.setattr(sup.master, "kill", lambda: None)
    args = []
    monkeypatch.setattr(sup, "_start_overseer_spawn", lambda *a: args.append(a))
    yield cfg, project, origin, sup, spawned, args
    sup.log.close()


def _start(sup, args) -> str:
    sup.overseer.request(ov.MANUAL, "by hand", urgent=True)
    sup._overseer_tick()
    pid = sup._overseer_live
    sup._overseer_spawn(*args[-1])
    sup._on_overseer_spawned(pid, "ok")
    return pid


def test_a_pass_works_in_its_own_mirror_and_its_ledger_edit_lands(env):
    cfg, project, origin, sup, spawned, args = env
    pid = _start(sup, args)
    mirror = cfg.wt_dir / ovrecord.mirror_name(pid)
    assert spawned[0]["cwd"] == mirror
    assert spawned[0]["env"]["SWARM_WORKTREE"] == str(mirror)
    assert spawned[0]["env"]["SWARM_PROJECT"] == str(project)
    assert ovrecord.load_json(cfg, pid).mirror == mirror.name

    (mirror / "PHASE-LEDGER.md").write_text("L1\nL2\nL3\nL4 needs:L3\n")
    _git(mirror, "add", "-A")
    _git(mirror, "commit", "-m", "overseer: file L4 from L3's risk")
    assert (project / "PHASE-LEDGER.md").read_text() == "L1\nL2\nL3\n"  # canonical untouched

    sup._end_overseer_pass(pid, ovrecord.DONE)

    _git(project, "checkout", "master")
    assert (project / "PHASE-LEDGER.md").read_text().endswith("L4 needs:L3\n")
    assert "PHASE-LEDGER.md" in _tree(origin)
    assert not mirror.exists()
    assert _out(project, "branch", "--list", "swarm/*").strip() == ""
    st = state_mod.read(cfg)
    assert st.integ_queue == [] and mirror.name not in st.done
    assert f"OVERSEER-INTEGRATED {mirror.name}" in cfg.supervisor_log.read_text()


def test_swarm_up_lands_a_pass_mirror_instead_of_discarding_it(env):
    cfg, project, _origin, sup, _spawned, args = env
    pid = _start(sup, args)
    mirror = cfg.wt_dir / ovrecord.mirror_name(pid)
    (mirror / "note.txt").write_text("committed before the supervisor died\n")
    _git(mirror, "add", "-A")
    _git(mirror, "commit", "-m", "overseer edit")

    log = Log(cfg.supervisor_log)
    try:
        plan = _mirror_plan(cfg)
        result = gitq.reconcile(cfg, {}, log, operator=plan)
    finally:
        log.close()

    assert plan == {mirror.name: "integrate"}
    assert result.operator_integrated == [mirror.name] and result.integrated == []
    _git(project, "checkout", "master")
    assert (project / "note.txt").is_file()


def test_a_pass_that_would_not_start_leaves_no_mirror(env):
    cfg, project, _origin, sup, _spawned, args = env
    sup.overseer.request(ov.MANUAL, "by hand", urgent=True)
    sup._overseer_tick()
    pid = sup._overseer_live
    sup._overseer_spawn(*args[-1])
    sup._on_overseer_spawned(pid, "failed")
    assert not (cfg.wt_dir / ovrecord.mirror_name(pid)).exists()
    assert _out(project, "branch", "--list", "swarm/*").strip() == ""
