"""Telegram notification, and the policy for what is worth sending.

Credentials live in a gitignored ``.env`` **in this repo**, and messages go
straight to the Bot API over stdlib HTTP — no shell script, no user-level channel
directory, nothing outside the checkout. Clone, drop a ``.env``, and the swarm can
talk. An external ``notify`` script is still honoured as a fallback for anyone
configured the old way. When ``SWARM_TG_SINK`` is set (hermetic tests), messages
are appended to that file instead of hitting the network, so the tests can assert
exactly one notification without a real bot token.

**Notification policy.** A swarm run generates a great deal
of true, interesting, machine-generated status. Almost none of it is worth an
owner's attention, and a channel that carries all of it trains them to ignore the
channel — at which point the two messages that *did* matter are lost with the rest.

So exactly two kinds reach the owner:

* :data:`QUESTION` — a worker needs a decision only the owner can make. **One
  message per question**, sent by the supervisor when the worker self-reports via
  ``swarm waiting``, so a question cannot be announced twice by two layers.
* :data:`FINISHED` — the run is over, with a summary of what happened.

Everything else — merge conflicts (a resolver handles them), parks, master-pane
faults, per-phase outcomes — is :func:`notify_event`, which **logs and drops**.
Those states remain fully visible in ``swarm why``, ``swarm status`` and the
supervisor log; they are simply not push notifications. Suppression is deliberate:
if a dropped state later proves to need the owner, surface it in the FINISHED
summary rather than adding a third notification kind.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: Where the bot credentials live. In the repo (gitignored), not in a user-level
#: channel directory, so the notifier is self-contained: clone, drop a `.env`,
#: and swarm can talk. Override with ``SWARM_TG_ENV`` for tests or a second bot.
_ENV_FILENAME = ".env"
_API = "https://api.telegram.org"


def _env_file() -> Path:
    """The `.env` the notifier reads, whether or not it exists yet."""
    override = os.environ.get("SWARM_TG_ENV")
    if override:
        return Path(override).expanduser()
    # …/src/swarm_orchestrator/telegram.py -> repo root
    return Path(__file__).resolve().parents[2] / _ENV_FILENAME


def _load_env() -> dict[str, str]:
    """Parse the `.env` into a dict. Missing file or junk lines are not errors —
    a notifier that raises while reporting a problem is worse than a silent one."""
    path = _env_file()
    out: dict[str, str] = {}
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip("\"'")
    return out


def _api(token: str, method: str, params: dict | None = None) -> dict | None:
    """Call one Telegram Bot API method. Returns the decoded body, or None."""
    url = f"{_API}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _persist_chat_id(chat_id: str) -> None:
    path = _env_file()
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"TELEGRAM_CHAT_ID={chat_id}\n")
        path.chmod(0o600)
    except OSError:
        pass


def _discover_chat_id(token: str) -> str | None:
    """Learn the chat id from whatever the bot has already received.

    Telegram will not reveal a chat id until the bot has been messaged, so a
    fresh bot cannot be configured entirely up front. Rather than require a
    babysitting process to be alive at the right moment, the first notification
    that finds no chat id looks one up and writes it down. The owner's only
    obligation is to message the bot once, whenever they like.
    """
    body = _api(token, "getUpdates")
    if not body or not body.get("ok"):
        return None
    for update in reversed(body.get("result") or []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = (message.get("chat") or {}).get("id")
        if chat is not None:
            return str(chat)
    return None


def _send_via_api(message: str) -> bool | None:
    """Send through the in-repo bot config. None = not configured, fall back."""
    env = _load_env()
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    chat_id = env.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        chat_id = _discover_chat_id(token)
        if not chat_id:
            return False  # configured but unreachable: message the bot once
        _persist_chat_id(chat_id)
    # Plain text, never parse_mode: swarm messages carry backticks, underscores
    # and <phase> placeholders that a Markdown/HTML parser mangles or rejects.
    body = _api(
        token,
        "sendMessage",
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"},
    )
    return bool(body and body.get("ok"))

#: A worker needs a decision only the owner can make.
QUESTION = "question"
#: The run is over.
FINISHED = "finished"

_OWNER_KINDS = frozenset({QUESTION, FINISHED})


def notify_owner(script: str, kind: str, message: str, log=None) -> bool:
    """Send ``message`` only if ``kind`` is owner-actionable; otherwise drop it.

    ``kind`` must be :data:`QUESTION` or :data:`FINISHED`. Anything else is
    recorded on ``log`` (when given) and not sent. Returns True only if a message
    actually went out.
    """
    if kind not in _OWNER_KINDS:
        if log is not None:
            log.line(f"NOTIFY-SUPPRESSED {kind}")
        return False
    return notify(script, message)


def notify_event(kind: str, message: str, log=None) -> bool:
    """Record a run event that is deliberately *not* pushed to the owner.

    Exists so suppression is explicit and greppable at the call site rather than
    an absence of code. The message still reaches the supervisor log.
    """
    if log is not None:
        log.line(f"NOTIFY-SUPPRESSED {kind}: {message}")
    return False


def notify(script: str, message: str) -> bool:
    """Send ``message``; return True on success. Never raises.

    Order: the test sink, then this repo's own bot (``.env``), then the external
    ``script`` for setups that predate the in-repo config.
    """
    sink = os.environ.get("SWARM_TG_SINK")
    if sink:
        try:
            with Path(sink).open("a", encoding="utf-8") as fh:
                fh.write(message + "\n")
            return True
        except OSError:
            return False
    sent = _send_via_api(message)
    if sent is not None:
        return sent
    path = Path(script).expanduser()
    try:
        proc = subprocess.run(
            [str(path), message], capture_output=True, text=True, timeout=30
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check(script: str) -> tuple[bool, str]:
    """Verify telegram is usable. Returns ``(ok, detail)``.

    Used by the init master before it relies on being able to notify — you cannot
    telegram that telegram is missing. The in-repo bot is checked first; a token
    with no chat id yet is still OK, because the id is discovered on first send.
    """
    if os.environ.get("SWARM_TG_SINK"):
        return True, "sink"
    env = _load_env()
    if env.get("TELEGRAM_BOT_TOKEN"):
        if env.get("TELEGRAM_CHAT_ID"):
            return True, f"in-repo bot ({_env_file()})"
        return True, "in-repo bot (chat id resolves on first send — message the bot once)"
    path = Path(script).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        return False, f"notify.sh missing or not executable: {path}"
    # The credentials live beside the script, whichever channel it belongs to —
    # hardcoding one channel's path made this check fail for any other
    # bot (the swarm now has its own, so the run would report telegram broken
    # while it worked perfectly).
    env = path.parent / ".env"
    if not env.is_file():
        return False, f"telegram env missing: {env}"
    body = env.read_text(encoding="utf-8")
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if var not in body:
            return False, f"{var} not set in {env}"
    return True, "ok"
