"""The resolver pane's command line and the line typed into it."""

from __future__ import annotations

import pytest

from swarm_orchestrator import config as config_mod
from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import resolver as resolver_mod
from swarm_orchestrator import tmux


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def line(self, text: str) -> None:
        self.lines.append(text)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "repo").mkdir(parents=True)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "resolvertest")
    monkeypatch.delenv("SWARM_RESOLVER_CMD", raising=False)
    cfg = config_mod.load(project_dir=str(project))
    cfg.driver = "tmux"
    monkeypatch.setattr(tmux, "new_window", lambda *a, **k: "@9")
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: ["%9"])
    monkeypatch.setattr(launch_mod, "pretrust_dir", lambda *a, **k: None)
    return cfg


def _spawned_cmd(cfg, monkeypatch) -> str:
    got: list[str] = []
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, cmd, **k: got.append(cmd))
    monkeypatch.setattr(resolver_mod, "_deliver", lambda *a, **k: None)
    resolver_mod.spawn(cfg, "P1", cfg.project_dir / "repo", _Log())
    return got[0]


def test_the_resolver_runs_on_sonnet_by_default(cfg, monkeypatch):
    assert cfg.resolver_model == "sonnet"
    assert _spawned_cmd(cfg, monkeypatch).endswith("exec claude --model sonnet")


def test_an_empty_resolver_model_inherits_the_users_setting(cfg, monkeypatch):
    cfg.resolver_model = ""
    assert _spawned_cmd(cfg, monkeypatch).endswith("exec claude")


def test_a_custom_resolver_cmd_is_used_as_is(cfg, monkeypatch):
    cfg.resolver_cmd = "my-resolver"
    assert _spawned_cmd(cfg, monkeypatch) == "my-resolver"


def test_the_typed_line_names_the_projects_checks(cfg, monkeypatch):
    typed: list[str] = []
    monkeypatch.setattr(launch_mod, "await_ready", lambda *a, **k: True)
    monkeypatch.setattr(tmux, "send_submit", lambda pane, line: typed.append(line) or True)
    cfg.git_auto_resolve_check = {"docs/PHASE-LEDGER.md": "python3 ci/gate.py"}
    resolver_mod._deliver(cfg, "%9", "P1", cfg.project_dir / "repo", _Log())
    assert "docs/PHASE-LEDGER.md: `python3 ci/gate.py`" in typed[0]
    assert "swarm resolved P1" in typed[0]
