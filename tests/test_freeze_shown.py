"""A frozen run says so, and its frozen hours are counted as nothing.

* **the word** — wherever a hold is shown (``swarm why``, the dashboard's
  footer and status bar, the web board, the forecast's headline) a freeze is
  shown the way a pause and a drain are, and before them;
* **the hours** — a phase's duration, a running worker's age, the forecast's
  work times and the pace leave out what the run stood frozen for: neither
  work nor idle time with a free slot.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App

from swarm_orchestrator import freezer
from swarm_orchestrator import pace
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import why as why_mod
from swarm_orchestrator.eta import engine, forecast, holds, record
from swarm_orchestrator.tui import alerts, books, data, home, shell
from swarm_orchestrator.web import board as board_mod

from test_eta_forecast import NOW as ETA_NOW
from test_eta_forecast import cfg as eta_cfg  # noqa: F401 - a fixture
from test_eta_forecast import words_forecast
from test_tui_home import NOW, FakeDash, healthy, plain, slot
from test_web_board import feed  # noqa: F401 - a fixture
from test_why import _cfg, _state

H = 3600.0
RECORD = {"since": NOW - 2 * H, "stage": freezer.FROZEN, "quiesced": True, "awake": [],
          "cgroups": [{"path": "/a", "kind": "worker", "id": "P0"},
                      {"path": "/b", "kind": "operator", "id": "op-1"}]}


# -- the word ---------------------------------------------------------------------
def test_why_says_a_ready_row_waits_for_the_thaw_before_any_other_hold(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, slots=2, paused=True, frozen=dict(RECORD))

    detail = why_mod.explain(cfg, "P0").detail

    assert "FROZEN" in detail and "`swarm thaw`" in detail and "PAUSED" not in detail


def test_why_counts_a_park_timer_from_when_the_freeze_began(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    since = time.time() - 2 * H
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(state_mod.State.fresh(2).__dict__)
        st.waiting["P0"] = since + 90  # a minute and a half left when it froze
        st.frozen = dict(RECORD, since=since)

    assert "(in 90s it moves" in why_mod.explain(cfg, "P0").detail


def test_the_footer_says_frozen_before_a_pause_or_a_drain():
    snap = healthy(paused=True, drain={"waiting": ["1 worker"], "then": ""}, frozen=RECORD)

    text = plain(home.footer_line(FakeDash(snap), 200, now=NOW))

    assert "Frozen since " in text and "2 groups frozen" in text and "`swarm thaw`" in text
    assert "paused" not in text and "Draining" not in text
    assert alerts.problems(FakeDash(snap), NOW)[0][1] == alerts.WARN


def test_frozen_hours_are_not_a_quiet_run_in_the_footer():
    quiet = dict(ok=True, supervisor_pid=42, supervisor_alive=True,
                 slots=[slot(0, "dash-W3", busy=True)], last_event_at=NOW - 2 * H - 30)
    assert "nothing has happened" in plain(
        home.footer_line(FakeDash(data.Snapshot(**quiet)), 200, now=NOW))

    text = plain(home.footer_line(FakeDash(data.Snapshot(**quiet, frozen=RECORD)), 200, now=NOW))

    assert "Frozen since " in text
    assert "nothing has happened" not in text and "quiet for" not in text


def test_the_status_bar_says_frozen():
    seen = {}

    class Host(App):
        def compose(self):
            yield shell.StatusBar(id="statusbar")

    app = Host()
    app.cfg = SimpleNamespace(name="demo", session="demo")

    async def steps(snap) -> str:
        async with app.run_test(size=(160, 10)) as pilot:
            bar = app.query_one(shell.StatusBar)
            bar.update_from(FakeDash(snap))
            await pilot.pause()
            seen["bar"], seen["classes"] = str(bar.render()), set(bar.classes)
        return seen["bar"]

    since = time.time() - 3 * H
    frozen = data.Snapshot(ok=True, supervisor_pid=42, supervisor_alive=True, paused=True,
                           frozen=dict(RECORD, since=since), last_event_at=since - 20,
                           slots=[slot(0, "dash-W3", busy=True)])
    bar = asyncio.run(asyncio.wait_for(steps(frozen), timeout=30))

    assert "● frozen" in bar and "paused" not in bar and "live" not in bar
    # Three hours frozen with a busy slot is not a run nothing happens in.
    assert not seen["classes"] & {"-bad", "-attention"}


def test_a_snapshot_carries_the_frozen_record(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _state(cfg, slots=1, frozen=dict(RECORD))
    state = json.loads(cfg.state_path.read_text())

    snap = data.build_snapshot(cfg, state, graph={"P0": set()})

    assert snap.frozen == RECORD
    assert data.build_snapshot(cfg, dict(state, frozen="junk"), graph={}).frozen == {}


def test_the_web_board_header_says_frozen_in_words(feed):  # noqa: F811
    assert feed.board["header"]["frozen"] == ""
    st = json.loads(feed.cfg.state_path.read_text())
    st["frozen"] = dict(RECORD, since=1_790_000_000.0)
    feed.cfg.state_path.write_text(json.dumps(st))

    assert feed.refresh() is True

    said = feed.board["header"]["frozen"]
    assert said.startswith("Frozen since ") and "2 groups frozen" in said
    assert "`swarm thaw`" in said
    v = feed.version
    assert feed.refresh(force=True) is False and feed.version == v  # time passing changes nothing


def test_the_web_page_shows_the_frozen_pill_before_the_other_holds():
    page = (Path(board_mod.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    frozen, drain = page.index('txt = "Frozen"'), page.index('txt = "Draining"')
    assert page.index('txt = "Finished"') < frozen < drain
    assert "st.title = h.frozen || h.drain" in page


def test_the_forecast_says_frozen_and_the_work_left_once_it_runs_again():
    calendar = holds.from_state(state_mod.State(paused=True, frozen=dict(RECORD)), [], [], {},
                                ETA_NOW)
    assert calendar.stopped == holds.FROZEN
    assert holds.from_state(state_mod.State(paused=True), [], [], {}, ETA_NOW).stopped \
        == holds.PAUSED
    fc = forecast.Forecast(**(words_forecast().__dict__ | {"stopped": holds.FROZEN}))
    assert books.overall_line(fc, ETA_NOW) == (
        "5 rows left · frozen · ~35h of work once it runs again · 2 more wait on you")


# -- the hours ----------------------------------------------------------------------
LOG = "\n".join([
    "2026-09-28 10:00:00.000 1.0 LAUNCH P0 slot=0",
    "2026-09-28 10:00:00.000 1.0 LAUNCH P1 slot=1",
    "2026-09-28 11:00:00.000 2.0 EVENT waiting P0 parked=False",
    "2026-09-28 13:30:00.000 3.0 EVENT resumed P0 cancelled=True parked=False",
    "2026-09-28 16:00:00.000 4.0 EVENT done P0 ok",
])


def _events():
    events = data.parse_events(LOG)
    start = events[0].ts
    # Frozen from 12:00 to 15:00: three hours, an hour and a half of them
    # inside P0's wait on the owner.
    return events, start, [(start + 2 * H, start + 5 * H)]


def test_a_phase_took_the_time_it_was_awake_for():
    events, start, frozen = _events()
    state = {"slots": [{"id": 1, "busy": True, "phase": "P1"}]}

    plain_runs = {r.phase: r for r in data.build_history(events, state=state)}
    runs = {r.phase: r for r in data.build_history(events, state=state, frozen=frozen)}

    assert plain_runs["P0"].duration_s == 6 * H and plain_runs["P0"].frozen_s == 0.0
    assert runs["P0"].frozen_s == 3 * H and runs["P0"].duration_s == 3 * H
    assert runs["P0"].ended_at == start + 6 * H  # when it ended is still when it ended
    # Still running: its age so far leaves the frozen hours out too.
    assert runs["P1"].running and runs["P1"].frozen_s == 3 * H
    assert runs["P1"].duration_s == pytest.approx(time.time() - start - 3 * H, abs=5)
    assert data.typical_durations([runs["P0"]]) == [3 * H]


def test_a_workers_age_counts_from_a_start_moved_past_the_freeze():
    now = 1_000_000.0
    frozen = [(now - 5 * H, now - 2 * H), (now - H, now)]  # the second is still on
    starts = {"before": now - 6 * H, "between": now - 1.5 * H, "inside": now - 0.5 * H}

    got = data.awake_starts(starts, frozen, now)

    assert now - got["before"] == 2 * H  # six hours ago, four of them frozen
    assert now - got["between"] == 0.5 * H
    assert now - got["inside"] == 0.0
    assert data.awake_starts(starts, [], now) == starts


def test_a_work_time_leaves_out_the_freeze_and_counts_no_second_twice():
    events, start, frozen = _events()
    history = data.build_history(events, state={"slots": []}, frozen=frozen)

    (wait,) = record.attempts(history, events)
    (both,) = record.attempts(history, events, frozen)

    assert wait.phase == "P0" and wait.waited_s == 2.5 * H  # the owner wait alone
    # 11:00-13:30 waiting and 12:00-15:00 frozen cover 11:00-15:00: four hours, not 5.5.
    assert both.waited_s == 4 * H
    rec = record.from_sources(history, events, pace.History(), frozen)
    (sample,) = record.samples(rec, start + 7 * H)
    assert sample.seconds == 2 * H  # six on the clock, two of them work


def test_the_forecast_reads_the_spans_and_a_running_rows_awake_age(eta_cfg):  # noqa: F811
    cfg = eta_cfg
    now = time.time()
    freezer.close_span(cfg, now - 5 * H, now - 2 * H)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("a-W1")
    st = state_mod.read(cfg)
    launch = data.Event(now - 6 * H, "launch", "a-W1", None, {}, "LAUNCH a-W1 slot=0")

    inputs = engine.gather(cfg, st, events=[launch], history=[], ledger_history=pace.History(),
                           usage=[], now=now)

    assert inputs.frozen == ((now - 5 * H, now - 2 * H),)
    assert inputs.started == {"a-W1": now - 6 * H}  # the key of the cache does not move
    assert engine.plan_of(inputs).running == {"a-W1": pytest.approx(3 * H)}
    with state_mod.transaction(cfg) as st:
        st.frozen = {"since": now - H, "stage": freezer.FROZEN}
    frozen_now = engine.gather(cfg, state_mod.read(cfg), events=[launch], history=[],
                               ledger_history=pace.History(), usage=[], now=now)
    assert engine.plan_of(frozen_now).running == {"a-W1": pytest.approx(2 * H)}
    assert engine.key(frozen_now, "f") != engine.key(inputs, "f")  # frozen is another forecast
    assert engine.holds_of(frozen_now, engine.fit(frozen_now)).stopped == holds.FROZEN


def test_a_busy_worker_burns_usage_only_in_the_hours_it_was_awake():
    from swarm_orchestrator.usage import Sample

    samples = [Sample(ts=ETA_NOW - (30 - i) * H, week_pct=40 + 2 * i,
                      week_resets_at=ETA_NOW + 20 * H, account="acct-a") for i in range(25)]
    events = [SimpleNamespace(kind="launch", phase="P0", ts=ETA_NOW - 40 * H, slot=0,
                              status=None)]

    awake = engine.burn_of(events, 1, samples, ETA_NOW)
    frozen = engine.burn_of(events, 1, samples, ETA_NOW,
                            frozen=((ETA_NOW - 5 * H, ETA_NOW),))

    # 48 points: over 40 busy hours, or over the 35 of them it was awake for.
    assert awake["week"] == pytest.approx(48 / 40, rel=0.05)
    assert frozen["week"] == pytest.approx(48 / 35, rel=0.05)


def test_frozen_hours_are_neither_work_nor_idle_in_the_pace():
    events = data.parse_events("\n".join([
        "2026-09-28 11:00:00.100 2.0 WATCHDOG idle=0s busy=[] paused=True",
        "2026-09-28 11:30:00.000 3.0 WATCHDOG idle=0s busy=[] paused=False",
    ]))
    paused = (events[0].ts, events[1].ts)
    frozen = [(paused[1] - 600, paused[1] + 3000), (paused[1] + 5 * H, paused[1] + 6 * H)]

    spans = data.idle_spans(events, frozen=frozen)

    # The pause and the freeze that overlaps it are one stretch; the later one its own.
    assert spans == [(paused[0], paused[1] + 3000), (paused[1] + 5 * H, paused[1] + 6 * H)]
    assert data.idle_spans(events) == [paused]

    now = 1_790_000_000.0
    times = {f"P{i}": now - (5 - i) * H for i in range(6)}  # one finish an hour
    t = sorted(times.values())
    froze = [(t[2] + 0.1 * H, t[2] + 0.6 * H)]  # half an hour frozen between two finishes
    assert pace.measure(times, set(), [], [], now).hours == pytest.approx(5.0)
    assert pace.measure(times, set(), data.idle_spans([], frozen=froze), [], now).hours \
        == pytest.approx(4.5)
