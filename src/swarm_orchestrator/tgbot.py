"""The swarm bot's command listener: ``/usage`` and ``/help``, for the owner only.

``notify.sh`` only ever sends. This is the other direction, kept deliberately
small: long-poll ``getUpdates`` for the swarm's own bot (the token and chat id
``notify.sh`` reads), answer a command from the owner's chat through the same
sender, and ignore everything else without a word.

It is a side helper, never part of the phase lifecycle. ``swarm up`` starts it as
a detached process of its own (under either driver), ``swarm down`` stops it,
and ``swarm telegram-bot`` runs it in the foreground. A crash, a network outage
or a rejected token here never touches the supervisor; the listener backs off
and keeps trying.

**One poller per bot token.** Telegram answers ``409 Conflict`` when two programs
call ``getUpdates`` on one token (or a webhook is set), and each call steals the
other's updates. Two swarm projects share the bot through this repo's ``.env``,
so a lock keyed by the token's hash keeps a second listener waiting to take
over instead of fighting; a 409 from anything else is logged and backed off.

Stdlib only (``urllib``): nothing here is worth a dependency.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import telegram
from . import usage as usage_mod

PIDFILE = "telegram-bot.pid"
LOG = "telegram-bot.log"
#: ``{"bot": <token hash>, "offset": N}``: the next update id to ask for.
OFFSET_FILE = "telegram-bot.offset.json"
#: ``{"state", "detail", "ts", "pid"}``: what the listener is doing, for doctor.
STATUS_FILE = "telegram-bot.status.json"

POLL_S = 50  # the long-poll Telegram holds open when nothing arrives
NET_BACKOFF = (5.0, 300.0)  # network / server errors: first wait, cap
CONFLICT_BACKOFF = (60.0, 600.0)  # 409: another poller holds the token
REJECTED_S = 600.0  # 401/404: the token is wrong; it will not fix itself soon
LOCK_RETRY_S = 60.0  # how often a waiting listener tries to take over
#: A command older than this was sent while no listener ran; answering it now
#: would reply to a question nobody is still asking.
MAX_AGE_S = 15 * 60

COMMANDS = (
    ("usage", "5-hour and weekly usage, and the usage caps"),
    ("help", "this list"),
)
HELP = "swarm bot commands:\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS)

# States written to the status file.
POLLING = "polling"
CONFLICT = "conflict"
NETWORK = "network-error"
REJECTED = "token-rejected"
WAITING_LOCK = "waiting-for-lock"


class ApiError(Exception):
    """Telegram answered, with an error. ``status`` is its HTTP/error code."""

    def __init__(self, status: int, description: str, retry_after: float | None = None):
        super().__init__(f"{status} {description}")
        self.status = status
        self.description = description
        self.retry_after = retry_after


class NetError(Exception):
    """No usable answer: DNS, connect, timeout, a body that is not JSON."""


def api_base() -> str:
    return os.environ.get("SWARM_TG_API", "https://api.telegram.org").rstrip("/")


def _redact(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


def api_call(token: str, method: str, params: dict, timeout: float) -> object:
    """POST one Bot API method; its ``result``, or :class:`ApiError`/:class:`NetError`.

    Every message is token-redacted: a URL in an exception carries the token.
    """
    body = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (list, dict)) else v for k, v in params.items()}
    ).encode()
    req = urllib.request.Request(f"{api_base()}/bot{token}/{method}", data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read())
        except (OSError, ValueError):
            data = {}
        raise ApiError(exc.code, _redact(str(data.get("description") or exc.reason), token),
                       _retry_after(data)) from None
    except (OSError, ValueError) as exc:  # URLError, timeouts and bad JSON included
        raise NetError(_redact(f"{type(exc).__name__}: {exc}", token)) from None
    if not isinstance(data, dict) or not data.get("ok"):
        data = data if isinstance(data, dict) else {}
        raise ApiError(int(data.get("error_code") or 0),
                       _redact(str(data.get("description") or "not ok"), token),
                       _retry_after(data))
    return data.get("result")


def _retry_after(data: dict) -> float | None:
    value = (data.get("parameters") or {}).get("retry_after")
    return float(value) if isinstance(value, (int, float)) else None


# -- credentials and files ------------------------------------------------------
def env_file(cfg) -> Path:
    """The file ``notify.sh`` reads the token and chat id from."""
    return telegram._sender_env_file(Path(cfg.telegram_notify).expanduser())


def credentials(cfg) -> tuple[str, str] | None:
    """``(token, chat_id)`` from the sender's env file, or ``None``.

    The file is shell (``notify.sh`` sources it): ``KEY=value``, maybe quoted,
    maybe ``export``-ed. Nothing fancier is read.
    """
    try:
        text = env_file(cfg).read_text(encoding="utf-8")
    except OSError:
        return None
    found: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)\s*=\s*(.*)$", line)
        if m:
            found[m.group(1)] = m.group(2).strip().strip("'\"")
    token, chat = found.get("TELEGRAM_BOT_TOKEN", ""), found.get("TELEGRAM_CHAT_ID", "")
    return (token, chat) if token and chat else None


def bot_key(token: str) -> str:
    """A name for the bot that is safe to write down: the token's hash."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def lock_path(token: str) -> Path:
    """Shared by every project on the box: the lock is per bot, not per run."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / f"swarm-tg-bot-{bot_key(token)}.lock"


def pidfile(cfg) -> Path:
    return Path(cfg.state_dir) / PIDFILE


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_status(cfg, pid: int | None = None) -> dict:
    """The listener's last written state; ``{}`` if ``pid`` did not write it
    (a status left by an earlier listener says nothing about this one)."""
    data = _read_json(Path(cfg.state_dir) / STATUS_FILE)
    return {} if pid is not None and data.get("pid") != pid else data


# -- the listener ----------------------------------------------------------------
def command_of(text: str) -> str | None:
    """``/usage@your_swarm_bot extra`` -> ``usage``; plain text -> ``None``."""
    if not text.startswith("/"):
        return None
    word = text.split(maxsplit=1)[0][1:]
    return word.split("@", 1)[0].lower() or None


class Listener:
    """Poll, filter, answer. Every collaborator is injectable, so the tests
    drive it without a network, a clock or a sleep."""

    def __init__(self, cfg, token: str, chat_id: str, *,
                 call: Callable[..., object] = api_call,
                 reply: Callable[[str], object] | None = None,
                 answer_usage: Callable[[], str] | None = None,
                 log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.token = token
        self.chat_id = str(chat_id).strip()
        self.call = call
        self.reply = reply or self._send
        self.answer_usage = answer_usage or (lambda: usage_mod.brief_for(cfg))
        self.log = log or _log
        self.clock = clock
        self.key = bot_key(token)
        self.offset = self._load_offset()
        self.failures = 0
        self.conflicts = 0
        self.state: str | None = None

    # -- persistence
    def _load_offset(self) -> int:
        data = _read_json(Path(self.cfg.state_dir) / OFFSET_FILE)
        off = data.get("offset")
        # An offset belongs to one bot: another bot's would skip its updates.
        return off if data.get("bot") == self.key and isinstance(off, int) else 0

    def _save_offset(self) -> None:
        _write_json(Path(self.cfg.state_dir) / OFFSET_FILE,
                    {"bot": self.key, "offset": self.offset})

    def _set_state(self, state: str, detail: str = "") -> None:
        if state != self.state:
            self.log(f"state {state}{': ' + detail if detail else ''}")
        self.state = state
        _write_json(Path(self.cfg.state_dir) / STATUS_FILE,
                    {"state": state, "detail": detail, "ts": self.clock(), "pid": os.getpid()})

    def _send(self, text: str) -> object:
        return telegram.notify(self.cfg.telegram_notify, text, kind="bot-reply",
                               source="tgbot", state_dir=self.cfg.state_dir)

    # -- one update
    def answer(self, update: dict) -> str | None:
        """The reply to one update, or ``None`` to say nothing.

        Only a message from the owner's chat is ever answered; a stranger who
        finds the bot gets silence, not even an error.
        """
        msg = update.get("message")
        if not isinstance(msg, dict):
            return None
        chat = (msg.get("chat") or {}).get("id")
        if chat is None or str(chat) != self.chat_id:
            return None
        date = msg.get("date")
        if isinstance(date, (int, float)) and self.clock() - date > MAX_AGE_S:
            return None
        cmd = command_of(str(msg.get("text") or ""))
        if cmd is None:
            return None
        if cmd == "usage":
            return self.answer_usage()
        if cmd in ("help", "start"):
            return HELP
        return f"unknown command /{cmd}\n\n{HELP}"

    # -- one poll
    def poll_once(self) -> float:
        """One ``getUpdates``; returns how long to wait before the next.

        The offset moves past an update *before* it is answered, and is saved:
        a listener killed mid-answer never answers the same update twice.
        """
        started = self.clock()
        try:
            updates = self.call(self.token, "getUpdates",
                                {"offset": self.offset, "timeout": POLL_S,
                                 "allowed_updates": ["message"]}, POLL_S + 15)
        except ApiError as exc:
            return self._api_failure(exc)
        except NetError as exc:
            self.failures += 1
            wait = _backoff(NET_BACKOFF, self.failures)
            self._set_state(NETWORK, f"{exc}; retrying in {wait:.0f}s")
            return wait
        self.failures = self.conflicts = 0
        self._set_state(POLLING)
        handled = 0
        for update in updates if isinstance(updates, list) else []:
            uid = update.get("update_id") if isinstance(update, dict) else None
            if not isinstance(uid, int) or uid < self.offset:
                continue
            self.offset = uid + 1
            self._save_offset()
            handled += 1
            try:
                text = self.answer(update)
                if text:
                    self.reply(text)
                    self.log(f"answered update {uid}")
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the loop
                self.log(f"update {uid} failed: {type(exc).__name__}: {exc}")
        # A server that answers at once with nothing (not a real long-poll) must
        # not turn this into a busy loop.
        return 1.0 if not handled and self.clock() - started < 1.0 else 0.0

    def _api_failure(self, exc: ApiError) -> float:
        if exc.status == 409:
            self.conflicts += 1
            wait = _backoff(CONFLICT_BACKOFF, self.conflicts)
            self._set_state(CONFLICT, (
                "409 Conflict: another program is calling getUpdates on this bot token "
                f"(or a webhook is set) — {exc.description}; retrying in {wait:.0f}s"))
            return wait
        if exc.status == 429:
            wait = max(1.0, exc.retry_after or NET_BACKOFF[0])
            self._set_state(NETWORK, f"429 rate-limited; retrying in {wait:.0f}s")
            return wait
        if exc.status in (401, 403, 404):
            self._set_state(REJECTED, f"{exc.status} {exc.description}: check "
                            f"TELEGRAM_BOT_TOKEN in {env_file(self.cfg)}; retrying in "
                            f"{REJECTED_S:.0f}s")
            return REJECTED_S
        self.failures += 1
        wait = _backoff(NET_BACKOFF, self.failures)
        self._set_state(NETWORK, f"{exc}; retrying in {wait:.0f}s")
        return wait

    def register_commands(self) -> None:
        """Best-effort: the command list Telegram offers when the owner types ``/``."""
        try:
            self.call(self.token, "setMyCommands",
                      {"commands": [{"command": c, "description": d} for c, d in COMMANDS]}, 20)
        except (ApiError, NetError) as exc:
            self.log(f"setMyCommands failed (harmless): {exc}")

    def run(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self.register_commands()
        while True:
            wait = self.poll_once()
            if wait > 0:
                sleep(wait)


def _backoff(bounds: tuple[float, float], n: int) -> float:
    first, cap = bounds
    return min(cap, first * 2 ** max(0, n - 1))


def _log(line: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}", flush=True)


def take_lock(token: str, project: str) -> object | None:
    """The per-bot lock, held for the process's life; ``None`` if another holds it."""
    path = lock_path(token)
    try:
        fh = path.open("a+")
    except OSError:
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps({"pid": os.getpid(), "project": project}))
    fh.flush()
    return fh


def lock_holder(token: str) -> str:
    data = _read_json(lock_path(token))
    return f"pid {data.get('pid', '?')}, project {data.get('project', '?')}"


def serve(cfg, pidfile_path: str | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """Run the listener until SIGTERM/SIGINT. Returns a process exit code."""
    creds = credentials(cfg)
    if creds is None:
        print(f"swarm telegram-bot: no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in {env_file(cfg)}",
              file=sys.stderr)
        return 1
    token, chat = creds
    pid_path = Path(pidfile_path) if pidfile_path else None
    if pid_path is not None:
        try:
            pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError:
            pid_path = None

    def _stop(signum, frame) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    listener = Listener(cfg, token, chat)
    lock = None
    try:
        _log(f"swarm telegram-bot for {cfg.project_dir.name}: answering /usage and /help "
             f"from chat {chat} only (state {cfg.state_dir})")
        while (lock := take_lock(token, cfg.project_dir.name)) is None:
            listener._set_state(WAITING_LOCK, f"another listener polls this bot "
                                f"({lock_holder(token)}); retrying in {LOCK_RETRY_S:.0f}s")
            sleep(LOCK_RETRY_S)
        listener.run(sleep)
    except KeyboardInterrupt:
        pass
    finally:
        if lock is not None:
            lock.close()
        if pid_path is not None:
            try:
                pid_path.unlink()
            except OSError:
                pass
    return 0


# -- lifecycle, as `swarm up` / `down` / `doctor` drive it -------------------------
def command(cfg) -> list[str]:
    """The listener's command line: this interpreter, this project."""
    return [sys.executable, "-m", "swarm_orchestrator", "--project-dir", str(cfg.project_dir),
            "telegram-bot", "--pidfile", str(pidfile(cfg))]


def _ours(pid: int) -> bool:
    """Is ``pid`` a live listener (not a recycled pid now running something else)?"""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    return b"swarm_orchestrator" in cmd and b" telegram-bot" in cmd


def running(cfg) -> int | None:
    """The pid of the listener ``up`` started, if it is still alive."""
    try:
        pid = int(pidfile(cfg).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 and _ours(pid) else None


def start_detached(cfg) -> tuple[int | None, str]:
    """Start the listener as its own process. Returns ``(pid, what happened)``.

    Detached under either driver — no tmux window: it has nothing to show, and
    its log is ``<state>/logs/telegram-bot.log``. It carries the run's
    ``SWARM_STATE_DIR``, so ``swarm down``'s reaping finds it like any session.
    """
    if not cfg.telegram_commands:
        return None, "off ([telegram] commands = false)"
    pid = running(cfg)
    if pid is not None:
        return pid, f"already running (pid {pid})"
    if credentials(cfg) is None:
        return None, f"not started: no bot token/chat id in {env_file(cfg)}"
    log_dir = Path(cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SWARM_STATE_DIR": str(cfg.state_dir)}
    try:
        with (log_dir / LOG).open("ab") as log:
            proc = subprocess.Popen(
                command(cfg), cwd=str(cfg.project_dir), env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True,
            )
    except OSError as exc:
        return None, f"not started: {exc}"
    return proc.pid, f"listening for /usage (pid {proc.pid})"


def stop(cfg, timeout: float = 5.0) -> bool:
    """Stop the listener ``up`` started, if it is still running. Returns whether one was."""
    path = pidfile(cfg)
    pid = running(cfg)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _ours(pid):
            time.sleep(0.05)
        if _ours(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        path.unlink()
    except OSError:
        pass
    return pid is not None


def status_line(cfg) -> str:
    """One line for ``swarm status``."""
    if not cfg.telegram_commands:
        return "telegram bot: off ([telegram] commands = false)"
    pid = running(cfg)
    if pid is None:
        return "telegram bot: not running — `swarm up` starts it, or run `swarm telegram-bot`"
    st = read_status(cfg, pid)
    state = st.get("state") or "starting"
    detail = f" — {st['detail']}" if st.get("detail") and state != POLLING else ""
    return f"telegram bot: {state} (pid {pid}){detail}"

