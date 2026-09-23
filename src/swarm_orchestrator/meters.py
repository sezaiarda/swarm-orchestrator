"""A worker's status-line tap: keep what Claude Code already tells its status bar.

Every render, Claude Code hands its ``statusLine`` command a JSON payload with the
exact context size, the session's API-equivalent cost and — for subscribers —
the 5-hour and weekly limit utilisation with their reset times. The dashboard
had none of it: context came from scraping ``420k/1.0M`` off a pane, cost was
reconstructed after the fact by transcript mining, and the weekly limit was
something a worker discovered by hitting it (a phase then sat idle until
the owner stepped in).

So each worker's settings carry this module as its status line
(:func:`settings_with_tap`). It writes ``<state>/meters/<phase>.json`` and, when
the 5-hour or weekly figure moves, appends a sample tagged with the open run to
``meters/limits.jsonl`` (see :mod:`swarm_orchestrator.usage`) — then runs
the owner's own status line on the same payload and prints what it prints, so a
pane looks exactly as it did and the pane-scraped meter keeps working.

Contract, as for ``scripts/stop-hook.py``: never fail, never block for long,
stdlib only (it starts a Python per render).

It runs several times a second per streaming worker, so it does as little as it
can: the meter file is rewritten only when a figure moved, and at most once per
:data:`MIN_WRITE_S` (the dashboard polls every two seconds anyway); the owner's
status-line command is resolved once, at launch, and handed over on the command
line instead of re-parsing ``~/.claude/settings.json`` on every render.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from . import runs, usage

METERS_DIR = usage.METERS_DIR
LIMITS_LOG = usage.LIMITS_LOG
SESSIONS_LOG = usage.SESSIONS_LOG
#: The owner's status line gets this long before the tap prints its own line.
CHAIN_TIMEOUT_S = 2.0
#: At most one meter write per phase this often; a skipped render is picked up
#: by the next one. The dashboard's own tick is two seconds.
MIN_WRITE_S = 2.0
#: Fields that change on every render without meaning anything moved.
_VOLATILE = ("ts", "duration_ms")


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _window(limits: dict, name: str) -> dict | None:
    w = limits.get(name) if isinstance(limits, dict) else None
    if not isinstance(w, dict) or _num(w.get("used_percentage")) is None:
        return None
    return {"pct": _num(w.get("used_percentage")), "resets_at": _num(w.get("resets_at"))}


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def record(payload: dict, state_dir: str | Path, phase: str, now: float | None = None) -> dict:
    """Fold one status-line payload into the phase's meter file; return the meter.

    The write is skipped when nothing but the clock moved, and when the file was
    written under :data:`MIN_WRITE_S` ago — except for a new session, whose first
    meter must replace the previous session's at once. The limit sample rides
    with the write, so a skipped render cannot append it twice.
    """
    now = time.time() if now is None else now
    root = Path(state_dir) / METERS_DIR
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{phase}.json"
    prev = _read_json(path)

    ctx = payload.get("context_window") if isinstance(payload.get("context_window"), dict) else {}
    cost = payload.get("cost") if isinstance(payload.get("cost"), dict) else {}
    limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else {}
    session = payload.get("session_id")
    # A relaunched phase is a new session with a fresh context: its peak starts over.
    same = prev.get("session_id") == session
    tokens = _num(ctx.get("total_input_tokens"))
    prev_peak = (_num(prev.get("peak_tokens")) or 0.0) if same else 0.0
    peak = max(tokens or 0.0, prev_peak)

    meter = {
        "ts": now,
        "phase": phase,
        "session_id": session,
        "started_at": prev.get("started_at") if same and prev.get("started_at") else now,
        "context_tokens": tokens,
        "context_window": _num(ctx.get("context_window_size")),
        "context_pct": _num(ctx.get("used_percentage")),
        "peak_tokens": peak or None,
        "cost_usd": _num(cost.get("total_cost_usd")),
        "duration_ms": _num(cost.get("total_duration_ms")),
        "effort": (payload.get("effort") or {}).get("level") if isinstance(payload.get("effort"), dict) else None,
        "five_hour": _window(limits, "five_hour"),
        "seven_day": _window(limits, "seven_day"),
    }
    moved = any(meter[k] != prev.get(k) for k in meter if k not in _VOLATILE)
    # The previous write's own timestamp, not the file's mtime: one clock, and
    # no extra stat per render.
    last = _num(prev.get("ts"))
    fresh = last is not None and 0.0 <= now - last < MIN_WRITE_S
    if not moved or (same and fresh):
        return meter
    _write_json(path, meter)

    if not same and prev.get("session_id") and prev.get("cost_usd") is not None:
        # The outgoing session's final figures, before its file is gone: a
        # retried phase's first attempt still counts toward the run's cost.
        _append(root / SESSIONS_LOG, {k: prev.get(k) for k in (
            "ts", "phase", "session_id", "started_at", "cost_usd", "duration_ms")})

    five, week = meter["five_hour"], meter["seven_day"]
    if (five or week) and (five != prev.get("five_hour") or week != prev.get("seven_day")):
        row = usage.sample_row(now, runs.current_id(state_dir), five, week)
        # Every worker reports the same account-wide figures, so each change
        # would otherwise be logged once per worker. Compare with the last row
        # written by anyone; reading a file's last kilobyte is cheap and only
        # happens when this worker's own figures moved.
        last = _last_row(root / LIMITS_LOG)
        if last is None or not usage.same_values(last, row):
            _append(root / LIMITS_LOG, row)
    return meter


def _append(path: Path, row: dict) -> None:
    line = json.dumps(row) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _last_row(path: Path) -> dict | None:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 1024))
            lines = fh.read().splitlines()
        row = json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None
    return row if isinstance(row, dict) else None


_OWNER_SETTINGS = Path.home() / ".claude" / "settings.json"


def _owner_command(settings_path: Path) -> str:
    """The status line the owner configured for themselves, if any."""
    line = _read_json(settings_path).get("statusLine")
    if isinstance(line, dict) and line.get("type", "command") == "command":
        return str(line.get("command") or "")
    return ""


def _fallback(meter: dict) -> str:
    """A bar in the ``412k/1.0M`` shape :func:`tui.probes.parse_context` reads."""
    tokens, window = meter.get("context_tokens"), meter.get("context_window")
    if not tokens or not window:
        return meter["phase"]
    return f"{meter['phase']} · {tokens / 1000:.0f}k/{window / 1_000_000:.1f}M"


def settings_with_tap(
    worker_settings: str,
    state_dir: str | Path,
    phase: str,
    owner_settings: Path | None = None,
) -> str:
    """``worker_settings`` with this tap as the status line, unless one is set.

    Only a JSON object is touched: an empty string means "pass no settings" and a
    project that configured its own ``statusLine`` keeps it. The owner's own
    status-line command is looked up here, once, and passed as the tap's third
    argument ("" = none), so a render never has to read ``settings.json``.
    """
    try:
        data = json.loads(worker_settings) if worker_settings else None
    except ValueError:
        return worker_settings
    if not isinstance(data, dict) or "statusLine" in data:
        return worker_settings
    owner = _owner_command(owner_settings or _OWNER_SETTINGS)
    cmd = " ".join(shlex.quote(p) for p in (
        sys.executable, "-m", "swarm_orchestrator.meters", str(state_dir), phase, owner))
    data["statusLine"] = {"type": "command", "command": cmd, "padding": 0}
    return json.dumps(data, separators=(",", ":"))


def main(argv: list[str]) -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {}
    meter = {"phase": argv[1] if len(argv) > 1 else "?"}
    if isinstance(payload, dict) and len(argv) in (2, 3):
        try:
            meter = record(payload, argv[0], argv[1])
        except OSError:
            pass
    # Third argument: the owner's command, resolved at launch. A tap launched by
    # an older build passes only two, and keeps reading settings.json as before.
    owner = argv[2] if len(argv) == 3 else _owner_command(_OWNER_SETTINGS)
    if owner:
        try:
            # shell=True on purpose: this is the owner's own statusLine command
            # string, run the way Claude Code itself runs it.
            out = subprocess.run(owner, shell=True, input=raw, capture_output=True,
                                 text=True, timeout=CHAIN_TIMEOUT_S)
            if out.stdout.strip():
                sys.stdout.write(out.stdout)
                return 0
        except (OSError, subprocess.SubprocessError):
            pass
    print(_fallback(meter) if "context_tokens" in meter else meter["phase"])
    return 0


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except BaseException:  # noqa: BLE001 - a status line must never fail its render
        pass
    sys.exit(0)
