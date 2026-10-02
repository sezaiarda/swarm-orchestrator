"""The build paths a Rust test compiles in, in a target cache many mirrors share.

cargo hands a test target the path of its package's binaries, of a scratch dir
and of its sources at compile time, spelled through the directory it was started
in. With one ``target/`` shared by every mirror of a repo, the next mirror runs
a test that starts the binary through the mirror that built it, which works only
until that mirror is removed. Proves the two halves of the cure: the rustc
wrapper that makes the compiled-in path the cache's own
(``gitq._pin_test_paths``), and the sweep that deletes an executable built
through a directory that is gone (``gitq._drop_stale_executables``), first on
fakes, then with real git and real cargo in the order that fails: a mirror
exists, another one builds, the builder goes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from swarm_orchestrator import gitq
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
needs_cargo = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("cargo") is None,
    reason="git and cargo are both needed",
)


def _cfg(monkeypatch, tmp_path: Path, project: Path, **overrides):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_SLUG", "bakedtest")
    monkeypatch.delenv("SWARM_GIT_REPOS", raising=False)
    monkeypatch.delenv("SWARM_BUILD_CACHE", raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, str(val))
    return load(project_dir=str(project))


def _log_text(tmp_path: Path) -> str:
    return (tmp_path / "l.log").read_text()


def _pin(cfg, tmp_path: Path) -> tuple[Path, Path]:
    """Run ``_pin_test_paths``; return where the wrapper and the config go."""
    log = Log(tmp_path / "l.log")
    gitq._pin_test_paths(cfg, log)
    log.close()
    home = cfg.state_dir / ".cargo"
    return home / "rustc-wrap", home / "config.toml"


# -- the wrapper and its config -------------------------------------------
def _through_wrapper(wrapper: Path, env: dict[str, str], *argv: str):
    """Run a program the way cargo runs rustc: ``<wrapper> <program> <args…>``."""
    return subprocess.run(
        [str(wrapper), *argv], env={**os.environ, **env}, capture_output=True, text=True,
        check=False,
    )


def test_the_wrapper_spells_a_build_path_by_its_real_path(tmp_path, monkeypatch):
    wrapper, _config = _pin(_cfg(monkeypatch, tmp_path, tmp_path), tmp_path)
    cache = tmp_path / "cache"
    (cache / "debug").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "target").symlink_to(cache)
    asked = {
        "CARGO_BIN_EXE_my-svc": str(wt / "target" / "debug" / "my-svc"),  # a hyphen is legal
        "CARGO_TARGET_TMPDIR": str(wt / "target" / "tmp"),
        "CARGO_BIN_EXE_checked": "placeholder:checked",  # what `cargo check` passes
        "CARGO_MANIFEST_DIR": str(wt),
    }
    code = (
        "import json, os, sys; "
        f"print(json.dumps({{k: os.environ[k] for k in {sorted(asked)!r}}})); sys.exit(7)"
    )
    out = _through_wrapper(wrapper, asked, sys.executable, "-c", code)
    assert out.returncode == 7, out.stderr  # the program's own exit status
    real = cache.resolve()
    assert json.loads(out.stdout) == {
        "CARGO_BIN_EXE_my-svc": str(real / "debug" / "my-svc"),
        "CARGO_TARGET_TMPDIR": str(real / "tmp"),  # need not exist yet
        "CARGO_BIN_EXE_checked": "placeholder:checked",
        "CARGO_MANIFEST_DIR": str(wt),  # sources stay where the worktree has them
    }


@pytest.mark.skipif(not Path("/proc/self/status").is_file(), reason="needs /proc")
def test_the_wrapper_hands_the_compiler_the_signals_it_was_given(tmp_path, monkeypatch):
    # Python ignores SIGPIPE and SIGXFSZ when it starts, and what is ignored
    # stays ignored across exec: the compiler and its linker would inherit that.
    wrapper, _config = _pin(_cfg(monkeypatch, tmp_path, tmp_path), tmp_path)
    out = _through_wrapper(wrapper, {}, "cat", "/proc/self/status")
    assert out.returncode == 0, out.stderr
    ignored = int(next(l for l in out.stdout.splitlines() if l.startswith("SigIgn:")).split()[1], 16)
    assert not ignored & (1 << 12 | 1 << 24)  # signals 13 (PIPE) and 25 (XFSZ)


def test_the_config_sits_above_every_mirror_and_names_the_wrapper(tmp_path, monkeypatch):
    cfg = _cfg(monkeypatch, tmp_path, tmp_path)
    wrapper, config = _pin(cfg, tmp_path)
    # cargo reads the config of every directory above its cwd: a mirror's too.
    assert config.parent.parent in (cfg.wt_dir / "any-phase").parents
    assert tomllib.loads(config.read_text()) == {"build": {"rustc-wrapper": str(wrapper)}}
    assert os.access(wrapper, os.X_OK)
    # Checked again for the next mirror, nothing is rewritten: a build may be running it.
    before = (wrapper.stat().st_ino, config.stat().st_ino)
    _pin(cfg, tmp_path)
    assert (wrapper.stat().st_ino, config.stat().st_ino) == before
    assert sorted(p.name for p in config.parent.iterdir()) == ["config.toml", "rustc-wrap"]
    assert "RUSTC-WRAP-SKIP" not in _log_text(tmp_path)


def test_no_wrapper_without_the_shared_cache(tmp_path, monkeypatch):
    cfg = _cfg(monkeypatch, tmp_path, tmp_path, SWARM_BUILD_CACHE=0)
    _pin(cfg, tmp_path)
    assert not (cfg.state_dir / ".cargo").exists()


def test_turning_the_shared_cache_off_takes_the_config_away(tmp_path, monkeypatch):
    # The config replaces a rustc-wrapper of the owner's own under the state dir;
    # with the cache off there is nothing left for it to cure.
    _wrapper, config = _pin(_cfg(monkeypatch, tmp_path, tmp_path), tmp_path)
    assert config.is_file()
    _pin(_cfg(monkeypatch, tmp_path, tmp_path, SWARM_BUILD_CACHE=0), tmp_path)
    assert not config.exists()
    assert "RUSTC-WRAP-SKIP" not in _log_text(tmp_path)


def test_a_wrapper_that_cannot_run_is_never_named(tmp_path, monkeypatch):
    # A config naming a wrapper that does not start would fail every build under
    # the state dir; without the config the paths are only spelled as before.
    cfg = _cfg(monkeypatch, tmp_path, tmp_path)
    wrapper, config = _pin(cfg, tmp_path)
    assert config.is_file()
    working = wrapper.read_text()
    monkeypatch.setattr(gitq, "_WRAP_PYTHONS", (str(tmp_path / "no-such-python"),))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nor-this-one"))
    _pin(cfg, tmp_path)  # must not raise: a mirror is being made
    assert not config.exists()
    assert "RUSTC-WRAP-SKIP" in _log_text(tmp_path)
    # The wrapper a running build may hold was never replaced by one that fails.
    assert wrapper.read_text() == working
    assert [p.name for p in config.parent.iterdir()] == ["rustc-wrap"]


def test_the_wrapper_falls_back_to_the_tools_own_interpreter(tmp_path, monkeypatch):
    cfg = _cfg(monkeypatch, tmp_path, tmp_path)
    monkeypatch.setattr(gitq, "_WRAP_PYTHONS", (str(tmp_path / "no-such-python"),))
    wrapper, config = _pin(cfg, tmp_path)
    assert wrapper.read_text().splitlines()[0] == f"#!{sys.executable} -SE"
    assert config.is_file()
    assert "RUSTC-WRAP-SKIP" not in _log_text(tmp_path)


def test_pinning_never_stops_a_mirror_from_being_made(tmp_path, monkeypatch):
    cfg = _cfg(monkeypatch, tmp_path, tmp_path)
    home = cfg.state_dir / ".cargo"
    # Files that are not text are replaced, not tripped over.
    home.mkdir(parents=True)
    (home / "rustc-wrap").write_bytes(b"\xff\xfe")
    (home / "config.toml").write_bytes(b"\xff\xfe")
    wrapper, config = _pin(cfg, tmp_path)
    assert tomllib.loads(config.read_text()) == {"build": {"rustc-wrapper": str(wrapper)}}
    # A state dir where the config cannot be written or removed is only logged.
    shutil.rmtree(home)
    home.write_text("in the way")
    _pin(cfg, tmp_path)
    assert "RUSTC-WRAP-SKIP" in _log_text(tmp_path)


# -- the sweep ------------------------------------------------------------
def _unit(deps: Path, name: str, *env_deps: str, suffix: str = "") -> Path:
    """A compiled unit as rustc leaves it: its output and its dep-info."""
    deps.mkdir(parents=True, exist_ok=True)
    out = deps / f"{name}{suffix}"
    out.write_text("elf")
    lines = "".join(f"# env-dep:{dep}\n" for dep in env_deps)
    (deps / f"{name}.d").write_text(f"{out}: tests/{name}.rs\n\ntests/{name}.rs:\n\n{lines}")
    return out


def test_only_an_executable_built_through_a_missing_directory_is_dropped(tmp_path):
    target = tmp_path / "main-checkout" / "target"
    (target / "debug").mkdir(parents=True)
    shared = tmp_path / "cache" / "svc"  # a cache entry may be a link to a checkout's target/
    shared.parent.mkdir()
    shared.symlink_to(target)
    live = tmp_path / "wt" / "live" / "svc"
    live.mkdir(parents=True)
    (live / "target").symlink_to(shared)
    gone = tmp_path / "wt" / "gone" / "svc"
    deps = target / "debug" / "deps"

    dropped = [
        _unit(deps, "boot-1", "CARGO_PKG_NAME=svc", f"CARGO_BIN_EXE_my-svc={gone}/target/debug/my-svc"),
        _unit(deps, "scratch-2", f"CARGO_TARGET_TMPDIR={gone}/target/tmp"),
        _unit(
            target / "x86_64-unknown-linux-musl" / "release" / "deps", "boot-3",
            f"CARGO_BIN_EXE_my-svc={gone}/target/x86_64-unknown-linux-musl/release/my-svc",
        ),
        # Pinned to the cache, but it reads its fixtures from sources that are gone.
        _unit(deps, "fixtures-4", f"CARGO_BIN_EXE_my-svc={target}/debug/my-svc",
              f"CARGO_MANIFEST_DIR={gone}"),
    ]
    kept = [
        _unit(deps, "live-5", f"CARGO_BIN_EXE_my-svc={live}/target/debug/my-svc",
              f"CARGO_MANIFEST_DIR={live}"),
        _unit(deps, "pinned-6", f"CARGO_BIN_EXE_my-svc={target}/debug/my-svc",
              f"CARGO_TARGET_TMPDIR={target}/tmp"),
        _unit(deps, "checked-7", "CARGO_BIN_EXE_my-svc=placeholder:my-svc"),
        _unit(deps, "other-8", f"OUT_DIR={gone}/target/debug/build/x/out", "CARGO_BIN_EXE_unset"),
        _unit(deps, "plain-9"),
        # A library's output is not named after its dep-info: nothing to delete.
        _unit(deps, "libsvc-10", f"CARGO_MANIFEST_DIR={gone}", suffix=".rlib"),
    ]

    log = Log(tmp_path / "l.log")
    gitq._drop_stale_executables(shared, tmp_path / "svc", log)
    assert not any(exe.exists() for exe in dropped)
    assert all(out.exists() for out in kept)
    assert "TARGET-CACHE-STALE svc dropped 4 executable(s)" in _log_text(tmp_path)
    # Nothing left to drop: nothing more is said. Nor of a cache never built into,
    # or one whose link loops.
    gitq._drop_stale_executables(shared, tmp_path / "svc", log)
    gitq._drop_stale_executables(tmp_path / "cache" / "never-built", tmp_path / "svc", log)
    loop = tmp_path / "cache" / "loop"
    loop.symlink_to(loop)
    gitq._drop_stale_executables(loop, tmp_path / "svc", log)
    log.close()
    assert _log_text(tmp_path).count("TARGET-CACHE-STALE") == 1


def test_a_resumed_worktree_sweeps_its_cache_too(tmp_path, monkeypatch):
    cfg = _cfg(monkeypatch, tmp_path, tmp_path)
    shared = cfg.build_cache_dir / "svc"
    stale = _unit(
        shared / "debug" / "deps", "boot-1",
        f"CARGO_BIN_EXE_svc={tmp_path}/wt/gone/svc/target/debug/svc",
    )
    wt = tmp_path / "wt" / "resumed" / "svc"
    wt.mkdir(parents=True)
    (wt / "Cargo.toml").write_text("[package]\n")
    (wt / "target").symlink_to(shared)  # linked by the attempt being resumed
    log = Log(tmp_path / "l.log")
    gitq._link_target_cache(cfg, wt, tmp_path / "svc", log)
    log.close()
    assert not stale.exists()


# -- real git, real cargo -------------------------------------------------
_BOOT_RS = """\
use std::{path::Path, process::Command};

#[test]
fn the_binary_starts() {
    let exe = env!("CARGO_BIN_EXE_svc-bin");
    let tmp = env!("CARGO_TARGET_TMPDIR");
    println!("exe={exe}");
    println!("tmp={tmp}");
    let out = Command::new(exe).output().expect("spawn the svc binary");
    assert!(out.status.success());
    assert!(Path::new(tmp).is_dir(), "{tmp} is not a directory");
}
"""

_FIXTURE_RS = """\
#[test]
fn the_fixture_is_read() {
    let dir = env!("CARGO_MANIFEST_DIR");
    println!("sources={dir}");
    let text = std::fs::read_to_string(format!("{dir}/tests/fixture.txt")).expect("read the fixture");
    assert_eq!(text, "kept\\n");
}
"""

_SVC = {
    "Cargo.toml": (
        '[package]\nname = "svc"\nversion = "0.1.0"\nedition = "2021"\n\n'
        '[[bin]]\nname = "svc-bin"\npath = "src/main.rs"\n'
    ),
    "src/main.rs": 'fn main() {\n    println!("up");\n}\n',
    "tests/boot.rs": _BOOT_RS,
    "tests/fixture.rs": _FIXTURE_RS,
    "tests/fixture.txt": "kept\n",
    ".gitignore": "/target\nCargo.lock\n",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path, seed: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "master", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "swarm@test")
    _git(path, "config", "user.name", "swarm")
    _git(path, "config", "commit.gpgsign", "false")
    for rel, content in seed.items():
        file = path / rel
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


def _workspace(tmp_path: Path) -> Path:
    """An umbrella that gitignores one independent Rust repo, ``svc``."""
    project = _init_repo(tmp_path / "project", {"README.md": "root\n", ".gitignore": "/svc/\n"})
    _init_repo(project / "svc", _SVC)
    return project


def _cargo_test(cwd: Path, *args: str, test: str = "boot", wrapper: bool = True):
    """``cargo test -v`` on one test target from ``cwd``. ``wrapper=False`` is a
    build from before the wrapper: an empty RUSTC_WRAPPER turns the config's off."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("RUSTC_WRAPPER", "CARGO_BUILD_RUSTC_WRAPPER", "CARGO_TARGET_DIR",
                     "CARGO_BUILD_TARGET_DIR")
    }
    if not wrapper:
        env["RUSTC_WRAPPER"] = ""
    run = subprocess.run(
        ["cargo", "test", "-v", "--offline", *args, "--test", test, "--", "--nocapture"],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=600, check=False,
    )
    run.said = run.stdout + run.stderr
    return run


def _compiled_through(run, cfg) -> bool:
    """The build ran its compiler through *this* state dir's wrapper. The tests
    may themselves run below a state dir that has one; that one must not count."""
    return f"Running `{cfg.state_dir / '.cargo' / 'rustc-wrap'} " in run.stderr


@needs_git
def test_making_a_mirror_installs_the_wrapper(tmp_path, monkeypatch):
    project = _workspace(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(tmp_path / "l.log")
    mirror = gitq.worktree_add(cfg, "any", log)
    log.close()
    assert (cfg.state_dir / ".cargo" / "config.toml").is_file()
    assert (mirror / "svc" / "target").is_symlink()
    # Nothing of it is in a checkout: no worktree has a file git would pick up.
    for wt in (mirror, mirror / "svc"):
        status = subprocess.run(
            ["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True, check=True
        )
        assert status.stdout == ""


@needs_cargo
def test_a_test_still_starts_its_binary_after_the_mirror_that_built_it_is_gone(tmp_path, monkeypatch):
    project = _workspace(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(tmp_path / "l.log")
    # The order that fails: this mirror exists before the other one builds, so
    # its sources are older than the build and cargo runs that build as it is.
    waiting = gitq.worktree_add(cfg, "waiting", log)
    builder = gitq.worktree_add(cfg, "builder", log)
    cache = (cfg.build_cache_dir / "svc").resolve()

    # Built from the mirror root by manifest path, as workers often do.
    first = _cargo_test(builder, "--manifest-path", "svc/Cargo.toml")
    assert first.returncode == 0, first.said
    assert _compiled_through(first, cfg)
    assert f"exe={cache}/debug/svc-bin\n" in first.stdout
    assert f"tmp={cache}/tmp\n" in first.stdout

    gitq.discard(cfg, "builder", log)
    log.close()
    assert not builder.exists()
    again = _cargo_test(waiting / "svc")
    assert again.returncode == 0, again.said
    assert "Compiling" not in again.stderr  # no source touched, nothing rebuilt
    assert "TARGET-CACHE-STALE" not in _log_text(tmp_path)  # and nothing had to be dropped


@needs_cargo
def test_a_test_built_before_the_wrapper_is_rebuilt_when_its_mirror_goes(tmp_path, monkeypatch):
    project = _workspace(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(tmp_path / "l.log")
    waiting = gitq.worktree_add(cfg, "waiting", log)
    builder = gitq.worktree_add(cfg, "builder", log)

    first = _cargo_test(builder / "svc", wrapper=False)
    assert first.returncode == 0, first.said
    assert not _compiled_through(first, cfg)
    assert f"exe={builder}/svc/target/debug/svc-bin\n" in first.stdout  # through the mirror

    gitq.discard(cfg, "builder", log)
    log.close()
    assert "TARGET-CACHE-STALE svc dropped 1 executable(s)" in _log_text(tmp_path)
    again = _cargo_test(waiting / "svc")
    assert again.returncode == 0, again.said
    assert _compiled_through(again, cfg)
    cache = (cfg.build_cache_dir / "svc").resolve()
    assert f"exe={cache}/debug/svc-bin\n" in again.stdout


@needs_cargo
def test_a_test_that_reads_its_sources_is_rebuilt_when_its_mirror_goes(tmp_path, monkeypatch):
    # The sources' path is the mirror's own, so no wrapper can make it last: the
    # test is rebuilt by the mirror that runs it next.
    project = _workspace(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(tmp_path / "l.log")
    waiting = gitq.worktree_add(cfg, "waiting", log)
    builder = gitq.worktree_add(cfg, "builder", log)

    first = _cargo_test(builder / "svc", test="fixture")
    assert first.returncode == 0, first.said
    assert f"sources={builder}/svc\n" in first.stdout

    gitq.discard(cfg, "builder", log)
    log.close()
    assert "TARGET-CACHE-STALE svc dropped 1 executable(s)" in _log_text(tmp_path)
    again = _cargo_test(waiting / "svc", test="fixture")
    assert again.returncode == 0, again.said
    assert f"sources={waiting}/svc\n" in again.stdout


@needs_cargo
def test_a_mirror_removed_behind_the_tools_back_is_swept_by_the_next_mirror(tmp_path, monkeypatch):
    project = _workspace(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(tmp_path / "l.log")
    waiting = gitq.worktree_add(cfg, "waiting", log)
    builder = gitq.worktree_add(cfg, "builder", log)
    assert _cargo_test(builder / "svc", wrapper=False).returncode == 0
    shutil.rmtree(builder)

    # What the sweep is for: cargo sees nothing to rebuild, and the test starts
    # its binary through the directory that is gone.
    broken = _cargo_test(waiting / "svc")
    assert broken.returncode != 0
    assert "Compiling" not in broken.stderr
    assert "spawn the svc binary" in broken.said and "NotFound" in broken.said

    gitq.worktree_add(cfg, "later", log)
    log.close()
    again = _cargo_test(waiting / "svc")
    assert again.returncode == 0, again.said
