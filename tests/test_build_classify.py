"""Which ``swarm build`` commands queue (heavy) and which just go out (light),
and the pre-flight checks that refuse a hopeless command before it queues.

Pure functions over a temp directory: no gate, no processes.
"""

from __future__ import annotations

import json
import shlex

import pytest

from swarm_orchestrator.buildclass import HEAVY, LIGHT, classify, preflight


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "rs").mkdir()
    (tmp_path / "rs" / "Cargo.toml").write_text("[package]\n")
    (tmp_path / "rs" / "src").mkdir()
    web = tmp_path / "web"
    web.mkdir()
    (web / "package.json").write_text(json.dumps({"scripts": {
        "build": "vite build", "fmt": "prettier --write .", "lint": "eslint ."}}))
    (tmp_path / "lint.sh").write_text("#!/bin/sh\n# count lines\nfind . -name '*.rs' | xargs wc -l"
                                      " | sort -n\n")
    (tmp_path / "gate.sh").write_text("#!/bin/bash\nset -e\ncargo fmt --check\ncargo nextest run\n")
    (tmp_path / "opaque.sh").write_text('#!/bin/sh\ncd "$(dirname "$0")"\nls\n')
    for f in ("lint.sh", "gate.sh", "opaque.sh"):
        (tmp_path / f).chmod(0o755)
    (tmp_path / "report.py").write_text("import json, sys\nprint(json.load(open(sys.argv[1])))\n")
    (tmp_path / "spawn.py").write_text("import subprocess\nsubprocess.run(['cargo', 'build'])\n")
    return tmp_path


CASES = [
    # cargo: compile/test is heavy, housekeeping is light
    ("cargo build", "rs", HEAVY), ("cargo nextest run -p core filter", "rs", HEAVY),
    ("cargo test", "rs", HEAVY), ("cargo clippy -- -D warnings", "rs", HEAVY),
    ("cargo check", "rs", HEAVY), ("cargo doc --no-deps", "rs", HEAVY),
    ("cargo install --path .", "rs", HEAVY), ("cargo clean", "rs", HEAVY),
    ("cargo update -p serde", "rs", LIGHT), ("cargo metadata --format-version 1", "rs", LIGHT),
    ("cargo fmt --check", "rs", LIGHT), ("cargo +nightly fmt", "rs", LIGHT),
    ("cargo tree -d", "rs", LIGHT), ("cargo --version", "rs", LIGHT),
    ("cargo generate-lockfile", "rs", LIGHT), ("cargo frobnicate", "rs", HEAVY),
    # docker: building is heavy, printing is not
    ("docker buildx bake", ".", HEAVY), ("docker buildx bake --print", ".", LIGHT),
    ("docker buildx bake --print web", ".", LIGHT), ("docker build .", ".", HEAVY),
    ("docker compose build", ".", HEAVY), ("docker compose up -d", ".", HEAVY),
    ("docker compose ps", ".", LIGHT), ("docker ps", ".", LIGHT), ("docker run img", ".", HEAVY),
    # js tooling
    ("bun test", "web", HEAVY), ("bunx playwright test", "web", HEAVY),
    ("bun run build", "web", HEAVY), ("bun run fmt", "web", LIGHT),
    ("bun run lint", "web", HEAVY), ("bun install", "web", HEAVY), ("vite build", "web", HEAVY),
    ("bun --version", "web", LIGHT), ("bun pm ls", "web", LIGHT),
    # everyday light commands
    ("git status", ".", LIGHT), ("ls -la", ".", LIGHT), ("rustfmt --check a.rs", ".", LIGHT),
    ("make", ".", HEAVY), ("make -n", ".", LIGHT), ("go test ./...", ".", HEAVY),
    ("go mod tidy", ".", LIGHT), ("uv lock", ".", LIGHT), ("uv run pytest", ".", HEAVY),
    # python: light unless it starts processes or runs tests
    ("python3 report.py x.json", ".", LIGHT), ("python3 spawn.py", ".", HEAVY),
    ("python3 -c 'print(1)'", ".", LIGHT), ("python3 -c 'import os; os.system(\"x\")'", ".", HEAVY),
    ("python3 -m pytest -q", ".", HEAVY), ("python3", ".", HEAVY),
    ("uv run python report.py", ".", LIGHT),
    # wrappers are looked through
    ("timeout 600 cargo test", "rs", HEAVY), ("timeout 60 git fetch", ".", LIGHT),
    ("env RUST_LOG=debug cargo test", "rs", HEAVY), ("env A=1 git log", ".", LIGHT),
    ("nice -n 10 cargo build", "rs", HEAVY), ("nohup ls", ".", LIGHT),
    ("flock /tmp/l cargo build", "rs", HEAVY), ("xargs cargo build", "rs", HEAVY),
    ("find . -name x -exec rm {} ;", ".", LIGHT),
    ("find . -name Cargo.toml -execdir cargo build ;", ".", HEAVY),
    ("git bisect run cargo test", "rs", HEAVY), ("swarm status", ".", LIGHT),
    ("swarm build cargo build", "rs", HEAVY), ("swarm build git log", ".", LIGHT),
    # shell scripts: light only if every command in them is light
    ("sh -c 'cd web && bun run build'", ".", HEAVY),
    ("bash -c 'cd web && bun run fmt'", ".", LIGHT),
    ("sh -c 'git fetch && git status'", ".", LIGHT),
    ("sh -c 'cargo test 2>&1 | tail -5'", "rs", HEAVY),
    ("sh -c 'echo \"#\"; cargo build'", "rs", HEAVY),  # a quoted # is not a comment
    ("sh -c 'git status # cargo build'", ".", LIGHT),  # a real comment is
    ("sh -c '(cd rs && cargo update) && ls'", ".", LIGHT),
    ("sh -c 'for f in a b; do cargo fmt; done'", "rs", LIGHT),
    ("sh -c 'if true; then cargo build; fi'", "rs", HEAVY),
    ("bash -lc 'X=1 Y=2'", ".", LIGHT),
    ("./lint.sh", ".", LIGHT), ("./gate.sh", "rs", HEAVY), ("sh gate.sh", ".", HEAVY),
    ("bash -e -o pipefail lint.sh", ".", LIGHT),
    # what cannot be read stays heavy
    ("sh -c 'echo $(git rev-parse HEAD)'", ".", HEAVY), ("./opaque.sh", ".", HEAVY),
    ("sh -c 'f() { ls; }; f'", ".", HEAVY), ("sh -c 'eval ls'", ".", HEAVY),
    ("sh -c '$CMD'", ".", HEAVY), ("sh", ".", HEAVY), ("bash -s", ".", HEAVY),
    ("unknown-tool --flag", ".", HEAVY), ("node script.js", ".", HEAVY),
]


@pytest.mark.parametrize("cmd,where,cls", CASES, ids=[c[0] for c in CASES])
def test_classification_table(tree, cmd, where, cls):
    v = classify(shlex.split(cmd), tree / where)
    assert v.cls == cls, v.why


def test_config_patterns_win_heavy_over_light(tree):
    cargo_check = shlex.split("cargo check -p core")
    assert classify(cargo_check, tree / "rs", light=["cargo check"]).cls == LIGHT
    assert classify(["git", "gc"], tree, heavy=["git gc*"]).cls == HEAVY
    both = classify(["git", "gc"], tree, heavy=["git gc"], light=["git *"])
    assert both.cls == HEAVY  # heavy wins
    # patterns are matched at every level: through wrappers and inside scripts
    assert classify(shlex.split("timeout 5 bun run lint"), tree / "web",
                    light=["bun run lint"]).cls == LIGHT
    assert classify(["./opaque.sh"], tree, light=["*/opaque.sh"]).cls == LIGHT


def test_heavy_steps_name_the_build(tree):
    v = classify(shlex.split("sh -c 'cd rs && cargo fmt && cargo nextest run'"), tree)
    assert [s.argv[:2] for s in v.heavy_steps] == [["cargo", "nextest"]]


# -- pre-flight -----------------------------------------------------------
def _pf(cmd, cwd, path=None):
    return preflight(classify(shlex.split(cmd), cwd), path=path)


def test_preflight_passes_a_good_command(tree):
    assert _pf("cargo build", tree / "rs") is None
    assert _pf("sh -c 'cd rs && cargo build'", tree) is None
    assert _pf("sh -c 'cd web && bun run build'", tree) is None


def test_preflight_missing_program(tree):
    assert "cannot run 'no-such-tool'" in _pf("no-such-tool --x", tree)
    assert "cannot run '/no/such/bin'" in _pf("/no/such/bin", tree)
    assert "cannot run './missing.sh'" in _pf("./missing.sh", tree)
    assert "not found" in _pf("timeout 5 no-such-tool", tree)


def test_preflight_missing_cd_target_and_manifest(tree):
    assert "cd target 'nodir'" in _pf("sh -c 'cd nodir && cargo build'", tree)
    assert "Cargo.toml" in _pf("cargo build", tree)  # wrong cwd
    assert "--manifest-path 'x/Cargo.toml'" in _pf(
        "cargo test --manifest-path x/Cargo.toml", tree)
    assert "no Makefile" in _pf("make all", tree)
    assert "script 'missing.sh'" in _pf("bash missing.sh", tree)


def test_preflight_missing_bake_or_compose_file(tree):
    assert "bake file" in _pf("docker buildx bake web", tree)
    assert "-f/--file 'nope.hcl'" in _pf("docker buildx bake -f nope.hcl", tree)
    (tree / "docker-bake.hcl").write_text("")
    assert _pf("docker buildx bake web", tree) is None
    assert "compose file" in _pf("docker compose build", tree / "rs" / "src")
    (tree / "compose.yaml").write_text("")
    assert _pf("docker compose build", tree / "rs" / "src") is None  # found above


def test_preflight_missing_package_script(tree):
    assert "no script 'buidl'" in _pf("bun run buidl", tree / "web")


def test_preflight_trusts_nothing_after_a_step_that_writes(tree):
    # the directory may be created by the mkdir; do not refuse it
    assert _pf("sh -c 'mkdir -p out && cd out && cargo build --manifest-path ../rs/Cargo.toml'",
               tree) is None
    # a function or PATH change inside a login shell is not second-guessed
    assert _pf("bash -lc 'nvm use 20 && bun test'", tree) is None
    # uv run's tools live in the venv, not on PATH yet
    bin_dir = tree / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text("#!/bin/sh\n")
    (bin_dir / "uv").chmod(0o755)
    assert _pf("uv run pytest", tree, path=str(bin_dir)) is None
    assert "cannot run 'uv'" in _pf("uv run pytest", tree, path=str(tree / "rs" / "src"))
