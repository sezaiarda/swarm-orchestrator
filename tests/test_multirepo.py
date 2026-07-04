"""Tier C+: the multi-repo full-workspace-mirror model + supervisor lifecycle.

Mirrors a multi-repo layout: an umbrella repo that gitignores independent component repos.
Each phase gets a full isolated mirror (umbrella + every component repo, nested at
its real path, all on ``swarm/<phase>``). Proves the mirror layout, that concurrent
phases in the SAME repo are isolated, that untouched repos are 0-ahead no-ops
merged without network, that a failed phase rolls back every repo, and the
supervisor lifecycle guards. Real git, no tmux, no claude.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import gitq
from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


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


def _init_repo(path: Path, seed: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "master", str(path)], check=True, capture_output=True)
    _identity(path)
    for rel, content in seed.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


def _with_origin(repo: Path, origin: Path) -> None:
    subprocess.run(["git", "init", "--bare", "-b", "master", str(origin)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "master")


def _make_workspace(tmp_path: Path, siblings=("pricing", "webhooks")):
    """An umbrella that gitignores independent component repos."""
    project = tmp_path / "myproject"
    _init_repo(
        project,
        {
            "docs/PHASE-LEDGER.md": "".join(f"{s}-P1\n" for s in siblings),
            ".gitignore": "".join(f"/{s}/\n" for s in siblings),
        },
    )
    repos = {}
    for s in siblings:
        r = _init_repo(project / s, {"code.txt": "base\n"})
        _with_origin(r, tmp_path / f"{s}.git")
        repos[s] = r
    _with_origin(project, tmp_path / "myproject.git")
    return project, repos


def _cfg(monkeypatch, tmp_path, project, driver="bare", repos_glob=None):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_DRIVER", driver)
    monkeypatch.setenv("SWARM_MASTER_CMD", "true")
    monkeypatch.setenv("SWARM_WORKER_CMD", "true")
    monkeypatch.setenv("SWARM_SLUG", "mrtest")
    if repos_glob is not None:
        monkeypatch.setenv("SWARM_GIT_REPOS", repos_glob)
    else:
        monkeypatch.delenv("SWARM_GIT_REPOS", raising=False)
    monkeypatch.delenv("SWARM_READY_MARKER", raising=False)
    return load(project_dir=str(project))


# -- discovery ------------------------------------------------------------
def test_discovers_direct_child_repos_by_default(monkeypatch, tmp_path):
    project, _ = _make_workspace(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    names = sorted(r.name for r in gitq.discovered_repos(cfg))
    assert names == ["pricing", "webhooks"]  # docs/ (no .git) and the umbrella excluded


def test_repos_glob_is_configurable(monkeypatch, tmp_path):
    project, _ = _make_workspace(tmp_path)
    # Restrict to just pricing via the glob knob.
    cfg = _cfg(monkeypatch, tmp_path, project, repos_glob="pricing")
    assert [r.name for r in gitq.discovered_repos(cfg)] == ["pricing"]


# -- the full-workspace mirror --------------------------------------------
def test_worktree_add_builds_a_full_nested_mirror(monkeypatch, tmp_path):
    project, _ = _make_workspace(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        wt = gitq.worktree_add(cfg, "P1", log)
        # The mirror looks like the real project: umbrella files + every repo,
        # each checked out on swarm/P1, at its real path (`cd pricing` works).
        assert (wt / "docs" / "PHASE-LEDGER.md").is_file()
        assert (wt / "pricing" / "code.txt").read_text() == "base\n"
        assert (wt / "webhooks" / "code.txt").read_text() == "base\n"
        assert _out(wt / "pricing", "rev-parse", "--abbrev-ref", "HEAD").strip() == "swarm/P1"
        # The nested component worktrees are gitignored -> umbrella stays clean.
        assert _out(wt, "status", "--porcelain").strip() == ""
    finally:
        log.close()


def test_concurrent_phases_in_the_same_repo_are_isolated(monkeypatch, tmp_path):
    """Two phases can build in the SAME component repo at once — each in its own
    worktree/branch — and both integrate without interfering."""
    project, repos = _make_workspace(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        wt1 = gitq.worktree_add(cfg, "P1", log)
        wt2 = gitq.worktree_add(cfg, "P2", log)
        (wt1 / "pricing" / "code.txt").write_text("P1 change\n")
        _git(wt1 / "pricing", "add", "-A"); _git(wt1 / "pricing", "commit", "-m", "P1")
        (wt2 / "pricing" / "new2.txt").write_text("P2 change\n")
        _git(wt2 / "pricing", "add", "-A"); _git(wt2 / "pricing", "commit", "-m", "P2")

        assert gitq.integrate(cfg, "P1", log) == gitq.MERGED
        assert gitq.integrate(cfg, "P2", log) == gitq.MERGED
        _git(repos["pricing"], "checkout", "master")
        # Both landed (P2 auto-merged on top of P1 — different files).
        assert (repos["pricing"] / "code.txt").read_text() == "P1 change\n"
        assert (repos["pricing"] / "new2.txt").read_text() == "P2 change\n"
        assert _out(repos["pricing"], "branch", "--list", "swarm/*").strip() == ""
    finally:
        log.close()


def test_untouched_repo_is_a_noop(monkeypatch, tmp_path):
    """A phase that only edits pricing still 'integrates' webhooks — but webhooks is
    0 commits ahead, so it is pruned with no merge and its main is untouched."""
    project, repos = _make_workspace(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        wt = gitq.worktree_add(cfg, "P1", log)
        (wt / "pricing" / "code.txt").write_text("only pricing\n")
        _git(wt / "pricing", "add", "-A"); _git(wt / "pricing", "commit", "-m", "P1")
        (wt / "docs" / "PHASE-LEDGER.md").write_text("pricing-P1 DONE\nwebhooks-P1\n")
        _git(wt, "add", "-A"); _git(wt, "commit", "-m", "tick")

        webhooks_before = _out(repos["webhooks"], "rev-parse", "master").strip()
        assert gitq.integrate(cfg, "P1", log) == gitq.MERGED
        _git(repos["pricing"], "checkout", "master")
        assert (repos["pricing"] / "code.txt").read_text() == "only pricing\n"  # merged
        assert _out(repos["webhooks"], "rev-parse", "master").strip() == webhooks_before  # untouched
        # every swarm branch pruned in every repo; mirror dir removed
        for r in (project, repos["pricing"], repos["webhooks"]):
            assert _out(r, "branch", "--list", "swarm/*").strip() == ""
        assert not (cfg.wt_dir / "P1").exists()
    finally:
        log.close()


def test_failed_phase_rolls_back_every_repo(monkeypatch, tmp_path):
    project, repos = _make_workspace(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        wt = gitq.worktree_add(cfg, "P1", log)
        (wt / "pricing" / "code.txt").write_text("half-built\n")
        _git(wt / "pricing", "add", "-A"); _git(wt / "pricing", "commit", "-m", "broken")

        gitq.discard(cfg, "P1", log)  # a failed build

        assert not (cfg.wt_dir / "P1").exists()  # whole mirror gone
        for r in (project, repos["pricing"], repos["webhooks"]):
            assert _out(r, "branch", "--list", "swarm/*").strip() == ""
        _git(repos["pricing"], "checkout", "master")
        assert (repos["pricing"] / "code.txt").read_text() == "base\n"  # not left live
    finally:
        log.close()


# -- supervisor lifecycle guards (bare cfg, no git needed) ----------------
def _bare_cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_SLUG", "life")
    monkeypatch.delenv("SWARM_GIT_REPOS", raising=False)
    return load(project_dir=str(tmp_path))


def test_finish_is_held_while_an_integration_is_pending(monkeypatch, tmp_path):
    cfg = _bare_cfg(monkeypatch, tmp_path)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.integ_queue = ["P1"]
    sup = Supervisor(cfg)
    try:
        sup._on_master_idle()
        assert not state_mod.read(cfg).finished
        assert "integrating=True" in cfg.supervisor_log.read_text()
    finally:
        sup.log.close()


def test_late_fail_is_ignored_for_a_blocked_phase(monkeypatch, tmp_path):
    cfg = _bare_cfg(monkeypatch, tmp_path)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P1")
        st.integ_blocked = "P1"
    sup = Supervisor(cfg)
    try:
        sup._on_done("P1", "fail")
        st = state_mod.read(cfg)
        assert any(s.busy and s.phase == "P1" for s in st.slots)
        assert st.integ_blocked == "P1"
        assert "DONE-FAIL-IGNORED" in cfg.supervisor_log.read_text()
    finally:
        sup.log.close()


def test_resume_redrives_an_alive_master(monkeypatch, tmp_path):
    cfg = _bare_cfg(monkeypatch, tmp_path)
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)
    try:
        injected: list[str] = []
        sup.master.is_alive = lambda: True
        sup.master.inject = lambda text: injected.append(text)
        sup._on_resume()
        assert injected
        spawned: list[str] = []
        sup.master.is_alive = lambda: False
        sup._spawn_master = lambda kind: spawned.append(kind)
        sup._on_resume()
        assert spawned == ["step"]
    finally:
        sup.log.close()


def test_duplicate_done_does_not_overwrite_settled_status(monkeypatch, tmp_path):
    cfg = _bare_cfg(monkeypatch, tmp_path)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.done = {"P1": "ok"}
    sup = Supervisor(cfg)
    try:
        sup._advance_done("P1", "fail")
        assert state_mod.read(cfg).done["P1"] == "ok"
        assert "DONE-DUPLICATE" in cfg.supervisor_log.read_text()
    finally:
        sup.log.close()
