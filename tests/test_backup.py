"""Backup pushes of unmerged work (real git; local bare repos stand in for GitHub)."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from swarm_orchestrator import backup, gitq
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path, origin: Path | None, files: dict[str, str]) -> None:
    subprocess.run(["git", "init", "-q", "-b", "master", str(path)], check=True)
    _git(path, "config", "user.email", "swarm@test")
    _git(path, "config", "user.name", "swarm")
    _git(path, "config", "commit.gpgsign", "false")
    for rel, text in files.items():
        (path / rel).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")
    if origin is not None:
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True)
        _git(path, "remote", "add", "origin", str(origin))
        _git(path, "push", "-q", "-u", "origin", "master")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An umbrella with one component repo nested inside it, each with an origin."""
    project = tmp_path / "project"
    _repo(project, tmp_path / "umbrella.git", {"PHASE-LEDGER.md": "P1\nP2\n"})
    _repo(project / "comp", tmp_path / "comp.git", {"lib.rs": "fn a() {}\n"})
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_GIT_REPOS", "comp")
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    cfg = load(project_dir=str(project))
    log = Log(cfg.supervisor_log)
    yield cfg, log, tmp_path
    log.close()


def _remote(origin: Path) -> dict[str, str]:
    out = _git(origin, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
    return dict(ln.split(" ") for ln in out.splitlines() if not ln.startswith("master "))


def test_a_phase_with_commits_and_edits_is_backed_up_without_touching_its_worktree(env):
    cfg, log, tmp = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "comp" / "lib.rs").write_text("fn a() {}\nfn b() {}\n")
    _git(wt / "comp", "commit", "-q", "-am", "b")
    (wt / "comp" / "lib.rs").write_text("fn a() {}\nfn b() {}\nfn c() {}\n")  # uncommitted
    (wt / "comp" / "new.rs").write_text("// untracked\n")
    (wt / "notes.md").write_text("umbrella edit\n")
    before = {p: (_git(p, "status", "--porcelain"),
                  Path(_git(p, "rev-parse", "--path-format=absolute", "--git-path", "index")).read_bytes())
              for p in (wt, wt / "comp")}

    res = backup.run(cfg, log)

    assert not res.failed, res
    comp = _remote(tmp / "comp.git")
    head = _git(wt / "comp", "rev-parse", "HEAD")
    assert comp["swarm/P1"] == head
    snap = comp["swarm-wip/P1"]
    assert _git(wt / "comp", "rev-parse", f"{snap}^") == head
    assert "fn c()" in _git(wt / "comp", "show", f"{snap}:lib.rs")
    assert _git(wt / "comp", "show", f"{snap}:new.rs") == "// untracked"
    # The umbrella: no commits of its own, so only the snapshot, and the nested
    # component is left out of it rather than recorded as an embedded repo.
    umb = _remote(tmp / "umbrella.git")
    assert "swarm/P1" not in umb
    tree = _git(wt, "ls-tree", "--name-only", umb["swarm-wip/P1"])
    assert "notes.md" in tree.split() and "comp" not in tree.split()
    for p, (status, index) in before.items():
        assert _git(p, "status", "--porcelain") == status
        path = Path(_git(p, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        assert path.read_bytes() == index


def test_a_second_pass_pushes_only_what_moved_and_force_updates_a_rewrite(env):
    cfg, log, tmp = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "comp" / "lib.rs").write_text("one\n")
    _git(wt / "comp", "commit", "-q", "-am", "one")
    assert backup.run(cfg, log).pushed == ["comp:swarm/P1"]
    assert backup.run(cfg, log).pushed == []  # nothing moved
    (wt / "comp" / "lib.rs").write_text("two\n")
    _git(wt / "comp", "commit", "-q", "--amend", "-am", "two")  # history rewritten
    res = backup.run(cfg, log)
    assert res.pushed == ["comp:swarm/P1"] and not res.failed
    assert _remote(tmp / "comp.git")["swarm/P1"] == _git(wt / "comp", "rev-parse", "HEAD")


def test_the_lease_refuses_to_clobber_a_backup_someone_else_moved(env, monkeypatch):
    cfg, log, tmp = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "comp" / "lib.rs").write_text("one\n")
    _git(wt / "comp", "commit", "-q", "-am", "one")
    real = backup._remote_refs

    def stale(repo):  # what ls-remote said a moment before another push landed
        found = real(repo)
        if found is not None and repo.name == "comp":
            _git(repo, "push", "-q", "origin", "master:refs/heads/swarm/P1")
        return found

    monkeypatch.setattr(backup, "_remote_refs", stale)
    res = backup.run(cfg, log)
    assert res.failed == ["comp:swarm/P1"]
    assert _remote(tmp / "comp.git")["swarm/P1"] == _git(wt / "comp", "rev-parse", "master")


def test_a_merged_phase_loses_its_remote_backups(env):
    cfg, log, tmp = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "comp" / "lib.rs").write_text("merged\n")
    _git(wt / "comp", "commit", "-q", "-am", "work")
    (wt / "comp" / "extra.rs").write_text("uncommitted\n")
    backup.run(cfg, log)
    assert set(_remote(tmp / "comp.git")) == {"swarm/P1", "swarm-wip/P1"}
    _git(wt / "comp", "add", "-A")
    _git(wt / "comp", "commit", "-q", "-m", "rest")
    assert gitq.integrate(cfg, "P1", log, {}) == gitq.MERGED

    res = backup.run(cfg, log)

    assert sorted(res.deleted) == ["comp:swarm-wip/P1", "comp:swarm/P1"]
    assert _remote(tmp / "comp.git") == {}


def test_a_discarded_phase_keeps_its_remote_backup(env):
    """Work that never reached main is exactly what the backup is for."""
    cfg, log, tmp = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "comp" / "lib.rs").write_text("lost locally\n")
    _git(wt / "comp", "commit", "-q", "-am", "work")
    backup.run(cfg, log)
    gitq.discard(cfg, "P1", log)
    res = backup.run(cfg, log)
    assert res.deleted == [] and "swarm/P1" in _remote(tmp / "comp.git")


def test_attic_refs_are_backed_up_as_branches(env):
    cfg, log, tmp = env
    comp = cfg.project_dir / "comp"
    (comp / "lib.rs").write_text("kept\n")
    _git(comp, "commit", "-q", "-am", "kept work")
    kept = _git(comp, "rev-parse", "HEAD")
    _git(comp, "reset", "-q", "--hard", "HEAD^")
    _git(comp, "update-ref", "refs/swarm-attic/P2/20260927T101500Z", kept)
    backup.run(cfg, log)
    assert _remote(tmp / "comp.git") == {"swarm-attic/P2-20260927T101500Z": kept}


def test_a_refusing_pre_push_hook_does_not_stop_a_backup(env):
    cfg, log, tmp = env
    wt = gitq.worktree_add(cfg, "P1", log)
    hook = cfg.project_dir / "comp" / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
    hook.chmod(0o755)
    (wt / "comp" / "lib.rs").write_text("x\n")
    _git(wt / "comp", "commit", "-q", "-am", "x")
    assert backup.run(cfg, log).pushed == ["comp:swarm/P1"]


def test_no_origin_means_no_push_and_a_dead_one_is_reported_not_raised(env):
    cfg, log, tmp = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "comp" / "lib.rs").write_text("x\n")
    _git(wt / "comp", "commit", "-q", "-am", "x")
    _git(cfg.project_dir / "comp", "remote", "remove", "origin")
    assert backup.run(cfg, log).failed == []
    _git(cfg.project_dir / "comp", "remote", "add", "origin", str(tmp / "gone.git"))
    res = backup.run(cfg, log)
    assert res.failed == ["comp: origin unreachable"]
    assert "BACKUP" in cfg.supervisor_log.read_text()


def test_no_worktree_isolation_means_no_backup(env, monkeypatch):
    cfg, log, _ = env
    monkeypatch.setattr(cfg, "git_isolation", "none")
    assert backup.run(cfg, log) == backup.Result()


def test_swarm_down_pushes_the_backup_after_the_sessions_end(env, monkeypatch, capsys):
    from swarm_orchestrator import cli

    cfg, log, tmp = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "comp" / "lib.rs").write_text("unfinished\n")  # never committed
    cfg.backup_on_down = True
    assert cli.cmd_down(cfg) == 0
    assert "backup to origin: 1 pushed" in capsys.readouterr().out
    assert set(_remote(tmp / "comp.git")) == {"swarm-wip/P1"}
    cfg.backup_on_down = False
    (wt / "comp" / "lib.rs").write_text("more\n")
    cli.cmd_down(cfg)
    assert "backup" not in capsys.readouterr().out


def test_the_supervisor_runs_a_pass_on_its_own_clock(env, monkeypatch):
    import time as time_mod

    from swarm_orchestrator.supervisor import Supervisor

    cfg, log, _ = env
    ran = []
    monkeypatch.setattr(backup, "run", lambda c, lg, budget_s=None: ran.append(c))
    cfg.backup_every_s = 600
    sup = Supervisor(cfg)
    sup._backup_tick()
    assert ran == []  # the first pass is a full interval after start-up
    assert 590 < sup._next_timeout() <= 600
    sup._backup_last = time_mod.time() - 601
    assert sup._next_timeout() == 0.0
    sup._backup_tick()
    sup._backup_thread.join(5)
    assert ran == [cfg]
    cfg.backup_every_s = 0
    sup._backup_last = 0.0
    sup._backup_tick()
    assert len(ran) == 1 and sup._next_timeout() is None


def test_gc_pruning_an_attic_ref_deletes_its_remote_backup(env):
    """A pass only deletes what reached main; set-aside work mostly never does."""
    from swarm_orchestrator import gc as gc_mod

    cfg, log, tmp = env
    comp = cfg.project_dir / "comp"
    head = _git(comp, "rev-parse", "HEAD")
    old = time.strftime(gitq.ATTIC_STAMP, time.gmtime(time.time() - 31 * 86400))
    new = time.strftime(gitq.ATTIC_STAMP, time.gmtime(time.time() - 29 * 86400))
    never = time.strftime(gitq.ATTIC_STAMP, time.gmtime(time.time() - 32 * 86400))
    _git(comp, "update-ref", f"refs/swarm-attic/P1/{old}", head)
    _git(comp, "update-ref", f"refs/swarm-attic/P1/{new}", head)
    backup.run(cfg, log)
    _git(comp, "update-ref", f"refs/swarm-attic/P2/{never}", head)  # never backed up
    gc_mod.apply(gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True)), log)
    assert list(_remote(tmp / "comp.git")) == [f"swarm-attic/P1-{new}"]
    text = cfg.supervisor_log.read_text()
    assert f"BACKUP-ATTIC-DELETED comp:swarm-attic/P1-{old}" in text
    assert "FAILED" not in text
