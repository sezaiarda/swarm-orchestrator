"""``swarm build`` — a fair, visible, crash-safe gate for heavy build commands.

Every worker builds in its own isolated worktree, so N workers compiling in
parallel means N independent full builds, each fanning ``cargo`` out across every
core. On a memory-capped host that is how a box thrashes (or worse). ``swarm
build <cmd...>`` lets at most ``[build].max_concurrent`` heavy builds run at
once; the rest wait their turn.

**One gate for the machine.** The limit is the machine's, not a swarm's: three
swarms that each allowed one build would run three. So the gate lives in the
machine directory (``<state root>/machine/buildsem``, :attr:`Config.buildsem_dir`),
every swarm on the machine queues at it, and its limits come from the machine's
own file (``machine.toml``, ``[build]``), read through to the file each time so
that every process of every swarm follows the same ones. Every ticket, record
and event names the swarm it belongs to (:func:`buildlog.swarm_of`). The queue
knows no swarm: arrivals are served in order whoever they are, with the bounded
passing described below, so one swarm with many builds queued cannot keep
another's waiting. Below, ``buildsem/`` is that directory.

**Slots and seats.** A build holds two ``flock``s, both on file descriptors
the build process *inherits*, so its whole process tree holds them: however the
build ends -- exit, crash, SIGKILL -- they free when the last process holding
them is gone. No daemon, no counter to leak.

- A **seat**, ``buildsem/seatK``, exclusively: one per build alive.
  There are ``max_concurrent + idle_yield_max`` of them, so the kernel itself
  caps the builds the gate can have alive. The seat file holds the build's
  record (id, swarm, phase, pid, command, start), which ``--status`` and the
  waiting line show; whether the lock is held is whether the build is alive.
- A **slot**, ``buildsem/slotN`` (``N < max_concurrent``), *shared*. It is
  a lock and nothing else. ``swarm gc`` takes every slot exclusively while it
  deletes build output (see *gc takes its turn*), which works only while no
  build is on any of them. So gc never runs while a build is alive, set aside
  or not, and no build starts while gc runs.

**Idle yield.** A holder whose whole process tree has done nothing for
``[build].idle_yield_s`` is *set aside*: it is not stopped or signalled, it
just stops counting against ``max_concurrent``, and the next waiter starts
beside it on the same slot. If it wakes up it counts again from that moment
(the build beside it keeps running; no new one starts while the builds that
count fill the slots). At most ``[build].idle_yield_max`` holders are set aside
at once. The waiters measure, and a set-aside holder's own ``swarm build``
keeps watching it while nobody waits; see :mod:`buildidle`. That same process
tells whoever ran the command, on stderr, when its slot was released and again
when it ends. ``swarm build --hold`` is the opt-out: such a build takes a slot
even if its command is light, and keeps it for as long as it runs (a
measurement that sleeps while something outside its process tree is measured).

**A holder that is gone, and one that stands frozen.** Nothing here trusts a
record over a lock. A build that died, with its whole swarm or without, holds no
lock: its seat is free the moment its last process is gone, whoever looks, and
the first to notice writes the ``end`` it never wrote. A build whose swarm
stands frozen (``swarm freeze``) is alive and holds its locks: its seat is
never taken from it, because it will resume. What it stops doing is keeping the
machine waiting: it is set aside as idle at once (:mod:`buildidle`), so the
next waiter starts beside it, and it counts again when its swarm is thawed. A
frozen swarm's *waiters* keep their tickets and their places, and are passed
over by the other swarms until they poll again.

**The queue.** Waiters take a ticket: ``buildsem/queue/<seq>-<id>.json``,
numbered under ``queue.lock`` and flocked by its waiter for as long as it waits.
A ticket whose lock can be taken belongs to a dead waiter and is deleted, so a
killed waiter never blocks the queue. Only the waiter whose turn it is tries
for a free slot, under ``queue.lock``, so arrivals are served in order (with
several slots, the next free one goes to the next in line). A waiter that stops
polling (a stopped process) is passed over until it polls again.

**Short builds first, boundedly.** A waiter whose command usually runs at most
``[build].short_s`` (median of its last runs, from the event log) may go ahead
of older waiters predicted long -- but each long waiter can be passed at most
``[build].overtake`` times, counted in ``queue.json``. So every waiter starts
after at most the waiters older than it plus ``overtake`` short ones: nobody
starves. ``overtake = 0`` is plain FIFO.

**Pairing rules.** With ``[build].pair = "distinct-repo"`` a build starts
beside the builds alive, any swarm's, only if it shares no repository with any
of them, and
a build that must run alone (an image build, ``--hold``, a build whose repo is
unknown: ``[build].alone``) starts only when no other build is alive, and
nothing starts while it runs (see :mod:`buildpair`). The queue then lets a
waiter the rules allow go ahead of older ones they hold back, out of the same
budget as the short ones: each waiter is passed at most ``[build].overtake``
times in all, and once it has been, or once a build that must run alone is the
oldest waiter, nothing starts until it has. ``"any"`` (the default) is the gate
without these rules.

**gc takes its turn.** gc must have the gate to itself: it deletes build
output while no build runs, and on a disk that builds strain that means no
build of any swarm. It gets that through the queue, never by taking slots one at a time: a gc that held one
slot while it waited for a build on another kept that slot from every build
for as long as it waited. :func:`whole` queues a ticket like any waiter and
holds nothing until, under ``queue.lock``, it finds no build alive and nobody
older waiting; then it takes every slot in that one step, or none. While its
ticket waits:

- *Builds pass it* for as long as any build is alive: the ticket holds nobody
  back, and gc runs the moment the gate is empty and it is the oldest waiter
  (no younger waiter starts ahead of it then).
- *For the last* ``hold_s`` *of its wait it is passed no more*: no build that
  queued after it starts until it has run, so the builds alive end and the
  gate empties under constant load too. Only builds at work are waited for
  like that; while a holder set aside as idle, a process a build left behind,
  or another gc is on the gate, nobody knows when it will be empty, and builds
  go on passing.
- *When its wait is over* it leaves the queue (a ``left`` event) and its caller
  tries again later. It never held a lock.

So the most a gc costs a waiting build is ``hold_s`` plus the sweep itself.
Its hold is recorded like a build's: ``buildsem/gc`` holds its record
and is locked for as long as it holds the gate, and its events carry
``cls`` ``"gc"``.

**What it says.** On stderr: the queue position, who holds each slot (which
swarm's build, when it is another's) and for how long, who is ahead, and an ETA
from past run times, on joining and every 45 s; "queued Xs,
starting" when it starts (and beside which idle holder, if one made room);
"ran Ys, exit N" when done. Every call is logged to
``events.jsonl`` (see :mod:`buildlog`), and ``swarm build --status`` shows the
gate now.

**Light commands** (see :mod:`buildclass`) skip the gate; a heavy command is
pre-flighted (program, ``cd`` target, ``-f`` file, manifest) before it queues.

The command runs as a child of ``swarm build``, which waits for it to log its
end and enforce ``--timeout`` (counted from the start, not the queue). Signals
``swarm build`` receives are passed on to the build's process tree, and the build
is told to exit (SIGTERM) if ``swarm build`` itself is killed.
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import buildclass, buildidle, buildlog, buildpair, freezer, procs
from .config import Config

_POLL_S = 0.5  # a waiter whose turn it is not yet
_TURN_POLL_S = 0.1  # the waiter whose turn it is, watching for a free slot
_REPORT_S = 45.0
_STALE_S = 15.0  # a ticket not refreshed this long is passed over
_KILL_GRACE_S = 10.0
_GC_POLL_S = 0.1  # a gc waiting for the gate: the waiters behind it wait on this
GC = "gc"  # ``cls`` of gc's events and of its record
GC_ARGV = "swarm gc"
GC_WHY = "gc deletes build output"
GC_FIRST = "gc runs first"  # why a waiter behind a gc ticket is held back
HELD_ENV = "SWARM_BUILD_HELD"  # "<slot>:<id>:<seat>" inside a build that holds a slot
HOLD_WHY = "started with --hold"
_HOLD_HINT = ("If it needs the machine to itself (a measurement), rerun it with"
              " `swarm build --hold ...`")


def _say(msg: str) -> None:
    print(f"swarm build: {msg}", file=sys.stderr, flush=True)


def _slot_path(cfg: Config, i: int) -> Path:
    return cfg.buildsem_dir / f"slot{i}"


def _seat_path(cfg: Config, k: int) -> Path:
    return cfg.buildsem_dir / f"seat{k}"


def _queue_dir(cfg: Config) -> Path:
    return cfg.buildsem_dir / "queue"


def gc_path(cfg: Config) -> Path:
    """gc's record; locked for as long as gc holds the gate."""
    return cfg.buildsem_dir / GC


def seats(cfg: Config) -> int:
    """How many builds the gate may have alive at once: the slots, plus the idle
    holders that may be set aside beside them."""
    if cfg.build_max_concurrent < 1:
        return 0
    extra = cfg.build_idle_yield_max if buildidle.enabled(cfg) else 0
    return cfg.build_max_concurrent + extra


def _seat_indices(cfg: Config) -> list[int]:
    """Every seat that may hold a build: this config's, and any other on disk
    (a holder started under a larger ``max_concurrent`` still counts)."""
    found = set(range(seats(cfg)))
    try:
        for p in cfg.buildsem_dir.iterdir():
            if p.name.startswith("seat") and p.name[4:].isdigit():
                found.add(int(p.name[4:]))
    except OSError:
        pass
    return sorted(found)


def _slot_indices(cfg: Config) -> list[int]:
    """Every slot a build may be on: this config's, and any other on disk."""
    found = set(range(max(0, cfg.build_max_concurrent)))
    try:
        for p in cfg.buildsem_dir.iterdir():
            if p.name.startswith("slot") and p.name[4:].isdigit():
                found.add(int(p.name[4:]))
    except OSError:
        pass
    return sorted(found)


# -- records --------------------------------------------------------------
def read_record(path: Path) -> dict | None:
    """The holder record at the start of a seat file, or of gc's (``None`` if
    none/partial)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").lstrip()
    except OSError:
        return None
    try:
        rec, _ = json.JSONDecoder().raw_decode(text)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _write_record(fd: int, rec: dict) -> None:
    """Rewrite the record in place: never replace the file, the lock is on it."""
    data = json.dumps(rec, separators=(",", ":")).encode()
    try:
        os.pwrite(fd, data, 0)
        os.ftruncate(fd, len(data))
    except OSError:
        pass


def _mark_ended(path: Path, rec: dict, ts: float) -> None:
    rec = dict(rec, ended=ts)
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return
    try:
        _write_record(fd, rec)
    finally:
        os.close(fd)


def _locked(path: Path) -> bool:
    """Is an exclusive ``flock`` held on ``path`` (a momentary shared probe)?"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def _holder_alive(rec: dict) -> bool:
    pid, gate = rec.get("pid"), rec.get("gate_pid")
    return bool((isinstance(pid, int) and procs.same(pid, rec.get("pid_start")))
                or (isinstance(gate, int) and procs.same(gate, rec.get("gate_start"))))


def _synthetic_end(cfg: Config, rec: dict, slot: int | None, now: float) -> None:
    start = rec.get("start_ts") or now
    run = buildlog.run_of(cfg, rec)  # the holder's own run: its frozen time, not ours
    buildlog.event(cfg, "end", id=rec.get("id", "?"), phase=rec.get("phase"),
                   pid=rec.get("pid") or rec.get("gate_pid") or 0, slot=slot,
                   cls=rec.get("cls") or "heavy", argv=rec.get("argv", ""),
                   cwd=rec.get("cwd", ""),
                   run_s=freezer.awake_elapsed(run, start, now) if run else now - start,
                   exit=None, repo=rec.get("repo"), ts=now, who=rec)


def reap_records(cfg: Config, seat: int | None = None) -> None:
    """Write the ``end`` a dead holder never wrote (``exit`` null), whichever
    swarm's build it was: the lock is the kernel's word, and it is the same for
    every swarm. Call under ``queue.lock`` so two noticers cannot both write it.
    A seat's holder is dead when its lock is free or both its processes are
    gone; one that stands frozen is neither. ``seat`` is one the caller has
    just taken: it was free, so whatever its record says is over."""
    now = time.time()
    for k in _seat_indices(cfg):
        path = _seat_path(cfg, k)
        rec = read_record(path)
        if not rec or rec.get("ended") or rec.get("v") != 1:
            continue
        if k != seat and _locked(path) and _holder_alive(rec):
            continue
        _synthetic_end(cfg, rec, rec.get("slot") if isinstance(rec.get("slot"), int) else None,
                       now)
        _mark_ended(path, rec, now)
    path = gc_path(cfg)  # a gc killed while it held the gate
    rec = read_record(path)
    if rec and not rec.get("ended") and rec.get("v") == 1 and not _locked(path):
        _synthetic_end(cfg, rec, None, now)
        _mark_ended(path, rec, now)


# -- the queue ------------------------------------------------------------
@contextmanager
def _qlock(cfg: Config, wait_s: float | None = None) -> Iterator[bool]:
    """Hold ``queue.lock`` for the block. A waiter blocks for it. A running
    build's own ``swarm build`` must never hang behind it (a stopped process may
    hold it): with ``wait_s`` it gives up after that long and yields ``False``."""
    cfg.buildsem_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.buildsem_dir / "queue.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        got = True
        if wait_s is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        got = False
                        break
                    time.sleep(0.02)
        yield got
    finally:
        os.close(fd)


def _read_q(cfg: Config) -> dict:
    try:
        data = json.loads((cfg.buildsem_dir / "queue.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"seq": 0, "overtaken": {}}
    return data if isinstance(data, dict) else {"seq": 0, "overtaken": {}}


def _write_q(cfg: Config, q: dict) -> None:
    path = cfg.buildsem_dir / "queue.json"
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(q), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Ticket:
    meta: dict
    path: Path
    fd: int


def _enqueue(cfg: Config, meta: dict) -> Ticket:
    qdir = _queue_dir(cfg)
    qdir.mkdir(parents=True, exist_ok=True)
    with _qlock(cfg):
        q = _read_q(cfg)
        # Never behind a ticket already waiting, even if queue.json was lost.
        seq = max([int(q.get("seq", 0))] + [int(p.name.split("-", 1)[0])
                                            for p in qdir.glob("[0-9]*-*.json")]) + 1
        q["seq"] = seq
        _write_q(cfg, q)
        meta = dict(meta, seq=seq)
        # Locked before it becomes visible: a probe must never see it unlocked.
        tmp = qdir / f".new-{meta['id']}"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, json.dumps(meta).encode())
        path = qdir / f"{seq:012d}-{meta['id']}.json"
        os.rename(tmp, path)
    return Ticket(meta, path, fd)


def _drop(t: Ticket) -> None:
    try:
        os.unlink(t.path)
    except OSError:
        pass
    try:
        os.close(t.fd)
    except OSError:
        pass


def _polling(cfg: Config, meta: dict, refreshed: float, now: float) -> bool:
    """Is the ticket ``meta``, last refreshed at ``refreshed``, still polled? A
    waiter that stands frozen (``swarm freeze``) cannot refresh its ticket and
    has not left: only the time its own run was awake counts toward
    :data:`_STALE_S`, so it is not passed by the waiters that wake before it.
    That is the order inside one swarm, which stands still as a whole. Another
    swarm does not wait for it: to its waiters a ticket of a swarm that stands
    frozen now is one nobody polls, passed over until it is polled again."""
    if now - refreshed < _STALE_S:
        return True
    run = buildlog.run_of(cfg, meta)
    if run is None or (run is not cfg and freezer.stands(run)):
        return False
    return freezer.awake_elapsed(run, refreshed, now) < _STALE_S


def live_tickets(cfg: Config, mine: Ticket | None = None,
                 prune: bool = True) -> list[dict]:
    """Every waiting ticket, oldest first, with ``fresh`` (still polling).
    ``prune`` deletes dead waiters' tickets (call it under ``queue.lock``)."""
    out: list[dict] = []
    try:
        entries = sorted(_queue_dir(cfg).iterdir())
    except OSError:
        return out
    now = time.time()
    for p in entries:
        if p.name.startswith(".new-"):
            if prune:  # its creator died inside queue.lock, before publishing it
                try:
                    os.unlink(p)
                except OSError:
                    pass
            continue
        if mine is not None and p == mine.path:
            out.append(dict(mine.meta, fresh=True))
            continue
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                pass  # held: its waiter is alive
            else:
                if prune:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                continue
            try:
                meta = json.loads(os.read(fd, 1 << 16) or b"{}")
            except ValueError:
                continue
            meta["fresh"] = _polling(cfg, meta, os.fstat(fd).st_mtime, now)
            out.append(meta)
        finally:
            os.close(fd)
    return sorted(out, key=lambda m: m.get("seq", 0))


def _is_short(meta: dict, short_s: int) -> bool:
    pred = meta.get("pred_s")
    return isinstance(pred, (int, float)) and pred <= short_s


def select(tickets: list[dict], counts: dict[str, int], overtake: int,
           short_s: int, blocked: dict[str, str] | None = None,
           wall: str | None = None) -> dict | None:
    """Whose turn it is: the oldest waiter, unless a predicted-short one stands
    behind predicted-long ones that may each still be passed (fewer than
    ``overtake`` times) -- then the oldest such short one.

    ``blocked`` (the pairing rules are on) names the waiters that may not start
    beside the builds alive. They are stepped over like the long ones, out of
    the same budget: the turn goes to the oldest waiter that is not blocked (or
    a short one behind it) as long as every older waiter may still be passed.
    A waiter that must run alone is never passed once it is the oldest. When
    nobody may start, the turn stays with the oldest, who waits for the builds
    in its way to end.

    ``wall`` is the id of a ticket nothing younger may pass, short or not: a
    gc's (see :func:`gc_view`)."""
    live = [t for t in tickets if t.get("fresh", True)]
    if not live:
        return None
    rules = blocked is not None
    first = None  # the oldest waiter the rules let start
    for n, t in enumerate(live):
        ok = not rules or t["id"] not in blocked
        if ok and overtake > 0 and _is_short(t, short_s):
            return t
        if ok and first is None:
            first = t
        if counts.get(t["id"], 0) >= overtake:
            break  # passed as often as it may be: nothing more goes ahead of it
        if (rules and n == 0 and t.get("alone")) or t["id"] == wall:
            break
    return first or live[0]


def service_order(tickets: list[dict], counts: dict[str, int], overtake: int,
                  short_s: int, blocked: dict[str, str] | None = None,
                  wall: str | None = None) -> list[dict]:
    """The order the current waiters would start in, if nobody else came (and,
    with ``blocked``, if the builds alive stayed as they are)."""
    left = [t for t in tickets if t.get("fresh", True)]
    counts = dict(counts)
    order = []
    while left:
        t = select(left, counts, overtake, short_s, blocked, wall)
        if t is None:
            break
        for older in left:
            if older["seq"] < t["seq"]:
                counts[older["id"]] = counts.get(older["id"], 0) + 1
        left.remove(t)
        order.append(t)
    return order + [t for t in tickets if not t.get("fresh", True)]


@dataclass
class View:
    tickets: list[dict] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    my_turn: bool = False
    # None: no pairing rules. Else {ticket id: why the rules hold it back now}.
    blocked: dict[str, str] | None = None
    wall: str | None = None  # the gc ticket nothing younger may pass now


@dataclass
class Claim:
    """What a build holds once it starts: its seat (exclusive) and a slot (shared)."""

    slot: int
    slot_fd: int
    seat: int
    seat_fd: int
    beside: list[dict] = field(default_factory=list)  # the idle holders that made room

    @property
    def fds(self) -> tuple[int, int]:
        return (self.slot_fd, self.seat_fd)

    def close(self) -> None:
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass


def live_holders(cfg: Config) -> list[dict]:
    """The record of every build alive on a seat, with ``seat`` and
    ``seat_path``. A seat whose lock is held but whose record is unreadable is a
    holder all the same (``id`` missing). Probes with a momentary shared lock."""
    out = []
    for k in _seat_indices(cfg):
        path = _seat_path(cfg, k)
        if _locked(path):
            rec = dict(read_record(path) or {}, seat=k, seat_path=path)
            if not isinstance(rec.get("slot"), int):
                rec["slot"] = None
            if not isinstance(rec.get("id"), str):
                rec.pop("id", None)
            out.append(rec)
    return out


def _gate_state(cfg: Config, holders: list[dict], now: float) -> tuple[bool, bool]:
    """``(busy, working)`` for :func:`gc_view`. Busy: something is on the gate,
    so a gc cannot start. Working: all of it is builds at work, which end; not
    a holder set aside as idle, one whose swarm stands frozen, a process a
    build left behind, or another gc, which holds the slots while it runs.
    Under ``queue.lock``."""
    sweeping = any(_locked(_slot_path(cfg, i)) for i in _slot_indices(cfg))
    if not holders and not sweeping:
        return False, False
    aside = {h.get("id") for h in buildidle.set_aside(cfg, buildidle.load(cfg), holders, now)}
    working = not sweeping and all(not h.get("over") and h.get("id") not in aside
                                   and not buildlog.frozen(cfg, h) for h in holders)
    return True, working


def gc_view(tickets: list[dict], now: float, busy: bool,
            working: bool) -> tuple[list[dict], str | None]:
    """The queue as a waiting build reads it: ``(tickets, wall)``.

    A gc's ticket (``gc`` set) is not a build's. While the gate is ``busy`` it
    is left out: builds pass it and it is not counted as passed. It stays in,
    as the ``wall`` no younger waiter may pass (:func:`select`), when the gate
    is empty (it is its turn if it is the oldest) and once it is *firm*: from
    its ``firm_ts`` on, while everything on the gate is ``working`` and so
    will end. A gc ticket whose waiter stopped polling is nobody's wall."""
    out: list[dict] = []
    wall = None
    for t in tickets:
        if t.get("gc"):
            if busy and not (working and now >= float(t.get("firm_ts") or 0.0)):
                continue
            if wall is None and t.get("fresh", True):
                wall = t["id"]
        out.append(t)
    return out, wall


def behind(tickets: list[dict], wall: str | None) -> set[str]:
    """The ids of the waiters ``wall`` holds back: every ticket younger."""
    seq = next((t.get("seq", 0) for t in tickets if t["id"] == wall), None)
    if seq is None:
        return set()
    return {t["id"] for t in tickets if t.get("seq", 0) > seq}


def _idle_pass(cfg: Config, holders: list[dict], now: float, force: bool = False) -> dict:
    """Measure the holders if due, and log who was set aside or woke up. A
    measurement that fails sets nobody aside: the queue then waits, as it would
    without idle yield, and the build that is waiting is not harmed."""
    try:
        st, changes = buildidle.update(cfg, holders, now, force=force)
    except Exception:  # noqa: BLE001 -- measuring must never break a queued build
        return {"ts": 0.0, "h": {}}
    for c in changes:
        rec = c.rec
        buildlog.event(cfg, c.kind, id=rec.get("id", "?"), phase=rec.get("phase"),
                       pid=rec.get("pid") or rec.get("gate_pid") or 0, slot=rec.get("slot"),
                       cls="heavy", argv=rec.get("argv", ""), cwd=rec.get("cwd", ""),
                       run_s=now - (rec.get("start_ts") or now), idle_s=c.idle_s,
                       repo=rec.get("repo"), why=c.why or None, ts=now, who=rec)
    return st


def _free_seat(cfg: Config) -> tuple[int, int] | None:
    """A seat nobody holds, locked: ``(index, fd)``; ``None`` when the builds
    alive already number ``max_concurrent + idle_yield_max``."""
    for k in range(seats(cfg)):
        fd = os.open(_seat_path(cfg, k), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        return k, fd
    return None


def _claim(cfg: Config) -> Claim | None:
    """Take a seat and a slot if a build may start now. Under ``queue.lock``.

    May start: fewer than ``max_concurrent`` builds *count* (alive and not set
    aside), a slot has no counted build on it, and a seat is free. A slot with
    nobody on it is preferred to one whose holders are all set aside. A slot
    that cannot be shared is gc's: it holds every one of them while it runs, so
    then there is none to take."""
    now = time.time()
    cap = cfg.build_max_concurrent
    holders = live_holders(cfg)
    st = _idle_pass(cfg, holders, now)
    aside = buildidle.set_aside(cfg, st, holders, now)
    if len(holders) - len(aside) >= cap:
        return None
    if aside:
        # A holder taken for idle stops counting only on a look taken just now,
        # over the last second or so. Until there is one, it counts.
        if len(buildidle.set_aside(cfg, st, holders, now, fine=True)) < len(aside):
            st = _idle_pass(cfg, holders, now, force=True)
        aside = buildidle.set_aside(cfg, st, holders, now, fine=True)
    ids = {h["id"] for h in aside}
    counted = [h for h in holders if h.get("id") not in ids]
    if len(counted) >= cap:
        return None
    busy = {h.get("slot") for h in counted}
    alive = {h.get("slot") for h in holders}
    free: list[tuple[int, int]] = []
    try:
        for i in range(cap):
            fd = os.open(_slot_path(cfg, i), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                continue
            if i in busy:
                os.close(fd)
            else:
                free.append((i, fd))
        free.sort(key=lambda g: (g[0] in alive, g[0]))
        seat = _free_seat(cfg) if free else None
    except BaseException:
        for _, fd in free:
            os.close(fd)
        raise
    for _, fd in free[1:] if seat is not None else free:
        os.close(fd)
    if seat is None:
        return None
    relied = len(holders) >= cap  # only the set-aside ones made room
    return Claim(free[0][0], free[0][1], seat[0], seat[1], aside if relied else [])


def _try_turn(cfg: Config, t: Ticket, overtake: int,
              short_s: int) -> tuple[Claim | None, View]:
    """If it is ``t``'s turn and a build may start, take a seat and a slot (and
    leave the queue). Every waiter that polls also keeps the holders measured."""
    try:
        os.utime(t.fd)  # still here: keep the ticket fresh
    except OSError:
        pass
    with _qlock(cfg):
        tickets = live_tickets(cfg, t)
        rules = buildpair.enabled(cfg)
        holders: list[dict] | None = None
        wall = None
        if any(m.get("gc") for m in tickets):  # a gc is waiting for the gate
            now = time.time()
            holders = live_holders(cfg)
            tickets, wall = gc_view(tickets, now, *_gate_state(cfg, holders, now))
        ids = {m["id"] for m in tickets}
        q = _read_q(cfg)
        counts = {k: int(v) for k, v in (q.get("overtaken") or {}).items() if k in ids}
        beside: list[dict] = []
        found: dict[str, str] = {}
        blocked = None
        if rules:
            beside = buildpair.counted(live_holders(cfg) if holders is None else holders)
            found = buildpair.marks(cfg)
            blocked = buildpair.blocked_all(buildpair.waiters(tickets), beside, found)
        chosen = select(tickets, counts, overtake, short_s, blocked, wall)
        view = View(tickets, counts, chosen is not None and chosen["id"] == t.meta["id"],
                    blocked, wall)
        if not view.my_turn or (blocked and t.meta["id"] in blocked):
            if buildidle.enabled(cfg):
                _idle_pass(cfg, live_holders(cfg), time.time())
            return None, view
        got = _claim(cfg)
        if got is None:
            return None, view
        if rules and beside:
            # The last look before a build starts beside others: is one of them
            # running something that must run alone, whatever its command said?
            seen = _spot_alone(cfg, beside, found)
            if seen is None or seen:
                got.close()
                if seen:
                    view.blocked = buildpair.blocked_all(tickets, beside, {**found, **seen})
                return None, view
        for m in tickets:
            if m["seq"] < t.meta["seq"]:
                counts[m["id"]] = counts.get(m["id"], 0) + 1
                if blocked and m["id"] in blocked:
                    buildlog.event(cfg, "passed", id=m["id"], phase=m.get("phase"),
                                   pid=m.get("pid") or 0, slot=None, cls="heavy",
                                   argv=m.get("argv", ""), cwd=m.get("cwd", ""),
                                   repo=m.get("repo"), why=blocked[m["id"]], by=t.meta["id"],
                                   who=m)
        q["overtaken"] = counts
        _write_q(cfg, q)
        reap_records(cfg, seat=got.seat)  # its last holder is gone: the end it never wrote
        _write_record(got.seat_fd, _record(t.meta, None, None, got))
        _drop(t)
    return got, view


def _note_alone(cfg: Config, seen: dict[str, str], now: float) -> None:
    """Record the builds just found to be running something that runs alone,
    and log each one once. Under ``queue.lock``."""
    live = live_holders(cfg)
    new = buildpair.mark(cfg, seen, live, now)
    for h in live:
        if h.get("id") in new:
            buildlog.event(cfg, "alone", id=h["id"], phase=h.get("phase"),
                           pid=h.get("pid") or h.get("gate_pid") or 0, slot=h.get("slot"),
                           cls="heavy", argv=h.get("argv", ""), cwd=h.get("cwd", ""),
                           run_s=now - (h.get("start_ts") or now), repo=h.get("repo"),
                           why=seen[h["id"]], ts=now, who=h)


def _spot_alone(cfg: Config, holders: list[dict], found: dict[str, str]) -> dict[str, str] | None:
    """Look at the processes of the holders not known to run alone: ``{build
    id: why}`` for those that do after all (recorded and logged). ``None`` when
    the look itself failed: then nothing starts beside them."""
    open_ = [h for h in holders if buildpair.holder_alone(h, found) is None]
    try:
        seen = buildpair.scan(cfg, open_)
        if seen:
            _note_alone(cfg, seen, time.time())
    except Exception:  # noqa: BLE001 -- unseen is not the same as safe
        return None
    return seen


def _record(meta: dict, pid: int | None, start_ts: float | None,
            claim: Claim | None = None) -> dict:
    return {
        "v": 1, "id": meta["id"], **buildlog.whose(meta), "phase": meta.get("phase"),
        "argv": meta.get("argv", ""), "cwd": meta.get("cwd", ""), "pred_s": meta.get("pred_s"),
        "queued_ts": meta.get("queued_ts"), "start_ts": start_ts or time.time(),
        "gate_pid": os.getpid(), "gate_start": procs.start_ticks(os.getpid()),
        "pid": pid, "pid_start": procs.start_ticks(pid) if pid else None, "ended": None,
        "slot": claim.slot if claim else None, "seat": claim.seat if claim else None,
        "noyield": meta.get("noyield"), "hold": bool(meta.get("hold")),
        "repo": meta.get("repo"), "repos": meta.get("repos"), "alone": meta.get("alone"),
    }


def _wait_turn(cfg: Config, t: Ticket, hist: buildlog.History | None,
               announce: bool, leave_ts: float | None = None) -> Claim | None:
    """Poll until it is ``t``'s turn and a build may start; report while waiting.
    With ``leave_ts`` the wait ends there: None, and the ticket is still ``t``'s.
    Time the run stood frozen is not time waited: the builds ahead stood still
    too, so the wait goes on for as long as it was cut short."""
    next_report = 0.0
    began = time.time()
    while True:
        got, view = _try_turn(cfg, t, cfg.build_overtake, cfg.build_short_s)
        if got is not None:
            return got
        now = time.time()
        if leave_ts is not None and now >= leave_ts:
            leave_ts += freezer.frozen_in(freezer.spans(cfg, now), began, now)
            began = now
            if now >= leave_ts:
                return None
        if now >= next_report:
            if announce:
                if next_report == 0.0:
                    _say("time in this queue does not count toward --timeout. If your own"
                         " tool timeout could fire before the build starts, run this in"
                         " the background and wait for it: a killed waiter loses its place.")
                    _say("several steps can share one turn: swarm build -- sh -c"
                         " 'step1 && step2'  (or: swarm build --script FILE)")
                from . import buildstatus

                _say(buildstatus.queue_line(cfg, t.meta, view, hist))
            with _qlock(cfg):
                reap_records(cfg)
            next_report = now + _REPORT_S
        time.sleep(_TURN_POLL_S if view.my_turn else _POLL_S)


# -- running the command --------------------------------------------------
def _build_env(cfg: Config, argv: list[str]) -> dict[str, str]:
    env = dict(os.environ)
    if cfg.build_jobs and argv and Path(argv[0]).name == "cargo":
        # Cap codegen fan-out so one build can't grab every core (memory spike);
        # honour an explicit override the caller already set.
        env.setdefault("CARGO_BUILD_JOBS", str(cfg.build_jobs))
    return env


def _die_with_parent():
    """A ``preexec_fn`` for the build: SIGTERM it when ``swarm build`` dies, so a
    killed gate does not leave its build running (Linux; best effort)."""
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None
    parent = os.getpid()

    def arm() -> None:
        prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG
        if os.getppid() != parent:  # the gate died before that took effect
            os._exit(143)

    return arm


def _tree(pid: int) -> list[tuple[int, int | None]]:
    """``pid`` and every descendant, with start times."""
    table = procs.table()
    kids: dict[int, list[int]] = {}
    for p, pp in table.items():
        kids.setdefault(pp, []).append(p)
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append((p, procs.start_ticks(p)))
        todo.extend(kids.get(p, []))
    return out


def _signal_tree(members: list[tuple[int, int | None]], sig: int) -> None:
    for p, ticks in members:
        if ticks is None or procs.same(p, ticks):
            try:
                os.kill(p, sig)
            except OSError:
                pass


class _OwnYield:
    """What a holder's own ``swarm build`` does once a second while it waits for
    its build. It tells whoever ran the command (stderr) when the gate sets the
    build aside as idle, and again when the build ends: other builds may have run
    beside it, which matters to a measurement. And while the build is set aside
    it keeps the holders measured at the usual rate when no waiter does, so a
    wake-up is counted (and logged) when it happens, not when the next waiter
    arrives. The waiters do not depend on any of it: a dead gate changes nothing."""

    def __init__(self, cfg: Config, bid: str) -> None:
        self.cfg, self.bid = cfg, bid
        self.every = buildidle.sample_every(cfg)
        self.due = 0.0
        self.since: float | None = None  # set aside since (None: it counts)
        self.first: tuple[float, float | None] | None = None  # when first, after how long idle
        self.total = 0.0  # seconds set aside, stretches already over

    def _look(self, now: float) -> bool:
        entry = buildidle.load(self.cfg)["h"].get(self.bid) or {}
        ts = entry.get("yielded")
        if ts is not None and self.since is None:
            self.since = float(ts)
            if self.first is None:
                self.first = (self.since, entry.get("idle_s"))
            _say(f"this command was idle for {buildlog.fmt_s(entry.get('idle_s'))}, so its build"
                 f" slot was released at {_clock(self.since)} and other builds may run beside it"
                 f" from now on (nothing was stopped). {_HOLD_HINT}")
        elif ts is None and self.since is not None:
            self.total += max(0.0, now - self.since)
            self.since = None
        return ts is not None

    def tick(self) -> None:
        now = time.time()
        if not self._look(now) or now < self.due:
            return
        self.due = now + self.every
        with _qlock(self.cfg, wait_s=0.0) as got:
            if got:
                _idle_pass(self.cfg, live_holders(self.cfg), time.time())

    def finish(self, now: float, run_s: float) -> None:
        """The last word, after the build's own output: was it ever set aside."""
        try:
            self._look(now)
        except Exception:  # noqa: BLE001 -- a notice must never change an exit code
            pass
        if self.first is None:
            return
        total = self.total + (max(0.0, now - self.since) if self.since is not None else 0.0)
        _say(f"note: this command sat idle for {buildlog.fmt_s(self.first[1])}, so its build"
             f" slot was released at {_clock(self.first[0])}; other builds may have run beside"
             f" it for {buildlog.fmt_s(total)} of its {buildlog.fmt_s(run_s)}. {_HOLD_HINT}")


class _OwnAlone:
    """What a holder's own ``swarm build`` does every :data:`buildpair.SCAN_S`
    under the pairing rules, for a build not known to run alone: it looks at the
    build's process tree for a command that must (a script that turned out to
    build an image). Found once, the build is alone for the rest of its run:
    recorded, logged, and said on stderr. The waiters do not depend on it: each
    takes its own look before it starts beside a running build."""

    def __init__(self, cfg: Config, bid: str, pid: int) -> None:
        self.cfg, self.bid, self.pid = cfg, bid, pid
        self.due = 0.0
        self.done = False

    def tick(self) -> None:
        now = time.time()
        if self.done or now < self.due:
            return
        self.due = now + buildpair.SCAN_S
        why = buildpair.tree_alone(self.cfg, self.pid)
        if why is None:
            return
        with _qlock(self.cfg, wait_s=1.0) as got:
            if not got:
                self.due = 0.0  # a stopped process holds the lock: look again soon
                return
            _note_alone(self.cfg, {self.bid: why}, now)
        self.done = True
        _say(f"this build runs alone from now on ({why}): no other build starts beside it")


def _clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _wait_child(cfg: Config, proc: subprocess.Popen, timeout: float | None, start: float,
                tick=None) -> int:
    """Wait for the build; stop it once it has run ``timeout``. Time the run
    stood frozen (``swarm freeze``) is not time the build ran: this process and
    its build were stopped with everything else, and a clock that counted it
    would end the build on the first look after the thaw."""
    received: list[int] = []

    def forward(signum, _frame):
        received.append(signum)
        _signal_tree(_tree(proc.pid), signum)

    old = {s: signal.signal(s, forward) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        while True:
            try:
                rc = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                pass
            if tick is not None:
                try:
                    tick()
                except Exception:  # noqa: BLE001 -- measuring must never end a build
                    tick = None
            if timeout is not None and time.time() - start >= timeout \
                    and freezer.awake_elapsed(cfg, start) >= timeout:
                _say(f"--timeout {buildlog.fmt_s(timeout)} reached (counted from the start)"
                     " — stopping the build")
                members = _tree(proc.pid)
                _signal_tree(members, signal.SIGTERM)
                try:
                    proc.wait(timeout=_KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
                _signal_tree(members + _tree(proc.pid), signal.SIGKILL)
                proc.wait()
                return 124
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    return 128 - rc if rc < 0 else rc


@dataclass
class _Call:
    id: str
    phase: str | None
    argv: list[str] | str
    cwd: str
    cls: str
    repo: str | None = None

    @property
    def text(self) -> str:
        return buildlog.argv_text(self.argv)

    def log(self, cfg: Config, kind: str, **kw) -> None:
        buildlog.event(cfg, kind, id=self.id, phase=self.phase, cls=self.cls,
                       argv=self.argv, cwd=self.cwd, repo=self.repo, **kw)


def _start_kw(meta: dict | None, held: Claim | None) -> dict:
    """What a ``start`` says of a build that took a slot (nothing for a bypass)."""
    if not held:
        return {"hold": None}
    why = (meta or {}).get("alone")
    return {"hold": bool(meta and meta.get("hold")), "alone": bool(why), "why": why}


def _ticker(ticks: list):
    """One callable for ``_wait_child`` out of several; one that fails is dropped."""
    def run() -> None:
        for tick in list(ticks):
            try:
                tick()
            except Exception:  # noqa: BLE001 -- measuring must never end a build
                ticks.remove(tick)

    return run if ticks else None


def _finish(claim: Claim, rec: dict, now: float) -> None:
    """The build is over: say so in its seat's record. ``over``: its own gate
    saw the command end. Whatever still holds the seat is a process it left
    behind, not a build (see :func:`buildpair.counted`)."""
    _write_record(claim.seat_fd, dict(rec, ended=now, over=True))


def _beside_text(cfg: Config, beside: list[dict]) -> str:
    if not beside:
        return ""
    h = beside[0]
    more = f" and {len(beside) - 1} more" if len(beside) > 1 else ""
    return (f" — beside {buildlog.who_text(cfg, h)}"
            f" `{buildlog.short_cmd(h.get('argv', ''), 40)}`"
            f"{more}, idle {buildlog.fmt_s(h.get('idle_s'))}: its slot was yielded")


def _run_child(cfg: Config, call: _Call, *, held: Claim | None,
               meta: dict | None, wait_s: float, timeout: float | None) -> int:
    env = _build_env(cfg, call.argv)
    slot = held.slot if held else None
    if held:
        env[HELD_ENV] = f"{slot}:{call.id}:{held.seat}"
    try:
        proc = subprocess.Popen(call.argv, env=env, pass_fds=held.fds if held else (),
                                preexec_fn=_die_with_parent())
    except OSError as exc:
        _say(f"cannot run {call.argv[0]!r}: {exc}")
        now = time.time()
        if held:
            _finish(held, _record(meta or {"id": call.id}, None, now, held), now)
        call.log(cfg, "start" if held else "bypass", pid=os.getpid(), slot=slot,
                 wait_s=wait_s, **_start_kw(meta, held))
        call.log(cfg, "end", pid=os.getpid(), slot=slot, run_s=0.0, exit=127)
        if held:
            held.close()
        return 127
    start = time.time()
    rec = None
    if held:
        rec = _record(meta or {"id": call.id}, proc.pid, start, held)
        _write_record(held.seat_fd, rec)
        waited = "no wait" if wait_s < 1 else f"queued {buildlog.fmt_s(wait_s)}"
        alone = f" — alone ({rec['alone']})" if rec.get("alone") else ""
        _say(f"{waited}, starting on slot {slot}: {buildlog.short_cmd(call.text)}"
             f"{_beside_text(cfg, held.beside)}{alone}")
    call.log(cfg, "start" if held else "bypass", pid=proc.pid, slot=slot, wait_s=wait_s,
             **_start_kw(rec, held))
    watch = _OwnYield(cfg, call.id) if held and buildidle.enabled(cfg) else None
    ticks = [watch.tick] if watch else []
    if held and buildpair.enabled(cfg) and not rec.get("alone"):
        ticks.append(_OwnAlone(cfg, call.id, proc.pid).tick)
    code = _wait_child(cfg, proc, timeout, start, _ticker(ticks))
    now = time.time()
    ran = freezer.awake_elapsed(cfg, start, now)  # what the next run is predicted from
    if held:
        _finish(held, rec, now)
    call.log(cfg, "end", pid=proc.pid, slot=slot, run_s=ran, exit=code, ts=now)
    if held:
        _say(f"ran {buildlog.fmt_s(ran)}, exit {code} (queued {buildlog.fmt_s(wait_s)})")
        if watch:
            watch.finish(now, ran)
        held.close()  # the seat and slot free once the build's leftovers are gone too
    return code


def _pairing(cfg: Config, cwd: str, verdict: buildclass.Verdict | None,
             hold: bool = False) -> dict:
    """What a ticket says of its build for the pairing rules: ``repos``, the
    repositories it works in as their places on the machine (``None``:
    unknown); ``names``, this swarm's name for each; ``repo``, the first one's;
    and ``alone``, why it runs alone under the rules (``None``: it need not)."""
    try:
        places = buildpair.repos(cfg, cwd, verdict)
        names = {p: buildpair.name(cfg, p) for p in places or ()}
    except Exception:  # noqa: BLE001 -- unknown is safe: such a build runs alone
        places, names = None, {}
    return {"repos": places, "names": names, "repo": names[places[0]] if places else None,
            "alone": buildpair.alone_why(cfg, verdict, places, hold)}


def _phase(cfg: Config) -> str | None:
    return os.environ.get(cfg.env_marker) or os.environ.get("SWARM_PHASE") or None


def _inside_held(cfg: Config) -> bool:
    """Are we running inside a build that already holds a slot (nested call)?"""
    _slot, bid, seat = (os.environ.get(HELD_ENV, "").split(":") + ["", ""])[:3]
    if not bid or not seat.isdigit():
        return False
    rec = read_record(_seat_path(cfg, int(seat)))
    return bool(rec and rec.get("id") == bid and not rec.get("ended"))


def run(cfg: Config, argv: list[str], timeout: float | None = None,
        hold: bool = False) -> int:
    """Run ``argv`` through the gate and return its exit code. ``hold``: it takes
    a slot whatever the command is, and never yields it for looking idle."""
    if not argv:
        _say("no command given")
        return 2
    try:
        cwd = os.getcwd()
    except OSError:
        _say("cannot run: the current directory no longer exists")
        return 2
    if cfg.build_max_concurrent >= 1 and _inside_held(cfg):
        _say("already inside a build that holds a slot — running without queueing"
             + (" (--hold does nothing here: give it to the outer swarm build)" if hold else ""))
        return _exec(cfg, argv)
    rules = buildpair.enabled(cfg)
    try:
        verdict = buildclass.classify(argv, cwd, cfg.build_heavy, cfg.build_light,
                                      alone=cfg.build_alone if rules else None)
    except Exception as exc:  # noqa: BLE001 -- a classifier bug must not stop a build
        verdict = buildclass.Verdict(buildclass.HEAVY, f"could not classify ({exc})",
                                     alone="its command could not be read" if rules else None)
    noyield = HOLD_WHY if hold else buildclass.daemon_side(verdict)
    if hold and verdict.cls == buildclass.LIGHT and cfg.build_max_concurrent >= 1:
        verdict.cls, verdict.why = buildclass.HEAVY, "--hold takes a slot"
    call = _Call(uuid.uuid4().hex[:12], _phase(cfg), list(argv), cwd, verdict.cls)
    try:
        if verdict.cls == buildclass.LIGHT or cfg.build_max_concurrent < 1:
            if verdict.cls == buildclass.LIGHT:
                _say(f"light command ({verdict.why}) — running now, not queued")
            return _run_child(cfg, call, held=None, meta=None, wait_s=0.0, timeout=timeout)
        problem = buildclass.preflight(verdict)
        if problem:
            _say(f"{problem} — checked before queueing; nothing ran")
            call.log(cfg, "preflight_fail", pid=os.getpid(), slot=None)
            return 127 if problem.startswith("cannot run") else 2
        hist = buildlog.History(cfg)
        pairing = _pairing(cfg, cwd, verdict, hold)
        call.repo, alone = pairing["repo"], pairing["alone"]
        queued = time.time()
        meta = {"id": call.id, **buildlog.swarm_of(cfg), "phase": call.phase,
                "pid": os.getpid(), "argv": call.text, "cwd": cwd, "queued_ts": queued,
                "pred_s": hist.predict(argv, cwd), "noyield": noyield, "hold": hold,
                **pairing}
        ticket = _enqueue(cfg, meta)
        call.log(cfg, "queued", pid=os.getpid(), slot=None, ts=queued, alone=bool(alone),
                 why=alone)
        held = _wait_turn(cfg, ticket, hist, announce=True)
        return _run_child(cfg, call, held=held, meta=ticket.meta,
                          wait_s=time.time() - queued, timeout=timeout)
    except KeyboardInterrupt:
        return 130


@dataclass
class Held:
    """What a :func:`slot` holder reports back: its exit code, if it has one."""

    exit: int | None = None


@contextmanager
def slot(cfg: Config, argv: list[str] | str = "", cwd: str | Path = "",
         phase: str | None = None, wait_s: float | None = None) -> Iterator[Held]:
    """Hold one build slot for the body, in this process, queueing like any
    ``swarm build``: for a build the swarm runs itself and waits on (a landing's
    lane check). The fds are not inherited, so a daemon the build leaves behind
    cannot keep the slot. Set ``.exit`` on the yielded object to log it.

    ``wait_s`` is for a caller nothing else can go on without (the integrator):
    with no slot after that long it leaves the queue (a ``left`` event), having
    held nothing, and :class:`Busy` is raised."""
    held = Held()
    if cfg.build_max_concurrent < 1:
        yield held
        return
    text = buildlog.argv_text(argv)
    hist = buildlog.History(cfg)
    queued = time.time()
    rules = buildpair.enabled(cfg)
    try:
        shell = ["sh", "-c", argv] if isinstance(argv, str) else list(argv)
        verdict = buildclass.classify(shell, str(cwd) or None,
                                      alone=cfg.build_alone if rules else None)
        noyield = buildclass.daemon_side(verdict)
    except Exception:  # noqa: BLE001 -- a classifier bug must not stop a build
        noyield = None
        verdict = buildclass.Verdict(buildclass.HEAVY, "could not classify",
                                     alone="its command could not be read" if rules else None)
    pairing = _pairing(cfg, str(cwd), verdict)
    alone = pairing["alone"]
    meta = {"id": uuid.uuid4().hex[:12], **buildlog.swarm_of(cfg), "phase": phase,
            "pid": os.getpid(), "argv": text, "cwd": str(cwd), "queued_ts": queued,
            "pred_s": hist.predict(text, str(cwd)), "noyield": noyield, **pairing}
    call = _Call(meta["id"], phase, text, str(cwd), buildclass.HEAVY, meta["repo"])
    ticket = _enqueue(cfg, meta)
    call.log(cfg, "queued", pid=os.getpid(), slot=None, ts=queued, alone=bool(alone),
             why=alone)
    claim = _wait_turn(cfg, ticket, hist, announce=False,
                       leave_ts=None if wait_s is None else queued + max(0.0, wait_s))
    if claim is None:
        _drop(ticket)
        now = time.time()
        holders = live_holders(cfg)
        why = _alive_text(cfg, holders, now) if holders else "older waiters come first"
        call.log(cfg, "left", pid=os.getpid(), slot=None, wait_s=now - queued,
                 alone=bool(alone), why=why)
        raise Busy(f"no build slot within {buildlog.fmt_s(wait_s)} ({why})")
    start = time.time()
    rec = _record(ticket.meta, os.getpid(), start, claim)
    _write_record(claim.seat_fd, rec)
    call.log(cfg, "start", pid=os.getpid(), slot=claim.slot, wait_s=start - queued,
             hold=False, alone=bool(alone), why=alone)
    try:
        yield held
    finally:
        now = time.time()
        _finish(claim, rec, now)
        call.log(cfg, "end", pid=os.getpid(), slot=claim.slot,
                 run_s=freezer.awake_elapsed(cfg, start, now), exit=held.exit, ts=now)
        claim.close()


# -- gc's turn ------------------------------------------------------------
class Busy(RuntimeError):
    """The gate did not empty within the wait (:func:`whole`), or gave no slot
    within it (:func:`slot` with ``wait_s``)."""


def _alive_text(cfg: Config, holders: list[dict], now: float) -> str:
    h = min(holders, key=lambda r: r.get("start_ts") or now)
    more = f" and {len(holders) - 1} more" if len(holders) > 1 else ""
    slot = f"slot {h['slot']}" if isinstance(h.get("slot"), int) else "a seat"
    return (f"{buildlog.who_text(cfg, h)} `{buildlog.short_cmd(h.get('argv', ''), 40)}`"
            f" on {slot}, running {buildlog.fmt_s(now - (h.get('start_ts') or now))}{more}")


def _take_whole(cfg: Config, t: Ticket) -> tuple[list[int] | None, str]:
    """Take the whole gate if nothing is alive on it and nobody older waits:
    every slot exclusively and gc's record, in this one step under
    ``queue.lock``, or nothing at all. ``(fds, "")``, the last fd being the
    record's, or ``(None, what is in the way)``."""
    try:
        os.utime(t.fd)  # still here: keep the ticket fresh
    except OSError:
        pass
    with _qlock(cfg):
        tickets = live_tickets(cfg, t)
        now = time.time()
        holders = live_holders(cfg)
        if holders:
            return None, _alive_text(cfg, holders, now)
        ahead = [m for m in tickets if m.get("fresh", True) and m["seq"] < t.meta["seq"]]
        if ahead:
            return None, f"{len(ahead)} older waiter(s) in the build queue"
        fds: list[int] = []
        try:
            for i in _slot_indices(cfg):
                fd = os.open(_slot_path(cfg, i), os.O_CREAT | os.O_RDWR, 0o644)
                fds.append(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    for held in fds:  # all of them or none: nothing is kept
                        os.close(held)
                    return None, "another gc holds the gate"
            reap_records(cfg)  # whoever was here is gone: the ends never written
            seat = os.open(gc_path(cfg), os.O_CREAT | os.O_RDWR, 0o644)
            fds.append(seat)
            fcntl.flock(seat, fcntl.LOCK_EX)  # only a momentary probe can be on it
            tick = procs.start_ticks(os.getpid())
            _write_record(seat, {
                "v": 1, "id": t.meta["id"], **buildlog.whose(t.meta), "cls": GC, "phase": None,
                "argv": t.meta.get("argv", GC_ARGV), "cwd": t.meta.get("cwd", ""),
                "queued_ts": t.meta.get("queued_ts"), "start_ts": now, "pid": os.getpid(),
                "pid_start": tick, "gate_pid": os.getpid(), "gate_start": tick, "ended": None,
            })
        except BaseException:
            for held in fds:
                os.close(held)
            raise
        _drop(t)
    return fds, ""


@contextmanager
def whole(cfg: Config, wait_s: float, hold_s: float = 0.0,
          argv: str = GC_ARGV) -> Iterator[list[int]]:
    """Hold the whole gate for the body, in this process: no build of any swarm
    is alive when it starts and none starts until it ends. For gc.

    It queues like a build and holds nothing while it waits (see *gc takes its
    turn* in the module docstring): for ``wait_s`` in all, of which the last
    ``hold_s`` hold back the builds queued after it. Raises :class:`Busy` when
    the gate did not empty in that time. Yields the slots' locked fds."""
    queued = time.time()
    wait_s = max(0.0, wait_s)
    meta = {"id": uuid.uuid4().hex[:12], **buildlog.swarm_of(cfg), "phase": None,
            "pid": os.getpid(), "argv": argv,
            "cwd": str(cfg.project_dir), "queued_ts": queued, "gc": True, "alone": GC_WHY,
            "firm_ts": queued + max(0.0, wait_s - max(0.0, hold_s)),
            "leave_ts": queued + wait_s}
    call = _Call(meta["id"], None, argv, meta["cwd"], GC)
    ticket = _enqueue(cfg, meta)
    call.log(cfg, "queued", pid=os.getpid(), slot=None, ts=queued, alone=True, why=GC_WHY)
    fds, why = None, "interrupted"
    try:
        while True:
            fds, why = _take_whole(cfg, ticket)
            if fds is not None or time.time() >= meta["leave_ts"]:
                break
            time.sleep(_GC_POLL_S)
    finally:
        if fds is None:
            _drop(ticket)
            call.log(cfg, "left", pid=os.getpid(), slot=None, wait_s=time.time() - queued,
                     alone=True, why=why)
    if fds is None:
        raise Busy(f"the build gate was not empty within {wait_s:.0f}s ({why}); nothing was"
                   " held while waiting — try again later")
    start = time.time()
    call.log(cfg, "start", pid=os.getpid(), slot=None, wait_s=start - queued, hold=False,
             alone=True, why=GC_WHY, ts=start)
    code = 1
    try:
        yield fds[:-1]
        code = 0
    finally:
        now = time.time()
        rec = read_record(gc_path(cfg))
        if rec and rec.get("id") == meta["id"]:
            _write_record(fds[-1], dict(rec, ended=now))
        call.log(cfg, "end", pid=os.getpid(), slot=None, run_s=now - start, exit=code, ts=now)
        for fd in reversed(fds):
            os.close(fd)  # closing releases the lock


def _exec(cfg: Config, argv: list[str]) -> int:
    try:
        os.execvpe(argv[0], argv, _build_env(cfg, argv))
    except OSError as exc:
        _say(f"cannot run {argv[0]!r}: {exc}")
    return 127
