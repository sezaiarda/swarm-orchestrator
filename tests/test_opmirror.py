"""The operator's own workspace mirror under worktree isolation (real git, no claude).

An operator job is a side worker: under ``isolation = "worktree"`` it gets a
full-workspace mirror named ``op-<job>`` (branch ``swarm/op-<job>``), commits
there like a worker, and on ``operator-done`` its branch goes through the same
merge queue a phase does — then the mirror is gone. What must never happen:

* the mirror is mistaken for an interrupted phase at ``swarm up`` and discarded
  with the job's commits in it;
* a retry rebuilds the mirror from scratch over commits a previous attempt made;
* a landed job is recorded ``done`` as though it were a ledger phase.

Driver is ``bare``, so no session is ever started: only the git side is real.
"""

from __future__ import annotations

import shutil

import pytest

from swarm_orchestrator import gitq
from swarm_orchestrator import operator as operator_mod
from swarm_orchestrator import opqueue
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor
from test_worktree import _cfg, _git, _make_project, _out, _tree

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)

JOB = "coral-W4"
NOTE = "roll the gateway to the new tag on the live box and verify it answers"


@pytest.fixture
def env(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path)
    (project / ".swarm.toml").write_text("[operator]\nenabled = true\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "operator on")
    _git(project, "push", "origin", "master")
    monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(tmp_path / "claude.json"))  # never the real one
    cfg = _cfg(monkeypatch, tmp_path, project, driver="bare")
    assert cfg.operator_enabled and cfg.git_isolation == "worktree"
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    yield cfg, project, origin, log
    log.close()


def _queue(cfg, job: str = JOB) -> None:
    assert opqueue.add(cfg, job, status="operator", note=NOTE) is not None


def test_a_job_gets_its_own_mirror_distinct_from_any_phase(env):
    cfg, project, _origin, log = env
    _queue(cfg)

    assert operator_mod.dispatch(cfg, JOB, log) is True

    mirror = cfg.wt_dir / f"op-{JOB}"
    assert (mirror / "README.md").is_file()
    assert _out(project, "branch", "--list", f"swarm/op-{JOB}").strip()
    assert not (cfg.wt_dir / JOB).exists()  # never the phase's own mirror name
    assert opqueue.load(cfg, JOB).mirror == f"op-{JOB}"


def test_an_ad_hoc_job_is_not_prefixed_twice():
    assert operator_mod.mirror_name("op-1700000000") == "op-1700000000"
    assert operator_mod.mirror_name("olive-W3") == "op-olive-W3"


def test_the_session_env_points_swarm_at_this_run_from_the_mirror(env):
    cfg, _project, _origin, _log = env
    mirror = cfg.wt_dir / f"op-{JOB}"

    envv = operator_mod._operator_env(cfg, JOB, mirror)
    cmd = operator_mod.operator_command(cfg, JOB, mirror)

    assert envv["SWARM_WORKTREE"] == str(mirror)
    assert envv["SWARM_PROJECT"] == str(cfg.project_dir)
    assert envv["SWARM_STATE_DIR"] == str(cfg.state_dir)
    assert envv["SWARM_GIT_ISOLATION"] == "worktree"
    assert cmd.startswith(f"cd {mirror} && exec claude")


def test_operator_done_lands_the_mirror_through_the_merge_queue(env):
    cfg, project, origin, log = env
    _queue(cfg)
    assert operator_mod.dispatch(cfg, JOB, log) is True
    mirror = cfg.wt_dir / f"op-{JOB}"
    (mirror / "deployed.txt").write_text("gateway on v1.2.3\n")
    _git(mirror, "add", "-A")
    _git(mirror, "commit", "-m", "record the roll")

    opqueue.complete(cfg, JOB, "rolled the gateway")
    Supervisor(cfg)._on_operator_done(JOB)

    _git(project, "checkout", "master")
    assert (project / "deployed.txt").read_text() == "gateway on v1.2.3\n"
    assert "deployed.txt" in _tree(origin)  # pushed like any integration
    assert not mirror.exists()
    assert _out(project, "branch", "--list", "swarm/*").strip() == ""
    st = state_mod.read(cfg)
    assert st.integ_queue == [] and st.integ_blocked is None
    assert f"op-{JOB}" not in st.done and JOB not in st.done  # not a ledger phase
    assert st.operator_phase is None
    assert f"OPERATOR-INTEGRATED op-{JOB}" in cfg.supervisor_log.read_text()


def test_a_retry_reuses_the_mirror_and_keeps_the_commits_already_made(env):
    cfg, project, _origin, log = env
    _queue(cfg)
    assert operator_mod.dispatch(cfg, JOB, log) is True
    mirror = cfg.wt_dir / f"op-{JOB}"
    (mirror / "half.txt").write_text("first attempt\n")
    _git(mirror, "add", "-A")
    _git(mirror, "commit", "-m", "half done")
    opqueue.release(cfg, JOB, "session died", now=0.0)  # back-off already over
    operator_mod.release(cfg, log)

    assert operator_mod.dispatch(cfg, JOB, log) is True

    assert (mirror / "half.txt").read_text() == "first attempt\n"
    assert "OPERATOR-MIRROR-REUSE" in cfg.supervisor_log.read_text()


def test_swarm_up_keeps_a_live_jobs_mirror_and_lands_a_finished_one(env):
    """Without the plan both read as interrupted phases and are discarded."""
    cfg, project, _origin, log = env
    _queue(cfg, "live-W1")
    _queue(cfg, "done-W2")
    for job, name in (("live-W1", "keep.txt"), ("done-W2", "landed.txt")):
        with state_mod.transaction(cfg) as st:
            st.release_operator()
        assert operator_mod.dispatch(cfg, job, log) is True
        mirror = cfg.wt_dir / f"op-{job}"
        (mirror / name).write_text(job)
        _git(mirror, "add", "-A")
        _git(mirror, "commit", "-m", job)
    opqueue.complete(cfg, "done-W2", "done")

    plan = operator_mod.mirror_plan(cfg)
    result = gitq.reconcile(cfg, {}, log, operator=plan)

    assert plan == {"op-live-W1": operator_mod.KEEP, "op-done-W2": operator_mod.INTEGRATE}
    assert result.integrated == [] and result.operator_integrated == ["op-done-W2"]
    assert (cfg.wt_dir / "op-live-W1" / "keep.txt").is_file()  # kept, commits and all
    _git(project, "checkout", "master")
    assert (project / "landed.txt").read_text() == "done-W2"
    assert _out(project, "branch", "--list", "swarm/op-done-W2").strip() == ""


def test_swarm_up_still_discards_an_abandoned_jobs_mirror(env):
    cfg, project, _origin, log = env
    _queue(cfg)
    assert operator_mod.dispatch(cfg, JOB, log) is True
    opqueue.abandon(cfg, JOB, "gave up")

    gitq.reconcile(cfg, {}, log, operator=operator_mod.mirror_plan(cfg))

    assert not (cfg.wt_dir / f"op-{JOB}").exists()
    assert _out(project, "branch", "--list", "swarm/*").strip() == ""


def test_under_isolation_none_the_job_runs_in_the_project(env, monkeypatch):
    cfg, _project, _origin, log = env
    cfg.git_isolation = "none"
    _queue(cfg)

    assert operator_mod.dispatch(cfg, JOB, log) is True

    assert not (cfg.wt_dir / f"op-{JOB}").exists()
    assert opqueue.load(cfg, JOB).mirror == ""
    assert operator_mod.integration_for(cfg, JOB) is None
    assert "project itself" in operator_mod.brief(cfg, opqueue.load(cfg, JOB))


def test_a_long_brief_goes_to_a_file_and_the_pane_gets_one_short_line(env, monkeypatch):
    """A 1-2 KB brief typed into the pane is folded by Claude Code into
    "[Pasted text #1]" and never submits; the job would be silently abandoned
    -- so the pane must only ever get a short pointer."""
    cfg, _project, _origin, log = env
    long_note = "Roll the provider and verify it. " * 60  # ~2 KB, like a real hand-off
    assert opqueue.add(cfg, JOB, status="operator", note=long_note) is not None
    item = opqueue.load(cfg, JOB)
    sent: list[str] = []
    monkeypatch.setattr(operator_mod.launch_mod, "await_ready", lambda *a, **k: True)
    monkeypatch.setattr(operator_mod.tmux, "send_submit", lambda pane, text, *a, **k: sent.append(text) or True)

    assert operator_mod._deliver(cfg, "%1", item, log) is True

    brief_file = opqueue.item_path(cfg, JOB).with_suffix(".brief.md")
    assert brief_file.read_text(encoding="utf-8").strip() == operator_mod.brief(cfg, item)
    assert len(sent) == 1 and len(sent[0]) < 300
    assert str(brief_file) in sent[0]
    assert opqueue.load(cfg, JOB) is not None  # the queue still reads only *.json


def test_swarm_up_reconciles_what_a_down_left_behind(env):
    """The aftermath of a `swarm down`: state still showed two busy
    slots and a running operator job, and every repo still held the worktrees and
    `swarm/*` branches of two workers, the operator job and an Overseer pass —
    all empty. What `swarm up` does before it starts a supervisor (``init_state``,
    ``opqueue.reconcile``, ``_reconcile_orphans``, in that order) must leave: no
    busy slot, the job queued for another attempt, no mirror, no branch, and every
    `done` record where it was."""
    from swarm_orchestrator import cli, ovrecord

    cfg, project, _origin, log = env
    with state_mod.transaction(cfg) as st:
        st.mark_done("L0", "ok")
    (cfg.done_dir / "L0.ok").write_text("L0 ok landed\n", encoding="utf-8")
    for phase in ("L1", "L2"):  # two launched workers, killed before a commit
        with state_mod.transaction(cfg) as st:
            assert st.claim_slot(phase) is not None
        gitq.worktree_add(cfg, phase, log)
    _queue(cfg)  # an operator job, dispatched and killed before a commit
    assert operator_mod.dispatch(cfg, JOB, log) is True
    pass_id = "20260925T201209Z"  # an Overseer pass, interrupted before a commit
    ovrecord.create(cfg, pass_id, [], None, ovrecord.mirror_name(pass_id))
    gitq.worktree_add(cfg, ovrecord.mirror_name(pass_id), log)
    ovrecord.update(cfg, pass_id, status=ovrecord.INTERRUPTED, ended_at=1.0)
    st = state_mod.read(cfg)
    assert sorted(s.phase for s in st.busy_slots()) == ["L1", "L2"]
    assert st.operator_phase == JOB and opqueue.load(cfg, JOB).state == opqueue.RUNNING
    assert _out(project, "branch", "--list", "swarm/*").count("swarm/") == 4

    state_mod.init_state(cfg)
    opqueue.reconcile(cfg, log)
    cli._reconcile_orphans(cfg)

    st = state_mod.read(cfg)
    assert st.busy_slots() == [] and st.operator_phase is None
    assert st.done == {"L0": "ok"}
    assert (cfg.done_dir / "L0.ok").is_file()
    item = opqueue.load(cfg, JOB)
    assert item.state == opqueue.QUEUED and item.attempts == 1
    assert _out(project, "branch", "--list", "swarm/*").strip() == ""
    assert [p.name for p in cfg.wt_dir.iterdir() if p.is_dir()] == []
    assert "empty operator mirror" in cfg.supervisor_log.read_text()

    assert operator_mod.dispatch(cfg, JOB, log) is True  # the retry builds afresh
    assert (cfg.wt_dir / f"op-{JOB}" / "README.md").is_file()


def test_swarm_up_keeps_a_live_jobs_mirror_that_holds_only_an_uncommitted_edit(env):
    cfg, _project, _origin, log = env
    _queue(cfg)
    assert operator_mod.dispatch(cfg, JOB, log) is True
    (cfg.wt_dir / f"op-{JOB}" / "draft.txt").write_text("not committed yet\n")

    gitq.reconcile(cfg, {}, log, operator=operator_mod.mirror_plan(cfg))

    assert (cfg.wt_dir / f"op-{JOB}" / "draft.txt").is_file()
