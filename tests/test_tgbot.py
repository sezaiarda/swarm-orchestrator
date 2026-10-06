"""The machine's bot: its answers, its listener and its lifecycle.

The answers are plain and short: ``/status`` a line per swarm, ``/usage`` the
account's figures once and then what differs per swarm. The listener is driven
without a network: its API call, reply and clock are injected, and the
end-to-end ``up``/``down`` tests point it at a fake Bot API on loopback. One
listener serves every swarm on the machine, survives one swarm's ``down`` and
stops with the last.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from pathlib import Path

import pytest

from conftest import machine_toml

from swarm_orchestrator import machine, procs, telegram, tgbot
from swarm_orchestrator import state as state_mod
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
    (tmp_path / ".swarm.toml").touch()
    c = load(project_dir=str(tmp_path))
    c.ensure_dirs()
    return c


def _ledger(cfg) -> list[dict]:
    path = cfg.state_dir / telegram.LEDGER_NAME
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_what_the_overseer_sends_carries_no_usage_footer(cfg, tmp_path, monkeypatch):
    """Usage reaches the phone only when the owner asks the bot for it."""
    meters = cfg.state_dir / usage_mod.METERS_DIR
    meters.mkdir(parents=True, exist_ok=True)
    row = usage_mod.sample_row(time.time() - 60, None, {"pct": 23, "resets_at": time.time() + 7000},
                               {"pct": 41, "resets_at": time.time() + 86400})
    (meters / usage_mod.LIMITS_LOG).write_text(json.dumps(row) + "\n")
    monkeypatch.setenv("SWARM_MASTER_KIND", "overseer")
    assert cli_main(["--project-dir", str(cfg.project_dir), "notify",
                     "Renew the staging certificate:\ndeploys fail on it."]) == 0
    assert (tmp_path / "tg.log").read_text() == (
        f"[{cfg.name}] Asks you: Renew the staging certificate: deploys fail on it.\n")
    assert _ledger(cfg)[-1]["kind"] == "session-ask"


def test_an_ask_from_anyone_else_has_no_footer(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_MASTER_KIND", "init")
    assert cli_main(["--project-dir", str(cfg.project_dir), "notify", "hello"]) == 0
    assert (tmp_path / "tg.log").read_text() == f"[{cfg.name}] Asks you: hello\n"
    assert _ledger(cfg)[-1]["kind"] == "session-ask"


# -- the answers: one line per swarm, the account once ----------------------------
def _swarm(root: Path, name: str, **flags) -> machine.Swarm:
    project = root / f"p-{name}"
    project.mkdir(parents=True, exist_ok=True)
    return machine.Swarm(slug=f"{name}-1", state_dir=root / f"{name}-1", name=name,
                         project_dir=project, session=name, **flags)


def _phases(done: int, total: int) -> dict:
    return {"done": done, "running": 0, "open": total - done, "total": total}


def test_status_is_a_line_per_swarm_with_those_that_wait_on_you_first(tmp_path, monkeypatch):
    found = [
        _swarm(tmp_path, "alpha", running=True, phases=_phases(3, 12)),
        _swarm(tmp_path, "beta", running=True, paused=True, phases=_phases(5, 8), asking=1,
               todos=1),
        _swarm(tmp_path, "delta", phases=_phases(2, 9)),
        _swarm(tmp_path, "gamma", finished=True, phases=_phases(8, 8)),
        _swarm(tmp_path, "held", running=True, held=True, phases=None),
        _swarm(tmp_path, "omega", phases=_phases(0, 4), todos=1),
        machine.Swarm(slug="gone-1", state_dir=tmp_path / "gone-1", name="gone",
                      project_dir=tmp_path / "no-such-folder", session="", running=True),
        machine.Swarm(slug="empty-1", state_dir=tmp_path / "empty-1", name="empty",
                      project_dir=None, session=""),
    ]
    monkeypatch.setattr(machine, "swarms", lambda root=None: found)
    assert tgbot.status_text(tmp_path).splitlines() == [
        "beta: paused, 5 of 8 done, 2 wait on you",
        "omega: down, 0 of 4 done, 1 waits on you",
        "alpha: running, 3 of 12 done",
        "held: paused by a usage cap, progress unknown",
        "gamma: finished",
        "delta: down, 2 of 9 done",
    ]  # the stale and the empty state dirs are not listed
    monkeypatch.setattr(machine, "swarms", lambda root=None: [])
    assert tgbot.status_text(tmp_path) == "No swarms on this machine."


def _recorded(root: Path, name: str, monkeypatch, toml: str = "", **state) -> Path:
    """A swarm called ``name`` under the state root ``root``, as its supervisor
    left it: ``config.json`` and ``state.json``."""
    project = root.parent / name
    project.mkdir(parents=True)
    (project / ".swarm.toml").write_text(f'[swarm]\nname = "{name}"\n{toml}')
    state_dir = root / f"{name}-1"
    monkeypatch.setenv("SWARM_STATE_DIR", str(state_dir))
    monkeypatch.delenv("SWARM_TG_COMMANDS", raising=False)  # the file says, as for a real one
    try:
        cfg = load(project_dir=str(project))
        cfg.ensure_dirs()
        snap = {k: str(v) if isinstance(v, Path) else v for k, v in vars(cfg).items()
                if k in {f for f in type(cfg).__dataclass_fields__}}
        (state_dir / "config.json").write_text(json.dumps(snap, default=str))
        with state_mod.transaction(cfg) as st:
            for key, value in state.items():
                setattr(st, key, value)
    finally:
        monkeypatch.delenv("SWARM_STATE_DIR")
    return state_dir


def _meter(state_dir: Path, ts: float, week: float, five: float) -> None:
    meters = state_dir / usage_mod.METERS_DIR
    meters.mkdir(parents=True, exist_ok=True)
    row = usage_mod.sample_row(ts, None, {"pct": five, "resets_at": AT_16},
                               {"pct": week, "resets_at": SAT_11})
    with (meters / usage_mod.LIMITS_LOG).open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def test_usage_says_the_account_once_and_then_only_what_differs(tmp_path, monkeypatch):
    root = tmp_path / "state-root"
    monkeypatch.delenv("SWARM_USAGE")  # caps on, as in a real project
    alpha = _recorded(root, "alpha", monkeypatch)
    beta = _recorded(root, "beta", monkeypatch, usage_hold={
        "week": {"at": 60, "pct": 61.0, "resets_at": SAT_11, "since": NOW - 60}})
    _recorded(root, "gamma", monkeypatch, usage_fired={"week:70": SAT_11})
    # The account's readings, as two swarms' sessions saw them: the newest wins.
    _meter(alpha, NOW - 3600, week=58.0, five=40.0)
    _meter(beta, NOW - 120, week=61.0, five=12.0)
    up = {alpha.name, beta.name}
    monkeypatch.setattr(procs, "fifo_has_reader", lambda path: Path(path).parent.name in up)

    assert tgbot.usage_text(root, NOW).splitlines() == [
        "Weekly 61%, resets Sat 11:00.",
        "5-hour 12%, resets 16:00.",
        "Read at 14:03, 2 min ago.",
        "Every running swarm pauses at weekly 60% and 5-hour 90%, and stops at weekly 70%.",
        "beta: paused at weekly 61% (cap 60%) until Sat 11:00.",
        "gamma: stopped at the weekly cap; down until you run swarm up (the window resets"
        " Sat 11:00).",
    ]
    # A running swarm with other caps is named with them.
    _recorded(root, "delta", monkeypatch, '[usage]\nenabled = false\n')
    up.add("delta-1")
    lines = tgbot.usage_text(root, NOW).splitlines()
    assert "delta: usage caps are off." in lines
    assert ("alpha, beta pauses at weekly 60% and 5-hour 90%, and stops at weekly 70%."
            in lines)


def test_a_swarm_with_commands_off_is_not_answered_for(tmp_path, monkeypatch):
    root = tmp_path / "state-root"
    _recorded(root, "alpha", monkeypatch)
    _recorded(root, "quiet", monkeypatch, "[telegram]\ncommands = false\n")
    assert [s.name for s in tgbot.answered(root)] == ["alpha"]
    assert tgbot.status_text(root).startswith("alpha: down, ")


def test_an_answer_never_raises(tmp_path, monkeypatch):
    def boom(_root, *_a):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(tgbot, "status_text", boom)
    assert tgbot.answer_text(tmp_path, "status") == (
        "/status is unavailable right now (RuntimeError: disk on fire)")
    assert tgbot.answer_text(tmp_path, "help") == tgbot.HELP
    assert tgbot.answer_text(tmp_path, "nope") == "Unknown command /nope. Send /help for the list."


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


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "state-root"


def _listener(root, api, clock=lambda: NOW):
    replies: list[str] = []
    lst = tgbot.Listener(root, "123:SECRET", OWNER, call=api, reply=replies.append,
                         answer=lambda cmd: f"ANSWER {cmd}", log=lambda _l: None, clock=clock)
    return lst, replies


def test_only_the_owners_chat_is_answered(root):
    api = FakeApi([_msg(1, "/usage", chat="999"), _msg(2, "/status"), _msg(3, "/help"),
                   _msg(4, "/usage@your_swarm_bot now"), _msg(5, "thanks"), _msg(6, "/nope"),
                   _msg(7, "/usage", date=NOW - 3600), {"update_id": 8, "edited_message": {}}])
    lst, replies = _listener(root, api)
    lst.poll_once()
    assert replies == ["ANSWER status", "ANSWER help", "ANSWER usage", "ANSWER nope"]
    # stranger, plain text, a command older than 15 minutes, an edit: silence
    assert lst.offset == 9


def test_no_update_is_handled_twice(root):
    api = FakeApi([_msg(10, "/usage")], [_msg(10, "/usage"), _msg(11, "/usage")])
    lst, replies = _listener(root, api)
    lst.poll_once()
    lst.poll_once()
    assert len(replies) == 2 and lst.offset == 12
    assert [c[1]["offset"] for c in api.calls] == [0, 11]
    # The offset outlives the process, in the machine directory, and belongs to
    # this bot only.
    assert (root / "machine" / tgbot.OFFSET_FILE).is_file()
    again, _ = _listener(root, FakeApi())
    assert again.offset == 12
    other = tgbot.Listener(root, "999:OTHER", OWNER, call=FakeApi(), log=lambda _l: None)
    assert other.offset == 0


def test_the_offset_moves_before_the_answer(root):
    """A reply that blows up is not retried on the next poll."""
    api = FakeApi([_msg(20, "/usage")], [])
    lst = tgbot.Listener(root, "123:SECRET", OWNER, call=api, log=lambda _l: None,
                         reply=lambda _t: 1 / 0, answer=lambda _c: "x", clock=lambda: NOW)
    lst.poll_once()
    assert lst.offset == 21 and api.calls[-1][1]["offset"] == 0
    lst.poll_once()
    assert api.calls[-1][1]["offset"] == 21


def test_409_backs_off_logs_and_recovers(root):
    conflict = tgbot.ApiError(409, "Conflict: terminated by other getUpdates request")
    api = FakeApi(*([conflict] * 6), [])
    logged: list[str] = []
    lst = tgbot.Listener(root, "123:SECRET", OWNER, call=api, reply=lambda _t: None,
                         log=logged.append, clock=lambda: NOW)
    waits = [lst.poll_once() for _ in range(6)]
    assert waits == [60, 120, 240, 480, 600, 600]
    mdir = root / "machine"
    assert tgbot.read_status(mdir)["state"] == tgbot.CONFLICT
    assert "another program is calling getUpdates" in tgbot.read_status(mdir)["detail"]
    assert sum("409 Conflict" in line for line in logged) == 1  # logged on the change
    lst.poll_once()
    assert tgbot.read_status(mdir)["state"] == tgbot.POLLING and lst.conflicts == 0


def test_errors_never_busy_loop(root):
    api = FakeApi(tgbot.NetError("URLError: no route"), tgbot.NetError("again"),
                  tgbot.ApiError(429, "slow down", retry_after=7),
                  tgbot.ApiError(401, "Unauthorized"), tgbot.ApiError(502, "Bad Gateway"), [])
    lst, _ = _listener(root, api)
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


def test_one_poller_per_bot_token_across_state_roots(tmp_path, monkeypatch):
    """The service makes one listener per state root; a second root, or one
    typed by hand, still waits instead of stealing the first one's updates."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    first = tgbot.take_lock("123:SECRET", tmp_path / "one")
    assert first is not None
    assert tgbot.take_lock("123:SECRET", tmp_path / "two") is None
    assert f"state root {tmp_path / 'one'}" in tgbot.lock_holder("123:SECRET")
    assert tgbot.take_lock("456:OTHER", tmp_path / "two") is not None  # another bot is free
    first.close()
    assert tgbot.take_lock("123:SECRET", tmp_path / "two") is not None


def test_credentials_are_read_from_the_machines_bot(tmp_path, monkeypatch):
    env = tmp_path / "bot.env"
    env.write_text("# the swarm bot\nexport TELEGRAM_BOT_TOKEN='123:SECRET'\nTELEGRAM_CHAT_ID=4242\n")
    monkeypatch.setenv("SWARM_TG_ENV", str(tmp_path / "a-shells-own.env"))  # never read
    machine_toml(telegram={"env": str(env)})
    assert tgbot.credentials() == ("123:SECRET", "4242")
    env.write_text("TELEGRAM_BOT_TOKEN=123:SECRET\n")
    assert tgbot.credentials() is None


# -- lifecycle: one listener for the machine ----------------------------------------
class FakeBotApi(http.server.BaseHTTPRequestHandler):
    """Hands out what a test queues in :attr:`pending`, then holds a quiet poll."""

    pending: list[dict] = []
    lock = threading.Lock()

    def do_POST(self):
        result: object = True
        if self.path.endswith("/getUpdates"):
            with FakeBotApi.lock:
                result, FakeBotApi.pending[:] = list(FakeBotApi.pending), []
            if not result:
                time.sleep(0.3)
        body = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _ask(uid: int, text: str, chat=OWNER) -> None:
    with FakeBotApi.lock:
        FakeBotApi.pending.append(_msg(uid, text, chat=chat, date=time.time()))


def _wait(pred, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def bot_api(tmp_path):
    """The fake Bot API, and the machine's bot pointed at it."""
    FakeBotApi.pending = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeBotApi)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    env_file = tmp_path / "bot.env"
    env_file.write_text(f"TELEGRAM_BOT_TOKEN=123:SECRET\nTELEGRAM_CHAT_ID={OWNER}\n")
    machine_toml(telegram={"env": str(env_file)})
    try:
        yield {"SWARM_TG_COMMANDS": "1", "SWARM_TG_API": f"http://127.0.0.1:{srv.server_address[1]}",
               "XDG_RUNTIME_DIR": str(tmp_path)}
    finally:
        srv.shutdown()


def _gone(pid: int) -> bool:
    try:
        return b"telegram-bot" not in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return True


def _replies(inst, start: str) -> list[str]:
    """The listener's replies in ``inst``'s sink: the lines of each reply that
    begins with ``start``, and the lines after it."""
    lines = inst.tg_lines()
    at = next((i for i, line in enumerate(lines) if line.startswith(start)), None)
    return [] if at is None else lines[at:]


def test_two_swarms_one_listener_that_outlives_one_down_and_stops_with_the_last(
        two_swarms, bot_api):
    a, b = two_swarms
    for inst in (a, b):
        inst.env.update(bot_api, FAKE_WORKER_PARK="1")
    mdir = machine.state_root().resolve() / "machine"
    pidfile = mdir / "telegram-bot.pid"
    try:
        out = a.up()
        assert "telegram bot: listening for /status and /usage" in out.stdout, out.stdout
        assert _wait(pidfile.exists), "the listener never wrote its pid file"
        pid = int(pidfile.read_text())
        assert f"already running (pid {pid})" in b.up().stdout  # one for the machine
        assert a.wait(lambda: a.busy_phases() == ["P0"]) and b.wait(
            lambda: b.busy_phases() == ["P0"])
        b.cli("pause")
        assert b.wait(lambda: b.state()["paused"], timeout=10)

        # /status answers for both, from the one listener, to the owner only.
        _ask(1, "/status", chat="999")
        _ask(2, "/status")
        assert _wait(lambda: len(_replies(a, "alpha: ")) >= 2), a.tg_lines()
        assert _replies(a, "alpha: ")[:2] == ["alpha: running, 0 of 5 done",
                                              "beta: paused, 0 of 5 done"]
        assert not b.tg_lines()  # replies leave through the sender that started it
        _ask(3, "/usage")
        assert _wait(lambda: _replies(a, "No usage reading yet"))
        assert sum(line.startswith("alpha: ") for line in a.tg_lines()) == 1  # answered once

        status = a.cli("status").stdout
        assert f"telegram bot: polling (pid {pid}), for every swarm on this machine" in status
        doctor = json.loads(a.cli("doctor", "--json", check=False).stdout)
        bot = next(c for c in doctor if c["name"] == "telegram.bot")
        assert bot["status"] == "ok" and f"pid {pid}" in bot["detail"]

        # One swarm goes down: the listener stays for the other.
        a.down()
        time.sleep(0.5)
        assert not _gone(pid) and int(pidfile.read_text()) == pid
        _ask(4, "/status")
        assert _wait(lambda: any(line.startswith("alpha: down, ") for line in a.tg_lines()))
    finally:
        b.down()
    # The last one goes down, and the listener with it.
    assert _wait(lambda: _gone(pid))
    assert not pidfile.exists()


def test_up_starts_none_when_off_or_without_a_token(swarm, tmp_path):
    machine_toml(swarm.env, telegram={"env": str(tmp_path / "missing.env")})
    swarm.env.update({"SWARM_TG_COMMANDS": "1"})
    mdir = swarm.state_dir.parent / "machine"
    out = swarm.up()
    try:
        assert "telegram bot: not started: no bot token/chat id" in out.stdout
        assert "missing.env" in out.stdout
        assert not (mdir / "telegram-bot.pid").exists()
        doctor = json.loads(swarm.cli("doctor", "--json", check=False).stdout)
        bot = next(c for c in doctor if c["name"] == "telegram.bot")
        assert bot["status"] == "warn" and "missing.env" in bot["detail"]
    finally:
        swarm.down()
    swarm.env["SWARM_TG_COMMANDS"] = "0"
    out = swarm.up()
    try:
        assert "telegram bot" not in out.stdout
        assert "telegram bot: off for this swarm" in swarm.cli("status").stdout
    finally:
        swarm.down()


def test_the_listener_carries_no_swarms_environment_but_its_own_seams(root):
    svc = tgbot.the_service(root)
    assert svc.argv()[3:] == ["telegram-bot", "serve", "--state-root", str(root)]
    from swarm_orchestrator import service as service_mod

    env = service_mod.clean_env({"SWARM_STATE_DIR": "/x", "SWARM_SLUG": "s",
                                 "SWARM_TG_API": "http://fake", "SWARM_TG_SINK": "/t",
                                 "PATH": "/bin"}, keep=svc.keep)
    assert env == {"SWARM_TG_API": "http://fake", "SWARM_TG_SINK": "/t", "PATH": "/bin"}
