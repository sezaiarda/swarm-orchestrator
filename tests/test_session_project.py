"""A launched session's ``swarm`` commands answer for its project, from any cwd.

A worker's cwd is wherever its work is: its mirror, a component repo inside the
mirror, a checkout of an external repo. ``SWARM_PROJECT`` names the project the
session belongs to, and ``SWARM_STATE_DIR`` already points every command at
that project's run state. Resolving the config from the cwd instead pairs that
state with the wrong settings and the wrong ledger: from a component repo there
is no ``.swarm.toml``, so lanes read as off and ``swarm widen`` records
nothing; from the mirror the ledger is the copy branched at launch, so
``swarm follow-up`` accepts an id main has taken since. Real git: an umbrella
with one component repo, lanes on, a phase mirror built by ``gitq.worktree_add``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import gitq, ledgerw
from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import why as why_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load, session_project
from swarm_orchestrator.logutil import Log

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

LEDGER = (
    "# Ledger\n\n"
    "- [ ] `hold-W1` · dir:`alpha` · needs:— · touches:`alpha/tests/**` · **in flight**\n"
    "- [ ] `wait-W1` · dir:`alpha` · needs:— · touches:`alpha/docs/**` · **not in flight**\n"
)
LATE_ROW = "- [ ] `late-W9` · dir:`alpha` · needs:— · touches:`alpha/late/**` · **filed since**\n"
TOML = (
    '[tasks]\nledger = "docs/PHASE-LEDGER.md"\n'
    '[lanes]\nenabled = true\ncommons = ["./docs/PHASE-LEDGER.md"]\n'
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init(path: Path, seed: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "master", str(path)], check=True)
    for k, v in (("user.email", "swarm@test"), ("user.name", "swarm"), ("commit.gpgsign", "false")):
        _git(path, "config", k, v)
    for rel, text in seed.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    origin = path.parent / f"{path.name}.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True)
    _git(path, "remote", "add", "origin", str(origin))
    _git(path, "push", "-q", "-u", "origin", "master")


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Lanes on; ``hold-W1`` in flight with a mirror; the session's environment.

    ``.swarm.toml`` is tracked, so the mirror root holds a copy of it and the
    component repo inside the mirror holds none."""
    project = tmp_path / "project"
    _init(project, {"docs/PHASE-LEDGER.md": LEDGER, ".gitignore": "/alpha/\n", ".swarm.toml": TOML})
    _init(project / "alpha", {"tests/unit/y1.py": "y\n"})
    for k, v in (("SWARM_STATE_DIR", str(tmp_path / "state")), ("SWARM_TG_SINK", str(tmp_path / "tg")),
                 ("SWARM_GIT_ISOLATION", "worktree"), ("SWARM_GIT_MAIN", "master"),
                 ("SWARM_DRIVER", "bare"), ("SWARM_WORKER_CMD", "true"),
                 ("SWARM_WORKER_SETTINGS", ""), ("SWARM_SLUG", "session-project")):
        monkeypatch.setenv(k, v)
    for k in ("SWARM_GIT_REPOS", "SWARM_LANES", "SWARM_PROJECT"):
        monkeypatch.delenv(k, raising=False)
    cfg = load(project_dir=str(project))
    assert cfg.lanes_enabled
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        mirror = gitq.worktree_add(cfg, "hold-W1", log)
    finally:
        log.close()
    with state_mod.transaction(cfg) as st:
        st.parked.append("hold-W1")
        st.lanes["hold-W1"] = ["alpha/tests/**"]
    assert (mirror / ".swarm.toml").is_file() and not (mirror / "alpha" / ".swarm.toml").exists()
    monkeypatch.setenv("SWARM_SESSION_ID", "worker:hold-W1")
    monkeypatch.setenv("SWARM_PROJECT", str(project))
    return cfg, mirror


def _queued(cfg, kind: str) -> list[dict]:
    return [op for data in ledgerw.pending(cfg).values() for op in data["ops"]
            if op.get("kind") == kind]


def _main_gains_a_row(cfg) -> None:
    """Main's ledger takes ``late-W9`` after the mirror branched."""
    ledger = cfg.project_dir / cfg.ledger
    ledger.write_text(ledger.read_text() + LATE_ROW)
    _git(cfg.project_dir, "commit", "-qam", "ledger: a row filed since")


def test_widen_from_a_component_repo_of_a_mirror_grows_the_projects_lane(ws, monkeypatch, capsys):
    cfg, mirror = ws
    monkeypatch.chdir(mirror / "alpha")
    assert cli_main(["widen", "hold-W1", "alpha/src/new.py"]) == 0
    assert "lanes are off" not in capsys.readouterr().out
    assert state_mod.read(cfg).lanes["hold-W1"] == ["alpha/src/new.py", "alpha/tests/**"]


def test_follow_up_with_an_id_only_on_mains_ledger_is_refused_at_once(ws, monkeypatch, capsys):
    cfg, mirror = ws
    _main_gains_a_row(cfg)
    assert "late-W9" not in (mirror / cfg.ledger).read_text()
    for cwd in (mirror, mirror / "alpha"):
        monkeypatch.chdir(cwd)
        assert cli_main(["follow-up", "hold-W1", "late-W9", "--title", "the same id again",
                         "--dir", "alpha", "--touches", "alpha/again/**", "scope"]) == 2
        assert "late-W9 already has a ledger row" in capsys.readouterr().err
    assert _queued(cfg, "row") == []


def test_follow_up_with_a_free_id_is_queued_with_the_phase(ws, monkeypatch):
    cfg, mirror = ws
    _main_gains_a_row(cfg)
    monkeypatch.chdir(mirror / "alpha")
    assert cli_main(["follow-up", "hold-W1", "next-W1", "--title", "more", "--needs", "late-W9",
                     "--dir", "alpha", "--touches", "alpha/next/**", "scope"]) == 0
    assert [(op["id"], op["needs"]) for op in _queued(cfg, "row")] == [("next-W1", ["late-W9"])]


def test_record_reshape_and_why_read_the_projects_ledger(ws, monkeypatch, capsys):
    cfg, mirror = ws
    _main_gains_a_row(cfg)
    monkeypatch.chdir(mirror / "alpha")
    assert cli_main(["record", "late-W9", "note", "seen from a component repo"]) == 0
    assert cli_main(["record", "late-W9", "blocked", "waits on a box"]) == 0
    assert cli_main(["reshape", "hold-W1", "late-W9", "--touches", "alpha/late/x*", "narrower"]) == 0
    assert [op["phase"] for op in _queued(cfg, "record")] == ["late-W9", "late-W9"]
    assert [op["phase"] for op in _queued(cfg, "reshape")] == ["late-W9"]
    capsys.readouterr()
    assert cli_main(["why", "late-W9", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] != why_mod.UNKNOWN


def test_an_explicit_project_dir_still_wins(ws, tmp_path, monkeypatch, capsys):
    cfg, mirror = ws
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(mirror / "alpha")
    assert cli_main(["--project-dir", str(other), "widen", "hold-W1", "alpha/src/new.py"]) == 0
    assert "lanes are off" in capsys.readouterr().out
    assert state_mod.read(cfg).lanes["hold-W1"] == ["alpha/tests/**"]


def test_outside_a_session_the_cwd_is_the_project(ws, monkeypatch, capsys):
    cfg, mirror = ws
    monkeypatch.delenv("SWARM_PROJECT")
    monkeypatch.chdir(cfg.project_dir)
    assert cli_main(["widen", "hold-W1", "alpha/src/new.py"]) == 0
    assert state_mod.read(cfg).lanes["hold-W1"] == ["alpha/src/new.py", "alpha/tests/**"]
    monkeypatch.chdir(mirror / "alpha")
    assert cli_main(["widen", "hold-W1", "alpha/src/other.py"]) == 0
    assert "lanes are off" in capsys.readouterr().out


def test_every_session_is_told_its_project_whatever_the_isolation(ws, monkeypatch):
    """In place, a worker in a component repo has the same cwd problem."""
    cfg, mirror = ws
    assert launch_mod.session_env(cfg, mirror)["SWARM_PROJECT"] == str(cfg.project_dir)
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "none")
    in_place = load(project_dir=str(cfg.project_dir))
    env = launch_mod.session_env(in_place)
    assert env["SWARM_PROJECT"] == str(cfg.project_dir) and "SWARM_WORKTREE" not in env


def test_session_project_is_the_named_directory_or_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_PROJECT", raising=False)
    assert session_project() is None
    monkeypatch.setenv("SWARM_PROJECT", "")
    assert session_project() is None
    monkeypatch.setenv("SWARM_PROJECT", str(tmp_path / "gone"))
    assert session_project() is None
    (tmp_path / "real").mkdir()
    monkeypatch.setenv("SWARM_PROJECT", str(tmp_path / "real" / ".." / "real"))
    assert session_project() == (tmp_path / "real").resolve()


def test_a_project_config_that_does_not_load_falls_back_to_the_cwd_and_says_so(
        ws, monkeypatch, capsys):
    """The owner's live file may be mid-edit; a worker's command must still run."""
    cfg, mirror = ws
    (cfg.project_dir / ".swarm.toml").write_text("[lanes\nenabled = true\n")
    monkeypatch.chdir(mirror)
    assert cli_main(["widen", "hold-W1", "alpha/src/new.py"]) == 0
    assert "does not load" in capsys.readouterr().err
    assert state_mod.read(cfg).lanes["hold-W1"] == ["alpha/src/new.py", "alpha/tests/**"]


def test_a_session_whose_project_is_gone_falls_back_to_the_cwd(ws, tmp_path, monkeypatch):
    cfg, mirror = ws
    monkeypatch.setenv("SWARM_PROJECT", str(tmp_path / "gone"))
    monkeypatch.chdir(cfg.project_dir)
    assert cli_main(["widen", "hold-W1", "alpha/src/new.py"]) == 0
    assert state_mod.read(cfg).lanes["hold-W1"] == ["alpha/src/new.py", "alpha/tests/**"]
