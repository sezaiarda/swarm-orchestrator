"""``swarm build`` — a fair, visible, crash-safe gate for heavy build commands.

Every worker builds in its own isolated worktree, so N workers compiling in
parallel means N independent full builds, each fanning ``cargo`` out across every
core. On a memory-capped host that is how a box thrashes (or worse). ``swarm
build <cmd...>`` lets at most ``[build].max_concurrent`` heavy builds run at
once; the rest wait their turn.

**Slots.** One ``flock`` per slot, ``<state>/buildsem/slotN``. The lock is taken
on a file descriptor the build process *inherits*, so the build's whole process
tree holds the slot: however the build ends -- exit, crash, SIGKILL -- the slot
frees when the last process holding it is gone. No daemon, no counter to leak.
An older ``swarm build`` (before the queue existed) takes these same slot locks,
so old and new callers can never together exceed ``max_concurrent``.

**The queue.** Waiters take a ticket: ``<state>/buildsem/queue/<seq>-<id>.json``,
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

**What it says.** On stderr: the queue position, who holds each slot and for how
long, and an ETA from past run times, on joining and every 45 s; "queued Xs,
starting" when it starts; "ran Ys, exit N" when done. Every call is logged to
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

from . import buildclass, buildlog, procs
from .config import Config

_POLL_S = 0.5  # a waiter whose turn it is not yet
_TURN_POLL_S = 0.1  # the waiter whose turn it is, watching for a free slot
_REPORT_S = 45.0
_STALE_S = 15.0  # a ticket not refreshed this long is passed over
_KILL_GRACE_S = 10.0
HELD_ENV = "SWARM_BUILD_HELD"  # "<slot>:<id>" inside a build that holds a slot


def _say(msg: str) -> None:
    print(f"swarm build: {msg}", file=sys.stderr, flush=True)


def _slot_path(cfg: Config, i: int) -> Path:
    return cfg.buildsem_dir / f"slot{i}"


def _queue_dir(cfg: Config) -> Path:
    return cfg.buildsem_dir / "queue"


# -- slots ----------------------------------------------------------------
def _try_slots(cfg: Config) -> tuple[int, int] | None:
    """One non-blocking sweep for a free slot: ``(index, locked fd)`` or ``None``.
    The fd stays open on purpose -- the flock lives on it."""
    cfg.buildsem_dir.mkdir(parents=True, exist_ok=True)
    for i in range(cfg.build_max_concurrent):
        fd = os.open(_slot_path(cfg, i), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        return i, fd
    return None


def _try_once(cfg: Config) -> int | None:
    """A free slot's held fd, or ``None`` (no queue: gc and tests)."""
    got = _try_slots(cfg)
    return got[1] if got else None


def read_record(path: Path) -> dict | None:
    """The holder record at the start of a slot file (``None`` if none/partial)."""
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


def _mark_ended(cfg: Config, i: int, rec: dict, ts: float) -> None:
    rec = dict(rec, ended=ts)
    try:
        fd = os.open(_slot_path(cfg, i), os.O_RDWR)
    except OSError:
        return
    try:
        _write_record(fd, rec)
    finally:
        os.close(fd)


def _holder_alive(rec: dict) -> bool:
    pid, gate = rec.get("pid"), rec.get("gate_pid")
    return bool((isinstance(pid, int) and procs.same(pid, rec.get("pid_start")))
                or (isinstance(gate, int) and procs.same(gate, rec.get("gate_start"))))


def reap_records(cfg: Config, only: int | None = None, force: bool = False) -> None:
    """Write the ``end`` a dead holder never wrote (``exit`` null). Call under
    ``queue.lock`` so two noticers cannot both write it."""
    now = time.time()
    idx = [only] if only is not None else range(cfg.build_max_concurrent)
    for i in idx:
        rec = read_record(_slot_path(cfg, i))
        if not rec or rec.get("ended") or rec.get("v") != 1:
            continue
        if not force and _holder_alive(rec):
            continue
        start = rec.get("start_ts") or now
        buildlog.event(cfg, "end", id=rec.get("id", "?"), phase=rec.get("phase"),
                       pid=rec.get("pid") or rec.get("gate_pid") or 0, slot=i,
                       cls="heavy", argv=rec.get("argv", ""), cwd=rec.get("cwd", ""),
                       run_s=now - start, exit=None, ts=now)
        _mark_ended(cfg, i, rec, now)


# -- the queue ------------------------------------------------------------
@contextmanager
def _qlock(cfg: Config) -> Iterator[None]:
    cfg.buildsem_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.buildsem_dir / "queue.lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
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
            meta["fresh"] = now - os.fstat(fd).st_mtime < _STALE_S
            out.append(meta)
        finally:
            os.close(fd)
    return sorted(out, key=lambda m: m.get("seq", 0))


def _is_short(meta: dict, short_s: int) -> bool:
    pred = meta.get("pred_s")
    return isinstance(pred, (int, float)) and pred <= short_s


def select(tickets: list[dict], counts: dict[str, int], overtake: int,
           short_s: int) -> dict | None:
    """Whose turn it is: the oldest waiter, unless a predicted-short one stands
    behind predicted-long ones that may each still be passed (fewer than
    ``overtake`` times) -- then the oldest such short one."""
    live = [t for t in tickets if t.get("fresh", True)]
    if not live:
        return None
    if overtake > 0:
        for t in live:
            if _is_short(t, short_s):
                return t
            if counts.get(t["id"], 0) >= overtake:
                break
    return live[0]


def service_order(tickets: list[dict], counts: dict[str, int], overtake: int,
                  short_s: int) -> list[dict]:
    """The order the current waiters would start in, if nobody else came."""
    left = [t for t in tickets if t.get("fresh", True)]
    counts = dict(counts)
    order = []
    while left:
        t = select(left, counts, overtake, short_s)
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


def _try_turn(cfg: Config, t: Ticket, overtake: int,
              short_s: int) -> tuple[tuple[int, int] | None, View]:
    """If it is ``t``'s turn and a slot is free, take it (and leave the queue)."""
    try:
        os.utime(t.fd)  # still here: keep the ticket fresh
    except OSError:
        pass
    with _qlock(cfg):
        tickets = live_tickets(cfg, t)
        ids = {m["id"] for m in tickets}
        q = _read_q(cfg)
        counts = {k: int(v) for k, v in (q.get("overtaken") or {}).items() if k in ids}
        chosen = select(tickets, counts, overtake, short_s)
        view = View(tickets, counts, chosen is not None and chosen["id"] == t.meta["id"])
        if not view.my_turn:
            return None, view
        got = _try_slots(cfg)
        if got is None:
            return None, view
        for m in tickets:
            if m["seq"] < t.meta["seq"]:
                counts[m["id"]] = counts.get(m["id"], 0) + 1
        q["overtaken"] = counts
        _write_q(cfg, q)
        reap_records(cfg, only=got[0], force=True)  # the slot was free: its old holder is gone
        _write_record(got[1], _record(t.meta, None, None))
        _drop(t)
    return got, view


def _record(meta: dict, pid: int | None, start_ts: float | None) -> dict:
    return {
        "v": 1, "id": meta["id"], "phase": meta.get("phase"), "argv": meta.get("argv", ""),
        "cwd": meta.get("cwd", ""), "pred_s": meta.get("pred_s"),
        "queued_ts": meta.get("queued_ts"), "start_ts": start_ts or time.time(),
        "gate_pid": os.getpid(), "gate_start": procs.start_ticks(os.getpid()),
        "pid": pid, "pid_start": procs.start_ticks(pid) if pid else None, "ended": None,
    }


def _wait_turn(cfg: Config, t: Ticket, hist: buildlog.History | None,
               announce: bool) -> tuple[int, int]:
    """Poll until it is ``t``'s turn and a slot is free; report while waiting."""
    overtake, short_s = cfg.build_overtake, cfg.build_short_s
    next_report = 0.0
    while True:
        got, view = _try_turn(cfg, t, overtake, short_s)
        if got is not None:
            return got
        now = time.time()
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


def _wait_child(proc: subprocess.Popen, timeout: float | None, start: float) -> int:
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
            if timeout is not None and time.time() - start >= timeout:
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

    @property
    def text(self) -> str:
        return buildlog.argv_text(self.argv)

    def log(self, cfg: Config, kind: str, **kw) -> None:
        buildlog.event(cfg, kind, id=self.id, phase=self.phase, cls=self.cls,
                       argv=self.argv, cwd=self.cwd, **kw)


def _run_child(cfg: Config, call: _Call, *, held: tuple[int, int] | None,
               meta: dict | None, wait_s: float, timeout: float | None) -> int:
    env = _build_env(cfg, call.argv)
    slot = held[0] if held else None
    if held:
        env[HELD_ENV] = f"{slot}:{call.id}"
    try:
        proc = subprocess.Popen(call.argv, env=env, pass_fds=(held[1],) if held else (),
                                preexec_fn=_die_with_parent())
    except OSError as exc:
        _say(f"cannot run {call.argv[0]!r}: {exc}")
        now = time.time()
        if held:
            _write_record(held[1], dict(_record(meta or {"id": call.id}, None, now), ended=now))
        call.log(cfg, "start" if held else "bypass", pid=os.getpid(), slot=slot,
                 wait_s=wait_s)
        call.log(cfg, "end", pid=os.getpid(), slot=slot, run_s=0.0, exit=127)
        if held:
            os.close(held[1])
        return 127
    start = time.time()
    rec = None
    if held:
        rec = _record(meta or {"id": call.id}, proc.pid, start)
        _write_record(held[1], rec)
        waited = "no wait" if wait_s < 1 else f"queued {buildlog.fmt_s(wait_s)}"
        _say(f"{waited}, starting on slot {slot}: {buildlog.short_cmd(call.text)}")
    call.log(cfg, "start" if held else "bypass", pid=proc.pid, slot=slot, wait_s=wait_s)
    code = _wait_child(proc, timeout, start)
    now = time.time()
    if held:
        _write_record(held[1], dict(rec, ended=now))
    call.log(cfg, "end", pid=proc.pid, slot=slot, run_s=now - start, exit=code, ts=now)
    if held:
        _say(f"ran {buildlog.fmt_s(now - start)}, exit {code} (queued {buildlog.fmt_s(wait_s)})")
        os.close(held[1])  # the slot frees once the build's leftovers are gone too
    return code


def _phase(cfg: Config) -> str | None:
    return os.environ.get(cfg.env_marker) or os.environ.get("SWARM_PHASE") or None


def _inside_held(cfg: Config) -> bool:
    """Are we running inside a build that already holds a slot (nested call)?"""
    held = os.environ.get(HELD_ENV, "")
    slot, _, bid = held.partition(":")
    if not slot.isdigit() or not bid:
        return False
    rec = read_record(_slot_path(cfg, int(slot)))
    return bool(rec and rec.get("id") == bid and not rec.get("ended"))


def run(cfg: Config, argv: list[str], timeout: float | None = None) -> int:
    """Run ``argv`` through the gate and return its exit code."""
    if not argv:
        _say("no command given")
        return 2
    try:
        cwd = os.getcwd()
    except OSError:
        _say("cannot run: the current directory no longer exists")
        return 2
    if cfg.build_max_concurrent >= 1 and _inside_held(cfg):
        _say("already inside a build that holds a slot — running without queueing")
        return _exec(cfg, argv)
    try:
        verdict = buildclass.classify(argv, cwd, cfg.build_heavy, cfg.build_light)
    except Exception as exc:  # noqa: BLE001 -- a classifier bug must not stop a build
        verdict = buildclass.Verdict(buildclass.HEAVY, f"could not classify ({exc})")
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
        queued = time.time()
        meta = {"id": call.id, "phase": call.phase, "pid": os.getpid(), "argv": call.text,
                "cwd": cwd, "queued_ts": queued, "pred_s": hist.predict(argv, cwd)}
        ticket = _enqueue(cfg, meta)
        call.log(cfg, "queued", pid=os.getpid(), slot=None, ts=queued)
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
         phase: str | None = None) -> Iterator[Held]:
    """Hold one build slot for the body, in this process, queueing like any
    ``swarm build``: for a build the swarm runs itself and waits on (a landing's
    lane check). The fd is not inherited, so a daemon the build leaves behind
    cannot keep the slot. Set ``.exit`` on the yielded object to log it."""
    held = Held()
    if cfg.build_max_concurrent < 1:
        yield held
        return
    text = buildlog.argv_text(argv)
    hist = buildlog.History(cfg)
    queued = time.time()
    meta = {"id": uuid.uuid4().hex[:12], "phase": phase, "pid": os.getpid(), "argv": text,
            "cwd": str(cwd), "queued_ts": queued, "pred_s": hist.predict(text, str(cwd))}
    call = _Call(meta["id"], phase, text, str(cwd), buildclass.HEAVY)
    ticket = _enqueue(cfg, meta)
    call.log(cfg, "queued", pid=os.getpid(), slot=None, ts=queued)
    i, fd = _wait_turn(cfg, ticket, hist, announce=False)
    start = time.time()
    rec = _record(ticket.meta, os.getpid(), start)
    _write_record(fd, rec)
    call.log(cfg, "start", pid=os.getpid(), slot=i, wait_s=start - queued)
    try:
        yield held
    finally:
        now = time.time()
        _write_record(fd, dict(rec, ended=now))
        call.log(cfg, "end", pid=os.getpid(), slot=i, run_s=now - start, exit=held.exit,
                 ts=now)
        os.close(fd)


def _exec(cfg: Config, argv: list[str]) -> int:
    try:
        os.execvpe(argv[0], argv, _build_env(cfg, argv))
    except OSError as exc:
        _say(f"cannot run {argv[0]!r}: {exc}")
    return 127
