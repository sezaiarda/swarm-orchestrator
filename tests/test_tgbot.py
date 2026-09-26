"""The bot's ``/usage`` answer and its command listener.

The answer (``usage.brief``) is plain and short: both limits, how old the
reading is, and the usage caps. The listener is
driven without a network: its API call, reply and clock are injected, and the
end-to-end ``up``/``down`` test points it at a fake Bot API on loopback.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
import time
from pathlib import Path

import pytest

from swarm_orchestrator import session as session_mod
from swarm_orchestrator import telegram, tgbot
from swarm_orchestrator import usage as usage_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load
from swarm_orchestrator.usage import Sample

# Wednesday 2026-09-23 14:05, local time: every clock string below is local.
NOW = time.mktime((2026, 9, 23, 14, 5, 0, 0, 0, -1))
AT_16 = time.mktime((2026, 9, 23, 16, 0, 0, 0, 0, -1))
SAT_11 = time.mktime((2026, 9, 26, 11, 0, 0, 0, 0, -1))
OWNER = "4242"


def _run(hours: float = 6.2, legacy: bool = False) -> dict:
    return {"hours": hours, "five_pct_per_h": 2.1, "week_pct_per_h": 0.9,
            "phases_finished": 14, "usd_per_h": 11.05, **({"legacy": True} if legacy else {})}


def _sample(ts: float, five=23.0, week=41.0) -> Sample:
    return Sample(ts=ts, five_pct=five, five_resets_at=AT_16, week_pct=week, week_resets_at=SAT_11)


# -- the formatter ---------------------------------------------------------------
def test_brief_is_plain_and_short():
    out = usage_mod.brief([_sample(NOW - 20)], NOW, ["No usage cap reached."])
    assert out.splitlines() == [
        "Weekly 41%, resets Sat 11:00.",
        "5-hour 23%, resets 16:00.",
        "Read at 14:04, just now.",
        "No usage cap reached.",
    ]


def test_brief_says_how_old_its_reading_is():
    assert "Read at 11:05, 3.0 h ago." in usage_mod.brief([_sample(NOW - 3 * 3600)], NOW)


def test_brief_uses_the_newest_sample_and_not_a_window_that_has_reset_since():
    old = Sample(ts=NOW - 7200, five_pct=80.0, five_resets_at=NOW - 60,
                 week_pct=40.0, week_resets_at=SAT_11)
    out = usage_mod.brief([old], NOW)
    assert "5-hour: not known, the window reset at 14:04." in out
    assert "Weekly 40%" in out
    out = usage_mod.brief([old, _sample(NOW - 600, five=3.0)], NOW)
    assert "5-hour 3%" in out and "10 min ago" in out


def test_brief_without_any_sample_is_one_line():
    assert usage_mod.brief([], NOW) == (
        "No usage reading yet. One arrives while a swarm session runs.")


def test_brief_for_includes_the_cap_state_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_USAGE", "1")
    cfg = load(project_dir=str(tmp_path))
    out = usage_mod.brief_for(cfg).splitlines()
    assert out[0].startswith("No usage reading yet")
    assert out[1] == ("The swarm pauses at weekly 60% and 5-hour 90%, and stops at weekly 70%.")
    monkeypatch.setenv("SWARM_USAGE", "0")
    assert usage_mod.brief_for(load(project_dir=str(tmp_path))).endswith("Usage caps are off.")

    def boom(_path):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(usage_mod, "load_samples", boom)
    assert usage_mod.brief_for(cfg) == (
        "Usage is unavailable right now (RuntimeError: disk on fire)")


def test_swarm_usage_says_how_old_its_sample_is():
    cur = {"run_id": "r", "start": NOW - 3600, "hours": 1.0, "max_workers": 1,
           "isolation": "none", "five_windows": 1, "five_used": 2.0, "week_used": 1.0,
           "phases_finished": 0, "phases_failed": 0, "phases_per_h": None, "usd": None,
           "usd_per_h": None} | _run(1.0)
    assert "sample  as of 13:05, 1.0 h ago — stale" in usage_mod.render(
        cur, [], [_sample(NOW - 3600)], NOW)
    assert "sample  none yet" in usage_mod.render(cur, [], [], NOW)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("SWARM_STATE_DIR", str(state))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_MASTER_KIND", raising=False)
    c = load(project_dir=str(tmp_path))
    c.ensure_dirs()
    return c


def _ledger(cfg) -> list[dict]:
    path = cfg.state_dir / telegram.LEDGER_NAME
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_overseers_summary_carries_no_usage_footer(cfg, tmp_path, monkeypatch):
    """Usage reaches the phone only when the owner asks the bot for it."""
    meters = cfg.state_dir / usage_mod.METERS_DIR
    meters.mkdir(parents=True, exist_ok=True)
    row = usage_mod.sample_row(time.time() - 60, None, {"pct": 23, "resets_at": time.time() + 7000},
                               {"pct": 41, "resets_at": time.time() + 86400})
    (meters / usage_mod.LIMITS_LOG).write_text(json.dumps(row) + "\n")
    monkeypatch.setenv("SWARM_MASTER_KIND", "overseer")
    assert cli_main(["--project-dir", str(cfg.project_dir), "notify", "3 done\n1 stuck",
                     "--attention"]) == 0
    assert (tmp_path / "tg.log").read_text() == "3 done\n1 stuck\n"
    assert _ledger(cfg)[-1]["kind"] == "overseer-digest"


def test_a_notify_from_anyone_else_has_no_footer(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_MASTER_KIND", "init")
    assert cli_main(["--project-dir", str(cfg.project_dir), "notify", "hello"]) == 0
    assert (tmp_path / "tg.log").read_text() == "hello\n"
    assert _ledger(cfg)[-1]["kind"] == "master-note"


# -- the listener ----------------------------------------------------------------
class FakeApi:
    """Scripted ``getUpdates`` answers: a list, or an exception to raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, token, method, params, timeout):
        self.calls.append((method, dict(params)))
        if method != "getUpdates":
            return True
        answer = self.answers.pop(0) if self.answers else []
        if isinstance(answer, Exception):
            raise answer
        return answer


def _msg(uid: int, text: str, chat=OWNER, date=None) -> dict:
    return {"update_id": uid, "message": {"message_id": uid, "chat": {"id": int(chat)},
                                          "date": NOW - 5 if date is None else date,
                                          "text": text}}


def _listener(cfg, api, clock=lambda: NOW):
    replies: list[str] = []
    lst = tgbot.Listener(cfg, "123:SECRET", OWNER, call=api, reply=replies.append,
                         answer_usage=lambda: "USAGE-BLOCK", log=lambda _l: None, clock=clock)
    return lst, replies


def test_only_the_owners_chat_is_answered(cfg):
    api = FakeApi([_msg(1, "/usage", chat="999"), _msg(2, "/usage"), _msg(3, "/help"),
                   _msg(4, "/usage@your_swarm_bot now"), _msg(5, "thanks"), _msg(6, "/nope"),
                   _msg(7, "/usage", date=NOW - 3600), {"update_id": 8, "edited_message": {}}])
    lst, replies = _listener(cfg, api)
    lst.poll_once()
    assert replies[0] == "USAGE-BLOCK" and replies[1] == tgbot.HELP
    assert replies[2] == "USAGE-BLOCK" and replies[3].startswith("unknown command /nope")
    assert len(replies) == 4  # stranger, plain text, an old command, an edit: silence
    assert lst.offset == 9


def test_no_update_is_handled_twice(cfg):
    api = FakeApi([_msg(10, "/usage")], [_msg(10, "/usage"), _msg(11, "/usage")])
    lst, replies = _listener(cfg, api)
    lst.poll_once()
    lst.poll_once()
    assert len(replies) == 2 and lst.offset == 12
    assert [c[1]["offset"] for c in api.calls] == [0, 11]
    # The offset outlives the process, and belongs to this bot only.
    again, _ = _listener(cfg, FakeApi())
    assert again.offset == 12
    other = tgbot.Listener(cfg, "999:OTHER", OWNER, call=FakeApi(), log=lambda _l: None)
    assert other.offset == 0


def test_the_offset_moves_before_the_answer(cfg):
    """A reply that blows up is not retried on the next poll."""
    api = FakeApi([_msg(20, "/usage")], [])
    lst = tgbot.Listener(cfg, "123:SECRET", OWNER, call=api, log=lambda _l: None,
                         reply=lambda _t: 1 / 0, answer_usage=lambda: "x", clock=lambda: NOW)
    lst.poll_once()
    assert lst.offset == 21 and api.calls[-1][1]["offset"] == 0
    lst.poll_once()
    assert api.calls[-1][1]["offset"] == 21


def test_409_backs_off_logs_and_recovers(cfg):
    conflict = tgbot.ApiError(409, "Conflict: terminated by other getUpdates request")
    api = FakeApi(*([conflict] * 6), [])
    logged: list[str] = []
    lst = tgbot.Listener(cfg, "123:SECRET", OWNER, call=api, reply=lambda _t: None,
                         log=logged.append, clock=lambda: NOW)
    waits = [lst.poll_once() for _ in range(6)]
    assert waits == [60, 120, 240, 480, 600, 600]
    assert tgbot.read_status(cfg)["state"] == tgbot.CONFLICT
    assert "another program is calling getUpdates" in tgbot.read_status(cfg)["detail"]
    assert sum("409 Conflict" in line for line in logged) == 1  # logged on the change
    lst.poll_once()
    assert tgbot.read_status(cfg)["state"] == tgbot.POLLING and lst.conflicts == 0


def test_errors_never_busy_loop(cfg):
    api = FakeApi(tgbot.NetError("URLError: no route"), tgbot.NetError("again"),
                  tgbot.ApiError(429, "slow down", retry_after=7),
                  tgbot.ApiError(401, "Unauthorized"), tgbot.ApiError(502, "Bad Gateway"), [])
    lst, _ = _listener(cfg, api)
    assert [lst.poll_once() for _ in range(5)] == [5, 10, 7, tgbot.REJECTED_S, 20]
    # A server that answers an empty long-poll at once still gets a pause.
    assert lst.poll_once() == 1.0


def test_api_call_redacts_the_token_and_reads_409(tmp_path, monkeypatch):
    class Conflict(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps({"ok": False, "error_code": 409,
                               "description": "Conflict: terminated by other getUpdates"})
            self.send_response(409)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Conflict)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("SWARM_TG_API", f"http://127.0.0.1:{srv.server_address[1]}")
        with pytest.raises(tgbot.ApiError) as err:
            tgbot.api_call("123:SECRET", "getUpdates", {"offset": 0}, 5)
        assert err.value.status == 409
    finally:
        srv.shutdown()
    monkeypatch.setenv("SWARM_TG_API", f"http://127.0.0.1:{srv.server_address[1]}")
    with pytest.raises(tgbot.NetError) as err:
        tgbot.api_call("123:SECRET", "getUpdates", {}, 2)
    assert "SECRET" not in str(err.value)


def test_one_listener_per_bot_token(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    first = tgbot.take_lock("123:SECRET", "alpha")
    assert first is not None
    assert tgbot.take_lock("123:SECRET", "beta") is None
    assert "project alpha" in tgbot.lock_holder("123:SECRET")
    assert tgbot.take_lock("456:OTHER", "beta") is not None  # another bot is free
    first.close()
    assert tgbot.take_lock("123:SECRET", "beta") is not None


def test_credentials_are_read_as_notify_sh_reads_them(tmp_path, monkeypatch, cfg):
    env = tmp_path / "bot.env"
    env.write_text("# the swarm bot\nexport TELEGRAM_BOT_TOKEN='123:SECRET'\nTELEGRAM_CHAT_ID=4242\n")
    monkeypatch.setenv("SWARM_TG_ENV", str(env))
    assert tgbot.credentials(cfg) == ("123:SECRET", "4242")
    env.write_text("TELEGRAM_BOT_TOKEN=123:SECRET\n")
    assert tgbot.credentials(cfg) is None


# -- lifecycle: `swarm up` starts it, `swarm down` stops it -----------------------
class FakeBotApi(http.server.BaseHTTPRequestHandler):
    """One pending ``/usage`` from the owner and one from a stranger, then quiet."""

    served = False

    def do_POST(self):
        result: object = True
        if self.path.endswith("/getUpdates"):
            if not FakeBotApi.served:
                FakeBotApi.served = True
                result = [_msg(1, "/usage", chat="999", date=time.time()),
                          _msg(2, "/usage", date=time.time())]
            else:
                time.sleep(0.5)
                result = []
        body = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _wait(pred, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_up_starts_the_listener_and_down_stops_it(swarm, tmp_path):
    FakeBotApi.served = False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeBotApi)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env_file = tmp_path / "bot.env"
    env_file.write_text(f"TELEGRAM_BOT_TOKEN=123:SECRET\nTELEGRAM_CHAT_ID={OWNER}\n")
    swarm.env.update({"SWARM_TG_COMMANDS": "1", "SWARM_TG_ENV": str(env_file),
                      "SWARM_TG_API": f"http://127.0.0.1:{srv.server_address[1]}",
                      "XDG_RUNTIME_DIR": str(tmp_path)})
    pidfile = swarm.state_dir / tgbot.PIDFILE
    try:
        out = swarm.up()
        assert "telegram bot: listening for /usage" in out.stdout
        assert _wait(pidfile.exists), "the listener never wrote its pid file"
        pid = int(pidfile.read_text())
        # Answered once, to the owner only; the stranger got nothing.
        assert _wait(lambda: any(line.startswith("No usage reading yet") for line in swarm.tg_lines()))
        time.sleep(1.0)
        assert sum(line.startswith("No usage reading yet") for line in swarm.tg_lines()) == 1
        doctor = json.loads(swarm.cli("doctor", "--json", check=False).stdout)
        bot = next(c for c in doctor if c["name"] == "telegram.bot")
        assert bot["status"] == "ok" and f"pid {pid}" in bot["detail"]
        assert f"telegram bot: polling (pid {pid})" in swarm.cli("status").stdout
        # `down`'s reaping would find it too: it carries the run's state dir.
        cfg = _cfg_for(swarm)
        assert pid in session_mod.session_processes(cfg)
    finally:
        swarm.down()
        srv.shutdown()
    assert _wait(lambda: not Path(f"/proc/{pid}").exists()
                 or b"telegram-bot" not in Path(f"/proc/{pid}/cmdline").read_bytes())
    assert not pidfile.exists()


def _cfg_for(swarm):
    old = os.environ.get("SWARM_STATE_DIR")
    os.environ["SWARM_STATE_DIR"] = str(swarm.state_dir)
    try:
        return load(project_dir=str(swarm.project))
    finally:
        if old is None:
            os.environ.pop("SWARM_STATE_DIR", None)
        else:
            os.environ["SWARM_STATE_DIR"] = old


def test_up_does_not_start_it_when_off_or_without_a_token(swarm, tmp_path):
    swarm.env.update({"SWARM_TG_COMMANDS": "1", "SWARM_TG_ENV": str(tmp_path / "missing.env")})
    out = swarm.up()
    try:
        assert "telegram bot: not started: no bot token/chat id" in out.stdout
        assert not (swarm.state_dir / tgbot.PIDFILE).exists()
        doctor = json.loads(swarm.cli("doctor", "--json", check=False).stdout)
        bot = next(c for c in doctor if c["name"] == "telegram.bot")
        assert bot["status"] == "warn" and "missing.env" in bot["detail"]
    finally:
        swarm.down()
    swarm.env["SWARM_TG_COMMANDS"] = "0"
    out = swarm.up()
    try:
        assert "telegram bot" not in out.stdout
        assert "telegram bot: off" in swarm.cli("status").stdout
    finally:
        swarm.down()
