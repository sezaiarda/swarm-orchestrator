"""Tier C: isolated worktrees + the serialized merge-queue (real git, no claude).

Every test builds throwaway git repos (a bare ``origin`` + a working clone as the
project) in ``tmp_path`` and drives ``gitq`` / the supervisor's integrate path
directly with fake "workers" (plain git commits on ``swarm/<phase>`` branches).
No tmux, no ``claude``. Proves: two-worker integrate, the ledger different-line
race (no lost update), the conflict block-and-resolve (queue held), optimistic
push-retry, and orphan-branch reconcile on ``up``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import gitq
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


# -- git helpers ----------------------------------------------------------
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "swarm@test")
    _git(repo, "config", "user.name", "swarm")
    _git(repo, "config", "commit.gpgsign", "false")


def _make_project(tmp_path: Path, ledger: str = "L1\nL2\nL3\n") -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(origin)], check=True, capture_output=True)
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-b", "master", str(project)], check=True, capture_output=True)
    _identity(project)
    (project / "PHASE-LEDGER.md").write_text(ledger)
    (project / "README.md").write_text("root\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "init")
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-u", "origin", "master")
    return project, origin


def _cfg(monkeypatch, tmp_path: Path, project: Path, driver: str = "tmux"):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_DRIVER", driver)
    monkeypatch.setenv("SWARM_MASTER_CMD", "true")  # harmless bare master in tests
    monkeypatch.setenv("SWARM_SLUG", "gittest")
    for leak in ("SWARM_WORKER_CMD", "SWARM_READY_MARKER"):
        monkeypatch.delenv(leak, raising=False)
    return load(project_dir=str(project))


def _worker(cfg, phase: str, edits: dict[str, str], log: Log) -> Path:
    """Fake a worker: build swarm/<phase> in its worktree and commit ``edits``."""
    wt = gitq.worktree_add(cfg, phase, log)
    for rel, content in edits.items():
        (wt / rel).write_text(content)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", f"{phase} work")
    return wt


def _tree(repo: Path, ref: str = "master") -> str:
    return _out(repo, "ls-tree", "-r", "--name-only", ref)


# -- Test: two workers integrate to main ----------------------------------
def test_two_workers_integrate_to_main(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        _worker(cfg, "P1", {"a.txt": "A"}, log)
        _worker(cfg, "P2", {"b.txt": "B"}, log)
        assert gitq.integrate(cfg, "P1", log) == gitq.MERGED
        assert gitq.integrate(cfg, "P2", log) == gitq.MERGED

        _git(project, "checkout", "master")
        assert (project / "a.txt").read_text() == "A"
        assert (project / "b.txt").read_text() == "B"
        # branches + worktrees pruned
        assert _out(project, "branch", "--list", "swarm/*").strip() == ""
        assert not (cfg.wt_dir / "P1").exists()
        assert not (cfg.wt_dir / "P2").exists()
        # both landed on origin/master too
        names = _tree(origin)
        assert "a.txt" in names and "b.txt" in names
    finally:
        log.close()


# -- Test: ledger race, different lines, no lost update -------------------
def test_ledger_race_different_lines_no_lost_update(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path, ledger="pricing-P0\nwebhooks-P0\npayments-P0\n")
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        # Both branch off the SAME base, then tick DIFFERENT ledger lines.
        wt1 = gitq.worktree_add(cfg, "P1", log)
        p1 = wt1 / "PHASE-LEDGER.md"
        p1.write_text(p1.read_text().replace("pricing-P0", "pricing-P0 DONE"))
        _git(wt1, "add", "-A")
        _git(wt1, "commit", "-m", "P1 tick")

        wt2 = gitq.worktree_add(cfg, "P2", log)
        p2 = wt2 / "PHASE-LEDGER.md"
        p2.write_text(p2.read_text().replace("payments-P0", "payments-P0 DONE"))
        _git(wt2, "add", "-A")
        _git(wt2, "commit", "-m", "P2 tick")

        assert gitq.integrate(cfg, "P1", log) == gitq.MERGED
        assert gitq.integrate(cfg, "P2", log) == gitq.MERGED  # auto-merges, no conflict

        _git(project, "checkout", "master")
        text = (project / "PHASE-LEDGER.md").read_text()
        assert "pricing-P0 DONE" in text  # neither tick lost
        assert "payments-P0 DONE" in text
        assert "<<<<<<<" not in text  # no residual conflict markers
    finally:
        log.close()


# -- Test: conflict blocks the queue, resolver unblocks it ----------------
def test_conflict_blocks_queue_then_resolves(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path, ledger="SHARED-LINE\n")
    cfg = _cfg(monkeypatch, tmp_path, project, driver="bare")
    log = Log(cfg.supervisor_log)
    from swarm_orchestrator.supervisor import Supervisor

    try:
        # Two workers change the SAME line; an external commit lands on master so
        # BOTH integrations diverge from it and conflict.
        wt1 = _worker(cfg, "P1", {"PHASE-LEDGER.md": "SHARED-LINE P1\n"}, log)  # noqa: F841
        wt2 = _worker(cfg, "P2", {"PHASE-LEDGER.md": "SHARED-LINE P2\n"}, log)  # noqa: F841
        _git(project, "checkout", "master")
        (project / "PHASE-LEDGER.md").write_text("SHARED-LINE EXT\n")
        _git(project, "add", "-A")
        _git(project, "commit", "-m", "external")
        _git(project, "push", "origin", "master")

        # Seed slot state: P1, P2 busy with their branches.
        state_mod.init_state(cfg)
        with state_mod.transaction(cfg) as st:
            st.claim_slot("P1")
            st.claim_slot("P2")
            for s in st.slots:
                if s.phase in ("P1", "P2"):
                    s.branch = f"swarm/{s.phase}"

        sup = Supervisor(cfg)

        # P1 done -> integrate conflicts -> queue blocks on P1.
        sup._on_done("P1", "ok")
        st = state_mod.read(cfg)
        assert st.integ_blocked == "P1"
        assert (project / ".git" / "MERGE_HEAD").is_file()  # left mid-merge

        # P2 done arrives WHILE BLOCKED -> queued, NOT integrated (queue held).
        sup._on_done("P2", "ok")
        st = state_mod.read(cfg)
        assert st.integ_blocked == "P1"  # still blocked on P1
        assert st.integ_queue == ["P1", "P2"]  # P2 waits behind P1, not lost
        assert _out(project, "branch", "--list", "swarm/P2").strip() != ""  # untouched

        # Resolve P1 (fake resolver: keep both intents, commit the merge).
        (project / "PHASE-LEDGER.md").write_text("SHARED-LINE EXT P1\n")
        _git(project, "add", "-A")
        _git(project, "commit", "--no-edit")
        sup._on_resolved("P1")

        # P1 integrated; draining P2 hits the same line -> re-blocks on P2.
        st = state_mod.read(cfg)
        assert st.integ_blocked == "P2"
        assert st.integ_queue == ["P2"]
        assert "P1" in st.done and _out(project, "branch", "--list", "swarm/P1").strip() == ""

        # Resolve P2 -> queue drains empty, both slots freed, main consistent.
        (project / "PHASE-LEDGER.md").write_text("SHARED-LINE EXT P1 P2\n")
        _git(project, "add", "-A")
        _git(project, "commit", "--no-edit")
        sup._on_resolved("P2")

        st = state_mod.read(cfg)
        assert st.integ_blocked is None
        assert st.integ_queue == []
        assert not st.any_busy()  # both slots freed after integration
        _git(project, "checkout", "master")
        text = (project / "PHASE-LEDGER.md").read_text()
        assert "<<<<<<<" not in text and text.strip() == "SHARED-LINE EXT P1 P2"
        assert _out(project, "branch", "--list", "swarm/*").strip() == ""
    finally:
        log.close()


# -- Test: optimistic push retry rebases an external commit ---------------
def test_push_with_retry_rebases_external_commit(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        # A local commit not yet pushed...
        (project / "local.txt").write_text("L")
        _git(project, "add", "-A")
        _git(project, "commit", "-m", "local")
        # ...and an external commit lands on origin first (our push will reject).
        ext = tmp_path / "ext"
        subprocess.run(["git", "clone", str(origin), str(ext)], check=True, capture_output=True)
        _identity(ext)
        (ext / "ext.txt").write_text("E")
        _git(ext, "add", "-A")
        _git(ext, "commit", "-m", "external")
        _git(ext, "push", "origin", "master")

        # First push rejects -> pull --rebase brings the external commit -> repush.
        assert gitq.push_with_retry(project, "master", log) is True
        names = _tree(origin)
        assert "local.txt" in names and "ext.txt" in names  # both present, none lost
        assert "PUSH-RETRY" in cfg.supervisor_log.read_text()
    finally:
        log.close()


# -- Test: reconcile is sentinel-driven, not branch-topology-driven -------
def test_reconcile_uses_sentinel_not_topology(monkeypatch, tmp_path):
    """A leftover branch is integrated only when its worker actually FINISHED
    (durable `ok` sentinel). A committed-but-unsignalled branch is an interrupted
    build: discarded and left NOT done so the master rebuilds it — never silently
    declared complete off branch topology (the H3 misclassification)."""
    from swarm_orchestrator import launch as launch_mod

    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        # (1) FINISHED: worker committed AND wrote its `ok` sentinel, but the
        # supervisor died before integrating -> reconcile completes it.
        _worker(cfg, "P1", {"orphan.txt": "O"}, log)
        launch_mod._write_sentinel(cfg, "P1", "ok", "")
        assert gitq.reconcile_orphans(cfg, {}, log) == ["P1"]
        _git(project, "checkout", "master")
        assert (project / "orphan.txt").read_text() == "O"
        assert _out(project, "branch", "--list", "swarm/P1").strip() == ""

        # (2) INTERRUPTED: committed real work but NO `swarm done` sentinel ->
        # discarded, NOT integrated, NOT done, so the master rebuilds it.
        _worker(cfg, "P2", {"p2.txt": "2"}, log)
        assert gitq.reconcile_orphans(cfg, {}, log) == []
        assert _out(project, "branch", "--list", "swarm/P2").strip() == ""
        assert "p2.txt" not in _tree(project)  # partial work dropped, not merged

        # (2b) The 0-commit case (H3): a branch created at launch but never even
        # ticked -> also interrupted, discarded, never phantom-marked done.
        gitq.worktree_add(cfg, "P4", log)
        assert gitq.reconcile_orphans(cfg, {}, log) == []
        assert _out(project, "branch", "--list", "swarm/P4").strip() == ""

        # (3) A phase already RECORDED done -> GC'd, NOT re-integrated.
        _worker(cfg, "P3", {"p3.txt": "3"}, log)
        assert gitq.reconcile_orphans(cfg, {"P3": "ok"}, log) == []
        assert _out(project, "branch", "--list", "swarm/P3").strip() == ""
        assert "p3.txt" not in _tree(project)  # its work was not merged
    finally:
        log.close()


# -- Test: a clean merge whose PUSH fails is PUSH_FAILED, not a clean-tree wedge
def test_push_failure_is_distinct_from_conflict(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        _worker(cfg, "P1", {"p1.txt": "1"}, log)
        # Make the remote unreachable so the push fails for a non-conflict reason.
        _git(project, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
        assert gitq.integrate(cfg, "P1", log) == gitq.PUSH_FAILED
        # Tree stays clean — nothing for a resolver to fix; no phantom wedge.
        assert not (project / ".git" / "MERGE_HEAD").is_file()
        assert gitq.blocked_repo(cfg, "P1") is None
    finally:
        log.close()


# -- Test: a dirty canonical tree is HELD (DIRTY), never a spurious CONFLICT
def test_dirty_umbrella_tree_is_held_not_wedged(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        _worker(cfg, "P1", {"p1.txt": "1"}, log)
        (project / "README.md").write_text("uncommitted owner edit\n")  # dirty tree
        assert gitq.integrate(cfg, "P1", log) == gitq.DIRTY
        # No merge was started, so no resolver is needed and nothing is wedged.
        assert not (project / ".git" / "MERGE_HEAD").is_file()
    finally:
        log.close()


# -- Test: an untracked file in the umbrella does NOT hold the queue as DIRTY
def test_untracked_file_does_not_hold_integrate(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        _worker(cfg, "P1", {"p1.txt": "1"}, log)
        # A stray untracked file (like a runtime .swarm.toml) must not wedge things.
        (project / ".swarm.toml").write_text("[git]\nisolation='worktree'\n")
        assert gitq.integrate(cfg, "P1", log) == gitq.MERGED  # merged, not held DIRTY
    finally:
        log.close()


# -- Test: a push-time / base conflict leaves a RESOLVABLE mid-merge, not a wedge
def test_push_time_conflict_is_resolvable(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        _worker(cfg, "P1", {"PHASE-LEDGER.md": "L1 P1\nL2\nL3\n"}, log)
        # An external writer lands a conflicting commit on origin before our push.
        ext = tmp_path / "ext"
        subprocess.run(["git", "clone", str(origin), str(ext)], check=True, capture_output=True)
        _identity(ext)
        (ext / "PHASE-LEDGER.md").write_text("L1 EXT\nL2\nL3\n")
        _git(ext, "add", "-A"); _git(ext, "commit", "-m", "ext"); _git(ext, "push", "origin", "master")

        # Integrate conflicts, but leaves a REAL mid-merge a resolver can finish.
        assert gitq.integrate(cfg, "P1", log) == gitq.CONFLICT
        assert (project / ".git" / "MERGE_HEAD").is_file()
        assert not gitq.resolve_ready(cfg, project)  # premature `resolved` refused

        # Resolver keeps both intents, commits; re-integrate drains cleanly.
        (project / "PHASE-LEDGER.md").write_text("L1 EXT P1\nL2\nL3\n")
        _git(project, "add", "-A"); _git(project, "commit", "--no-edit")
        assert gitq.resolve_ready(cfg, project)
        assert gitq.integrate(cfg, "P1", log) == gitq.MERGED
        _git(project, "checkout", "master")
        text = (project / "PHASE-LEDGER.md").read_text()
        assert "L1 EXT P1" in text and "<<<<<<<" not in text
        assert _out(project, "branch", "--list", "swarm/*").strip() == ""
    finally:
        log.close()


# -- Test: default isolation is 'none' (today's behavior unchanged) -------
def test_isolation_defaults_to_none(monkeypatch, tmp_path):
    project, _ = _make_project(tmp_path)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    for leak in ("SWARM_GIT_ISOLATION", "SWARM_GIT_MAIN"):
        monkeypatch.delenv(leak, raising=False)
    cfg = load(project_dir=str(project))
    assert cfg.git_isolation == "none"  # opt-in: worktree machinery never engages
    assert cfg.git_main_branch == "master"
