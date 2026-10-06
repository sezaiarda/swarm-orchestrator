"""``u`` copies the web board's URL and shows it whole.

Someone reading the dashboard on a phone over ssh and
tmux: at 54 columns the status bar's ``board http://…`` never shows, so one key
copies it to the phone's clipboard (OSC 52) and a toast shows it in full.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from conftest import machine_toml

from swarm_orchestrator.config import load as load_config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("- [ ] a-W1 build\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "copyurl")
    c = load_config(project_dir=str(project))
    c.ensure_dirs()
    c.state_path.write_text(json.dumps({"slots": [], "done": {}, "parked": [],
                                        "last_event_at": time.time()}), encoding="utf-8")
    return c


def test_u_copies_the_board_url_and_shows_it(cfg, monkeypatch, capfd):
    from swarm_orchestrator.tui.app import SwarmApp
    from swarm_orchestrator.tui.dash import Dash

    monkeypatch.setattr(Dash, "probe", lambda self, now=None: None)
    app = SwarmApp(cfg)
    copied: list[str] = []
    shown: list[str] = []
    monkeypatch.setattr(app, "board_url", lambda: "http://100.1.2.3:8780/")
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    monkeypatch.setattr(app, "notify", lambda message, **kw: shown.append(str(message)))

    async def drive():
        async with app.run_test(size=(54, 48)) as pilot:  # the owner's phone
            await pilot.press("u")
            await pilot.pause()

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(drive(), timeout=30))
    assert copied == ["http://100.1.2.3:8780/"]
    assert any("http://100.1.2.3:8780/" in m for m in shown)


def test_the_url_is_this_swarms_page_on_the_machines_board(cfg, monkeypatch):
    """Before the probe has found the board (or with it down) the URL is still
    where this swarm's page will be: the machine's address, then the swarm."""
    from swarm_orchestrator.tui.app import SwarmApp
    from swarm_orchestrator.web import lifecycle

    monkeypatch.setattr(lifecycle, "urls", lambda at: [f"http://100.9.9.9:{at.port}/"])
    machine_toml(web={"port": 8780})
    app = SwarmApp(cfg)
    assert app.board_url() == "http://100.9.9.9:8780/s/state/"
    app.board_link = "http://100.1.2.3:8780/s/state/"  # what the probe found
    assert app.board_url() == "http://100.1.2.3:8780/s/state/"
