"""resources: downsampling, bounded storage, capacity math and the views."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from swarm_orchestrator.config import load
from swarm_orchestrator.resources import capacity, store, view

DAY = 86400.0
NOW = 1_800_000_000.0


def sample(ts: float, **kw) -> dict:
    row = {"ts": ts, "k": "s", "cpu": 10.0, "avail_mb": 20000.0, "anon_mb": 3000.0,
           "psi": {"mem": 0.0, "io": 1.0}}
    row.update(kw)
    return row


# -- downsampling -----------------------------------------------------------------
def test_a_minute_folds_into_min_avg_max():
    rows = [sample(60.0, cpu=10.0, b={"x": [2.0, 900.0]}, w={"worker:A": [0.1, 300.0]}),
            sample(61.0, cpu=30.0, b={"x": [4.0, 1500.0]}, w={"worker:A": [0.3, 350.0]},
                   psi={"mem": 5.0, "io": 3.0}),
            sample(125.0, cpu=50.0),
            {"ts": 70.0, "k": "dirs", "state_gb": 1.0}]
    out = store.aggregate(rows)
    assert [r["k"] for r in out] == ["m", "dirs", "m"]
    first = out[0]
    assert first["ts"] == 60.0 and first["n"] == 2
    assert first["cpu"] == [10.0, 20.0, 30.0]
    assert first["psi"]["mem"] == [0.0, 2.5, 5.0]
    assert first["b"]["x"] == [3.0, 1500.0]  # average cores, peak anon
    assert first["w"]["worker:A"] == [0.2, 350.0]
    assert store.val(first, "cpu", "max") == 30.0 and store.val(sample(0), "cpu") == 10.0
    assert store.psi_val(first, "mem", "avg") == 2.5


def test_a_minute_keeps_which_builds_were_another_swarms():
    rows = [sample(60.0, nb=2, b={"x": [2.0, 900.0], "far": [1.0, 400.0]}, bo=["far"]),
            sample(61.0, nb=2, b={"x": [2.0, 900.0], "far2": [1.0, 500.0]}, bo=["far2"]),
            sample(62.0, nb=1, b={"x": [2.0, 900.0]}),
            sample(125.0, nb=1, b={"x": [2.0, 900.0]})]
    first, second = store.aggregate(rows)
    assert first["bo"] == ["far", "far2"] and set(first["b"]) == {"x", "far", "far2"}
    assert first["nb"] == [1, 1.67, 2]
    assert "bo" not in second  # a minute of this swarm's builds only


def test_compaction_ages_a_day_into_minutes_and_keeps_the_rest(tmp_path):
    full = store.meters_dir(tmp_path) / store.FULL
    old = [sample(NOW - 2 * DAY + i) for i in range(120)]  # two minutes, two days ago
    new = [sample(NOW - 100 + i) for i in range(10)]
    for row in old + new:
        store.append(full, row)
    done = store.compact(tmp_path, NOW)
    assert done["aged"] == 120 and done["minute_rows"] == 2 and done["kept"] == 10
    assert len(store.read_rows(full)) == 10
    agg = store.read_rows(store.meters_dir(tmp_path) / store.AGG)
    assert [r["n"] for r in agg] == [60, 60]
    # history stitches minutes and full rows without overlap, oldest first
    hist = store.history(tmp_path, NOW - 3 * DAY)
    assert [r["k"] for r in hist] == ["m", "m"] + ["s"] * 10


def test_compaction_leaves_a_fresh_file_alone(tmp_path):
    full = store.meters_dir(tmp_path) / store.FULL
    store.append(full, sample(NOW - 10))
    before = full.stat().st_mtime_ns
    assert store.compact(tmp_path, NOW)["aged"] == 0
    assert full.stat().st_mtime_ns == before


def test_minute_rows_older_than_a_month_are_dropped(tmp_path):
    agg = store.meters_dir(tmp_path) / store.AGG
    for ts in (NOW - 40 * DAY, NOW - 31 * DAY, NOW - 2 * DAY):
        store.append(agg, {"ts": ts, "k": "m", "n": 1})
    assert store.compact(tmp_path, NOW)["dropped"] == 2
    assert [r["ts"] for r in store.read_rows(agg)] == [NOW - 2 * DAY]


def test_every_file_has_a_byte_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MAX_FULL_BYTES", 2000)
    monkeypatch.setattr(store, "MAX_AGG_BYTES", 2000)
    monkeypatch.setattr(store, "MAX_BUILDS_BYTES", 2000)
    root = store.meters_dir(tmp_path)
    for i in range(100):  # all within the day, but far too many bytes
        store.append(root / store.FULL, sample(NOW - 1000 + i))
        store.append(root / store.BUILDS, {"id": f"b{i}", "ended": NOW - 1000 + i, "pad": "x" * 30})
    store.compact(tmp_path, NOW)
    kept = store.read_rows(root / store.FULL)
    assert 0 < len(kept) <= 50 and kept[-1]["ts"] == NOW - 901  # the newest survive
    builds = store.read_rows(root / store.BUILDS)
    assert len(builds) == 50 and builds[-1]["id"] == "b99"
    for _ in range(3):
        store.compact(tmp_path, NOW)
    assert all(v <= 2000 * 2 for v in store.sizes(tmp_path).values())


# -- capacity ----------------------------------------------------------------------
def test_percentiles():
    assert capacity.pct([], 95) is None
    assert capacity.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert capacity.pct([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    # time-weighted: a value held for a minute outweighs ten one-second blips
    assert capacity.wpct([(100.0, 60.0)] + [(1.0, 1.0)] * 10, 50) == 100.0


def build_row(anon: float, cores: float, jobs: int = 6, **kw) -> dict:
    row = {"id": f"b{anon}", "cls": "heavy", "samples": 30, "peak_anon_mb": anon,
           "avg_cores": cores, "peak_cores": cores * 1.5, "run_s": 120.0, "jobs": jobs,
           "min_avail_mb": 15000.0, "psi_max": {"memf": 0.0, "iof": 2.0}}
    row.update(kw)
    return row


def worker_rows(hours: float, anon: float = 400.0, cores: float = 0.2,
                host_anon: float = 5000.0) -> list[dict]:
    return [{"ts": NOW - hours * 3600 + i * 60, "k": "m", "n": 60,
             "anon_mb": [host_anon] * 3,
             "w": {"worker:A": [cores, anon], "worker:B": [cores, anon]},
             "x": [0.0, 100.0]}
            for i in range(int(hours * 60))]


HOST = {"ncpu": 24, "mem_total_mb": 28000.0, "swap_total_mb": 8192.0}


def test_the_capacity_math_is_shown_and_right():
    rows = [build_row(a, 5.0) for a in (3000, 3200, 3400, 3600, 3800, 4000)]
    out = capacity.analyse(rows, worker_rows(2), HOST, 1, 6, 4)
    by = {s["name"]: s for s in out["scenarios"]}
    # other = host anon 5000 - (2 workers x 400 + infra 100) = 4100
    assert out["sessions"]["other_anon_mb"]["p95"] == 4100.0
    now = by["now"]
    assert now["need_mb"] == round(4100 + 4 * 400 + 1 * 4000)
    assert now["fits_mem"] and now["fits_cpu"] and not now["thin"]
    two = by["2 builds"]
    assert two["need_mb"] == round(4100 + 4 * 400 + 2 * 4000)
    assert "2 x 4000 build" in two["math"] and "85% of 28000" in two["math"]
    eight = by["8 workers"]
    assert eight["need_mb"] == round(4100 + 8 * 400 + 4000)
    jobs = by["jobs 12"]
    assert jobs["need_mb"] == round(4100 + 4 * 400 + 8000)  # memory scales with jobs
    assert jobs["need_cores"] == pytest.approx(4 * 0.2 + 10.0)


def test_a_scenario_that_does_not_fit_says_so():
    rows = [build_row(12000, 9.0) for _ in range(6)]
    out = capacity.analyse(rows, worker_rows(2), HOST, 1, 6, 4)
    by = {s["name"]: s for s in out["scenarios"]}
    assert by["now"]["fits_mem"] and not by["2 builds"]["fits_mem"]


def test_thin_data_is_said_plainly():
    out = capacity.analyse([build_row(3000, 4.0)], worker_rows(0.2), HOST, 1, 6, 4)
    assert all(s["thin"] for s in out["scenarios"])
    assert any("too thin: 1 measured build" in n for n in out["notes"])
    assert any("h of worker samples" in n for n in out["notes"])


def test_the_pairing_mode_is_noted_beside_the_two_builds_scenario():
    rows = [build_row(3000, 4.0) for _ in range(6)]
    plain = capacity.analyse(rows, worker_rows(2), HOST, 2, 6, 4)
    assert plain["config"]["pair"] == "any" and not any("pair" in n for n in plain["notes"])
    out = capacity.analyse(rows, worker_rows(2), HOST, 2, 6, 4, "distinct-repo")
    assert out["config"]["pair"] == "distinct-repo"
    assert any("[build].pair = distinct-repo" in n and "runs alone" in n for n in out["notes"])


def test_pressure_during_builds_is_called_out():
    rows = [build_row(3000, 4.0, psi_max={"memf": 12.0, "iof": 40.0}) for _ in range(6)]
    notes = capacity.analyse(rows, worker_rows(2), HOST, 1, 6, 4)["notes"]
    assert any("memory pressure" in n for n in notes)
    assert any("IO pressure" in n for n in notes)


def test_everything_else_holds_no_swarms_build():
    """The gate is the machine's, so a row's builds are every swarm's. All of
    them come out of "everything else": a neighbour's build is a build, not
    host load this swarm's builds get blamed for."""
    rows = worker_rows(2)
    for r in rows[:60]:  # an hour with a build of ours and one of a neighbour's running
        r["b"] = {"own": [4.0, 1000.0], "far": [4.0, 1500.0]}
        r["bo"] = ["far"]
    # host anon 5000 - (2 workers x 400 + infra 100) = 4100, less the builds while they ran
    assert capacity.session_stats(rows[:60])["other_anon_mb"]["p95"] == 1600.0
    assert capacity.session_stats(rows[60:])["other_anon_mb"]["p95"] == 4100.0


def test_the_build_figure_is_the_machines_and_the_text_says_what_is_whose():
    rows = [build_row(a, 5.0, mine=True, swarm="here", swarm_name="here")
            for a in (3000, 3200, 3400, 3600)]
    rows += [build_row(a, 5.0, mine=False, swarm="glas-1", swarm_name="glasheim")
             for a in (3800, 9000)]
    out = capacity.analyse(rows, worker_rows(2), HOST, 1, 6, 4)
    assert (out["builds"]["n"], out["builds"]["others"]) == (6, 2)
    assert out["builds"]["peak_anon_mb"]["p95"] == 9000  # a neighbour's build is a build
    assert any("2 of the 6 measured build(s) were other swarms'" in n for n in out["notes"])
    text = "\n".join(view._capacity_lines(out))
    for needle in ("the machine's gate (machine.toml): max_concurrent=1",
                   "this project: jobs=6 max_workers=4",
                   "a heavy build, any swarm's (6 measured on the machine's gate,"
                   " 2 of them other swarms')",
                   "a worker of this swarm (",
                   "everything else on the host (the other swarms' sessions included,"
                   " no swarm's builds)"):
        assert needle in text, needle
    alone = capacity.analyse(rows[:4] + rows[:2], worker_rows(2), HOST, 1, 6, 4)
    assert alone["builds"]["others"] == 0
    assert not any("other swarms'" in n for n in alone["notes"])


def test_unmeasured_and_light_builds_do_not_count():
    rows = [build_row(3000, 4.0), build_row(9000, 4.0, samples=0), build_row(9000, 4.0, cls="light")]
    assert capacity.build_stats(rows)["n"] == 1


# -- views ----------------------------------------------------------------------
def test_sparklines_scale_to_the_range():
    assert view.spark([0, 50, 100, None]) == "▁▅█ "
    assert view.spark([5, 5]) == "▁▁"
    assert view.spark([None, None]) == "  "


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_RESOURCES", "1")
    project = tmp_path / "project"
    project.mkdir()
    (project / ".swarm.toml").touch()
    return load(project_dir=str(project))


def seed(cfg, now: float) -> None:
    root = store.meters_dir(cfg.state_dir)
    for i in range(0, 3600, 15):
        store.append(root / store.FULL, sample(now - 3600 + i, nb=1 if i % 600 < 60 else 0))
    for row in [build_row(3000 + i * 100, 4.0, ended=now - 100 * i, phase=f"P{i}",
                          argv="cargo build") for i in range(6)]:
        store.append(root / store.BUILDS, row)
    store.write_now(cfg.state_dir, {
        "ts": now - 5, "static": HOST, "source": "events", "queued": 1,
        "host": sample(now - 5, psi={"cpu": 1.0, "mem": 0.0, "memf": 0.0, "io": 2.0, "iof": 1.0},
                       load=2.0, cache_mb=18000.0, swap_mb=100.0, rd_mbs=1.0, wr_mbs=20.0),
        "disk": {"headroom_gb": 76.0, "slack_gb": 57.0, "host_free_gb": 19.0, "wsl": True},
        "dirs": {"ts": now - 60, "state_gb": 6.0, "wt_gb": 2.0, "cache_gb": 80.0,
                 "cache_growth_gb_h": 0.5, "du_s": 4.0},
        "builds": [{"id": "b", "pid": 42, "slot": 0, "phase": "P9", "argv": "cargo test",
                    "age_s": 90.0, "cores": 5.5, "anon_mb": 2500.0, "peak_anon_mb": 3000.0,
                    "procs": 12, "idle": False}],
        "workers": [{"label": "worker:P9", "cores": 0.2, "anon_mb": 400.0, "rss_mb": 500.0,
                     "procs": 4}],
        "infra": {"cores": 0.0, "anon_mb": 50.0},
        "idle_holders": [], "sampler": {"pct_core": 0.4, "cpu_s": 1.0, "samples": 10,
                                        "bytes_per_day": 10 * 2**20},
    })


def test_the_report_has_now_the_day_the_builds_and_capacity(cfg):
    import time
    now = time.time()
    seed(cfg, now)
    text = view.render(view.collect(cfg, now=now))
    for needle in ("NOW", "headroom 76G (vhdx slack 57G + Windows drive free 19G)",
                   "slot 0 pid 42 P9", "worker:P9", "LAST 24 H", "cpu %", "MemAvailable",
                   "BUILDS (6 finished", "CAPACITY", "2 builds", "8 workers",
                   "build caches 80.0G (+0.50G/h)", "sampler cost 0.40% of one core"):
        assert needle in text, needle


def test_the_cli_prints_json(cfg, tmp_path):
    import os
    import time
    seed(cfg, time.time())
    env = dict(os.environ, SWARM_STATE_DIR=str(cfg.state_dir))
    out = subprocess.run([sys.executable, "-m", "swarm_orchestrator", "--project-dir",
                          str(cfg.project_dir), "resources", "--json"],
                         capture_output=True, text=True, env=env, check=True, timeout=60)
    data = json.loads(out.stdout)
    assert data["now"]["builds"][0]["pid"] == 42
    assert data["builds"]["count"] == 6
    assert {s["name"] for s in data["capacity"]["scenarios"]} >= {"now", "2 builds", "8 workers"}
    text = subprocess.run([sys.executable, "-m", "swarm_orchestrator", "--project-dir",
                           str(cfg.project_dir), "resources"],
                          capture_output=True, text=True, env=env, check=True, timeout=60)
    assert "CAPACITY" in text.stdout


def test_no_samples_is_said_not_faked(cfg):
    text = view.render(view.collect(cfg))
    assert "no samples yet" in text and "none recorded yet" in text
    assert view.status_lines(cfg) == ["resources: no samples yet"]


def test_the_doctor_flags_a_file_past_its_bound(cfg, monkeypatch):
    monkeypatch.setattr(store, "MAX_BUILDS_BYTES", 100)
    root = store.meters_dir(cfg.state_dir)
    for i in range(10):
        store.append(root / store.BUILDS, {"id": f"b{i}", "pad": "x" * 50})
    rows = {r[0]: r for r in view.doctor_checks(cfg, supervisor_alive=False)}
    assert rows["resources.files"][1] == "warn"
    assert rows["resources.sampler"][1] == "ok"  # the supervisor is down: nothing to sample


def test_the_dashboard_box_shows_now_and_marks_an_idle_holder():
    pytest.importorskip("textual")
    from swarm_orchestrator.tui import resourcebox

    snap = {"ts": NOW, "static": HOST, "queued": 2,
            "host": sample(NOW, cache_mb=18000.0, swap_mb=100.0, wr_mbs=20.0, load=2.0,
                           psi={"mem": 1.0, "memf": 0.0, "io": 3.0, "iof": 2.0}),
            "disk": {"headroom_gb": 76.0},
            "builds": [{"slot": 0, "phase": "P[b]", "age_s": 700.0, "cores": 0.0,
                        "anon_mb": 300.0, "idle": True}]}
    text = "\n".join(resourcebox.box_lines(snap, NOW + 1))
    assert "cpu 10% of 24" in text and "cache 17.6G" in text and "free 76G" in text
    assert "IDLE holder" in text and "P\\[b]" in text  # markup escaped
    assert "2 build(s) queued behind" in text
    assert "not running" in resourcebox.box_lines(snap, NOW + 3600)[0]
    assert "no samples yet" in resourcebox.box_lines(None, NOW)[0]


def test_the_dashboard_box_names_a_neighbours_build_and_does_not_blame_this_swarm():
    pytest.importorskip("textual")
    from swarm_orchestrator.tui import resourcebox
    from swarm_orchestrator.tui.theme import BAD, MUTED, paint

    def build(**kw) -> dict:
        return {"slot": 0, "phase": "P1", "age_s": 700.0, "cores": 0.0, "anon_mb": 300.0,
                "idle": True, "mine": True, "swarm": "here-1", "swarm_name": "here", **kw}

    far = {"mine": False, "swarm": "glas-1", "swarm_name": "glasheim", "phase": "W7"}
    snap = {"ts": NOW, "static": HOST, "queued": 3, "queued_mine": 1, "host": sample(NOW),
            "builds": [build(), build(slot=1, **far),
                       build(slot=2, idle=False, **dict(far, phase="W8"))]}
    lines = resourcebox.box_lines(snap, NOW + 1)
    [own] = [x for x in lines if "slot 0" in x]
    [idle] = [x for x in lines if "slot 1" in x]
    [busy] = [x for x in lines if "slot 2" in x]
    assert own == paint(own_text := "slot 0 P1 12m · 0.0 cores · 0.3G anon · IDLE holder", BAD)
    assert "[" not in own_text  # this swarm's own build carries no name
    assert "\\[glasheim] W7" in idle and "IDLE holder" not in idle
    assert idle == paint("slot 1 \\[glasheim] W7 12m · 0.0 cores · 0.3G anon"
                         " · idle (its swarm's to look at)", MUTED)
    assert "\\[glasheim] W8" in busy
    assert "3 build(s) queued behind (1 this swarm's)" in lines[-1]
    snap["queued_mine"] = 0  # nothing of this swarm's waits: not its warning
    assert resourcebox.box_lines(snap, NOW + 1)[-1] == paint(
        "3 build(s) queued behind (0 this swarm's)", MUTED)
