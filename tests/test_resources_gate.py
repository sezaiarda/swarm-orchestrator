"""The resource sampler against the real build gate: real ``swarm build`` calls,
the event log they write, and the sampler thread reading it from the real /proc.

Five heavy builds (a Python script that holds memory and spins a core) share a
two-slot gate, one light command burns CPU beside them, and one heavy build's
``swarm build`` is SIGKILLed mid-run. ``meters/builds.jsonl`` must hold exactly
the heavy builds, with figures that match what they did, the killed one closed
out, and never more than two at once.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarm_orchestrator import buildclass
from swarm_orchestrator.config import load
from swarm_orchestrator.resources import sampler as sampler_mod
from swarm_orchestrator.resources import store

MAX = 2
MB = 120  # each heavy build holds this much, touched
RUN_S = 3.0

BURN = """\
import os, signal, sys, time
tag, secs, mb, log = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
def mark(what):
    fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    os.write(fd, f"{what} {tag} {time.time():.3f}\\n".encode())
    os.close(fd)
def stop(signum, frame):
    mark("end")
    os._exit(128 + signum)
signal.signal(signal.SIGTERM, stop)
mark("start")
held = bytearray(mb << 20)
for i in range(0, len(held), 4096):
    held[i] = 1
t0 = time.time()
n = 0
while time.time() - t0 < secs:
    n += 1
mark("end")
"""

LIGHT = "import time\nt = time.time()\nwhile time.time() - t < 2.0:\n    pass\n"


def _events(state: Path) -> list[dict]:
    path = state / "buildsem" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _wait(pred, timeout: float, what: str):
    end = time.time() + timeout
    while time.time() < end:
        got = pred()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _peak(intervals: list[tuple[float, float]]) -> int:
    edges = sorted([(a, 1) for a, _ in intervals] + [(b, -1) for _, b in intervals],
                   key=lambda e: (e[0], e[1]))  # an end before a start at the same instant
    run = peak = 0
    for _, d in edges:
        run += d
        peak = max(peak, run)
    return peak


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs Linux /proc")
def test_the_sampler_measures_real_gated_builds(tmp_path, monkeypatch):
    state, proj = tmp_path / "state", tmp_path / "proj"
    proj.mkdir()
    burn = proj / "burn.py"
    burn.write_text(BURN)
    log = tmp_path / "burn.log"
    heavy_pat = "python* */burn.py"
    env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
    env.update(SWARM_STATE_DIR=str(state), SWARM_BUILD_MAX=str(MAX),
               SWARM_BUILD_HEAVY=heavy_pat, SWARM_BUILD_OVERTAKE="0")
    for k in [k for k in os.environ if k.startswith("SWARM_")]:
        monkeypatch.delenv(k)
    for k, v in env.items():
        if k.startswith("SWARM_"):
            monkeypatch.setenv(k, v)
    cfg = load(project_dir=str(proj))
    cfg.ensure_dirs()
    heavy_argv = [sys.executable, str(burn)]
    assert buildclass.classify(heavy_argv, proj, [heavy_pat]).cls == buildclass.HEAVY
    light_argv = [sys.executable, "-c", LIGHT]
    assert buildclass.classify(light_argv, proj, [heavy_pat]).cls == buildclass.LIGHT

    s = sampler_mod.Sampler(lambda: cfg, wsl=False)
    s._last_dirs = float("inf")  # no du thread: nothing here is about disk sizes
    procs: list[subprocess.Popen] = []

    def build(tag: str, argv: list[str]) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-m", "swarm_orchestrator", "build", *argv],
                             cwd=proj, env=dict(env, SWARM_PHASE=f"P-{tag}"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        return p

    s.start()
    try:
        # K takes a slot first and is killed mid-run; A-D queue behind; L is light.
        killed = build("K", [*heavy_argv, "K", "30", str(MB), str(log)])
        _wait(lambda: [e for e in _events(state) if e["event"] == "start"
                       and e["phase"] == "P-K"], 20, "K to start")
        for tag in "ABCD":
            build(tag, [*heavy_argv, tag, str(RUN_S), str(MB), str(log)])
        light = build("L", light_argv)
        time.sleep(2.0)  # K runs, is sampled, and is killed with its gate
        killed.send_signal(signal.SIGKILL)
        for p in procs:
            p.wait(timeout=120)

        heavy_ids = {e["id"]: e["phase"] for e in _events(state) if e["event"] == "start"}
        assert sorted(heavy_ids.values()) == ["P-A", "P-B", "P-C", "P-D", "P-K"]

        def rows() -> list[dict]:
            got = store.builds(state)
            return got if len(got) >= 5 else []
        rows = _wait(rows, 30, "five build summaries")
    finally:
        s.stop()
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()

    assert light.returncode == 0 and all(p.returncode == 0 for p in procs[1:5])
    events = _events(state)
    light_ids = {e["id"] for e in events if e["event"] == "bypass"}
    assert len(light_ids) == 1 and all(e["phase"] == "P-L" for e in events
                                       if e["id"] in light_ids)

    # exactly the heavy builds, once each; the light command is not a build
    by_phase = {r["phase"]: r for r in rows}
    assert len(rows) == 5 and set(by_phase) == {"P-A", "P-B", "P-C", "P-D", "P-K"}
    assert {r["id"] for r in rows} == set(heavy_ids)
    assert not light_ids & {r["id"] for r in rows}

    for tag in "ABCD":
        r = by_phase[f"P-{tag}"]
        assert r["source"] == "events" and r["ended_by"] == "end" and r["exit"] == 0, r
        assert RUN_S - 0.5 <= r["run_s"] <= RUN_S + 10, r
        assert r["wait_s"] is not None and r["wait_s"] >= 0, r
        assert 0.5 <= r["cpu_s"] <= r["run_s"] + 1, r  # one core, at most
        assert MB * 0.8 <= r["peak_rss_mb"] <= MB * 4, r
        assert r["peak_anon_mb"] >= MB * 0.8, r
        assert r["samples"] >= 2 and not r["partial"], r
        assert r["slot"] in range(MAX), r
    # four builds for two slots behind a running one: some of them waited
    assert max(by_phase[f"P-{t}"]["wait_s"] for t in "ABCD") >= RUN_S - 1

    k = by_phase["P-K"]
    assert k["exit"] is None and k["ended_by"] in ("gone", "end"), k
    assert 1.0 <= k["run_s"] < 30, k
    assert k["cpu_s"] > 0.3 and k["peak_rss_mb"] >= MB * 0.8, k

    # never more than MAX at once: the builds' own marks, the gate's log, the sampler's rows
    marks: dict[str, dict[str, float]] = {}
    for line in log.read_text().splitlines():
        what, tag, ts = line.split()
        marks.setdefault(tag, {})[what] = float(ts)
    assert set(marks) == set("ABCDK") and all(set(m) == {"start", "end"} for m in marks.values())
    assert _peak([(m["start"], m["end"]) for m in marks.values()]) <= MAX
    starts = {e["id"]: e["ts"] for e in events if e["event"] == "start"}
    ends = {e["id"]: e["ts"] for e in events if e["event"] == "end" and e["id"] in starts}
    ends.update({r["id"]: r["ended"] for r in rows if r["id"] not in ends})  # K's gate died
    assert _peak([(starts[i], ends[i]) for i in starts]) <= MAX
    samples = store.history(state, 0)
    assert samples and max(r.get("nb", 0) for r in samples if r.get("k") == "s") <= MAX
    assert max(r.get("nb", 0) for r in samples if r.get("k") == "s") == MAX
