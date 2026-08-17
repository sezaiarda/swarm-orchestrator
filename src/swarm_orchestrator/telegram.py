"""Telegram notification wrapper, and the policy for what is worth sending.

Wraps the swarm's own ``notify.sh``. When ``SWARM_TG_SINK`` is set (hermetic tests),
messages are appended to that file instead of hitting the network, so the tests
can assert exactly one final notification without a real bot token.

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

import os
import subprocess
from pathlib import Path

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
    """Send ``message``; return True on success. Never raises."""
    sink = os.environ.get("SWARM_TG_SINK")
    if sink:
        try:
            with Path(sink).open("a", encoding="utf-8") as fh:
                fh.write(message + "\n")
            return True
        except OSError:
            return False
    path = Path(script).expanduser()
    try:
        proc = subprocess.run(
            [str(path), message], capture_output=True, text=True, timeout=30
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check(script: str) -> tuple[bool, str]:
    """Verify telegram is usable (env file + executable notify.sh).

    Returns ``(ok, detail)``. Used by the init master before it relies on being
    able to notify — you cannot telegram that telegram is missing.
    """
    if os.environ.get("SWARM_TG_SINK"):
        return True, "sink"
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
