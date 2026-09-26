"""Telegram notification wrapper + the notification ledger.

Wraps the swarm's own ``notify.sh``. When ``SWARM_TG_SINK`` is set (hermetic
tests), messages are appended to that file instead of hitting the network, so the
tests can assert exactly one final notification without a real bot token.

Every send — sink or wire, delivered or dropped — appends one JSON line to
``<state_dir>/notifications.jsonl``. Before that ledger existed a dropped ping was
indistinguishable from a healthy one even forensically: ``notify.sh``'s exit code
was the only signal, its stderr was captured and thrown away, and every caller
discards the returned bool. The ledger is what lets the dashboard (and a
post-mortem) say WHO pinged the owner, WHY, and whether it actually arrived.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

LEDGER_NAME = "notifications.jsonl"

# Telegram rejects anything over 4096 chars outright; clamp below that. Worker
# prose recaps and the raw git stderr gitq embeds can both blow past it, and a
# rejected send is a *silently* lost message, not a truncated one.
MAX_MESSAGE_CHARS = 3800
_TRUNC_MARK = " ...[truncated]"

# Short slugs for the ``kind`` column, one per reason the swarm pings the owner.
# Kept here (not at the call sites) so the dashboard has one list to render.
KINDS = (
    "worker-done",  # launch.done: a worker finished needs-owner / fail
    "operator-abandoned",  # opqueue: the hand-off queue gave up after MAX_ATTEMPTS
    "operator-ask",  # cli: the operator session is waiting on an owner decision
    "operator-done",  # cli: an operator job finished — its one-line outcome
    "operator-todo",  # launch.done: a hand-off with no operator to run it
    "waiting",  # launch.waiting: a worker is blocked on the owner
    "park",  # supervisor: a waiting worker moved to its own window
    "integrate-hold",  # supervisor: merge conflict / dirty tree (push failed: legacy)
    "push-owed",  # pushowed: a repo merged locally but its push failed / was cleared
    "finish",  # supervisor: the run is over
    "master-timeout",  # master: never became ready / prompt would not submit
    "master-note",  # cli.notify: the master telling the owner about a blocker it cannot decide
    "overseer",  # supervisor: an Overseer pass hung past its timeout / would not start
    "overseer-ask",  # cli: the Overseer is waiting on an owner-level decision
    "overseer-digest",  # cli.notify: the Overseer's summary to the owner (+ usage footer)
    "bot-reply",  # tgbot: an answer to the owner's /usage or /help
    "worktree-fail",  # launch: the phase mirror could not be created
    "spawn-fail",  # launch: the worker process/pane would not start
    "web-board",  # cli.cmd_up: the LAN board's window/process did not come up
    "other",  # unclassified (the default)
)


@dataclass(frozen=True)
class NotifyResult:
    """One send attempt: what went out, whether it landed, and why it didn't."""

    delivered: bool
    text: str  # exactly what was sent (post-truncation)
    error: str | None  # notify.sh's token-redacted API error, or None
    #: Why it was deliberately NOT sent (a quiet-pings policy), or None. A
    #: suppressed message is logged like any other, so nothing disappears; it
    #: just never reaches the phone, and it is not a drop.
    suppressed: str | None = None


def notify(
    script: str,
    message: str,
    *,
    kind: str = "other",
    phase: str | None = None,
    source: str = "",
    state_dir: str | Path | None = None,
    suppressed: str | None = None,
) -> bool:
    """Send ``message``; return True on success. Never raises.

    The bool-returning face of :func:`notify_detail`, kept so existing call sites
    (which only ever branch on success) work unchanged. ``kind``/``phase``/
    ``source`` are ledger metadata only — they never reach the owner's phone.
    ``suppressed`` (a reason) records the message without sending it.
    """
    return notify_detail(
        script, message, kind=kind, phase=phase, source=source, state_dir=state_dir,
        suppressed=suppressed,
    ).delivered


def notify_detail(
    script: str,
    message: str,
    *,
    kind: str = "other",
    phase: str | None = None,
    source: str = "",
    state_dir: str | Path | None = None,
    suppressed: str | None = None,
) -> NotifyResult:
    """Send ``message`` and report *why* it failed. Never raises.

    ``notify.sh`` prints a token-redacted API error on stderr; it used to be
    captured and discarded, which is how a ``400 can't parse entities`` drop
    looked exactly like a healthy send to every caller. It is now returned to the
    caller *and* recorded in the ledger, whatever the caller does with it.

    With ``suppressed`` set (the reason), nothing is sent: the message goes to
    the ledger only, ``delivered: false`` with that reason, so the dashboard's
    alerts still show it and nothing reads it as a drop.
    """
    text = _clamp(message)
    if suppressed:
        result = NotifyResult(False, text, None, suppressed)
        _record(state_dir, kind, phase, source, result)
        return result
    sink = os.environ.get("SWARM_TG_SINK")
    result = _write_sink(sink, text) if sink else _run_script(script, text)
    _record(state_dir, kind, phase, source, result)
    return result


def with_footer(message: str, footer: str) -> str:
    """``message`` then ``footer``, the message cut so the whole fits the cap.

    :func:`_clamp` cuts from the end, which is exactly where a footer sits: a
    long message would lose it first. Here the message gives way instead.
    """
    footer = footer.strip()
    if not footer:
        return message
    room = MAX_MESSAGE_CHARS - len(footer) - 2
    if len(message) > room:
        message = message[: max(0, room - len(_TRUNC_MARK))] + _TRUNC_MARK
    return f"{message}\n\n{footer}"


def _clamp(message: str) -> str:
    if len(message) <= MAX_MESSAGE_CHARS:
        return message
    return message[: MAX_MESSAGE_CHARS - len(_TRUNC_MARK)] + _TRUNC_MARK


def _write_sink(sink: str, text: str) -> NotifyResult:
    try:
        with Path(sink).open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError as exc:
        return NotifyResult(False, text, str(exc))
    return NotifyResult(True, text, None)


def _run_script(script: str, text: str) -> NotifyResult:
    path = Path(script).expanduser()
    try:
        proc = subprocess.run(
            [str(path), text], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return NotifyResult(False, text, str(exc))
    if proc.returncode == 0:
        return NotifyResult(True, text, None)
    detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
    return NotifyResult(False, text, detail[:400])


def _record(
    state_dir: str | Path | None,
    kind: str,
    phase: str | None,
    source: str,
    result: NotifyResult,
) -> None:
    """Append one line to the notification ledger (best-effort; never raises).

    One ``json.dumps`` + one append-mode ``write`` per send, so concurrent workers
    interleave whole lines rather than corrupting each other's.
    """
    path = _ledger_path(state_dir)
    if path is None:
        return
    row = {
        "ts": time.time(),
        "kind": kind,
        "phase": phase,
        "source": source,
        "text": result.text,
        "delivered": result.delivered,
        "error": result.error,
    }
    if result.suppressed:
        row["suppressed"] = result.suppressed
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass  # the ledger is diagnostics: never fail a send over it


def _ledger_path(state_dir: str | Path | None) -> Path | None:
    """Where the ledger lives, resolved the way every swarm process already does.

    The caller's own ``cfg.state_dir`` when it has one; else ``SWARM_STATE_DIR``
    (exported onto every worker, master and resolver pane); else the project
    config for the cwd. ``None`` when nothing resolves — a send from outside any
    run is still sent, just not logged.
    """
    if state_dir is not None:
        return Path(state_dir) / LEDGER_NAME
    env = os.environ.get("SWARM_STATE_DIR")
    if env:
        return Path(env).expanduser() / LEDGER_NAME
    try:
        from .config import load

        return load().state_dir / LEDGER_NAME
    except (OSError, ValueError):
        return None


def _sender_env_file(script: Path) -> Path:
    """The env file ``notify.sh`` will actually read.

    ``$SWARM_TG_ENV`` else ``.env`` in the repo root above the script — the same
    resolution the script does with ``dirname "$BASH_SOURCE"/..``.
    """
    override = os.environ.get("SWARM_TG_ENV")
    if override:
        return Path(override).expanduser()
    return script.resolve().parent.parent / ".env"


def check(script: str) -> tuple[bool, str]:
    """Verify telegram is usable (env file + executable notify.sh).

    Returns ``(ok, detail)``. Used before the swarm relies on being able to notify
    — you cannot telegram that telegram is missing.

    It validates the env file *the sender reads* (see :func:`_sender_env_file`),
    so it reports on the swarm's own bot and no other.
    """
    if os.environ.get("SWARM_TG_SINK"):
        return True, "sink"
    path = Path(script).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        return False, f"notify.sh missing or not executable: {path}"
    env = _sender_env_file(path)
    if not env.is_file():
        return False, f"telegram env missing: {env}"
    body = env.read_text(encoding="utf-8")
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if var not in body:
            return False, f"{var} not set in {env}"
    return True, "ok"
