"""What the build gate is doing now: ``swarm build --status``, the waiting
line a queued ``swarm build`` prints, and the one line in ``swarm status``.

Read-only: slot occupancy comes from ``/proc/locks`` (no lock is taken to look),
the queue from the ticket files, history from ``events.jsonl``.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path

from . import buildlog, buildsem
from .buildlog import fmt_s, short_cmd
from .config import Config


def _locked_inodes() -> set[int] | None:
    """Inodes with a granted flock, from ``/proc/locks`` (``None`` if unreadable)."""
    try:
        text = Path("/proc/locks").read_text()
    except OSError:
        return None
    out: set[int] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[1] == "->" or parts[1] != "FLOCK":
            continue
        try:
            out.add(int(parts[5].rsplit(":", 1)[1]))
        except (IndexError, ValueError):
            continue
    return out


def slot_busy(path: Path, locked: set[int] | None) -> bool:
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


def holders(cfg: Config, hist: buildlog.History, now: float,
            find_openers: bool = False) -> list[dict]:
    locked = _locked_inodes()
    out = []
    for i in range(cfg.build_max_concurrent):
        path = buildsem._slot_path(cfg, i)
        busy = slot_busy(path, locked)
        rec = buildsem.read_record(path) if busy else None
        entry: dict = {"slot": i, "busy": busy}
        if rec and not rec.get("ended") and rec.get("v") == 1:
            start = rec.get("start_ts") or now
            pred = rec.get("pred_s")
            entry.update(id=rec.get("id"), phase=rec.get("phase"), argv=rec.get("argv", ""),
                         cwd=rec.get("cwd", ""), pid=rec.get("pid") or rec.get("gate_pid"),
                         running_s=now - start, pred_s=pred)
        elif busy:
            entry["unknown"] = True
            if rec and rec.get("ended"):
                entry["after"] = {"id": rec.get("id"), "phase": rec.get("phase"),
                                  "argv": rec.get("argv", ""), "ended_s": now - rec["ended"]}
            if find_openers:
                entry["pids"] = _openers(path)
        out.append(entry)
    return out


def _remaining(h: dict, hist: buildlog.History) -> float:
    if not h["busy"]:
        return 0.0
    if h.get("unknown"):
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


def _holder_text(h: dict) -> str:
    if not h["busy"]:
        return f"slot {h['slot']}: free"
    if h.get("unknown"):
        after = h.get("after")
        text = f"slot {h['slot']}: busy, no current record (an older swarm build, or a"
        text += " process a build left behind"
        if after:
            text += f" — the last build here, {after.get('phase') or '-'}"
            text += f" `{short_cmd(after['argv'], 40)}`, ended {fmt_s(after['ended_s'])} ago"
        pids = h.get("pids")
        if pids:
            text += f"; held open by pid {', '.join(map(str, pids[:5]))}"
        return text + ")"
    usual = f" (usually ~{fmt_s(h['pred_s'])})" if h.get("pred_s") else ""
    return (f"slot {h['slot']}: {h.get('phase') or '-'} `{short_cmd(h['argv'], 50)}`"
            f" running {fmt_s(h['running_s'])}{usual}")


def queue_line(cfg: Config, meta: dict, view: buildsem.View,
               hist: buildlog.History | None) -> str:
    """The line a queued ``swarm build`` prints on joining and every 45 s."""
    hist = hist or buildlog.History(cfg)
    now = time.time()
    order = buildsem.service_order(view.tickets, view.counts, cfg.build_overtake,
                                   cfg.build_short_s)
    pos = next((n for n, t in enumerate(order, 1) if t["id"] == meta["id"]), len(order))
    slots = holders(cfg, hist, now)
    eta = etas(order, slots, hist).get(meta["id"])
    held = "; ".join(_holder_text(h) for h in slots)
    how = "" if meta.get("pred_s") else ", rough: no history for this command yet"
    usual = f" usually runs ~{fmt_s(meta['pred_s'])};" if meta.get("pred_s") else ""
    return (f"queued {fmt_s(now - meta['queued_ts'])} — #{pos} of {len(order)} for"
            f" {cfg.build_max_concurrent} slot(s); {held};{usual} starts in"
            f" ~{fmt_s(eta)}{how}")


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
    slots = holders(cfg, hist, now, find_openers=True)
    tickets = buildsem.live_tickets(cfg, prune=False)
    ids = {t["id"] for t in tickets}
    counts = {k: v for k, v in (buildsem._read_q(cfg).get("overtaken") or {}).items()
              if k in ids}
    order = buildsem.service_order(tickets, counts, cfg.build_overtake, cfg.build_short_s)
    when = etas(order, slots, hist)
    queue = [{"id": t["id"], "phase": t.get("phase"), "argv": t.get("argv", ""),
              "pid": t.get("pid"), "waiting_s": now - (t.get("queued_ts") or now),
              "pred_s": t.get("pred_s"), "passed": counts.get(t["id"], 0),
              "stale": not t.get("fresh", True), "starts_in_s": when.get(t["id"])}
             for t in order]
    return {"max_concurrent": cfg.build_max_concurrent, "overtake": cfg.build_overtake,
            "short_s": cfg.build_short_s, "slots": slots, "queue": queue,
            "recent": recent(events, n_recent)}


def render(snap: dict) -> str:
    busy = sum(1 for s in snap["slots"] if s["busy"])
    rule = ("plain FIFO" if not snap["overtake"] else
            f"a build that usually takes ≤{snap['short_s']}s may pass a long one,"
            f" each long one at most {snap['overtake']}×")
    lines = [f"build gate: {snap['max_concurrent']} slot(s), {busy} busy,"
             f" {len(snap['queue'])} waiting ({rule})"]
    if not snap["max_concurrent"]:
        lines[0] = "build gate: off ([build].max_concurrent = 0)"
    for h in snap["slots"]:
        text = _holder_text(h)
        if h.get("pid") and not h.get("unknown"):
            text += f" — pid {h['pid']}"
        lines.append("  " + text)
    if snap["queue"]:
        lines.append("queue, in the order they would start:")
    for n, t in enumerate(snap["queue"], 1):
        usual = f", usually ~{fmt_s(t['pred_s'])}" if t.get("pred_s") else ""
        stale = " (not polling: passed over)" if t["stale"] else ""
        lines.append(f"  {n}. {t.get('phase') or '-'} `{short_cmd(t['argv'], 50)}`"
                     f" waiting {fmt_s(t['waiting_s'])}{usual}, starts in"
                     f" ~{fmt_s(t['starts_in_s'])}{stale}")
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
        lines.append(f"  {when} {r.get('phase') or '-'} {wait}ran {fmt_s(r.get('run_s'))},"
                     f" {code} `{short_cmd(r['argv'], 50)}`")
    return "\n".join(lines)


def summary_line(cfg: Config) -> str | None:
    """One line for ``swarm status``; ``None`` if no build ever went through."""
    if cfg.build_max_concurrent < 1 or not cfg.buildsem_dir.is_dir():
        return None
    now = time.time()
    hist = buildlog.History(cfg, [])
    slots = holders(cfg, hist, now)
    tickets = buildsem.live_tickets(cfg, prune=False)
    busy = [h for h in slots if h["busy"]]
    who = ", ".join(
        f"{h.get('phase') or '-'} `{short_cmd(h.get('argv', ''), 30)}` {fmt_s(h['running_s'])}"
        if not h.get("unknown") else "unrecorded holder" for h in busy)
    line = f"build gate: {len(busy)}/{len(slots)} busy" + (f" ({who})" if who else "")
    if tickets:
        oldest = max(now - (t.get("queued_ts") or now) for t in tickets)
        line += f", {len(tickets)} waiting (longest {fmt_s(oldest)})"
    return line + " — swarm build --status"
