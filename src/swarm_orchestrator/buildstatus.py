"""What the build gate is doing now: ``swarm build --status``, the waiting
line a queued ``swarm build`` prints, and the one line in ``swarm status``.

Read-only: who holds what comes from ``/proc/locks`` (no lock is taken to look),
the queue from the ticket files, history from ``events.jsonl``, and which
holders are set aside as idle from the waiters' last measurement
(``buildsem/idle.json``, see :mod:`buildidle`). A gc is shown as what it is:
waiting for the gate to run alone (its ticket), or running (its record,
``buildsem/gc``, locked while it holds every slot).

The gate is the machine's, so what is shown is every swarm's builds. Each
holder, waiter, gc and finished call carries ``swarm`` (the slug), ``swarm_name``
and ``mine`` (is it the swarm that asked), and the text puts the name in front
of a build that is another swarm's: ``[glasheim] W3 `cargo nextest run```.
``frozen`` marks a holder whose swarm stands frozen.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

from . import buildidle, buildlog, buildpair, buildsem
from .buildlog import fmt_s, short_cmd
from .config import Config


def _locked_inodes(write_only: bool = False) -> set[int] | None:
    """Inodes with a granted flock, from ``/proc/locks`` (``None`` if unreadable).
    ``write_only``: exclusive locks only. A build holds its seat exclusively and
    its slot shared; a waiter's momentary probe is shared."""
    try:
        text = Path("/proc/locks").read_text()
    except OSError:
        return None
    out: set[int] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[1] == "->" or parts[1] != "FLOCK":
            continue
        if write_only and parts[3] != "WRITE":
            continue
        try:
            out.add(int(parts[5].rsplit(":", 1)[1]))
        except (IndexError, ValueError):
            continue
    return out


def slot_busy(path: Path, locked: set[int] | None) -> bool:
    """Is an exclusive lock held on ``path``?"""
    try:
        ino = path.stat().st_ino
    except OSError:
        return False
    if locked is not None:
        return ino in locked
    try:  # no /proc/locks: a momentary probe
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


def _whose(cfg: Config, rec: dict) -> dict:
    """The swarm fields of an entry made from ``rec`` (a record, a ticket, an
    event): whose it is, and whether that is the swarm that asked."""
    return {**buildlog.whose(rec), "mine": buildlog.mine(cfg, rec)}


def _openers(path: Path) -> list[int]:
    """Processes that have ``path`` open (what holds a slot nobody claims)."""
    target = str(path)
    out = []
    try:
        pids = [e for e in Path("/proc").iterdir() if e.name.isdigit()]
    except OSError:
        return out
    for p in pids:
        try:
            for fd in (p / "fd").iterdir():
                if os.readlink(fd) == target:
                    out.append(int(p.name))
                    break
        except OSError:
            continue
    return out


def builds(cfg: Config, now: float) -> list[dict]:
    """Every build alive on a seat, any swarm's, oldest first: ``state`` is
    ``active``, ``yielded`` (set aside as idle; it does not count) or ``left``
    (the build ended but a process it started still holds its seat)."""
    locked = _locked_inodes(write_only=True)
    st = buildidle.load(cfg)
    found = buildpair.marks(cfg) if buildpair.enabled(cfg) else {}
    out = []
    for k in buildsem._seat_indices(cfg):
        path = buildsem._seat_path(cfg, k)
        if not slot_busy(path, locked):
            continue
        rec = buildsem.read_record(path) or {}
        start = rec.get("start_ts") or now
        entry: dict = {"seat": k, "slot": rec.get("slot"), "id": rec.get("id"),
                       **_whose(cfg, rec), "frozen": buildlog.frozen(cfg, rec),
                       "phase": rec.get("phase"), "argv": rec.get("argv", ""),
                       "cwd": rec.get("cwd", ""), "pid": rec.get("pid") or rec.get("gate_pid"),
                       "running_s": now - start, "pred_s": rec.get("pred_s"),
                       "hold": bool(rec.get("hold")), "repo": rec.get("repo"),
                       "alone": rec.get("alone") or found.get(rec.get("id")),
                       "state": "left" if rec.get("ended") or not rec else "active"}
        if "repos" in rec:
            entry["repos"] = rec["repos"]
        if rec.get("over"):
            entry["over"] = True
        entry.update(buildidle.describe(cfg, st, rec, now))
        if "yielded_s" in entry:
            entry["state"] = "yielded"
        if entry["state"] == "left":
            entry["ended_s"] = now - rec["ended"] if rec.get("ended") else None
        out.append(entry)
    return sorted(out, key=lambda e: -e["running_s"])


def gc_running(cfg: Config, locked: set[int] | None) -> dict | None:
    """The record of the gc that holds the gate now, or ``None``."""
    path = buildsem.gc_path(cfg)
    if not slot_busy(path, locked):
        return None
    rec = buildsem.read_record(path)
    return rec if rec and not rec.get("ended") else None


def holders(cfg: Config, hist: buildlog.History, now: float,
            find_openers: bool = False, alive: list[dict] | None = None) -> list[dict]:
    """One entry per slot: the build that counts on it (``busy``), else free. A
    slot held exclusively with no seat behind it is gc (``gc``: it holds every
    slot while it runs) or someone from outside: an older ``swarm build``, or a
    process such a build left behind."""
    alive = builds(cfg, now) if alive is None else alive
    locked = _locked_inodes(write_only=True)
    gc = gc_running(cfg, locked)
    out = []
    for i in range(cfg.build_max_concurrent):
        path = buildsem._slot_path(cfg, i)
        counted = [b for b in alive if b["slot"] == i and b["state"] != "yielded"]
        entry: dict = {"slot": i, "busy": bool(counted)}
        if counted:
            first = counted[-1]  # the newest: the one a waiter is behind
            entry.update({k: first.get(k) for k in ("id", "swarm", "swarm_name", "mine",
                                                    "phase", "argv", "cwd", "pid",
                                                    "running_s", "pred_s", "seat")})
            for key in ("quiet_s", "noyield", "busy_why", "hold", "repo", "alone", "frozen"):
                if key in first:
                    entry[key] = first[key]
            if first["state"] == "left":
                entry["left"] = True
        elif slot_busy(path, locked) and gc:
            entry.update(busy=True, gc=True, id=gc.get("id"), **_whose(cfg, gc), phase=None,
                         argv=gc.get("argv", buildsem.GC_ARGV), cwd=gc.get("cwd", ""),
                         pid=gc.get("pid"), running_s=now - (gc.get("start_ts") or now),
                         pred_s=None)
        elif slot_busy(path, locked):
            rec = buildsem.read_record(path)
            entry["busy"] = True
            if rec and not rec.get("ended") and rec.get("v") == 1 and "seat" not in rec:
                start = rec.get("start_ts") or now
                entry.update(id=rec.get("id"), **_whose(cfg, rec), phase=rec.get("phase"),
                             argv=rec.get("argv", ""), cwd=rec.get("cwd", ""),
                             pid=rec.get("pid") or rec.get("gate_pid"),
                             running_s=now - start, pred_s=rec.get("pred_s"))
            else:
                entry["unknown"] = True
                if rec and rec.get("ended"):
                    entry["after"] = {"id": rec.get("id"), **_whose(cfg, rec),
                                      "phase": rec.get("phase"),
                                      "argv": rec.get("argv", ""),
                                      "ended_s": now - rec["ended"]}
                if find_openers:
                    entry["pids"] = _openers(path)
        out.append(entry)
    return out


def _remaining(h: dict, hist: buildlog.History) -> float:
    if not h["busy"]:
        return 0.0
    if h.get("gc"):
        return max(1.0, hist.gc_default - h["running_s"])
    if h.get("unknown") or h.get("left"):
        return hist.default / 2
    pred = h.get("pred_s") or hist.default
    left = pred - h["running_s"]
    return left if left > 0 else max(30.0, 0.25 * pred)  # overdue: a little longer


def etas(order: list[dict], slots: list[dict], hist: buildlog.History) -> dict[str, float]:
    """When each waiter would start, seconds from now, if history holds."""
    free = [_remaining(h, hist) for h in slots] or [0.0]
    out = {}
    for t in order:
        i = min(range(len(free)), key=free.__getitem__)
        out[t["id"]] = free[i]
        free[i] += t.get("pred_s") or hist.default
    return out


def _pair_text(h: dict) -> str:
    """Under the pairing rules: the holder's repo, and that it runs alone."""
    text = f", repo {buildpair.repo_text(h.get('repo'))}"
    if h.get("alone"):
        text += f", runs alone ({h['alone']})"
    return text


def blocked_now(cfg: Config, tickets: list[dict], alive: list[dict]) -> dict[str, str] | None:
    """``{ticket id: why}`` for the waiters the pairing rules hold back, read
    from :func:`builds`; ``None`` when the rules are off."""
    if not buildpair.enabled(cfg):
        return None
    return buildpair.blocked_all(buildpair.waiters(tickets), buildpair.counted(alive), {})


def gate_state(alive: list[dict], slots: list[dict]) -> tuple[bool, bool]:
    """``(busy, working)`` as :func:`buildsem.gc_view` wants them, read from
    :func:`builds` and :func:`holders`."""
    foreign = any(h.get("unknown") or h.get("gc") or (h["busy"] and "seat" not in h)
                  for h in slots)
    busy = bool(alive) or foreign
    return busy, busy and not foreign and all(b["state"] == "active" and not b.get("frozen")
                                              for b in alive)


def gcs(cfg: Config, tickets: list[dict], wall: str | None, busy: bool,
        now: float) -> list[dict]:
    """The gc tickets waiting for the gate, oldest first: for how long, when
    each stops being passed, when it gives up, and whether it holds the builds
    behind it back right now."""
    out = []
    for t in tickets:
        if not t.get("gc"):
            continue
        firm, leave = float(t.get("firm_ts") or 0.0), float(t.get("leave_ts") or 0.0)
        out.append({"state": "waiting", "id": t["id"], **_whose(cfg, t), "pid": t.get("pid"),
                    "waiting_s": now - (t.get("queued_ts") or now),
                    "firm_in_s": max(0.0, firm - now), "leaves_in_s": max(0.0, leave - now),
                    "holding": busy and t["id"] == wall, "stale": not t.get("fresh", True)})
    return out


def _gc_name(e: dict) -> str:
    return buildlog.who({**e, "phase": "gc"})


def _gc_text(e: dict) -> str:
    text = f"{_gc_name(e)}: waiting {fmt_s(e['waiting_s'])} to run alone"
    if e.get("stale"):
        return text + " (not polling: passed over)"
    if e["holding"]:
        return text + (f"; no build starts until it has run (it gives up in"
                       f" {fmt_s(e['leaves_in_s'])})")
    if 0 < e["firm_in_s"] < e["leaves_in_s"]:
        return text + (f"; builds pass it for another {fmt_s(e['firm_in_s'])}, then none"
                       " starts until it has run")
    return text + f"; builds pass it (it gives up in {fmt_s(e['leaves_in_s'])})"


#: A holder whose swarm stands frozen: it is alive, keeps its seat, and does
#: nothing until that swarm's ``swarm thaw``.
FROZEN = "frozen with its swarm (`swarm thaw` there wakes it; it keeps its seat)"


def _holder_text(h: dict, yield_s: int = 0, pair: bool = False) -> str:
    if not h["busy"]:
        return f"slot {h['slot']}: free"
    if h.get("gc"):
        return (f"slot {h['slot']}: {_gc_name(h)} running {fmt_s(h['running_s'])} (it deletes"
                " build output: no build runs beside it)")
    if h.get("unknown"):
        after = h.get("after")
        text = f"slot {h['slot']}: busy, no current record (an older swarm build, or a"
        text += " process a build left behind"
        if after:
            text += f" — the last build here, {buildlog.who(after)}"
            text += f" `{short_cmd(after['argv'], 40)}`, ended {fmt_s(after['ended_s'])} ago"
        pids = h.get("pids")
        if pids:
            text += f"; held open by pid {', '.join(map(str, pids[:5]))}"
        return text + ")"
    usual = f" (usually ~{fmt_s(h['pred_s'])})" if h.get("pred_s") else ""
    text = (f"slot {h['slot']}: {buildlog.who(h)} `{short_cmd(h['argv'], 50)}`"
            f" running {fmt_s(h['running_s'])}{usual}")
    if h.get("left"):
        text += ", ended, but a process it started still holds the slot"
    if h.get("frozen"):
        text += f", {FROZEN}"
    if h.get("hold"):
        text += ", keeps its slot while idle (--hold)"
    elif h.get("noyield"):
        text += f", never yields ({h['noyield']})"
    elif yield_s and (h.get("quiet_s") or 0) >= min(30.0, yield_s / 2):
        text += f", idle {fmt_s(h['quiet_s'])} (yields its slot at {fmt_s(yield_s)})"
    return text + (_pair_text(h) if pair else "")


def _yielded_text(b: dict, pair: bool = False) -> str:
    text = (f"yielded: {buildlog.who(b)} `{short_cmd(b['argv'], 50)}` yielded after"
            f" {fmt_s(b.get('idle_s'))} idle, still running {fmt_s(b['running_s'])}")
    if b.get("frozen"):
        text += f", {FROZEN}"
    if pair:
        text += _pair_text(b) + ", still holds its repo"
    if (b.get("sampled_s") or 0) > 6 * buildidle.SAMPLE_S:
        text += f" (last measured {fmt_s(b['sampled_s'])} ago)"
    return text


def queue_line(cfg: Config, meta: dict, view: buildsem.View,
               hist: buildlog.History | None) -> str:
    """The line a queued ``swarm build`` prints on joining and every 45 s."""
    hist = hist or buildlog.History(cfg)
    now = time.time()
    order = [t for t in buildsem.service_order(view.tickets, view.counts, cfg.build_overtake,
                                               cfg.build_short_s, view.blocked, view.wall)
             if not t.get("gc")]
    pos = next((n for n, t in enumerate(order, 1) if t["id"] == meta["id"]), len(order))
    first = [f"{buildlog.who_text(cfg, t)} `{short_cmd(t.get('argv', ''), 30)}`"
             for t in order[:pos - 1]]
    ahead = (f", behind {', '.join(first[:2])}"
             + (f" and {len(first) - 2} more" if len(first) > 2 else "")) if first else ""
    alive = builds(cfg, now)
    slots = holders(cfg, hist, now, alive=alive)
    eta = etas(order, slots, hist).get(meta["id"])
    yield_s = cfg.build_idle_yield_s if buildidle.enabled(cfg) else 0
    pair = view.blocked is not None
    held = "; ".join([_holder_text(h, yield_s, pair) for h in slots]
                     + [_yielded_text(b, pair) for b in alive if b["state"] == "yielded"])
    how = "" if meta.get("pred_s") else ", rough: no history for this command yet"
    usual = f" usually runs ~{fmt_s(meta['pred_s'])};" if meta.get("pred_s") else ""
    why = (view.blocked or {}).get(meta["id"])
    if not why and meta["id"] in buildsem.behind(view.tickets, view.wall):
        why = buildsem.GC_FIRST
    rule = f"; held back: {why}" if why else ""
    return (f"queued {fmt_s(now - meta['queued_ts'])} — #{pos} of {len(order)} for"
            f" {cfg.build_max_concurrent} slot(s) on this machine{ahead}; {held};{usual}"
            f" starts in ~{fmt_s(eta)}{how}{rule}")


def recent(events: list[dict], n: int) -> list[dict]:
    """The last ``n`` finished calls, every swarm's: whose, phase, class, wait,
    run, exit, command."""
    by_id: dict[str, dict] = {}
    done = []
    for e in events:
        rid = e.get("id")
        if not rid:
            continue
        row = by_id.setdefault(rid, {"id": rid})
        kind = e.get("event")
        if kind in ("start", "bypass"):
            row.update(wait_s=e.get("wait_s"), start_ts=e.get("ts"))
        if kind == "yield":
            row["yielded_ts"] = e.get("ts")
        elif kind in ("unyield", "end") and row.get("yielded_ts"):
            row["yielded_s"] = (row.get("yielded_s") or 0.0) + max(
                0.0, (e.get("ts") or 0.0) - row.pop("yielded_ts"))
        row.update(buildlog.whose(e), phase=e.get("phase"), cls=e.get("cls"),
                   argv=e.get("argv", ""),
                   slot=e.get("slot") if e.get("slot") is not None else row.get("slot"))
        if kind == "end":
            row.update(run_s=e.get("run_s"), exit=e.get("exit"), end_ts=e.get("ts"))
            done.append(row)
        elif kind == "preflight_fail":
            row.update(preflight=True, end_ts=e.get("ts"))
            done.append(row)
        elif kind == "left":
            row.update(left=True, wait_s=e.get("wait_s"), why=e.get("why"), end_ts=e.get("ts"))
            done.append(row)
    return done[-n:]


def snapshot(cfg: Config, n_recent: int = 10) -> dict:
    """The gate now, as ``swarm build --status --json`` prints it: the
    machine's limits, every swarm's holders and waiters, and the last calls.
    ``swarm`` and ``swarm_name`` at the top are the swarm that asked, the one
    each entry's ``mine`` is about."""
    now = time.time()
    events = buildlog.read_events(cfg)
    hist = buildlog.History(cfg, events)
    alive = builds(cfg, now)
    slots = holders(cfg, hist, now, find_openers=True, alive=alive)
    waiting = buildsem.live_tickets(cfg, prune=False)
    busy, working = gate_state(alive, slots)
    tickets, wall = buildsem.gc_view(waiting, now, busy, working)
    ids = {t["id"] for t in tickets}
    counts = {k: v for k, v in (buildsem._read_q(cfg).get("overtaken") or {}).items()
              if k in ids}
    blocked = blocked_now(cfg, tickets, alive)
    held = buildsem.behind(tickets, wall)
    order = [t for t in buildsem.service_order(tickets, counts, cfg.build_overtake,
                                               cfg.build_short_s, blocked, wall)
             if not t.get("gc")]
    when = etas(order, slots, hist)
    queue = [{"id": t["id"], **_whose(cfg, t), "phase": t.get("phase"),
              "argv": t.get("argv", ""),
              "pid": t.get("pid"), "waiting_s": now - (t.get("queued_ts") or now),
              "pred_s": t.get("pred_s"), "passed": counts.get(t["id"], 0),
              "stale": not t.get("fresh", True), "starts_in_s": when.get(t["id"]),
              "repo": t.get("repo"), "alone": t.get("alone") if blocked is not None else None,
              "blocked": (blocked or {}).get(t["id"])
              or (buildsem.GC_FIRST if t["id"] in held else None)}
             for t in order]
    running = next((h for h in slots if h.get("gc")), None)
    gc = ([{"state": "running", "id": running["id"], **buildlog.whose(running),
            "mine": running.get("mine"), "pid": running["pid"],
            "running_s": running["running_s"]}] if running else [])
    gc += gcs(cfg, waiting, wall, busy, now)
    on = buildidle.enabled(cfg)
    last = [dict(r, mine=buildlog.mine(cfg, r)) for r in recent(events, n_recent)]
    return {**buildlog.swarm_of(cfg),
            "max_concurrent": cfg.build_max_concurrent, "overtake": cfg.build_overtake,
            "short_s": cfg.build_short_s,
            "pair": cfg.build_pair if blocked is not None else "any",
            "alone": list(cfg.build_alone) if blocked is not None else [],
            "idle_yield_s": cfg.build_idle_yield_s if on else 0,
            "idle_yield_max": cfg.build_idle_yield_max if on else 0,
            "slots": slots, "builds": alive,
            "yielded": [b for b in alive if b["state"] == "yielded"],
            "queue": queue, "gc": gc, "recent": last}


def render(snap: dict) -> str:
    busy = sum(1 for s in snap["slots"] if s["busy"])
    rule = ("plain FIFO" if not snap["overtake"] else
            f"a build that usually takes ≤{snap['short_s']}s may pass a long one,"
            f" each long one at most {snap['overtake']}×")
    aside = snap.get("yielded") or []
    pair = snap.get("pair", "any") != "any"
    lines = [f"build gate: {snap['max_concurrent']} slot(s) on this machine, {busy} busy,"
             + (f" {len(aside)} yielded," if aside else "")
             + f" {len(snap['queue'])} waiting ({rule})"]
    if not snap["max_concurrent"]:
        lines[0] = "build gate: off ([build].max_concurrent = 0 in machine.toml)"
    yield_s = snap.get("idle_yield_s") or 0
    shown = set()
    for h in snap["slots"]:
        text = _holder_text(h, yield_s,
                            pair and h["busy"] and not h.get("unknown") and not h.get("gc"))
        if h.get("pid") and not h.get("unknown"):
            text += f" — pid {h['pid']}"
        lines.append("  " + text)
        shown.add(h.get("id"))
    for b in snap.get("builds") or []:
        if b["state"] == "yielded":
            lines.append(f"  {_yielded_text(b, pair)} — pid {b['pid']} (does not count: the"
                         " next build starts beside it; nothing was stopped)")
        elif b.get("id") not in shown:  # a second build counting on one slot: one woke up
            lines.append("  " + _holder_text(dict(b, busy=True), yield_s, pair)
                         + f" — pid {b['pid']} (was yielded, working again)")
    if yield_s:
        lines.append(f"  a holder idle for {fmt_s(yield_s)} yields its slot (at most"
                     f" {snap.get('idle_yield_max')} at once); it keeps running")
    if pair:
        names = ", ".join(snap.get("alone") or []) or "none"
        lines.append("  pairing: no two builds in one repo; these run with no build beside"
                     f" them: --hold, an unknown repo, and [build].alone ({names})")
    for e in snap.get("gc") or []:
        if e["state"] == "waiting":
            lines.append(f"  {_gc_text(e)} — pid {e['pid']}")
    if snap["queue"]:
        lines.append("queue, in the order they would start:")
    for n, t in enumerate(snap["queue"], 1):
        usual = f", usually ~{fmt_s(t['pred_s'])}" if t.get("pred_s") else ""
        stale = " (not polling: passed over)" if t["stale"] else ""
        rule = f" — held back: {t['blocked']}" if t.get("blocked") else ""
        lines.append(f"  {n}. {buildlog.who(t)} `{short_cmd(t['argv'], 50)}`"
                     f" waiting {fmt_s(t['waiting_s'])}{usual}, starts in"
                     f" ~{fmt_s(t['starts_in_s'])}{stale}{rule}")
    if snap["recent"]:
        lines.append("recent:")
    for r in snap["recent"]:
        when = time.strftime("%H:%M:%S", time.localtime(r.get("end_ts") or 0))
        if r.get("preflight"):
            lines.append(f"  {when} {buildlog.who(r)} refused before queueing"
                         f" `{short_cmd(r['argv'], 50)}`")
            continue
        if r.get("left") and r.get("cls") != buildsem.GC:
            lines.append(f"  {when} {buildlog.who(r)} waited {fmt_s(r.get('wait_s'))}"
                         f" for a slot and left; nothing ran ({r.get('why') or '?'})"
                         f" `{short_cmd(r['argv'], 50)}`")
            continue
        if r.get("left"):
            lines.append(f"  {when} {_gc_name(r)} waited {fmt_s(r.get('wait_s'))} for the gate"
                         " to empty"
                         f" and left; it held nothing ({r.get('why') or '?'})")
            continue
        code = "killed, unrecorded" if r.get("exit") is None else f"exit {r['exit']}"
        wait = (f"queued {fmt_s(r['wait_s'])}, " if r.get("cls") in ("heavy", buildsem.GC)
                else "light, ")
        aside = f" (yielded {fmt_s(r['yielded_s'])} of it)" if r.get("yielded_s") else ""
        lines.append(f"  {when} {_gc_name(r) if r.get('cls') == buildsem.GC else buildlog.who(r)}"
                     f" {wait}ran {fmt_s(r.get('run_s'))}"
                     f"{aside}, {code} `{short_cmd(r['argv'], 50)}`")
    return "\n".join(lines)


def summary_line(cfg: Config) -> str | None:
    """One line for ``swarm status``; ``None`` if no build ever went through.
    The gate is the machine's, so a neighbour's build is in it, by name."""
    if cfg.build_max_concurrent < 1 or not cfg.buildsem_dir.is_dir():
        return None
    now = time.time()
    hist = buildlog.History(cfg, [])
    alive = builds(cfg, now)
    slots = holders(cfg, hist, now, alive=alive)
    waiting = buildsem.live_tickets(cfg, prune=False)
    tickets = [t for t in waiting if not t.get("gc")]
    busy = [h for h in slots if h["busy"]]
    if any(h.get("gc") for h in busy):
        who = f"{_gc_name(busy[0])} {fmt_s(busy[0]['running_s'])}"
    else:
        who = ", ".join(
            f"{buildlog.who(h)} `{short_cmd(h.get('argv', ''), 30)}`"
            f" {fmt_s(h['running_s'])}"
            if not h.get("unknown") else "unrecorded holder" for h in busy)
    line = (f"build gate: {len(busy)}/{len(slots)} busy on this machine"
            + (f" ({who})" if who else ""))
    aside = [b for b in alive if b["state"] == "yielded"]
    if aside:
        line += f", {len(aside)} idle holder(s) yielded"
    if tickets:
        oldest = max(now - (t.get("queued_ts") or now) for t in tickets)
        line += f", {len(tickets)} waiting (longest {fmt_s(oldest)})"
    if len(waiting) > len(tickets):
        line += ", gc waiting to run alone"
    return line + " — swarm build --status"
