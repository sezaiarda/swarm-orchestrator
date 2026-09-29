"""The home screen's usage box: both limits, the rules, the burn, the outlook, a chart.

Usage used to be two lines of prose under the headline: true, but the owner had
to read them to learn whether anything was close to a limit, and nothing showed
how fast the figure had been climbing. So it is a box of its own now:

* one meter per window (5-hour, weekly) with the ``[usage].rules`` marked on the
  meter where the swarm pauses or stops, and when the window resets;
* the rules in words, and this run's burn per hour;
* **next cap** — when the lowest pause (or the account's 100%) is reached at the
  burn the forecast itself simulates with (:func:`eta.engine.burn_of`, points
  per busy worker-hour, times the workers busy now), against when the forecast
  says the work is done;
* a chart of each window over time, on a fixed 0-100% scale with the rules drawn
  across it, from ``meters/limits.jsonl``.

Text in, text out, like the rest of home's painters: :func:`box_lines` does no
I/O and never asks what time it is except through ``now``.
"""

from __future__ import annotations

import math
import re
import time

from rich.text import Text

from .. import usage as usage_mod
from .charts import axis, axis_time, hold_last, level_chart, time_grid
from .data import fmt_coarse, fmt_when
from .theme import BAD, INFO, MUTED, OK, WARN, paint

#: ``(rules window, samples prefix, label)`` in the order the box shows them.
WINDOWS = (("five_hour", "five", "5-hour"), ("week", "week", "week"))
#: The account's own limit: at 100% nothing gets done, whatever the rules say.
LIMIT = 100.0
#: How far back the 5-hour chart reaches (it resets five times a day, so a day
#: shows the saw); the weekly chart shows its whole current window.
FIVE_SPAN_S = 24 * 3600.0
WEEK_S = 7 * 86400.0
#: Below this inner width the two charts stack instead of sitting side by side.
SIDE_BY_SIDE = 40
#: Cap lines on the chart and the meter: pause, then stop.
PAUSE_MARK, STOP_MARK = "┄", "━"
LABEL_W = 7


def rules_for(cfg, window: str) -> tuple[list[float], list[float]]:
    """``(pause levels, stop levels)`` for one window; empty when caps are off."""
    if not getattr(cfg, "usage_enabled", False):
        return [], []
    rules = list(getattr(cfg, "usage_rules", None) or [])
    pause = sorted(float(r["at"]) for r in rules if r["window"] == window and r["action"] == "pause")
    stop = sorted(float(r["at"]) for r in rules if r["window"] == window and r["action"] == "down")
    return pause, stop


def reading(samples, prefix: str, now: float) -> tuple[float, float | None] | None:
    """``(pct, resets_at)`` — the newest reading still in its window."""
    return usage_mod.latest(list(samples or ()), prefix, now)


def _meter(pct: float, width: int, pause: list[float], stop: list[float]) -> str:
    """A meter with the rules marked on it: ``█████░░┄░░━░``."""
    filled = max(0, min(width, round(width * pct / 100.0)))
    cells = ["█"] * filled + ["░"] * (width - filled)
    marks = {}
    for level, char in [(p, PAUSE_MARK) for p in pause] + [(s, STOP_MARK) for s in stop]:
        at = max(0, min(width - 1, round(width * level / 100.0) - 1))
        marks[at] = char
    state = _state(pct, pause, stop)
    out = []
    for i, cell in enumerate(cells):
        if i in marks and cell == "░":
            out.append(paint(marks[i], BAD if marks[i] == STOP_MARK else WARN))
        else:
            out.append(paint(cell, state if cell == "█" else MUTED))
    return "".join(out)


def _state(pct: float, pause: list[float], stop: list[float]) -> str:
    """Red at or past the lowest rule, amber within ten points of it."""
    first = min(pause + stop + [LIMIT])
    return BAD if pct >= first else (WARN if pct >= first - 10 else OK)


def window_line(label: str, got, width: int, pause: list[float], stop: list[float],
                now: float) -> str:
    """``5-hour  ██████░░┄░ 25%  resets 19:20 (in 3h 10m)``."""
    if got is None:
        return paint(f"{label:<{LABEL_W}}", MUTED) + paint("not reported yet", MUTED)
    pct, resets = got
    tail = "" if resets is None else f"  resets {fmt_when(resets, now)} (in {fmt_coarse(max(0.0, resets - now))})"
    if LABEL_W + 12 + 6 + len(tail) > width:
        tail = "" if resets is None else f"  resets {fmt_when(resets, now)}"
    bar_w = max(6, min(40, width - LABEL_W - 6 - len(tail)))
    state = _state(pct, pause, stop)
    return (f"{label:<{LABEL_W}}{_meter(pct, bar_w, pause, stop)} "
            + paint(f"{pct:>3.0f}%", state) + paint(tail, MUTED))


#: Short window names for a narrow box.
SHORT = {"5-hour": "5h", "week": "wk"}


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def limits_line(cfg, width: int = 80) -> str:
    """``limits  pause 5-hour 90% · pause week 90% · stop week 95%``."""
    head = f"{'limits':<{LABEL_W}}"
    room = width - LABEL_W
    if not getattr(cfg, "usage_enabled", False):
        return head + paint(_clip("usage caps are off", room), MUTED)

    def parts(short: bool) -> list[tuple[str, str]]:
        out = []
        for window, _, label in WINDOWS:
            name = SHORT[label] if short else label
            pause, stop = rules_for(cfg, window)
            out += [(f"pause {name} {p:g}%", WARN) for p in pause]
            out += [(f"stop {name} {s:g}%", BAD) for s in stop]
        return out

    got = parts(False)
    if len(" · ".join(t for t, _ in got)) > room:
        # Grouped by action: ``pause 5h 90%, wk 90% · stop wk 95%``.
        short = parts(True)
        got = []
        for action, state in (("pause", WARN), ("stop", BAD)):
            mine = [t.split(" ", 1)[1] for t, _ in short if t.startswith(action + " ")]
            if mine:
                got.append((f"{action} {', '.join(mine)}", state))
    if not got:
        return head + paint("no rules set", MUTED)
    return head + paint(" · ", MUTED).join(paint(_clip(t, room), st) for t, st in got)


def burn_line(run_usage: dict | None, width: int = 80) -> str:
    """``burn    this run 5-hour 3.2%/h · week 0.9%/h``."""
    run_usage = run_usage or {}
    head = f"{'burn':<{LABEL_W}}"
    room = width - LABEL_W

    def text(short: bool) -> str:
        parts = []
        for key, label in (("five_pct_per_h", "5-hour"), ("week_pct_per_h", "week")):
            value = run_usage.get(key)
            if isinstance(value, (int, float)):
                parts.append(f"{SHORT[label] if short else label} {value:.1f}%/h")
        usd = run_usage.get("usd_per_h")
        if isinstance(usd, (int, float)):
            parts.append(f"${usd:.2f}/h")
        return ("" if short else "this run ") + " · ".join(parts) if parts else ""

    got = text(False)
    if not got:
        return head + paint(_clip("not measured yet (a quarter of an hour of readings)", room),
                            MUTED)
    return head + _clip(got if len(got) <= room else text(True), room)


def next_cap(dash, now: float) -> tuple[str, str]:
    """``(text, state)``: which cap the run reaches first, when, against the work.

    Each window's cap is its lowest pause rule, or the account's 100% — what
    :func:`eta.holds.from_state` schedules the simulation against — and it is
    reached at the forecast's burn (points per busy worker-hour) times the
    workers busy now. A window that resets first is no cap at all.
    """
    cfg = dash.cfg
    samples = getattr(dash, "samples", None) or []
    burn = getattr(dash, "burn", None) or {}
    hold, override = getattr(dash, "usage_state", ({}, {}))
    snap = dash.snapshot
    busy = sum(1 for s in snap.slots if s.busy)
    fc = getattr(dash, "forecast", None)
    done = None
    if fc is not None and fc.overall is not None and math.isfinite(fc.overall.p50):
        done = fc.overall.p50
    work = f"the work is done ~{fmt_when(done, now)}" if done is not None else ""
    held = [w for w, _, _ in WINDOWS if w in hold]
    if held:
        label = dict((w, lab) for w, _, lab in WINDOWS)
        until = max((float((hold[w] or {}).get("resets_at") or 0.0) for w in held), default=0.0)
        when = f" until {fmt_when(until, now)}" if until else ""
        return (f"held by the {' and '.join(label[w] for w in held)} cap{when}"
                " — no new workers", BAD)
    hits = []
    safe = []
    for window, prefix, label in WINDOWS:
        if override.get(window):
            continue
        got = reading(samples, prefix, now)
        if got is None:
            continue
        pct, resets = got
        pause, _ = rules_for(cfg, window) if getattr(cfg, "usage_enabled", False) else ([], [])
        cap = min(pause + [LIMIT])
        if pct >= cap:
            hits.append((now, label, cap, resets))
            continue
        rate = burn.get(window, 0.0) * busy
        if rate <= 0:
            continue
        at = now + (cap - pct) / rate * 3600.0
        if resets is not None and at >= resets:
            safe.append(label)
            continue
        hits.append((at, label, cap, resets))
    if not hits:
        if busy == 0:
            text = "none — nothing is running, so nothing burns"
        elif safe:
            text = f"none — {' and '.join(safe)} reset before their caps"
        else:
            text = "not known yet — no burn measured"
        return (f"{text}{' · ' + work if work else ''}", OK if busy == 0 or safe else MUTED)
    at, label, cap, resets = min(hits)
    head = f"{label} {cap:g}% ~{fmt_when(at, now)} (in {fmt_coarse(max(0.0, at - now))})"
    if at <= now:
        head = f"{label} {cap:g}% reached"
    if done is None:
        return head, WARN
    if at < done:
        return f"{head}, before {work}", BAD if at - now < 3600 else WARN
    return f"{head}, after {work}", OK


def next_line(dash, now: float, width: int) -> str:
    text, state = next_cap(dash, now)
    room = width - LABEL_W
    if len(text) > room:  # a narrow box: the times, without the words around them
        text = re.sub(r" \(in [^)]*\)", "", text)
        text = re.sub(r"(before|after) the work is done ~.*$", r"\1 it's done", text)
        for long, short in SHORT.items():
            text = text.replace(f"{long} ", f"{short} ")
    text = _clip(text, room)
    return f"{'next':<{LABEL_W}}" + paint(text, state)


# -- the chart -----------------------------------------------------------------
def series(samples, prefix: str, t0: float, now: float) -> list[tuple[float, float]]:
    """``(ts, pct)`` steps for one window from ``t0`` to now, zero at each reset.

    Within a window only the running maximum counts: a lagging session's status
    line reports an older, lower figure, and plotting it would draw a dip that
    never happened. A reset with no reading after it drops to zero at the reset.
    """
    out: list[tuple[float, float]] = []
    top = res = None
    jump = usage_mod.RESET_JUMP_S
    for s in sorted(samples or (), key=lambda s: s.ts):
        pct, resets = getattr(s, f"{prefix}_pct"), getattr(s, f"{prefix}_resets_at")
        if pct is None or s.ts > now:
            continue
        if res is not None and s.ts >= res:  # the window reset before this reading
            out.append((res, 0.0))
            top = res = None
        if res is not None and resets is not None:
            if resets < res - jump:
                continue  # a reading from a window already over
            if resets > res + jump:
                top = None  # a new window
        top = pct if top is None else max(top, pct)
        res = resets if resets is not None else res
        out.append((s.ts, top))
    if res is not None and res <= now:
        out.append((res, 0.0))
    before = [p for p in out if p[0] <= t0]
    after = [p for p in out if p[0] > t0]
    return ([(t0, before[-1][1])] if before else []) + after


def chart(samples, prefix: str, label: str, width: int, height: int, now: float,
          pause: list[float], stop: list[float], t0: float) -> list[str]:
    """One window's chart: a title row, the plot, the time axis under it."""
    points = series(samples, prefix, t0, now)
    since = f" · since {axis_time(t0, now - t0)}"
    title = paint(f"{label} %", INFO) + paint(since if len(label) + 2 + len(since) <= width
                                              else "", MUTED)
    if not points:
        return [title, paint(_clip("no readings in this span", width), MUTED)]
    marks = [(p, PAUSE_MARK) for p in pause] + [(s, STOP_MARK) for s in stop]
    first = points[0][0]
    grid = time_grid(t0, now, width)
    values = [None if t < first else v for t, v in zip(grid, hold_last(points, grid))]
    body = level_chart(values, width, height, top=100.0, marks=marks)
    if not body:
        return [title]
    gutter = body[0].index("┤") + 1 if "┤" in body[0] else 0
    reach = now - t0
    labels = [axis_time(t0 + reach * f, reach) for f in (0.0, 0.5, 1.0)]
    return [title] + [_paint_row(row, gutter) for row in body] + [
        paint(" " * gutter + axis(labels, max(1, width - gutter)), MUTED)]


def _paint_row(row: str, gutter: int) -> str:
    """Colour a chart row: the scale muted, the curve blue, the cap lines by rule."""
    out = [paint(row[:gutter], MUTED)]
    run, kind = "", None
    for ch in row[gutter:]:
        k = WARN if ch == PAUSE_MARK else BAD if ch == STOP_MARK else (
            INFO if ch not in " ·" else MUTED)
        if k != kind and run:
            out.append(paint(run, kind))
            run = ""
        run, kind = run + ch, k
    if run:
        out.append(paint(run, kind))
    return "".join(out)


def chart_lines(dash, width: int, height: int, now: float) -> list[str]:
    """Both windows' charts: side by side when there is room, else stacked."""
    samples = getattr(dash, "samples", None) or []
    cfg = dash.cfg
    week = reading(samples, "week", now)
    week_t0 = week[1] - WEEK_S if week and week[1] else now - WEEK_S
    specs = [("five_hour", "five", "5-hour", now - FIVE_SPAN_S),
             ("week", "week", "week", max(week_t0, now - WEEK_S))]
    side = width >= SIDE_BY_SIDE
    each = (width - 2) // 2 if side else width
    blocks = []
    for window, prefix, label, t0 in specs:
        pause, stop = rules_for(cfg, window)
        blocks.append(chart(samples, prefix, label, each, height, now, pause, stop, t0))
    if not side:
        return blocks[0] + blocks[1]
    left, right = blocks
    rows = max(len(left), len(right))
    left += [""] * (rows - len(left))
    right += [""] * (rows - len(right))
    return [f"{_pad(a, each)}  {b}" for a, b in zip(left, right)]


def _pad(markup: str, width: int) -> str:
    """Right-pad a markup line to ``width`` visible cells."""
    return markup + " " * max(0, width - Text.from_markup(markup).cell_len)


# -- the box -------------------------------------------------------------------
def box_lines(dash, width: int, now: float | None = None, chart_rows: int = 5) -> list[str]:
    """Every line of the usage box, for ``width`` inner columns.

    ``chart_rows`` 0 leaves the charts out (a short terminal)."""
    now = time.time() if now is None else now
    samples = getattr(dash, "samples", None) or []
    cfg = dash.cfg
    lines = []
    for window, prefix, label in WINDOWS:
        pause, stop = rules_for(cfg, window)
        lines.append(window_line(label, reading(samples, prefix, now), width, pause, stop, now))
    lines.append(limits_line(cfg, width))
    lines.append(burn_line(getattr(dash, "usage", None), width))
    lines.append(next_line(dash, now, width))
    if chart_rows > 0 and samples:
        if chart_rows >= 5:
            lines.append("")
        lines += chart_lines(dash, width, chart_rows, now)
    return lines


def subtitle(dash, now: float | None = None) -> str:
    """``as of 14:05, 12 min ago`` — every reader says how old the figures are."""
    now = time.time() if now is None else now
    return usage_mod.as_of(list(getattr(dash, "samples", None) or ()), now) or "no reading yet"

