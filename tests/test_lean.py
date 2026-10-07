"""`[swarm].lean_sessions`: every session the swarm opens runs on the project's
setup, not the owner's personal one (see ``lean.py``).

Measured on Claude Code 2.1.292, the owner's CLAUDE.md, memory, plugins, user
skills and connectors were about 40% of a bare session's first-turn input. The
switch that drops them (``--setting-sources project,local``) also drops the
owner's settings.json, so that is carried in ``--settings``: these tests pin both
halves, on every kind of session, and the off switch.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import bigpic, console, guide, launch, lean, master, operator, recap
from swarm_orchestrator import resolver as resolver_mod
from swarm_orchestrator import tmux
from swarm_orchestrator.config import load

OWNER = {
    "permissions": {"defaultMode": "bypassPermissions", "allow": ["Bash(ls:*)"]},
    "model": "opus[1m]",
    "env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"},
    "statusLine": {"type": "command", "command": "bash ~/.claude/statusline.sh"},
    "enabledPlugins": {"context7@claude-plugins-official": True},
    "extraKnownMarketplaces": {"m": {}},
    "claudeMdExcludes": ["x"],
}


@pytest.fixture
def owner(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "owner-settings.json"
    path.write_text(json.dumps(OWNER), encoding="utf-8")
    monkeypatch.setattr(lean, "OWNER_SETTINGS", path)
    return path


def _cfg(tmp_path: Path, monkeypatch, toml: str = ""):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    for key in ("SWARM_LEAN_SESSIONS", "SWARM_WORKER_CMD", "SWARM_WORKER_SETTINGS",
                "SWARM_MASTER_CMD", "SWARM_OVERSEER_CMD", "SWARM_OPERATOR_CMD",
                "SWARM_RESOLVER_CMD", "SWARM_BIG_PICTURE_CMD", "SWARM_CONSOLE_CMD"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".swarm.toml").write_text(toml, encoding="utf-8")
    return load(project_dir=str(tmp_path))


def _flags(tokens: list[str]) -> tuple[str | None, dict | None]:
    """The ``--setting-sources`` value and the one ``--settings`` object."""
    assert tokens.count("--settings") <= 1, "claude only takes the last --settings"
    sources = tokens[tokens.index("--setting-sources") + 1] if "--setting-sources" in tokens else None
    settings = json.loads(tokens[tokens.index("--settings") + 1]) if "--settings" in tokens else None
    return sources, settings


def _shell(cmd: str) -> tuple[str | None, dict | None]:
    return _flags(shlex.split(cmd))


def _assert_lean(sources, settings):
    assert sources == "project,local"
    assert settings["autoMemoryEnabled"] is False
    assert settings["env"]["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    # The owner's settings ride along: how a session runs is kept...
    assert settings["permissions"]["defaultMode"] == "bypassPermissions"
    assert settings["model"] == "opus[1m]"
    assert settings["env"]["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] == "1"
    # ...what is personal context is not.
    assert not {"enabledPlugins", "extraKnownMarketplaces", "claudeMdExcludes"} & set(settings)


# -- the settings object -----------------------------------------------------------------
def test_the_project_files_win_over_the_carried_owner_settings(tmp_path, owner):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(
        {"model": "sonnet", "permissions": {"allow": ["Bash(git:*)"]}}), encoding="utf-8")
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps(
        {"env": {"FOO": "1"}}), encoding="utf-8")

    got = lean.settings(tmp_path, {"teammateMode": "in-process"})

    assert got["model"] == "sonnet"  # --settings outranks the project: its word is kept here
    assert got["permissions"] == {"defaultMode": "bypassPermissions",
                                  "allow": ["Bash(ls:*)", "Bash(git:*)"]}
    assert got["env"] == {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1", "FOO": "1",
                          "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    assert got["teammateMode"] == "in-process"


def test_no_owner_settings_still_gives_a_lean_session(tmp_path):
    assert lean.settings(tmp_path) == lean.LEAN


def test_the_swarm_settings_go_on_top(tmp_path, owner):
    tap = {"type": "command", "command": "tap"}
    got = lean.settings(tmp_path, {"autoMemoryEnabled": True, "statusLine": tap})
    assert got["autoMemoryEnabled"] is True and got["statusLine"] == tap


# -- every kind of session ----------------------------------------------------------------
def test_a_worker_is_lean_with_its_tap_and_teammates_in_one_settings(tmp_path, monkeypatch, owner):
    cfg = _cfg(tmp_path, monkeypatch)
    cmd = launch._worker_shell(cfg, "P1", tmp_path)

    sources, settings = _shell(cmd)
    _assert_lean(sources, settings)
    assert settings["teammateMode"] == "in-process"
    assert "swarm_orchestrator.meters" in settings["statusLine"]["command"]
    assert cmd.endswith("--effort high")


@pytest.mark.parametrize("build", [
    lambda cfg, cwd: operator.operator_command(cfg, "J1", cwd),
    lambda cfg, cwd: master.master_command(cfg, master.OVERSEER, cwd),
    lambda cfg, cwd: master.master_command(cfg, master.INIT),
    lambda cfg, cwd: bigpic.command(cfg, cwd),
    lambda cfg, cwd: guide.command(cfg),
], ids=["operator", "overseer", "init", "big-picture", "guide"])
def test_every_pane_session_is_lean(tmp_path, monkeypatch, owner, build):
    cfg = _cfg(tmp_path, monkeypatch)
    _assert_lean(*_shell(build(cfg, tmp_path)))


def test_the_resolver_is_lean_in_its_repo(tmp_path, monkeypatch, owner):
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.driver = "tmux"
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text('{"model":"haiku"}', encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(tmux, "new_window", lambda *a, **k: "@1")
    monkeypatch.setattr(tmux, "list_panes", lambda win: ["%1"])
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, cmd, env=None: seen.append(cmd))
    monkeypatch.setattr(launch, "pretrust_dir", lambda *a, **k: None)
    monkeypatch.setattr(resolver_mod, "_await_reap", lambda phase: None)
    monkeypatch.setattr(resolver_mod, "_deliver", lambda *a, **k: True)

    class _Log:
        def line(self, *_a, **_k):
            pass

    assert resolver_mod.spawn(cfg, "P1", repo, _Log()) == "@1"
    sources, settings = _shell(seen[0])
    assert sources == "project,local" and settings["model"] == "haiku"  # the repo's own files


def test_the_console_is_lean_but_keeps_its_primer(tmp_path, monkeypatch, owner):
    cfg = _cfg(tmp_path, monkeypatch)
    argv = console.claude_argv(cfg)
    _assert_lean(*_flags(argv))
    assert argv[-2] == "--append-system-prompt"


def test_a_recap_call_is_lean(tmp_path, monkeypatch, owner):
    cfg = _cfg(tmp_path, monkeypatch)
    seen: list[list[str]] = []

    def run(argv, **_k):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"result": "ok"}', stderr="")

    monkeypatch.setattr(recap.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(recap.subprocess, "run", run)
    assert recap._cli_summary(cfg, "sum it up", "haiku")[0] == "ok"
    assert seen[0][:3] == ["claude", "-p", "sum it up"]
    _assert_lean(*_flags(seen[0]))


# -- what is left as configured --------------------------------------------------------------
def test_the_off_switch_gives_the_full_setup_back(tmp_path, monkeypatch, owner):
    cfg = _cfg(tmp_path, monkeypatch, "[swarm]\nlean_sessions = false\n")
    assert cfg.lean_sessions is False
    sources, settings = _shell(launch._worker_shell(cfg, "P1", tmp_path))
    assert sources is None
    assert settings["teammateMode"] == "in-process" and "permissions" not in settings
    assert _flags(console.claude_argv(cfg)) == (None, None)
    assert "--setting-sources" not in master.master_command(cfg, master.INIT)


def test_the_environment_turns_it_off(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    assert cfg.lean_sessions is True
    monkeypatch.setenv("SWARM_LEAN_SESSIONS", "0")
    assert load(project_dir=str(tmp_path)).lean_sessions is False


def test_a_command_that_picks_its_own_sources_keeps_them(tmp_path, monkeypatch, owner):
    cfg = _cfg(tmp_path, monkeypatch, '[worker]\nworker_cmd = "claude --setting-sources=user"\n')
    cmd = launch._worker_shell(cfg, "P1", tmp_path)
    assert cmd.count("--setting-sources") == 1
    assert "permissions" not in _shell(cmd)[1]
    cfg.console_cmd = "claude --setting-sources user,project"
    assert console.claude_argv(cfg).count("--setting-sources") == 1


def test_worker_settings_from_a_file_are_merged_in(tmp_path, monkeypatch, owner):
    path = tmp_path / "worker.json"
    path.write_text('{"teammateMode":"tmux"}', encoding="utf-8")
    cfg = _cfg(tmp_path, monkeypatch, f'[worker]\nworker_settings = "{path}"\n')
    sources, settings = _shell(launch._worker_shell(cfg, "P1", tmp_path))
    _assert_lean(sources, settings)
    assert settings["teammateMode"] == "tmux"


def test_unreadable_worker_settings_are_passed_as_given(tmp_path, monkeypatch, owner):
    cfg = _cfg(tmp_path, monkeypatch, '[worker]\nworker_settings = "/nonexistent/w.json"\n')
    cmd = launch._worker_shell(cfg, "P1", tmp_path)
    assert "--setting-sources" not in cmd and "--settings /nonexistent/w.json" in cmd


def test_a_replacing_command_is_left_alone(tmp_path, monkeypatch, owner):
    cfg = _cfg(tmp_path, monkeypatch, '[swarm]\nmaster_cmd = "fake-master.sh"\n')
    assert master.master_command(cfg, master.INIT) == "fake-master.sh"
