"""gc and the build gate, with real processes: gc takes its turn through the
build queue and never sits on one slot while it waits for another.

It runs the moment no build is alive, ahead of the builds queued after it;
builds pass it until then; for the last ``[gc].hold_s`` of its wait nothing
queued after it starts, so a stream of builds cannot starve it; a wait that
runs out leaves the queue with nothing held; SIGKILL while waiting and while
holding; what the event log and ``--status`` say; and the other callers that
want a slot or the whole gate (``swarm gc --yes``, the landing's lane check).

Each build is the fake ``cargo`` of :mod:`test_build_queue`; gc is the real
:func:`gc.auto` on a thread (as in the supervisor) or, to be killed, the gate
half of it in a process of its own.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import pytest
from test_build_pair import PairGate
from test_build_queue import KEYS, Gate, _finish

from swarm_orchestrator import buildsem, buildstatus, gc

HOLDER = """\
import sys, time
from swarm_orchestrator import buildsem
from swarm_orchestrator.config import load
with buildsem.whole(load(project_dir=sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])):
    print("held", flush=True)
    time.sleep(float(sys.argv[4]))
"""


@pytest.fixture
def gate(tmp_path):
    made: list[Gate] = []

    def make(pair: bool = False, **kw) -> Gate:
        made.append(PairGate(tmp_path, **kw) if pair else Gate(tmp_path, **kw))
        return made[-1]

    yield make
    for g in made:
        g.cleanup()


def _cfg(g: Gate, wait_s: float, hold_s: float):
    cfg = g.cfg()
    cfg.gc_wait_s, cfg.gc_hold_s = wait_s, hold_s
    return cfg


def _auto(cfg) -> tuple[threading.Thread, dict]:
    """The interval fires: one automatic gc, on a thread as in the supervisor."""
    out: dict = {}

    def run() -> None:
        out["result"] = gc.auto(cfg)
        out["at"] = time.time()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, out


def _manual(cfg, wait_s: float) -> tuple[threading.Thread, dict]:
    """``swarm gc --yes``: plan, then apply under the gate."""
    out: dict = {}

    def run() -> None:
        try:
            gc.apply(gc.plan_gc(cfg, gc.GcOptions(yes=True, gate_timeout_s=wait_s)))
            out["result"] = gc.AutoResult(gc.AUTO_DONE)
        except gc.GcRefused as exc:
            out["result"] = gc.AutoResult(gc.AUTO_BUSY, detail=str(exc))
        out["at"] = time.time()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, out


def _gc_proc(g: Gate, wait_s: float, hold_s: float, sweep_s: float) -> subprocess.Popen:
    """A gc in a process of its own, so that it can be killed."""
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(g.proj), str(wait_s), str(hold_s),
                          str(sweep_s)], env=g.env, stdout=subprocess.PIPE, text=True)
    g.procs.append(p)
    return p


def _gc_events(g: Gate) -> list[dict]:
    return [e for e in g.events() if e["cls"] == "gc"]


def _wait_gc(g: Gate, kind: str, timeout: float = 15.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        for e in _gc_events(g):
            if e["event"] == kind:
                return e
        time.sleep(0.02)
    raise AssertionError(f"no gc {kind} event: {g.events()}")


def _dead_weight(cfg):
    """Something an automatic gc deletes: the temp dir of a phase long gone."""
    path = cfg.tmp_dir / "P-dead" / "f"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 4096)
    return path


def _long_build_on_slot_1(g: Gate, dur: float, pair: bool = False):
    """Slot 0 free, a long build on slot 1 (in repo ``a`` under the rules)."""
    first = g.at(g.b, "first", dur=1.2) if pair else g.start("first", dur=1.2)
    g.wait_event("start", "P-first")
    long = g.at(g.a, "long", dur=dur) if pair else g.start("long", dur=dur)
    assert g.wait_event("start", "P-long")["slot"] == 1
    first.wait(timeout=20)  # slot 0 is free again
    return long


def _two_chains(g: Gate) -> list[subprocess.Popen]:
    """Two builds that end half a second apart: with a queue behind them each
    slot is taken again while the other is still busy, so the gate is never
    empty by itself."""
    first = [g.start("f0", dur=1.0), g.start("f1", dur=1.5)]
    for n in range(2):
        g.wait_event("start", f"P-f{n}")
    return first


def _queue_files(g: Gate) -> list[str]:
    qdir = g.sem / "queue"
    return sorted(p.name for p in qdir.iterdir()) if qdir.is_dir() else []


def _status(g: Gate) -> tuple[dict, str]:
    snap = buildstatus.snapshot(g.cfg())
    return snap, buildstatus.render(snap)


# -- the bug: gc sat on slot 0 while it waited for slot 1 ----------------------
@pytest.mark.parametrize("pair", [False, True], ids=["pair-any", "distinct-repo"])
@pytest.mark.parametrize("how", ["auto", "manual"])
def test_a_gc_waiting_for_a_long_build_leaves_the_free_slot_to_builds(gate, pair, how):
    """Two slots, a long build on slot 1, and the gc interval fires (or the
    owner types ``swarm gc --yes``). gc cannot run until that build ends;
    until then slot 0 belongs to the builds."""
    g = gate(pair=pair, max_concurrent=2)
    long = _long_build_on_slot_1(g, dur=7.0, pair=pair)
    cfg = _cfg(g, wait_s=3, hold_s=1)
    th, out = _auto(cfg) if how == "auto" else _manual(cfg, 3)
    time.sleep(0.6)  # gc is waiting for the gate
    b = g.at(g.c, "b", dur=0.2) if pair else g.start("b", dur=0.2)
    b.wait(timeout=20)
    start = g.wait_event("start", "P-b")
    th.join(20)
    assert start["slot"] == 0 and start["wait_s"] < 1.0  # not behind gc's whole wait
    assert start["ts"] < out["at"] - 1.0  # it ran while gc was still waiting
    assert out["result"].outcome == gc.AUTO_BUSY
    _finish([long])


# -- gc as a waiter that runs alone ---------------------------------------------
@pytest.mark.parametrize("pair", [False, True], ids=["pair-any", "distinct-repo"])
def test_gc_runs_when_the_gate_empties_ahead_of_the_builds_queued_after_it(gate, pair):
    g = gate(pair=pair, max_concurrent=2)
    cfg = _cfg(g, wait_s=20, hold_s=0)  # it never holds a build back: none of this needs it
    dead = _dead_weight(cfg)
    a = g.at(g.a, "a", dur=2.0) if pair else g.start("a", dur=2.0)
    g.wait_event("start", "P-a")
    th, out = _auto(cfg)
    queued = _wait_gc(g, "queued")
    # under the rules `b` may not start beside `a` (same repo), so it waits behind gc
    b = g.at(g.a, "b", dur=0.3) if pair else None
    if b:
        g.wait_event("queued", "P-b")
    th.join(20)
    assert out["result"].outcome == gc.AUTO_DONE and not dead.exists()
    late = g.at(g.c, "late", dur=0.1) if pair else g.start("late", dur=0.1)
    _finish([a, late] + ([b] if b else []))
    start, end = _wait_gc(g, "start"), _wait_gc(g, "end")
    assert start["ts"] >= g.wait_event("end", "P-a")["ts"] - 0.05  # no build was alive
    assert start["wait_s"] > 1.0 and end["exit"] == 0 and end["run_s"] >= 0
    if b:  # the gate was empty and gc was older: gc first, then the build
        assert g.wait_event("start", "P-b")["ts"] >= end["ts"] - 0.05
    events = _gc_events(g)
    assert [e["event"] for e in events] == ["queued", "start", "end"]
    assert all(set(e) == KEYS for e in events)
    assert {e["id"] for e in events} == {queued["id"]}
    for e in events:
        assert (e["pid"], e["slot"], e["phase"], e["argv"]) == (os.getpid(), None, None,
                                                                "swarm gc")
    assert (queued["alone"], queued["why"]) == (True, buildsem.GC_WHY)
    assert _queue_files(g) == []


def test_an_empty_gate_is_taken_at_once(gate):
    g = gate(max_concurrent=2)
    cfg = _cfg(g, wait_s=0, hold_s=0)  # wait_s 0: only a gate that is empty right now
    dead = _dead_weight(cfg)
    t0 = time.time()
    result = gc.auto(cfg)
    assert result.outcome == gc.AUTO_DONE and not dead.exists() and time.time() - t0 < 5
    assert _wait_gc(g, "start")["wait_s"] < 1.0


def test_gc_is_not_starved_by_a_stream_of_builds(gate):
    """Two slots and more builds than they can take: the gate is never empty by
    itself. Builds pass gc while it waits, and for the end of its wait none
    queued after it starts, so the two that are running end and gc runs."""
    g = gate(max_concurrent=2)
    cfg = _cfg(g, wait_s=8, hold_s=5)  # passed for 3s, then no more
    dead = _dead_weight(cfg)
    first = _two_chains(g)
    th, out = _auto(cfg)
    queued = _wait_gc(g, "queued")
    stream = [g.start(f"s{n}", dur=1.0) for n in range(14)]  # ~7s of work for two slots
    th.join(30)
    assert out["result"].outcome == gc.AUTO_DONE and not dead.exists()
    _finish(first + stream, timeout=90)
    start, end = _wait_gc(g, "start"), _wait_gc(g, "end")
    builds = {}
    for e in g.events():
        if e["cls"] == "heavy" and e["event"] in ("start", "end"):
            builds.setdefault(e["phase"], {})[e["event"]] = e["ts"]
    assert len(builds) == 16 and all(len(b) == 2 for b in builds.values())
    firm = queued["ts"] + 3
    passed = [p for p, b in builds.items() if queued["ts"] < b["start"] < firm]
    after = [p for p, b in builds.items() if b["start"] >= end["ts"] - 0.05]
    assert passed, "builds pass a gc that is waiting"
    assert after, "gc ran while builds were still waiting, not when they ran out"
    assert firm - 0.1 <= start["ts"] < firm + 3.0  # once it was passed no more, and soon
    for phase, b in builds.items():  # no build was alive while gc held the gate
        assert b["end"] <= start["ts"] + 0.05 or b["start"] >= end["ts"] - 0.05, phase


def test_with_hold_s_0_gc_holds_nobody_back_and_takes_only_an_empty_gate(gate):
    g = gate(max_concurrent=2)
    cfg = _cfg(g, wait_s=3, hold_s=0)
    first = _two_chains(g)
    th, out = _auto(cfg)
    _wait_gc(g, "queued")
    stream = [g.start(f"s{n}", dur=1.0) for n in range(10)]
    th.join(30)
    assert out["result"].outcome == gc.AUTO_BUSY
    _finish(first + stream, timeout=90)
    waits = [g.wait_event("start", f"P-s{n}")["wait_s"] for n in range(10)]
    assert max(waits) < 7.5  # ten 1s builds on two slots: nobody waited for gc


def test_gc_leaves_the_queue_cleanly_when_its_wait_runs_out(gate):
    g = gate(max_concurrent=2)
    long = _long_build_on_slot_1(g, dur=9.0)
    cfg = _cfg(g, wait_s=4, hold_s=3)  # passed for 1s, then holds for 3s
    dead = _dead_weight(cfg)
    th, out = _auto(cfg)
    queued = _wait_gc(g, "queued")
    snap, text = _status(g)
    [entry] = snap["gc"]
    assert entry["state"] == "waiting" and not entry["holding"] and entry["pid"] == os.getpid()
    assert "gc: waiting" in text and "to run alone; builds pass it for another" in text
    time.sleep(max(0.0, queued["ts"] + 1.3 - time.time()))  # gc is passed no more
    b = g.start("b", dur=0.2)
    g.wait_event("queued", "P-b")
    time.sleep(0.6)
    snap, text = _status(g)
    assert snap["gc"][0]["holding"] and "no build starts until it has run" in text
    assert [q["blocked"] for q in snap["queue"]] == [buildsem.GC_FIRST]
    assert not [e for e in g.events() if e["phase"] == "P-b" and e["event"] == "start"]
    th.join(20)
    assert out["result"].outcome == gc.AUTO_BUSY and dead.exists()  # nothing was deleted
    assert "not empty within 4s" in out["result"].detail and "P-long" in out["result"].detail
    assert "nothing was held while waiting" in out["result"].detail
    b.wait(timeout=20)  # the wait is over: the build goes on, the long one still running
    assert g.wait_event("start", "P-b")["ts"] < g.wait_event("end", "P-long", timeout=30)["ts"]
    assert g.wait_event("start", "P-b")["ts"] >= out["at"] - 0.3
    events = _gc_events(g)
    assert [e["event"] for e in events] == ["queued", "left"]
    left = events[1]
    assert set(left) == KEYS and 3.9 < left["wait_s"] < 6 and "P-long" in left["why"]
    assert _queue_files(g) == []
    snap, text = _status(g)
    assert snap["gc"] == [] and "gc waited" in text and "it held nothing" in text
    assert "held back: gc runs first" in b.stderr.read()
    _finish([long])


# -- SIGKILL -------------------------------------------------------------------
def test_a_gc_killed_while_it_waits_holds_nobody_back(gate):
    g = gate(max_concurrent=2)
    long = _long_build_on_slot_1(g, dur=8.0)
    proc = _gc_proc(g, wait_s=60, hold_s=60, sweep_s=0)  # passed by nobody, from the start
    _wait_gc(g, "queued")
    b = g.start("b", dur=0.2)
    g.wait_event("queued", "P-b")
    time.sleep(1.0)
    assert not [e for e in g.events() if e["phase"] == "P-b" and e["event"] == "start"]
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    killed = time.time()
    b.wait(timeout=20)
    start = g.wait_event("start", "P-b")
    assert start["ts"] - killed < 2.0 and start["slot"] == 0
    assert start["ts"] < g.wait_event("end", "P-long", timeout=30)["ts"]
    assert _queue_files(g) == []  # its ticket went with it
    assert [e["event"] for e in _gc_events(g)] == ["queued"]
    _finish([long])


def test_a_gc_killed_while_it_holds_the_gate_frees_it(gate):
    g = gate(max_concurrent=2)
    proc = _gc_proc(g, wait_s=10, hold_s=0, sweep_s=60)
    assert proc.stdout.readline().strip() == "held"
    snap, text = _status(g)
    assert [h.get("gc") for h in snap["slots"]] == [True, True]
    assert all(h["pid"] == proc.pid and not h.get("unknown") for h in snap["slots"])
    assert snap["gc"] == [{"state": "running", "id": snap["slots"][0]["id"], "swarm": "state",
                           "swarm_name": "proj", "mine": True, "pid": proc.pid,
                           "running_s": snap["slots"][0]["running_s"]}]
    assert text.count("gc running") == 2 and "no record" not in text
    assert buildstatus.summary_line(g.cfg()).startswith("build gate: 2/2 busy on this machine (gc ")
    b = g.start("b", dur=0.2)
    g.wait_event("queued", "P-b")
    time.sleep(1.0)
    assert not g.lines()  # nothing runs beside gc
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=10)
    killed = time.time()
    b.wait(timeout=20)
    assert g.wait_event("start", "P-b")["ts"] - killed < 2.0
    assert "gc running" in b.stderr.read()  # what the waiting build was told
    kinds = [(e["event"], e["exit"]) for e in _gc_events(g)]
    assert kinds == [("queued", None), ("start", None), ("end", None)]  # the end is synthetic
    snap, text = _status(g)
    assert snap["gc"] == [] and not any(h["busy"] for h in snap["slots"])
    assert "killed, unrecorded `swarm gc`" in text


# -- its safety is what it was ----------------------------------------------------
def test_gc_never_runs_beside_a_process_a_build_left_behind(gate):
    """The build ended, a process it started still holds its seat and slot. gc
    must not run; and since nobody knows when that process will end, gc holds
    no build back for it either."""
    g = gate(max_concurrent=2)
    left = g.start("left", "sh", "-c", "sleep 4 & cargo build", dur=0.2)
    g.wait_event("end", "P-left")
    cfg = _cfg(g, wait_s=2, hold_s=2)  # passed by nobody, if that could help
    dead = _dead_weight(cfg)
    th, out = _auto(cfg)
    _wait_gc(g, "queued")
    b = g.start("b", dur=0.2)
    b.wait(timeout=20)
    assert g.wait_event("start", "P-b")["wait_s"] < 1.0
    th.join(20)
    assert out["result"].outcome == gc.AUTO_BUSY and dead.exists()
    _finish([left])
    cfg.gc_wait_s = 15  # the process ends: now it runs
    assert gc.auto(cfg).outcome == gc.AUTO_DONE and not dead.exists()


def test_gc_view_is_a_wall_only_when_holding_helps():
    now = 1000.0
    gc_t = {"id": "g", "seq": 2, "gc": True, "alone": buildsem.GC_WHY, "firm_ts": now + 10}
    older, younger = {"id": "o", "seq": 1}, {"id": "y", "seq": 3, "pred_s": 1.0}
    tickets = [older, gc_t, younger]

    def view(at: float, **kw) -> tuple[list[str], str | None]:
        got, wall = buildsem.gc_view(tickets, at, **kw)
        return [t["id"] for t in got], wall

    # a busy gate, gc still being passed: it is not there at all
    assert view(now, busy=True, working=True) == (["o", "y"], None)
    # past its firm time, builds at work on the gate: the wall
    assert view(now + 10, busy=True, working=True) == (["o", "g", "y"], "g")
    # past it, but something on the gate is not a build at work: passed again
    assert view(now + 10, busy=True, working=False) == (["o", "y"], None)
    # an empty gate: the wall whatever the clock says
    assert view(now, busy=False, working=False) == (["o", "g", "y"], "g")
    # a gc that stopped polling is nobody's wall
    stale = [older, dict(gc_t, fresh=False), younger]
    assert buildsem.gc_view(stale, now, busy=False, working=False)[1] is None
    # select: the oldest goes first; behind the wall nothing does, short or not
    assert buildsem.behind(tickets, "g") == {"y"}
    assert buildsem.select(tickets, {}, 2, 30, None, "g")["id"] == "o"
    assert buildsem.select(tickets[1:], {}, 2, 30, None, "g")["id"] == "g"
    assert buildsem.select(tickets[1:], {}, 2, 30, {}, "g")["id"] == "g"
    assert buildsem.select(tickets[1:], {}, 2, 30, None, None)["id"] == "y"  # what it stops
    order = buildsem.service_order(tickets, {}, 2, 30, None, "g")
    assert [t["id"] for t in order] == ["o", "g", "y"]


# -- the other callers that queue --------------------------------------------------
def test_a_lane_check_waiting_for_its_turn_holds_no_slot(gate):
    """``buildsem.slot`` (the landing's lane check) waits as a ticket too: while
    it waits for the build in its repo to end, the other slot stays in use."""
    g = gate(pair=True, max_concurrent=2)
    first = g.at(g.a, "a", dur=3.0)
    g.wait_event("start", "P-a")
    done = {}

    def check() -> None:
        with buildsem.slot(g.cfg(), "cargo test", g.a, "P-check") as held:
            done["at"] = time.time()
            held.exit = 0

    th = threading.Thread(target=check, daemon=True)
    th.start()
    g.wait_event("queued", "P-check")
    other = g.at(g.b, "b", dur=0.2)
    other.wait(timeout=20)
    assert g.wait_event("start", "P-b")["wait_s"] < 1.0 and "at" not in done
    th.join(20)
    _finish([first])
    assert done["at"] >= g.wait_event("end", "P-a")["ts"] - 0.05
