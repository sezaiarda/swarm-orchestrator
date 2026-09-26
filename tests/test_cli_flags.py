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


# -- the ledger is the project's, wherever the command runs from ----------
CYCLE = "- [ ] `a-P0` · needs:`a-P1`\n- [ ] `a-P1` · needs:`a-P0`\n"


def test_check_reads_the_project_ledger_from_another_cwd(cfg, tmp_path, monkeypatch, capsys):
    (cfg.project_dir / cfg.ledger).write_text(CYCLE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # `swarm --project-dir <project> check` from elsewhere
    assert cli.cmd_check(cfg, strict=False) == 1
    out = capsys.readouterr().out
    assert "ledger: 2 phases" in out and "dependency cycle" in out


def test_retry_cascade_reads_the_project_ledger_from_another_cwd(cfg, tmp_path, monkeypatch):
    (cfg.project_dir / cfg.ledger).write_text(
        "- [ ] `a-P2` · needs:—\n- [ ] `a-P3` · needs:`a-P2`\n", encoding="utf-8")
    with state_mod.transaction(cfg) as st:
        st.done = {"a-P2": "fail", "a-P3": "ok"}
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_retry(cfg, ["a-P2"], all_failed=False, cascade=True,
                         launch=False, keep_branch=True) == 0
    assert state_mod.read(cfg).done == {}


# -- messages that name real commands -------------------------------------
def test_retry_without_a_supervisor_names_real_commands(cfg, capsys):
    assert cli.cmd_retry(cfg, ["a-P2"], all_failed=False, cascade=False,
                         launch=False, keep_branch=True) == 0
    out = capsys.readouterr().out
    assert "nudge" not in out and "swarm up" in out
    for tail in out.split("`swarm ")[1:]:
        assert tail.split("`")[0].split()[0] in cli._known_commands()


def test_doctor_points_a_failed_phase_at_retry(cfg):
    from swarm_orchestrator import doctor

    check = doctor._check_failed(state_mod.read(cfg))
    assert check.fix_hint.startswith("swarm retry a-P2")


# -- swarm up over a live session ------------------------------------------
def test_up_refuses_an_existing_session_before_touching_state(cfg, monkeypatch, capsys):
    from swarm_orchestrator import tmux

    cfg.driver = "tmux"
    monkeypatch.setattr(tmux, "session_exists", lambda name: name == cfg.session)
    before = cfg.state_path.read_bytes()
    assert cli.cmd_up(cfg, attach=False) == 1
    err = capsys.readouterr().err
    assert "already up" in err and "swarm down" in err and "tmux attach" in err
    assert cfg.state_path.read_bytes() == before  # nothing was reset
    assert not cfg.fifo_path.exists()


# -- swarm skip ------------------------------------------------------------
def test_a_skip_survives_the_next_swarm_up(cfg):
    # A skip once lived only in state.json, so a restart forgot it and
    # rows re-blocked behind the skipped phase with a slot free.
    from swarm_orchestrator import gitq

    assert cli.cmd_skip(cfg, "a-P7") == 0
    assert gitq.sentinel_done(cfg)["a-P7"] == "skip"
    assert state_mod.read(cfg).done["a-P7"] == "skip"
