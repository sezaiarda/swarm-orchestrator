"""Idle yield in the build gate, with real processes and a short window.

A holder that does nothing for ``[build].idle_yield_s`` is set aside: it keeps
running, stops counting against ``max_concurrent``, and the next waiter starts
beside it. Proved here: the yield itself and its events; no yield for a busy
holder, across short quiet gaps, for work that runs in a daemon, or under
``--hold``; what the command itself is told; a woken holder counts again; the
cap on set-aside holders; SIGKILL of a yielded holder
and of the waiter that was measuring; gc and old callers still shut out;
``--status``; and the rule itself (:func:`buildidle.advance`) sample by sample.

Each build is a fake ``cargo`` that follows ``$PLAN`` (``sleep:3 burn:2``: idle
for three seconds, then spin one core for two) and logs its start and end.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_build_queue import KEYS, Gate, _descendants, _finish, _max_overlap

from swarm_orchestrator import buildclass, buildidle, buildsem, buildstatus, gc
from swarm_orchestrator.resources import ptree

WINDOW = 2  # [build].idle_yield_s in these tests

CARGO = """\
#!{python}
import os, sys, time
def mark(what):
    with open({log!r}, "a") as fh:
        fh.write(what + " " + os.environ.get("TAG", "") + "\\n")
mark("start")
for step in os.environ.get("PLAN", "sleep:0.2").split():
    kind, _, secs = step.partition(":")
    end = time.time() + float(secs)
    if kind == "burn":
        while time.time() < end:
            pass
    else:
        time.sleep(float(secs))
mark("end")
sys.exit(int(os.environ.get("RC", "0")))
"""


class IdleGate(Gate):
    def __init__(self, tmp: Path, max_concurrent: int = 1, yield_s: int = WINDOW,
                 yield_max: int = 2):
        super().__init__(tmp, max_concurrent=max_concurrent)
        (self.bin / "cargo").write_text(CARGO.format(python=sys.executable, log=str(self.log)))
        docker = self.bin / "docker"  # a client that waits while "the daemon" works
        docker.write_text('#!/bin/sh\nsleep "${DUR:-3}"\n')
        docker.chmod(0o755)
        self.env.update(SWARM_BUILD_IDLE_YIELD_S=str(yield_s),
                        SWARM_BUILD_IDLE_YIELD_MAX=str(yield_max))

    def run(self, tag: str, plan: str, *cmd: str, env: dict | None = None):
        return self.start(tag, *cmd, env={"PLAN": plan, **(env or {})})

    def kinds(self, phase: str) -> list[str]:
        return [e["event"] for e in self.events() if e["phase"] == phase]

    def look(self) -> tuple[dict, str]:
        """``--status`` without starting a process (when every second counts)."""
        snap = buildstatus.snapshot(self.cfg())
        return snap, buildstatus.render(snap)

    def status(self) -> tuple[dict, str]:
        base = [sys.executable, "-m", "swarm_orchestrator", "build", "--status"]
        snap = subprocess.run([*base, "--json"], cwd=self.proj, env=self.env,
                              capture_output=True, text=True, timeout=20)
        text = subprocess.run(base, cwd=self.proj, env=self.env, capture_output=True,
                              text=True, timeout=20)
        return json.loads(snap.stdout), text.stdout


@pytest.fixture
def gate(tmp_path):
    made: list[IdleGate] = []

    def make(**kw) -> IdleGate:
        made.append(IdleGate(tmp_path, **kw))
        return made[-1]

    yield make
    for g in made:
        g.cleanup()


def _kill_tree(gate_pid: int, build_pid: int) -> None:
    for pid in [gate_pid, build_pid, *_descendants(build_pid)]:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# -- the yield ----------------------------------------------------------------
def test_an_idle_holder_yields_and_the_waiter_starts_beside_it(gate):
    g = gate()
    h = g.run("h", f"sleep:{WINDOW * 2 + 1}")
    start_h = g.wait_event("start", "P-h")
    w = g.run("w", "burn:1")
    y = g.wait_event("yield", "P-h")
    start_w = g.wait_event("start", "P-w")
    _finish([w])
    assert h.poll() is None  # the idle holder was not stopped: it is still running
    _finish([h])
    assert (h.returncode, w.returncode) == (0, 0)
    assert g.lines() == ["start h", "start w", "end w", "end h"]

    assert all(set(e) == KEYS for e in g.events())
    assert (y["id"], y["pid"], y["slot"], y["cls"]) == (start_h["id"], start_h["pid"], 0, "heavy")
    assert y["ts"] - start_h["ts"] >= WINDOW  # never before a full window of quiet
    assert WINDOW <= y["idle_s"] < WINDOW + 3 and y["run_s"] >= y["idle_s"] - 0.5
    assert y["wait_s"] is None and y["exit"] is None
    assert start_w["ts"] >= y["ts"] and start_w["slot"] == 0  # the same slot, shared
    assert g.kinds("P-h") == ["queued", "start", "yield", "end"]  # an end, no unyield
    assert g.wait_event("end", "P-h")["exit"] == 0
    err = w.stderr.read()
    assert "beside P-h `cargo build`" in err and "its slot was yielded" in err
    assert "--hold" not in err  # w itself was never set aside: nothing to tell it


def test_the_command_that_was_set_aside_is_told_then_and_at_the_end(gate):
    """Whoever ran the idle command reads its output afterwards. It must be able
    to tell that other builds may have run beside it: once when the slot is
    released, and once more as the last line."""
    g = gate()
    h = g.run("h", f"sleep:{WINDOW * 2 + 1.5}")
    g.wait_event("start", "P-h")
    w = g.run("w", "burn:1")
    y = g.wait_event("yield", "P-h")
    time.sleep(1.3)  # h's own `swarm build` looks once a second
    assert h.poll() is None
    _finish([h, w])
    lines = [x for x in h.stderr.read().splitlines() if x.startswith("swarm build: ")]
    at = time.strftime("%H:%M:%S", time.localtime(y["ts"]))
    then = [x for x in lines if "this command was idle for" in x]
    assert len(then) == 1 and f"its build slot was released at {at}" in then[0]
    assert "nothing was stopped" in then[0] and "`swarm build --hold ...`" in then[0]
    assert lines.index(then[0]) < next(i for i, x in enumerate(lines) if " ran " in x)
    last = lines[-1]  # after "ran ..., exit 0": what a reader of the tail sees
    assert last.startswith("swarm build: note: this command sat idle for")
    assert f"released at {at}" in last and "other builds may have run beside it for" in last
    assert "`swarm build --hold ...`" in last
    assert g.wait_event("end", "P-h")["exit"] == 0 and h.returncode == 0


def test_hold_keeps_the_slot_however_idle_the_command_looks(gate):
    g = gate()
    out = subprocess.run([sys.executable, "-m", "swarm_orchestrator", "build", "--help"],
                         cwd=g.proj, env=g.env, capture_output=True, text=True, timeout=20)
    assert "--hold" in out.stdout and "swarm build --hold -- ./measure.sh" in out.stdout
    h = g.start("h", "cargo", "build", extra=("--hold",), env={"PLAN": f"sleep:{WINDOW + 2.5}"})
    start_h = g.wait_event("start", "P-h")
    w = g.run("w", "sleep:0.1")
    g.wait_event("queued", "P-w")
    time.sleep(WINDOW + 1.0)  # idle for more than a window, and still the holder
    snap, text = g.look()
    assert snap["slots"][0]["phase"] == "P-h" and snap["slots"][0]["hold"] is True
    assert snap["yielded"] == [] and "keeps its slot while idle (--hold)" in text
    _finish([h, w])
    assert g.lines() == ["start h", "end h", "start w", "end w"]
    assert "yield" not in [e["event"] for e in g.events()]
    assert start_h["hold"] is True and g.wait_event("start", "P-w")["hold"] is False
    assert all(e["hold"] is None for e in g.events() if e["event"] != "start")
    assert "--hold" not in h.stderr.read()  # nothing to tell it: it was never set aside


def test_hold_takes_a_slot_even_for_a_light_command(gate):
    """``sleep`` alone skips the gate. Under ``--hold`` it is the measurement that
    wants the machine to itself: it queues, holds a slot, and nothing starts
    beside it."""
    g = gate()
    light = g.start("light", "sleep", "0.2")
    light.wait(timeout=15)
    assert g.kinds("P-light") == ["bypass", "end"]
    h = g.start("h", "sleep", str(WINDOW + 2), extra=("--hold",))
    start_h = g.wait_event("start", "P-h")
    w = g.run("w", "sleep:0.1")
    _finish([h, w])
    assert g.kinds("P-h") == ["queued", "start", "end"]
    assert (start_h["cls"], start_h["hold"], start_h["slot"]) == ("heavy", True, 0)
    assert g.wait_event("start", "P-w")["ts"] >= g.wait_event("end", "P-h")["ts"] - 0.05
    assert "yield" not in [e["event"] for e in g.events()]


def test_a_busy_holder_never_yields(gate):
    g = gate()
    h = g.run("h", f"burn:{WINDOW + 2}")
    g.wait_event("start", "P-h")
    w = g.run("w", "sleep:0.1")
    _finish([h, w])
    assert g.lines() == ["start h", "end h", "start w", "end w"]
    assert "yield" not in [e["event"] for e in g.events()]


def test_short_quiet_gaps_inside_a_build_do_not_yield(gate):
    """Quiet for most of its run, but never for a whole window in a row."""
    g = gate()
    gap = WINDOW * 0.6
    h = g.run("h", f"burn:0.5 sleep:{gap} burn:0.5 sleep:{gap} burn:0.5 sleep:{gap} burn:0.3")
    g.wait_event("start", "P-h")
    w = g.run("w", "sleep:0.1")
    _finish([h, w])
    assert g.lines() == ["start h", "end h", "start w", "end w"]
    assert "yield" not in [e["event"] for e in g.events()]


def test_idle_yield_off_keeps_the_queue_as_it_was(gate):
    g = gate(yield_s=0)
    h = g.run("h", f"sleep:{WINDOW + 1.5}")
    g.wait_event("start", "P-h")
    w = g.run("w", "sleep:0.1")
    _finish([h, w])
    assert g.lines() == ["start h", "end h", "start w", "end w"]
    assert "yield" not in [e["event"] for e in g.events()]
    assert buildsem.seats(g.cfg()) == 1


def test_a_build_starts_beside_an_idle_holder_only_on_a_fresh_short_look(gate, monkeypatch):
    """The ordinary samples span seconds, and a holder that woke up a second ago
    can hide in such an average. So the sample that sets a holder aside does not
    by itself let a build start: one more look, over the last moment only, does."""
    g = gate()
    h = g.run("h", f"sleep:{WINDOW * 3 + 4}")
    start_h = g.wait_event("start", "P-h")
    cfg = g.cfg()
    monkeypatch.setattr(buildidle, "fresh_s", lambda _cfg: 0.1)  # as with the real 5 s / 1 s
    claims = []
    deadline = time.time() + WINDOW * 3
    while time.time() < deadline:
        with buildsem._qlock(cfg):
            claim = buildsem._claim(cfg)
        claims.append((claim, "yield" in g.kinds("P-h")))
        if claim is not None:
            break
        time.sleep(0.15)
    last, marked = claims[-1]
    assert last is not None and marked, claims
    assert (None, True) in claims[:-1]  # set aside, and still no start on that sample
    assert [b["id"] for b in last.beside] == [start_h["id"]] and last.slot == 0
    entry = buildidle.load(cfg)["h"][start_h["id"]]
    assert 0 < entry["dt"] <= 0.2 and entry["ok"]
    last.close()
    h.kill()


# -- waking up ----------------------------------------------------------------
def test_a_woken_holder_counts_again_and_blocks_new_starts(gate):
    g = gate()
    h = g.run("h", f"sleep:{WINDOW + 2} burn:6")
    g.wait_event("start", "P-h")
    beside = g.run("b", "burn:4.5")  # starts beside h, and is still running when h wakes
    g.wait_event("start", "P-b")
    # nobody is queued now: h's own `swarm build` notices that it woke up
    woke = g.wait_event("unyield", "P-h")
    late = g.run("l", "sleep:0.1")  # arrives with two builds working: it must wait
    _finish([h, beside, late])
    assert all(p.returncode == 0 for p in (h, beside, late))
    y, end_h = g.wait_event("yield", "P-h"), g.wait_event("end", "P-h")
    end_b, start_l = g.wait_event("end", "P-b"), g.wait_event("start", "P-l")
    assert woke["idle_s"] == pytest.approx(woke["ts"] - y["ts"], abs=0.01)
    assert set(woke) == KEYS and woke["id"] == y["id"]
    assert woke["why"] == "it is using CPU again"  # the log says what was seen
    assert end_b["ts"] > woke["ts"] and end_b["exit"] == 0  # the one beside it kept running
    assert end_b["ts"] < end_h["ts"]
    # b ended while h was still working: one build counts, so the slot is full
    assert start_l["ts"] >= end_h["ts"] - 0.05
    assert g.kinds("P-h") == ["queued", "start", "yield", "unyield", "end"]


# -- work that happens elsewhere ----------------------------------------------
def test_docker_style_commands_never_yield(gate):
    """A client waiting on a daemon is at 0% CPU and is not idle: by its argv
    (``docker build``), and by what runs in its tree when the script is opaque."""
    g = gate(max_concurrent=2)
    named = g.start("named", "docker", "build", ".", dur=WINDOW + 3)
    opaque = g.start("opaque", "sh", "-c", "x=$(true); docker build .", dur=WINDOW + 3)
    g.wait_event("start", "P-named")
    g.wait_event("start", "P-opaque")
    w = g.run("w", "sleep:0.1")
    g.wait_event("queued", "P-w")
    time.sleep(WINDOW + 1.0)
    snap, text = g.look()
    by = {s["phase"]: s for s in snap["slots"]}
    assert "docker" in by["P-named"]["noyield"] and "never yields" in text
    assert "noyield" not in by["P-opaque"] and snap["yielded"] == []
    _finish([named, opaque, w])
    assert "yield" not in [e["event"] for e in g.events()]
    first_end = min(g.wait_event("end", p)["ts"] for p in ("P-named", "P-opaque"))
    assert g.wait_event("start", "P-w")["ts"] >= first_end - 0.05


def test_daemon_side_reads_the_command_through_wrappers(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n")

    def why(*argv):
        return buildclass.daemon_side(buildclass.classify(list(argv), tmp_path))

    for argv in (["docker", "build", "."], ["docker", "buildx", "bake"],
                 ["docker", "compose", "build"], ["docker", "compose", "up", "-d"],
                 ["env", "X=1", "timeout", "600", "docker", "build", "."],
                 ["sh", "-c", "cargo build && docker buildx bake web"],
                 ["flock", "/tmp/x.lock", "nice", "docker", "build", "."],
                 ["sccache", "cargo", "build"], ["bazel", "build", "//..."]):
        assert why(*argv), argv
    for argv in (["cargo", "build"], ["sh", "-c", "docker ps && cargo test"],
                 ["sh", "-c", "docker buildx bake --print && cargo build"],
                 ["sh", "-c", "docker image inspect x; cargo nextest run"]):
        assert why(*argv) is None, argv


# -- the caps -----------------------------------------------------------------
def test_at_most_idle_yield_max_holders_are_set_aside(gate):
    g = gate(yield_max=1)
    first = g.run("h1", f"sleep:{WINDOW * 3 + 2.5}")
    g.wait_event("start", "P-h1")
    second = g.run("h2", "sleep:60")  # starts beside h1, then idles itself
    g.wait_event("start", "P-h2")
    w = g.run("w", "burn:0.3")
    g.wait_event("queued", "P-w")
    time.sleep(WINDOW + 1.5)  # h2 has been quiet a full window, but one is set aside already
    assert first.poll() is None and "yield" not in g.kinds("P-h2")
    assert "start" not in g.kinds("P-w")
    _finish([first, w])  # h1 ends: now h2 may be set aside, and w starts beside it
    end_first = g.wait_event("end", "P-h1")
    assert g.wait_event("yield", "P-h2")["ts"] >= end_first["ts"]
    assert g.wait_event("start", "P-w")["ts"] >= end_first["ts"]
    assert second.poll() is None
    assert _max_overlap(g.lines()) == 2  # never more than max_concurrent + idle_yield_max
    seats = sorted(p.name for p in (g.state / "buildsem").iterdir()
                   if p.name.startswith("seat"))
    assert seats == ["seat0", "seat1"]


# -- crashes --------------------------------------------------------------------
def test_a_killed_yielded_holder_leaks_nothing(gate):
    g = gate()
    h = g.run("h", "sleep:60")
    start_h = g.wait_event("start", "P-h")
    beside = g.run("b", "burn:3.5")
    g.wait_event("start", "P-b")
    _kill_tree(h.pid, start_h["pid"])  # SIGKILL: the gate process and the build
    late = g.run("l", "sleep:0.1")  # b still counts: the dead holder's mark frees nothing
    _finish([beside, late])
    assert (beside.returncode, late.returncode) == (0, 0)
    assert g.wait_event("start", "P-l")["ts"] >= g.wait_event("end", "P-b")["ts"] - 0.05
    ends = [e for e in g.events() if e["event"] == "end" and e["phase"] == "P-h"]
    assert len(ends) == 1 and ends[0]["exit"] is None  # written by the gate, once
    assert "unyield" not in g.kinds("P-h")
    cfg = g.cfg()
    assert buildsem.live_holders(cfg) == []  # no seat, no slot, no mark left behind
    fd = buildsem._try_once(cfg)
    assert fd is not None
    os.close(fd)
    again = g.run("a", "sleep:0.1")
    again.wait(timeout=15)
    assert g.wait_event("start", "P-a")["wait_s"] < 1.0


def test_a_killed_measuring_waiter_does_not_stop_the_yield(gate):
    g = gate()
    h = g.run("h", f"sleep:{WINDOW * 2 + 1.5}")
    g.wait_event("start", "P-h")
    dead = g.run("dead", "sleep:0.1")
    g.wait_event("queued", "P-dead")
    time.sleep(WINDOW / 2)  # it has measured the holder, and dies mid-window
    dead.send_signal(signal.SIGKILL)
    w = g.run("w", "burn:0.3")
    _finish([w])
    assert w.returncode == 0 and h.poll() is None  # w started beside the idle holder
    assert g.kinds("P-dead") == ["queued"]
    _finish([h])
    assert g.lines() == ["start h", "start w", "end w", "end h"]
    assert list((g.state / "buildsem" / "queue").iterdir()) == []


def test_a_stale_yield_mark_on_a_working_holder_is_measured_again_first(gate):
    """Whoever marked the holder idle is gone and the holder has woken up since.
    The next waiter does not take the old mark's word: it measures, counts the
    holder again, and waits for it."""
    g = gate()
    h = g.run("h", f"burn:{WINDOW + 2}")
    start_h = g.wait_event("start", "P-h")
    cfg = g.cfg()
    then = time.time() - 60
    buildidle._save(cfg, {"ts": then, "h": {start_h["id"]: {
        "t": then, "cpu": 0.0, "io": 0, "quiet": then - 30, "yielded": then, "ok": True,
        "idle_s": 30.0}}})
    w = g.run("w", "sleep:0.1")
    _finish([h, w])
    assert g.lines() == ["start h", "end h", "start w", "end w"]
    assert g.kinds("P-h") == ["queued", "start", "unyield", "end"]


def test_a_process_a_build_left_behind_stops_blocking_once_idle(gate):
    """The build ended but a child it started still holds its seat and slot. It
    used to hold the slot until it exited; idle, it is set aside like any holder."""
    g = gate()
    left = g.start("left", "sh", "-c", "cargo build & sleep 0.3",
                   env={"PLAN": f"sleep:{WINDOW * 2 + 1.5}"})
    g.wait_event("end", "P-left")
    w = g.run("w", "burn:0.3")
    g.wait_event("queued", "P-w")
    snap, text = g.look()  # well before a window has passed
    assert snap["slots"][0]["left"] and "still holds the slot" in text
    _finish([left, w])
    assert g.kinds("P-left") == ["queued", "start", "end", "yield"]
    deadline = time.time() + 20
    while "end left" not in g.lines() and time.time() < deadline:
        time.sleep(0.1)
    assert g.lines() == ["start left", "start w", "end w", "end left"]


# -- gc and older callers stay shut out -----------------------------------------
def test_gc_and_old_callers_wait_for_a_yielded_holder_too(gate):
    """gc takes every slot before it deletes build output, and an older ``swarm
    build`` takes its slot whole: neither may get in while any build is alive,
    set aside or beside one."""
    g = gate()
    h = g.run("h", f"sleep:{WINDOW * 2 + 2.5}")
    g.wait_event("start", "P-h")
    w = g.run("w", "burn:1.5")
    g.wait_event("start", "P-w")
    cfg = g.cfg()
    opts = gc.GcOptions(gate_timeout_s=0.3)
    for _ in range(2):  # with both alive, then with only the yielded holder left
        with pytest.raises(gc.GcRefused):
            with gc.build_gate(cfg, opts):
                pass
        assert buildsem._try_once(cfg) is None
        _finish([w])
    assert h.poll() is None
    _finish([h])
    with gc.build_gate(cfg, opts) as fds:
        assert len(fds) == 1


# -- what it says ---------------------------------------------------------------
def test_status_shows_yielded_holders_apart(gate):
    g = gate()
    h = g.run("h", f"sleep:{WINDOW * 3 + 2}")
    g.wait_event("start", "P-h")
    w = g.run("w", "burn:4")
    g.wait_event("start", "P-w")
    queued = g.run("q", "sleep:0.1")
    g.wait_event("queued", "P-q")
    time.sleep(0.6)
    snap, text = g.status()
    assert [s["phase"] for s in snap["slots"]] == ["P-w"] and snap["slots"][0]["busy"]
    [y] = snap["yielded"]
    assert (y["phase"], y["state"], y["slot"]) == ("P-h", "yielded", 0)
    assert WINDOW <= y["idle_s"] < WINDOW + 3 and y["running_s"] > y["idle_s"]
    assert snap["idle_yield_s"] == WINDOW and snap["idle_yield_max"] == 2
    assert "1 slot(s), 1 busy, 1 yielded, 1 waiting" in text
    assert "slot 0: P-w `cargo build` running" in text
    assert re.search(r"yielded: P-h `cargo build` yielded after \ds idle, still running", text)
    assert "nothing was stopped" in text
    assert "1 idle holder(s) yielded" in buildstatus.summary_line(g.cfg())
    _finish([h, w, queued])
    assert "yielded: P-h" in queued.stderr.read()  # the waiting line names it too
    _snap, text = g.status()
    assert "yielded" not in text.split("recent:")[0].replace("yields its slot", "")
    assert "(yielded " in text.split("recent:")[1]  # the recent line says how long


# -- the rule, sample by sample -----------------------------------------------
REC = {"id": "b1", "start_ts": 1000.0}


def _walk(samples, window=10.0, every=1.0, room=True, entry=None):
    """Feed ``(t, cpu_s, io, busy)`` samples; returns the entry and the changes."""
    changes = []
    for t, cpu, io, busy in samples:
        entry, change = buildidle.advance(entry, REC, buildidle.Sample(cpu, io, 1, busy),
                                          1000.0 + t, window, every, room)
        if change is not None:
            changes.append((change.kind, t, round(change.idle_s, 2)))
    return entry, changes


def test_the_rule_needs_a_whole_quiet_window():
    quiet = [(t, 0.0, 0, None) for t in range(1, 10)]
    assert _walk(quiet)[1] == []
    assert _walk(quiet + [(10, 0.0, 0, None)])[1] == [("yield", 10, 10.0)]
    # one busy second in the middle restarts the clock
    bump = [(t, 0.0 if t < 5 else 0.3, 0, None) for t in range(1, 14)]
    assert _walk(bump)[1] == []
    assert _walk(bump + [(14, 0.3, 0, None), (15, 0.3, 0, None)])[1] == [("yield", 15, 10.0)]


def test_the_rule_counts_disk_io_and_daemon_clients_as_work():
    io = [(t, 0.0, t * buildidle.IDLE_IO_BPS, None) for t in range(1, 30)]
    assert _walk(io)[1] == []
    daemon = [(t, 0.0, 0, "docker is running") for t in range(1, 30)]
    assert _walk(daemon)[1] == []
    entry, changes = _walk([(t, 0.0, 0, None) for t in range(1, 12)])
    assert changes == [("yield", 10, 10.0)]
    # a daemon client turning up in a set-aside holder's tree wakes it at once
    _e, changes = _walk([(12, 0.0, 0, "docker is running")], entry=entry)
    assert changes == [("unyield", 12, 2.0)]


def test_the_rule_has_hysteresis_between_idle_and_awake():
    base = [(t, 0.0, 0, None) for t in range(1, 11)]
    entry, changes = _walk(base)
    assert changes == [("yield", 10, 10.0)]
    # hovering above "quiet" but below "working": stays set aside, sample after sample
    cpu, hover = 0.0, []
    for t in range(11, 40):
        cpu += 0.3
        hover.append((t, cpu, 0, None))
    entry, changes = _walk(hover, entry=entry)
    assert changes == [] and entry["yielded"] and entry["ok"]
    # real work wakes it, and it is not set aside again before a new full window
    entry, changes = _walk([(40, cpu + 1.0, 0, None)], entry=entry)
    assert changes == [("unyield", 40, 30.0)] and entry["yielded"] is None
    cpu += 1.0
    again = [(t, cpu, 0, None) for t in range(41, 50)]
    assert _walk(again, entry=dict(entry))[1] == []
    assert _walk(again + [(50, cpu, 0, None)], entry=dict(entry))[1] == [("yield", 50, 10.0)]
    # the same hover never yields a holder that was not set aside
    never = [(t, 0.3 * t, 0, None) for t in range(1, 60)]
    assert _walk(never)[1] == []


def test_the_rule_respects_the_cap_and_unwatched_stretches():
    quiet = [(t, 0.0, 0, None) for t in range(1, 30)]
    entry, changes = _walk(quiet, room=False)
    assert changes == [] and entry["yielded"] is None  # no room: it stays a holder
    assert _walk([(30, 0.0, 0, None)], entry=entry)[1] == [("yield", 30, 30.0)]
    # nobody measured for ten minutes and next to nothing was used: quiet all along,
    # which one ordinary sample must confirm (the clock may have jumped a suspend)
    entry, changes = _walk([(600, 0.01, 0, None)])
    assert changes == [] and entry["ok"] is False and entry["quiet"] == 1000.0
    assert _walk([(601, 0.01, 0, None)], entry=dict(entry))[1] == [("yield", 601, 601.0)]
    assert _walk([(601, 0.9, 0, None)], entry=dict(entry))[1] == []  # it was not idle
    # ...and a few CPU seconds somewhere in the gap restart the clock
    entry, changes = _walk([(600, 3.0, 0, None)])
    assert changes == [] and entry["quiet"] == 1600.0 and entry["ok"] is True
    # a set-aside holder nobody watched: unknown until the next sample, not trusted
    entry, _ = _walk([(t, 0.0, 0, None) for t in range(1, 11)])
    entry, changes = _walk([(300, 4.0, 0, None)], entry=entry)
    assert changes == [] and entry["yielded"] and entry["ok"] is False
    entry, changes = _walk([(301, 4.0, 0, None)], entry=entry)
    assert changes == [] and entry["ok"] is True
    # busy on average over the whole gap: surely awake
    entry, _ = _walk([(t, 0.0, 0, None) for t in range(1, 11)])
    assert _walk([(300, 200.0, 0, None)], entry=entry)[1] == [("unyield", 300, 290.0)]


def test_the_rule_is_not_fooled_by_the_clock_or_a_starved_build():
    quiet = [(t, 0.0, 0, None) for t in range(1, 10)]
    # the clock stepped back: a new baseline, the quiet stretch starts over
    entry, _ = _walk(quiet)
    entry, changes = _walk([(5, 0.0, 0, None)], entry=entry)
    assert changes == [] and entry["quiet"] == 1005.0 and entry["ok"] is False
    assert _walk([(t, 0.0, 0, None) for t in range(6, 15)], entry=dict(entry))[1] == []
    # ...and a set-aside holder is not trusted again until a sample after it
    entry, _ = _walk(quiet + [(10, 0.0, 0, None)])
    entry, changes = _walk([(4, 0.0, 0, None)], entry=entry)
    assert changes == [] and entry["yielded"] and entry["ok"] is False
    # no CPU progress, but a process that is runnable (or waiting on disk): not idle
    entry = None
    for t in range(1, 40):
        s = buildidle.Sample(0.0, 0, 1, None, runnable=True)
        entry, change = buildidle.advance(entry, REC, s, 1000.0 + t, 10.0, 1.0, True)
        assert change is None
    # once set aside, that alone does not wake it: only real use does
    entry, _ = _walk(quiet + [(10, 0.0, 0, None)])
    s = buildidle.Sample(0.01, 0, 1, None, runnable=True)
    entry, change = buildidle.advance(entry, REC, s, 1011.0, 10.0, 1.0, True)
    assert change is None and entry["yielded"]


def _cfg_for(tmp_path, monkeypatch, **env):
    from swarm_orchestrator.config import load

    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    for key, val in {"SWARM_BUILD_MAX": 1, "SWARM_BUILD_IDLE_YIELD_S": 10, **env}.items():
        monkeypatch.setenv(key, str(val))
    cfg = load(project_dir=str(tmp_path))
    cfg.buildsem_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_a_yield_mark_is_only_trusted_fresh_alive_and_within_the_cap(tmp_path, monkeypatch):
    cfg = _cfg_for(tmp_path, monkeypatch, SWARM_BUILD_IDLE_YIELD_MAX=2)
    every = buildidle.sample_every(cfg)
    now = 5000.0
    st = {"ts": now, "h": {
        "a": {"t": now, "yielded": now - 50, "ok": True, "dt": 1.0},
        "b": {"t": now, "yielded": now - 40, "ok": True, "dt": 5.0},  # a long average
        "c": {"t": now, "yielded": now - 30, "ok": True},  # over the cap of two
        "stale": {"t": now - 3 * every - 2, "yielded": now - 60, "ok": True},
        "ahead": {"t": now + 30, "yielded": now - 60, "ok": True},  # the clock went back
        "unsure": {"t": now, "yielded": now - 60, "ok": False},
        "gone": {"t": now, "yielded": now - 60, "ok": True},  # no live holder has this id
        "busy": {"t": now, "yielded": None, "ok": True},
    }}
    holders = [{"id": i} for i in ("a", "b", "c", "stale", "ahead", "unsure", "busy")]
    holders.append({"id": "pinned", "noyield": "docker hands its work to a daemon"})
    assert [h["id"] for h in buildidle.set_aside(cfg, st, holders, now)] == ["a", "b"]
    # to start a build beside one, the look must be fresh and span a second or two:
    # a holder that woke up a second ago cannot hide in a five-second average
    fresh = buildidle.fresh_s(cfg)
    assert [h["id"] for h in buildidle.set_aside(cfg, st, holders, now, fine=True)] == ["a"]
    assert buildidle.set_aside(cfg, st, holders, now + fresh + 0.1, fine=True) == []
    off = _cfg_for(tmp_path, monkeypatch, SWARM_BUILD_IDLE_YIELD_S=0)
    assert buildidle.set_aside(off, st, holders, now) == []


def test_a_damaged_state_file_sets_nobody_aside(tmp_path, monkeypatch):
    """``idle.json`` is only a cache of measurements. Entries that are not
    measurements are dropped (the holder is measured afresh), and a measurement
    that fails outright leaves the queue as it is without idle yield."""
    cfg = _cfg_for(tmp_path, monkeypatch)
    path = cfg.buildsem_dir / buildidle.STATE
    good = {"t": 1.0, "cpu": 0.0, "io": 0, "quiet": 1.0, "yielded": None}
    path.write_text(json.dumps({"ts": "soon", "h": {
        "ok": good, "text": "yielded", "partial": {"yielded": 5.0},
        "typed": dict(good, t="1.0"), "marked": dict(good, yielded="yes")}}))
    assert buildidle.load(cfg) == {"ts": 0.0, "h": {"ok": good}}
    for junk in ("", "{", "[1, 2]", '{"h": 3}'):
        path.write_text(junk)
        assert buildidle.load(cfg) == {"ts": 0.0, "h": {}}

    def boom(*_a, **_k):
        raise RuntimeError("no /proc today")

    monkeypatch.setattr(buildidle.ptree, "scan", boom)
    holder = {"id": "b1", "start_ts": time.time() - 500, "seat_path": None}
    st = buildsem._idle_pass(cfg, [holder], time.time())
    assert st == {"ts": 0.0, "h": {}} and buildidle.set_aside(cfg, st, [holder], time.time()) == []


def test_measuring_drops_the_dead_and_skips_what_never_yields(tmp_path, monkeypatch):
    cfg = _cfg_for(tmp_path, monkeypatch)
    me = os.getpid()
    ticks = ptree.parse_stat(me, Path(f"/proc/{me}/stat").read_text()).start
    now = time.time()
    mine = {"id": "me", "pid": me, "pid_start": ticks, "gate_pid": me, "start_ts": now - 100,
            "seat_path": None}
    pinned = dict(mine, id="pinned", noyield="docker hands its work to a daemon")
    buildidle._save(cfg, {"ts": now - 60, "h": {"dead": {"t": now - 60, "cpu": 0.0, "io": 0,
                                                       "quiet": now - 90, "yielded": now - 70,
                                                       "ok": True}}})
    st, changes = buildidle.update(cfg, [mine, pinned], now)
    assert set(st["h"]) == {"me"} and changes == []  # this test process is not idle
    assert st["h"]["me"]["quiet"] == pytest.approx(now) and st["h"]["me"]["procs"] >= 1
    assert buildidle.load(cfg)["ts"] == pytest.approx(now, abs=0.01)
    # not due yet: nothing is read, the state stands
    again, _ = buildidle.update(cfg, [mine], now + 0.1)
    assert again["ts"] == st["ts"]


def test_the_measured_tree_includes_detached_children_holding_the_seat(tmp_path):
    """A child that left the build's process tree (its parent exited) still has
    the inherited seat open: it is measured, and the waiting gate process is not."""
    seat = tmp_path / "seat0"
    seat.write_text("{}")
    fd = os.open(seat, os.O_RDWR)
    try:
        # sh exits at once; its background sleep is re-parented and keeps the fd
        root = subprocess.Popen(["sh", "-c", "sleep 30 & echo $!"], pass_fds=(fd,),
                                stdout=subprocess.PIPE, text=True)
        orphan = int(root.stdout.readline())
        root.wait(timeout=10)
        me = os.getpid()
        table = ptree.scan()
        rec = {"pid": root.pid, "pid_start": None, "gate_pid": me,
               "gate_start": table[me].start}
        found = buildidle.members(rec, seat, table, ptree.children(table))
        assert orphan in found and me not in found
        sample = buildidle.measure(rec, seat, table, ptree.children(table))
        assert sample.busy is None and sample.procs == len(found)
        assert buildidle.measure(rec, None, table, ptree.children(table)).busy  # nobody seen
    finally:
        os.close(fd)
        try:
            os.kill(orphan, signal.SIGKILL)
        except (OSError, UnboundLocalError):
            pass


# -- a holder that is ending --------------------------------------------------
def _set_aside(cfg, bid: str, now: float) -> None:
    """``bid`` was set aside half a minute ago and last measured one sample ago."""
    then = now - buildidle.sample_every(cfg)
    buildidle._save(cfg, {"ts": then, "h": {bid: {"t": then, "cpu": 0.0, "io": 0,
                                                  "quiet": now - 60, "yielded": now - 30,
                                                  "ok": True}}})


def test_a_process_that_is_ending_is_not_one_that_cannot_be_measured(tmp_path, monkeypatch):
    """The kernel refuses the IO counters of a process that is exiting, as it
    does another user's. A sample that catches a set-aside holder between the
    scan and the read must not log a build that only finished as counting again."""
    cfg = _cfg_for(tmp_path, monkeypatch)
    child = subprocess.Popen(["sleep", "30"])
    try:
        table = ptree.scan()  # the look that still saw it alive
        me = os.getpid()
        rec = {"id": "b1", "pid": child.pid, "pid_start": table[child.pid].start, "gate_pid": me,
               "gate_start": table[me].start, "start_ts": time.time() - 100, "seat_path": None}
        child.kill()
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)  # ended, not yet reaped
        sample = buildidle.measure(rec, None, table, ptree.children(table))
        assert sample.busy is None and sample.procs == 1
        now = time.time()
        _set_aside(cfg, "b1", now)
        monkeypatch.setattr(buildidle.ptree, "scan", lambda root=ptree.PROC: table)
        st, changes = buildidle.update(cfg, [rec], now)
        assert changes == [] and st["h"]["b1"]["yielded"] and st["ts"] == pytest.approx(now)
    finally:
        child.kill()
        child.wait()


def test_only_a_live_process_that_cannot_be_read_keeps_a_holder_busy(tmp_path):
    """Told apart by a second look at ``stat``: a process that has given up its
    memory (it still reads as running for a moment) is ending; one that has
    memory is someone else's, and its work cannot be ruled out."""
    if os.geteuid() == 0:
        pytest.skip("root reads every file: nothing here is refused")

    def busy(pid: int, state: str, vsize: int) -> str | None:
        proc = tmp_path / str(pid)
        proc.mkdir()
        (proc / "stat").write_text(f"{pid} (cc) {state} 1 " + "0 " * 17 + f"77 {vsize} 0\n")
        (proc / "io").write_text("read_bytes: 1\nwrite_bytes: 1\n")
        (proc / "io").chmod(0)
        table = ptree.scan(tmp_path)
        rec = {"pid": pid, "pid_start": 77, "gate_pid": 1}
        return buildidle.measure(rec, None, table, ptree.children(table), tmp_path).busy

    assert busy(500, "R", 0) is None  # exiting: its memory is gone, it is not a zombie yet
    assert busy(501, "D", 0) is None
    assert busy(502, "S", 4096) == "a process in its tree cannot be measured"
    assert busy(503, "R", 4096) == "a process in its tree cannot be measured"


GATE = """\
import os, subprocess, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)  # the seat, held as a `swarm build` holds it
child = subprocess.Popen(["true"])
child.wait()  # the command has ended and is reaped; the seat is not released yet
print(child.pid, flush=True)
time.sleep(60)
"""


def test_a_command_that_has_ended_is_not_at_work_while_its_gate_closes_it(tmp_path, monkeypatch):
    """Between a command's exit and the release of its seat another waiter may
    measure: the command is gone and only its ``swarm build`` holds the seat.
    Nothing is at work, so a set-aside holder is not logged as counting again.
    A command not started yet, or a seat nobody can be seen on, counts as ever."""
    cfg = _cfg_for(tmp_path, monkeypatch)
    seat = tmp_path / "seat0"
    seat.write_text("{}")
    gate = subprocess.Popen([sys.executable, "-c", GATE, str(seat)], stdout=subprocess.PIPE,
                            text=True)
    try:
        ended = int(gate.stdout.readline())
        table = ptree.scan()
        kids = ptree.children(table)
        rec = {"id": "b1", "pid": ended, "pid_start": -1, "gate_pid": gate.pid,
               "gate_start": table[gate.pid].start, "start_ts": time.time() - 100,
               "seat_path": seat}
        sample = buildidle.measure(rec, seat, table, kids)
        assert sample.busy is None and sample.procs == 0
        now = time.time()
        _set_aside(cfg, "b1", now)
        st, changes = buildidle.update(cfg, [rec], now)
        assert changes == [] and st["h"]["b1"]["yielded"] and st["ts"] == pytest.approx(now)

        unseen = "its processes cannot be seen"
        starting = dict(rec, pid=None, pid_start=None)  # the record before the command runs
        assert buildidle.measure(starting, seat, table, kids).busy == unseen
        assert buildidle.measure(rec, None, table, kids).busy == unseen  # no seat to look at
        gate.kill()
        gate.wait()
        table = ptree.scan()  # the gate is gone: whoever holds the seat now is not seen
        assert buildidle.measure(rec, seat, table, ptree.children(table)).busy == unseen
    finally:
        gate.kill()
        gate.wait()
