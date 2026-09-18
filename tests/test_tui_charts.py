"""Tests for the plain-text charts.

One property matters more than everything else here and gets tested by brute
force: **a chart line is exactly as wide as it was asked to be**. These are drawn
into fixed-width Textual panels on a repaint tick, and a line one column too long
does not look slightly wrong — it reflows the panel and shoves the rest of the
screen around, on a dashboard whose whole promise is that nothing moves unless
something happened. So every function is swept over every awkward width (0,
negative, narrower than the data, narrower than its own axis labels) with every
awkward series (nothing, one point, all equal, all zero, negative values), and
each line it returns is measured.

The rest of the cases are the ones the module exists for: zero must not draw like
a small value, an axis label must not be nudged off its tick, and a bar chart
must carry a scale rather than just a ranking.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime

import pytest

from swarm_orchestrator.tui import charts

BASE = datetime(2026, 8, 27, 9, 0, 0).timestamp()

#: Every series that has ever broken a chart, plus the ones that obviously would.
SERIES = [
    [],
    [0.0],
    [7.0],
    [0.0, 0.0, 0.0, 0.0],
    [3.0] * 6,
    [0.0, 1.0, 0.0, 9.0, 4.0],
    [-2.0, 0.0, 5.0],
    list(range(200)),
]

WIDTHS = [-5, 0, 1, 2, 3, 7, 12, 40, 64]


def columns(line: str) -> int:
    """Printable columns. Every glyph these charts use must be single-width."""
    assert "\n" not in line
    for char in line:
        assert unicodedata.east_asian_width(char) not in ("W", "F"), repr(char)
    return len(line)


# -- the invariant ---------------------------------------------------------
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("values", SERIES)
def test_sparkline_is_exactly_as_wide_as_asked(values, width):
    out = charts.sparkline(values, width)
    assert columns(out) == (width if values and width > 0 else 0)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("values", SERIES)
@pytest.mark.parametrize("height", [1, 2, 6])
@pytest.mark.parametrize("y_label", [True, False])
def test_area_rows_are_exactly_as_wide_as_asked(values, width, height, y_label):
    rows = charts.area(values, width, height, y_label=y_label)
    assert rows == [] if not values or width <= 0 else len(rows) == height
    assert all(columns(row) == width for row in rows)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize(
    "pairs",
    [
        [],
        [("only", 1.0)],
        [("zero", 0.0), ("also-zero", 0.0)],
        [("a-very-long-phase-name-indeed", 3600.0), ("b", 1.0), ("c", -4.0)],
    ],
)
def test_bars_rows_are_exactly_as_wide_as_asked(pairs, width):
    rows = charts.bars(pairs, width)
    assert rows == [] if not pairs or width <= 0 else len(rows) == len(pairs) + 1
    assert all(columns(row) == width for row in rows)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("frac", [-1.0, 0.0, 0.004, 0.5, 1.0, 3.0])
def test_meter_is_exactly_as_wide_as_asked(frac, width):
    assert columns(charts.meter(frac, width)) == max(0, width)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize(
    "labels",
    [[], [""], ["09:00"], ["09:00", "17:00"], ["09:00", "13:00", "17:00"], ["x"] * 8],
)
def test_axis_is_exactly_as_wide_as_asked(labels, width):
    assert columns(charts.axis(labels, width)) == (width if labels and width > 0 else 0)


def test_area_never_overflows_even_when_its_own_scale_column_does_not_fit():
    """A five-digit maximum in a four-column panel must still return four columns."""
    rows = charts.area([12345.0, 0.0], 4, 3)
    assert [columns(row) for row in rows] == [4, 4, 4]


# -- sparkline -------------------------------------------------------------
def test_sparkline_shows_zero_as_empty():
    """An idle hour and a quiet-but-busy one must not look the same."""
    out = charts.sparkline([0.0, 4.0, 0.0], 3)
    assert out[0] == "·" and out[2] == "·" and out[1] not in ("·", " ")


def test_sparkline_scales_from_zero_not_from_the_minimum():
    """Anchoring on min would draw the smallest value as empty and hide the floor."""
    out = charts.sparkline([4.0, 5.0], 2)
    assert out[0] != "·"
    assert out[1] == "█"


def test_sparkline_of_nothing_and_of_nothing_happening():
    assert charts.sparkline([], 10) == ""
    assert charts.sparkline([0.0, 0.0], 4) == "·" * 4
    assert charts.sparkline([1.0], -1) == ""


def test_sparkline_downsamples_by_peak_so_a_spike_survives():
    values = [0.0] * 99 + [50.0]
    assert "█" in charts.sparkline(values, 10)


# -- area ------------------------------------------------------------------
def test_area_labels_the_scale_with_max_on_top_and_zero_at_the_bottom():
    rows = charts.area([0.0, 4.0, 8.0], 40, 4)
    assert rows[0].startswith("8 ┤")
    assert rows[-1].startswith("0 ┤")
    assert rows[1].startswith("  │") and rows[2].startswith("  │")


def test_area_without_a_scale_column_spends_every_column_on_data():
    plain = charts.area([1.0, 2.0], 20, 3, y_label=False)
    labelled = charts.area([1.0, 2.0], 20, 3)
    assert "┤" not in "".join(plain)
    assert "┤" in "".join(labelled)


def test_area_gives_a_rising_series_a_visible_slope():
    """The reason it is worth more than one row: flat and climbing must differ."""
    rising = charts.area(list(range(20)), 24, 4, y_label=False)
    assert rising[0].count("█") < rising[-1].count("█")
    flat = charts.area([5.0] * 20, 24, 4, y_label=False)
    assert flat[0].count("█") == flat[-1].count("█")


def test_area_keeps_the_sparklines_dotted_floor_so_an_idle_chart_is_still_a_chart():
    rows = charts.area([0.0, 0.0, 0.0], 20, 3, y_label=False)
    assert rows[-1] == "·" * 20
    assert rows[0].strip() == ""


def test_area_draws_a_value_too_small_for_a_whole_row_rather_than_dropping_it():
    rows = charts.area([100.0, 1.0], 2, 4, y_label=False)
    assert rows[-1][1] not in ("·", " ")


def test_area_of_one_point_and_of_nothing():
    assert charts.area([], 20, 3) == []
    assert charts.area([1.0], 20, 0) == []
    assert len(charts.area([1.0], 20, 3)) == 3


# -- bars ------------------------------------------------------------------
def test_bars_carry_a_scale_not_just_a_ranking():
    """"The longest phase took 4 minutes" must not draw like "it took 4 hours"."""
    rows = charts.bars([("dash-W7", 3600.0), ("dash-W8", 900.0)], 48)
    assert len(rows) == 3
    assert rows[0].startswith("dash-W7") and "3600" in rows[0]
    assert rows[-1].lstrip().startswith("0") and "3600" in rows[-1]


def test_bars_longest_value_fills_the_bar_column():
    rows = charts.bars([("a", 10.0), ("b", 5.0)], 40)
    assert rows[0].count("█") > rows[1].count("█")
    assert "·" not in rows[0].split()[1]  # the top bar has no empty tail


def test_bars_honour_a_limit_and_keep_the_order_they_were_given():
    rows = charts.bars([("a", 3.0), ("b", 2.0), ("c", 1.0)], 40, limit=2)
    assert len(rows) == 3  # two bars and the scale line
    assert rows[0].startswith("a") and rows[1].startswith("b")
    assert charts.bars([("a", 1.0)], 40, limit=0) == []


def test_bars_format_the_value_column_with_the_callers_units():
    rows = charts.bars([("dash-W7", 3600.0)], 48, fmt=lambda v: f"{v / 3600:.0f}h")
    assert rows[0].rstrip().endswith("1h")
    assert "1h" in rows[-1]


def test_bars_squeeze_the_label_before_the_bar():
    """The bar is the comparison; the label is only what it is a comparison of."""
    wide = charts.bars([("a-long-phase-name", 1.0)], 60)
    narrow = charts.bars([("a-long-phase-name", 1.0)], 16)
    assert wide[0].startswith("a-long-phase-name")
    assert not narrow[0].startswith("a-long-phase-name")
    assert "█" in narrow[0] and narrow[0].rstrip().endswith("1")


def test_bars_survive_an_all_zero_maximum():
    rows = charts.bars([("P1", 0.0)], 24)
    assert len(rows) == 2
    assert "█" not in rows[0]


def test_bars_of_nothing():
    assert charts.bars([], 40) == []
    assert charts.bars([("a", 1.0)], 0) == []


# -- meter -----------------------------------------------------------------
def test_meter_fills_in_proportion_and_clamps_at_both_ends():
    assert charts.meter(0.5, 10) == "█████░░░░░"
    assert charts.meter(0.0, 4) == "░░░░"
    assert charts.meter(1.0, 4) == "████"
    assert charts.meter(-3.0, 4) == "░░░░"
    assert charts.meter(9.0, 4) == "████"
    assert charts.meter(0.5, 0) == ""


# -- axis ------------------------------------------------------------------
def test_axis_anchors_the_ends_and_drops_a_colliding_middle():
    wide = charts.axis(["09:00", "13:00", "17:00"], 40)
    assert wide.startswith("09:00") and wide.rstrip().endswith("17:00") and "13:00" in wide
    tight = charts.axis(["09:00", "13:00", "17:00"], 14)
    assert tight.startswith("09:00") and tight.rstrip().endswith("17:00")
    assert "13:00" not in tight


def test_axis_never_nudges_a_label_off_its_tick():
    """A label moved to make room still reads as a measurement, and a wrong one."""
    out = charts.axis(["0", "1", "2", "3", "4", "5", "6", "7"], 12)
    assert out.startswith("0") and out.rstrip().endswith("7")
    assert out.count("1") <= 1


def test_axis_of_a_single_label_and_of_nothing():
    assert charts.axis(["only"], 10).startswith("only")
    assert charts.axis([], 10) == ""
    assert charts.axis(["09:00"], 0) == ""
    assert charts.axis(["far-too-long-to-fit"], 6).strip() == ""


# -- feeding a chart from events -------------------------------------------
def test_time_grid_spans_the_window_inclusively():
    assert charts.time_grid(100.0, 200.0, 5) == [100.0, 125.0, 150.0, 175.0, 200.0]
    assert charts.time_grid(100.0, 100.0, 3) == [100.0] * 3
    assert charts.time_grid(0.0, 1.0, 0) == []


def test_hold_last_resamples_a_step_function_onto_time():
    """The series is event-indexed; without this the x-axis would be a lie."""
    points = [(0.0, 1.0), (10.0, 5.0)]
    assert charts.hold_last(points, [0.0, 5.0, 10.0, 15.0]) == [1.0, 1.0, 5.0, 5.0]
    # Before the first event the value is zero, not the first event's value.
    assert charts.hold_last([(10.0, 5.0)], [0.0, 10.0]) == [0.0, 5.0]
    assert charts.hold_last([], [0.0, 1.0]) == [0.0, 0.0]


def test_hold_last_makes_a_stall_look_like_a_stall():
    """Ten completions in a minute must not take the width of a six-hour gap."""
    points = [(0.0, 1.0), (60.0, 10.0)]
    values = charts.hold_last(points, charts.time_grid(0.0, 3600.0, 10))
    assert values[-1] == 10.0
    assert values.count(10.0) == 9  # nine tenths of the window is the flat part


def test_axis_time_matches_the_window_it_labels():
    """Seconds on a six-hour axis are noise; a date on a ten-minute one is worse."""
    assert charts.axis_time(BASE, 300).count(":") == 2       # HH:MM:SS
    assert charts.axis_time(BASE, 6 * 3600).count(":") == 1  # HH:MM
    assert "-" in charts.axis_time(BASE, 3 * 86400)          # a date is needed
    assert charts.axis_time(None, 60) == "—"
    assert charts.axis_time(1e30, 60) == "—"


def test_a_time_chart_composes_without_overflowing_its_panel():
    """The whole point of the module, drawn end to end at one fixed width."""
    width = 52
    points = [(BASE + i * 600.0, float(i)) for i in range(12)]
    values = charts.hold_last(points, charts.time_grid(BASE, BASE + 7200.0, width))
    rows = charts.area(values, width, 5)
    rows.append(charts.axis([charts.axis_time(BASE, 7200), charts.axis_time(BASE + 7200, 7200)],
                            width))
    assert [columns(row) for row in rows] == [width] * 6
