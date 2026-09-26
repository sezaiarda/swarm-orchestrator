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

from . import ask as ask_mod
from . import doctor as doctor_mod
from . import ledger as ledger_mod
from . import notes as notes_mod
from . import opqueue
from . import pushowed
from . import recap as recap_mod
from . import statuses, telegram
from .config import Config
from .master import build_context
from .overseer import SUMMARY_TRIGGERS, Reason, overseer_dir, starvation_map
from .state import State

#: How many recently finished phases the digest lists in full.
MAX_FINISHED = 40
MAX_FAILURES = 20
MAX_BLOCKERS = 15
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


def resources(cfg: Config, meminfo: Path = Path("/proc/meminfo")) -> dict:
    """Free RAM, swap, ``/tmp`` and the state dir's filesystem, plus plain flags.

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


def failures(cfg: Config, st: State) -> list[dict]:
    out = []
    for phase in sorted(p for p, s in st.done.items() if s == statuses.FAIL)[:MAX_FAILURES]:
        _, note = recap_mod.sentinel(cfg, phase)
        out.append({"phase": phase, "note": note})
    return out


def owner_questions(cfg: Config, st: State, now: float) -> list[dict]:
    """Every question the owner has not answered yet, and how long it has waited."""
    out: list[dict] = []
    for phase, deadline in sorted(st.waiting.items()):
        asked = float(deadline) - cfg.park_after
        out.append({"who": phase, "state": "waiting", "age_s": now - asked,
                    "question": doctor_mod.waiting_question(cfg, phase)})
    for phase in sorted(st.parked):
        out.append({"who": phase, "state": "parked", "age_s": None,
                    "question": doctor_mod.waiting_question(cfg, phase)})
    for item in opqueue.load_all(cfg):
        if item.state == opqueue.WAITING:
            out.append({"who": f"operator {item.phase}", "state": "operator",
                        "age_s": now - item.asked_at if item.asked_at else None,
                        "question": item.question})
    for ask in ask_mod.open_asks(cfg):
        out.append({"who": f"ask {ask.name} ({ask_mod.rows_text(ask.rows)})", "state": "ask",
                    "age_s": ask.age_s(now), "question": ask.why})
    return out


#: How many operator outcomes the digest lists in full (flagged ones first).
MAX_OUTCOMES = 30


def operator_outcomes(items: list[opqueue.Item], since: float) -> list[dict]:
    """Every operator job finished since ``since``: flagged ones first, then newest.

    Routine outcomes no longer ping the owner (``[operator].notify``), so this is
    how they reach them: the Overseer folds them into its summary.
    """
    done = [i for i in items if i.state == opqueue.DONE and i.done_at >= since]
    done.sort(key=lambda i: (not i.attention, -i.done_at))
    return [
        {"job": i.phase, "at": i.done_at, "attention": i.attention,
         "outcome": i.outcome[:400]}
        for i in done
    ]


def answered_asks(cfg: Config, since: float) -> list[dict]:
    """Every ask answered since ``since``, newest first.

    What the operator said has to reach something that acts on it. An ask session
    queues the follow-up work itself (``swarm operator-add``), but an answer
    that session left unacted on would otherwise be read by nobody: the Overseer
    sees each one here and picks up what is still owed.
    """
    done = [a for a in ask_mod.load_all(cfg) if not a.is_open and a.done_at >= since]
    done.sort(key=lambda a: -a.done_at)
    return [
        {"name": a.name, "rows": list(a.rows), "by": a.by, "at": a.done_at,
         "attention": a.attention, "question": (a.question or a.why)[:300],
         "outcome": a.outcome[:400]}
        for a in done[:MAX_OUTCOMES]
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
    """``phase -> building|parked`` for the starvation map."""
    out = {s.phase: "building" for s in st.busy_slots() if s.phase}
    for p in st.integ_queue:
        out.setdefault(p, "building")
    if st.integ_blocked:
        out.setdefault(st.integ_blocked, "building")
    for p in launching:
        out.setdefault(p, "building")
    for p in list(st.waiting) + list(st.parked):
        out[p] = "parked"
    return out


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
    for status in st.done.values():
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
        "waiting": ctx["waiting"],
        "parked": ctx["parked"],
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
    starve = starvation_map(graph, done, set(cfg.exclude), flying)
    unasked = ask_mod.owner_run_unasked(cfg, graph, done, set(flying))
    starve["blockers"] = starve["blockers"][:MAX_BLOCKERS]
    starve["cycle"] = starve["cycle"][:20]
    return {
        "generated_at": now,
        "since": since,
        # Whether this pass's `swarm notify` reaches the phone without --attention.
        "summary_sends": (
            telegram.sends_all(cfg)
            or any(r.key in SUMMARY_TRIGGERS for r in reasons)
        ),
        "reasons": [
            {"key": r.key, "text": r.text, "urgent": r.urgent, "at": r.at} for r in reasons
        ],
        "context": context,
        "operator": operator_summary(cfg, st, since),
        "finished": finished_since(cfg, st, since),
        "failures": failures(cfg, st),
        "owner": owner_questions(cfg, st, now),
        "answered": answered_asks(cfg, since),
        # Owner-run rows whose dependencies have landed and that no open ask
        # names: open an ask for the ones the owner answers at a keyboard.
        "owner_run_unasked": unasked,
        "starvation": starve,
        "resources": resources(cfg),
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
    if d.get("summary_sends", True):
        out.append("- your summary (`swarm notify`) goes to the owner's phone")
    else:
        out.append(
            "- your summary is recorded, not sent: not a cadence pass. Add `--attention`"
            " only if something needs the owner"
        )
    out += ["", "## The swarm now"]
    out.append(
        f"- slots: {len(c['busy_slots'])} busy {c['busy_slots'] or ''}, {len(c['free_slots'])} free"
        + ("; PAUSED" if c["paused"] else "")
    )
    out.append(f"- ready: {c['ready_count']} {c['ready'][:10]}; launchable now: {c['launchable']}")
    if c["launching"] or c["given_up"]:
        out.append(f"- launching: {c['launching']}; launch given up: {c['given_up']}")
    out.append(f"- waiting on owner: {c['waiting']}; parked: {c['parked']}")
    hold = c["integ_blocked"]
    out.append(
        f"- merge queue: {c['integ_queue']}"
        + (f"; HELD on {hold['phase']} ({hold['kind']} in {hold['repo']})" if hold else "")
    )
    out.append(f"- push owed: {'; '.join(c['push_owed']) or 'none'}")
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
    out += ["", f"## Operator jobs finished since {since} ({len(done)}, {flagged} flagged)"]
    for o in done[:MAX_OUTCOMES]:
        mark = "**[needs the owner]** " if o["attention"] else ""
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

    out += ["", f"## Failed phases ({len(d['failures'])})"]
    out += [f"- {f['phase']}: {f['note'] or '(no note)'}" for f in d["failures"]] or ["- none"]

    out += ["", "## Waiting on the owner"]
    out += [
        f"- {q['who']} ({q['state']}, {_age(q['age_s'])}): {q['question'] or '(question not recorded)'}"
        for q in d["owner"]
    ] or ["- nobody"]

    answered = d.get("answered") or []
    out += ["", f"## Asks the owner answered since {since} ({len(answered)})"]
    if answered:
        out.append("Check each answer was acted on (a row recorded, a follow-up job"
                   " queued); queue what is still owed with `swarm operator-add`.")
    for a in answered:
        mark = "**[needs the owner]** " if a["attention"] else ""
        out.append(f"- {mark}{a['name']} ({', '.join(a['rows'])}, by {a['by']}):"
                   f" asked {a['question']} — answered: {a['outcome'] or '(no outcome given)'}")
    if not answered:
        out.append("- none")

    unasked = d.get("owner_run_unasked") or []
    out += ["", f"## Owner-run rows ready, no ask open ({len(unasked)})"]
    if unasked:
        out.append("Their dependencies have landed and no ask names them. Open one"
                   " (`swarm ask`) for a review or pick the owner makes at a keyboard;"
                   " leave the physical ones and name them in your summary.")
    out += [f"- {row}" for row in unasked] or ["- none"]

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

    lp = d.get("last_pass")
    if lp:
        out += ["", "## Last pass",
                f"- {lp.get('id')} [{lp.get('status')}]: {lp.get('summary') or '(no summary)'}"]
        if lp.get("left"):
            out.append(f"- it left for the owner: {lp['left'][:400]}")
    return "\n".join(out) + "\n"


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
