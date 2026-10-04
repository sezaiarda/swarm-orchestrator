"""Usage caps: stop launching workers, or stop the swarm, when a limit runs high.

Readings are the 5-hour and weekly figures the workers' status-line tap logs to
``meters/limits.jsonl`` (see :mod:`usage`). The tap only writes while some
session renders its status line, so a quiet swarm has no fresh figure exactly
when a hold wants one to lift. Then — and at most once per half hour —
the supervisor asks Claude Code's own usage endpoint (:func:`fetch_api`) and logs
the answer as an ordinary sample marked ``src: api``, so every reader sees it.

A rule is ``{window, at, action}``:

* ``pause`` holds new launches while a fresh reading of ``window`` is at or over
  ``at``. The hold is its own record (``State.usage_hold``), separate from
  ``swarm pause``, so lifting it can never undo a pause the owner made. It lifts
  once the window has reset and a fresh reading is under the limit; a reading
  that lags under the same reset cannot lift it early.
* ``down`` stops the swarm the way ``swarm down`` does, once per window.

A stale or missing reading never creates a hold and never lifts one. Workers are
never told any of this: caps act on the swarm, not inside a session.

A limit reset before its window ends (:func:`usage.restarts`) is a reset like
any other: the reading counts from it, and a hold, an override or a ``down``
that fired stands only if the window has reached its limit again since. A
status line cannot show such a reset, so when every fresh reading sits well
under the window's highest the endpoint is asked too.

Everything is per account (:func:`usage.active_account`). Readings are taken
from the samples of the account the swarm runs on now, a hold remembers the
account it was measured on, and when the owner switches accounts a fresh
reading of the new one under the limit lifts it at once: another account's
figure is never a lagging reading of the held window. ``down`` crossings and
overrides are remembered per account too.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import runs, usage

#: Config window name -> (the prefix :class:`usage.Sample` uses, owner-facing label).
WINDOWS = {"week": ("week", "weekly"), "five_hour": ("five", "5-hour")}

API_URL = "https://api.anthropic.com/api/oauth/usage"
API_BETA = "oauth-2025-04-20"
API_TIMEOUT_S = 5.0
#: At most one call to the endpoint this often, whatever ``[usage].stale_s`` says.
API_MIN_GAP_S = 30 * 60
CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


# -- readings -------------------------------------------------------------------
@dataclass(frozen=True)
class Reading:
    pct: float
    resets_at: float | None
    ts: float
    #: When the window was last reset in place (:func:`usage.restarts`), if it was.
    restarted: float | None = None
    #: The highest figure the window has read since it began or was last reset
    #: in place, fresh or not; ``None`` = not known, ``pct`` stands for it.
    top: float | None = None

    @property
    def peak(self) -> float:
        return self.pct if self.top is None else self.top


def _same_window(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= usage.RESET_JUMP_S


def readings(samples: list[usage.Sample], now: float, stale_s: float) -> dict[str, Reading]:
    """The fresh reading of each window: no older than ``stale_s``, window not reset since.

    Sessions render at different moments, so a lagging one can log a lower figure
    right after a higher one under the same reset. The highest of the newest
    window's fresh samples is the reading. A window reset in place
    (:func:`usage.restarts`) counts from that reset only, and a stale snapshot
    of what it read before is no reading.
    """
    out = {}
    marks = usage.restarts(samples)
    for name, (prefix, _) in WINDOWS.items():
        pts = [(s.ts, getattr(s, f"{prefix}_pct"), getattr(s, f"{prefix}_resets_at"), prefix in hit)
               for s, hit in zip(samples, marks) if hit is not None]
        pts = [x for x in pts if x[1] is not None and (x[2] is None or x[2] > now)]
        fresh = [x for x in pts if now - x[0] <= stale_s]
        if not fresh:
            continue
        newest = max((x[2] for x in fresh if x[2] is not None), default=None)
        if newest is not None:
            pts = [x for x in pts if _same_window(x[2], newest)]
        restarted = max((x[0] for x in pts if x[3]), default=None)
        if restarted is not None:
            pts = [x for x in pts if x[0] >= restarted]
        fresh = [x for x in pts if now - x[0] <= stale_s]
        if not fresh:
            continue
        out[name] = Reading(pct=max(x[1] for x in fresh), resets_at=newest,
                            ts=max(x[0] for x in fresh), restarted=restarted,
                            top=max(x[1] for x in pts))
    return out


def needs_api(samples: list[usage.Sample], now: float, stale_s: float) -> bool:
    """The tap cannot say where the account stands: some window has no fresh
    reading, or every fresh one sits well under the window's highest, which is
    a lagging session or a limit reset in place, and only the endpoint knows."""
    reads = readings(samples, now, stale_s)
    return len(reads) < len(WINDOWS) or any(
        r.pct < r.peak - usage.INPLACE_DROP for r in reads.values())


def _epoch(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _api_window(data) -> dict | None:
    if not isinstance(data, dict):
        return None
    pct = data.get("utilization")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    return {"pct": float(pct), "resets_at": _epoch(data.get("resets_at"))}


def _urlopen(req, timeout):
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_api(state_dir, now: float, credentials: Path | None = None) -> tuple[dict | None, str]:
    """Ask Claude Code's usage endpoint once; ``(sample row, note)``.

    The OAuth access token is read from Claude Code's credentials file and used
    only while unexpired. It is never refreshed (a refresh racing Claude Code's
    own can sign it out), never written and never logged: every note here is
    built without it. On any failure the row is ``None`` and the note says why.
    """
    path = CREDENTIALS if credentials is None else credentials
    try:
        oauth = json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except (OSError, ValueError, AttributeError):
        return None, "no Claude login found"
    token, expires = oauth.get("accessToken"), oauth.get("expiresAt")
    if not isinstance(token, str) or not token:
        return None, "no Claude login found"
    if isinstance(expires, (int, float)) and expires / 1000 <= now:
        return None, "the Claude login has expired; any Claude session renews it"
    req = urllib.request.Request(API_URL, headers={
        "Authorization": f"Bearer {token}", "anthropic-beta": API_BETA,
        "Content-Type": "application/json"})
    try:
        with _urlopen(req, API_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, f"the usage endpoint answered HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, f"the usage endpoint did not answer ({type(exc).__name__})"
    if not isinstance(data, dict):
        return None, "the usage endpoint answered in an unexpected shape"
    five, week = _api_window(data.get("five_hour")), _api_window(data.get("seven_day"))
    if five is None and week is None:
        return None, "the usage endpoint answered in an unexpected shape"
    row = usage.sample_row(now, runs.current_id(state_dir), five, week,
                           usage.login_account()) | {"src": "api"}
    return row, "ok"


def record_api(state_dir, row: dict) -> None:
    """Append an API reading to ``limits.jsonl``, where every reader already looks."""
    path = Path(state_dir) / usage.METERS_DIR / usage.LIMITS_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# -- the decision ---------------------------------------------------------------
@dataclass
class Outcome:
    """What one evaluation changed. ``hold``/``fired``/``override`` replace the state's."""

    hold: dict
    fired: dict
    override: dict
    held: list[str] = field(default_factory=list)  # windows newly held
    lifted: list[str] = field(default_factory=list)  # windows released by a reset
    switched: list[str] = field(default_factory=list)  # released: another account is in use
    released: list[str] = field(default_factory=list)  # released by config or an override
    ended: list[str] = field(default_factory=list)  # overrides a reset in place ended
    down: dict | None = None  # the ``down`` rule that crossed: {window, at, pct, resets_at}


def _mine(key: str, account: str | None) -> str:
    """``key`` as ``account`` records it (``week@1a2b3c4d``); plain when unknown."""
    return f"{key}@{account}" if account else key


def overridden(override: dict, window: str, account: str | None, resets_at) -> bool:
    """Did the owner choose to run through ``account``'s ``window`` ending at
    ``resets_at``? Its own entry first, then a plain one (written for an older
    supervisor, or when the account was not known); either must name the window."""
    own = override.get(_mine(window, account)) if account else None
    return _same_window(own, resets_at) or _same_window(override.get(window), resets_at)


def evaluate(rules: list[dict], reads: dict[str, Reading], hold: dict, fired: dict,
             override: dict, now: float, account: str | None = None) -> Outcome:
    """Apply ``rules`` to the fresh ``reads`` of ``account``. Pure: the caller
    persists the result.

    ``hold`` is ``{window: {at, pct, resets_at, since, account}}``; ``fired``
    remembers which ``down`` rule already acted in which window, per account
    (``"week:70@1a2b3c4d" -> resets_at``), so each crossing acts once;
    ``override`` is ``{window: resets_at}`` (and ``{window@account: …}``) from
    ``swarm resume --override-cap`` and silences that window's pause rules until
    it resets. A hold or record without an account predates the tag, and is
    matched by its window's reset alone, as before.

    A window reset in place (``Reading.restarted``) starts over: a hold, an
    override or a fired ``down`` of it stands only while the window has been at
    that limit again since the reset (``Reading.peak``), which a lagging
    reading cannot undo.
    """
    hold = dict(hold)
    override = {w: t for w, t in override.items() if t is not None and t > now}
    fired = {k: t for k, t in fired.items() if t is not None and t > now}
    out = Outcome(hold=hold, fired=fired, override=override)
    for window in WINDOWS:
        r = reads.get(window)
        limits = [x["at"] for x in rules if x["window"] == window and x["action"] == "pause"]
        again = r is not None and r.restarted is not None
        if again and limits and r.peak < min(limits):
            mine = [k for k in {window, _mine(window, account)}
                    if _same_window(override.get(k), r.resets_at)]
            for key in mine:
                del override[key]
            if mine:
                out.ended.append(window)
        for x in rules if again else []:
            if x["window"] == window and x["action"] == "down" and r.peak < x["at"]:
                key = f"{window}:{x['at']:g}"
                for k in {key, _mine(key, account)}:
                    if _same_window(fired.get(k), r.resets_at):
                        del fired[k]
        prev = hold.get(window)
        # Held on another account: nothing that account read says anything here.
        other = prev is not None and None not in (account, prev.get("account")) \
            and prev["account"] != account
        trip = max((at for at in limits if r is not None and r.pct >= at), default=None)
        if trip is not None and not overridden(override, window, account, r.resets_at):
            same = prev is not None and not other and _same_window(
                prev.get("resets_at"), r.resets_at)
            hold[window] = {"at": trip, "pct": max(r.pct, prev["pct"]) if same else r.pct,
                            "resets_at": r.resets_at, "since": prev["since"] if same else now}
            if account:
                hold[window]["account"] = account
            if prev is None or other:
                out.held.append(window)
        elif prev is not None:
            if not any(prev["pct"] >= at for at in limits) or overridden(
                    override, window, prev.get("account"), prev.get("resets_at")):
                del hold[window]
                out.released.append(window)
            elif other and r is not None:
                del hold[window]
                out.switched.append(window)
            elif r is not None and r.resets_at is not None and prev.get("resets_at") is not None \
                    and r.resets_at - prev["resets_at"] > usage.RESET_JUMP_S:
                del hold[window]
                out.lifted.append(window)
            elif again and r.peak < prev["at"] and _same_window(
                    prev.get("resets_at"), r.resets_at):
                del hold[window]
                out.lifted.append(window)
            elif account and prev.get("account") is None and r is not None \
                    and _same_window(prev.get("resets_at"), r.resets_at):
                # A hold from before the tag, read again on its own window:
                # it is this account's.
                hold[window] = prev | {"account": account}
    for rule in rules:
        r = reads.get(rule["window"])
        if rule["action"] != "down" or r is None or r.pct < rule["at"]:
            continue
        key = f"{rule['window']}:{rule['at']:g}"
        if _same_window(fired.get(_mine(key, account)), r.resets_at) or _same_window(
                fired.get(key), r.resets_at):
            continue
        fired[_mine(key, account)] = r.resets_at if r.resets_at is not None else now + 7 * 86400
        if out.down is None:
            out.down = {"window": rule["window"], "at": rule["at"], "pct": r.pct,
                        "resets_at": r.resets_at}
    return out


# -- plain English ----------------------------------------------------------------
def label(window: str) -> str:
    return WINDOWS[window][1]


def when(ts: float | None, now: float) -> str:
    return "at an unknown time" if ts is None else usage._clock(ts, now)


def _on(h: dict) -> str:
    return f" on account {h['account']}" if h.get("account") else ""


def describe_hold(hold: dict, now: float) -> list[str]:
    """One line per held window, for every view of the run."""
    return [f"Paused by usage cap: {label(w)} {h['pct']:.0f}% (limit {h['at']:g}%){_on(h)}. "
            f"Resumes automatically after the reset, {when(h.get('resets_at'), now)}"
            + (", or on a switch to an account under the limit." if h.get("account") else ".")
            for w, h in sorted(hold.items())]


def pause_ping(window: str, h: dict, now: float) -> str:
    return (f"Swarm paused: {label(window)} usage reached {h['pct']:.0f}% (your limit is "
            f"{h['at']:g}%). Running workers finish; no new ones start. It resumes by "
            f"itself after the reset, {when(h.get('resets_at'), now)}. Nothing to do.")


def switch_ping(window: str, reading: Reading | None, account: str | None) -> str:
    now_pct = "" if reading is None else f", whose {label(window)} usage is {reading.pct:.0f}%"
    return (f"Swarm resumed: the {label(window)} cap held another account; Claude is now "
            f"logged in as account {account}{now_pct}. New workers start again.")


def lift_ping(window: str, reading: Reading | None) -> str:
    now_pct = "" if reading is None else f" and usage is now {reading.pct:.0f}%"
    return (f"Swarm resumed: the {label(window)} usage window reset{now_pct}. "
            "New workers start again.")


def down_ping(down: dict, now: float, still_held: bool) -> str:
    msg = (f"Swarm stopped: {label(down['window'])} usage reached {down['pct']:.0f}% "
           f"(your limit {down['at']:g}%). It resets {when(down.get('resets_at'), now)}. "
           "Start it again with swarm up when you want.")
    if still_held:
        msg += (" A usage pause still holds new workers until the reset; "
                "swarm resume --override-cap runs them anyway.")
    return msg


def _pct(reads: dict[str, Reading], window: str) -> str:
    r = reads.get(window)
    return f"{label(window)} {r.pct:.0f}%" if r else f"{label(window)} unknown"


def reading_line(reads: dict[str, Reading], now: float, account: str | None = None) -> str:
    """``Usage now: weekly 48%, 5-hour 11% (as of 14:05, account 1a2b3c4d).``"""
    on = f", account {account}" if account else ""
    if not reads:
        return f"Usage now: unknown (no recent reading{on})."
    newest = max(r.ts for r in reads.values())
    return (f"Usage now: {_pct(reads, 'week')}, {_pct(reads, 'five_hour')} "
            f"(as of {usage._clock(newest, now)}{on}).")


def limits_line(rules: list[dict]) -> str:
    """``The swarm pauses at weekly 60% and 5-hour 90%, and stops at weekly 70%.``"""
    def join(action):
        items = [f"{label(r['window'])} {r['at']:g}%" for r in rules if r["action"] == action]
        return " and ".join(items)

    parts = [f"pauses at {p}" if (p := join("pause")) else "", f"stops at {d}" if (d := join("down")) else ""]
    parts = [p for p in parts if p]
    return f"The swarm {', and '.join(parts)}." if parts else "No usage caps are set."


def summary(cfg, hold: dict, samples: list[usage.Sample], now: float) -> list[str]:
    """The cap state in plain English: the hold, if any, then the reading and the limits."""
    if not getattr(cfg, "usage_enabled", False):
        return ["Usage caps are off."]
    lines = describe_hold(hold, now)
    account = usage.active_account(samples)
    reads = readings(usage.of_account(samples, account), now, cfg.usage_stale_s)
    lines.append(reading_line(reads, now, account))
    if not hold:
        lines.append(limits_line(cfg.usage_rules))
    return lines


def summary_for(cfg, hold: dict, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    samples = usage.load_samples(Path(cfg.state_dir) / usage.METERS_DIR / usage.LIMITS_LOG)
    return summary(cfg, hold, samples, now)
