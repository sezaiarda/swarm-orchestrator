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


def test_resume_launches_the_ready_set_without_a_master(monkeypatch, tmp_path):
    """`resume` used to spawn a step master (or nudge a live one) to do the
    launching. It launches the ready set itself now, and never touches a master
    — live or not."""
    cfg = _bare_cfg(monkeypatch, tmp_path)
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(
        "- [ ] `A1` · needs:—\n- [ ] `B1` · needs:—\n", encoding="utf-8"
    )
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)
    try:
        touched: list[str] = []
        sup.master.is_alive = lambda: True
        sup.master.inject = lambda text: touched.append(f"inject {text}")
        sup._spawn_master = lambda kind: touched.append(f"spawn {kind}") or True
        sup._on_resume()
        assert sup.stub_launches == ["A1", "B1"]
        assert touched == []
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


# -- worktree_add builds the components in parallel, all or nothing ----------
def test_worktree_add_builds_components_concurrently(monkeypatch, tmp_path):
    """Many repos added one after another made every launch slow. The components go
    through a bounded pool now; the umbrella still goes first (they nest in it)."""
    import threading
    import time

    project, _ = _make_workspace(tmp_path, siblings=("a1", "b2", "c3", "d4"))
    cfg = _cfg(monkeypatch, tmp_path, project)
    real = gitq._mirror_base
    lock = threading.Lock()
    live = {"now": 0, "peak": 0}
    order: list[str] = []

    def slow_base(repo, main):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
            order.append(repo.name)
        time.sleep(0.2)
        with lock:
            live["now"] -= 1
        return real(repo, main)

    monkeypatch.setattr(gitq, "_mirror_base", slow_base)
    log = Log(cfg.supervisor_log)
    try:
        wt = gitq.worktree_add(cfg, "P1", log)
    finally:
        log.close()
    assert order[0] == project.name  # umbrella first, alone
    assert live["peak"] > 1  # components overlapped
    for s in ("a1", "b2", "c3", "d4"):
        assert _out(wt / s, "rev-parse", "--abbrev-ref", "HEAD").strip() == "swarm/P1"


def test_a_failed_component_discards_the_whole_partial_mirror(monkeypatch, tmp_path):
    project, repos = _make_workspace(tmp_path, siblings=("a1", "b2", "c3"))
    cfg = _cfg(monkeypatch, tmp_path, project)
    real = gitq._add_one

    def flaky(c, repo, main, phase, log):
        if repo.name == "b2":
            raise gitq.GitError("git worktree add @ b2: disk full")
        return real(c, repo, main, phase, log)

    monkeypatch.setattr(gitq, "_add_one", flaky)
    log = Log(cfg.supervisor_log)
    try:
        with pytest.raises(gitq.GitError, match="disk full"):
            gitq.worktree_add(cfg, "P1", log)
    finally:
        log.close()
    assert not (cfg.wt_dir / "P1").exists()  # no half mirror left behind
    for r in (project, *repos.values()):
        assert _out(r, "branch", "--list", "swarm/*").strip() == ""
    assert "WORKTREE-ADD-FAIL P1" in cfg.supervisor_log.read_text()


# -- a CHANGELOG / lessons journal in ANY repo is union-merged ------------------
def test_changelog_union_resolves_in_a_component_repo(monkeypatch, tmp_path):
    """`[git].auto_resolve` keys are matched against the path *inside the repo
    being merged*, so one bare `CHANGELOG.md` key covers every repo's changelog —
    no per-repo entry, no glob needed. Two phases each prepending an entry is the
    conflict union exists for: both land, neither is duplicated."""
    project, repos = _make_workspace(tmp_path)
    v = repos["pricing"]
    (v / "CHANGELOG.md").write_text("# Changelog\n\n## 0.1.0\n- first\n")
    (v / "tasks").mkdir()
    (v / "tasks" / "lessons.md").write_text("# Lessons\n")
    _git(v, "add", "-A")
    _git(v, "commit", "-m", "journals")
    cfg = _cfg(monkeypatch, tmp_path, project)
    cfg.git_auto_resolve = {"CHANGELOG.md": "union", "tasks/lessons.md": "union"}
    log = Log(cfg.supervisor_log)
    try:
        wts = {p: gitq.worktree_add(cfg, p, log) for p in ("P1", "P2")}
        for p, wt in wts.items():
            cl = wt / "pricing" / "CHANGELOG.md"
            cl.write_text(cl.read_text().replace("\n\n## 0.1.0", f"\n\n## {p}\n- {p} entry\n\n## 0.1.0"))
            ls = wt / "pricing" / "tasks" / "lessons.md"
            ls.write_text(ls.read_text() + f"- {p} lesson\n")
            _git(wt / "pricing", "add", "-A")
            _git(wt / "pricing", "commit", "-m", p)
        assert gitq.integrate(cfg, "P1", log) == gitq.MERGED
        assert gitq.integrate(cfg, "P2", log) == gitq.MERGED  # conflicted, auto-resolved
    finally:
        log.close()
    _git(v, "checkout", "master")
    text = (v / "CHANGELOG.md").read_text()
    assert text.count("## P1") == 1 and text.count("## P2") == 1
    assert text.count("## 0.1.0") == 1
    lessons = (v / "tasks" / "lessons.md").read_text()
    assert "- P1 lesson" in lessons and "- P2 lesson" in lessons
    assert "AUTORESOLVE" in cfg.supervisor_log.read_text()


def test_a_failed_auto_resolve_check_leaves_a_real_conflict(monkeypatch, tmp_path):
    """The check fails after the journal merged cleanly in memory: the repo must
    be exactly mid-merge again, markers on disk and the path still unmerged, so
    the resolver takes it as if no automatic merge had been tried."""
    project, repos = _make_workspace(tmp_path)
    v = repos["pricing"]
    (v / "CHANGELOG.md").write_text("# Changelog\n\n## 0.1.0\n- first\n")
    _git(v, "add", "-A")
    _git(v, "commit", "-m", "journal")
    cfg = _cfg(monkeypatch, tmp_path, project)
    cfg.git_auto_resolve = {"CHANGELOG.md": "union"}
    cfg.git_auto_resolve_check = {"CHANGELOG.md": "false"}
    log = Log(cfg.supervisor_log)
    try:
        wts = {p: gitq.worktree_add(cfg, p, log) for p in ("P1", "P2")}
        for p, wt in wts.items():
            cl = wt / "pricing" / "CHANGELOG.md"
            cl.write_text(cl.read_text().replace("\n\n## 0.1.0", f"\n\n## {p}\n\n## 0.1.0"))
            _git(wt / "pricing", "add", "-A")
            _git(wt / "pricing", "commit", "-m", p)
        assert gitq.integrate(cfg, "P1", log) == gitq.MERGED
        assert gitq.integrate(cfg, "P2", log) == gitq.CONFLICT
    finally:
        log.close()
    assert gitq.blocked_repo(cfg, "P2") == v
    assert _out(v, "diff", "--name-only", "--diff-filter=U").split() == ["CHANGELOG.md"]
    assert "<<<<<<<" in (v / "CHANGELOG.md").read_text()
    assert "AUTORESOLVE-CHECK-FAILED P2 pricing" in cfg.supervisor_log.read_text()
