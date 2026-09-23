"""The board's lifecycle: ``swarm up`` starts it, ``swarm down`` stops it,
``status``/``doctor`` say where it is. The suite turns the board off by default
(conftest ``_web_off``); these tests turn it back on, on a free port."""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import tmux
from swarm_orchestrator.config import load
from swarm_orchestrator.web import lifecycle

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _wait(pred, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_up_starts_the_board_and_down_stops_it(swarm):
    port = _free_port()
    swarm.env.update({"SWARM_WEB": "1", "SWARM_WEB_PORT": str(port), "SWARM_WEB_HOST": "127.0.0.1"})
    out = swarm.up()
    assert f":{port}/" in out.stdout
    try:
        assert _wait(lambda: _listening(port)), "the board never came up"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board", timeout=5) as r:
            board = json.loads(r.read())
        assert {c["key"] for c in board["columns"]} >= {"ready", "done", "blocked"}
        pid = int((swarm.state_dir / lifecycle.PIDFILE).read_text())
        status = swarm.cli("status").stdout
        assert f"web: http://127.0.0.1:{port}/ (listening)" in status
        doctor = swarm.cli("doctor", "--json", check=False).stdout
        web = next(c for c in json.loads(doctor) if c["name"] == "web.board")
        assert web["status"] == "ok" and "listening" in web["detail"]
    finally:
        swarm.down()
    assert _wait(lambda: not _listening(port)), "down left the board running"
    assert not (swarm.state_dir / lifecycle.PIDFILE).exists()
    assert not Path(f"/proc/{pid}/cmdline").exists() or b"web" not in Path(
        f"/proc/{pid}/cmdline").read_bytes()


def test_the_board_is_not_started_when_disabled(swarm):
    port = _free_port()
    swarm.env.update({"SWARM_WEB": "0", "SWARM_WEB_PORT": str(port)})
    swarm.up()
    try:
        time.sleep(1.0)
        assert not _listening(port)
        assert "web: off" in swarm.cli("status").stdout
    finally:
        swarm.down()


def test_status_line_when_nothing_listens(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_WEB", "1")
    monkeypatch.setenv("SWARM_WEB_PORT", str(_free_port()))
    cfg = load(project_dir=str(tmp_path))
    assert "not listening" in lifecycle.status_line(cfg)
    assert lifecycle.stop(cfg) is False  # no pid file: nothing to stop


def test_lan_ips_are_never_loopback_or_container_bridges():
    for ip in lifecycle.lan_ips():
        assert not ip.startswith("127.") and not ip.startswith("172.17.")


def test_the_command_is_this_interpreter_and_this_project(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    cfg = load(project_dir=str(tmp_path))
    cmd = lifecycle.command(cfg)
    assert cmd.startswith(sys.executable) and "-m swarm_orchestrator" in cmd
    assert str(tmp_path) in cmd and "--pidfile" in cmd and f"--port {cfg.web_port}" in cmd


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not available")
def test_tmux_up_puts_the_board_in_the_last_window(monkeypatch, tmp_path):
    project = tmp_path / "project"
    shutil.copytree(DEMO, project)
    port = _free_port()
    for k, v in {"SWARM_STATE_DIR": str(tmp_path / "state"), "SWARM_DRIVER": "tmux",
                 "SWARM_SESSION": f"swarm-web-{os.getpid()}", "SWARM_SLUG": "webtest",
                 "SWARM_TUI_AUTOSTART": "0", "SWARM_WEB": "1", "SWARM_WEB_PORT": str(port),
                 "SWARM_WEB_HOST": "127.0.0.1"}.items():
        monkeypatch.setenv(k, v)
    cfg = load(project_dir=str(project))
    try:
        session_mod.setup(cfg)
        names = [n for n in tmux.run(["list-windows", "-t", cfg.session, "-F",
                                      "#{window_name}"]).stdout.split("\n") if n]
        # Every index the owner knows stays put; the board comes last.
        assert names[:4] == ["dash", "overseer", "operator", "workers"]
        assert names[-1] == "web"
        assert "web" in state_mod.read(cfg).windows
        assert _wait(lambda: _listening(port)), "the web window never served"
    finally:
        session_mod.teardown(cfg)
    assert _wait(lambda: not _listening(port)), "the board outlived its session"
