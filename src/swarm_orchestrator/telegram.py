"""The one place the swarm messages its owner, and the log of all it had to say.

Two kinds of message reach the owner's phone, each named for its swarm:

    [<swarm>] Asks you: <what you must do or decide, and why>
    [<swarm>] Overseer: <what landed and what is running; whether anything waits on you>

An *ask* (:func:`ask`) goes out when the swarm, one phase or the operator cannot
move forward until the owner does or decides something. The *summary*
(:func:`summary`) goes out on the Overseer's clock (``[overseer].every_s``) and
once more when the run ends. Both are read in a phone notification, so the whole
message, prefix included, is held to :data:`PHONE_MAX` characters and leads with
what matters.

Everything else is *held back*: written to the log and never sent. :func:`fold`
is for what the next summary should account for (the Overseer's digest lists
it), :func:`log` for what only the record needs.

One more thing leaves through here and is not a message the swarm starts:
:func:`reply` answers a command the owner typed to the bot (``/usage``).

Sends go through the swarm's own ``notify.sh`` (``[telegram].notify``). When
``SWARM_TG_SINK`` is set (hermetic tests), they are appended to that file
instead of hitting the network.

Every message — sent, dropped or held back — appends one JSON line to
``<state_dir>/notifications.jsonl``. Before that log existed a dropped ping was
indistinguishable from a healthy one even forensically: ``notify.sh``'s exit code
was the only signal, its stderr was captured and thrown away, and every caller
discards the returned bool. The log is what lets the dashboard (and a
post-mortem) say WHO messaged the owner, WHY, and whether it actually arrived.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

LEDGER_NAME = "notifications.jsonl"

#: The longest message the owner is sent unasked, prefix included. The owner
#: reads a notification, not the chat: one or two sentences, and nothing after
#: them. 280 is two plain sentences of about twenty words each plus the
#: ``[<swarm>] Asks you: `` in front (the size of a classic tweet). A banner
#: shows only the start of a message, so the first sentence has to be the ask
#: itself.
PHONE_MAX = 280

#: What follows the swarm's name, per kind.
ASK_LEAD = "Asks you: "
SUMMARY_LEAD = "Overseer: "

#: A reply to the owner's own command is as long as its answer; Telegram
#: rejects anything over 4096 chars outright, and a rejected send is a
#: *silently* lost message, not a truncated one.
MAX_REPLY_CHARS = 3800
_CUT = "…"

# The ``class`` column: how a row left, or why it did not.
ASK = "ask"
SUMMARY = "summary"
REPLY = "reply"
FOLDED = "folded"
LOGGED = "logged"

#: The ``suppressed`` reason of a folded row, as the dashboard shows it.
FOLD_REASON = "in the Overseer's next summary"
LOG_REASON = "kept in the log only"

# Short slugs for the ``kind`` column, one per thing the swarm has to say.
# Kept here (not at the call sites) so the dashboard has one list to render.
KINDS = (
    "worker-done",  # launch.done: a worker finished fail / blocked
    "operator-abandoned",  # opqueue: the hand-off queue gave up after MAX_ATTEMPTS
    "operator-done",  # cli: an operator job finished — its outcome, or its ask
    "operator-todo",  # launch.done: a hand-off with no operator to run it
    "waiting",  # cli.waiting: a worker, operator job or Overseer pass is blocked on the owner
    "owner-row",  # supervisor: an owner-run row started holding other rows up (once per row)
    "park",  # supervisor: a waiting worker moved to its own window
    "integrate-hold",  # supervisor: merge conflict / dirty tree (push failed: legacy)
    "push-owed",  # pushowed: a repo merged locally but its push failed / was cleared
    "lane-unprepared",  # landing: a worktree could not be made ready for its lane check
    "finish",  # supervisor: the run is over — the last summary
    "master-timeout",  # master: never became ready / prompt would not submit
    "session-ask",  # cli.notify: a session asking the owner for something it cannot do
    "overseer",  # supervisor: an Overseer pass hung past its timeout / would not start
    "summary",  # the Overseer's summary on the clock, or the swarm's in its place
    "bot-reply",  # tgbot: an answer to the owner's /usage or /help
    "worktree-fail",  # launch: the phase mirror could not be created
    "spawn-fail",  # launch: the worker process/pane would not start
    "web-board",  # cli.cmd_up: the LAN board's window/process did not come up
    "usage-cap",  # supervisor: a usage cap paused or stopped the swarm, or a pause lifted
    "blocked",  # supervisor: blocked phases of one burst, one ask
    "drain",  # supervisor: `swarm down --drain` finished waiting and is stopping the swarm
    "idle-build",  # resources.sampler: a build holds a slot with its tree idle
    "restart",  # restart: a `swarm restart` did not happen, or did not come back up
    "other",  # unclassified (the default)
)

#: A failure that is routine once (a master that would not boot, an Overseer
#: pass that ran long) asks the owner when it happens this many times in a row.
STREAK = 3


# -- the two kinds ----------------------------------------------------------
def prefix(cfg) -> str:
    """``[<swarm>] ``: what every message starts with, exactly once."""
    return f"[{cfg.name}] "


def room(cfg, lead: str = ASK_LEAD) -> int:
    """How many characters of its own a message of this kind may carry."""
    return PHONE_MAX - len(prefix(cfg)) - len(lead)


class TooLong(ValueError):
    """A session's ask or summary does not fit a phone notification. The text
    is written for the session that sent it: the limit, and what to do."""


def short(cfg, text: str, lead: str = ASK_LEAD) -> str:
    """``text`` on one line, as a session wrote it for the phone.

    Raises :class:`TooLong` when it is empty or over :func:`room`. It is never
    cut to fit: half a recap explains nothing, and the caller is a session that
    can write two sentences that do.
    """
    text = " ".join((text or "").split())
    limit = room(cfg, lead)
    what = "ask" if lead == ASK_LEAD else "summary"
    if not text:
        raise TooLong(f"the {what} is empty: say what the owner needs to know in one or"
                      f" two short sentences (at most {limit} characters)")
    if len(text) > limit:
        first = ("what you need from them, then why" if lead == ASK_LEAD else
                 "what landed and what is running, then whether anything waits on them")
        raise TooLong(
            f"the {what} is {len(text)} characters and at most {limit} fit: the owner"
            f" reads it in a phone notification ({PHONE_MAX} characters with"
            f" `{prefix(cfg)}{lead}` in front). Rewrite it, do not cut it: {first}, in"
            " one or two short plain sentences. The detail stays where it already is"
            " (your recap, your record, your pane)."
        )
    return text


def clip(text: str, width: int) -> str:
    """A fragment the swarm itself puts in a message (a title, an error, a list
    of ids), cut to ``width``. Never a session's ask: see :func:`short`."""
    text = " ".join(str(text or "").split())
    if len(text) <= width:
        return text
    if width <= len(_CUT):
        return ""
    return text[: width - len(_CUT)].rstrip() + _CUT


def fitted(cfg, before: str, detail: str, after: str = "", lead: str = ASK_LEAD) -> str:
    """``before + detail + after`` with ``detail`` cut to what is left of the
    room, so the fixed words on both sides always arrive whole."""
    return before + clip(detail, room(cfg, lead) - len(before) - len(after)) + after


def names(items, limit: int = 3) -> str:
    """``a, b, c and 4 more``: a list of ids short enough for a notification."""
    items = list(items)
    head = ", ".join(str(i) for i in items[:limit])
    return head + (f" and {len(items) - limit} more" if len(items) > limit else "")


@dataclass(frozen=True)
class NotifyResult:
    """One message: what it said, whether it landed, and why it didn't."""

    delivered: bool
    text: str  # exactly what was sent, or would have been
    error: str | None  # notify.sh's token-redacted API error, or None
    #: Why it was deliberately NOT sent, or None. A held-back message is logged
    #: like any other, so nothing disappears; it just never reaches the phone,
    #: and it is not a drop.
    suppressed: str | None = None


def ask(cfg, text: str, *, kind: str = "other", phase: str | None = None,
        source: str = "", detail: str = "") -> NotifyResult:
    """Send ``[<swarm>] Asks you: <text>``. Never raises.

    For when the swarm, a phase or the operator is stopped, or will stop, on
    something only the owner can do. ``text`` starts with what they must do or
    decide and then says why. A session's own words come through :func:`short`
    first; the swarm's own wording is built to fit (:func:`fitted`), and one
    that still runs over is cut rather than lost.

    ``detail`` is the long form (a recap, an outcome): kept in the log beside
    the ask, for the board and ``swarm todo``, and never sent.
    """
    text = " ".join((text or "").split())
    body = prefix(cfg) + ASK_LEAD + clip(text, room(cfg, ASK_LEAD))
    extra = {"ask": text, **({"detail": detail} if detail else {})}
    return _send(cfg, body, ASK, kind, phase, source, extra=extra)


def summary(cfg, text: str, *, kind: str = "summary", source: str = "") -> NotifyResult:
    """Send ``[<swarm>] Overseer: <text>``: what landed and what is running,
    then whether anything waits on the owner. Never raises."""
    text = " ".join((text or "").split())
    body = prefix(cfg) + SUMMARY_LEAD + clip(text, room(cfg, SUMMARY_LEAD))
    return _send(cfg, body, SUMMARY, kind, None, source)


def reply(cfg, text: str, *, source: str = "") -> NotifyResult:
    """Answer a command the owner typed to the bot. Never raises."""
    body = prefix(cfg) + text
    if len(body) > MAX_REPLY_CHARS:
        body = body[: MAX_REPLY_CHARS - len(_CUT)] + _CUT
    return _send(cfg, body, REPLY, "bot-reply", None, source)


def fold(cfg, text: str, *, kind: str = "other", phase: str | None = None,
         source: str = "") -> NotifyResult:
    """Record ``text`` without sending it; the Overseer's next summary accounts
    for it (its digest lists what was folded since the last one)."""
    return _hold(cfg, text, FOLDED, FOLD_REASON, kind, phase, source)


def log(cfg, text: str, *, why: str = "", kind: str = "other", phase: str | None = None,
        source: str = "") -> NotifyResult:
    """Record ``text`` without sending it: for the record only. ``why`` says
    what makes it no news (the dashboard shows it beside the row)."""
    return _hold(cfg, text, LOGGED, why or LOG_REASON, kind, phase, source)


def _hold(cfg, text: str, cls: str, why: str, kind: str, phase: str | None,
          source: str) -> NotifyResult:
    result = NotifyResult(False, " ".join((text or "").split()), None, why)
    _record(cfg, cls, kind, phase, source, result)
    return result


def _send(cfg, body: str, cls: str, kind: str, phase: str | None, source: str,
          extra: dict | None = None) -> NotifyResult:
    sink = os.environ.get("SWARM_TG_SINK")
    result = _write_sink(sink, body) if sink else _run_script(cfg.telegram_notify, body)
    _record(cfg, cls, kind, phase, source, result, extra)
    return result


def _write_sink(sink: str, text: str) -> NotifyResult:
    try:
        with Path(sink).open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError as exc:
        return NotifyResult(False, text, str(exc))
    return NotifyResult(True, text, None)


def _run_script(script: str, text: str) -> NotifyResult:
    """``notify.sh`` prints a token-redacted API error on stderr; it is kept, so
    a ``400 can't parse entities`` drop never looks like a healthy send."""
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


def _record(cfg, cls: str, kind: str, phase: str | None, source: str,
            result: NotifyResult, extra: dict | None = None) -> None:
    """Append one line to the log (best-effort; never raises).

    One ``json.dumps`` + one append-mode ``write`` per message, so concurrent
    workers interleave whole lines rather than corrupting each other's.
    """
    row = {
        "ts": time.time(),
        "class": cls,
        "kind": kind,
        "phase": phase,
        "source": source,
        "text": result.text,
        "delivered": result.delivered,
        "error": result.error,
        **(extra or {}),
    }
    if result.suppressed:
        row["suppressed"] = result.suppressed
    try:
        path = Path(cfg.state_dir) / LEDGER_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass  # the log is diagnostics: never fail a send over it


# -- reading the log ----------------------------------------------------------
def rows(state_dir: str | Path) -> list[dict]:
    """Every row of the log, oldest first; ``[]`` when there is none."""
    try:
        lines = (Path(state_dir) / LEDGER_NAME).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _ts(row: dict) -> float:
    ts = row.get("ts")
    return float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else 0.0


def recorded(state_dir: str | Path, kinds: tuple[str, ...], phase: str) -> bool:
    """Has anything of one of ``kinds`` been said about ``phase`` before, sent
    or held back? Read from the log, so it holds across processes and restarts."""
    return any(r.get("phase") == phase and r.get("kind") in kinds for r in rows(state_dir))


def last_summary_at(state_dir: str | Path) -> float:
    """When the owner was last sent a summary; ``0.0`` if never."""
    return max((_ts(r) for r in rows(state_dir) if r.get("class") == SUMMARY), default=0.0)


def folded_since(state_dir: str | Path, since: float) -> list[dict]:
    """What was held back for the summary after ``since``, oldest first."""
    return [r for r in rows(state_dir) if r.get("class") == FOLDED and _ts(r) > since]


#: The owner's "I have seen the pings that never arrived": ``{"ts": <epoch>}``.
#: Drops up to that moment stop counting against the run; ``notifications.jsonl``
#: itself is history and is never rewritten.
ACK_NAME = "notifications.ack.json"


def acked_at(state_dir: str | Path) -> float:
    """When the owner last acknowledged the dropped pings; ``0.0`` if never."""
    try:
        data = json.loads((Path(state_dir) / ACK_NAME).read_text(encoding="utf-8"))
        return float(data.get("ts") or 0.0) if isinstance(data, dict) else 0.0
    except (OSError, ValueError, TypeError):
        return 0.0


def acknowledge(state_dir: str | Path, now: float | None = None) -> float:
    """Acknowledge every drop so far; return the moment recorded."""
    now = time.time() if now is None else now
    path = Path(state_dir) / ACK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps({"ts": now}), encoding="utf-8")
    os.replace(tmp, path)
    return now


def unacknowledged(ts: float | None, acked: float) -> bool:
    """Is a ping sent at ``ts`` newer than the acknowledgement? One without a
    time cannot be placed after it, so an acknowledgement covers it."""
    if acked <= 0:
        return True
    return ts is not None and ts > acked


def open_drops(rows: list[dict], acked: float) -> list[dict]:
    """Log rows that were meant to reach the owner, did not, and are not
    acknowledged. A held-back (``suppressed``) message is not a drop."""
    out = []
    for row in rows:
        if row.get("delivered") or row.get("suppressed"):
            continue
        ts = row.get("ts")
        ts = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None
        if unacknowledged(ts, acked):
            out.append(row)
    return out


# -- the sender's own health ---------------------------------------------------
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
