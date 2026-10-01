"""Every interactive session the swarm launches carries a display name
(``claude -n``), so the owner can tell them apart in claude's ``/resume`` picker.

The name is checked the way the shell will see it: the launch command is split
with ``shlex`` and the token after ``-n`` must be the whole name, once.
"""

from __future__ import annotations

import shlex

import pytest

from swarm_orchestrator import bigpic, console, guide, master, tmux
from swarm_orchestrator import config as config_mod
from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import operator as operator_mod
from swarm_orchestrator import resolver as resolver_mod


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "repo").mkdir(parents=True)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    for leak in ("SWARM_WORKER_CMD", "SWARM_MASTER_CMD", "SWARM_OVERSEER_CMD",
                 "SWARM_OPERATOR_CMD", "SWARM_RESOLVER_CMD", "SWARM_BIG_PICTURE_CMD",
                 "SWARM_CONSOLE_CMD"):
        monkeypatch.delenv(leak, raising=False)
    return config_mod.load(project_dir=str(project))


def names(argv: list[str] | str) -> list[str]:
    tokens = shlex.split(argv) if isinstance(argv, str) else argv
    return [tokens[i + 1] for i, t in enumerate(tokens) if t in ("-n", "--name")]


def resolver_cmd(cfg, monkeypatch) -> str:
    got: list[str] = []
    cfg.driver = "tmux"
    monkeypatch.setattr(tmux, "new_window", lambda *a, **k: "@9")
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: ["%9"])
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, cmd, **k: got.append(cmd))
    monkeypatch.setattr(resolver_mod, "_deliver", lambda *a, **k: True)

    class _Log:
        def line(self, text: str) -> None:
            pass

    resolver_mod.spawn(cfg, "P1", cfg.project_dir / "repo", _Log())
    return got[0]


def test_every_launch_path_names_its_session(cfg, monkeypatch):
    launched = {
        "worker": launch_mod._worker_shell(cfg, "P1", cfg.project_dir),
        "overseer": master.master_command(cfg, master.OVERSEER),
        "init": master.master_command(cfg, master.INIT),
        "operator": operator_mod.operator_command(cfg, "op-7"),
        "resolver": resolver_cmd(cfg, monkeypatch),
        "guide": guide.command(cfg),
        "big-picture": bigpic.command(cfg, cfg.project_dir),
        "console": console.claude_argv(cfg, "abc", resume=False),
    }
    assert {kind: names(cmd) for kind, cmd in launched.items()} == {
        "worker": ["swarm · worker · P1"],
        "overseer": ["swarm · overseer"],
        "init": ["swarm · init"],
        "operator": ["swarm · operator · op-7"],
        "resolver": ["swarm · resolver · P1"],
        "guide": ["swarm · guide"],
        "big-picture": ["swarm · big-picture"],
        "console": ["swarm · console"],
    }


def test_a_command_that_names_itself_keeps_its_name(cfg):
    """An older `worker_cmd` still carries `-n worker:{phase}`: it keeps working as
    it did, with that one name, never two."""
    cfg.worker_cmd = "claude --model opus -n worker:{phase}"
    assert names(launch_mod._worker_shell(cfg, "P1", cfg.project_dir)) == ["worker:P1"]
    cfg.console_cmd = "claude --name=mine"
    argv = console.claude_argv(cfg, "abc", resume=True)
    assert names(argv) == [] and "--name=mine" in argv


def test_a_worker_whose_agent_is_named_either_way_is_still_matched():
    """The dashboard matches a worker's live claude by name under isolation=none:
    the new name and the old one both find it, and P1 never claims P10's."""
    from swarm_orchestrator.tui import probes

    def agent(name):
        return probes.AgentInfo(pid=1, cwd="", kind="", started_at=None, session_id="",
                                name=name, status="busy", waiting_for="")

    for name in ("swarm · worker · P1", "worker:P1"):
        assert probes.match_agent([agent(name.replace("P1", "P10")), agent(name)], "P1",
                                  None).name == name
