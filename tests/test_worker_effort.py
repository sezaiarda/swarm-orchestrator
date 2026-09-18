"""`[worker].effort`: every worker's effort is pinned, not inherited from the owner's settings.

A worker used to run at whatever ~/.claude/settings.json said that day, so an
owner switching their own sessions to xhigh made every phase dearer without a
line of swarm config changing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_orchestrator import launch
from swarm_orchestrator.config import load


def _cfg(tmp_path: Path, monkeypatch, toml: str = ""):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("SWARM_WORKER_EFFORT", raising=False)
    monkeypatch.delenv("SWARM_WORKER_CMD", raising=False)
    (tmp_path / ".swarm.toml").write_text(toml, encoding="utf-8")
    return load(project_dir=str(tmp_path))


def test_workers_run_at_high_effort_by_default(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    assert cfg.worker_effort == "high"
    assert launch._worker_shell(cfg, "P1", tmp_path).endswith("--effort high")


def test_an_empty_effort_inherits_the_owner_setting(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, '[worker]\neffort = ""\n')
    assert "--effort" not in launch._worker_shell(cfg, "P1", tmp_path)


def test_the_environment_overrides_the_file(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, '[worker]\neffort = "low"\n')
    assert cfg.worker_effort == "low"
    monkeypatch.setenv("SWARM_WORKER_EFFORT", "xhigh")
    assert load(project_dir=str(tmp_path)).worker_effort == "xhigh"


def test_a_misspelt_effort_fails_at_load_not_in_every_pane(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="effort"):
        _cfg(tmp_path, monkeypatch, '[worker]\neffort = "hihg"\n')
