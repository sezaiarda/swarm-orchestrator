"""The ``swarm build`` concurrency gate + the shared cargo ``target`` cache.

No tmux, no ``claude``. Proves: the semaphore caps concurrency at ``max_concurrent``
and releases on close, ``swarm build`` passes the child exit code through and only
sets ``CARGO_BUILD_JOBS`` for ``cargo``, and worktree creation symlinks a Rust
repo's ``target`` to one shared per-repo cache (but leaves non-Rust repos alone).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from swarm_orchestrator import buildsem, gitq, launch
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log


def _cfg(tmp_path: Path, monkeypatch, **overrides):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    for key, val in overrides.items():
        monkeypatch.setenv(key, str(val))
    return load(project_dir=str(tmp_path))


# -- the semaphore --------------------------------------------------------
def test_semaphore_caps_and_releases(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SWARM_BUILD_MAX=2)
    a = buildsem._try_once(cfg)
    b = buildsem._try_once(cfg)
    assert a is not None and b is not None  # both slots free
    assert buildsem._try_once(cfg) is None  # capped: no third slot
    os.close(a)  # a build dies -> its flock releases
    c = buildsem._try_once(cfg)
    assert c is not None  # the freed slot is reusable
    os.close(b)
    os.close(c)


def test_env_override_beats_config(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SWARM_BUILD_MAX=3, SWARM_BUILD_JOBS=4, SWARM_BUILD_CACHE=0)
    assert (cfg.build_max_concurrent, cfg.build_jobs, cfg.build_cache) == (3, 4, False)


def test_int_env_degrades_instead_of_crashing(monkeypatch):
    from swarm_orchestrator import config as c
    monkeypatch.delenv("X", raising=False)
    assert c._int_env("X", None, 2, minimum=0) == 2   # absent -> default
    assert c._int_env("X", "abc", 2, minimum=0) == 2  # wrong-type config -> default
    assert c._int_env("X", [1, 2], 2, minimum=0) == 2  # wrong-type config -> default
    assert c._int_env("X", 3, 2, minimum=0) == 3      # good config value used
    assert c._int_env("X", -5, 2, minimum=0) == 0     # floored at minimum
    monkeypatch.setenv("X", "7")
    assert c._int_env("X", 3, 2, minimum=0) == 7      # env override wins
    monkeypatch.setenv("X", "junk")
    assert c._int_env("X", 3, 2, minimum=0) == 3      # bad env -> config value


# -- the `swarm build` CLI ------------------------------------------------
def _build(tmp_path, *argv, extra_env=None):
    env = {**os.environ, "SWARM_STATE_DIR": str(tmp_path / "state")}
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "swarm_orchestrator", "build", *argv],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=30,
    )


def test_exit_code_passthrough(tmp_path):
    assert _build(tmp_path, "/bin/true").returncode == 0
    assert _build(tmp_path, "/bin/false").returncode == 1


def test_no_command_is_usage_error(tmp_path):
    r = _build(tmp_path)
    assert r.returncode == 2 and "no command" in r.stderr


def test_bad_exec_reports_127(tmp_path):
    r = _build(tmp_path, "/nonexistent/xyz")
    assert r.returncode == 127 and "cannot run" in r.stderr


def test_cargo_build_jobs_only_for_cargo(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "cargo").write_text('#!/bin/sh\necho "jobs=$CARGO_BUILD_JOBS"\n')
    (fake / "cargo").chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}", "SWARM_BUILD_JOBS": "5"}
    cargo = _build(tmp_path, "cargo", "build", extra_env=env)
    assert "jobs=5" in cargo.stdout  # cargo gets the cap
    other = _build(tmp_path, "sh", "-c", 'echo "jobs=[$CARGO_BUILD_JOBS]"', extra_env=env)
    assert "jobs=[]" in other.stdout  # a non-cargo command does not


def test_gate_disabled_runs_through(tmp_path):
    r = _build(tmp_path, "/bin/echo", "hi", extra_env={"SWARM_BUILD_MAX": "0"})
    assert r.returncode == 0 and "hi" in r.stdout


# -- worker env carries the gate config only in worktree mode -------------
def test_worker_env_build_vars_worktree_only(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    plain = launch._worker_env(cfg, "P1", worktree=None)
    assert "CARGO_BUILD_JOBS" not in plain and "SWARM_BUILD_MAX" not in plain
    wt = launch._worker_env(cfg, "P1", worktree=tmp_path / "wt")
    assert wt["CARGO_BUILD_JOBS"] == str(cfg.build_jobs)
    assert wt["SWARM_BUILD_MAX"] == str(cfg.build_max_concurrent)


def test_every_session_builds_without_debuginfo_or_incremental(tmp_path, monkeypatch):
    """One cargo profile for every swarm build, so the shared target holds one
    generation of units; a value the owner exported still wins."""
    for key in launch.CARGO_ENV:
        monkeypatch.delenv(key, raising=False)
    cfg = _cfg(tmp_path, monkeypatch)
    env = launch._worker_env(cfg, "P1", worktree=tmp_path / "wt")
    assert {k: env[k] for k in launch.CARGO_ENV} == {
        "CARGO_INCREMENTAL": "0", "CARGO_PROFILE_DEV_DEBUG": "0", "CARGO_PROFILE_TEST_DEBUG": "0",
    }
    monkeypatch.setenv("CARGO_PROFILE_DEV_DEBUG", "line-tables-only")
    assert launch.session_env(cfg)["CARGO_PROFILE_DEV_DEBUG"] == "line-tables-only"


def test_the_lane_check_builds_with_the_workers_cargo_env(tmp_path, monkeypatch):
    from swarm_orchestrator import landing

    for key in launch.CARGO_ENV:
        monkeypatch.delenv(key, raising=False)
    out = tmp_path / "out.log"
    with out.open("w") as fh:
        assert landing._run('echo "debug=$CARGO_PROFILE_TEST_DEBUG inc=$CARGO_INCREMENTAL"',
                            tmp_path, fh, 30)
    assert "debug=0 inc=0" in out.read_text()


# -- the shared target cache symlink --------------------------------------
def _rust_wt(wt: Path, *, ignore_target: bool = True) -> None:
    """A worktree that looks like a Rust repo checkout (git tree + Cargo.toml)."""
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    (wt / "Cargo.toml").write_text("[package]\n")
    if ignore_target:
        (wt / ".gitignore").write_text("/target\n")


def test_target_cache_symlink_shared_per_repo(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)  # cache defaults on
    log = Log(tmp_path / "l.log")
    repo = tmp_path / "payments"
    repo.mkdir()
    # two different worktrees of the same repo -> same shared cache
    for phase in ("payments-P4", "payments-P5"):
        wt = tmp_path / "wt" / phase
        _rust_wt(wt)
        gitq._link_target_cache(cfg, wt, repo, log)
        link = wt / "target"
        assert link.is_symlink()
        assert link.resolve() == (cfg.build_cache_dir / "payments").resolve()
    log.close()


def test_target_cache_recreates_dangling_cache_link(tmp_path, monkeypatch):
    # The cache entry points at the main checkout's target/, which a cargo clean
    # or the disk guard removed: recreate it rather than fall back to a cold build.
    cfg = _cfg(tmp_path, monkeypatch)
    log = Log(tmp_path / "l.log")
    main_target = tmp_path / "payments" / "target"
    cfg.build_cache_dir.mkdir(parents=True, exist_ok=True)
    (cfg.build_cache_dir / "payments").symlink_to(main_target)
    wt = tmp_path / "wt" / "payments-P4"
    _rust_wt(wt)
    gitq._link_target_cache(cfg, wt, tmp_path / "payments", log)
    assert (wt / "target").is_symlink()
    assert main_target.is_dir()
    assert "TARGET-CACHE-SKIP" not in (tmp_path / "l.log").read_text()
    log.close()


def test_no_symlink_when_target_not_ignored(tmp_path, monkeypatch):
    # A Rust worktree that does NOT gitignore target -> skip, so the machine-local
    # symlink can never be `git add -A`'d and merged into canonical main.
    cfg = _cfg(tmp_path, monkeypatch)
    log = Log(tmp_path / "l.log")
    wt = tmp_path / "wt" / "payments-P4"
    _rust_wt(wt, ignore_target=False)
    gitq._link_target_cache(cfg, wt, tmp_path / "payments", log)
    assert not (wt / "target").is_symlink()
    assert "target-not-ignored" in (tmp_path / "l.log").read_text()
    log.close()


def test_no_symlink_for_non_rust_repo(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    log = Log(tmp_path / "l.log")
    wt = tmp_path / "wt" / "frontend-P1"
    wt.mkdir(parents=True)  # no Cargo.toml -> a TS repo
    gitq._link_target_cache(cfg, wt, tmp_path / "frontend", log)
    assert not (wt / "target").exists()
    log.close()


def test_cache_disabled_makes_no_symlink(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, SWARM_BUILD_CACHE=0)
    log = Log(tmp_path / "l.log")
    wt = tmp_path / "wt" / "payments-P4"
    _rust_wt(wt)
    gitq._link_target_cache(cfg, wt, tmp_path / "payments", log)
    assert not (wt / "target").is_symlink()
    log.close()
