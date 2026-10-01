"""What the build gate is doing now: ``swarm build --status``, the waiting
line a queued ``swarm build`` prints, and the one line in ``swarm status``.

Read-only: who holds what comes from ``/proc/locks`` (no lock is taken to look),
the queue from the ticket files, history from ``events.jsonl``, and which
holders are set aside as idle from the waiters' last measurement
(``buildsem/idle.json``, see :mod:`buildidle`).
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
    """Every build alive on a seat, oldest first: ``state`` is ``active``,
    ``yielded`` (set aside as idle; it does not count) or ``left`` (the build
    ended but a process it started still holds its seat)."""
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


def holders(cfg: Config, hist: buildlog.History, now: float,
            find_openers: bool = False, alive: list[dict] | None = None) -> list[dict]:
    """One entry per slot: the build that counts on it (``busy``), else free. A
    slot held exclusively with no seat behind it is someone from outside: gc, an
    older ``swarm build``, or a process such a build left behind."""
    alive = builds(cfg, now) if alive is None else alive
    locked = _locked_inodes(write_only=True)
    out = []
    for i in range(cfg.build_max_concurrent):
        path = buildsem._slot_path(cfg, i)
        counted = [b for b in alive if b["slot"] == i and b["state"] != "yielded"]
        entry: dict = {"slot": i, "busy": bool(counted)}
        if counted:
            first = counted[-1]  # the newest: the one a waiter is behind
            entry.update({k: first.get(k) for k in ("id", "phase", "argv", "cwd", "pid",
                                                    "running_s", "pred_s", "seat")})
            for key in ("quiet_s", "noyield", "busy_why", "hold", "repo", "alone"):
                if key in first:
                    entry[key] = first[key]
            if first["state"] == "left":
                entry["left"] = True
        elif slot_busy(path, locked):
            rec = buildsem.read_record(path)
            entry["busy"] = True
            if rec and not rec.get("ended") and rec.get("v") == 1 and "seat" not in rec:
                start = rec.get("start_ts") or now
                entry.update(id=rec.get("id"), phase=rec.get("phase"),
                             argv=rec.get("argv", ""), cwd=rec.get("cwd", ""),
                             pid=rec.get("pid") or rec.get("gate_pid"),
                             running_s=now - start, pred_s=rec.get("pred_s"))
            else:
                entry["unknown"] = True
                if rec and rec.get("ended"):
                    entry["after"] = {"id": rec.get("id"), "phase": rec.get("phase"),
                                      "argv": rec.get("argv", ""),
                                      "ended_s": now - rec["ended"]}
                if find_openers:
                    entry["pids"] = _openers(path)
        out.append(entry)
    return out


def _remaining(h: dict, hist: buildlog.History) -> float:
    if not h["busy"]:
        return 0.0
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


def _holder_text(h: dict, yield_s: int = 0, pair: bool = False) -> str:
    if not h["busy"]:
        return f"slot {h['slot']}: free"
    if h.get("unknown"):
        after = h.get("after")
        text = f"slot {h['slot']}: busy, no current record (gc, an older swarm build, or a"
        text += " process a build left behind"
        if after:
            text += f" — the last build here, {after.get('phase') or '-'}"
            text += f" `{short_cmd(after['argv'], 40)}`, ended {fmt_s(after['ended_s'])} ago"
        pids = h.get("pids")
        if pids:
            text += f"; held open by pid {', '.join(map(str, pids[:5]))}"
        return text + ")"
    usual = f" (usually ~{fmt_s(h['pred_s'])})" if h.get("pred_s") else ""
    text = (f"slot {h['slot']}: {h.get('phase') or '-'} `{short_cmd(h['argv'], 50)}`"
            f" running {fmt_s(h['running_s'])}{usual}")
    if h.get("left"):
        text += ", ended, but a process it started still holds the slot"
    if h.get("hold"):
        text += ", keeps its slot while idle (--hold)"
    elif h.get("noyield"):
        text += f", never yields ({h['noyield']})"
    elif yield_s and (h.get("quiet_s") or 0) >= min(30.0, yield_s / 2):
        text += f", idle {fmt_s(h['quiet_s'])} (yields its slot at {fmt_s(yield_s)})"
    return text + (_pair_text(h) if pair else "")


def _yielded_text(b: dict, pair: bool = False) -> str:
    text = (f"yielded: {b.get('phase') or '-'} `{short_cmd(b['argv'], 50)}` yielded after"
            f" {fmt_s(b.get('idle_s'))} idle, still running {fmt_s(b['running_s'])}")
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
    order = buildsem.service_order(view.tickets, view.counts, cfg.build_overtake,
                                   cfg.build_short_s, view.blocked)
    pos = next((n for n, t in enumerate(order, 1) if t["id"] == meta["id"]), len(order))
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
    rule = f"; held back: {why}" if why else ""
    return (f"queued {fmt_s(now - meta['queued_ts'])} — #{pos} of {len(order)} for"
            f" {cfg.build_max_concurrent} slot(s); {held};{usual} starts in"
            f" ~{fmt_s(eta)}{how}{rule}")


def recent(events: list[dict], n: int) -> list[dict]:
    """The last ``n`` finished calls: phase, class, wait, run, exit, command."""
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
        row.update(phase=e.get("phase"), cls=e.get("cls"), argv=e.get("argv", ""),
                   slot=e.get("slot") if e.get("slot") is not None else row.get("slot"))
        if kind == "end":
            row.update(run_s=e.get("run_s"), exit=e.get("exit"), end_ts=e.get("ts"))
            done.append(row)
        elif kind == "preflight_fail":
            row.update(preflight=True, end_ts=e.get("ts"))
            done.append(row)
    return done[-n:]


def snapshot(cfg: Config, n_recent: int = 10) -> dict:
    now = time.time()
    events = buildlog.read_events(cfg)
    hist = buildlog.History(cfg, events)
    alive = builds(cfg, now)
    slots = holders(cfg, hist, now, find_openers=True, alive=alive)
    tickets = buildsem.live_tickets(cfg, prune=False)
    ids = {t["id"] for t in tickets}
    counts = {k: v for k, v in (buildsem._read_q(cfg).get("overtaken") or {}).items()
              if k in ids}
    blocked = blocked_now(cfg, tickets, alive)
    order = buildsem.service_order(tickets, counts, cfg.build_overtake, cfg.build_short_s,
                                   blocked)
    when = etas(order, slots, hist)
    queue = [{"id": t["id"], "phase": t.get("phase"), "argv": t.get("argv", ""),
              "pid": t.get("pid"), "waiting_s": now - (t.get("queued_ts") or now),
              "pred_s": t.get("pred_s"), "passed": counts.get(t["id"], 0),
              "stale": not t.get("fresh", True), "starts_in_s": when.get(t["id"]),
              "repo": t.get("repo"), "alone": t.get("alone") if blocked is not None else None,
              "blocked": (blocked or {}).get(t["id"])}
             for t in order]
    on = buildidle.enabled(cfg)
    return {"max_concurrent": cfg.build_max_concurrent, "overtake": cfg.build_overtake,
            "short_s": cfg.build_short_s,
            "pair": cfg.build_pair if blocked is not None else "any",
            "alone": list(cfg.build_alone) if blocked is not None else [],
            "idle_yield_s": cfg.build_idle_yield_s if on else 0,
            "idle_yield_max": cfg.build_idle_yield_max if on else 0,
            "slots": slots, "builds": alive,
            "yielded": [b for b in alive if b["state"] == "yielded"],
            "queue": queue, "recent": recent(events, n_recent)}


def render(snap: dict) -> str:
    busy = sum(1 for s in snap["slots"] if s["busy"])
    rule = ("plain FIFO" if not snap["overtake"] else
            f"a build that usually takes ≤{snap['short_s']}s may pass a long one,"
            f" each long one at most {snap['overtake']}×")
    aside = snap.get("yielded") or []
    pair = snap.get("pair", "any") != "any"
    lines = [f"build gate: {snap['max_concurrent']} slot(s), {busy} busy,"
             + (f" {len(aside)} yielded," if aside else "")
             + f" {len(snap['queue'])} waiting ({rule})"]
    if not snap["max_concurrent"]:
        lines[0] = "build gate: off ([build].max_concurrent = 0)"
    yield_s = snap.get("idle_yield_s") or 0
    shown = set()
    for h in snap["slots"]:
        text = _holder_text(h, yield_s, pair and h["busy"] and not h.get("unknown"))
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
    if snap["queue"]:
        lines.append("queue, in the order they would start:")
    for n, t in enumerate(snap["queue"], 1):
        usual = f", usually ~{fmt_s(t['pred_s'])}" if t.get("pred_s") else ""
        stale = " (not polling: passed over)" if t["stale"] else ""
        rule = f" — held back: {t['blocked']}" if t.get("blocked") else ""
        lines.append(f"  {n}. {t.get('phase') or '-'} `{short_cmd(t['argv'], 50)}`"
                     f" waiting {fmt_s(t['waiting_s'])}{usual}, starts in"
                     f" ~{fmt_s(t['starts_in_s'])}{stale}{rule}")
    if snap["recent"]:
        lines.append("recent:")
    for r in snap["recent"]:
        when = time.strftime("%H:%M:%S", time.localtime(r.get("end_ts") or 0))
        if r.get("preflight"):
            lines.append(f"  {when} {r.get('phase') or '-'} refused before queueing"
                         f" `{short_cmd(r['argv'], 50)}`")
            continue
        code = "killed, unrecorded" if r.get("exit") is None else f"exit {r['exit']}"
        wait = f"queued {fmt_s(r['wait_s'])}, " if r.get("cls") == "heavy" else "light, "
        aside = f" (yielded {fmt_s(r['yielded_s'])} of it)" if r.get("yielded_s") else ""
        lines.append(f"  {when} {r.get('phase') or '-'} {wait}ran {fmt_s(r.get('run_s'))}"
                     f"{aside}, {code} `{short_cmd(r['argv'], 50)}`")
    return "\n".join(lines)


def summary_line(cfg: Config) -> str | None:
    """One line for ``swarm status``; ``None`` if no build ever went through."""
    if cfg.build_max_concurrent < 1 or not cfg.buildsem_dir.is_dir():
        return None
    now = time.time()
    hist = buildlog.History(cfg, [])
    alive = builds(cfg, now)
    slots = holders(cfg, hist, now, alive=alive)
    tickets = buildsem.live_tickets(cfg, prune=False)
    busy = [h for h in slots if h["busy"]]
    who = ", ".join(
        f"{h.get('phase') or '-'} `{short_cmd(h.get('argv', ''), 30)}` {fmt_s(h['running_s'])}"
        if not h.get("unknown") else "unrecorded holder" for h in busy)
    line = f"build gate: {len(busy)}/{len(slots)} busy" + (f" ({who})" if who else "")
    aside = [b for b in alive if b["state"] == "yielded"]
    if aside:
        line += f", {len(aside)} idle holder(s) yielded"
    if tickets:
        oldest = max(now - (t.get("queued_ts") or now) for t in tickets)
        line += f", {len(tickets)} waiting (longest {fmt_s(oldest)})"
    return line + " — swarm build --status"
