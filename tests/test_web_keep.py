"""The board's read-only list of what ``swarm keep`` left running.

The board is open on the LAN, so it says what a kept process is for, whether it
is alive and how to stop it — and not its command line, working dir or log path.
A new record reaches the board within one poll, like every other fast source.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from swarm_orchestrator import procs
from swarm_orchestrator.web.feed import Feed

from test_tui_keep import write_record
from test_web_board import make_run


@pytest.fixture
def sleeper():
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    yield proc
    proc.kill()
    proc.wait()


def test_a_new_kept_process_reaches_the_board_without_its_command(tmp_path, monkeypatch, sleeper):
    cfg = make_run(tmp_path, monkeypatch)
    feed = Feed(cfg)
    feed.refresh(force=True)
    assert feed.board["kept"] == []

    write_record(cfg, "page", sleeper.pid, procs.start_ticks(sleeper.pid), time.time() - 60)
    assert feed.refresh()  # not a slow source: no waiting out SLOW_S
    (kept,) = feed.board["kept"]
    assert kept["name"] == "page" and kept["alive"] is True and kept["pid"] == sleeper.pid
    assert kept["why"] == "the mockup page opened on the phone"
    assert kept["by"] == "worker:coral-W0" and kept["stop"] == "swarm keep --stop page"
    assert not {"command", "cwd", "log", "argv"} & set(kept)
