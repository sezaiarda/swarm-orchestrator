"""The Usage tab's data: both windows over time, their caps, and where they are heading.

For each window (5-hour, weekly) the page draws the readings from
``meters/limits.jsonl``, the ``[usage].rules`` as lines across it, the reset
times, and a **projection** from the newest reading at the current burn to the
cap it will hit (or to its reset, if that comes first).

Readings, as :func:`tui.usagebox.series` reads them: inside one window only the
running maximum counts (a lagging session's status line reports an older, lower
figure), and a window that resets drops to zero at its reset. One addition: the
owner switches accounts, and a reading from the other account is a different
counter altogether. A **sudden big move** that is not a reset — down by
:data:`BREAK_DROP` points or more, or up by :data:`BREAK_JUMP` — ends the line
there and starts a new segment, so the chart shows a break instead of a fall
(or a rise) that never happened.

The burn is the forecast's own (:func:`eta.engine.burn_of`: points per busy
worker-hour, times the workers busy now), as the TUI's *next cap* line uses;
before one is measured, the run's own pace. Every time is epoch seconds and
nothing here is "time from now", so the payload changes only when a reading, the
burn or the rules do.
"""

from __future__ import annotations

import math

from .. import usage as usage_mod

#: ``(rules window, samples prefix, label, length of the window in seconds)``.
WINDOWS = (("five_hour", "five", "5-hour", 5 * 3600.0), ("week", "week", "Weekly", 7 * 86400.0))
LIMIT = 100.0
#: A move this big between two readings of one window is another account's counter.
BREAK_DROP = 8.0
BREAK_JUMP = 20.0
#: How far back each chart reaches.
SPAN_S = {"five": 24 * 3600.0, "week": 7 * 86400.0}


def rules(cfg, window: str) -> list[dict]:
    """``[{"at", "action"}]`` for one window, lowest first; none when caps are off."""
    if not getattr(cfg, "usage_enabled", False):
        return []
    got = [{"at": float(r["at"]), "action": str(r["action"])}
           for r in (getattr(cfg, "usage_rules", None) or []) if r.get("window") == window]
    return sorted(got, key=lambda r: r["at"])


def segments(samples, prefix: str, t0: float, now: float) -> tuple[list[list[list[float]]],
                                                                   list[float], list[float]]:
    """``(segments, resets, breaks)`` for one window from ``t0`` to ``now``.

    Each segment is ``[[ts, pct], …]``; ``resets`` are the window resets seen
    (the line drops to zero there, within its segment); ``breaks`` are where
    one segment ended and the next began (an account switch).
    """
    segs: list[list[list[float]]] = []
    resets: list[float] = []
    breaks: list[float] = []
    cur: list[list[float]] = []
    top = res = before = None

    def point(ts: float, value: float) -> None:
        if ts < t0:
            return
        if not cur and not segs and not breaks and before is not None and ts > t0:
            cur.append([round(t0, 1), before])  # the line enters from the left edge
        cur.append([round(ts, 1), value])

    for s in sorted(samples or (), key=lambda s: s.ts):
        pct, rs = getattr(s, f"{prefix}_pct"), getattr(s, f"{prefix}_resets_at")
        if pct is None or s.ts > now:
            continue
        if res is not None and s.ts >= res:  # the window reset before this reading
            point(res, top)
            point(res, 0.0)
            if res >= t0:
                resets.append(res)
            else:
                before = 0.0
            top = res = None
        if top is not None and (pct <= top - BREAK_DROP or pct >= top + BREAK_JUMP):
            if cur:
                segs.append(cur)
                cur = []
            if s.ts >= t0:
                breaks.append(round(s.ts, 1))
            top = res = None
        elif res is not None and rs is not None and rs > res + usage_mod.RESET_JUMP_S:
            top = None  # a new window, read before its predecessor's reset passed
        top = pct if top is None else max(top, pct)
        res = rs if rs is not None else res
        if s.ts < t0:
            before = top
        point(s.ts, top)
    if res is not None and t0 <= res <= now:
        point(res, top)
        point(res, 0.0)
        resets.append(res)
    if cur:
        segs.append(cur)
    return segs, resets, breaks


def _reading(samples, prefix: str, now: float):
    """``(ts, pct, resets_at)`` of the newest reading still in its window."""
    for s in reversed(samples or ()):
        pct, rs = getattr(s, f"{prefix}_pct"), getattr(s, f"{prefix}_resets_at")
        if pct is not None:
            if rs is not None and rs <= now:
                return None
            return s.ts, pct, rs
    return None


def project(pct: float, ts: float, resets: float | None, rate_h: float | None,
            caps: list[float]) -> dict | None:
    """Where one window goes from ``(ts, pct)`` at ``rate_h`` points an hour.

    ``hits`` is the lowest cap still ahead and when it is reached — or ``None``
    when the window resets first (or nothing burns). ``to`` is where the drawn
    projection line ends: the hit, or the reset.
    """
    ahead = sorted(c for c in caps if c > pct)
    if not rate_h or rate_h <= 0 or not math.isfinite(rate_h):
        return {"rate_h": rate_h or 0.0, "hits": None, "to": None}
    hits = None
    for cap in ahead:
        at = ts + (cap - pct) / rate_h * 3600.0
        if resets is not None and at >= resets:
            break
        # To five minutes: the burn is re-measured every minute, and a clock that
        # moves by seconds would re-send (and re-draw) the board each time.
        hits = {"at": round(at / 300) * 300, "pct": cap}
        break
    if hits is not None:
        to = [hits["at"], hits["pct"]]
    elif resets is not None:
        to = [resets, min(LIMIT, pct + rate_h * (resets - ts) / 3600.0)]
    else:
        to = [ts + 24 * 3600.0, min(LIMIT, pct + rate_h * 24)]
    return {"rate_h": round(rate_h, 3), "hits": hits, "to": [round(to[0]), round(to[1], 2)]}


def windows(cfg, samples, *, burn: dict, busy: int, run_usage: dict | None,
            hold: dict | None, override: dict | None, now: float) -> list[dict]:
    """Both windows, as the Usage tab and the overview's next-cap line read them."""
    out = []
    for window, prefix, label, length in WINDOWS:
        rs = rules(cfg, window)
        caps = [r["at"] for r in rs if r["action"] == "pause"] + [LIMIT]
        t0 = now - SPAN_S[prefix]
        segs, resets, breaks = segments(samples, prefix, t0, now)
        got = _reading(samples, prefix, now)
        rate = burn.get(window, 0.0) * busy if burn.get(window) else None
        basis = "burn" if rate else ""
        if not rate and run_usage:
            rate = run_usage.get(f"{prefix}_pct_per_h")
            basis = "run" if rate else ""
        entry = {
            "key": prefix, "window": window, "label": label, "length_s": length,
            "rules": rs, "limit": LIMIT, "t0": round(t0),
            "segments": segs, "resets": resets, "breaks": breaks,
            "pct": got[1] if got else None, "read_at": round(got[0]) if got else None,
            "resets_at": got[2] if got else None,
            "held": bool((hold or {}).get(window)), "override": bool((override or {}).get(window)),
            "projection": None, "basis": basis,
        }
        if got is not None and not entry["override"]:
            entry["projection"] = project(got[1], got[0], got[2], rate, caps)
        out.append(entry)
    return out


def next_cap(wins: list[dict], done_at: float | None) -> dict | None:
    """The first cap the run reaches, against when the forecast says the work is done.

    ``None`` when no window has a reading. ``hits`` is ``None`` when no cap is
    reached before its window resets (``why`` says which).
    """
    seen = [w for w in wins if w.get("pct") is not None]
    if not seen:
        return None
    held = [w for w in seen if w["held"]]
    if held:
        until = max((w["resets_at"] or 0) for w in held) or None
        return {"held": [w["label"] for w in held], "until": until, "hits": None}
    hits = [(w["projection"]["hits"]["at"], w) for w in seen
            if w.get("projection") and w["projection"].get("hits")]
    if not hits:
        burning = any((w.get("projection") or {}).get("rate_h") for w in seen)
        return {"hits": None, "why": "resets first" if burning else "nothing burns",
                "done_at": done_at}
    at, w = min(hits, key=lambda h: h[0])
    return {"hits": {"at": at, "pct": w["projection"]["hits"]["pct"], "window": w["key"],
                     "label": w["label"]},
            "before_done": bool(done_at is not None and at < done_at), "done_at": done_at}


def runs(current: dict | None, past: list[dict], limit: int = 20) -> list[dict]:
    """The runs table: the open run first, then closed ones, newest first."""
    keys = ("run_id", "start", "end", "hours", "max_workers", "phases_finished",
            "phases_failed", "five_used", "week_used", "five_pct_per_h", "week_pct_per_h",
            "usd", "usd_per_h", "closed_by")
    out = []
    for i, rec in enumerate(([current] if current else []) + list(past or [])):
        if not isinstance(rec, dict):
            continue
        row = {k: rec.get(k) for k in keys}
        row["open"] = bool(current) and i == 0
        out.append(row)
        if len(out) >= limit:
            break
    return out


def payload(cfg, dash, board: dict, now: float) -> dict:
    """``/api/usage``: both windows, the next cap and the runs table."""
    snap = dash.snapshot
    busy = sum(1 for s in snap.slots if s.busy)
    hold, override = getattr(dash, "usage_state", ({}, {}))
    wins = windows(cfg, getattr(dash, "samples", None) or [], burn=getattr(dash, "burn", {}) or {},
                   busy=busy, run_usage=getattr(dash, "usage", None), hold=hold,
                   override=override, now=now)
    eta = (board.get("header") or {}).get("eta") or {}
    return {"windows": wins, "next": next_cap(wins, eta.get("p50")), "busy": busy,
            "runs": runs(getattr(dash, "usage", None), getattr(dash, "past_runs", None) or []),
            "now": round(now)}
