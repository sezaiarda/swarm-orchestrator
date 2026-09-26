"""The shells tab: what ``swarm keep`` left running, and how to stop it.

The owner wants to *see* the processes a session left behind on purpose and read
the one line saying what each is for. Records are written the way ``keep.start``
writes them, one alive (a real ``sleep``, so liveness goes through ``/proc``
exactly as in production) and one dead, and the checks are the ones a person
would make: the why is there, alive is alive, the stop command is right, and
``x`` asks before it stops anything.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time

import pytest

from swarm_orchestrator import procs
from swarm_orchestrator.config import load as load_config
from swarm_orchestrator.tui import data, tables
from swarm_orchestrator.tui.dash import KEEP_RECHECK_S, Dash


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\nP1 needs:P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "test")
    cfg = load_config(project_dir=str(project))
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def sleeper():
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    yield proc
    proc.kill()
    proc.wait()


def write_record(cfg, name: str, pid: int, ticks, started_at: float, **extra) -> None:
    rec = {
        "name": name, "pid": pid, "start_ticks": ticks, "started_at": started_at,
        "argv": ["bun", "run", "preview", "--port", "4173"], "cwd": "/srv/site",
        "by": "worker:coral-W0", "why": "the mockup page opened on the phone",
        "log": str(cfg.state_dir / "keep" / f"{name}.log"),
    } | extra
    folder = cfg.state_dir / "keep"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps(rec), encoding="utf-8")


@pytest.fixture
def kept(cfg, sleeper):
    """``page`` alive for two hours, ``old`` dead (its pid long gone)."""
    now = time.time()
    write_record(cfg, "page", sleeper.pid, procs.start_ticks(sleeper.pid), now - 7200)
    done = subprocess.Popen(["true"])
    done.wait()
    write_record(cfg, "old", done.pid, 1, now - 3 * 86400, by="owner",
                 why="a tunnel for the demo")
    return now


# -- the pure half ---------------------------------------------------------
def test_rows_carry_why_state_age_owner_and_stop(cfg, kept, sleeper):
    rows = {r["name"]: r for r in data.kept_rows(data.load_kept(cfg), kept)}
    assert list(rows) == ["old", "page"]  # by name

    page = rows["page"]
    assert page["alive"] is True and page["state"] == "alive"
    assert page["pid"] == sleeper.pid
    assert page["why"] == "the mockup page opened on the phone"
    assert page["by"] == "worker:coral-W0"
    assert page["age"] == "2.0h" and page["stale"] is False
    assert page["stop"] == "swarm keep --stop page"
    assert page["command"] == "bun run preview --port 4173"

    old = rows["old"]
    assert old["alive"] is False and old["state"] == "dead"
    assert old["why"] == "a tunnel for the demo" and old["by"] == "owner"
    assert old["age"] == "3.0d"
    assert old["stop"] == "swarm keep --stop old"  # also how a dead record goes


def test_alive_past_a_week_is_stale(cfg, sleeper):
    now = time.time()
    write_record(cfg, "forgotten", sleeper.pid, procs.start_ticks(sleeper.pid), now - 8 * 86400)
    (row,) = data.kept_rows(data.load_kept(cfg), now)
    assert row["stale"] is True


def test_no_keep_dir_and_a_torn_record_cost_nothing(cfg):
    assert data.kept_rows(data.load_kept(cfg)) == []
    (cfg.state_dir / "keep").mkdir()
    (cfg.state_dir / "keep" / "half.json").write_text("{", encoding="utf-8")
    assert data.kept_rows(data.load_kept(cfg)) == []


def test_row_and_detail_say_it_in_colour(cfg, kept):
    rows = {r["name"]: r for r in data.kept_rows(data.load_kept(cfg), kept)}
    live, dead = tables.shell_row(rows["page"]), tables.shell_row(rows["old"])
    assert len(live) == len(tables.SHELL_COLUMNS)
    assert "the mockup page" in live[2] and "alive" in live[3]
    assert "swarm keep --stop page" in live[7]
    assert "dead" in dead[3] and "—" in dead[4]  # no pid for a dead one
    detail = tables.shell_detail(rows["page"])
    for text in ("bun run preview --port 4173", "/srv/site", "page.log", "swarm keep --stop page"):
        assert text in detail


def test_dash_sees_a_new_record_and_a_death(cfg, sleeper):
    dash = Dash(cfg)
    dash.poll()
    assert dash.kept == []
    write_record(cfg, "page", sleeper.pid, procs.start_ticks(sleeper.pid), time.time())
    assert "keep" in dash.poll()
    assert [(r.name, r.alive) for r in dash.kept] == [("page", True)]
    sleeper.kill()
    sleeper.wait()
    dash._kept_at -= KEEP_RECHECK_S  # the periodic re-check is due
    assert "keep" in dash.poll()
    assert [(r.name, r.alive) for r in dash.kept] == [("page", False)]
    assert "keep" not in dash.poll()  # nothing moved since


# -- the widget ------------------------------------------------------------
def _boot(cfg, steps, monkeypatch, capfd):
    from swarm_orchestrator.tui import commands
    from swarm_orchestrator.tui.app import SwarmApp

    monkeypatch.setattr(Dash, "probe", lambda self, now=None: None)  # no tmux/claude/git
    # The parser's own `keep` may not exist in this build; the tab only needs its name.
    real = commands.discover
    monkeypatch.setattr(commands, "discover", lambda parser=None: [
        s for s in real(parser) if s.name != "keep"] + [
        commands.CommandSpec("keep", "leave a process running", "swarm keep")])
    app = SwarmApp(cfg)
    got: dict = {}

    async def drive():
        async with app.run_test(size=(140, 40)) as pilot:
            app.dash.poll()
            app.refresh_all()
            await pilot.pause()
            await steps(app, pilot, got)

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(drive(), timeout=30))
    return got


def test_zero_shows_the_shells_and_x_asks_before_stopping(cfg, kept, monkeypatch, capfd):
    from swarm_orchestrator.tui.commands import ConfirmRun

    async def steps(app, pilot, got):
        await pilot.press("0")
        await pilot.pause()
        tab = app.query_one("#tab-shells")
        tab.table.focus()
        got["cols"] = [str(c.label) for c in tab.table.columns.values()]
        got["rows"] = [r["name"] for r in tab.rows]
        got["head"] = tab.query_one(".tab-head")._swarm_text
        await pilot.press("j")  # old -> page
        await pilot.pause()
        got["detail"] = tab.query_one(".detail-body")._swarm_text
        await pilot.press("x")
        await pilot.pause()
        screen = app.screen
        got["confirm"] = getattr(screen, "line", None) if isinstance(screen, ConfirmRun) else None
        await pilot.press("n")
        await pilot.pause()
        got["tab"] = app.query_one("#tabs").active

    got = _boot(cfg, steps, monkeypatch, capfd)
    assert {"name", "what it is for", "state", "age", "by", "stop it with"} <= set(got["cols"])
    assert got["rows"] == ["old", "page"]
    assert "1 kept process(es) running" in got["head"] and "1 dead record(s)" in got["head"]
    assert "the mockup page opened on the phone" in got["detail"]
    assert got["confirm"] == "swarm keep --stop page"
    assert got["tab"] == "commands"
