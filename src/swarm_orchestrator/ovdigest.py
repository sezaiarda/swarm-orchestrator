"""The digest the Overseer reads first: the whole swarm on one page.

Written by the supervisor just before each pass, as ``digest-<id>.md`` (for the
session) and ``digest-<id>.json`` (the same data, for anything that wants to show
it). Everything in it is already on disk somewhere — ``swarm context``, the
recaps, the completion sentinels, the decision notes, the operator queue, the
notification ledger — but scattered across a dozen files a session would spend
its first ten minutes and a good share of its context finding. The digest does
that walk once, cheaply, and keeps it short: counts before lists, lists capped.

Nothing here decides anything. What the Overseer should *do* about what it reads
is the prompt's business (``prompts/overseer.md``).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from . import buildstatus
from . import doctor as doctor_mod
from . import landing as landing_mod
from . import ledger as ledger_mod
from . import ledgerw
from . import machine
from . import notes as notes_mod
from . import opqueue
from . import owner as owner_mod
from . import pushowed
from . import recap as recap_mod
from . import state as state_mod
from . import statuses, telegram
from .config import Config
from .master import build_context
from .overseer import SUMMARY, Reason, overseer_dir, starvation_map
from .state import State

#: How many recently finished phases the digest lists in full.
MAX_FINISHED = 40
MAX_FAILURES = 20
MAX_BLOCKERS = 15
#: How many held-back messages the digest lists, newest kept.
MAX_HELD = 40
_GIB = 1024**3


# -- resources (cheap reads only) -------------------------------------------
def _meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    """``/proc/meminfo`` in bytes; ``{}`` where there is none (not Linux)."""
    out: dict[str, int] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key.strip()] = int(parts[0]) * (1024 if len(parts) > 1 else 1)
    return out


def _usage(path: Path) -> dict[str, int] | None:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": u.total, "used": u.used, "free": u.free}


def gate(cfg: Config) -> dict | None:
    """The machine's build gate right now: its limit, every build alive on it
    with its swarm, and how many wait. The gate is shared by every swarm on the
    machine, so each holder says whether it is this swarm's (``mine``). ``None``
    when the gate cannot be read: that must never fail the digest."""
    try:
        snap = buildstatus.snapshot(cfg, n_recent=1)
    except Exception:  # noqa: BLE001 - the digest reports; it does not depend on the gate
        return None
    keys = ("swarm", "swarm_name", "mine", "phase", "state", "frozen", "running_s")
    queue = snap["queue"]
    return {
        "max_concurrent": snap["max_concurrent"],
        "holders": [{**{k: b.get(k) for k in keys}, "argv": str(b.get("argv") or "")[:80]}
                    for b in snap["builds"]],
        "queued": len(queue),
        "queued_mine": sum(bool(t.get("mine")) for t in queue),
    }


def neighbours(cfg: Config) -> list[dict]:
    """The other swarms on this machine, by name, each with its status
    (:attr:`machine.Swarm.status`): whoever else loads the box."""
    return [{"slug": s.slug, "name": s.name, "status": s.status}
            for s in machine.swarms(cfg.state_dir.parent) if s.slug != cfg.state_dir.name]


def resources(cfg: Config, meminfo: Path = Path("/proc/meminfo")) -> dict:
    """Free RAM, swap, ``/tmp`` and the state dir's filesystem, plus plain flags.
    The figures are the whole box's, whichever swarm is using it; whose load it
    is, the digest says beside them (:func:`gate`, :func:`neighbours`).

    Deliberately no ``du``: a walk of a large build cache can take longer than
    the whole pass is allowed, and the free space of the filesystem is what
    decides whether the next build fits. ``/tmp`` gets its own line because on
    this kind of box it is RAM (tmpfs) — a full ``/tmp`` is memory pressure.
    """
    mem = _meminfo(meminfo)
    snap: dict = {
        "mem_total": mem.get("MemTotal"),
        "mem_available": mem.get("MemAvailable"),
        "swap_total": mem.get("SwapTotal"),
        "swap_free": mem.get("SwapFree"),
        "tmp": _usage(Path("/tmp")),
        "state_fs": _usage(cfg.state_dir),
    }
    flags: list[str] = []
    total, avail = snap["mem_total"], snap["mem_available"]
    if total and avail is not None and avail < 0.10 * total:
        flags.append(f"RAM low: {avail / _GIB:.1f} GiB available of {total / _GIB:.1f} GiB")
    st, sf = snap["swap_total"], snap["swap_free"]
    if st and sf is not None and (st - sf) > 0.80 * st:
        flags.append(f"swap {100 * (st - sf) / st:.0f}% used")
    tmp = snap["tmp"]
    if tmp and tmp["total"] and tmp["used"] > 0.85 * tmp["total"]:
        flags.append(f"/tmp {100 * tmp['used'] / tmp['total']:.0f}% full")
    fs = snap["state_fs"]
    if fs and fs["total"] and (fs["free"] < 20 * _GIB or fs["free"] < 0.10 * fs["total"]):
        flags.append(f"state disk low: {fs['free'] / _GIB:.1f} GiB free")
    snap["flags"] = flags
    return snap


# -- the pieces ----------------------------------------------------------
def _sentinel_mtime(cfg: Config, phase: str, status: str) -> float:
    try:
        return os.path.getmtime(cfg.done_dir / f"{phase}.{status}")
    except OSError:
        return 0.0


def finished_since(cfg: Config, st: State, since: float) -> list[dict]:
    """Phases whose ``swarm done`` landed after ``since``, newest first, with
    everything they left behind: status, recap, completion note, notes."""
    rows: list[tuple[float, str, str]] = []
    for phase, status in st.done.items():
        at = _sentinel_mtime(cfg, phase, status)
        if at >= since:
            rows.append((at, phase, status))
    rows.sort(reverse=True)
    out: list[dict] = []
    for at, phase, status in rows[:MAX_FINISHED]:
        rec = recap_mod.load(cfg, phase)
        _, note = recap_mod.sentinel(cfg, phase)
        out.append(
            {
                "phase": phase,
                "status": status,
                "at": at,
                "recap": (rec.summary if rec else None) or "",
                "note": note,
                "notes": [{"kind": n.kind, "text": n.text} for n in notes_mod.load(cfg, phase)],
            }
        )
    return out


def failures(cfg: Config, st: State, waits: dict[str, str] | None = None) -> list[dict]:
    """The failed phases and their notes. A phase that finished ``later`` is
    not one (``waits``, :func:`ledgerw.dated`): it is listed by :func:`dated`."""
    done = ledgerw.not_failed(st.done, ledgerw.dated(cfg) if waits is None else waits)
    out = []
    for phase in sorted(p for p, s in done.items() if s == statuses.FAIL)[:MAX_FAILURES]:
        _, note = recap_mod.sentinel(cfg, phase)
        out.append({"phase": phase, "note": note})
    return out


def dated(waits: dict[str, str]) -> list[dict]:
    """Open rows waiting for a date, soonest first: the swarm starts each on
    its day, so none of them is a failure or a job for a pass."""
    return [{"phase": p, "until": d} for p, d in sorted(waits.items(), key=lambda kv: kv[::-1])]


def _workers(keys) -> list[str]:
    """The worker phases among ``waiting``/``parked`` keys (the rest are listed
    from their own records)."""
    return [k for k in keys if state_mod.waiter(k)[0] == state_mod.WORKER]


def owner_questions(cfg: Config, st: State, now: float) -> list[dict]:
    """Every question the owner has not answered yet, and how long it has waited.

    Only a session asking now is here (:meth:`state.State.on_owner`), aged from
    its latest question. A parked worker the owner has answered is working in its
    own window: :func:`working_parked` lists it."""
    out: list[dict] = []
    asking = _workers(st.on_owner())
    for phase in sorted(asking, key=lambda p: (p not in st.waiting, p)):
        asked = st.asked_at(phase, cfg.park_after)
        out.append({"who": phase, "state": "waiting" if phase in st.waiting else "parked",
                    "age_s": now - asked if asked is not None else None,
                    "question": doctor_mod.waiting_question(cfg, phase)})
    for item in opqueue.load_all(cfg):
        if item.state == opqueue.WAITING:
            key = state_mod.waiter_key(state_mod.OPERATOR, item.phase)
            out.append({"who": f"operator {item.phase}",
                        "state": "operator, parked" if key in st.parked else "operator",
                        "age_s": now - item.asked_at if item.asked_at else None,
                        "question": item.question})
    return out


def working_parked(st: State, now: float) -> list[dict]:
    """Parked sessions the owner has answered: each works on in its own window
    and waits on nobody, so none of them is a question for a pass to chase."""
    return [{"who": key, "window": state_mod.wait_window(key),
             "answered_s": now - st.answered[key]} for key in sorted(st.working_parked())]


#: How many operator outcomes the digest lists in full (flagged ones first).
MAX_OUTCOMES = 30


def operator_outcomes(items: list[opqueue.Item], since: float) -> list[dict]:
    """Every operator job finished since ``since``: ones that asked the owner
    first, then newest.

    An outcome with nothing for the owner is not sent to them, so this is how
    it reaches them: the Overseer's summary accounts for it.
    """
    done = [i for i in items if i.state == opqueue.DONE and i.done_at >= since]
    done.sort(key=lambda i: (not i.attention, -i.done_at))
    return [
        {"job": i.phase, "at": i.done_at, "attention": i.attention,
         "outcome": i.outcome[:400]}
        for i in done
    ]


def operator_summary(cfg: Config, st: State, since: float = 0.0) -> dict:
    items = opqueue.load_all(cfg)
    counts = {k: sum(1 for i in items if i.state == k) for k in opqueue.STATES}
    open_items = [
        {"job": i.phase, "state": i.state, "brief": i.note[:160]}
        for i in items
        if not i.terminal
    ]
    return {"enabled": cfg.operator_enabled, "counts": counts,
            "current": st.operator_phase, "open": open_items[:15],
            "finished": operator_outcomes(items, since)}


def in_flight(st: State, launching: set[str] | frozenset[str] = frozenset()) -> dict[str, str]:
    """``phase -> building|parked`` for the starvation map. ``parked`` is a phase
    asking the owner now, on its park timer or in its own window."""
    out = {s.phase: "building" for s in st.busy_slots() if s.phase}
    for p in st.integ_queue:
        out.setdefault(p, "building")
    if st.integ_blocked:
        out.setdefault(st.integ_blocked, "building")
    for p in launching:
        out.setdefault(p, "building")
    for p in st.working_parked():
        out[p] = "building"  # answered: it works on in its own window
    for p in st.on_owner():
        out[p] = "parked"
    return out


def lane_blockers(blockers: list[dict], waits: dict[str, dict]) -> list[dict]:
    """The starvation map's blockers plus, under lanes, one ``lane`` entry per
    phase whose lane holds ready rows back: each such row counts under its
    holder, with everything that row itself holds back. Most-blocking first."""
    behind = {b["phase"]: b["blocks"] for b in blockers}
    waiting: dict[str, list[str]] = {}
    for row, w in waits.items():
        waiting.setdefault(w["holder"], []).append(row)
    lanes = [
        {"phase": holder, "kind": "lane", "blocks": sum(1 + behind.get(r, 0) for r in rows),
         "examples": rows[:5]}
        for holder, rows in waiting.items()
    ]
    return sorted([*blockers, *lanes], key=lambda b: -b["blocks"])


def held_back(cfg: Config, since: float) -> dict:
    """What the swarm had to say since the last summary and held for the next
    one (:func:`telegram.fold`): the newest :data:`MAX_HELD`, oldest first."""
    rows = telegram.folded_since(cfg.state_dir, since)
    return {
        "since": since,
        "count": len(rows),
        "rows": [{"kind": r.get("kind"), "phase": r.get("phase"), "ts": r.get("ts"),
                  "text": telegram.clip(r.get("text") or "", 300)}
                 for r in rows[-MAX_HELD:]],
    }


def _finished_counts(cfg: Config, st: State, since: float) -> tuple[int, int]:
    """How many phases landed, and how many failed, after ``since``. A phase
    that finished ``later`` waits for its date: it is neither."""
    waits = ledgerw.dated(cfg)
    landed = failed = 0
    for phase, status in st.done.items():
        if _sentinel_mtime(cfg, phase, status) < since:
            continue
        if status in statuses.INTEGRATES:
            landed += 1
        elif status == statuses.FAIL and phase not in waits:
            failed += 1
    return landed, failed


def nothing_to_report(cfg: Config, st: State, since: float) -> bool:
    """Has the swarm stood still since ``since``? Nothing landed or failed,
    nothing was held back for the summary, and nothing is building. A summary
    then would only repeat the last one: what waits on the owner was asked of
    them when it started to."""
    if any(s.phase for s in st.busy_slots()):
        return False
    if any(_finished_counts(cfg, st, since)):
        return False
    return not telegram.folded_since(cfg.state_dir, since)


def own_summary(cfg: Config, st: State, since: float, now: float | None = None) -> str:
    """The summary the swarm writes when no Overseer pass wrote one: the counts
    since ``since``, what is running, and whether anything waits on the owner.
    Two sentences, built to fit :func:`telegram.room`."""
    now = time.time() if now is None else now
    landed, failed = _finished_counts(cfg, st, since)
    start = f"Since {time.strftime('%H:%M', time.localtime(since))}" if since else "So far"
    first = f"{start}: {landed} phase{'' if landed == 1 else 's'} landed"
    if failed:
        first += f", {failed} failed"
    running = sum(1 for s in st.busy_slots() if s.phase)
    first += f"; {running} building now"
    if st.usage_hold:
        first += ", and a usage cap holds new ones until it resets"
    elif st.paused:
        first += ", and the swarm is paused"
    asking = owner_mod.waits(cfg, st)
    second = f"Waiting on you: {', '.join(asking)}." if asking else "Nothing waits on you."
    return f"{first}. {second}"


# -- assembly ----------------------------------------------------------------
def build(
    cfg: Config,
    st: State,
    reasons: list[Reason],
    *,
    since: float,
    launching: set[str] | frozenset[str] = frozenset(),
    given_up: list[str] | None = None,
    last_pass: dict | None = None,
    now: float | None = None,
) -> dict:
    """Everything the digest says, as data."""
    now = time.time() if now is None else now
    ctx = build_context(cfg, st)
    graph = ledger_mod.load(cfg.project_dir / cfg.ledger)
    counts: dict[str, int] = {}
    for status in ledgerw.not_failed(st.done, ledgerw.dated(cfg)).values():
        counts[status] = counts.get(status, 0) + 1
    context = {
        "free_slots": ctx["free_slots"],
        "busy_slots": ctx["busy_slots"],
        "ready": ctx["ready"][:20],
        "ready_count": len(ctx["ready"]),
        "launchable": ctx["launchable"],
        "launching": sorted(launching),
        "given_up": list(given_up or []),
        "paused": st.paused,
        "usage_hold": bool(st.usage_hold),
        "waiting": ctx["waiting"],
        # Parked and asking now; the answered ones are working, listed apart.
        "parked": [k for k in st.parked if k not in st.answered],
        "parked_working": st.working_parked(),
        "integ_queue": list(st.integ_queue),
        "integ_blocked": (
            {"phase": st.integ_blocked, "kind": st.integ_blocked_kind,
             "repo": st.integ_blocked_repo}
            if st.integ_blocked else None
        ),
        "push_owed": pushowed.describe(st.push_owed, now),
        "ledger_issues": ctx["ledger_issues"][:10],
        "done_counts": counts,
        "ledger_phases": len(graph),
        "excluded": len(cfg.exclude),
    }
    flying = in_flight(st, launching)
    done = ledger_mod.with_ticked(
        st.done, ledger_mod.load_ticked(cfg.project_dir / cfg.ledger), flying
    )
    waits = ledgerw.dated(cfg)
    starve = starvation_map(graph, done, set(cfg.exclude), flying, dated=set(waits))
    starve["blockers"] = lane_blockers(starve["blockers"], ctx["lanes"].get("waits") or {})
    mine = ledger_mod.owner_rows(graph, done, set(cfg.exclude), set(flying))
    starve["blockers"] = starve["blockers"][:MAX_BLOCKERS]
    starve["cycle"] = starve["cycle"][:20]
    return {
        "generated_at": now,
        "since": since,
        # Whether this is the pass that writes the owner their summary, and how
        # long `swarm overseer-summary` lets it be.
        "summary_due": any(r.key == SUMMARY for r in reasons),
        "summary_room": telegram.room(cfg, telegram.SUMMARY_LEAD),
        "held_back": held_back(cfg, telegram.last_summary_at(cfg.state_dir) or since),
        "reasons": [
            {"key": r.key, "text": r.text, "urgent": r.urgent, "at": r.at} for r in reasons
        ],
        "context": context,
        "operator": operator_summary(cfg, st, since),
        "finished": finished_since(cfg, st, since),
        "failures": failures(cfg, st, waits),
        "dated": dated(waits),
        "owner": owner_questions(cfg, st, now),
        "owner_answered": working_parked(st, now),
        # Rows only the owner can do, ready, holding other rows up: the owner
        # was pinged about each and sees them under "Needs you".
        "owner_rows": [{"row": r, "blocks": n} for r, n in mine],
        "starvation": starve,
        # Lanes: phases that landed with files outside their lane.
        "undeclared": landing_mod.undeclared_since(cfg, since),
        "resources": {**resources(cfg), "gate": gate(cfg), "swarms": neighbours(cfg)},
        "last_pass": last_pass,
    }


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = max(0, int(seconds))
    if s < 5400:
        return f"{s // 60}m"
    return f"{s / 3600:.1f}h"


def _gib(n: int | None) -> str:
    return "?" if n is None else f"{n / _GIB:.1f} GiB"


def render(d: dict) -> str:
    """The markdown the session reads. Short sections, capped lists."""
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(d["generated_at"]))
    c = d["context"]
    out = [f"# Overseer digest — {when}", "", "## Why this pass"]
    out += [f"- {'[urgent] ' if r['urgent'] else ''}{r['text']}" for r in d["reasons"]] or ["- (manual)"]
    if d.get("summary_due"):
        out.append(
            "- this is the summary pass: before you sign off, send the owner their"
            f' summary with `swarm overseer-summary "<text>"` (two short sentences, at'
            f" most {d.get('summary_room')} characters)"
        )
    else:
        out.append(
            "- no summary is due on this pass (`swarm overseer-summary` would only be"
            " recorded). If something needs the owner, ask them with `swarm notify`"
        )
    out += ["", "## The swarm now"]
    out.append(
        f"- slots: {len(c['busy_slots'])} busy {c['busy_slots'] or ''}, {len(c['free_slots'])} free"
        + ("; PAUSED" if c["paused"] else "")
        + ("; HELD BY A USAGE CAP (the owner's to lift)" if c.get("usage_hold") else "")
    )
    out.append(f"- ready: {c['ready_count']} {c['ready'][:10]}; launchable now: {c['launchable']}")
    if c["launching"] or c["given_up"]:
        out.append(f"- launching: {c['launching']}; launch given up: {c['given_up']}")
    out.append(f"- waiting on owner: {c['waiting']}; parked: {c['parked']}"
               + (f"; answered and working in their own windows: {c['parked_working']}"
                  if c.get("parked_working") else ""))
    hold = c["integ_blocked"]
    out.append(
        f"- merge queue: {c['integ_queue']}"
        + (f"; HELD on {hold['phase']} ({hold['kind']} in {hold['repo']})" if hold else "")
    )
    out.append(f"- push owed: {'; '.join(c['push_owed']) or 'none'}")
    for u in d.get("undeclared") or []:
        out.append(f"- lane: {u['phase']} touched outside its declaration:"
                   f" {' '.join(u['paths'])[:300]}")
    if c["ledger_issues"]:
        out.append(f"- ledger issues: {'; '.join(c['ledger_issues'])}")
    out.append(
        f"- ledger: {c['ledger_phases']} phases, {c['excluded']} excluded; done: "
        + ", ".join(f"{k} {v}" for k, v in sorted(c["done_counts"].items()))
    )
    op = d["operator"]
    oc = op["counts"]
    out.append(
        f"- operator{'' if op['enabled'] else ' (OFF)'}: queued {oc.get('queued', 0)},"
        f" running {oc.get('running', 0)}, waiting {oc.get('waiting', 0)},"
        f" done {oc.get('done', 0)}, abandoned {oc.get('abandoned', 0)}"
        + (f"; current {op['current']}" if op["current"] else "")
    )
    for item in op["open"][:8]:
        out.append(f"  - {item['job']} [{item['state']}] {item['brief']}")

    since = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(d["since"])) if d["since"] else "the start"
    done = op.get("finished") or []
    flagged = sum(1 for o in done if o["attention"])
    out += ["", f"## Operator jobs finished since {since} ({len(done)}, {flagged} asked the owner)"]
    for o in done[:MAX_OUTCOMES]:
        mark = "**[asked the owner]** " if o["attention"] else ""
        out.append(f"- {mark}{o['job']}: {o['outcome'] or '(no outcome given)'}")
    if len(done) > MAX_OUTCOMES:
        out.append(f"- … and {len(done) - MAX_OUTCOMES} more")
    if not done:
        out.append("- none")
    out += ["", f"## Finished since {since} ({len(d['finished'])})"]
    for f in d["finished"]:
        out.append(f"- **{f['phase']}** {f['status']}: {f['recap'] or f['note'] or '(no recap)'}")
        if f["note"] and f["recap"] and f["note"] != f["recap"]:
            out.append(f"  - completion note: {f['note']}")
        for n in f["notes"]:
            out.append(f"  - {n['kind']}: {n['text']}")
    if not d["finished"]:
        out.append("- none")

    held = d.get("held_back") or {}
    out += ["", f"## Held back since the last summary ({held.get('count', 0)})",
            "None of these was sent to the owner. The summary accounts for the ones"
            " that matter to them, in a few words; it never lists them."]
    for h in held.get("rows") or []:
        who = f" {h['phase']}" if h.get("phase") else ""
        out.append(f"- [{h['kind']}{who}] {h['text']}")
    if held.get("count", 0) > len(held.get("rows") or []):
        out.append(f"- … and {held['count'] - len(held['rows'])} earlier")
    if not held.get("count"):
        out.append("- nothing")

    out += ["", f"## Failed phases ({len(d['failures'])})"]
    out += [f"- {f['phase']}: {f['note'] or '(no note)'}" for f in d["failures"]] or ["- none"]

    waits = d.get("dated") or []
    out += ["", f"## Waiting for a date ({len(waits)})"]
    out += ["The swarm starts each on its day, with the work it had committed. None of"
            " them failed: do not retry or reshape one for being here."] if waits else []
    out += [f"- {w['phase']}: until {w['until']}" for w in waits] or ["- none"]

    out += ["", "## Waiting on the owner"]
    out += [
        f"- {q['who']} ({q['state']}, {_age(q['age_s'])}): {q['question'] or '(question not recorded)'}"
        for q in d["owner"]
    ] or ["- nobody"]
    answered = d.get("owner_answered") or []
    if answered:
        out.append("Answered by the owner and working again, each in its own window (none"
                   " of them waits on anyone): "
                   + ", ".join(f"{a['who']} ({a['window']}, answered {_age(a['answered_s'])} ago)"
                               for a in answered))

    mine = d.get("owner_rows") or []
    out += ["", f"## Rows only the owner can do, holding others up ({len(mine)})"]
    if mine:
        out.append("The owner has been told about each and sees them under Needs you."
                   " For a review or pick they make at a keyboard, an operator job can"
                   " walk them through it (`swarm operator-add --phase <row>`).")
    out += [f"- {r['row']} holds up {r['blocks']}" for r in mine] or ["- none"]

    s = d["starvation"]
    out += ["", "## Starvation map",
            f"Open backlog {s['backlog']} (ready {len(s['ready'])}, blocked {s['blocked']})."
            " Root blockers, most-blocking first:"]
    for b in s["blockers"]:
        out.append(f"- {b['phase']} [{b['kind']}] blocks {b['blocks']}: {', '.join(b['examples'])}")
    if not s["blockers"]:
        out.append("- none")
    if s["cycle"]:
        out.append(f"- in a dependency cycle: {', '.join(s['cycle'])}")

    r = d["resources"]
    tmp, fs = r["tmp"] or {}, r["state_fs"] or {}
    out += ["", "## Resources",
            f"- RAM available {_gib(r['mem_available'])} of {_gib(r['mem_total'])};"
            f" swap free {_gib(r['swap_free'])} of {_gib(r['swap_total'])}",
            f"- /tmp {_gib(tmp.get('used'))} used of {_gib(tmp.get('total'))};"
            f" state disk {_gib(fs.get('free'))} free"]
    out += [f"- WARNING: {flag}" for flag in r["flags"]]
    out += _gate_lines(r["gate"])
    out.append("- other swarms on this machine (the figures above are the whole box's,"
               " theirs included): "
               + (", ".join(f"{s['name']} ({s['status']})" for s in r["swarms"]) or "none"))

    lp = d.get("last_pass")
    if lp:
        out += ["", "## Last pass",
                f"- {lp.get('id')} [{lp.get('status')}]: {lp.get('summary') or '(no summary)'}"]
        if lp.get("left"):
            out.append(f"- it left for the owner: {lp['left'][:400]}")
    return "\n".join(out) + "\n"


def _gate_lines(g: dict | None) -> list[str]:
    """The machine's build gate, one line plus one per build on it: whose each
    is, so a neighbour's build is not taken for this swarm's."""
    head = "- build gate (the machine's, shared by every swarm on it):"
    if g is None:
        return [f"{head} could not be read"]
    if g["max_concurrent"] < 1:
        return [f"{head} off, builds do not queue"]
    held = g["holders"]
    theirs = sum(bool(h["swarm"]) and not h["mine"] for h in held)
    out = [f"{head} {len(held)} build(s) on it, {theirs} of them another swarm's;"
           f" limit {g['max_concurrent']} at once;"
           f" {g['queued']} waiting, {g['queued_mine']} of them this swarm's"]
    for h in held:
        owner = ("this swarm" if h["mine"] else "a swarm its record does not name"
                 if not h["swarm"] else f"another swarm [{h['swarm_name'] or h['swarm']}]")
        notes = [{"yielded": "set aside as idle, does not count against the limit",
                  "left": "ended, a process it left still holds the seat"}.get(h["state"]),
                 "frozen with its swarm" if h["frozen"] else None]
        tail = "".join(f"; {n}" for n in notes if n)
        out.append(f"  - {owner}, {h['phase'] or '-'}, {_age(h['running_s'])}: {h['argv']}{tail}")
    return out


def write(cfg: Config, pass_id: str, data: dict) -> Path:
    """Write ``digest-<id>.md`` and its JSON twin; return the markdown path."""
    d = overseer_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    md = d / f"digest-{pass_id}.md"
    md.write_text(render(data), encoding="utf-8")
    (d / f"digest-{pass_id}.json").write_text(
        json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return md
