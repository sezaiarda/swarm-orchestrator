"""The board hosted by the TUI reads its dash; a standalone one polls, and goes quiet."""

from __future__ import annotations

import time

from swarm_orchestrator.tui import webboard
from swarm_orchestrator.tui.dash import Dash
from swarm_orchestrator.web import feed as feed_mod
from swarm_orchestrator.web.feed import Feed

from test_web_board import make_run


def test_hosted_feed_uses_the_given_dash_and_never_polls_it(tmp_path, monkeypatch):
    cfg = make_run(tmp_path, monkeypatch)
    dash = Dash(cfg)
    dash.poll()
    polls = []
    real = dash._poll
    monkeypatch.setattr(dash, "_poll", lambda: polls.append(1) or real())
    f = Feed(cfg, dash=dash)
    assert f.dash is dash and f.shared
    f.refresh(force=True)
    f.refresh()
    assert polls == []
    assert f.board["columns"]


def test_the_dash_banks_changes_for_a_second_reader(tmp_path, monkeypatch):
    cfg = make_run(tmp_path, monkeypatch)
    dash = Dash(cfg)
    got = dash.poll()
    assert got and dash.take_changes() == got
    assert dash.take_changes() == set()


def test_the_tui_board_passes_its_dash_to_the_server(tmp_path, monkeypatch):
    cfg = make_run(tmp_path, monkeypatch)
    cfg.web_enabled, cfg.web_port, cfg.web_host = True, 0, "127.0.0.1"
    seen = {}

    class Srv:
        server_address = ("127.0.0.1", 1)

        def serve_forever(self, poll_interval=0.5):
            pass

    def make(cfg_, host, port, **kw):
        seen.update(kw)
        return Srv()

    dash = Dash(cfg)
    board = webboard.WebBoard(cfg, make=make, probe=lambda c: ("closed", ""),
                              urls=lambda c: ["http://x:1/"], dash=dash)
    board.ensure()
    assert seen["dash"] is dash


def test_a_standalone_feed_goes_idle_and_wakes_on_a_request(tmp_path, monkeypatch):
    cfg = make_run(tmp_path, monkeypatch)
    f = Feed(cfg)
    assert feed_mod.POLL_S >= 2.0 and not f.idle()
    f._touched = time.monotonic() - feed_mod.IDLE_S - 1
    assert f.idle()
    f.touch()
    assert not f.idle()
    hosted = Feed(cfg, dash=Dash(cfg))
    hosted._touched = 0.0
    assert not hosted.idle()  # the TUI's own dash is the one being polled


def test_an_idle_feed_stops_polling_until_touched(tmp_path, monkeypatch):
    import threading

    cfg = make_run(tmp_path, monkeypatch)
    f = Feed(cfg)
    n = []
    monkeypatch.setattr(f, "refresh", lambda *a, **k: n.append(1) or False)
    f._touched = time.monotonic() - feed_mod.IDLE_S - 1
    t = threading.Thread(target=f.run, kwargs={"poll_s": 0.01}, daemon=True)
    t.start()
    time.sleep(0.3)
    assert n == []
    f.touch()
    time.sleep(0.3)
    assert n
    f.stop()
    t.join(2)
