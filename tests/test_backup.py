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


# -- an umbrella that gitignores its component repos ---------------------------
def _ignore(project: Path, *patterns: str) -> None:
    """Commit ignore rules to the umbrella's main, the way a project that keeps
    its component repos out of its own tree does."""
    (project / ".gitignore").write_text("".join(f"{p}\n" for p in patterns))
    _git(project, "add", ".gitignore")
    _git(project, "commit", "-q", "-m", "ignore the component repos")
    _git(project, "push", "-q", "origin", "master")


def _index(wt: Path) -> Path:
    return Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-path", "index"))


def test_an_umbrella_that_gitignores_its_component_repo_is_still_snapshotted(env):
    """git refuses a pathspec that names an ignored path, inside ``:(exclude)``
    too, so the snapshot of exactly the repo that has nested repos was abandoned."""
    cfg, log, _ = env
    _ignore(cfg.project_dir, "/comp/")
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "notes.md").write_text("uncommitted, untracked\n")
    (wt / "PHASE-LEDGER.md").write_text("P1\nP2\nP3\n")  # uncommitted, tracked
    (wt / "comp" / "lib.rs").write_text("the component's own edit\n")
    head, status, index = _git(wt, "rev-parse", "HEAD"), _git(wt, "status", "--porcelain"), \
        _index(wt).read_bytes()

    snap = backup.snapshot(cfg, cfg.project_dir, "P1", log)

    assert "BACKUP-SNAPSHOT-FAILED" not in cfg.supervisor_log.read_text()
    assert snap is not None
    assert _git(wt, "rev-parse", f"{snap}^") == head
    names = _git(wt, "ls-tree", "-r", "--name-only", snap).split()
    assert "notes.md" in names and not [n for n in names if n.split("/")[0] == "comp"]
    assert "P3" in _git(wt, "show", f"{snap}:PHASE-LEDGER.md")
    # The worker's own branch, files and index are exactly as they were.
    assert _git(wt, "symbolic-ref", "HEAD") == "refs/heads/swarm/P1"
    assert _git(wt, "rev-parse", "HEAD") == head
    assert _git(wt, "status", "--porcelain") == status and _index(wt).read_bytes() == index


def test_a_pass_over_an_ignoring_umbrella_puts_its_snapshot_on_the_origin(env):
    cfg, log, tmp = env
    _ignore(cfg.project_dir, "/comp/")
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "notes.md").write_text("umbrella edit\n")
    (wt / "comp" / "lib.rs").write_text("component edit\n")

    res = backup.run(cfg, log)

    assert not res.failed and not res.snapshots, res
    assert sorted(res.pushed) == ["comp:swarm-wip/P1", "project:swarm-wip/P1"]
    umb = _remote(tmp / "umbrella.git")["swarm-wip/P1"]
    assert _git(wt, "show", f"{umb}:notes.md") == "umbrella edit"
    assert "comp" not in _git(wt, "ls-tree", "--name-only", umb).split()
    # The component is a separate repo with its own backup.
    comp = _remote(tmp / "comp.git")["swarm-wip/P1"]
    assert _git(wt / "comp", "show", f"{comp}:lib.rs") == "component edit"
    assert "FAILED" not in cfg.supervisor_log.read_text()


def test_a_component_under_an_ignored_folder_beside_one_that_is_not_ignored(env):
    """An ignored parent trips git the same way; a component no rule covers still
    needs its exclude, or it is recorded as an embedded repository."""
    cfg, log, _ = env
    _repo(cfg.project_dir / "libs" / "deep", None, {"x.rs": "x\n"})
    cfg.git_repos = ["comp", "libs/*"]
    _ignore(cfg.project_dir, "/libs/")
    wt = gitq.worktree_add(cfg, "P1", log)
    assert (wt / "libs" / "deep" / ".git").exists() and (wt / "comp" / ".git").exists()
    (wt / "notes.md").write_text("umbrella edit\n")

    snap = backup.snapshot(cfg, cfg.project_dir, "P1", log)

    assert "BACKUP-SNAPSHOT-FAILED" not in cfg.supervisor_log.read_text()
    assert snap is not None
    assert _git(wt, "ls-tree", "--name-only", snap).split() == [
        ".gitignore", "PHASE-LEDGER.md", "notes.md"]


# -- a snapshot that fails must not look like a backup -------------------------
def _refuse(monkeypatch, wt: Path, verb: str, why: str = "fatal: git would not"):
    """Make ``git <verb>`` fail in the umbrella mirror ``wt``; returns the undo."""
    real = backup._git

    def refusing(cwd, *args, **kw):
        if args[0] == verb and Path(cwd) == wt:
            return subprocess.CompletedProcess(args, 1, "", f"{why}\nhint: a second line\n")
        return real(cwd, *args, **kw)

    monkeypatch.setattr(backup, "_git", refusing)
    return lambda: monkeypatch.setattr(backup, "_git", real)


@pytest.mark.parametrize("verb", ["status", "add", "write-tree", "commit-tree"])
def test_a_snapshot_that_cannot_be_taken_is_counted_and_named(env, monkeypatch, verb):
    cfg, log, tmp = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "notes.md").write_text("on this disk only\n")
    (wt / "comp" / "lib.rs").write_text("saved\n")
    _refuse(monkeypatch, wt, verb)

    res = backup.run(cfg, log)

    assert list(res.snapshots) == ["project:swarm-wip/P1"]
    assert res.failed == ["project:swarm-wip/P1 (no snapshot)"]
    assert res.pushed == ["comp:swarm-wip/P1"]
    assert res.line() == "1 pushed, 0 deleted, 1 failed (1 snapshot)"
    assert "swarm-wip/P1" not in _remote(tmp / "umbrella.git")
    lines = [ln.split(" ", 3)[3] for ln in cfg.supervisor_log.read_text().splitlines()]
    assert f"BACKUP-SNAPSHOT-FAILED P1 project: {verb}: fatal: git would not hint: a second line" \
        in lines  # one line, whatever git printed
    assert ("BACKUP 1 pushed, 0 deleted, 1 failed (1 snapshot)"
            " failed=project:swarm-wip/P1 (no snapshot)") in lines


def test_a_pass_with_nothing_but_a_failed_snapshot_still_writes_its_summary(env, monkeypatch):
    cfg, log, _ = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "notes.md").write_text("on this disk only\n")
    _refuse(monkeypatch, wt, "add")
    assert backup.run(cfg, log).line() == "0 pushed, 0 deleted, 1 failed (1 snapshot)"
    assert " BACKUP 0 pushed, 0 deleted, 1 failed (1 snapshot) " in cfg.supervisor_log.read_text()


def test_swarm_down_says_which_work_it_could_not_snapshot(env, monkeypatch, capsys):
    from swarm_orchestrator import cli

    cfg, log, _ = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "notes.md").write_text("on this disk only\n")
    _refuse(monkeypatch, wt, "add")
    cfg.backup_on_down = True
    cli.cmd_down(cfg)
    said = capsys.readouterr()
    assert "backup to origin: 0 pushed, 0 deleted, 1 failed (1 snapshot)" in said.out
    assert "backup failed: project:swarm-wip/P1 (no snapshot)" in said.err


def test_doctor_reports_unsaved_work_until_a_pass_saves_it(env, monkeypatch):
    from swarm_orchestrator import doctor

    cfg, log, tmp = env
    assert doctor._check_backup(cfg) == doctor.Check("backup", doctor.OK, "no pass recorded yet")
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "notes.md").write_text("on this disk only\n")
    undo = _refuse(monkeypatch, wt, "add", "The following paths are ignored")

    backup.run(cfg, log)
    first = doctor._check_backup(cfg)
    assert first.name == "backup" and first.status == doctor.WARN  # one bad pass can be a blip
    assert "1 snapshot failed" in first.detail and "project:swarm-wip/P1" in first.detail
    assert "add: The following paths are ignored" in first.detail
    assert "BACKUP-SNAPSHOT-FAILED" in first.fix_hint

    backup.run(cfg, log)
    second = doctor._check_backup(cfg)
    assert second.status == doctor.FAIL and "2 passes running" in second.detail

    undo()
    backup.run(cfg, log)
    healed = doctor._check_backup(cfg)
    assert healed.status == doctor.OK and "1 pushed, 0 deleted" in healed.detail
    assert "swarm-wip/P1" in _remote(tmp / "umbrella.git")


def test_doctor_warns_about_a_ref_the_last_pass_could_not_push(env):
    from swarm_orchestrator import doctor

    cfg, log, tmp = env
    wt = gitq.worktree_add(cfg, "P1", log)
    (wt / "comp" / "lib.rs").write_text("edit\n")
    shutil.rmtree(tmp / "comp.git")
    backup.run(cfg, log)
    check = doctor._check_backup(cfg)
    assert check.status == doctor.WARN and "comp: origin unreachable" in check.detail


def test_doctor_says_the_backup_is_off_without_worktree_isolation(env, monkeypatch):
    from swarm_orchestrator import doctor

    cfg, _, _ = env
    monkeypatch.setattr(cfg, "git_isolation", "none")
    check = doctor._check_backup(cfg)
    assert check.status == doctor.OK and check.detail.startswith("off")
