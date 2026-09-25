"""CLI flags and messages that must do what they say.

Each of these was accepted by argparse (or printed to the owner) while the code
behind it did nothing, or pointed at something that does not exist.
"""

from __future__ import annotations

import json

import pytest

from swarm_orchestrator import cli
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / ".swarm.toml").write_text('[swarm]\ndriver = "bare"\n[web]\nenabled = false\n',
                                          encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_DRIVER", raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    with state_mod.transaction(c) as st:
        st.done = {"a-P0": "ok", "a-P1": "skip", "a-P2": "fail"}
    return c


# -- swarm status --json / --all -----------------------------------------
def test_status_counts_the_done_map_unless_all(cfg, capsys):
    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert "done: 3 (fail=1 ok=1 skip=1) failed: a-P2" in out
    assert "a-P0" not in out

    assert cli.cmd_status(cfg, show_all=True) == 0
    assert "done={'a-P0': 'ok'" in capsys.readouterr().out


def test_status_json_is_machine_readable(cfg, capsys):
    assert cli.cmd_status(cfg, as_json=True) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["done_counts"] == {"fail": 1, "ok": 1, "skip": 1}
    assert data["failed"] == ["a-P2"] and "done" not in data
    assert data["config"]["driver"] == "bare" and isinstance(data["slots"], list)

    assert cli.cmd_status(cfg, as_json=True, show_all=True) == 0
    assert json.loads(capsys.readouterr().out)["done"]["a-P1"] == "skip"
