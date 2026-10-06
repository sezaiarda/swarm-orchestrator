"""The machine's bot listener: ``/status``, ``/usage`` and ``/help``, for the owner only.

``notify.sh`` only ever sends. This is the other direction, kept deliberately
small: long-poll ``getUpdates`` for the machine's bot (the token and chat id
its sender reads, :func:`telegram.bot`), answer a command from the owner's chat
through the same sender, and ignore everything else without a word.

**One listener for the machine.** There is one bot whatever the number of
swarms, so there is one listener, and it answers for all of them: ``/status``
is a line per swarm, ``/usage`` the account's figures once and then what is
different for each swarm. It is a machine service (:mod:`service`), like the
web board: any ``swarm up`` starts it when it is absent, every later ``up``
leaves it alone, and the ``swarm down`` of the last swarm that is up stops it.
Its pid, log, offset and status files are in the machine directory. A crash, a
network outage or a rejected token here never touches a supervisor; the
listener backs off and keeps trying.

**One poller per bot token, still.** Telegram answers ``409 Conflict`` when two
programs call ``getUpdates`` on one token (or a webhook is set), and each call
steals the other's updates. The service makes one listener per state root, but
two can still poll one token: a ``swarm telegram-bot serve`` typed by hand, a
second state root (``SWARM_STATE_DIR`` pointed elsewhere), or a listener of
the per-swarm kind left running from before the bot was the machine's. So a
lock keyed by the token's hash keeps a second listener waiting to take over
instead of fighting; a 409 from anything else is logged and backed off.

Stdlib only (``urllib``): nothing here is worth a dependency.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import config as config_mod
from . import machine
from . import service as service_mod
from . import telegram

#: The listener's name as a machine service: ``telegram-bot.pid`` and
#: ``telegram-bot.log`` in the machine directory.
NAME = "telegram-bot"
#: ``{"bot": <token hash>, "offset": N}``: the next update id to ask for.
OFFSET_FILE = "telegram-bot.offset.json"
#: ``{"state", "detail", "ts", "pid"}``: what the listener is doing, for doctor.
STATUS_FILE = "telegram-bot.status.json"
#: The variables it is started with although they are ``SWARM_*``: about the
#: bot itself (where its API is, where a hermetic run's messages go), never
#: about one swarm.
KEEP = ("SWARM_TG_API", "SWARM_TG_SINK")

POLL_S = 50  # the long-poll Telegram holds open when nothing arrives
NET_BACKOFF = (5.0, 300.0)  # network / server errors: first wait, cap
CONFLICT_BACKOFF = (60.0, 600.0)  # 409: another poller holds the token
REJECTED_S = 600.0  # 401/404: the token is wrong; it will not fix itself soon
LOCK_RETRY_S = 60.0  # how often a waiting listener tries to take over
#: A command older than this was sent while no listener ran; answering it now
#: would reply to a question nobody is still asking.
MAX_AGE_S = 15 * 60

COMMANDS = (
    ("status", "every swarm: how it stands, its progress, what waits on you"),
    ("usage", "5-hour and weekly usage, and where a usage cap holds a swarm"),
    ("help", "this list"),
)
HELP = "Swarm bot commands:\n" + "\n".join(f"/{c} — {d}" for c, d in COMMANDS)

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
def env_file() -> Path:
    """The file the machine's sender reads the token and chat id from."""
    return telegram.bot().env


def credentials() -> tuple[str, str] | None:
    """``(token, chat_id)`` from the sender's env file, or ``None``.

    The file is shell (``notify.sh`` sources it): ``KEY=value``, maybe quoted,
    maybe ``export``-ed. Nothing fancier is read.
    """
    try:
        text = env_file().read_text(encoding="utf-8")
    except (OSError, machine.SettingsError):
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
    """Shared by everything on the box: the lock is per bot, not per state root."""
    base = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return Path(base) / f"swarm-tg-bot-{bot_key(token)}.lock"


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


def read_status(mdir: Path, pid: int | None = None) -> dict:
    """The listener's last written state; ``{}`` if ``pid`` did not write it
    (a status left by an earlier listener says nothing about this one)."""
    data = _read_json(Path(mdir) / STATUS_FILE)
    return {} if pid is not None and data.get("pid") != pid else data


# -- the answers -----------------------------------------------------------------
#: Each status of :class:`machine.Swarm` in a plain word.
WORDS = {
    machine.RUNNING: "running",
    machine.PAUSED: "paused",
    machine.HELD: "paused by a usage cap",
    machine.FROZEN: "frozen",
    machine.FINISHED: "finished",
    machine.STOPPED: "down",
}
#: Up first, then the runs that ended by themselves, then the ones taken down.
_RANK = {machine.RUNNING: 0, machine.PAUSED: 0, machine.HELD: 0, machine.FROZEN: 0,
         machine.FINISHED: 1, machine.STOPPED: 2}


def answered(root: Path) -> list[machine.Swarm]:
    """The swarms the bot answers for, in the order ``/status`` lists them:
    the ones waiting on the owner first. A state dir whose project is gone, one
    no supervisor ever ran in, and a swarm with ``[telegram] commands = false``
    are left out."""
    found = [s for s in machine.swarms(root)
             if s.status in WORDS and config_mod.recorded(s.state_dir).get(
                 "telegram_commands", True)]
    return sorted(found, key=lambda s: (s.needs_owner == 0, _RANK[s.status],
                                        s.name.lower(), s.slug))


def _status_line(s: machine.Swarm) -> str:
    n = s.phases
    if s.status == machine.FINISHED:
        line = f"{s.name}: finished"
    elif n is None:
        line = f"{s.name}: {WORDS[s.status]}, progress unknown"
    else:
        line = f"{s.name}: {WORDS[s.status]}, {n['done']} of {n['total']} done"
    if s.needs_owner:
        line += f", {s.needs_owner} wait{'s' if s.needs_owner == 1 else ''} on you"
    return line


def _builds_line(state_dir: Path) -> str | None:
    """``Builds: 1 running, 2 waiting.`` for the machine's one gate, asked
    through any swarm's config; ``None`` when nothing runs or waits."""
    from . import buildlog, buildsem, buildstatus

    cfg = machine.swarm_config(state_dir)
    if cfg is None or cfg.build_max_concurrent < 1 or not cfg.buildsem_dir.is_dir():
        return None
    now = time.time()
    alive = buildstatus.builds(cfg, now)
    busy = sum(1 for h in buildstatus.holders(cfg, buildlog.History(cfg, []), now, alive=alive)
               if h["busy"])
    waiting = sum(1 for t in buildsem.live_tickets(cfg, prune=False) if not t.get("gc"))
    return f"Builds: {busy} running, {waiting} waiting." if busy or waiting else None


def status_text(root: Path) -> str:
    """The answer to ``/status``: one line per swarm, then the build gate."""
    found = answered(root)
    if not found:
        return "No swarms on this machine."
    lines = [_status_line(s) for s in found]
    try:
        builds = _builds_line(found[0].state_dir)
    except Exception:  # noqa: BLE001 - the gate is a footnote; the swarms are the answer
        builds = None
    return "\n".join(lines + ([builds] if builds else []))


def _held(s: machine.Swarm, st, now: float) -> list[str]:
    """What a usage cap does to this swarm right now, one line each: paused
    until a window resets, or stopped until the owner starts it."""
    from . import caps

    if s.running:
        return [f"{s.name}: paused at {caps.label(w)} {h['pct']:.0f}% (cap {h['at']:g}%)"
                f" until {caps.when(h.get('resets_at'), now)}."
                for w, h in sorted(st.usage_hold.items())]
    fired = sorted((key.split(":", 1)[0], at) for key, at in st.usage_fired.items()
                   if at is not None and at > now)
    if not fired or s.finished:
        return []
    window, resets = fired[0]
    return [f"{s.name}: stopped at the {caps.label(window)} cap; down until you run"
            f" swarm up (the window resets {caps.when(resets, now)})."]


def usage_text(root: Path, now: float | None = None) -> str:
    """The answer to ``/usage``. The figures are the account's, so they are
    said once, from every swarm's readings together; then the caps, once when
    every running swarm has the same ones; then each swarm a cap holds."""
    from . import caps
    from . import state as state_mod
    from . import usage as usage_mod

    now = time.time() if now is None else now
    found = answered(root)
    samples = sorted((x for s in found for x in usage_mod.load_samples(
        s.state_dir / usage_mod.METERS_DIR / usage_mod.LIMITS_LOG)), key=lambda x: x.ts)
    limits: dict[str, list[str]] = {}
    held: list[str] = []
    for s in found:
        cfg = machine.swarm_config(s.state_dir)
        if cfg is None:
            continue
        if s.running:
            line = (caps.limits_line(cfg.usage_rules, "{who}") if cfg.usage_enabled
                    else "{who}: usage caps are off.")
            limits.setdefault(line, []).append(s.name)
        held += _held(s, state_mod.read(cfg), now)
    if len(limits) == 1:
        lines = [next(iter(limits)).format(who="Every running swarm")]
    else:
        lines = [line.format(who=", ".join(names)) for line, names in limits.items()]
    return usage_mod.brief(samples, now, lines + held)


def answer_text(root: Path, cmd: str) -> str:
    """The reply to ``/<cmd>``. Never raises: the bot must answer."""
    try:
        if cmd == "status":
            return status_text(root)
        if cmd == "usage":
            return usage_text(root)
    except Exception as exc:  # noqa: BLE001 - diagnostics only, never fatal
        return f"/{cmd} is unavailable right now ({type(exc).__name__}: {exc})"[:300]
    if cmd in ("help", "start"):
        return HELP
    return f"Unknown command /{cmd}. Send /help for the list."



# -- the listener ----------------------------------------------------------------
def command_of(text: str) -> str | None:
    """``/usage@your_swarm_bot extra`` -> ``usage``; plain text -> ``None``."""
    if not text.startswith("/"):
        return None
    word = text.split(maxsplit=1)[0][1:]
    return word.split("@", 1)[0].lower() or None


class Listener:
    """Poll, filter, answer. Every collaborator is injectable, so the tests
    drive it without a network, a clock or a sleep.

    ``root`` is the state root whose swarms it answers for; its files are in
    that root's machine directory."""

    def __init__(self, root: Path, token: str, chat_id: str, *,
                 call: Callable[..., object] = api_call,
                 reply: Callable[[str], object] | None = None,
                 answer: Callable[[str], str] | None = None,
                 log: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.root = Path(root)
        self.mdir = self.root / machine.DIR_NAME
        self.token = token
        self.chat_id = str(chat_id).strip()
        self.call = call
        self.reply = reply or self._send
        self.answer_text = answer or (lambda cmd: answer_text(self.root, cmd))
        self.log = log or _log
        self.clock = clock
        self.key = bot_key(token)
        self.offset = self._load_offset()
        self.failures = 0
        self.conflicts = 0
        self.state: str | None = None

    # -- persistence
    def _load_offset(self) -> int:
        data = _read_json(self.mdir / OFFSET_FILE)
        off = data.get("offset")
        # An offset belongs to one bot: another bot's would skip its updates.
        return off if data.get("bot") == self.key and isinstance(off, int) else 0

    def _save_offset(self) -> None:
        _write_json(self.mdir / OFFSET_FILE,
                    {"bot": self.key, "offset": self.offset})

    def _set_state(self, state: str, detail: str = "") -> None:
        if state != self.state:
            self.log(f"state {state}{': ' + detail if detail else ''}")
        self.state = state
        _write_json(self.mdir / STATUS_FILE,
                    {"state": state, "detail": detail, "ts": self.clock(), "pid": os.getpid()})

    def _send(self, text: str) -> object:
        sent = telegram.reply(text)
        if not sent.delivered:
            self.log(f"reply not delivered: {sent.error}")
        return sent.delivered

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
        return None if cmd is None else self.answer_text(cmd)

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
                            f"TELEGRAM_BOT_TOKEN in {env_file()}; retrying in "
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


def take_lock(token: str, root: Path) -> object | None:
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
    fh.write(json.dumps({"pid": os.getpid(), "root": str(root)}))
    fh.flush()
    return fh


def lock_holder(token: str) -> str:
    data = _read_json(lock_path(token))
    return f"pid {data.get('pid', '?')}, state root {data.get('root', '?')}"


def serve(root: Path, sleep: Callable[[float], None] = time.sleep) -> int:
    """Run the listener for the swarms of ``root`` until SIGTERM/SIGINT.
    Returns a process exit code."""
    creds = credentials()
    if creds is None:
        print(f"swarm telegram-bot: no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID in {env_named()}",
              file=sys.stderr)
        return 1
    token, chat = creds

    def _stop(signum, frame) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    listener = Listener(root, token, chat)
    lock = None
    try:
        _log(f"swarm telegram-bot: answering /status, /usage and /help for every swarm"
             f" in {root}, from chat {chat} only")
        while (lock := take_lock(token, root)) is None:
            listener._set_state(WAITING_LOCK, f"another listener polls this bot "
                                f"({lock_holder(token)}); retrying in {LOCK_RETRY_S:.0f}s")
            sleep(LOCK_RETRY_S)
        listener.run(sleep)
    except KeyboardInterrupt:
        pass
    finally:
        if lock is not None:
            lock.close()
    return 0


def env_named() -> str:
    """The env file, or why none can be named."""
    try:
        return str(env_file())
    except machine.SettingsError as exc:
        return f"(the machine file does not read: {exc})"


# -- lifecycle, as `swarm up` / `down` / `restart` / `doctor` drive it --------------
def the_service(root: Path) -> service_mod.Service:
    """The listener as a machine service: its command line names the state
    root it answers for, because it is started with no swarm's environment."""
    return service_mod.Service(NAME, (NAME, "serve", "--state-root", str(root)),
                               mark=(NAME, "serve"), keep=KEEP)


def _root(state_dir: Path | None = None) -> Path:
    return machine.directory(state_dir).parent.resolve()


def running(state_dir: Path | None = None) -> int | None:
    """The pid of the machine's listener (the machine of ``state_dir``), if it
    is alive."""
    root = _root(state_dir)
    return service_mod.running(the_service(root), root / machine.DIR_NAME)


def ensure(state_dir: Path | None = None) -> tuple[int | None, str]:
    """Start the machine's listener unless it runs. Returns ``(pid, what
    happened)``. Never raises: the listener is a side helper, never the run."""
    root = _root(state_dir)
    if credentials() is None:
        return None, f"not started: no bot token/chat id in {env_named()}"
    try:
        pid, started = service_mod.start(the_service(root), root / machine.DIR_NAME)
    except OSError as exc:
        return None, f"not started: {exc}"
    if not started:
        return pid, f"already running (pid {pid}), for every swarm on this machine"
    return pid, f"listening for /status and /usage (pid {pid}), for every swarm on this machine"


def stop(state_dir: Path | None = None) -> bool:
    """Stop the machine's listener. Returns whether one was running."""
    root = _root(state_dir)
    return service_mod.stop(the_service(root), root / machine.DIR_NAME)


def stop_with_last(state_dir: Path) -> bool:
    """``swarm down``'s half: stop the listener unless a swarm other than the
    one in ``state_dir`` is still up. Returns whether it was stopped."""
    root = _root(state_dir)
    return service_mod.stop_with_last(the_service(root), root / machine.DIR_NAME, state_dir)


def restart(state_dir: Path | None = None) -> bool:
    """Start the listener again, so it runs the code on disk. Returns whether
    one runs now."""
    stop(state_dir)
    return ensure(state_dir)[0] is not None


def log_path(state_dir: Path | None = None) -> Path:
    root = _root(state_dir)
    return service_mod.logfile(the_service(root), root / machine.DIR_NAME)


def machine_line(state_dir: Path | None = None) -> str:
    """One line: whether the machine's listener runs, and what it is doing."""
    pid = running(state_dir)
    if pid is None:
        return ("telegram bot: not running — `swarm up` starts it, or run"
                " `swarm telegram-bot`")
    st = read_status(_root(state_dir) / machine.DIR_NAME, pid)
    state = st.get("state") or "starting"
    detail = f" — {st['detail']}" if st.get("detail") and state != POLLING else ""
    return f"telegram bot: {state} (pid {pid}), for every swarm on this machine{detail}"


def status_line(cfg) -> str:
    """One line for ``swarm status``."""
    if not cfg.telegram_commands:
        return "telegram bot: off for this swarm ([telegram] commands = false)"
    return machine_line(cfg.state_dir)
