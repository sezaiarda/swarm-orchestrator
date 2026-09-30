"""A reshape moves the lane a phase in flight holds.

The scheduler reads a held lane from the ``State.lanes`` snapshot taken at
launch, so a reshape that narrows a parked phase's touches must replace that
snapshot too, or the rows behind it keep waiting on a lane the row no longer
names. It may only do so when the new lane still covers everything the phase's
worktree changed against its base (commits, edits and new files, commons left
out); otherwise it is refused, naming each path. Real git: an umbrella with one
component repo, lanes on, a phase mirror built by ``gitq.worktree_add``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import gitq, ledgerw, master
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

LEDGER = (
    "# Ledger\n\n"
    "- [ ] `hold-W1` · dir:`alpha` · needs:— · touches:`alpha/tests/**` `@live-box`"
    " · **the parked phase**\n"
    "- [ ] `wait-W1` · dir:`alpha` · needs:— · touches:`alpha/tests/unit/y*` · **behind it**\n"
    "- [ ] `free-W1` · dir:`alpha` · needs:— · touches:`alpha/docs/**` · **not in flight**\n"
)
HELD = ["@live-box", "alpha/tests/**"]


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
    """Lanes on; ``hold-W1`` parked with its launch snapshot and a mirror."""
    project = tmp_path / "project"
    _init(project, {"docs/PHASE-LEDGER.md": LEDGER, ".gitignore": "/alpha/\n/.swarm.toml\n"})
    (project / ".swarm.toml").write_text(
        '[tasks]\nledger = "docs/PHASE-LEDGER.md"\n'
        '[lanes]\nenabled = true\nresources = ["live-box"]\n'
        'commons = ["./docs/PHASE-LEDGER.md", "*/CHANGELOG.md"]\n')
    _init(project / "alpha", {"tests/unit/y1.py": "y\n", "CHANGELOG.md": "# log\n"})
    for k, v in (("SWARM_STATE_DIR", str(tmp_path / "state")), ("SWARM_TG_SINK", str(tmp_path / "tg")),
                 ("SWARM_GIT_ISOLATION", "worktree"), ("SWARM_GIT_MAIN", "master"),
                 ("SWARM_DRIVER", "bare"), ("SWARM_WORKER_CMD", "true"),
                 ("SWARM_WORKER_SETTINGS", ""), ("SWARM_SLUG", "relane")):
        monkeypatch.setenv(k, v)
    for k in ("SWARM_SESSION_ID", "SWARM_GIT_REPOS", "SWARM_LANES"):
        monkeypatch.delenv(k, raising=False)
    cfg = load(project_dir=str(project))
    assert cfg.lanes_enabled
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        gitq.worktree_add(cfg, "hold-W1", log)
    finally:
        log.close()
    with state_mod.transaction(cfg) as st:
        st.parked.append("hold-W1")
        st.lanes["hold-W1"] = list(HELD)
    return cfg


def _alpha(cfg, *, commit: dict[str, str] = {}, loose: dict[str, str] = {}) -> None:
    """Commit ``commit`` in hold-W1's worktree of alpha, then leave ``loose`` there
    uncommitted (new, or edits to tracked files)."""
    wt = cfg.wt_dir / "hold-W1" / "alpha"
    for rel, text in {**commit, **loose}.items():
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        (wt / rel).write_text(text)
        if rel in commit:
            _git(wt, "add", rel)
    if commit:
        _git(wt, "commit", "-qm", "hold-W1 work")


def _reshape(cfg, row: str, touches: str) -> int:
    return cli_main(["--project-dir", str(cfg.project_dir), "reshape", "overseer", row,
                     "--touches", touches, "narrow the held lane"])


def _flush(cfg) -> ledgerw.Applied:
    log = Log(cfg.supervisor_log)
    try:
        return ledgerw.flush(cfg, log, {})
    finally:
        log.close()


def _lanes(cfg) -> dict:
    return master.build_context(cfg, state_mod.read(cfg))["lanes"]


def test_narrowing_a_parked_lane_frees_the_row_waiting_behind_it(ws, capsys):
    _alpha(ws, commit={"tests/e2e/x1.py": "x\n"},
           loose={"tests/e2e/x2.py": "new\n", "CHANGELOG.md": "# log\n- a commons edit\n"})
    before = _lanes(ws)
    assert "wait-W1" not in before["picked"]
    assert before["waits"]["wait-W1"]["holder"] == "hold-W1"
    assert _reshape(ws, "hold-W1", "alpha/tests/e2e/x*,@live-box") == 0
    assert "reshape hold-W1: queued" in capsys.readouterr().out
    assert _flush(ws).touched == ["reshape hold-W1"]
    assert state_mod.read(ws).lanes["hold-W1"] == ["@live-box", "alpha/tests/e2e/x*"]
    assert "LANE-RESHAPED hold-W1 @live-box alpha/tests/** -> @live-box alpha/tests/e2e/x*" \
        in ws.supervisor_log.read_text()
    assert "wait-W1" in _lanes(ws)["picked"]


def test_a_reshape_that_drops_a_changed_path_is_refused_and_names_it(ws, capsys):
    _alpha(ws, commit={"tests/e2e/x1.py": "x\n", "tests/unit/y1.py": "edited\n"},
           loose={"tests/unit/z.py": "untracked\n"})
    assert _reshape(ws, "hold-W1", "alpha/tests/e2e/x*,@live-box") == 2
    err = capsys.readouterr().err
    assert "hold-W1 is in flight" in err
    assert "alpha/tests/unit/y1.py" in err and "alpha/tests/unit/z.py" in err
    assert "alpha/tests/e2e/x1.py" not in err
    assert ledgerw.pending(ws) == {}
    assert state_mod.read(ws).lanes["hold-W1"] == HELD


def test_the_writer_refuses_it_too_when_the_worktree_moved_after_filing(ws):
    led = ws.project_dir / "docs" / "PHASE-LEDGER.md"
    before = led.read_bytes()
    ledgerw.file_reshape(ws, "overseer", "hold-W1", "narrow", touches=["alpha/tests/e2e/x*", "@live-box"])
    _alpha(ws, loose={"tests/unit/z.py": "untracked\n"})
    got = _flush(ws)
    assert got.refused and "alpha/tests/unit/z.py" in got.refused[0]
    assert not got.relaned
    assert led.read_bytes() == before
    assert state_mod.read(ws).lanes["hold-W1"] == HELD
    hist = ledgerw.history_text(ws.project_dir, ws.history_dir, "hold-W1")
    assert "reshape of `hold-W1` refused" in hist


def test_a_held_resource_can_only_be_kept(ws, capsys):
    """Nothing on disk says whether a phase used the box: dropping it is refused."""
    assert _reshape(ws, "hold-W1", "alpha/tests/**") == 2
    assert "@live-box (its changes there cannot be read)" in capsys.readouterr().err
    assert _reshape(ws, "hold-W1", "alpha/tests/e2e/**,@live-box") == 0


def test_widen_after_a_narrowing_still_adds(ws):
    assert _reshape(ws, "hold-W1", "alpha/tests/e2e/x*,@live-box") == 0
    _flush(ws)
    assert cli_main(["--project-dir", str(ws.project_dir), "widen", "hold-W1", "alpha/docs/**"]) == 0
    assert state_mod.read(ws).lanes["hold-W1"] == ["@live-box", "alpha/docs/**", "alpha/tests/e2e/x*"]


def test_a_widen_between_filing_and_landing_is_kept(ws):
    assert _reshape(ws, "hold-W1", "alpha/tests/e2e/x*,@live-box") == 0
    assert cli_main(["--project-dir", str(ws.project_dir), "widen", "hold-W1", "alpha/docs/**"]) == 0
    _flush(ws)
    assert state_mod.read(ws).lanes["hold-W1"] == ["@live-box", "alpha/docs/**", "alpha/tests/e2e/x*"]


def test_a_row_not_in_flight_only_changes_the_ledger(ws):
    assert _reshape(ws, "free-W1", "alpha/docs/a.md") == 0
    got = _flush(ws)
    assert got.touched == ["reshape free-W1"] and not got.relaned
    assert "free-W1" not in state_mod.read(ws).lanes
