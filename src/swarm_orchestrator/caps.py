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


def _same_window(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= usage.RESET_JUMP_S


def readings(samples: list[usage.Sample], now: float, stale_s: float) -> dict[str, Reading]:
    """The fresh reading of each window: no older than ``stale_s``, window not reset since.

    Sessions render at different moments, so a lagging one can log a lower figure
    right after a higher one under the same reset. The highest of the newest
    window's fresh samples is the reading.
    """
    out = {}
    for name, (prefix, _) in WINDOWS.items():
        pts = [(s.ts, getattr(s, f"{prefix}_pct"), getattr(s, f"{prefix}_resets_at"))
               for s in samples if now - s.ts <= stale_s]
        pts = [(t, p, r) for t, p, r in pts if p is not None and (r is None or r > now)]
        if not pts:
            continue
        newest = max((r for _, _, r in pts if r is not None), default=None)
        if newest is not None:
            pts = [x for x in pts if _same_window(x[2], newest)]
        out[name] = Reading(pct=max(p for _, p, _ in pts), resets_at=newest,
                            ts=max(t for t, _, _ in pts))
    return out


def needs_api(samples: list[usage.Sample], now: float, stale_s: float) -> bool:
    """No fresh reading of some window: the tap has nothing current to say."""
    return len(readings(samples, now, stale_s)) < len(WINDOWS)


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
    row = usage.sample_row(now, runs.current_id(state_dir), five, week) | {"src": "api"}
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
    released: list[str] = field(default_factory=list)  # released by config or an override
    down: dict | None = None  # the ``down`` rule that crossed: {window, at, pct, resets_at}


def evaluate(rules: list[dict], reads: dict[str, Reading], hold: dict, fired: dict,
             override: dict, now: float) -> Outcome:
    """Apply ``rules`` to the fresh ``reads``. Pure: the caller persists the result.

    ``hold`` is ``{window: {at, pct, resets_at, since}}``; ``fired`` remembers
    which ``down`` rule already acted in which window (``"week:70" -> resets_at``),
    so each crossing acts once; ``override`` is ``{window: resets_at}`` from
    ``swarm resume --override-cap`` and silences that window's pause rules until
    it resets.
    """
    hold = dict(hold)
    override = {w: t for w, t in override.items() if t is not None and t > now}
    fired = {k: t for k, t in fired.items() if t is not None and t > now}
    out = Outcome(hold=hold, fired=fired, override=override)
    for window in WINDOWS:
        r = reads.get(window)
        limits = [x["at"] for x in rules if x["window"] == window and x["action"] == "pause"]
        prev = hold.get(window)
        trip = max((at for at in limits if r is not None and r.pct >= at), default=None)
        if trip is not None and not _same_window(override.get(window), r.resets_at):
            same = prev is not None and _same_window(prev.get("resets_at"), r.resets_at)
            hold[window] = {"at": trip, "pct": max(r.pct, prev["pct"]) if same else r.pct,
                            "resets_at": r.resets_at, "since": prev["since"] if same else now}
            if prev is None:
                out.held.append(window)
        elif prev is not None:
            if not any(prev["pct"] >= at for at in limits) or _same_window(
                    override.get(window), prev.get("resets_at")):
                del hold[window]
                out.released.append(window)
            elif r is not None and r.resets_at is not None and prev.get("resets_at") is not None \
                    and r.resets_at - prev["resets_at"] > usage.RESET_JUMP_S:
                del hold[window]
                out.lifted.append(window)
    for rule in rules:
        r = reads.get(rule["window"])
        if rule["action"] != "down" or r is None or r.pct < rule["at"]:
            continue
        key = f"{rule['window']}:{rule['at']:g}"
        if _same_window(fired.get(key), r.resets_at):
            continue
        fired[key] = r.resets_at if r.resets_at is not None else now + 7 * 86400
        if out.down is None:
            out.down = {"window": rule["window"], "at": rule["at"], "pct": r.pct,
                        "resets_at": r.resets_at}
    return out


# -- plain English ----------------------------------------------------------------
def label(window: str) -> str:
    return WINDOWS[window][1]


def when(ts: float | None, now: float) -> str:
    return "at an unknown time" if ts is None else usage._clock(ts, now)


def describe_hold(hold: dict, now: float) -> list[str]:
    """One line per held window, for every view of the run."""
    return [f"Paused by usage cap: {label(w)} {h['pct']:.0f}% (limit {h['at']:g}%). "
            f"Resumes automatically after the reset, {when(h.get('resets_at'), now)}."
            for w, h in sorted(hold.items())]


def pause_ping(window: str, h: dict, now: float) -> str:
    return (f"Swarm paused: {label(window)} usage reached {h['pct']:.0f}% (your limit is "
            f"{h['at']:g}%). Running workers finish; no new ones start. It resumes by "
            f"itself after the reset, {when(h.get('resets_at'), now)}.")


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


def reading_line(reads: dict[str, Reading], now: float) -> str:
    """``Usage now: weekly 48%, 5-hour 11% (as of 14:05).``"""
    if not reads:
        return "Usage now: unknown (no recent reading)."
    newest = max(r.ts for r in reads.values())
    return (f"Usage now: {_pct(reads, 'week')}, {_pct(reads, 'five_hour')} "
            f"(as of {usage._clock(newest, now)}).")


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
    lines.append(reading_line(readings(samples, now, cfg.usage_stale_s), now))
    if not hold:
        lines.append(limits_line(cfg.usage_rules))
    return lines


def summary_for(cfg, hold: dict, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    samples = usage.load_samples(Path(cfg.state_dir) / usage.METERS_DIR / usage.LIMITS_LOG)
    return summary(cfg, hold, samples, now)
