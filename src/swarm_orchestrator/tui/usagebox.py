"""The home screen's usage box: both limits, the rules, the burn, the outlook, a chart.

Usage used to be two lines of prose under the headline: true, but the owner had
to read them to learn whether anything was close to a limit, and nothing showed
how fast the figure had been climbing. So it is a box of its own now:

* one meter per window (5-hour, weekly) with the ``[usage].rules`` marked on the
  meter where the swarm pauses or stops, and when the window resets;
* the rules in words, and this run's burn per hour;
* **ahead** — per window, in a sentence, when its lowest pause (or the
  account's 100%) is reached at the burn the forecast itself simulates with
  (:func:`eta.engine.burn_of`, points per busy worker-hour, times the workers
  busy now), or that it will not be before the window resets;
* a chart of each window over time, on a fixed 0-100% scale with the rules drawn
  across it, from ``meters/limits.jsonl``, running on to the window's reset with
  the same projection dotted in, and a break where the account changed.

Text in, text out, like the rest of home's painters: :func:`box_lines` does no
I/O and never asks what time it is except through ``now``.
"""

from __future__ import annotations

import time

from rich.text import Text

from .. import usage as usage_mod
from .charts import (BREAK, PROJ, axis, axis_time, hold_last, level_chart, level_plot_width,
                     time_grid)
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


def _tail(got, now: float, full: bool) -> str:
    if got is None or got[1] is None:
        return ""
    resets = got[1]
    if full:
        return f"  resets {fmt_when(resets, now)} (in {fmt_coarse(max(0.0, resets - now))})"
    return f"  resets {fmt_when(resets, now)}"


def meter_width(width: int, tails: list[str]) -> int:
    """One bar width for every window: the longest tail sets it, so the bars
    start, end and scale alike (a shorter tail once bought a longer bar, and
    the same percentage drew a different length)."""
    return max(6, min(40, width - LABEL_W - 6 - max((len(t) for t in tails), default=0)))


def window_line(label: str, got, width: int, pause: list[float], stop: list[float],
                now: float, bar_w: int | None = None, tail: str | None = None) -> str:
    """``5-hour  ██████░░┄░ 25%  resets 19:20 (in 3h 10m)``.

    ``bar_w`` and ``tail`` come from :func:`window_lines`, which sizes both
    windows' bars together; alone, the line sizes its own."""
    if got is None:
        return paint(f"{label:<{LABEL_W}}", MUTED) + paint("not reported yet", MUTED)
    pct, _ = got
    if tail is None:
        tail = _tail(got, now, True)
        if LABEL_W + 12 + 6 + len(tail) > width:
            tail = _tail(got, now, False)
    if bar_w is None:
        bar_w = meter_width(width, [tail])
    state = _state(pct, pause, stop)
    return (f"{label:<{LABEL_W}}{_meter(pct, bar_w, pause, stop)} "
            + paint(f"{pct:>3.0f}%", state) + paint(tail, MUTED))


def window_lines(cfg, samples, width: int, now: float) -> list[str]:
    """Both windows' meters on one scale: same start, same width."""
    got = [(window, label, reading(samples, prefix, now)) for window, prefix, label in WINDOWS]
    full = [_tail(g, now, True) for _, _, g in got]
    if LABEL_W + 12 + 6 + max(len(t) for t in full) > width:
        full = [_tail(g, now, False) for _, _, g in got]
    bar_w = meter_width(width, full)
    out = []
    for (window, label, g), tail in zip(got, full):
        pause, stop = rules_for(cfg, window)
        out.append(window_line(label, g, width, pause, stop, now, bar_w, tail))
    return out


#: Short window names for a narrow box.
SHORT = {"5-hour": "5h", "week": "wk"}
#: How the outlook names each window in a sentence.
NAMES = {"five_hour": "5-hour", "week": "weekly"}


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def wrap(label: str, text: str, width: int) -> list[str]:
    """``text`` after ``label`` in words, its continuation lines under the text:
    nothing in the box is cut off."""
    room = max(8, width - LABEL_W)
    lines, cur = [], ""
    for word in text.split(" "):
        while len(word) > room:  # a word longer than the room (never in practice)
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:room])
            word = word[room:]
        if cur and len(cur) + 1 + len(word) > room:
            lines.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    lines.append(cur)
    return [f"{label if i == 0 else '':<{LABEL_W}}{line}" for i, line in enumerate(lines)]


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
        return head + paint(_clip("not measured yet" if room < 52 else
                                  "not measured yet (a quarter of an hour of readings)", room),
                            MUTED)
    return head + _clip(got if len(got) <= room else text(True), room)


def _busy(dash) -> int:
    return sum(1 for s in dash.snapshot.slots if s.busy)


def cap_of(cfg, window: str) -> tuple[float, str]:
    """``(level, words)``: where a window stops the swarm, as the outlook says it.

    Its lowest pause rule, or the account's 100% — what
    :func:`eta.holds.from_state` schedules the simulation against."""
    pause, _ = rules_for(cfg, window)
    return (min(pause), f"the {min(pause):g}% pause") if pause else (LIMIT, "the 100% limit")


def hits_at(dash, window: str, got, now: float) -> float | None:
    """When ``window`` reaches its cap at this pace (the forecast's burn per busy
    worker-hour times the workers busy now); ``None`` if it does not burn."""
    rate = (getattr(dash, "burn", None) or {}).get(window, 0.0) * _busy(dash)
    if got is None or rate <= 0:
        return None
    level, _ = cap_of(dash.cfg, window)
    return now + max(0.0, level - got[0]) / rate * 3600.0


def outlook(dash, now: float) -> list[tuple[str, str]]:
    """``(sentence, state)`` per window: when it hits its cap at this pace, or
    that it will not before it resets. ``weekly hits the 90% pause ~Thu 14:20
    at this pace (resets Wed 11:00)``."""
    samples = getattr(dash, "samples", None) or []
    hold, override = getattr(dash, "usage_state", ({}, {}))
    busy = _busy(dash)
    out = []
    for window, prefix, _ in WINDOWS:
        name = NAMES[window]
        if window in hold:
            until = float((hold[window] or {}).get("resets_at") or 0.0)
            when = f" until {fmt_when(until, now)}" if until else ""
            out.append((f"{name} cap holds new work{when}", BAD))
            continue
        got = reading(samples, prefix, now)
        if got is None:
            continue
        pct, resets = got
        level, words = cap_of(dash.cfg, window)
        reset = f"resets {fmt_when(resets, now)}" if resets is not None else ""
        if override.get(window):
            out.append((f"{name} runs past {words}, as you chose", MUTED))
        elif pct >= level:
            out.append((f"{name} is at {words} ({pct:.0f}%)", BAD))
        elif busy == 0:
            out.append((f"{name} won't hit {words}: nothing is running", OK))
        else:
            at = hits_at(dash, window, got, now)
            if at is None:
                out.append((f"{name}: no pace measured yet", MUTED))
            elif resets is not None and at >= resets:
                out.append((f"{name} won't hit {words} before it {reset}", OK))
            else:
                tail = f" ({reset})" if reset else ""
                out.append((f"{name} hits {words} ~{fmt_when(at, now)} at this pace{tail}",
                            BAD if at - now < 3600 else WARN))
    return out


def outlook_lines(dash, now: float, width: int) -> list[str]:
    """The outlook under the label ``ahead``, wrapped, never cut."""
    out = []
    for i, (text, state) in enumerate(outlook(dash, now)):
        out += [line[:LABEL_W] + paint(line[LABEL_W:], state)
                for line in wrap("ahead" if i == 0 else "", text, width)]
    return out


# -- the chart -----------------------------------------------------------------
def series(samples, prefix: str, t0: float, now: float) -> list[tuple[float, float]]:
    """``(ts, pct)`` steps for one window from ``t0`` to now, zero at each reset.

    Within a window only the running maximum counts: a lagging session's status
    line reports an older, lower figure, and plotting it would draw a dip that
    never happened. A reset with no reading after it drops to zero at the reset.
    Another account (:func:`usage.accounts`) starts from its own first reading.
    """
    out: list[tuple[float, float]] = []
    top = res = None
    account = None
    jump = usage_mod.RESET_JUMP_S
    samples = list(samples or ())
    for s, seg in sorted(zip(samples, usage_mod.accounts(samples)), key=lambda x: x[0].ts):
        pct, resets = getattr(s, f"{prefix}_pct"), getattr(s, f"{prefix}_resets_at")
        if pct is None or s.ts > now or seg is None:
            continue
        if seg != account:
            top = res = None
            account = seg
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


def projection(pct: float, rate: float, now: float, until: float, level: float,
               grid: list[float]) -> list[float | None]:
    """The dotted line ahead: ``pct`` climbing at ``rate`` points an hour from
    now until it reaches ``level`` or the window resets at ``until``."""
    out: list[float | None] = []
    reached = False
    for t in grid:
        v = pct + rate * (t - now) / 3600.0
        if t <= now or t > until or reached:
            out.append(None)
            continue
        reached = v >= level
        out.append(min(v, level))
    return out


def chart(samples, prefix: str, label: str, width: int, height: int, now: float,
          pause: list[float], stop: list[float], t0: float, t1: float | None = None,
          rate: float = 0.0, level: float = LIMIT) -> list[str]:
    """One window's chart: a title row, the plot, the time axis under it.

    The readings run from ``t0`` to now; with ``t1`` past now, a dotted line
    carries the figure on at ``rate`` points an hour until it reaches ``level``
    or the window resets at ``t1``. An account switch is a dashed break, and
    the other account's readings before it are drawn muted."""
    t1 = now if t1 is None or t1 < now else t1
    points = series(samples, prefix, t0, now)
    since = f" · since {axis_time(t0, now - t0)}"
    title = paint(f"{label} %", INFO) + paint(since if len(label) + 2 + len(since) <= width
                                              else "", MUTED)
    if not points:
        return [title, paint(_clip("no readings in this span", width), MUTED)]
    marks = [(p, PAUSE_MARK) for p in pause] + [(s, STOP_MARK) for s in stop]
    plot = level_plot_width(width, 100.0, marks)
    first = points[0][0]
    grid = time_grid(t0, t1, plot)
    # The column holding now shows the reading now, not the one at its left
    # edge: on a week-wide chart a column is hours wide.
    at = [now if t <= now < t + (t1 - t0) / max(1, plot - 1) else t for t in grid]
    values = [None if t < first or t > now else v for t, v in zip(grid, hold_last(points, at))]
    ahead = projection(points[-1][1], rate, now, t1, level, grid) if t1 > now else []
    col = (lambda t: round((t - t0) / (t1 - t0) * (plot - 1))) if t1 > t0 else (lambda t: 0)
    breaks = [col(t) for t in usage_mod.switch_times(list(samples or ())) if t0 < t <= now]
    body = level_chart(values, width, height, top=100.0, marks=marks, projection=ahead,
                       breaks=breaks)
    if not body:
        return [title]
    gutter = width - plot
    reach = t1 - t0
    labels = [axis_time(t0 + reach * f, reach) for f in (0.0, 0.5, 1.0)]
    dim = gutter + max(breaks) if breaks else 0
    return [title] + [_paint_row(row, gutter, dim) for row in body] + [
        paint(" " * gutter + axis(labels, max(1, width - gutter)), MUTED)]


def _paint_row(row: str, gutter: int, dim: int = 0) -> str:
    """Colour a chart row: the scale muted, the curve blue (muted before an
    account switch at column ``dim``), the cap lines by rule, the projection amber."""
    out = [paint(row[:gutter], MUTED)]
    run, kind = "", None
    for i, ch in enumerate(row[gutter:], start=gutter):
        k = WARN if ch in (PAUSE_MARK, PROJ) else BAD if ch == STOP_MARK else (
            MUTED if ch in " ·" + BREAK or i < dim else INFO)
        if k != kind and run:
            out.append(paint(run, kind))
            run = ""
        run, kind = run + ch, k
    if run:
        out.append(paint(run, kind))
    return "".join(out)


def chart_lines(dash, width: int, height: int, now: float) -> list[str]:
    """Both windows' charts: side by side when there is room, else stacked.
    Each runs to its window's reset, the projection drawn up to it."""
    samples = getattr(dash, "samples", None) or []
    cfg = dash.cfg
    burn = getattr(dash, "burn", None) or {}
    busy = _busy(dash)
    week = reading(samples, "week", now)
    five = reading(samples, "five", now)
    week_t0 = week[1] - WEEK_S if week and week[1] else now - WEEK_S
    specs = [("five_hour", "five", "5-hour", now - FIVE_SPAN_S, five),
             ("week", "week", "week", max(week_t0, now - WEEK_S), week)]
    side = width >= SIDE_BY_SIDE
    each = (width - 2) // 2 if side else width
    blocks = []
    for window, prefix, label, t0, got in specs:
        pause, stop = rules_for(cfg, window)
        t1 = got[1] if got and got[1] else None
        blocks.append(chart(samples, prefix, label, each, height, now, pause, stop, t0, t1,
                            burn.get(window, 0.0) * busy, cap_of(cfg, window)[0]))
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
    lines = window_lines(cfg, samples, width, now)
    lines.append(limits_line(cfg, width))
    lines.append(burn_line(getattr(dash, "usage", None), width))
    lines += outlook_lines(dash, now, width)
    if chart_rows > 0 and samples:
        if chart_rows >= 5:
            lines.append("")
        lines += chart_lines(dash, width, chart_rows, now)
    return lines


def subtitle(dash, now: float | None = None) -> str:
    """``as of 14:05, 12 min ago`` — every reader says how old the figures are."""
    now = time.time() if now is None else now
    return usage_mod.as_of(list(getattr(dash, "samples", None) or ()), now) or "no reading yet"

