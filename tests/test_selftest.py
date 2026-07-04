"""Tier B: real tmux mechanics (no claude).

Exercises session bring-up + tagged slots and the worker launcher's real
respawn-pane / readiness-detect / send-submit path against the fake worker
banner. Skipped if tmux is absent. Each test uses a throwaway session and tears
it down in ``finally``.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import tmux
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / "examples" / "demo"

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not available"
)


def _cfg(monkeypatch, tmp_path: Path, tag: str):
    project = tmp_path / "project"
    shutil.copytree(DEMO, project)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_BIN", f"{sys.executable} -m swarm_orchestrator")
    monkeypatch.setenv("SWARM_DRIVER", "tmux")
    monkeypatch.setenv("SWARM_SESSION", f"swarm-test-{os.getpid()}-{tag}")
    monkeypatch.setenv("SWARM_SLUG", "tmuxtest")
    monkeypatch.setenv("FAKE_WORKER_SLEEP", "1")
    for leak in ("SWARM_MASTER_CMD", "SWARM_WORKER_CMD", "SWARM_READY_MARKER"):
        monkeypatch.delenv(leak, raising=False)
    return load(project_dir=str(project))


def test_session_setup_creates_four_tagged_slots(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch, tmp_path, "setup")
    try:
        session_mod.setup(cfg)
        assert tmux.session_exists(cfg.session)
        st = state_mod.read(cfg)
        assert set(st.windows) == {"master", "workers"}
        assert st.master_pane
        pairs = tmux.list_panes_with_slot(st.windows["workers"])
        assert len(pairs) == 4
        assert sorted(tag for _, tag in pairs) == ["0", "1", "2", "3"]
        # every slot's pane id was recorded in state
        assert all(s.pane_id for s in st.slots)
    finally:
        session_mod.teardown(cfg)


def test_worker_launch_readiness_sendkeys_and_done(monkeypatch, tmp_path):
    cfg = _cfg(monkeypatch, tmp_path, "launch")
    log = Log(cfg.supervisor_log)
    try:
        session_mod.setup(cfg)
        # Real respawn-pane + readiness detector (fake banner) + send-keys +
        # echo verification all happen inside launch().
        assert launch_mod.launch(cfg, "P0", log) is True
        st = state_mod.read(cfg)
        assert any(s.busy and s.phase == "P0" for s in st.slots)

        sentinel = cfg.done_dir / "P0.ok"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not sentinel.is_file():
            time.sleep(0.1)
        assert sentinel.is_file(), "worker never signalled done"
    finally:
        log.close()
        session_mod.teardown(cfg)


def test_worker_survives_the_folder_trust_dialog(monkeypatch, tmp_path):
    """A real claude in a fresh worktree pops "Do you trust the files in this
    folder?". The fake reproduces it (and re-prompts once). Readiness detection
    must dismiss it reactively and still submit /prime — so the worker reaches
    `done` rather than sitting on the dialog. A launcher that dismisses only once
    (or treats the dialog as ready) would hang here and time out."""
    monkeypatch.setenv("FAKE_WORKER_TRUST", "1")
    monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(tmp_path / "claude.json"))  # never the real one
    cfg = _cfg(monkeypatch, tmp_path, "trust")
    log = Log(cfg.supervisor_log)
    try:
        session_mod.setup(cfg)
        assert launch_mod.launch(cfg, "P0", log) is True
        sentinel = cfg.done_dir / "P0.ok"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not sentinel.is_file():
            time.sleep(0.1)
        assert sentinel.is_file(), "worker stuck at the folder-trust dialog"
    finally:
        log.close()
        session_mod.teardown(cfg)
