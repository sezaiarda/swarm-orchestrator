"""Every subagent a swarm session starts runs on ``[worker] subagent_model`` at
``subagent_effort`` (``subagents.py``), and each kind of session gets its own
model and effort. What claude does with these flags was checked against the
requests it sends (see the module docstring); here, that each session gets them.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import master as master_mod
from swarm_orchestrator import operator as operator_mod
from swarm_orchestrator.config import load


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    for leak in ("SWARM_WORKER_CMD", "SWARM_MASTER_CMD", "SWARM_OVERSEER_CMD",
                 "SWARM_OPERATOR_CMD", "SWARM_WORKER_EFFORT"):
        monkeypatch.delenv(leak, raising=False)
    (tmp_path / ".swarm.toml").write_text('[worker]\neffort = "medium"\n')
    return load(project_dir=str(tmp_path))


def _settings(flags: dict[str, str]) -> dict:
    """The one ``--settings`` object, from the file a lean session is given."""
    value = flags["--settings"]
    return json.loads(value) if value.startswith("{") else json.loads(Path(value).read_text())


def _flags(shell: str) -> dict[str, str]:
    """The value after each ``--flag`` of a session's shell command."""
    words = shlex.split(shell.split(" && exec ", 1)[1])
    return {w: words[i + 1] for i, w in enumerate(words[:-1]) if w.startswith("--")}


def test_a_lead_gets_only_sonnet_subagents_and_forces_them(cfg):
    flags = _flags(launch_mod._worker_shell(cfg, "k-W1", cfg.project_dir))
    agents = json.loads(flags["--agents"])
    assert agents["builder"]["model"] == "claude-sonnet-5-5"
    assert "builder-hard" not in agents
    assert {a["model"] for a in agents.values()} == {"claude-sonnet-5-5"}
    for name in ("builder", "general-purpose", "Explore", "Plan"):
        assert agents[name]["effort"] == "high", name
    assert agents["Explore"]["model"] == "claude-sonnet-5-5"
    assert "Edit" not in agents["Explore"]["tools"]
    env = _settings(flags)["env"]
    assert env["CLAUDE_CODE_SUBAGENT_MODEL"] == "claude-sonnet-5-5"
    assert env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"
    assert flags["--effort"] == "medium"  # the lead's own, from [worker].effort


@pytest.mark.parametrize("kind", ["operator", "overseer"])
def test_operator_and_overseer_run_sonnet_high_and_force_their_subagents(cfg, kind):
    shell = (operator_mod.operator_command(cfg, "op-1") if kind == "operator"
             else master_mod.master_command(cfg, master_mod.OVERSEER))
    flags = _flags(shell)
    assert flags["--model"] == "claude-sonnet-5-5" and flags["--effort"] == "high"
    agents = json.loads(flags["--agents"])
    assert "builder-hard" not in agents and agents["builder"]["model"] == "claude-sonnet-5-5"
    env = _settings(flags)["env"]
    assert env["CLAUDE_CODE_SUBAGENT_MODEL_FORCE"] == "1"


def test_subagent_settings_can_be_left_to_claude(cfg):
    cfg.subagent_model, cfg.subagent_effort = "", ""
    flags = _flags(launch_mod._worker_shell(cfg, "k-W1", cfg.project_dir))
    agents = json.loads(flags["--agents"])
    assert all("model" not in a and "effort" not in a for a in agents.values())
    assert "builder-hard" not in agents
    assert "env" not in _settings(flags) or not any(
        k.startswith("CLAUDE_CODE_SUBAGENT") for k in _settings(flags)["env"])


def test_a_row_naming_the_alias_of_the_swarms_model_runs_on_the_swarms_own(cfg):
    from swarm_orchestrator import models as models_mod

    cfg.worker_cmd = "claude --model claude-opus-5-5"
    text = ("- [ ] `a-W1` · model:`opus` · **the same model**\n"
            "- [ ] `a-W2` · model:`sonnet` · **another**\n")
    assert models_mod.overrides(cfg, text) == {"a-W2": "sonnet"}
    assert models_mod.override(cfg, "a-W1", text) == ""


def test_a_sonnet_lead_takes_sonnet_effort_and_an_opus_lead_keeps_worker_effort(cfg, monkeypatch):
    from swarm_orchestrator import models as models_mod

    cfg.worker_cmd = "claude --model claude-opus-5-5"
    cfg.worker_sonnet_effort = "high"
    rows = {"k-W1": "", "k-W2": "sonnet"}
    monkeypatch.setattr(models_mod, "override", lambda c, phase, *a: rows[phase])
    opus = _flags(launch_mod._worker_shell(cfg, "k-W1", cfg.project_dir))
    sonnet = _flags(launch_mod._worker_shell(cfg, "k-W2", cfg.project_dir))
    assert opus["--model"] == "claude-opus-5-5" and opus["--effort"] == "medium"
    assert sonnet["--model"] == "sonnet" and sonnet["--effort"] == "high"
