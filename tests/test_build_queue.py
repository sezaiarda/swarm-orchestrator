"""The ``swarm build`` queue with real processes: FIFO order under contention,
never more than ``max_concurrent`` heavy builds (also against an old-style
bare-flock holder), crashes of waiters and holders, light bypass, pre-flight,
``--timeout`` counted from the start, short-first with bounded overtaking, the
event log's shape and ``--status``.

Each build is a fake ``cargo`` (a shell script on PATH) that appends
``start``/``end`` lines to a shared log, so the order and overlap of the real
builds can be read back.
"""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarm_orchestrator import buildlog, buildsem
from swarm_orchestrator.config import load

KEYS = {"ts", "event", "id", "phase", "pid", "slot", "cls", "argv", "cwd", "wait_s", "run_s",
        "exit", "idle_s", "hold"}


class Gate:
    """A temp state dir, a project dir with a Cargo.toml and a fake cargo."""

    def __init__(self, tmp: Path, max_concurrent: int = 1, overtake: int = 0):
        self.tmp = tmp
        self.state = tmp / "state"
        self.proj = tmp / "proj"
        self.proj.mkdir(parents=True)
        (self.proj / "Cargo.toml").write_text("[package]\n")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.log = tmp / "builds.log"
        cargo = self.bin / "cargo"
        cargo.write_text(f'#!/bin/sh\necho "start $TAG" >> {self.log}\nsleep "${{DUR:-0.2}}"\n'
                         f'echo "end $TAG" >> {self.log}\nexit "${{RC:-0}}"\n')
        cargo.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SWARM_")}
        self.env.update(PATH=f"{self.bin}:{os.environ['PATH']}", SWARM_STATE_DIR=str(self.state),
                        SWARM_BUILD_MAX=str(max_concurrent), SWARM_BUILD_OVERTAKE=str(overtake))
        self.procs: list[subprocess.Popen] = []

    def cfg(self):
        old = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update(self.env)
            return load(project_dir=str(self.proj))
        finally:
            os.environ.clear()
            os.environ.update(old)

    def start(self, tag: str, *cmd: str, dur: float = 0.2, rc: int = 0, extra=(),
              env: dict | None = None) -> subprocess.Popen:
        args = [sys.executable, "-m", "swarm_orchestrator", "build", *extra,
                *(cmd or ("cargo", "build"))]
        e = dict(self.env, TAG=tag, DUR=str(dur), RC=str(rc), SWARM_PHASE=f"P-{tag}",
                 **(env or {}))
        p = subprocess.Popen(args, cwd=self.proj, env=e, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def events(self) -> list[dict]:
        path = self.state / "buildsem" / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def wait_event(self, kind: str, phase: str, timeout: float = 15.0) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            for e in self.events():
                if e["event"] == kind and e["phase"] == phase:
                    return e
            time.sleep(0.02)
        raise AssertionError(f"no {kind} event for {phase}: {self.events()}")

    def lines(self) -> list[str]:
        return self.log.read_text().split("\n")[:-1] if self.log.exists() else []

    def cleanup(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.communicate()


@pytest.fixture
def gate(tmp_path):
    made: list[Gate] = []

    def make(**kw) -> Gate:
        made.append(Gate(tmp_path, **kw))
        return made[-1]

    yield make
    for g in made:
        g.cleanup()


def _starts(lines: list[str]) -> list[str]:
    return [line.split()[1] for line in lines if line.startswith("start")]


def _max_overlap(lines: list[str]) -> int:
    running = peak = 0
    for line in lines:
        running += 1 if line.startswith("start") else -1
        peak = max(peak, running)
    return peak


def _finish(procs, timeout=60):
    for p in procs:
        p.wait(timeout=timeout)


# -- order and the cap ----------------------------------------------------
def test_fifo_order_under_contention(gate):
    g = gate(max_concurrent=1)
    procs = [g.start("0", dur=1.0)]
    g.wait_event("start", "P-0")
    tags = [str(i) for i in range(1, 9)]
    for t in tags:  # arrive one after another, each queued before the next comes
        procs.append(g.start(t, dur=0.05))
        g.wait_event("queued", f"P-{t}")
    _finish(procs)
    assert all(p.returncode == 0 for p in procs)
    assert _starts(g.lines()) == ["0", *tags]
    assert _max_overlap(g.lines()) == 1


def test_never_more_than_max_concurrent_and_next_slot_goes_to_oldest(gate):
    g = gate(max_concurrent=2)
    procs = [g.start("a", dur=0.8), g.start("b", dur=0.8)]
    g.wait_event("start", "P-a")
    g.wait_event("start", "P-b")
    tags = [str(i) for i in range(10)]
    for t in tags:
        procs.append(g.start(t, dur=random.choice((0.05, 0.2, 0.4))))
        g.wait_event("queued", f"P-{t}")
    _finish(procs)
    lines = g.lines()
    assert _max_overlap(lines) == 2  # the cap holds and both slots are used
    assert _starts(lines)[2:] == tags  # served in arrival order
    starts = [e for e in g.events() if e["event"] == "start"]
    assert {e["slot"] for e in starts} == {0, 1}


def test_old_style_flock_holder_and_new_queue_share_the_cap(gate):
    """A pre-queue ``swarm build`` only takes the bare slot flock; the new gate
    must count it: it waits while the old one holds the slot."""
    g = gate(max_concurrent=1)
    cfg = g.cfg()
    fd = buildsem._try_once(cfg)  # exactly what the old code did before exec'ing
    assert fd is not None
    p = g.start("new")
    g.wait_event("queued", "P-new")
    time.sleep(1.0)
    assert g.lines() == []  # still waiting behind the old-style holder
    os.close(fd)  # the old build ends
    p.wait(timeout=15)
    assert p.returncode == 0 and _starts(g.lines()) == ["new"]
    # and the other way round: an old-style poller cannot get in while it runs
    p = g.start("new2", dur=1.0)
    g.wait_event("start", "P-new2")
    assert buildsem._try_once(cfg) is None
    p.wait(timeout=15)
    fd = buildsem._try_once(cfg)
    assert fd is not None
    os.close(fd)


# -- crashes ----------------------------------------------------------------
def test_a_killed_waiter_is_skipped(gate):
    g = gate(max_concurrent=1)
    holder = g.start("h", dur=1.0)
    g.wait_event("start", "P-h")
    dead = g.start("dead")
    g.wait_event("queued", "P-dead")
    after = g.start("after")
    g.wait_event("queued", "P-after")
    dead.send_signal(signal.SIGKILL)
    _finish([holder, after])
    assert after.returncode == 0
    assert _starts(g.lines()) == ["h", "after"]
    assert list((g.state / "buildsem" / "queue").iterdir()) == []  # its ticket was pruned


def test_a_killed_holder_frees_the_slot_and_gets_a_synthetic_end(gate):
    g = gate(max_concurrent=1)
    holder = g.start("h", dur=30)
    g.wait_event("start", "P-h")
    waiter = g.start("w")
    g.wait_event("queued", "P-w")
    # SIGKILL the whole build: the gate process and every process under it
    build_pid = g.wait_event("start", "P-h")["pid"]
    tree = [holder.pid, build_pid, *_descendants(build_pid)]
    for pid in tree:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    waiter.wait(timeout=15)
    assert waiter.returncode == 0
    ends = [e for e in g.events() if e["event"] == "end" and e["phase"] == "P-h"]
    assert len(ends) == 1 and ends[0]["exit"] is None  # written by the gate, once
    assert ends[0]["pid"] == build_pid


def test_killing_only_swarm_build_stops_the_build_too(gate):
    """If the gate process alone is SIGKILLed, its build is told to stop and the
    slot frees once the build's processes are gone -- never before."""
    g = gate(max_concurrent=1)
    holder = g.start("h", dur=2)
    g.wait_event("start", "P-h")
    waiter = g.start("w")
    g.wait_event("queued", "P-w")
    os.kill(holder.pid, signal.SIGKILL)
    waiter.wait(timeout=15)
    assert waiter.returncode == 0
    lines = g.lines()
    assert "end h" not in lines  # the build was stopped (SIGTERM on its parent's death)...
    # ...but its leftover `sleep` still held the slot, so w started only after it
    started_h = g.wait_event("start", "P-h")["ts"]
    assert g.wait_event("start", "P-w")["ts"] - started_h > 1.8
    assert g.wait_event("end", "P-h")["exit"] is None  # the gate wrote the end h never did


def _descendants(pid: int) -> list[int]:
    from swarm_orchestrator import procs

    table = procs.table()
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        kids = [c for c, pp in table.items() if pp == p]
        out += kids
        todo += kids
    return out


# -- light, pre-flight, timeout -------------------------------------------
def test_light_commands_bypass_a_busy_gate(gate):
    g = gate(max_concurrent=1)
    holder = g.start("h", dur=3)
    g.wait_event("start", "P-h")
    t0 = time.time()
    light = g.start("l", "cargo", "metadata")
    light.wait(timeout=10)
    assert light.returncode == 0 and time.time() - t0 < 2.5  # did not wait for h
    assert "light command" in light.stderr.read()
    kinds = [(e["event"], e["cls"]) for e in g.events() if e["phase"] == "P-l"]
    assert kinds == [("bypass", "light"), ("end", "light")]
    holder.wait(timeout=15)


def test_preflight_refuses_before_queueing(gate):
    g = gate(max_concurrent=1)
    holder = g.start("h", dur=3)
    g.wait_event("start", "P-h")
    t0 = time.time()
    missing = g.start("m", "no-such-build-tool", "all")
    (g.proj / "sub").mkdir()
    wrong_dir = g.start("d", "sh", "-c", "cd nodir && cargo build")
    missing.wait(timeout=10)
    wrong_dir.wait(timeout=10)
    assert time.time() - t0 < 2.5  # neither waited behind h
    assert missing.returncode == 127 and "cannot run" in missing.stderr.read()
    assert wrong_dir.returncode == 2 and "cd target 'nodir'" in wrong_dir.stderr.read()
    for phase in ("P-m", "P-d"):
        assert [e["event"] for e in g.events() if e["phase"] == phase] == ["preflight_fail"]
    holder.wait(timeout=15)


def test_timeout_counts_from_the_start_not_the_queue(gate):
    g = gate(max_concurrent=1)
    holder = g.start("h", dur=2.0)
    g.wait_event("start", "P-h")
    p = g.start("t", dur=30, extra=("--timeout", "1"))
    p.wait(timeout=30)
    assert p.returncode == 124
    start = g.wait_event("start", "P-t")
    end = g.wait_event("end", "P-t")
    assert start["wait_s"] > 1.0  # it queued longer than its timeout...
    assert 0.9 < end["run_s"] < 8 and end["exit"] == 124  # ...and was stopped 1s after start
    assert "start t" in g.lines() and "end t" not in g.lines()
    holder.wait(timeout=15)


def test_exit_code_and_batch_script_in_one_turn(gate):
    g = gate(max_concurrent=1)
    script = g.proj / "gate.sh"
    script.write_text("TAG=s1 cargo build\nTAG=s2 cargo build\nfalse\nTAG=s3 cargo build\n")
    p = g.start("b", extra=("--script", str(script)))
    p.wait(timeout=20)
    assert p.returncode == 1  # stops at the first failing step
    assert _starts(g.lines()) == ["s1", "s2"]
    starts = [e for e in g.events() if e["phase"] == "P-b" and e["event"] == "start"]
    assert len(starts) == 1  # one turn for the whole batch
    p = g.start("rc", rc=3)
    p.wait(timeout=20)
    assert p.returncode == 3 and g.wait_event("end", "P-rc")["exit"] == 3


def test_a_nested_swarm_build_runs_inside_the_held_slot(gate):
    g = gate(max_concurrent=1)
    inner = f"{sys.executable} -m swarm_orchestrator build cargo build"
    p = g.start("n", "sh", "-c", f"TAG=inner {inner}")
    p.wait(timeout=20)
    assert p.returncode == 0 and _starts(g.lines()) == ["inner"]


# -- short first, bounded -------------------------------------------------
def _seed_history(g: Gate, argv: str, run_s: float, n: int = 3) -> None:
    cfg = g.cfg()
    for _ in range(n):
        buildlog.event(cfg, "end", id="seed", phase=None, pid=1, slot=0, cls="heavy",
                       argv=argv, cwd=str(g.proj), run_s=run_s, exit=0)


def test_a_short_build_goes_ahead_of_a_long_one(gate):
    g = gate(max_concurrent=1, overtake=2)
    _seed_history(g, "cargo test", 2.0)  # short
    _seed_history(g, "cargo build", 600.0)  # long
    procs = [g.start("h", dur=1.0)]
    g.wait_event("start", "P-h")
    procs.append(g.start("long"))
    g.wait_event("queued", "P-long")
    procs.append(g.start("short", "cargo", "test"))
    g.wait_event("queued", "P-short")
    _finish(procs)
    assert _starts(g.lines()) == ["h", "short", "long"]


def test_select_bounds_overtaking():
    def t(seq, short):
        return {"id": f"t{seq}", "seq": seq, "pred_s": 5 if short else 900}

    q = [t(1, False), t(2, False), t(3, True), t(4, True), t(5, True)]
    order = [x["id"] for x in buildsem.service_order(q, {}, 2, 60)]
    assert order == ["t3", "t4", "t1", "t2", "t5"]  # each long one passed at most twice
    assert [x["id"] for x in buildsem.service_order(q, {}, 0, 60)] == [
        "t1", "t2", "t3", "t4", "t5"]  # overtake 0: FIFO


def test_no_waiter_is_passed_more_than_overtake_times():
    rng = random.Random(7)
    for trial in range(200):
        k = rng.randint(0, 3)
        waiting, counts, seq, passed = [], {}, 0, {}
        for _step in range(60):
            for _ in range(rng.randint(0, 2)):  # arrivals
                seq += 1
                waiting.append({"id": f"t{seq}", "seq": seq,
                                "pred_s": rng.choice((None, 5, 30, 400))})
            chosen = buildsem.select(waiting, counts, k, 60)
            if chosen is None:
                continue
            for w in waiting:
                if w["seq"] < chosen["seq"]:
                    counts[w["id"]] = counts.get(w["id"], 0) + 1
                    passed[w["id"]] = passed.get(w["id"], 0) + 1
            waiting.remove(chosen)
        assert all(n <= k for n in passed.values()), (trial, k, passed)


# -- the log and --status -------------------------------------------------
def test_event_log_shape(gate):
    g = gate(max_concurrent=1)
    h = g.start("h", dur=0.8)
    g.wait_event("start", "P-h")
    w = g.start("w")
    _finish([h, w])
    g.start("l", "git", "--version").wait(timeout=10)
    events = g.events()
    assert all(set(e) == KEYS for e in events)
    by = {(e["phase"], e["event"]): e for e in events}
    q, s, e = by[("P-w", "queued")], by[("P-w", "start")], by[("P-w", "end")]
    assert q["id"] == s["id"] == e["id"] and q["cls"] == "heavy"
    assert isinstance(s["wait_s"], float) and s["wait_s"] > 0.2 and s["slot"] == 0
    assert isinstance(e["run_s"], float) and e["exit"] == 0 and e["pid"] == s["pid"]
    assert s["pid"] != q["pid"]  # start/end carry the build's own pid
    assert q["wait_s"] is None and q["run_s"] is None and q["exit"] is None
    assert by[("P-l", "bypass")]["slot"] is None and by[("P-l", "end")]["cls"] == "light"
    assert s["argv"] == "cargo build" and s["cwd"] == str(g.proj)


def test_event_log_rotates(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    cfg = load(project_dir=str(tmp_path))
    monkeypatch.setattr(buildlog, "ROTATE_BYTES", 2000)
    for i in range(60):
        buildlog.event(cfg, "bypass", id=str(i), phase=None, pid=1, slot=None, cls="light",
                       argv=["x" * 400], cwd="/")
    path = buildlog.events_path(cfg)
    assert path.with_name("events.jsonl.1").exists() and path.stat().st_size < 4000
    rec = json.loads(path.read_text().splitlines()[-1])
    assert len(rec["argv"]) == buildlog.ARGV_MAX
    assert [e["id"] for e in buildlog.read_events(cfg)][-1] == "59"


def test_status_shows_holder_queue_and_recent(gate):
    g = gate(max_concurrent=1)
    h = g.start("h", dur=2.0)
    g.wait_event("start", "P-h")
    w = g.start("w")
    g.wait_event("queued", "P-w")
    env = dict(g.env)
    out = subprocess.run([sys.executable, "-m", "swarm_orchestrator", "build", "--status",
                          "--json"], cwd=g.proj, env=env, capture_output=True, text=True,
                         timeout=20)
    snap = json.loads(out.stdout)
    assert snap["slots"][0]["busy"] and snap["slots"][0]["phase"] == "P-h"
    assert [q["phase"] for q in snap["queue"]] == ["P-w"]
    text = subprocess.run([sys.executable, "-m", "swarm_orchestrator", "build", "--status"],
                          cwd=g.proj, env=env, capture_output=True, text=True, timeout=20).stdout
    assert "slot 0: P-h `cargo build` running" in text and "1. P-w" in text
    _finish([h, w])
    err = w.stderr.read()
    assert "#1 of 1" in err and "slot 0: P-h" in err
    assert "starting on slot 0" in err and "ran " in err and "exit 0" in err
    from swarm_orchestrator import buildstatus

    assert buildstatus.summary_line(g.cfg()).startswith("build gate: 0/1 busy")
    text = subprocess.run([sys.executable, "-m", "swarm_orchestrator", "build", "--status"],
                          cwd=g.proj, env=env, capture_output=True, text=True, timeout=20).stdout
    assert "recent:" in text and "exit 0 `cargo build`" in text
