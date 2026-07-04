"""Telegram notification wrapper.

Wraps the swarm's own ``notify.sh``. When ``SWARM_TG_SINK`` is set (hermetic tests),
messages are appended to that file instead of hitting the network, so the tests
can assert exactly one final notification without a real bot token.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def notify(script: str, message: str) -> bool:
    """Send ``message``; return True on success. Never raises."""
    sink = os.environ.get("SWARM_TG_SINK")
    if sink:
        with Path(sink).open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        return True
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
    env = Path("~/.config/swarm/telegram.env").expanduser()
    if not env.is_file():
        return False, f"telegram env missing: {env}"
    body = env.read_text(encoding="utf-8")
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if var not in body:
            return False, f"{var} not set in {env}"
    return True, "ok"
