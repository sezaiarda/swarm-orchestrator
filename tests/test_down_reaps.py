"""``swarm down`` ends every session the run spawned, and confirms it.

After ``swarm down`` the tmux session could be gone while ``claude``
processes (workers, the Overseer, an operator) kept running until killed by
hand. Killing the session only hangs up on its panes, and a
``respawn-pane -k`` (the operator's release) leaves a process that outlives its
SIGHUP running with no pane at all. The stand-in here is a shell that ignores
SIGHUP and SIGTERM, as does the child it waits on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from swarm_orchestrator import cli
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import tmux
from swarm_orchestrator.config import load

from test_web_lifecycle import DEMO

#: Ignores SIGHUP and SIGTERM, and so does the child it waits on (an ignored
#: signal stays ignored across exec) — only SIGKILL ends either.
STUBBORN = "trap '' HUP TERM; sleep 300 & wait"


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            stat = fh.read()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2 :][:1] not in ("Z", "X")


def _tree(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(pid)], capture_output=True, text=True)
    return [pid, *(int(p) for p in out.stdout.split())]


def _wait(pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    project = tmp_path / "project"
    shutil.copytree(DEMO, project)
    for k, v in {"SWARM_STATE_DIR": str(tmp_path / "state"), "SWARM_SLUG": "downtest",
                 "SWARM_SESSION": f"swarm-down-{os.getpid()}", "SWARM_TUI_AUTOSTART": "0",
                 "SWARM_TG_SINK": str(tmp_path / "tg.log")}.items():
        monkeypatch.setenv(k, v)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


def test_stubborn_sessions_are_escalated_to_sigkill_trees_and_all(cfg):
    env = {**os.environ, "SWARM_STATE_DIR": str(cfg.state_dir)}
    clean = {k: v for k, v in os.environ.items() if k != "SWARM_STATE_DIR"}
    unrelated = subprocess.Popen(["sleep", "300"], env=clean)  # no marker: never touched
    procs = [subprocess.Popen(["bash", "-c", STUBBORN], env=env, start_new_session=True)
             for _ in range(2)]
    pids: set[int] = set()
    try:
        assert _wait(lambda: all(len(_tree(p.pid)) == 2 for p in procs))
        pids = {pid for p in procs for pid in _tree(p.pid)}
        found = session_mod.session_processes(cfg)
        assert pids <= found and unrelated.pid not in found and os.getpid() not in found

        ended, left = session_mod.end_processes(cfg, found, wait=0.5)

        assert left == [] and ended >= 4
        assert not any(_alive(pid) for pid in pids)
        assert _alive(unrelated.pid)
    finally:
        _kill(pids)
        for p in (*procs, unrelated):
            p.kill()
            p.wait(timeout=5)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not available")
def test_down_ends_every_session_including_one_its_pane_let_go(cfg, monkeypatch):
    monkeypatch.setenv("SWARM_DRIVER", "tmux")
    monkeypatch.setattr(session_mod, "END_WAIT_S", 0.5)
    tcfg = load(project_dir=str(cfg.project_dir))
    session_mod.setup(tcfg)
    released: list[int] = []
    in_pane: list[int] = []
    try:
        st = state_mod.read(tcfg)
        marker = {"SWARM_STATE_DIR": str(tcfg.state_dir)}
        worker = st.slots[0].pane_id
        tmux.respawn_pane(worker, STUBBORN, env=marker)
        # The operator's release: its session is respawned away to `sleep`, and a
        # process that shrugs off the hang-up keeps running with no pane at all.
        tmux.respawn_pane(st.operator_pane, STUBBORN, env=marker)
        assert _wait(lambda: len(_tree(_pane_pid(st.operator_pane))) == 2)
        released = _tree(_pane_pid(st.operator_pane))
        tmux.respawn_pane(st.operator_pane, "exec sleep infinity")
        assert _wait(lambda: len(_tree(_pane_pid(worker))) == 2)
        in_pane = _tree(_pane_pid(worker))
        time.sleep(0.3)
        assert all(_alive(pid) for pid in released), "the stand-in should survive SIGHUP"

        assert cli.cmd_down(tcfg) == 0

        assert not tmux.session_exists(tcfg.session)
        survivors = [pid for pid in (*released, *in_pane) if _alive(pid)]
        assert survivors == []
    finally:
        session_mod.teardown(tcfg)
        _kill([*released, *in_pane])


def _kill(pids) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _pane_pid(pane: str) -> int:
    return int(tmux.run(["display-message", "-p", "-t", pane, "#{pane_pid}"]).stdout.strip())


# -- `swarm down` only ends what is this swarm's ------------------------------
@pytest.fixture
def tmux_cfg(cfg, monkeypatch):
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")
    monkeypatch.setenv("SWARM_DRIVER", "tmux")
    tcfg = load(project_dir=str(cfg.project_dir))
    yield tcfg
    if tmux.session_exists(tcfg.session):
        tmux.kill_session(tcfg.session)


def test_down_leaves_a_same_named_session_it_does_not_own(tmux_cfg, capsys):
    tmux.new_session(tmux_cfg.session)  # the owner's own, by coincidence same name
    assert cli.cmd_down(tmux_cfg) == 0
    assert tmux.session_exists(tmux_cfg.session)
    assert "not this swarm's" in capsys.readouterr().err


def test_down_leaves_a_session_another_swarm_marked(tmux_cfg):
    tmux.new_session(tmux_cfg.session)
    tmux.mark_owner(tmux_cfg.session, "/some/other/state")
    cli.cmd_down(tmux_cfg)
    assert tmux.session_exists(tmux_cfg.session)


def test_down_ends_its_own_session_and_no_unmarked_one_whatever_state_records(tmux_cfg):
    session_mod.setup(tmux_cfg)
    assert tmux.session_owner(tmux_cfg.session) == str(tmux_cfg.state_dir)
    cli.cmd_down(tmux_cfg)
    assert not tmux.session_exists(tmux_cfg.session)

    # A session with no marker is not a swarm's, even when a window id this run
    # once recorded is in it: window ids start again with every tmux server, so
    # the id names whatever window got that number since.
    win = tmux.new_session(tmux_cfg.session)
    assert tmux.session_owner(tmux_cfg.session) == ""
    with state_mod.transaction(tmux_cfg) as st:
        st.windows = {"dash": win}
    assert not session_mod.owns_session(tmux_cfg)
    cli.cmd_down(tmux_cfg)
    assert tmux.session_exists(tmux_cfg.session)


def test_down_never_signals_a_pid_that_is_no_longer_the_supervisor(cfg, capsys):
    clean = {k: v for k, v in os.environ.items() if k != "SWARM_STATE_DIR"}
    stranger = subprocess.Popen(["sleep", "300"], env=clean)
    try:
        with state_mod.transaction(cfg) as st:
            st.supervisor_pid = stranger.pid  # the number, since reused
        cli.cmd_down(cfg)
        assert stranger.poll() is None
        assert "no longer this project's supervisor" in capsys.readouterr().err
    finally:
        stranger.kill()
        stranger.wait(timeout=5)
