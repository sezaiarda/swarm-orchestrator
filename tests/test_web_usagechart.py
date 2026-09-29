"""The Usage tab: readings as segments, account-switch breaks, and the projection."""

from __future__ import annotations

from types import SimpleNamespace

from swarm_orchestrator.usage import Sample
from swarm_orchestrator.web import usagechart

H = 3600.0


def _week(ts: float, pct: float, resets: float) -> Sample:
    return Sample(ts=ts, week_pct=pct, week_resets_at=resets)


def test_a_steady_window_is_one_segment():
    samples = [_week(i * H, 10 + i, 100 * H) for i in range(5)]
    segs, resets, breaks = usagechart.segments(samples, "week", 0.0, 10 * H)
    assert len(segs) == 1 and resets == [] and breaks == []
    assert [p[1] for p in segs[0]] == [10, 11, 12, 13, 14]


def test_a_lagging_dip_is_held_at_the_running_maximum():
    samples = [_week(0, 20, 100 * H), _week(H, 22, 100 * H), _week(2 * H, 21, 100 * H)]
    segs, _, breaks = usagechart.segments(samples, "week", 0.0, 3 * H)
    assert breaks == [] and [p[1] for p in segs[0]] == [20, 22, 22]


def test_a_reset_drops_to_zero_inside_the_segment():
    samples = [_week(0, 80, 2 * H), _week(H, 85, 2 * H), _week(3 * H, 3, 200 * H)]
    segs, resets, breaks = usagechart.segments(samples, "week", 0.0, 4 * H)
    assert resets == [2 * H] and breaks == [] and len(segs) == 1
    assert [2 * H, 0.0] in segs[0]


def test_an_account_switch_is_a_break_not_a_drop():
    # 90% on one account, then the other account's 31% under a different reset.
    samples = [_week(0, 88, 50 * H), _week(H, 90, 50 * H), _week(2 * H, 31, 90 * H),
               _week(3 * H, 32, 90 * H)]
    segs, resets, breaks = usagechart.segments(samples, "week", 0.0, 4 * H)
    assert breaks == [2 * H] and resets == []
    assert [[p[1] for p in s] for s in segs] == [[88, 90], [31, 32]]


def test_readings_before_the_span_enter_from_its_left_edge():
    samples = [_week(0, 40, 100 * H), _week(10 * H, 45, 100 * H)]
    segs, _, _ = usagechart.segments(samples, "week", 5 * H, 11 * H)
    assert segs[0][0] == [5 * H, 40]


def test_projection_hits_the_cap_before_the_reset():
    got = usagechart.project(80.0, 0.0, 10 * H, 5.0, [90.0, 100.0])
    assert got["hits"] == {"at": 7200, "pct": 90.0} and got["to"] == [7200, 90.0]


def test_projection_ends_at_the_reset_when_it_comes_first():
    got = usagechart.project(20.0, 0.0, 2 * H, 5.0, [90.0, 100.0])
    assert got["hits"] is None and got["to"] == [round(2 * H), 30.0]


def test_projection_without_burn_says_so():
    assert usagechart.project(20.0, 0.0, 2 * H, 0.0, [90.0])["to"] is None


def _cfg():
    return SimpleNamespace(usage_enabled=True, usage_rules=[
        {"window": "week", "at": 90, "action": "pause"},
        {"window": "week", "at": 95, "action": "down"},
        {"window": "five_hour", "at": 90, "action": "pause"}])


def test_windows_and_the_next_cap():
    now = 10 * H
    samples = [Sample(ts=now - 60, five_pct=50, five_resets_at=now + 4 * H,
                      week_pct=85, week_resets_at=now + 48 * H)]
    wins = usagechart.windows(_cfg(), samples, burn={"week": 1.0, "five_hour": 2.0}, busy=2,
                              run_usage=None, hold={}, override={}, now=now)
    week = next(w for w in wins if w["key"] == "week")
    assert week["rules"] == [{"at": 90.0, "action": "pause"}, {"at": 95.0, "action": "down"}]
    assert week["projection"]["hits"]["pct"] == 90.0 and week["basis"] == "burn"
    five = next(w for w in wins if w["key"] == "five")
    assert five["projection"]["hits"] is None  # 50% + 4 h x 4 %/h = 66% at the reset
    nxt = usagechart.next_cap(wins, done_at=now + 100 * H)
    assert nxt["hits"]["window"] == "week" and nxt["before_done"] is True
    held = usagechart.next_cap([{**week, "held": True}], None)
    assert held["held"] == ["Weekly"]


def test_the_web_breaks_where_the_tui_does():
    from swarm_orchestrator import usage
    from swarm_orchestrator.tui import usagebox

    samples = [_week(0, 88, 50 * H), _week(H, 90, 50 * H), _week(2 * H, 31, 90 * H),
               _week(3 * H, 32, 90 * H)]
    segs, _, breaks = usagechart.segments(samples, "week", 0.0, 4 * H)
    assert breaks == usage.switch_times(samples)
    # The last segment is the TUI's series from the switch on; the old one is cut off.
    assert [tuple(p) for p in segs[-1]] == [(2 * H, 31), (3 * H, 32)]
    assert [p[1] for p in usagebox.series(samples, "week", 0.0, 4 * H)] == [88, 90, 31, 32]


def test_burn_and_projection_start_from_the_current_account():
    samples = [_week(0, 88, 50 * H), _week(H, 90, 50 * H), _week(2 * H, 31, 90 * H),
               _week(3 * H, 32, 90 * H)]
    assert usagechart._reading(samples, "week", 4 * H) == (3 * H, 32, 90 * H)
    cfg = SimpleNamespace(usage_enabled=False, usage_rules=[])
    (_, week) = usagechart.windows(cfg, samples, burn={"week": 1.0}, busy=1, run_usage=None,
                                   hold=None, override=None, now=4 * H)
    assert week["pct"] == 32 and week["projection"]["rate_h"] == 1.0
    assert week["breaks"] == [2 * H]


def test_a_lagging_reading_of_the_other_account_is_ignored():
    # One reading under another account's reset, contradicted by the next: ignored.
    samples = [_week(0, 40, 50 * H), _week(H, 5, 90 * H), _week(2 * H, 41, 50 * H)]
    segs, _, breaks = usagechart.segments(samples, "week", 0.0, 3 * H)
    assert breaks == [] and [[p[1] for p in s] for s in segs] == [[40, 41]]
