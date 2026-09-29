"""Plain-text charts, with nothing Textual in them.

These lived inside a Graphs tab that answered five questions nobody was asking
there. The tab is gone; one or two of the charts are worth keeping, on the home
screen where the question is actually asked, so the drawing moved here rather
than dying with its tab.

Two properties make that reuse safe:

* **Text in, text out.** No widget, no markup, no disk, and never a question
  about what time it is. That is what
  makes these testable for real, and it is also why nothing here escapes its
  input: Rich markup escaping turns one column into two and would break the
  invariant below. A caller putting a label on screen escapes it itself.
* **Every line is exactly ``width`` columns.** A chart that returns one column
  too many does not look slightly wrong — it reflows the panel around it. All
  the glyphs used are single-width, so a line's ``len`` is its column count.

Degenerate input (nothing, one point, all equal, all zero, a width narrower than
the data) returns a blank line or no lines rather than raising. These run on the
repaint tick, so they are also single-pass and allocate nothing before the cheap
guards at the top of each function.
"""

from __future__ import annotations

from datetime import datetime

#: 1/8-cell steps. A value too small to round up to a whole cell still gets
#: ``▁``: on a chart of phase counts, "one" and "none" must not draw the same.
_BLOCKS = "▁▂▃▄▅▆▇█"

#: Zero is a value on these charts — an idle hour is the interesting one — so it
#: gets its own mark instead of the shortest block, which reads as "a little".
_ZERO = "·"

#: A meter is not a chart: solid/track, deliberately not the block gradient,
#: because a gradient reads as texture and a meter has to read across a room.
_FILL = "█"
_TRACK = "░"

#: Blank columns demanded between two x-axis labels before both are drawn.
_GAP = 2


# -- internals -------------------------------------------------------------
def _fit(line: str, width: int) -> str:
    """The invariant, in one place: exactly ``width`` columns."""
    return line[:width] if len(line) > width else line + " " * (width - len(line))


def _clip(text, width: int) -> str:
    """One line, at most ``width`` columns. A newline in a label breaks a chart."""
    line = " ".join(str(text if text is not None else "").split())
    if width <= 0:
        return ""
    return line if len(line) <= width else line[: max(1, width - 1)] + "…"


def _num(value: float) -> str:
    """An axis number: an integral value loses the pointless ``.0``."""
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


def _samples(values, width: int) -> list[float]:
    """``values`` resampled onto exactly ``width`` columns.

    Down by *max*, so a spike that a mean would average away survives. Up by
    nearest neighbour, so a short series fills the panel it was handed instead of
    hugging one edge underneath x-axis labels drawn for the full width.
    """
    if width <= 0:
        return []
    vals = [float(v) for v in values]
    if not vals or len(vals) == width:
        return vals
    if len(vals) < width:
        return [vals[i * len(vals) // width] for i in range(width)]
    size = len(vals) / width
    return [
        max(vals[int(i * size) : max(int((i + 1) * size), int(i * size) + 1)] or [0.0])
        for i in range(width)
    ]


def _blocks(value: float, maximum: float, width: int) -> str:
    """A bar with 1/8-cell resolution.

    Eighths rather than whole cells because these bars are short: at width 20 a
    whole-cell bar quantises to 5% steps, which draws "1 of 30" as zero.
    """
    if width <= 0:
        return ""
    if maximum <= 0 or value <= 0:
        return _ZERO * width
    eighths = int(round(min(1.0, value / maximum) * width * 8))
    full, rem = divmod(eighths, 8)
    out = _BLOCKS[-1] * min(full, width)
    if len(out) < width and rem:
        out += _BLOCKS[rem - 1]
    return out + _ZERO * (width - len(out))


# -- charts ----------------------------------------------------------------
def sparkline(values, width: int) -> str:
    """One row of blocks in which zero reads as *empty*, not as a small bar.

    Scaled from zero, not from the minimum: on these series zero is the value
    worth seeing — no slots busy, nothing completed — and a min-anchored scale
    hides it.
    """
    if width <= 0:
        return ""
    cells = _samples(values, width)
    if not cells:
        return ""
    top = max(cells)
    if top <= 0:
        return _ZERO * width
    scale = (len(_BLOCKS) - 1) / top
    return "".join(
        _ZERO if v <= 0 else _BLOCKS[min(len(_BLOCKS) - 1, int(v * scale))] for v in cells
    )


def area(values, width: int, height: int, *, y_label: bool = True) -> list[str]:
    """The same series given ``height`` rows to use, with a labelled y axis.

    A one-row sparkline can only show shape; with rows to spend, the difference
    between "climbing" and "flat since lunch" is a slope you can see. The scale
    column carries the max at the top and 0 at the bottom, because a curve with
    no number against it says only that something changed.

    The bottom row keeps the sparkline's convention that zero draws as ``·``, so
    an all-zero chart is still visibly a chart and not a blank panel.
    """
    if width <= 0 or height <= 0:
        return []
    top = max((float(v) for v in values), default=0.0)
    plot, pad = width, 0
    if y_label:
        pad = max(len(_num(top)), 1)
        if width - pad - 2 < 1:  # no room for a scale column; the data wins
            pad = 0
        else:
            plot = width - pad - 2
    cells = _samples(values, plot)
    if not cells:
        return []

    rows = []
    for row in range(height):
        level = height - 1 - row  # whole rows of value below this one
        line = []
        for value in cells:
            fill = value / top * height - level if top > 0 else 0.0
            if fill >= 1.0:
                line.append(_BLOCKS[-1])
            elif fill <= 0.0:
                line.append(_ZERO if level == 0 else " ")
            else:
                line.append(_BLOCKS[min(len(_BLOCKS) - 1, int(fill * len(_BLOCKS)))])
        if pad:
            label = _num(top) if row == 0 else ("0" if row == height - 1 else "")
            line.insert(0, f"{label:>{pad}} {'┤' if label else '│'}")
        rows.append(_fit("".join(line), width))
    return rows


def bars(pairs, width: int, *, limit: int | None = None, fmt=None) -> list[str]:
    """Horizontal bars in the order given, with a scale line under them.

    The scale line is not decoration: without it a bar chart shows only *rank*,
    and "the longest phase took 4 minutes" draws identically to "it took 4
    hours". ``fmt`` renders the value column — pass one when the numbers are not
    plain counts.

    The bar column is what carries the comparison, so it is what keeps the space
    a narrow panel leaves over: the label yields first.
    """
    items = list(pairs)[: limit if limit is not None and limit >= 0 else None]
    if width <= 0 or not items:
        return []
    fmt = _num if fmt is None else fmt
    labels = [_clip(name, 20) for name, _ in items]
    texts = [str(fmt(value)) for _, value in items]
    top = max(float(value) for _, value in items)

    value_w = max(len(text) for text in texts)
    label_w = min(max(len(label) for label in labels), max(0, width - value_w - 6))
    bar_w = max(0, width - label_w - value_w - 2)
    labels = [_clip(label, label_w) for label in labels]

    out = [
        _fit(f"{label:<{label_w}} {_blocks(value, top, bar_w)} {text:>{value_w}}", width)
        for label, text, (_, value) in zip(labels, texts, items)
    ]
    out.append(_fit(" " * (label_w + 1) + axis(["0", fmt(top)], bar_w), width))
    return out


def meter(frac: float, width: int) -> str:
    """A proportional fill bar for a 0..1 fraction, clamped at both ends."""
    if width <= 0:
        return ""
    filled = max(0, min(width, round(width * frac)))
    return _FILL * filled + _TRACK * (width - filled)


def axis(labels, width: int) -> str:
    """An x-axis tick row: ``labels`` spread across ``width``, collisions dropped.

    A label that would touch its neighbour is dropped, never nudged: shifted off
    its tick it still *looks* like a reading of the axis, and a wrong reading is
    worse than a missing one. The two ends are placed first — they anchor the
    axis, so a crowded middle label is the one that goes.
    """
    if width <= 0:
        return ""
    texts = [str(text) for text in labels]
    if not texts:
        return ""
    out = [" "] * width
    last = len(texts) - 1
    taken: list[tuple[int, int]] = []
    for index in [0] + ([last] if last else []) + list(range(1, last)):
        text = texts[index]
        if not text or len(text) > width:
            continue
        start = 0 if last == 0 else round(index * (width - len(text)) / last)
        start = max(0, min(start, width - len(text)))
        stop = start + len(text)
        if any(start < end + _GAP and begin < stop + _GAP for begin, end in taken):
            continue
        out[start:stop] = text
        taken.append((start, stop))
    return "".join(out)


# -- feeding a chart from events -------------------------------------------
def time_grid(t0: float, t1: float, width: int) -> list[float]:
    """``width`` sample instants spanning ``t0``..``t1`` inclusive."""
    if width <= 0:
        return []
    if width == 1 or t1 <= t0:
        return [t0] * width
    step = (t1 - t0) / (width - 1)
    return [t0 + step * i for i in range(width)]


def hold_last(points, grid) -> list[float]:
    """Sample a step function onto a time grid: last value at or before each instant.

    This is what makes the x-axis honest. The supervisor's series are event-
    indexed — one point per launch or completion — so charting the raw values
    plots *event number*, not time, and a wall-clock axis under it would be a
    lie: ten completions in a minute would take the same width as a six-hour
    stall. Resampling onto uniform time first makes the flat stretch look flat,
    which is the single most important thing this dashboard has to show.
    """
    ordered = sorted(points, key=lambda p: p[0])
    out: list[float] = []
    index = 0
    current = 0.0
    for instant in grid:
        while index < len(ordered) and ordered[index][0] <= instant:
            current = float(ordered[index][1])
            index += 1
        out.append(current)
    return out


def axis_time(ts: float | None, span: float) -> str:
    """A chart tick at the resolution the window deserves.

    Seconds on a six-hour axis are noise that also widen the label enough to
    collide with its neighbour; a date on a ten-minute axis is the same mistake
    in the other direction.
    """
    if ts is None:
        return "—"
    try:
        moment = datetime.fromtimestamp(ts)
    except (OSError, OverflowError, ValueError):
        return "—"
    if span >= 36 * 3600:
        return moment.strftime("%m-%d %H:%M")
    if span <= 600:
        return moment.strftime("%H:%M:%S")
    return moment.strftime("%H:%M")


#: A projection drawn ahead of the readings, and a break in the series.
PROJ = "∙"
BREAK = "╎"


def _levels(top: float, marks) -> list[tuple[float, str]]:
    return sorted(((float(lvl), ch) for lvl, ch in marks if 0 < float(lvl) <= top),
                  key=lambda m: m[0])


def level_plot_width(width: int, top: float = 100.0, marks=()) -> int:
    """The columns :func:`level_chart` plots in after its scale column, so a
    caller can sample exactly one value per column. Resampled down by max, a
    fall at the right edge (a reset, another account) stayed hidden behind the
    peak before it."""
    labels = [_num(top), "0"] + [_num(lvl) for lvl, _ in _levels(top, marks)]
    plot = width - max(len(t) for t in labels) - 1
    return plot if plot >= 1 else width


def level_chart(values, width: int, height: int, *, top: float = 100.0,
                marks=(), projection=(), breaks=()) -> list[str]:
    """A series on a fixed ``0..top`` scale, with reference levels drawn across it.

    :func:`area` scales to the series' own maximum, which is right for a count and
    wrong for a usage percentage: 30% would fill the chart as fully as 95%, and a
    cap at 90% would have no row to sit on. Here the scale is fixed, and each
    ``(level, char)`` in ``marks`` is drawn with ``char`` through the empty cells
    of the row holding that level, with the level in the scale column, so the line
    the swarm stops at is on the chart the value climbs towards. ``None`` values
    (no reading yet) are left blank.

    ``projection`` (one value or ``None`` per value) is drawn as a dotted line
    through the cells with no reading, and each index in ``breaks`` (plot
    columns) as a dashed vertical line through the empty cells of its column.
    """
    if width <= 0 or height <= 0 or top <= 0:
        return []
    marks = _levels(top, marks)
    plot = level_plot_width(width, top, marks)
    pad = 0 if plot == width else width - plot - 1
    raw = list(values)
    if not raw:
        return []
    cells = _samples([-1.0 if v is None else v for v in raw], plot)
    proj = list(projection)
    ahead = (_samples([-1.0 if v is None else v for v in proj], plot) if proj
             else [-1.0] * plot)
    cut = set(breaks)

    def band(row: int) -> tuple[float, float]:
        level = height - 1 - row
        return level * top / height, (level + 1) * top / height

    rows = []
    for row in range(height):
        lo, hi = band(row)
        # Two levels in one row: the lower one shows, it is the one reached first.
        mark = next((m for m in marks if lo <= m[0] < hi or (m[0] == top and row == 0)), None)
        level = height - 1 - row
        line = []
        for i, value in enumerate(cells):
            if value < 0:
                p = ahead[i]
                if p >= 0 and (lo <= p < hi or (row == 0 and p >= hi)):
                    line.append(PROJ)
                else:
                    line.append(BREAK if i in cut else mark[1] if mark else " ")
                continue
            fill = value / top * height - level
            if fill >= 1.0:
                line.append(_BLOCKS[-1])
            elif fill > 0.0:
                line.append(_BLOCKS[min(len(_BLOCKS) - 1, int(fill * len(_BLOCKS)))])
            elif i in cut:
                line.append(BREAK)
            elif mark:
                line.append(mark[1])
            else:
                line.append(_ZERO if level == 0 else " ")
        if pad:
            label = (_num(mark[0]) if mark else _num(top) if row == 0
                     else "0" if row == height - 1 else "")
            line.insert(0, f"{label:>{pad}}{'┤' if label else '│'}")
        rows.append(_fit("".join(line), width))
    return rows
