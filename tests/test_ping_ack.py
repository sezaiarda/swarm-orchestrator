"""The owner can acknowledge the pings that never reached the phone.

A user could not clear the warning that pings never reached the phone once the
problem was fixed: the footer, the alerts tab and ``swarm doctor``
counted every drop in ``notifications.jsonl`` since the state dir was made, so a
problem fixed days ago stayed red forever. ``swarm notify --ack`` (or ``x`` on
the alerts tab) records a moment; only drops after it count. The ping log itself
is history and is never rewritten. Nothing here reaches the network: every send
goes to a ``SWARM_TG_SINK`` file.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from swarm_orchestrator import cli, doctor, telegram
from swarm_orchestrator.config import load
from swarm_orchestrator.doctor import FAIL, OK, WARN
from swarm_orchestrator.tui import data, drawer, home
from swarm_orchestrator.tui.dash import Dash


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\nP1 needs:P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_SLUG", "pingack")
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    (project / ".swarm.toml").touch()
    c = load(project_dir=str(project))
    c.ensure_dirs()
    return c


def ledger_path(cfg):
    return cfg.state_dir / telegram.LEDGER_NAME


def drop(cfg, ts: float, error: str = "swarm notify FAILED:") -> None:
    with ledger_path(cfg).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "kind": "waiting", "phase": "P0", "text": "t",
                             "delivered": False, "error": error}) + "\n")


def delivered(cfg) -> None:
    telegram.ask(cfg, "fine")  # the sink


def footer(cfg) -> str:
    dash = Dash(cfg)
    dash.poll()
    return home.footer_line(dash, 200)


def sends(cfg):
    return doctor._check_telegram(cfg)[1]


@pytest.fixture
def eleven_old_drops(cfg):
    """The owner's case: drops that stopped, then sends that worked again."""
    old = time.time() - 12 * 3600
    for i in range(11):
        drop(cfg, old + i)
    delivered(cfg)
    return cfg


def test_the_footer_says_how_to_clear_it(eleven_old_drops):
    text = footer(eleven_old_drops)
    assert "11 ping(s) never reached your phone" in text and "x clears" in text


def test_ack_clears_the_footer_the_drawer_and_doctor(eleven_old_drops, capsys):
    cfg = eleven_old_drops
    assert sends(cfg).status in (WARN, FAIL)
    assert cli.main(["--project-dir", str(cfg.project_dir), "notify", "--ack"]) == 0
    assert "acknowledged 11 ping(s)" in capsys.readouterr().out

    assert "never reached" not in footer(cfg)
    dash = Dash(cfg)
    dash.poll()
    assert "NOT DELIVERED" not in drawer.pings_head(dash)
    check = sends(cfg)
    assert check.status == OK and "acknowledged 11 earlier drop(s)" in check.detail


def test_ack_never_rewrites_the_ping_log(eleven_old_drops):
    cfg = eleven_old_drops
    before = ledger_path(cfg).read_bytes()
    cli.cmd_notify_ack(cfg)
    assert ledger_path(cfg).read_bytes() == before
    assert (cfg.state_dir / telegram.ACK_NAME).is_file()
    # The alerts tab's "failed" filter still lists every drop: it is history.
    assert sum(1 for n in data.load_notifications(ledger_path(cfg)) if n.dropped) == 11


def test_a_drop_after_the_ack_counts_again(eleven_old_drops):
    cfg = eleven_old_drops
    telegram.acknowledge(cfg.state_dir, now=time.time() - 60)
    drop(cfg, time.time())
    assert "1 ping(s) never reached your phone" in footer(cfg)
    check = sends(cfg)
    assert check.status == FAIL
    assert "1 send(s) DROPPED since you acknowledged" in check.detail


def test_ack_with_nothing_to_acknowledge_records_nothing(cfg, capsys):
    delivered(cfg)
    assert cli.cmd_notify_ack(cfg) == 0
    assert "no undelivered pings" in capsys.readouterr().out
    assert not (cfg.state_dir / telegram.ACK_NAME).exists()


def test_notify_ack_takes_no_message_and_sends_nothing(cfg, tmp_path):
    assert cli.main(["--project-dir", str(cfg.project_dir), "notify", "--ack", "hi"]) == 2
    assert cli.main(["--project-dir", str(cfg.project_dir), "notify"]) == 2
    assert not (tmp_path / "tg.log").exists()


def test_doctor_warns_not_fails_for_old_drops_once_sends_work_again(eleven_old_drops):
    check = sends(eleven_old_drops)
    assert check.status == WARN
    assert "sends have worked since" in check.detail and "swarm notify --ack" in check.fix_hint


def test_doctor_still_fails_while_sends_keep_failing(cfg):
    delivered(cfg)
    drop(cfg, time.time() - 12 * 3600)  # old, but the newest attempt: still broken
    assert sends(cfg).status == FAIL


def test_a_drop_without_a_time_is_covered_by_an_ack(cfg):
    with ledger_path(cfg).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"delivered": False, "error": "x"}) + "\n")
    rows = doctor._notifications(cfg)
    assert len(telegram.open_drops(rows, 0.0)) == 1
    assert telegram.open_drops(rows, time.time()) == []


def test_x_on_the_alerts_tab_acknowledges(eleven_old_drops, monkeypatch, capfd):
    from swarm_orchestrator.tui.app import SwarmApp

    cfg = eleven_old_drops
    monkeypatch.setattr(Dash, "probe", lambda self, now=None: None)  # no tmux/claude/git
    app = SwarmApp(cfg)
    got: dict = {}

    async def drive():
        async with app.run_test(size=(160, 40)) as pilot:
            app.dash.poll()
            app.refresh_all()
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            tab = app.query_one("#tab-alerts")
            tab.table.focus()
            got["before"] = tab.query_one(".tab-head")._swarm_text
            await pilot.press("x")
            await pilot.pause()
            got["after"] = tab.query_one(".tab-head")._swarm_text

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(drive(), timeout=30))
    assert "11 NOT DELIVERED (x clears)" in got["before"]
    assert "NOT DELIVERED" not in got["after"]
    assert "11 earlier not delivered, acknowledged" in got["after"]
    assert telegram.acked_at(cfg.state_dir) > 0
