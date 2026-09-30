"""Runs and per-run usage: lifecycle, piecewise paces, summaries, `swarm usage`, run-aware ETA."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from swarm_orchestrator import cli, runs, state as state_mod, usage
from swarm_orchestrator.tui import data
from swarm_orchestrator.tui.data import PhaseRun, eta_sample, live_meters, Meter

H = 3600.0
T0 = 1_800_000_000.0


def _cfg(tmp_path, workers=1, iso="none"):
    state_dir = tmp_path / "state"
    (state_dir / "logs").mkdir(parents=True)
    return SimpleNamespace(state_dir=state_dir, supervisor_log=state_dir / "logs" / "supervisor.log",
                           max_workers=workers, git_isolation=iso)


def _write_samples(cfg, rows):
    d = cfg.state_dir / "meters"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "limits.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _row(ts, five=None, five_r=None, week=None, week_r=None, rid=None):
    return {"ts": ts, "run_id": rid, "five_pct": five, "five_resets_at": five_r,
            "week_pct": week, "week_resets_at": week_r}


def _log(cfg, lines):
    import time as _t

    with cfg.supervisor_log.open("a") as fh:
        for ts, msg in lines:
            stamp = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(ts)) + f".{int(ts % 1 * 1000):03d}"
            fh.write(f"{stamp} 0.000 {msg}\n")


# -- lifecycle ----------------------------------------------------------------
def test_up_down_reset_lifecycle(tmp_path):
    sd = tmp_path
    assert runs.current(sd) is None  # legacy: nothing recorded yet

    first, closed = runs.start(sd, 2, "worktree", now=T0)
    assert closed is None and runs.current(sd)["run_id"] == first["run_id"]
    assert runs.current_id(sd) == first["run_id"]

    done = runs.close(sd, now=T0 + H, summarize=lambda rec, end: {"hours": (end - rec["epoch_ts"]) / H})
    assert done["end_ts"] == T0 + H and done["summary"] == {"hours": 1.0}
    assert runs.current(sd) is None and runs.close(sd) is None  # nothing open to close

    second, _ = runs.start(sd, 1, "none", now=T0 + 2 * H)
    third, closed = runs.start(sd, 1, "none", reason="reset", now=T0 + 3 * H)
    assert closed["run_id"] == second["run_id"] and closed["end_ts"] == T0 + 3 * H
    assert "was still open" in closed["closed_by"]
    assert [r["run_id"] for r in runs.list_runs(sd)] == [third["run_id"], second["run_id"], first["run_id"]]


def test_two_starts_in_one_second_get_distinct_ids(tmp_path):
    a, _ = runs.start(tmp_path, 1, "none", now=T0)
    b, _ = runs.start(tmp_path, 1, "none", now=T0)
    assert a["run_id"] != b["run_id"] and b["run_id"].startswith(a["run_id"])


def test_a_reload_that_moves_the_config_is_noted_and_splits_the_run(tmp_path):
    runs.start(tmp_path, 1, "none", now=T0)
    assert not runs.note_config(tmp_path, 1, "none", now=T0 + H)  # no change, no note
    assert runs.note_config(tmp_path, 4, "none", now=T0 + 2 * H)
    rec = runs.current(tmp_path)
    assert runs.config_at(rec, T0 + H) == (1, "none")
    assert runs.config_at(rec, T0 + 3 * H) == (4, "none")
    assert runs.segments(rec, T0 + 5 * H) == [(T0, T0 + 2 * H, 1, "none"), (T0 + 2 * H, T0 + 5 * H, 4, "none")]


def test_a_torn_run_file_is_skipped_not_fatal(tmp_path):
    rec, _ = runs.start(tmp_path, 1, "none", now=T0)
    (runs.runs_dir(tmp_path) / "junk").mkdir()
    (runs.runs_dir(tmp_path) / "junk" / "run.json").write_text("{nope")
    assert [r["run_id"] for r in runs.list_runs(tmp_path)] == [rec["run_id"]]


def test_up_closes_a_crashed_run_at_its_last_activity_and_reset_keeps_the_live_config(tmp_path):
    cfg = _cfg(tmp_path, workers=1, iso="none")
    usage.start_run(cfg, "up", now=T0)
    runs.note_config(cfg.state_dir, 3, "none", now=T0 + H)
    _write_samples(cfg, [_row(T0 + 2 * H, week=10.0, week_r=T0 + 100 * H)])
    # Never downed; `up` two days later ends it where it went quiet.
    second, closed = usage.start_run(cfg, "up", now=T0 + 48 * H)
    assert closed["end_ts"] == T0 + 2 * H
    assert (second["max_workers"], second["isolation"]) == (1, "none")  # up reads the file
    runs.note_config(cfg.state_dir, 2, "none", now=T0 + 49 * H)
    third, closed = usage.start_run(cfg, "reset", now=T0 + 50 * H)
    assert closed["end_ts"] == T0 + 50 * H
    assert third["max_workers"] == 2  # reset restarts nothing: the live config carries over
    assert (runs.runs_dir(cfg.state_dir) / closed["run_id"] / "limits.jsonl").exists()


def test_a_new_run_is_seeded_with_the_last_reading_so_its_first_usage_counts(tmp_path):
    cfg = _cfg(tmp_path)
    d = cfg.state_dir / "meters"
    d.mkdir()
    (d / "P1.json").write_text(json.dumps({
        "phase": "P1", "ts": T0 - 60,
        "five_hour": {"pct": 20.0, "resets_at": T0 + H},
        "seven_day": {"pct": 30.0, "resets_at": T0 - 1}}))  # weekly window already over
    rec, _ = usage.start_run(cfg, "up", now=T0)
    samples = usage.load_samples(d / "limits.jsonl")
    assert samples == [usage.Sample(ts=T0, run_id=rec["run_id"], five_pct=20.0, five_resets_at=T0 + H)]


# -- piecewise pace -----------------------------------------------------------
def test_five_pace_sums_increases_across_window_resets():
    # 12 h, three 5-hour windows: 0→30, then (reset) 0→40, then (reset) 0→20.
    w1, w2, w3 = T0 + 5 * H, T0 + 10 * H, T0 + 15 * H
    s = [usage.Sample(T0, five_pct=0, five_resets_at=w1),
         usage.Sample(T0 + 2 * H, five_pct=18, five_resets_at=w1),
         usage.Sample(T0 + 4 * H, five_pct=30, five_resets_at=w1),
         usage.Sample(T0 + 5.5 * H, five_pct=5, five_resets_at=w2),   # new window: 5 since the reset
         usage.Sample(T0 + 9 * H, five_pct=40, five_resets_at=w2),
         usage.Sample(T0 + 10.2 * H, five_pct=2, five_resets_at=w3),
         usage.Sample(T0 + 12 * H, five_pct=20, five_resets_at=w3)]
    pace = usage.five_pace(s, T0, T0 + 12 * H)
    assert pace.windows == 3
    assert pace.used == 30 + 40 + 20
    assert pace.per_h == pytest.approx(90 / 12)


def test_a_reset_seen_only_as_a_drop_still_starts_a_window_and_wobbles_do_not_double_count():
    s = [usage.Sample(T0, five_pct=50), usage.Sample(T0 + H, five_pct=60),
         usage.Sample(T0 + 1.1 * H, five_pct=59.8), usage.Sample(T0 + 1.2 * H, five_pct=61),
         usage.Sample(T0 + 3 * H, five_pct=4)]  # no resets_at at all, but it fell: a reset
    pace = usage.five_pace(s, T0, T0 + 4 * H)
    assert pace.windows == 2 and pace.used == pytest.approx(11 + 4)


def test_a_stale_reading_under_the_same_reset_is_not_a_new_window():
    # A lagging status line read 37 % between two 38 %
    # readings, all under the same reset. Counted as a reset it added 37
    # points and showed an inflated weekly rate for a run that used little.
    wk = T0 + 90 * H
    s = [usage.Sample(T0, week_pct=34, week_resets_at=wk),
         usage.Sample(T0 + 2 * H, week_pct=38, week_resets_at=wk),
         usage.Sample(T0 + 3 * H, week_pct=37, week_resets_at=wk),
         usage.Sample(T0 + 3 * H + 7, week_pct=38, week_resets_at=wk)]
    pace = usage.week_pace(s, T0, T0 + 3.2 * H)
    assert pace.windows == 1 and pace.used == pytest.approx(4)


def test_a_stale_reading_from_the_previous_window_is_skipped():
    w1, w2 = T0 + H, T0 + 6 * H
    s = [usage.Sample(T0, five_pct=70, five_resets_at=w1),
         usage.Sample(T0 + 1.5 * H, five_pct=3, five_resets_at=w2),
         usage.Sample(T0 + 1.6 * H, five_pct=72, five_resets_at=w1),  # lagging session
         usage.Sample(T0 + 2 * H, five_pct=6, five_resets_at=w2)]
    pace = usage.five_pace(s, T0, T0 + 2 * H)
    assert pace.windows == 2 and pace.used == pytest.approx(3 + 3)


def test_week_pace_is_piecewise_too_and_only_counts_inside_the_run():
    wk1, wk2 = T0 + 3 * H, T0 + 171 * H
    s = [usage.Sample(T0 - 5 * H, week_pct=10, week_resets_at=wk1),  # before the run
         usage.Sample(T0, week_pct=90, week_resets_at=wk1),
         usage.Sample(T0 + 2 * H, week_pct=96, week_resets_at=wk1),
         usage.Sample(T0 + 4 * H, week_pct=1, week_resets_at=wk2),
         usage.Sample(T0 + 6 * H, week_pct=3, week_resets_at=wk2)]
    pace = usage.week_pace(s, T0, T0 + 6 * H)
    assert pace.used == 6 + 1 + 2 and pace.per_h == pytest.approx(9 / 6)


def test_a_pace_under_fifteen_minutes_is_not_a_pace():
    s = [usage.Sample(T0, five_pct=1), usage.Sample(T0 + 60, five_pct=5)]
    assert usage.five_pace(s, T0, T0 + 600).per_h is None
    assert usage.five_pace([], T0, T0 + 5 * H).per_h is None  # nothing reported


def test_legacy_rows_are_read_as_weekly():
    assert usage.parse_sample({"ts": 5.0, "pct": 41.0, "resets_at": 9.0}) == \
        usage.Sample(ts=5.0, week_pct=41.0, week_resets_at=9.0)
    assert usage.parse_sample({"nope": 1}) is None


def test_the_sample_tail_reads_only_whole_new_lines(tmp_path):
    path = tmp_path / "limits.jsonl"
    tail = usage.SampleTail(path)
    assert tail.poll() is False
    path.write_text(json.dumps(_row(1.0, five=1)) + "\n" + '{"ts": 2.0, "five_')
    assert tail.poll() and [s.ts for s in tail.samples] == [1.0]
    with path.open("a") as fh:
        fh.write('pct": 2}\n')
    assert tail.poll() and [s.ts for s in tail.samples] == [1.0, 2.0]
    path.write_text("")  # truncated: start over
    tail.poll()
    assert tail.samples == []


# -- summaries ----------------------------------------------------------------
def test_run_cost_prorates_a_session_straddling_the_epoch():
    sessions = [{"session_id": "a", "started_at": T0 - H, "ts": T0 + H, "cost_usd": 10.0},
                {"session_id": "b", "started_at": T0 + H, "ts": T0 + 2 * H, "cost_usd": 4.0},
                {"session_id": "c", "started_at": T0 - 9 * H, "ts": T0 - 8 * H, "cost_usd": 99.0}]
    assert usage.run_cost(sessions, T0, T0 + 2 * H) == pytest.approx(5.0 + 4.0)
    assert usage.run_cost([], T0, T0 + H) is None


def test_the_summary_math_with_a_mid_run_worker_change(tmp_path):
    cfg = _cfg(tmp_path)
    runs.start(cfg.state_dir, 1, "none", now=T0)
    runs.note_config(cfg.state_dir, 2, "none", now=T0 + 6 * H)
    _write_samples(cfg, [
        _row(T0, five=0, five_r=T0 + 5 * H, week=10, week_r=T0 + 100 * H),
        _row(T0 + 4 * H, five=20, five_r=T0 + 5 * H, week=12, week_r=T0 + 100 * H),
        _row(T0 + 6 * H, five=10, five_r=T0 + 11 * H, week=14, week_r=T0 + 100 * H),
        _row(T0 + 12 * H, five=70, five_r=T0 + 11 * H + 1, week=22, week_r=T0 + 100 * H),
    ])
    _log(cfg, [(T0 + H, "EVENT done P1 ok freed_slot=0 parked=False"),
               (T0 + 2 * H, "EVENT done P2 fail freed_slot=0 parked=False"),
               (T0 + 8 * H, "EVENT done P3 ok freed_slot=0 parked=False"),
               (T0 - H, "EVENT done P0 ok freed_slot=0 parked=False")])  # an earlier run's
    (cfg.state_dir / "meters" / "P1.json").write_text(json.dumps(
        {"phase": "P1", "session_id": "s", "started_at": T0, "ts": T0 + 12 * H, "cost_usd": 24.0}))

    s = usage.Sources(cfg).summarize(runs.current(cfg.state_dir), T0 + 12 * H)
    assert s["hours"] == 12.0
    assert (s["phases_finished"], s["phases_failed"]) == (2, 1)
    assert s["five_used"] == 20 + 10 + 60 and s["five_windows"] == 2
    assert s["five_pct_per_h"] == pytest.approx(90 / 12)
    assert s["week_pct_per_h"] == pytest.approx(12 / 12)
    assert s["usd_per_h"] == pytest.approx(2.0)
    assert s["phases_per_h"] == pytest.approx(2 / 12)
    seg1, seg2 = s["segments"]
    assert (seg1["max_workers"], seg1["hours"], seg1["phases_finished"]) == (1, 6.0, 1)
    assert seg1["five_pct_per_h"] == pytest.approx(30 / 6)  # 0→20, then the new window's 10
    assert (seg2["max_workers"], seg2["hours"]) == (2, 6.0)
    assert seg2["five_pct_per_h"] == pytest.approx(60 / 6)


def test_down_closes_the_run_with_its_summary_and_up_mirrors_it_into_state(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg.state_path = cfg.state_dir / "state.json"
    cfg.lock_path = cfg.state_dir / "state.json.lock"
    cfg.ensure_dirs = lambda: None
    rec = cli._start_run(cfg, "up")
    assert state_mod.read(cfg).run_id == rec["run_id"]
    closed = usage.close_run(cfg, "down", now=rec["epoch_ts"] + H)
    assert closed["summary"]["hours"] == pytest.approx(1.0)
    assert runs.current(cfg.state_dir) is None
    assert usage.past_summaries(cfg.state_dir, 5)[0]["run_id"] == rec["run_id"]


def test_old_state_without_run_keys_loads():
    st = state_mod.State.from_dict({"slots": []})
    assert (st.run_id, st.run_epoch) == (None, 0.0)


# -- `swarm usage` ------------------------------------------------------------
def test_usage_output_shows_the_twelve_hour_measurement(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    runs.start(cfg.state_dir, 1, "none", now=T0 - 30 * H)
    runs.close(cfg.state_dir, now=T0 - 20 * H, summarize=lambda r, e: usage.summarize(
        r, e, samples=[], events=[], sessions=[]))
    rec, _ = runs.start(cfg.state_dir, 1, "none", now=T0)
    _write_samples(cfg, [_row(T0, five=0, five_r=T0 + 5 * H, week=10, week_r=T0 + 100 * H),
                         _row(T0 + 12 * H, five=30, five_r=T0 + 15 * H, week=16, week_r=T0 + 100 * H)])
    src = usage.Sources(cfg)
    cur = usage.live_summary(cfg, src, now=T0 + 12 * H)
    out = usage.render(cur, usage.past_summaries(cfg.state_dir, 10), src.samples, T0 + 12 * H)
    assert f"current run {rec['run_id']}" in out
    assert "12.0 h elapsed · 1 worker(s) · isolation none" in out
    assert "this run 2.50 %/h" in out and "over 2 window(s)" in out  # 5-hour: 30 pts / 12 h
    assert "this run 0.50 %/h" in out  # weekly: 6 pts / 12 h
    assert "past runs" in out and "account-wide" in out


def test_usage_on_a_legacy_state_dir_reads_since_the_last_supervisor_start(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _log(cfg, [(T0 - 50 * H, "SUPERVISOR-START pid=1"), (T0, "SUPERVISOR-START pid=2")])
    _write_samples(cfg, [{"ts": T0 + H, "pct": 5.0, "resets_at": T0 + 90 * H},
                         {"ts": T0 + 3 * H, "pct": 9.0, "resets_at": T0 + 90 * H}])
    src = usage.Sources(cfg)
    cur = usage.live_summary(cfg, src, now=T0 + 4 * H)
    assert cur["legacy"] and cur["start"] == pytest.approx(T0, abs=0.01)
    assert cur["week_pct_per_h"] == pytest.approx(4 / 4, rel=1e-3)
    assert "legacy period" in usage.render(cur, [], src.samples, T0 + 4 * H)


def test_usage_cli_json(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    runs.start(cfg.state_dir, 1, "none")
    assert cli.cmd_usage(cfg, as_json=True, last=3) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["current"]["live"] and out["runs"] == [] and "account-wide" in out["note"]


def test_usage_help_carries_the_skew_note():
    help_text = cli._build_parser()._subparsers._group_actions[0].choices["usage"].format_help()
    assert "account-wide" in help_text


# -- the dashboard's run-aware reads -------------------------------------------
def _run(phase, start, end, status="ok"):
    return PhaseRun(phase=phase, status=status, started_at=start, ended_at=end)


def test_eta_uses_only_this_runs_phases_once_it_has_three():
    old = [_run(f"O{i}", T0 - 10 * H, T0 - 9 * H) for i in range(5)]  # 1 h each
    mine = [_run(f"N{i}", T0, T0 + 600) for i in range(3)]  # 10 min each
    got, borrowed = eta_sample(mine + old, T0)
    assert not borrowed and data.typical_durations(got) == [600.0] * 3


def test_eta_borrows_the_history_until_the_run_has_three_and_says_so():
    old = [_run(f"O{i}", T0 - 10 * H, T0 - 9 * H) for i in range(5)]
    got, borrowed = eta_sample([_run("N0", T0, T0 + 600)] + old, T0)
    assert borrowed and len(data.typical_durations(got)) == 6
    assert eta_sample(old, None) == (old, False)  # legacy: all history, no label
    got, borrowed = eta_sample([_run("N0", T0, T0 + 600)], T0)  # nothing to borrow
    assert not borrowed


def test_meters_of_phases_not_in_this_run_are_dropped_unless_busy():
    ms = {"old": Meter(phase="old", ts=T0 - H), "idle": Meter(phase="idle", ts=T0 - H),
          "new": Meter(phase="new", ts=T0 + 5)}
    assert set(live_meters(ms, T0, {"idle"})) == {"idle", "new"}
    assert set(live_meters(ms, None)) == set(ms)


def test_home_usage_lines_show_both_windows_at_this_runs_pace():
    lim = data.Limits(observed_at=T0, week_pct=40, week_resets_at=T0 + 100 * H,
                      five_pct=30, five_resets_at=T0 + 4 * H)
    (five, fstate), (week, _) = data.usage_outlook(
        lim, {"five_pct_per_h": 35.0, "week_pct_per_h": 1.0}, None, now=T0)
    assert "5-hour 30%" in five and "this run 35.0%/h" in five and "100% in ~2h" in five
    assert fstate == "warn"
    assert "week 40%" in week and "1.0%/h this run" in week
    (five, _), _ = data.usage_outlook(lim, {"five_pct_per_h": 1.0}, None, now=T0)
    assert "lasts until the reset" in five
    # No run (legacy): the weekly line falls back to its recent slope.
    assert data.usage_outlook(lim, None, None, now=T0)[1][0].endswith("pace unknown")


# -- accounts -------------------------------------------------------------------
def test_untagged_rows_load_as_account_unknown_and_never_join_a_known_one():
    assert usage.parse_sample(_row(T0, week=5, week_r=T0 + 90 * H)).account is None
    assert usage.parse_sample(_row(T0, week=5) | {"account": 7}).account is None
    assert usage.parse_sample(_row(T0, week=5) | {"account": "acct-a"}).account == "acct-a"
    old = usage.parse_sample(_row(T0, week=90, week_r=T0 + 20 * H))
    new = usage.parse_sample(_row(T0 + H, week=1, week_r=T0 + 3 * H) | {"account": "acct-b"})
    assert usage.newest_account([old, new]) == "acct-b"
    assert usage.current([old, new]) == [new]  # nobody logged in here: the newest tag
    assert usage.of_account([old, new], None) == [old, new]
    assert usage.current([old]) == [old]  # a log from before the tag reads as it always did


def _tagged(ts, week, week_r, account, five=None, five_r=None):
    return usage.parse_sample(_row(ts, five=five, five_r=five_r, week=week, week_r=week_r)
                              | {"account": account})


def test_a_switch_is_neither_a_reset_nor_usage():
    """Weekly 88 -> 90% on one account, then 0 -> 3% on another whose week
    resets *sooner*: the old guesswork read those as lagging readings."""
    samples = [_tagged(T0, 88, T0 + 20 * H, "acct-a"), _tagged(T0 + H, 90, T0 + 20 * H, "acct-a"),
               _tagged(T0 + 2 * H, 0, T0 + 5 * H, "acct-b"),
               _tagged(T0 + 3 * H, 3, T0 + 5 * H, "acct-b"),
               _tagged(T0 + 4 * H, 91, T0 + 20 * H, "acct-a")]  # and back again
    assert usage.accounts(samples) == [0, 0, 1, 1, 2]
    assert usage.switch_times(samples) == [T0 + 2 * H, T0 + 4 * H]
    assert usage.week_pace(samples, T0, T0 + 5 * H).used == 2 + 3
    assert usage.by_account(samples, T0, T0 + 5 * H) == [
        {"account": "acct-a", "five_used": 0.0, "week_used": 3.0},
        {"account": "acct-b", "five_used": 0.0, "week_used": 3.0}]


def test_usage_shows_the_account_in_use_and_each_accounts_share(tmp_path):
    cfg = _cfg(tmp_path)
    runs.start(cfg.state_dir, 1, "none", now=T0)
    rows = [(T0, 88, T0 + 20 * H, "acct-a", 40), (T0 + H, 90, T0 + 20 * H, "acct-a", 50),
            (T0 + 2 * H, 0, T0 + 50 * H, "acct-b", 0), (T0 + 3 * H, 3, T0 + 50 * H, "acct-b", 4)]
    _write_samples(cfg, [_row(t, five=f, five_r=T0 + 4 * H if a == "acct-b" else T0 + 1.5 * H,
                              week=w, week_r=r) | {"account": a} for t, w, r, a, f in rows])
    src = usage.Sources(cfg)
    now = T0 + 3.5 * H
    cur = usage.live_summary(cfg, src, now=now)
    assert cur["week_used"] == 5 and cur["five_used"] == 10 + 4
    out = usage.render(cur, [], src.samples, now)
    assert "account acct-b" in out
    assert "now 3% (resets in" in out and "now 90%" not in out
    assert "account acct-a: 5-hour 10 pts · weekly 2 pts used this run" in out
    assert "account acct-b (now): 5-hour 4 pts · weekly 3 pts used this run" in out
    brief = usage.brief(src.samples, now)
    assert brief.startswith("Weekly 3%") and "account acct-b" in brief and "90%" not in brief
