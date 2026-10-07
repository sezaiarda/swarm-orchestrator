"""One build gate for the machine, with real processes of two swarms side by
side: the limit holds between them; arrivals are served in order whoever they
belong to, and one swarm's short builds pass another's long one only as often
as the queue allows; ``--status`` and the waiting line say whose build is whose;
the pairing rules know a repository by its place, across swarms; gc waits for a
neighbour's build; a dead neighbour's seat is free and its ``end`` written; a
frozen neighbour's build keeps its seat and stops counting, and its waiter is
passed over and keeps its place. And where the limits live: ``machine.toml``,
read live, never a project's file and never a variable.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest
from test_build_idle import CARGO
from test_build_pair import PairGate
from test_build_queue import KEYS, Gate, _descendants, _finish, _max_overlap, _starts

from swarm_orchestrator import buildidle, buildlog, buildsem, buildstatus, freezer, machine
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load


@pytest.fixture
def box(tmp_path):
    """``box(**limits)``: two swarms, ``alpha`` and ``beta``, on one machine.
    Their state dirs are side by side, so they share the machine directory and
    the gate in it; their builds write one log."""
    made: list[Gate] = []

    def make(kind=Gate, **limits) -> tuple[Gate, Gate]:
        root = tmp_path / "root"
        pair = tuple(kind(tmp_path, name=name, state=root / name, **limits)
                     for name in ("alpha", "beta"))
        made.extend(pair)
        assert pair[0].sem == pair[1].sem == root / "machine" / "buildsem"
        return pair

    yield make
    for g in made:
        g.cleanup()


def _swarms(g: Gate) -> dict[str, set[str]]:
    """``{phase: the swarms its events name}``."""
    out: dict[str, set[str]] = {}
    for e in g.events():
        out.setdefault(e["phase"], set()).add(e["swarm"])
    return out


# -- one limit -----------------------------------------------------------------
def test_two_swarms_cannot_exceed_the_machines_limit_between_them(box):
    a, b = box(max_concurrent=1)
    procs = [a.start("a1", dur=0.8)]
    a.wait_event("start", "P-a1")
    procs += [b.start("b1", dur=0.3), a.start("a2", dur=0.3), b.start("b2", dur=0.3)]
    _finish(procs)
    assert _max_overlap(a.lines()) == 1 and len(_starts(a.lines())) == 4
    # one log for the machine, and every line says whose build it was
    assert a.events() == b.events() and all(set(e) == KEYS for e in a.events())
    assert _swarms(a) == {"P-a1": {"alpha"}, "P-a2": {"alpha"}, "P-b1": {"beta"},
                          "P-b2": {"beta"}}
    assert {e["swarm_name"] for e in a.events()} == {"alpha", "beta"}
    assert not (a.state / "buildsem").exists() and not (b.state / "buildsem").exists()


def test_two_slots_are_two_builds_for_the_machine_not_two_each(box):
    a, b = box(max_concurrent=2)
    procs = [a.start("a1", dur=0.8), a.start("a2", dur=0.8)]
    a.wait_event("start", "P-a1")
    a.wait_event("start", "P-a2")
    procs += [b.start("b1", dur=0.4), b.start("b2", dur=0.4)]
    b.wait_event("queued", "P-b1")
    time.sleep(0.4)
    assert _starts(a.lines()) == ["a1", "a2"] or _starts(a.lines()) == ["a2", "a1"]
    _finish(procs)
    assert _max_overlap(a.lines()) == 2


# -- first come, whoever comes --------------------------------------------------
def test_two_swarms_alternating_are_served_in_the_order_they_came(box):
    a, b = box(max_concurrent=1, overtake=0)
    procs = [a.start("a0", dur=3.0)]  # long enough for all five to be waiting
    a.wait_event("start", "P-a0")
    for g, tag in ((a, "a1"), (b, "b1"), (a, "a2"), (b, "b2"), (a, "a3")):
        procs.append(g.start(tag, dur=0.1))
        g.wait_event("queued", f"P-{tag}")
    _finish(procs)
    assert _starts(a.lines()) == ["a0", "a1", "b1", "a2", "b2", "a3"]


def test_a_swarm_with_many_short_builds_cannot_keep_anothers_waiting(box):
    """``overtake`` is the queue's, not a swarm's: alpha's short builds pass
    beta's long one twice, and then it starts, however many more alpha queues."""
    a, b = box(max_concurrent=1, overtake=2)
    for n in range(2):  # alpha's build is known to be short; beta's has no history
        a.start(f"warm{n}", dur=0.05).wait(timeout=20)
    hist = buildlog.History(a.cfg())
    assert hist.predict(["cargo", "build"], str(a.proj)) is not None
    assert buildlog.History(b.cfg()).predict(["cargo", "build"], str(b.proj)) is None
    procs = [a.start("hold", dur=4.0)]  # long enough for all six to be waiting
    a.wait_event("start", "P-hold")
    procs.append(b.start("long", dur=0.1))
    b.wait_event("queued", "P-long")
    for n in range(5):
        procs.append(a.start(f"s{n}", dur=0.1))
        a.wait_event("queued", f"P-s{n}")
    _finish(procs)
    order = [t for t in _starts(a.lines()) if not t.startswith("warm")]
    assert order == ["hold", "s0", "s1", "long", "s2", "s3", "s4"]


def test_the_queue_knows_no_swarm():
    """The model: the same tickets are served in the same order whichever
    swarm each belongs to."""
    def queue(owners: str) -> list[dict]:
        return [{"id": f"t{n}", "seq": n, "pred_s": 900 if n % 3 == 0 else 5,
                 "swarm": owner} for n, owner in enumerate(owners)]

    orders = {tuple(t["id"] for t in buildsem.service_order(queue(owners), {}, 2, 60))
              for owners in ("aaaaaaa", "abababa", "abbbbbb", "bbbaaab")}
    assert len(orders) == 1


# -- whose build ----------------------------------------------------------------
def test_status_and_the_waiting_line_name_the_other_swarm(box):
    a, b = box(max_concurrent=1)
    holder = a.start("a1", dur=3.0)
    a.wait_event("start", "P-a1")
    first = a.start("a2", dur=0.1)
    a.wait_event("queued", "P-a2")
    waiter = b.start("b1", dur=0.1)
    b.wait_event("queued", "P-b1")
    time.sleep(0.6)

    snap = buildstatus.snapshot(b.cfg())  # asked from beta
    text = buildstatus.render(snap)
    assert (snap["swarm"], snap["swarm_name"]) == ("beta", "beta")
    slot = snap["slots"][0]
    assert (slot["swarm"], slot["swarm_name"], slot["mine"], slot["phase"]) == (
        "alpha", "alpha", False, "P-a1")
    assert [(q["swarm"], q["mine"], q["phase"]) for q in snap["queue"]] == [
        ("alpha", False, "P-a2"), ("beta", True, "P-b1")]
    assert "build gate: 1 slot(s) on this machine, 1 busy, 2 waiting" in text
    assert "slot 0: [alpha] P-a1 `cargo build` running" in text
    assert "1. [alpha] P-a2 `cargo build`" in text and "2. P-b1 `cargo build`" in text
    line = buildstatus.summary_line(b.cfg())
    assert line.startswith("build gate: 1/1 busy on this machine ([alpha] P-a1 `cargo build`")

    mine = buildstatus.render(buildstatus.snapshot(a.cfg()))  # and from alpha
    assert "slot 0: P-a1 `cargo build` running" in mine
    assert "1. P-a2 `cargo build`" in mine and "2. [beta] P-b1 `cargo build`" in mine

    _finish([holder, first, waiter])
    said = waiter.stderr.read()
    assert "#2 of 2 for 1 slot(s) on this machine, behind [alpha] P-a2 `cargo build`;" in said
    assert "slot 0: [alpha] P-a1 `cargo build` running" in said
    assert "[beta]" not in said  # its own swarm is not named to it
    recent = buildstatus.snapshot(b.cfg())["recent"]
    assert {(r["swarm"], r["mine"]) for r in recent} == {("alpha", False), ("beta", True)}


# -- the pairing rules, across swarms -------------------------------------------
def test_two_swarms_with_one_checkout_never_build_in_it_side_by_side(box, tmp_path):
    """Alpha's project holds the repo ``a``; a second swarm's project *is* that
    repo. One repository, known by its place: with two slots free, the second
    build in it still queues, and a build elsewhere starts beside the first."""
    a, b = box(kind=PairGate, max_concurrent=2)
    (a.a / ".swarm.toml").touch()  # the component is a project of its own, too
    inner = dict(SWARM_PROJECT=str(a.a), SWARM_STATE_DIR=str(a.state.parent / "inner"))
    first = a.at(a.a, "umbrella", dur=1.5)  # alpha, in its repo `a`
    a.wait_event("start", "P-umbrella")
    same = a.at(a.a, "inner", dur=0.2, env=inner)  # the other swarm, in its own repo `.`
    a.wait_event("queued", "P-inner")
    other = b.at(b.b, "elsewhere", dur=0.3)  # a third swarm, in a repo of its own
    b.wait_event("start", "P-elsewhere")
    assert not a.started("inner")
    snap = buildstatus.snapshot(a.cfg())
    assert snap["queue"][0]["swarm_name"] == "a"
    assert snap["queue"][0]["blocked"] == "same repo as slot 0 (the project's own repo)"
    _finish([first, same, other])
    lines = a.lines()
    assert _max_overlap(lines) == 2
    assert _max_overlap([x for x in lines if x.split()[1] in ("umbrella", "inner")]) == 1
    assert a.ts("start", "inner") >= a.ts("end", "umbrella") - 0.05
    assert "held back: same repo as slot 0 (the project's own repo)" in same.stderr.read()
    # both called it by their own name, and the gate knew it for one place
    starts = {e["phase"]: e for e in a.events() if e["event"] == "start"}
    assert (starts["P-umbrella"]["repo"], starts["P-inner"]["repo"]) == ("a", ".")
    assert (starts["P-umbrella"]["swarm"], starts["P-inner"]["swarm"]) == ("alpha", "inner")


def test_two_projects_are_not_one_repo_because_each_calls_its_own_dot(box):
    a, b = box(kind=PairGate, max_concurrent=2)
    procs = [a.at(a.proj, "a-root", dur=1.0), b.at(b.proj, "b-root", dur=1.0)]
    _finish(procs)
    assert _max_overlap(a.lines()) == 2  # side by side: two places
    starts = [e for e in a.events() if e["event"] == "start"]
    assert [e["repo"] for e in starts] == [".", "."]
    again = [a.at(a.proj, "a1", dur=0.8), a.at(a.proj, "a2", dur=0.2)]
    _finish(again)
    assert _max_overlap([x for x in a.lines() if x.split()[1] in ("a1", "a2")]) == 1


def test_a_build_that_runs_alone_waits_for_every_swarms_builds(box):
    a, b = box(kind=PairGate, max_concurrent=2)
    build = a.at(a.a, "a", dur=1.2)
    a.wait_event("start", "P-a")
    image = b.at(b.b, "img", "docker", "build", ".", dur=0.3)
    b.wait_event("queued", "P-img")
    time.sleep(0.4)
    assert not b.started("img")
    _finish([build, image])
    assert a.ts("start", "img") >= a.ts("end", "a") - 0.05
    assert "held back: waits to run alone (`docker build` is in [build].alone)" in \
        image.stderr.read()


# -- gc ----------------------------------------------------------------------------
def test_gc_waits_for_a_neighbours_build_and_holds_the_machines_gate(box):
    a, b = box(max_concurrent=2)
    build = a.start("a1", dur=1.5)
    started = a.wait_event("start", "P-a1")["ts"]
    with pytest.raises(buildsem.Busy, match=r"\[alpha\] P-a1 `cargo build` on slot 0"):
        with buildsem.whole(b.cfg(), wait_s=0.3):
            raise AssertionError("gc ran beside a neighbour's build")
    with buildsem.whole(b.cfg(), wait_s=20, hold_s=0):  # beta's gc: it takes its turn
        assert "end a1" in a.lines() and time.time() >= started + 1.4
        late = a.start("a2", dur=0.1)  # and no build of any swarm starts under it
        a.wait_event("queued", "P-a2")
        time.sleep(0.5)
        assert "start a2" not in a.lines()
        snap = buildstatus.snapshot(a.cfg())
        assert [(h.get("gc"), h["swarm"], h["mine"]) for h in snap["slots"]] == [
            (True, "beta", False)] * 2
        assert "slot 0: [beta] gc running" in buildstatus.render(snap)
    _finish([build, late])
    gc = [e for e in a.events() if e["cls"] == "gc"]
    assert {e["swarm"] for e in gc} == {"beta"} and [e["event"] for e in gc] == [
        "queued", "left", "queued", "start", "end"]


def _gc_turns(cfg, until: float, out: list) -> threading.Thread:
    """A swarm's automatic gc, its interval cut to nothing: it queues, waits its
    turn, leaves when the wait is over and queues again, until it runs."""
    def run() -> None:
        while time.time() < until:
            try:
                with buildsem.whole(cfg, wait_s=1.5, hold_s=1.0):
                    out.append(("ran", time.time()))
                    return
            except buildsem.Busy as exc:
                out.append(("busy", str(exc)))

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def test_processes_builds_left_behind_never_fill_the_gate_while_two_gcs_wait(box):
    """2026-10-07, 03:23 to 04:35: three builds had each ended and left a process
    on its seat (a failing test's children), which were all the seats of
    ``max_concurrent = 2`` plus ``idle_yield_max = 1``; the two swarms' gcs took
    turns waiting for an empty gate, and no build of either swarm started for 72
    minutes. A process a build left behind is not a build: it takes neither a
    slot nor one of the seats, however many there are, so the builds go on; gc
    still waits for it (it may be using build output), and runs once it is gone."""
    a, b = box(kind=PairGate, max_concurrent=2)
    a.machine(idle_yield_s=300, idle_yield_max=1)  # the live limits: three seats
    assert buildsem.seats(a.cfg()) == 3
    left_s = 8.0
    for g, where, tag in ((a, a.a, "l1"), (b, b.a, "l2"), (a, a.b, "l3"), (b, b.b, "l4")):
        g.at(where, tag, "sh", "-c", f"sleep {left_s} & cargo build", dur=0.1)
        assert g.wait_event("start", f"P-{tag}")["wait_s"] < 1.5  # beside every leftover
        g.wait_event("end", f"P-{tag}")
    assert len(buildsem.live_holders(a.cfg())) == 4  # more than there are seats
    until = time.time() + left_s + 15
    turns = {"alpha": [], "beta": []}
    gcs = [_gc_turns(a.cfg(), until, turns["alpha"]), _gc_turns(b.cfg(), until, turns["beta"])]
    procs = []
    for g, where, tag in ((a, a.c, "a1"), (b, b.c, "b1"), (a, a.a, "a2"), (b, b.a, "b2")):
        procs.append(g.at(where, tag, dur=0.6))
        time.sleep(0.3)
    _finish(procs)
    for tag in ("a1", "b1", "a2", "b2"):
        assert a.wait_event("start", f"P-{tag}")["wait_s"] < 1.5
    assert _max_overlap(a.lines()) <= 2
    snap = buildstatus.snapshot(a.cfg())
    assert not any(s["busy"] for s in snap["slots"])  # what is left there is no build
    for th in gcs:
        th.join(until - time.time() + 5)
    for swarm, out in turns.items():
        assert out and out[-1][0] == "ran", (swarm, out)  # each gc ran once they were gone
        assert all("left behind" in why for kind, why in out if kind == "busy"), out
        assert out[-1][1] >= a.wait_event("start", "P-l4")["ts"] + left_s - 0.5


# -- a holder that is gone ---------------------------------------------------------
def test_a_dead_neighbours_seat_is_free_and_its_end_is_written_by_whoever_notices(box):
    a, b = box(max_concurrent=1)
    holder = a.start("a1", dur=60)
    build_pid = a.wait_event("start", "P-a1")["pid"]
    waiter = b.start("b1", dur=0.2)
    b.wait_event("queued", "P-b1")
    for pid in [holder.pid, build_pid, *_descendants(build_pid)]:
        try:
            os.kill(pid, signal.SIGKILL)  # the whole build, its `swarm build` too
        except OSError:
            pass
    waiter.wait(timeout=30)
    assert waiter.returncode == 0 and "start b1" in a.lines()
    end = a.wait_event("end", "P-a1")  # written by beta's waiter, in alpha's name
    assert (end["exit"], end["swarm"], end["swarm_name"]) == (None, "alpha", "alpha")
    assert buildstatus.snapshot(b.cfg())["builds"] == []


# -- a holder that stands frozen ---------------------------------------------------
def _freeze(g: Gate) -> None:
    """``swarm freeze``, as the gate sees it: the record in the swarm's state.
    (The holder below sleeps, which is all a frozen process does.)"""
    cfg = g.cfg()
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.frozen = {"since": time.time(), "stage": freezer.FROZEN, "quiet_at": 0.0,
                     "waiting": [], "quiesced": True, "cgroups": [], "awake": []}


def _thaw(g: Gate) -> None:
    cfg = g.cfg()
    with state_mod.transaction(cfg) as st:
        since, st.frozen = st.frozen["since"], {}
    freezer.close_span(cfg, since, time.time())


def _planned(g: Gate) -> None:
    (g.bin / "cargo").write_text(CARGO.format(python=sys.executable, log=str(g.log)))


def test_a_frozen_neighbours_build_keeps_its_seat_and_stops_counting(box):
    """Nothing takes a seat from a build that will resume, and a swarm that
    stands still does not keep the machine waiting: its holder is set aside at
    once, well inside the window an idle holder is given."""
    a, b = box(max_concurrent=1)
    _planned(a)
    a.machine(idle_yield_s=300, idle_yield_max=1)
    holder = a.start("a1", env={"PLAN": "sleep:12"})
    start = a.wait_event("start", "P-a1")
    _freeze(a)
    waiter = b.start("b1", env={"PLAN": "sleep:0.3"})
    b.wait_event("start", "P-b1", timeout=25)  # beside it: the window is five minutes
    assert holder.poll() is None
    gone = a.wait_event("yield", "P-a1")
    assert (gone["why"], gone["swarm"], gone["id"]) == (buildidle.FROZEN_WHY, "alpha", start["id"])
    snap = buildstatus.snapshot(b.cfg())
    [held] = [x for x in snap["builds"] if x["phase"] == "P-a1"]
    assert (held["state"], held["frozen"], held["swarm"], held["seat"]) == (
        "yielded", True, "alpha", 0)  # set aside, and on its seat still
    assert "[alpha] P-a1" in buildstatus.render(snap) and "frozen with its swarm" in \
        buildstatus.render(snap)
    waiter.wait(timeout=30)
    _thaw(a)
    assert not buildlog.frozen(b.cfg(), held)
    holder.wait(timeout=40)
    assert holder.returncode == 0  # it ran to its end on the seat it never lost
    ends = [e for e in a.events() if e["event"] == "end" and e["phase"] == "P-a1"]
    assert [e["exit"] for e in ends] == [0]


def test_a_frozen_build_that_may_not_be_set_aside_keeps_its_slot_and_is_named(box):
    a, b = box(max_concurrent=1)
    _planned(a)
    a.machine(idle_yield_s=300, idle_yield_max=0)  # nothing is ever set aside here
    holder = a.start("a1", env={"PLAN": "sleep:5"})
    a.wait_event("start", "P-a1")
    _freeze(a)
    waiter = b.start("b1", env={"PLAN": "sleep:0.1"})
    b.wait_event("queued", "P-b1")
    time.sleep(2.0)
    assert "start b1" not in a.lines()
    text = buildstatus.render(buildstatus.snapshot(b.cfg()))
    assert "slot 0: [alpha] P-a1 `cargo build` running" in text and "frozen with its swarm" in text
    _finish([holder, waiter])
    assert _max_overlap(a.lines()) == 1


def test_a_frozen_neighbours_waiter_is_passed_over_and_keeps_its_place(box):
    a, b = box(max_concurrent=1)
    now = time.time()
    refreshed = now - 3600
    theirs, mine = {"swarm": "alpha"}, {"swarm": "beta"}
    cfg = b.cfg()
    state_mod.init_state(cfg)
    assert not buildsem._polling(cfg, theirs, refreshed, now)  # an hour unpolled: stale
    _freeze(a)
    with state_mod.transaction(a.cfg()) as st:
        st.frozen["since"] = refreshed + 2
    # Frozen two seconds after its last poll, and still: beta does not wait for
    # it. (Alpha's own waiters do: inside a frozen swarm the order stands still.)
    assert not buildsem._polling(cfg, theirs, refreshed, now)
    assert buildsem._polling(a.cfg(), theirs, refreshed, now)
    with state_mod.transaction(a.cfg()) as st:
        st.frozen = {}
    freezer.close_span(a.cfg(), refreshed + 2, now - 5)  # thawed five seconds ago
    assert buildsem._polling(cfg, theirs, refreshed, now)  # it has its place back
    assert not buildsem._polling(cfg, theirs, refreshed, now + 60)
    assert not buildsem._polling(cfg, {}, refreshed, now)  # a ticket that names nobody
    assert not buildsem._polling(cfg, mine, refreshed, now)

    # With real waiters: alpha's is stopped where it queued, as a freeze stops
    # it. Beta's builds pass it, and it starts when it is woken.
    hold = b.start("b0", dur=1.0)
    b.wait_event("start", "P-b0")
    stuck = a.start("a1", dur=0.1)
    a.wait_event("queued", "P-a1")
    stuck.send_signal(signal.SIGSTOP)
    try:
        _freeze(a)
        with state_mod.transaction(a.cfg()) as st:
            st.frozen["since"] = refreshed + 2  # frozen all but two seconds of that hour
        ticket = next((a.sem / "queue").glob("*.json"))
        os.utime(ticket, (refreshed, refreshed))  # not polled for longer than a waiter may be
        later = b.start("b1", dur=0.1)
        later.wait(timeout=30)
        assert _starts(a.lines()) == ["b0", "b1"] and ticket.exists()
    finally:
        _thaw(a)
        stuck.send_signal(signal.SIGCONT)
    _finish([hold, stuck])
    assert _starts(a.lines()) == ["b0", "b1", "a1"]


# -- where the limits live ---------------------------------------------------------
def test_a_moved_key_in_a_project_file_is_refused_and_names_the_machine_file(box):
    a, _ = box()
    (a.proj / ".swarm.toml").write_text(
        '[build]\njobs = 4\nmax_concurrent = 1\npair = "distinct-repo"\n')
    with pytest.raises(ValueError) as exc:
        a.cfg()
    said = str(exc.value)
    assert "[build].max_concurrent, [build].pair are not a project's to set" in said
    assert str(a.proj / ".swarm.toml") in said and str(machine.settings_path()) in said
    assert "[build].jobs" not in said  # that one is the project's, and stays
    p = a.start("a1")
    _, err = p.communicate(timeout=20)
    assert p.returncode == 2 and "config error" in err and "machine.toml" in err
    assert a.lines() == []  # nothing ran on a limit nobody set
    (a.proj / ".swarm.toml").write_text("[build]\njobs = 4\n")
    assert a.cfg().build_jobs == 4


def test_a_sessions_build_under_a_project_file_that_no_longer_loads_says_so_and_queues(box):
    """A worker of a swarm that is running when its project file is found to
    hold a moved key: its ``swarm build`` is not lost. It says what is wrong,
    runs on what the swarm recorded, and queues under the machine's limit, never
    the one the file names."""
    a, b = box(max_concurrent=1)
    cfg = a.cfg()
    cfg.ensure_dirs()
    record = {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(cfg).items()}
    (a.state / "config.json").write_text(json.dumps(record, default=str))
    (a.proj / ".swarm.toml").write_text("[build]\nmax_concurrent = 5\n")
    session = {"SWARM_PROJECT": str(a.proj)}
    holder = b.start("b1", dur=1.0)
    b.wait_event("start", "P-b1")
    worker = a.start("a1", dur=0.1, env=session)
    _finish([holder, worker])
    err = worker.stderr.read()
    assert worker.returncode == 0 and "does not load" in err
    assert "[build].max_concurrent is not a project's to set" in err
    assert _max_overlap(a.lines()) == 1 and _starts(a.lines()) == ["b1", "a1"]


def test_the_machine_file_is_read_live_and_a_broken_one_keeps_what_was_read(box):
    a, _ = box(max_concurrent=1)
    cfg = a.cfg()
    assert (cfg.build_max_concurrent, buildsem.seats(cfg)) == (1, 3)
    a.machine(max_concurrent=2, idle_yield_max=0)
    assert (cfg.build_max_concurrent, buildsem.seats(cfg)) == (2, 2)  # no reload
    path = machine.settings_path()
    path.write_text("[build]\nmax_concurrent = \"two\"\n")
    assert cfg.build_max_concurrent == 2  # a process that is up keeps what it read
    with pytest.raises(machine.SettingsError, match=r"\[build\].max_concurrent must be"):
        load(project_dir=str(a.proj))  # and every new command says what is wrong
    p = a.start("a1")
    _, err = p.communicate(timeout=20)
    assert p.returncode == 2 and "[build].max_concurrent must be a whole number" in err
    path.write_text("[build]\nmax_concurrent = 1\n")
    assert cfg.build_max_concurrent == 1 and load(project_dir=str(a.proj)).build_pair == "any"


def test_run_time_history_is_each_swarms_own(box):
    a, b = box()
    ev = {"event": "end", "exit": 0, "cls": "heavy", "argv": "cargo build", "cwd": "x"}
    events = ([dict(ev, swarm="alpha", run_s=10.0)] * 3 + [dict(ev, swarm="beta", run_s=900.0)] * 3
              + [dict(ev, swarm="alpha", cls="gc", run_s=4.0)])
    mine, theirs = buildlog.History(a.cfg(), events), buildlog.History(b.cfg(), events)
    assert mine.predict("cargo build", "x") == 10.0 and theirs.predict("cargo build", "x") == 900.0
    assert mine.default == theirs.default  # an unknown command: every build on the machine
    assert mine.gc_default == theirs.gc_default == 4.0
