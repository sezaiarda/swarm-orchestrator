"""The web board's Resources tab: its history buckets, its views and its endpoints."""

from __future__ import annotations

import gzip
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import machine_toml

from swarm_orchestrator.config import load
from swarm_orchestrator.resources import capacity, store, view
from swarm_orchestrator.web import reshist, resview
from swarm_orchestrator.web import server as web_server

from test_web_board import make_run, serve

#: A minute boundary, so buckets and test offsets line up.
NOW = 1_800_000_000.0 - 1_800_000_000.0 % 7200 + 7200
HOST = {"ncpu": 8, "mem_total_mb": 16000.0, "swap_total_mb": 4096.0}
TOKEN = "sk-ant-api03-abcdefghijklmnopqrstu"


def sample(ts: float, **kw) -> dict:
    row = {"ts": ts, "k": "s", "cpu": 10.0, "load": 1.0, "avail_mb": 12000.0,
           "anon_mb": 3000.0, "cache_mb": 5000.0, "swap_mb": 0.0, "rd_mbs": 0.0,
           "wr_mbs": 2.0, "nb": 0,
           "psi": {"cpu": 0.5, "mem": 0.0, "memf": 0.0, "io": 1.0, "iof": 0.5},
           "w": {"worker:al-W2": [0.2, 400.0]}, "x": [0.0, 50.0]}
    row.update(kw)
    return row


def build(i: int, ended: float, **kw) -> dict:
    row = {"id": f"b{i}", "phase": f"al-W{i % 3}", "argv": "cargo build", "cwd": "/somewhere/else",
           "slot": 0, "cls": "heavy", "source": "events", "pid": 100 + i, "jobs": 4,
           "started": ended - 60.0, "ended": ended, "wait_s": 30.0, "run_s": 60.0, "exit": 0,
           "ended_by": "end", "partial": False, "cpu_s": 120.0, "avg_cores": 2.0,
           "peak_cores": 4.0, "peak_anon_mb": 1000.0 + 100 * i, "peak_rss_mb": 1500.0,
           "min_avail_mb": 9000.0, "psi_max": {"cpu": 1.0, "mem": 0.0, "memf": 0.0,
                                               "io": 5.0, "iof": 4.0 + i},
           "rd_mb": 1.0, "wr_mb": 2.0, "samples": 60, "idle_flagged": False}
    row.update(kw)
    return row


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_RESOURCES", "1")
    machine_toml(build={"max_concurrent": 0})  # no gate: nothing here reads /proc/locks
    project = tmp_path / "project"
    project.mkdir()
    return load(project_dir=str(project))


def full(cfg) -> Path:
    return store.meters_dir(cfg.state_dir) / store.FULL


def rewrite(path: Path, rows: list[dict]) -> None:
    """What the store's compaction does: a new file, renamed over the old one."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
    os.replace(tmp, path)


def write_samples(cfg, rows: list[dict]) -> None:
    for row in rows:
        store.append(full(cfg), row)


def snapshot(cfg, now: float, **kw) -> None:
    home = str(Path.home())
    data = {
        "ts": now - 3, "static": HOST, "source": "events", "queued": 2,
        "host": sample(now - 3, cpu=40.0, b={"b9": [3.0, 2000.0]}),
        "disk": {"headroom_gb": 80.0, "slack_gb": 60.0, "host_free_gb": 20.0, "wsl": True},
        "dirs": {"k": "dirs", "ts": now - 60, "state_gb": 6.0, "wt_gb": 2.0, "cache_gb": 30.0,
                 "cache": {"core": 20.0, "web": 10.0}, "cache_growth_gb_h": 0.5, "du_s": 4.0},
        "builds": [{"id": "b9", "pid": 4242, "slot": 0, "phase": "al-W2",
                    "argv": f"bash {home}/run.sh --token {TOKEN} {cfg.state_dir}/wt/al-W2/x.sh",
                    "cwd": f"{home}/secret/place", "age_s": 90.0, "cores": 3.0,
                    "anon_mb": 2000.0, "peak_anon_mb": 2500.0, "cpu_s": 200.0, "procs": 12,
                    "idle": False, "source": "events", "yielded": True}],
        "workers": [{"label": "worker:al-W2", "cores": 0.2, "anon_mb": 400.0, "rss_mb": 500.0,
                     "procs": 4}],
        "infra": {"cores": 0.0, "anon_mb": 50.0, "rss_mb": 60.0, "procs": 2},
        "console": None, "idle_holders": [], "idle_s": 600.0,
        "sampler": {"pct_core": 0.4, "samples": 10, "bytes_per_day": 10 * 2**20},
    }
    data.update(kw)
    store.write_now(cfg.state_dir, data)


# -- the tail ---------------------------------------------------------------------
def test_a_tail_reads_only_what_was_appended(tmp_path):
    path = tmp_path / "f.jsonl"
    tail = reshist.Tail(path)
    assert list(tail.lines()) == []
    path.write_bytes(b'{"a":1}\n{"a":2}\n{"a":3')
    assert list(tail.lines()) == [b'{"a":1}\n', b'{"a":2}\n'] and tail.reset
    with path.open("ab") as fh:
        fh.write(b'}\n{"a":4}\n')
    assert list(tail.lines()) == [b'{"a":3}\n', b'{"a":4}\n'] and not tail.reset
    assert list(tail.lines()) == []


def test_a_replaced_file_is_read_from_the_top(tmp_path):
    path = tmp_path / "f.jsonl"
    path.write_bytes(b'{"a":1}\n{"a":2}\n')
    tail = reshist.Tail(path)
    assert len(list(tail.lines())) == 2
    tmp = tmp_path / "f.tmp"
    tmp.write_bytes(b'{"a":2}\n{"a":3}\n')
    os.replace(tmp, path)
    assert list(tail.lines()) == [b'{"a":2}\n', b'{"a":3}\n'] and tail.reset


# -- windows and downsampling -----------------------------------------------------
def test_a_window_is_a_few_hundred_buckets_whatever_the_file_holds(cfg):
    write_samples(cfg, [sample(NOW - 1800 + i, cpu=float(i % 10)) for i in range(1800)])
    hist = reshist.History(cfg.state_dir)
    for name, (span, step) in reshist.WINDOWS.items():
        got = hist.window(name, NOW)
        assert got["step"] == step and got["n"] == round(span / step) <= 360
        assert got["t0"] + got["n"] * step > NOW >= got["t0"] + (got["n"] - 1) * step
        for series in got["series"].values():
            assert {len(series[k]) for k in ("min", "avg", "max")} == {got["n"]}
    hour = hist.window("1h", NOW)["series"]["cpu"]
    assert hour["min"][:179] == [None] * 179          # nothing sampled in the first half
    assert hour["min"][200] == 0 and hour["max"][200] == 9 and hour["avg"][200] == 4.5
    assert hour["all"] == [0, 4.5, 9]


def test_a_one_second_burst_survives_a_coarse_bucket(cfg):
    rows = [sample(NOW - 3600 + i) for i in range(3600)]
    rows[1234]["cpu"] = 99.0
    rows[1234]["avail_mb"] = 900.0
    write_samples(cfg, rows)
    month = reshist.History(cfg.state_dir).window("30d", NOW)
    assert month["step"] == 7200
    cpu, avail = month["series"]["cpu"], month["series"]["avail_mb"]
    hit = [i for i, v in enumerate(cpu["max"]) if v is not None]
    assert len(hit) == 1
    i = hit[0]
    assert cpu["max"][i] == 99.0 and cpu["min"][i] == 10.0 and cpu["avg"][i] < 10.1
    assert avail["min"][i] == 900 and avail["max"][i] == 12000


def test_a_poll_folds_only_the_new_samples(cfg):
    write_samples(cfg, [sample(NOW - 600 + i) for i in range(600)])
    hist = reshist.History(cfg.state_dir)
    hist.window("1h", NOW)
    assert (hist.folded, hist.loads) == (600, 1)
    hist.window("6h", NOW)
    hist.window("1h", NOW + 1)
    assert (hist.folded, hist.loads) == (600, 1)
    write_samples(cfg, [sample(NOW + i, cpu=50.0) for i in range(1, 4)])
    got = hist.window("1h", NOW + 5)
    assert (hist.folded, hist.loads) == (603, 1)
    assert got["series"]["cpu"]["all"][2] == 50.0


def test_a_window_nobody_opens_does_not_grow(cfg):
    write_samples(cfg, [sample(NOW - 7200 + i * 5) for i in range(1440)])
    hist = reshist.History(cfg.state_dir)
    hist.window("24h", NOW)
    assert len(hist.rings["1h"].buckets) <= 360
    write_samples(cfg, [sample(NOW + i * 5) for i in range(1440)])
    hist.window("24h", NOW + 7200)
    assert len(hist.rings["1h"].buckets) <= 360
    assert all(len(r.buckets) <= 361 for r in hist.rings.values())


def test_compaction_does_not_count_a_sample_twice(cfg):
    rows = [sample(NOW - 600 + i) for i in range(600)]
    write_samples(cfg, rows)
    hist = reshist.History(cfg.state_dir)
    before = hist.window("1h", NOW)["series"]["cpu"]
    # What compaction does: the newer rows, rewritten under a new inode.
    rewrite(full(cfg), rows[300:])
    write_samples(cfg, [sample(NOW + 1, cpu=30.0)])
    after = hist.window("1h", NOW)["series"]["cpu"]
    assert (hist.folded, hist.loads) == (601, 1)
    assert after["avg"][:-1] == before["avg"][:-1]


def test_samples_that_aged_out_unseen_are_found_again(cfg):
    write_samples(cfg, [sample(NOW - 600 + i) for i in range(10)])
    hist = reshist.History(cfg.state_dir)
    hist.window("1h", NOW)
    # Nobody looked for a long time: the file now starts after what was folded.
    rewrite(full(cfg), [sample(NOW - 300 + i, cpu=20.0) for i in range(10)])
    got = hist.window("1h", NOW)
    assert hist.loads == 2
    assert got["series"]["cpu"]["all"] == [20.0, 20.0, 20.0]


def test_minute_aggregates_fill_the_long_windows(cfg):
    old = [sample(NOW - 3 * 86400 + i, cpu=float(i % 60)) for i in range(120)]
    for row in store.aggregate(old):
        store.append(store.meters_dir(cfg.state_dir) / store.AGG, row)
    write_samples(cfg, [sample(NOW - 60 + i, cpu=5.0) for i in range(60)])
    hist = reshist.History(cfg.state_dir)
    week = hist.window("7d", NOW)["series"]["cpu"]
    known = [i for i, v in enumerate(week["max"]) if v is not None]
    assert len(known) == 2
    assert (week["min"][known[0]], week["max"][known[0]]) == (0.0, 59.0)
    assert week["avg"][known[0]] == 29.5
    assert hist.window("24h", NOW)["series"]["cpu"]["all"] == [5.0, 5.0, 5.0]


def test_missing_and_unknown_fields_leave_gaps_not_errors(cfg):
    write_samples(cfg, [
        {"ts": NOW - 30, "k": "host", "ncpu": 8},
        sample(NOW - 25, cpu=None, psi=None, rd_mbs=None, wr_mbs=None),  # a first sample
        {"k": "dirs", "state_gb": 1.0, "ts": NOW - 24},
        {"ts": NOW - 20, "k": "s", "avail_mb": 11000.0, "later_field": {"x": 1}},
    ])
    with full(cfg).open("ab") as fh:
        fh.write(b"not json\n")
    got = reshist.History(cfg.state_dir).window("1h", NOW)["series"]
    assert got["cpu"]["all"] is None and got["psi_iof"]["all"] is None
    # The second sample stands for the five seconds since the first.
    assert got["avail_mb"]["all"] == [11000, 11167, 12000]


def test_queue_depth_is_a_sweep_not_a_count_of_touching_intervals():
    one_after_another = [(10.0 * i, 10.0 * i + 9) for i in range(10)]
    assert reshist.level_max(one_after_another, 0.0, 100.0, 2, 150.0) == [1, 0]
    overlapping = [(5.0, 50.0), (10.0, 20.0), (15.0, 130.0)]
    assert reshist.level_max(overlapping, 0.0, 60.0, 3, 130.0) == [3, 1, 1]
    assert reshist.level_max(overlapping, 60.0, 60.0, 3, 100.0) == [1, None, None]


def test_back_to_back_builds_stay_apart_and_short_ones_share_a_band():
    longs = [{"a": 0.0, "b": 300.0, "phase": "al-W1", "cmd": "x", "exit": 0},
             {"a": 300.0, "b": 700.0, "phase": "al-W2", "cmd": "y", "exit": 1}]
    got = reshist.spans(longs, 0.0, 1000.0, 60.0)
    assert [(s["a"], s["b"], s["n"], s["bad"]) for s in got] == [(0.0, 300.0, 1, 0),
                                                               (300.0, 700.0, 1, 1)]
    assert got[1]["cmd"] == "y"
    shorts = [{"a": 10.0 * i, "b": 10.0 * i + 5, "phase": f"al-W{i % 2}", "cmd": "z", "exit": 0}
              for i in range(12)]
    got = reshist.spans(shorts, 0.0, 1000.0, 60.0)
    assert [s["n"] for s in got] == [6, 6]
    assert got[0]["phases"] == ["al-W0", "al-W1"] and "cmd" not in got[0]
    assert reshist.spans(longs, 800.0, 900.0, 60.0) == []


# -- now ---------------------------------------------------------------------------
def test_now_has_the_host_the_builds_and_the_sessions(cfg):
    snapshot(cfg, NOW)
    got = resview.now_payload(cfg, resview.gate(cfg), NOW)
    assert got["stale"] is False and got["age_s"] == 3.0
    s = got["sample"]
    assert s["static"] == HOST and s["host"]["cpu"] == 40.0 and s["host"]["psi"]["iof"] == 0.5
    assert "b" not in s["host"] and "w" not in s["host"]
    assert s["disk"]["headroom_gb"] == 80.0 and s["dirs"]["cache"] == {"core": 20.0, "web": 10.0}
    assert s["queued"] == 2 and s["workers"][0]["label"] == "worker:al-W2"
    b = s["builds"][0]
    assert b["since"] == NOW - 93 and b["cores"] == 3.0 and b["peak_anon_mb"] == 2500.0
    assert b["yielded"] is True                      # a field this code does not know
    assert got["gate"] == {"on": False}
    assert got["windows"] == list(reshist.WINDOWS)


def test_command_lines_lose_their_paths_and_credentials(cfg):
    snapshot(cfg, NOW)
    got = json.dumps(resview.now_payload(cfg, resview.gate(cfg), NOW))
    home = str(Path.home())
    assert home not in got and str(cfg.state_dir) not in got and TOKEN not in got
    b = resview.now_payload(cfg, {}, NOW)["sample"]["builds"][0]
    assert "argv" not in b and "cwd" not in b
    assert b["cmd"].startswith("bash ~/run.sh --token [redacted] <state>/wt/al-W2/x.sh")
    assert len(resview.tidy("x" * 500, cfg, resview.CMD_MAX)) == resview.CMD_MAX


def test_an_old_snapshot_is_stale_and_none_is_said(cfg):
    assert resview.now_payload(cfg, {}, NOW)["sample"] is None
    assert resview.now_payload(cfg, {}, NOW)["stale"] is True
    snapshot(cfg, NOW - 3600)
    got = resview.now_payload(cfg, {}, NOW)
    assert got["stale"] is True and got["sample"]["host"]["cpu"] == 40.0


def test_the_gate_view_turns_waits_into_times(cfg, monkeypatch):
    machine_toml(build={"max_concurrent": 1})
    home = str(Path.home())
    monkeypatch.setattr(resview.time, "time", lambda: NOW)
    monkeypatch.setattr(resview.buildstatus, "snapshot", lambda cfg, n_recent=10: {
        "max_concurrent": 1, "overtake": 2, "short_s": 60,
        "slots": [{"slot": 0, "busy": True, "id": "b9", "phase": "al-W2", "argv": "cargo test",
                   "cwd": f"{home}/x", "pid": 7, "running_s": 90.0, "pred_s": 200.0}],
        "queue": [{"id": "q1", "phase": "al-W3", "argv": f"bash {home}/gate.sh", "pid": 8,
                   "waiting_s": 120.0, "pred_s": None, "passed": 2, "stale": False,
                   "starts_in_s": 110.0, "new_field": 1}],
        "recent": []})
    got = resview.gate(cfg)
    own = {"swarm": None, "swarm_name": None, "mine": True, "frozen": False}
    assert got["slots"] == [{"slot": 0, "busy": True, "unknown": False, "gc": False, "id": "b9",
                             "phase": "al-W2", **own, "cmd": "cargo test", "since": NOW - 90,
                             "usual_s": 200.0}]
    assert got["queue"] == [{"id": "q1", "phase": "al-W3", **own, "cmd": "bash ~/gate.sh",
                             "queued_at": NOW - 120, "usual_s": None, "starts_at": NOW + 110,
                             "passed": 2, "stale": False}]


def test_the_gate_view_says_whose_each_build_is(cfg, monkeypatch):
    """One gate for the machine: its holders and waiters are every swarm's, and
    each says whose it is and whether its swarm stands frozen."""
    machine_toml(build={"max_concurrent": 2})
    monkeypatch.setattr(resview.buildstatus, "snapshot", lambda cfg, n_recent=10: {
        "max_concurrent": 2, "overtake": 2, "short_s": 60,
        "slots": [{"slot": 0, "busy": True, "id": "b1", "phase": "al-W2", "argv": "cargo test",
                   "swarm": "alpha-1", "swarm_name": "alpha", "mine": True, "running_s": 5.0},
                  {"slot": 1, "busy": True, "id": "b2", "phase": "W3", "argv": "make",
                   "swarm": "glasheim-9", "swarm_name": "glasheim", "mine": False,
                   "frozen": True, "running_s": 9.0}],
        "queue": [{"id": "q1", "phase": "W4", "argv": "make", "swarm": "glasheim-9",
                   "swarm_name": "glasheim", "mine": False, "waiting_s": 3.0}],
        "recent": []})
    got = resview.gate(cfg)
    assert [(h["swarm"], h["swarm_name"], h["mine"], h["frozen"]) for h in got["slots"]] == [
        ("alpha-1", "alpha", True, False), ("glasheim-9", "glasheim", False, True)]
    assert [(t["swarm"], t["swarm_name"], t["mine"]) for t in got["queue"]] == [
        ("glasheim-9", "glasheim", False)]


def test_a_gate_that_cannot_be_read_is_said_not_raised(cfg, monkeypatch):
    machine_toml(build={"max_concurrent": 1})

    def boom(cfg, n_recent=10):
        raise RuntimeError("no")

    monkeypatch.setattr(resview.buildstatus, "snapshot", boom)
    assert resview.gate(cfg) == {"on": True, "error": True, "max_concurrent": 1,
                                 "slots": [], "queue": []}


# -- the builds table -------------------------------------------------------------
def rows_of(cfg, builds: list[dict]) -> list[dict]:
    for row in builds:
        store.append(store.meters_dir(cfg.state_dir) / store.BUILDS, row)
    book = resview.BuildRows(cfg.state_dir)
    book.update(cfg)
    return book.rows


def test_the_table_sorts_filters_and_limits(cfg):
    rows = rows_of(cfg, [build(i, NOW - 100 * i) for i in range(9)]
                   + [build(9, NOW - 50, phase=None, exit=None, wait_s=None, exit_code_gone=1)])
    got = resview.table(rows, now=NOW)
    assert got["total"] == got["matched"] == 10 and got["rows"][0]["id"] == "b0"
    assert [r["id"] for r in resview.table(rows, now=NOW, sort="peak_anon_mb",
                                           limit=3)["rows"]] == ["b9", "b8", "b7"]
    assert [r["id"] for r in resview.table(rows, now=NOW, sort="peak_anon_mb", desc=False,
                                           limit=2)["rows"]] == ["b0", "b1"]
    # A row with no value for the sorted column goes last, either way round.
    for desc in (True, False):
        assert resview.table(rows, now=NOW, sort="wait_s", desc=desc)["rows"][-1]["id"] == "b9"
    one = resview.table(rows, now=NOW, phase="w1")
    assert one["matched"] == 3 and one["total"] == 10
    assert {r["phase"] for r in one["rows"]} == {"al-W1"}
    assert one["sum"] == {"run_s": 180.0, "wait_s": 90.0, "cpu_s": 360.0, "failed": 0}
    assert sorted(got["phases"]) == [["al-W0", 3], ["al-W1", 3], ["al-W2", 3]]
    assert resview.table(rows, now=NOW, sort="phase", desc=False)["rows"][0]["phase"] == "al-W0"


def test_the_table_follows_the_window(cfg):
    rows = rows_of(cfg, [build(0, NOW - 100), build(1, NOW - 2 * 3600), build(2, NOW - 3 * 86400)])
    assert resview.table(rows, now=NOW, window="1h")["total"] == 1
    assert resview.table(rows, now=NOW, window="24h")["total"] == 2
    assert resview.table(rows, now=NOW, window="7d")["total"] == 3
    assert resview.table(rows, now=NOW, window=resview.ALL)["total"] == 3


def test_a_build_row_keeps_what_it_knows_and_tolerates_the_rest(cfg):
    home = str(Path.home())
    rows = rows_of(cfg, [
        build(1, NOW, argv=f"bash {home}/b.sh {TOKEN}", cwd=f"{cfg.wt_dir}/al-W1/core/lib",
              yielded_s=42.5, some_new_thing={"a": 1}),
        {"id": "bare"},
    ])
    full_row, bare = rows
    assert full_row["cmd"] == "bash ~/b.sh [redacted]" and full_row["where"] == "core/lib"
    assert full_row["yielded_s"] == 42.5 and full_row["psi_iof"] == 5.0
    assert "argv" not in full_row and "cwd" not in full_row and "pid" not in full_row
    assert "some_new_thing" not in full_row
    assert bare["yielded_s"] is None and bare["phase"] is None and bare["ended"] is None
    assert bare["cmd"] == "" and bare["psi_memf"] is None
    assert resview.table(rows, now=NOW)["rows"][-1]["id"] == "bare"
    # A record from before builds said whose they are is the reader's own.
    assert (bare["swarm"], bare["swarm_name"], bare["mine"]) == (None, None, True)


def test_a_neighbours_build_is_named_with_its_swarm(cfg):
    """``builds.jsonl`` and the snapshot hold every swarm's builds. The table
    row says whose each is, and the history names a neighbour's with its swarm
    in front, as ``swarm build --status`` does."""
    theirs = {"swarm": "glasheim-9", "swarm_name": "glasheim", "mine": False}
    rows = rows_of(cfg, [build(1, NOW - 100), build(2, NOW - 50, **theirs)])
    assert [(r["swarm_name"], r["mine"]) for r in rows] == [(None, True), ("glasheim", False)]
    win = json.loads(resview.Resources(cfg.state_dir).window(cfg, "1h", NOW)[0])
    named = [p for s in win["spans"] for p in s["phases"]]
    assert len(named) == 2 and sum(p.startswith("[glasheim] ") for p in named) == 1


def test_the_builds_are_read_once_then_only_as_they_grow(cfg):
    path = store.meters_dir(cfg.state_dir) / store.BUILDS
    store.append(path, build(0, NOW - 100))
    book = resview.BuildRows(cfg.state_dir)
    book.update(cfg)
    first = book.rows[0]
    store.append(path, build(1, NOW - 50))
    book.update(cfg)
    assert [r["id"] for r in book.rows] == ["b0", "b1"] and book.rows[0] is first
    rewrite(path, [build(1, NOW - 50)])  # trimmed: the oldest half dropped
    book.update(cfg)
    assert [r["id"] for r in book.rows] == ["b1"]


# -- capacity ----------------------------------------------------------------------
def seed(cfg, now: float) -> None:
    old = [sample(now - 2 * 86400 + i * 15) for i in range(960)]
    for row in store.aggregate(old):
        store.append(store.meters_dir(cfg.state_dir) / store.AGG, row)
    write_samples(cfg, [{"ts": now - 3600, "k": "host", **HOST}]
                  + [sample(now - 3600 + i * 5, nb=1 if i % 100 < 10 else 0) for i in range(720)])
    for i in range(7):
        store.append(store.meters_dir(cfg.state_dir) / store.BUILDS, build(i, now - 400 * i - 10))
    snapshot(cfg, now)


def test_capacity_is_what_swarm_resources_works_out(cfg):
    seed(cfg, NOW)
    got = resview.capacity_payload(cfg, NOW)
    want = view.collect(cfg, now=NOW)["capacity"]
    for key in ("builds", "sessions", "host", "scenarios", "notes", "config", "mem_budget"):
        assert got[key] == want[key], key
    assert [s["name"] for s in got["scenarios"]][:4] == ["now", "2 builds", "8 workers",
                                                        "2 builds + 8 workers"]
    assert got["sessions"]["worker_hours"] > capacity.MIN_WORKER_H and got["days"] == 30.0


def test_the_streamed_rows_are_the_stores_history(cfg):
    seed(cfg, NOW)
    since = NOW - 30 * 86400
    assert list(resview.Rows(cfg.state_dir, since)) == store.history(cfg.state_dir, since)
    recent = NOW - 1800
    assert list(resview.Rows(cfg.state_dir, recent)) == store.history(cfg.state_dir, recent)


def test_thin_data_and_pressure_are_said_plainly(cfg):
    write_samples(cfg, [sample(NOW - 60 + i) for i in range(60)])
    store.append(store.meters_dir(cfg.state_dir) / store.BUILDS,
                 build(0, NOW - 10, psi_max={"memf": 0.0, "iof": 60.0}))
    snapshot(cfg, NOW)
    got = resview.capacity_payload(cfg, NOW)
    assert all(s["thin"] for s in got["scenarios"])
    assert any(n.startswith("too thin: 1 measured build") for n in got["notes"])
    assert any("IO pressure during builds is high" in n for n in got["notes"])


def test_no_data_at_all_is_an_empty_page_not_an_error(cfg):
    res = resview.Resources(cfg.state_dir)
    now = json.loads(res.now(cfg, NOW)[0])
    assert now["sample"] is None and now["stale"] is True
    win = json.loads(res.window(cfg, "24h", NOW)[0])
    assert win["n"] == 360 and win["spans"] == [] and set(win["queued"]) == {0}
    assert all(s["all"] is None for s in win["series"].values())
    table = json.loads(res.table(cfg, now=NOW)[0])
    assert table["rows"] == [] and table["total"] == 0
    cap = json.loads(res.capacity(cfg, NOW)[0])
    assert all(s["thin"] for s in cap["scenarios"]) and len(cap["notes"]) >= 2


# -- the cached views --------------------------------------------------------------
def test_a_window_carries_its_builds_and_queue(cfg):
    seed(cfg, NOW)
    res = resview.Resources(cfg.state_dir)
    win = json.loads(res.window(cfg, "1h", NOW)[0])
    assert win["window"] == "1h" and win["static"] == HOST and win["now"] == NOW
    # Seven finished builds fall in the hour, and the one the snapshot says is running.
    assert sum(s["n"] for s in win["spans"]) == 8
    assert [s for s in win["spans"] if s["live"]][0]["phases"] == ["al-W2"]
    assert max(win["queued"]) == 1 and win["series"]["nb"]["all"] == [0, 0, 1]


def test_views_are_rebuilt_only_when_something_moved(cfg):
    seed(cfg, NOW)
    res = resview.Resources(cfg.state_dir)
    first = res.now(cfg, NOW)
    assert res.now(cfg, NOW + 1)[0] is first[0]
    snapshot(cfg, NOW + 2, queued=5)
    assert json.loads(res.now(cfg, NOW + 2)[0])["sample"]["queued"] == 5
    win = res.window(cfg, "24h", NOW)
    assert res.window(cfg, "24h", NOW + 1)[0] is win[0]       # inside the same two minutes
    assert res.window(cfg, "24h", NOW + 121)[0] is not win[0]
    cap = res.capacity(cfg, NOW)
    assert res.capacity(cfg, NOW + resview.CAPACITY_EVERY_S - 1)[0] is cap[0]
    folded = res.history.folded
    for _ in range(3):
        res.window(cfg, "1h", NOW + 5)
    assert res.history.folded == folded and res.history.loads == 1


# -- over HTTP ---------------------------------------------------------------------
@pytest.fixture
def srv(tmp_path, monkeypatch):
    machine_toml(build={"max_concurrent": 0})
    cfg = make_run(tmp_path, monkeypatch)
    import time
    seed(cfg, time.time())
    s = serve(cfg)
    try:
        yield s
    finally:
        web_server.close(s)


def _get(srv, path: str, headers: dict | None = None):
    # Every path is this swarm's: its page and its data are under /s/<slug>.
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{srv.at}{path}",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_every_resources_endpoint_answers_with_an_etag(srv):
    for path, key in (("/api/resources", "sample"),
                      ("/api/resources/history", "series"),
                      ("/api/resources/history?window=7d", "series"),
                      ("/api/resources/builds", "rows"),
                      ("/api/resources/builds?window=24h&sort=peak_anon_mb&dir=asc"
                       "&phase=al-W1&limit=2", "rows"),
                      ("/api/resources/capacity", "scenarios")):
        code, headers, body = _get(srv, path)
        assert code == 200 and headers["Content-Type"].startswith("application/json"), path
        assert key in json.loads(body), path
        assert _get(srv, path, {"If-None-Match": headers["ETag"]})[0] == 304, path
    code, headers, body = _get(srv, "/api/resources/history?window=1h",
                               {"Accept-Encoding": "gzip"})
    assert headers.get("Content-Encoding") == "gzip"
    assert json.loads(gzip.decompress(body))["window"] == "1h"
    default = json.loads(_get(srv, "/api/resources/history")[2])
    assert default["window"] == reshist.DEFAULT_WINDOW
    table = json.loads(_get(srv, "/api/resources/builds?sort=peak_anon_mb&limit=2")[2])
    assert [r["id"] for r in table["rows"]] == ["b6", "b5"] and table["total"] == 7


def test_resources_endpoints_refuse_what_is_off_their_menus(srv):
    for path in ("/api/resources/history?window=2h", "/api/resources/builds?sort=pid",
                 "/api/resources/builds?dir=sideways", "/api/resources/builds?window=1y",
                 "/api/resources/builds?limit=many", "/api/resources/builds?phase=a%20b",
                 "/api/resources/builds?phase=%3Cscript%3E"):
        assert _get(srv, path)[0] == 400, path
    for path in ("/api/resources/nope", "/api/resources/../state.json", "/api/resourcesx"):
        assert _get(srv, path)[0] == 404, path
    big = json.loads(_get(srv, "/api/resources/builds?limit=99999")[2])
    assert big["limit"] == resview.MAX_LIMIT


def test_nothing_a_resources_endpoint_serves_names_a_path_or_a_credential(srv):
    home = str(Path.home())
    state = str(srv.feed.cfg.state_dir)
    for path in ("/api/resources", "/api/resources/history?window=1h", "/api/resources/builds",
                 "/api/resources/capacity"):
        body = _get(srv, path)[2].decode()
        assert home not in body and state not in body and TOKEN not in body, path
    assert "[redacted]" in _get(srv, "/api/resources")[2].decode()


def test_the_page_has_a_resources_tab(srv):
    html = _get(srv, "/")[2].decode()
    assert '["resources", "Resources"' in html and '"api/resources/history' in html
    assert 'data-tab="resources"' in html      # the overview's host line links to it
