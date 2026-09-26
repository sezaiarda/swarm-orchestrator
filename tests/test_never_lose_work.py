"""The swarm never destroys work: real git, no tmux, no claude.

Every path that removes a phase's worktree or branch keeps what is not on main
(uncommitted edits become a commit, the tip goes under ``refs/swarm-attic``); an
interrupted phase is resumed on its own branch; the merge never switches the
owner's checkout to another branch.
"""

from __future__ import annotations

import shutil

import pytest

from swarm_orchestrator import gitq
from swarm_orchestrator.logutil import Log

from test_multirepo import _cfg as _mr_cfg
from test_multirepo import _make_workspace
from test_worktree import _cfg, _git, _make_project, _out, _tree, _worker

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@pytest.fixture
def repo(monkeypatch, tmp_path):
    project, _origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    yield project, cfg, log
    log.close()


def _attic(project, phase):
    return [ref for ref, _ in gitq.attic_refs(project) if f"/{phase}/" in ref]


def test_discard_keeps_commits_and_edits_in_the_attic(repo):
    project, cfg, log = repo
    wt = _worker(cfg, "P1", {"done.txt": "committed"}, log)
    (wt / "draft.txt").write_text("uncommitted")
    gitq.discard(cfg, "P1", log)
    assert _out(project, "branch", "--list", "swarm/P1").strip() == ""
    assert not (cfg.wt_dir / "P1").exists()
    [ref] = _attic(project, "P1")
    assert {"done.txt", "draft.txt"} <= set(_tree(project, ref).split())
    assert "ATTIC P1" in cfg.supervisor_log.read_text()


def test_discard_of_an_empty_branch_leaves_no_attic(repo):
    project, cfg, log = repo
    gitq.worktree_add(cfg, "P1", log)
    gitq.discard(cfg, "P1", log)
    assert _attic(project, "P1") == []


def test_nothing_is_removed_when_the_work_cannot_be_saved(repo, monkeypatch):
    project, cfg, log = repo
    wt = _worker(cfg, "P1", {"a.txt": "A"}, log)
    (wt / "draft.txt").write_text("uncommitted")
    monkeypatch.setattr(gitq, "_save_wip", lambda *a, **k: False)
    gitq.discard(cfg, "P1", log)
    assert (wt / "draft.txt").read_text() == "uncommitted"
    assert _out(project, "branch", "--list", "swarm/P1").strip() != ""
    assert "WORKTREE-GC-KEPT P1" in cfg.supervisor_log.read_text()


def test_launch_resumes_a_branch_whose_worktree_is_gone(repo):
    project, cfg, log = repo
    wt = _worker(cfg, "P1", {"a.txt": "A"}, log)
    _git(project, "worktree", "remove", "--force", str(wt))
    again = gitq.worktree_add(cfg, "P1", log)
    assert (again / "a.txt").read_text() == "A"
    assert "WORKTREE-RESUME P1" in cfg.supervisor_log.read_text()


def test_an_empty_leftover_starts_from_the_current_main(repo):
    project, cfg, log = repo
    gitq.worktree_add(cfg, "P1", log)  # an attempt that made nothing
    (project / "new.txt").write_text("main moved")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "main moves on")
    again = gitq.worktree_add(cfg, "P1", log)
    assert (again / "new.txt").read_text() == "main moved"


def test_worktree_add_failure_keeps_an_earlier_attempts_work(repo, monkeypatch):
    project, cfg, log = repo
    _worker(cfg, "P1", {"a.txt": "A"}, log)
    monkeypatch.setattr(gitq, "_link_target_cache", _raise_os)
    with pytest.raises(gitq.GitError):
        gitq.worktree_add(cfg, "P1", log)
    assert "a.txt" in _tree(project, "swarm/P1")


def _raise_os(*_a, **_k):
    raise OSError("disk full")


def test_integration_never_switches_the_owners_branch(repo):
    project, cfg, log = repo
    _worker(cfg, "P1", {"a.txt": "A"}, log)
    _git(project, "checkout", "-b", "owner-work")
    assert gitq.integrate(cfg, "P1", log) == gitq.DIRTY
    assert _out(project, "rev-parse", "--abbrev-ref", "HEAD").strip() == "owner-work"
    assert gitq.blocked_repo(cfg, "P1") == project
    reason = gitq.off_main_reason(cfg, project, "P1")
    assert "owner-work" in reason and "will not switch" in reason
    assert not gitq.resolve_ready(cfg, project)
    _git(project, "checkout", "master")
    assert gitq.resolve_ready(cfg, project)
    assert gitq.integrate(cfg, "P1", log) == gitq.MERGED
    assert "a.txt" in _tree(project)


def test_an_untracked_file_in_the_way_holds_as_dirty_not_conflict(repo):
    project, cfg, log = repo
    _worker(cfg, "P1", {"a.txt": "A"}, log)
    (project / "a.txt").write_text("owner's own, untracked")
    assert gitq.integrate(cfg, "P1", log) == gitq.DIRTY
    assert (project / "a.txt").read_text() == "owner's own, untracked"
    assert not gitq._merge_in_progress(project)


@pytest.mark.parametrize("name", ["..", ".", "a/../..", ""])
def test_discard_refuses_a_name_outside_the_worktree_dir(repo, name):
    _project, cfg, log = repo
    cfg.wt_dir.mkdir(parents=True, exist_ok=True)
    keep = cfg.state_dir / "keep.txt"
    keep.write_text("state")
    gitq.discard(cfg, name, log)
    assert keep.exists() and cfg.wt_dir.exists()


def test_attic_refs_read_their_age_from_the_name(repo):
    project, _cfg_, _log = repo
    head = _out(project, "rev-parse", "HEAD").strip()
    _git(project, "update-ref", "refs/swarm-attic/P1/20260101T000000Z", head)
    _git(project, "update-ref", "refs/swarm-attic/P1/20260101T000000Z-2", head)
    _git(project, "update-ref", "refs/swarm-attic/P1/garbage", head)
    refs = dict(gitq.attic_refs(project))
    assert refs == {
        "refs/swarm-attic/P1/20260101T000000Z": 1767225600.0,
        "refs/swarm-attic/P1/20260101T000000Z-2": 1767225600.0,
    }


def test_set_aside_saves_each_repo_but_never_adds_a_nested_repo(monkeypatch, tmp_path):
    """The umbrella's WIP commit must not swallow a component worktree as an
    embedded repo, even when the umbrella does not gitignore it."""
    project, _repos = _make_workspace(tmp_path)
    cfg = _mr_cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        wt = gitq.worktree_add(cfg, "P1", log)
        (wt / ".gitignore").write_text("")  # the components now show as untracked
        (wt / "umbrella.txt").write_text("u")
        (wt / "pricing" / "code.txt").write_text("changed\n")
        assert gitq.set_aside(cfg, "P1", log)
        names = _tree(project, "swarm/P1").split()
        assert "umbrella.txt" in names
        assert not any(n.startswith("pricing") for n in names)
        pricing = project / "pricing"
        assert _out(pricing, "show", "swarm/P1:code.txt") == "changed\n"
    finally:
        log.close()


def test_down_then_up_resumes_every_repo_of_an_interrupted_phase(monkeypatch, tmp_path):
    """The hard-cap path: workers killed mid-phase, then `swarm up`. Nothing
    they made, committed or not, in any repo, may be lost, and the relaunch
    starts where they stopped."""
    project, repos = _make_workspace(tmp_path)
    cfg = _mr_cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        wt = gitq.worktree_add(cfg, "P1", log)
        (wt / "pricing" / "code.txt").write_text("committed\n")
        _git(wt / "pricing", "commit", "-am", "pricing half")
        (wt / "webhooks" / "new.txt").write_text("uncommitted\n")
        (wt / "notes.txt").write_text("umbrella draft\n")

        assert gitq.reconcile(cfg, {}, log).integrated == []  # no sentinel: interrupted

        again = gitq.worktree_add(cfg, "P1", log)
        assert (again / "pricing" / "code.txt").read_text() == "committed\n"
        assert (again / "webhooks" / "new.txt").read_text() == "uncommitted\n"
        assert (again / "notes.txt").read_text() == "umbrella draft\n"
        assert _out(repos["webhooks"], "show", "swarm/P1:new.txt") == "uncommitted\n"
    finally:
        log.close()
