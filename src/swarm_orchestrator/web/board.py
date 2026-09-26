"""The web board's data: every ledger phase in one column, and the header above.

Pure. Everything read from disk arrives already loaded — the TUI's
:class:`~swarm_orchestrator.tui.dash.Dash` (state snapshot, sentinels, recaps,
notes, operator queue, history, meters, usage), the raw state dict, the parsed
ledger rows and the Overseer passes — so the same inputs always give the same
board, and the server can build it once per change for every client.

**Every** row of the ledger is on the board, not only what is launchable: the
the board shows future work, so blocked rows sit in *Blocked* with the root
that holds them, and owner-run rows in *Excluded*. A phase lands in exactly one
column, chosen by the first rule that fits, in this order — the order in which
a person would want to be told:

1. **Needs you** — a worker waiting or parked on a question, a retired
   ``needs-owner`` finish, an operator job asking (or given up), or an
   owner-run row that holds other rows up.
2. **Building** — it holds a slot.
3. **Merging / held** — in the merge queue, holding it, or pushed-but-owed.
4. **Operator** — a hand-off job queued or running for it.
5. **Done** — landed (``ok`` / ``operator`` / ``skip``), or ticked in the
   ledger (an owner-run row the owner ticked too); the card says which.
6. **Excluded** — in ``[tasks].exclude`` (owner-run rows), and not done.
7. **Failed** — recorded ``fail``.
8. **Blocked** — a dependency has not landed; the card names the root.
9. **Ready** — nothing stands in its way.

Dependency satisfaction is the launcher's own (:data:`statuses.SATISFIES_DEPS`
over :func:`ledger.with_ticked`'s view of the ``done`` map), so *Blocked* here is exactly what the supervisor will not
launch — the board can never disagree with the scheduler about what is ready.
"""

from __future__ import annotations

import time
from statistics import median

from .. import bigpic, ledger, opqueue, statuses
from ..drain import line as drain_line
from ..overseer import starvation_map
from ..tui.campaign import campaign_of
from ..tui.data import (
    five_outlook, forecast, kept_rows, limit_outlook, typical_durations,
)
from .rows import clip

NEEDS_YOU = "needs_you"
BLOCKED = "blocked"
READY = "ready"
BUILDING = "building"
MERGING = "merging"
OPERATOR = "operator"
DONE = "done"
FAILED = "failed"
EXCLUDED = "excluded"

#: ``(key, title, one line of what the column means)``, in board order: the
#: pipeline left to right, with what needs a person first.
COLUMNS: tuple[tuple[str, str, str], ...] = (
    (NEEDS_YOU, "Needs you", "waiting on your answer"),
    (BLOCKED, "Blocked", "a dependency has not landed"),
    (READY, "Ready", "launches when a slot frees"),
    (BUILDING, "Building", "a worker holds a slot"),
    (MERGING, "Merging / held", "finished, landing on main"),
    (OPERATOR, "Operator", "a side job runs after it"),
    (DONE, "Done", "landed"),
    (FAILED, "Failed", "attempted; its branch was discarded"),
    (EXCLUDED, "Excluded", "owner-run, never launched"),
)
#: Columns whose phases are still owed work — what the ETA counts.
OPEN = frozenset({NEEDS_YOU, BLOCKED, READY, BUILDING, MERGING})
#: Phases a campaign is *moving* on: the swimlane view floats these first.
ACTIVE = frozenset({NEEDS_YOU, BUILDING, MERGING, OPERATOR})

_QUESTION_CHARS = 400
_DOING_CHARS = 240
_NOTE_CHARS = 1500


def build(cfg, dash, *, state: dict | None, rows: dict, metas: dict, passes: list,
          turns: dict | None = None, now: float | None = None) -> dict:
    """The whole board as a JSON-ready dict.

    ``rows`` is :func:`web.rows.parse`'s ``{id: Row}``, ``metas`` the
    :func:`web.campaigns.describe` map, ``passes`` Overseer
    :class:`~swarm_orchestrator.ovrecord.PassRecord` s newest first, and
    ``turns`` a busy phase's last captured turn ``{phase: {"ts", "text"}}``.
    """
    now = time.time() if now is None else now
    state = state or {}
    snap = dash.snapshot
    graph = dict(dash.graph or {})
    for pid in rows:
        graph.setdefault(pid, set())
    done = dict(snap.done)
    excluded = set(getattr(cfg, "exclude", None) or [])
    waiting = dict(state.get("waiting") or {})
    parked = list(state.get("parked") or [])
    busy = {s.phase: s for s in snap.slots if s.busy and s.phase}
    queue = [p for p, _ in snap.integ_queue]
    owed = {str(rec.get("phase")): (repo, rec)
            for repo, rec in (state.get("push_owed") or {}).items() if isinstance(rec, dict)}
    questions = {b.phase: b for b in snap.blockers}
    jobs: dict[str, list] = {}
    for item in snap.operator or []:
        jobs.setdefault(opqueue.owning_phase(item.phase), []).append(item)
    flying = _in_flight(snap, waiting, parked)
    # The launcher's view: a ticked row with no record of ours has landed.
    view = ledger.with_ticked(done, {p for p, r in rows.items() if r.checked}, flying)
    satisfied = {p for p, s in view.items() if s in statuses.SATISFIES_DEPS}

    starve = starvation_map(graph, view, excluded, flying, examples=len(graph) + 1)
    roots_of: dict[str, list[dict]] = {}
    for b in starve["blockers"]:
        for p in b["examples"]:
            roots_of.setdefault(p, []).append(b)

    cards: dict[str, dict] = {}
    for pid in graph:
        row = rows.get(pid)
        card = {"id": pid, "c": campaign_of(pid), "t": row.title if row else ""}
        if row is not None and row.dirs:
            card["r"] = row.dirs
        col, extra = _place(pid, graph, view, satisfied, excluded, waiting, parked, busy,
                            queue, snap, owed, jobs, questions, roots_of)
        card["col"] = col
        card.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
        cards[pid] = card
    _building_extras(cards, busy, dash, turns or {})
    _done_times(cards, dash)
    extra_cards = _job_cards(jobs, graph) + _pass_cards(passes)

    columns = []
    for key, title, hint in COLUMNS:
        mine = [c for c in cards.values() if c["col"] == key] + [
            c for c in extra_cards if c["col"] == key]
        columns.append({"key": key, "title": title, "hint": hint,
                        "cards": _order(key, mine, queue, graph)})

    return {
        "generated_at": now,
        "project": getattr(cfg, "project_dir", None) and cfg.project_dir.name,
        "header": _header(cfg, dash, cards, extra_cards, passes, now),
        "columns": columns,
        "campaigns": _campaigns(cards, metas),
        "activity": _activity(dash, passes),
        # Landed rows stall nothing, so a cycle through one is history, not an issue.
        "issues": ledger.validate(graph, satisfied)[:20],
        "cycle": starve.get("cycle", [])[:50],
        "kept": _kept(dash, now),
    }


#: What the board says about a kept process. Not its command line, cwd or log
#: path: the one line of why is what a phone needs, and the rest stays on the box.
_KEPT_KEYS = ("name", "why", "alive", "pid", "started_at", "by", "stop", "stale")


def _kept(dash, now: float) -> list[dict]:
    """``swarm keep`` records, read-only — the processes left running on purpose."""
    return [{k: row[k] for k in _KEPT_KEYS}
            for row in kept_rows(getattr(dash, "kept", None) or [], now)]


def _in_flight(snap, waiting: dict, parked: list) -> dict[str, str]:
    """``phase -> building|parked``, the shape :func:`starvation_map` takes.

    Mirrors :func:`ovdigest.in_flight`, which reads a typed ``State``; the board
    holds the dashboard's normalised snapshot instead (a state file from a newer
    build would not load as a ``State``).
    """
    out = {s.phase: "building" for s in snap.slots if s.busy and s.phase}
    for p, _ in snap.integ_queue:
        out.setdefault(p, "building")
    if snap.integ_blocked:
        out.setdefault(snap.integ_blocked, "building")
    for p in list(waiting) + list(parked):
        out[p] = "parked"
    return out


def _place(pid, graph, done, satisfied, excluded, waiting, parked, busy, queue, snap,
           owed, jobs, questions, roots_of) -> tuple[str, dict]:
    """``(column, card extras)`` for one ledger phase — the rules in the module doc.

    ``done`` is the launcher's view (:func:`ledger.with_ticked`), so a ticked row
    is Done here exactly when the launcher treats it as landed — excluded or not.
    """
    status = done.get(pid)
    mine = jobs.get(pid, [])
    ask = next((j for j in mine if j.state == opqueue.WAITING), None)
    gave_up = next((j for j in mine if j.state == opqueue.ABANDONED), None)
    blocker = questions.get(pid)
    if pid in waiting:
        return NEEDS_YOU, {"sub": "waiting on you (holds its slot)",
                           "q": clip(blocker.question if blocker else "", _QUESTION_CHARS),
                           "since": blocker.since if blocker else None,
                           "parks_at": waiting.get(pid)}
    if pid in parked:
        return NEEDS_YOU, {"sub": "parked on your answer",
                           "q": clip(blocker.question if blocker else "", _QUESTION_CHARS),
                           "since": blocker.since if blocker else None}
    if status == statuses.NEEDS_OWNER:
        return NEEDS_YOU, {"sub": "finished; needs you",
                           "q": clip(blocker.question if blocker else "", _QUESTION_CHARS)}
    if ask is not None or (gave_up is not None and gave_up.asked):
        job = ask or gave_up
        return NEEDS_YOU, {"sub": "operator job asks you", "job": job.phase,
                           "q": clip(job.question or job.last_error, _QUESTION_CHARS),
                           "since": job.asked_at or job.queued_at or None}
    if gave_up is not None:
        return NEEDS_YOU, {"sub": "operator gave up", "job": gave_up.phase,
                           "q": clip(gave_up.last_error or gave_up.note, _QUESTION_CHARS)}
    if pid in busy:
        return BUILDING, {"slot": busy[pid].id}
    if snap.integ_blocked == pid:
        kind = snap.integ_blocked_kind or "conflict"
        return MERGING, {"sub": f"merge held: {kind}", "held": True}
    if pid in queue:
        pos = queue.index(pid)
        return MERGING, {"sub": "next to merge" if pos == 0 else f"merge queue #{pos + 1}"}
    if pid in owed:
        return MERGING, {"sub": "merged; push owed", "held": True}
    live = [j for j in mine if j.state in (opqueue.QUEUED, opqueue.RUNNING)]
    if live:
        return OPERATOR, {"sub": f"operator {live[0].state}", "job": live[0].phase}
    if status in statuses.SATISFIES_DEPS:
        if status == statuses.SKIP:
            sub = "skipped"
        elif status == statuses.LEDGER:
            sub = "ticked in the ledger"
        elif status == statuses.OPERATOR:
            sub = "built · operator done" if mine and all(
                j.state == opqueue.DONE for j in mine) else "built · operator"
        else:
            sub = "built"
        return DONE, {"sub": sub, "st": status}
    if blocker is not None and blocker.kind == "owner-row":
        return NEEDS_YOU, {"sub": "only you can do this", "q": blocker.question}
    if pid in excluded:
        return EXCLUDED, {"sub": "owner-run"}
    if status == statuses.FAIL:
        return FAILED, {"st": status}
    unmet = sorted(d for d in graph.get(pid, ()) if d not in satisfied)
    if unmet:
        roots = sorted(roots_of.get(pid, []), key=lambda b: -b["blocks"])
        extra = {"unmet": len(unmet), "needs": unmet[:6]}
        if roots:
            extra.update(root=roots[0]["phase"], root_kind=roots[0]["kind"],
                         root_blocks=roots[0]["blocks"])
            if len(roots) > 1:
                extra["roots"] = [b["phase"] for b in roots[:6]]
        else:
            extra["sub"] = "dependency cycle"
        return BLOCKED, extra
    return READY, {}


def _building_extras(cards: dict, busy: dict, dash, turns: dict) -> None:
    """Elapsed, context and the worker's own last words, for every busy card."""
    meters = getattr(dash, "meters", None) or {}
    # The phase every busy card is measured against (the client draws elapsed
    # from ``since`` itself, so the board does not change every second).
    seen = typical_durations(getattr(dash, "eta_runs", None) or [])
    typical = median(seen) if seen else None
    for pid, slot in busy.items():
        card = cards.get(pid)
        if card is None:
            continue
        m = meters.get(pid)
        if slot.started_at:
            card["since"] = slot.started_at
        if m is not None:
            if m.context_tokens and m.context_window:
                card["ctx"] = round(min(100.0, 100.0 * m.context_tokens / m.context_window), 1)
            card["last"] = m.ts or None
        turn = turns.get(pid) or {}
        if turn.get("text"):
            card["doing"] = clip(str(turn["text"]), _DOING_CHARS)
            card["last"] = max(card.get("last") or 0, float(turn.get("ts") or 0)) or None
        if typical is not None:
            card["typical"] = typical


def _done_times(cards: dict, dash) -> None:
    """When each finished phase finished: its sentinel's mtime, else the log."""
    sentinels = getattr(dash, "sentinels", None) or {}
    ended = {}
    for run in getattr(dash, "history", None) or []:
        if run.ended_at and run.phase not in ended:
            ended[run.phase] = run.ended_at
    for pid, card in cards.items():
        if card["col"] not in (DONE, FAILED, OPERATOR, MERGING):
            continue
        s = sentinels.get(pid)
        at = ended.get(pid) or (s.mtime if s else None)
        if at:
            card["at"] = at


def _job_cards(jobs: dict, graph: dict) -> list[dict]:
    """Operator jobs that belong to no ledger phase (``swarm operator-add``)."""
    out = []
    for owner, items in jobs.items():
        if owner in graph:
            continue
        for job in items:
            base = {"id": job.phase, "kind": "job", "c": "operator",
                    "t": clip(job.note, 160), "job": job.phase}
            if job.state == opqueue.WAITING or (job.state == opqueue.ABANDONED and job.asked):
                out.append({**base, "col": NEEDS_YOU, "sub": "operator job asks you",
                            "q": clip(job.question or job.last_error, _QUESTION_CHARS)})
            elif job.state == opqueue.ABANDONED:
                out.append({**base, "col": NEEDS_YOU, "sub": "operator gave up",
                            "q": clip(job.last_error, _QUESTION_CHARS)})
            elif job.state in (opqueue.QUEUED, opqueue.RUNNING):
                out.append({**base, "col": OPERATOR, "sub": f"operator {job.state}"})
    return out


def _pass_cards(passes: list) -> list[dict]:
    """An Overseer pass waiting on the owner is a question like any other."""
    out = []
    for rec in passes or []:
        if rec.status == "running" and rec.question and not rec.answer:
            out.append({"id": f"overseer:{rec.id}", "kind": "pass", "c": "overseer",
                        "t": "the Overseer asks you", "col": NEEDS_YOU,
                        "q": clip(rec.question, _QUESTION_CHARS),
                        "since": rec.asked_at or rec.started_at or None})
    return out


def _order(key: str, cards: list[dict], queue: list[str], graph: dict) -> list[dict]:
    """Ledger order (the owner's build order) except where time says more."""
    pos = {p: i for i, p in enumerate(graph)}
    if key in (DONE, FAILED):
        return sorted(cards, key=lambda c: (-(c.get("at") or 0), pos.get(c["id"], 1 << 30)))
    if key == NEEDS_YOU:
        return sorted(cards, key=lambda c: (c.get("since") or 0, c["id"]))
    if key == BUILDING:
        return sorted(cards, key=lambda c: c.get("slot", 0))
    if key == MERGING:
        return sorted(cards, key=lambda c: (queue.index(c["id"]) if c["id"] in queue else -1))
    return sorted(cards, key=lambda c: (pos.get(c["id"], 1 << 30), c["id"]))


def _campaigns(cards: dict, metas: dict) -> list[dict]:
    """Campaign header cards: what it is, its progress, its phases by column."""
    groups: dict[str, dict] = {}
    for order, (pid, card) in enumerate(cards.items()):
        name = card["c"]
        g = groups.setdefault(name, {"name": name, "first": order, "open_at": 1 << 30,
                                     "counts": {}, "done": 0, "total": 0})
        col = card["col"]
        if col in OPEN or col == OPERATOR:
            # Where its next work sits in the file: the swarm launches ready rows
            # in ledger order, so this is the owner's own build order.
            g["open_at"] = min(g["open_at"], order)
        g["counts"][col] = g["counts"].get(col, 0) + 1
        if col == EXCLUDED:
            continue
        g["total"] += 1
        if col == DONE:
            g["done"] += 1
    out = []
    for name, g in groups.items():
        meta = metas.get(name)
        counts = g["counts"]
        active = any(counts.get(k) for k in ACTIVE)
        open_ = sum(counts.get(k, 0) for k in OPEN)
        out.append({
            "name": name,
            "what": meta.what if meta else "",
            "about": meta.about if meta else "",
            "adr": meta.adr if meta else "",
            "done": g["done"],
            "total": g["total"],
            "counts": counts,
            "active": active,
            "open": open_,
            "rank": (0 if active else 1 if counts.get(READY) else 2 if open_ else 3,
                     g["open_at"], g["first"]),
        })
    out.sort(key=lambda c: c["rank"])
    for c in out:
        c.pop("rank")
    return out


def _header(cfg, dash, cards: dict, extra: list, passes: list, now: float) -> dict:
    snap = dash.snapshot
    cols: dict[str, int] = {}
    for c in list(cards.values()) + extra:
        cols[c["col"]] = cols.get(c["col"], 0) + 1
    workers = len(snap.slots) or int(getattr(cfg, "max_workers", 0) or 0)
    remaining = sum(1 for c in cards.values() if c["col"] in OPEN)
    seconds, label = forecast(getattr(dash, "eta_runs", None) or [], remaining, workers,
                              running=cols.get(BUILDING, 0), ready=cols.get(READY, 0))
    # Seconds, not a clock time: the client adds them to ``generated_at``, so a
    # quiet board does not change (and wake every phone) just because time passed.
    eta = {"remaining": remaining, "label": label, "seconds": seconds,
           "from_history": bool(getattr(dash, "eta_from_history", False))}
    run = getattr(dash, "run", None) or None
    usage = getattr(dash, "usage", None) or {}
    last = passes[0] if passes else None
    return {
        "counts": cols,
        "needs_you": cols.get(NEEDS_YOU, 0),
        "slots": {"busy": sum(1 for s in snap.slots if s.busy), "total": workers},
        "paused": bool(snap.paused),
        "usage_hold": snap.usage_hold,
        "drain": drain_line(snap.drain),
        "finished": bool(snap.finished),
        "running": bool(snap.ok and snap.supervisor_alive),
        "state": snap.reason if not snap.ok else "",
        "last_event_at": snap.last_event_at,
        "run": {
            "id": run.get("run_id"), "started": run.get("epoch_ts"),
            "hours": usage.get("hours"), "finished": usage.get("phases_finished"),
            "failed": usage.get("phases_failed"), "usd": usage.get("usd"),
        } if run else None,
        "usage": _usage(getattr(dash, "limits", None), usage, seconds, now),
        "eta": eta,
        "last_pass": {"id": last.id, "status": last.status, "started_at": last.started_at,
                      "ended_at": last.ended_at, "summary": clip(last.summary, 300)}
        if last else None,
        "big_picture": bigpic.web_view(cfg, bigpic.load(cfg)),
    }


def _usage(limits, usage: dict, finish_in: float | None, now: float) -> dict:
    """The 5-hour and weekly windows: now, pace, runway, and the TUI's verdict."""
    out = {}
    for which, pct, resets, pace in (
        ("five", getattr(limits, "five_pct", None), getattr(limits, "five_resets_at", None),
         usage.get("five_pct_per_h")),
        ("week", getattr(limits, "week_pct", None), getattr(limits, "week_resets_at", None),
         usage.get("week_pct_per_h")),
    ):
        if which == "five":
            text, tone = five_outlook(limits, pace, now)
        elif usage:
            text, tone = limit_outlook(limits, finish_in, now, pace=pace, pace_label=" this run")
        else:
            text, tone = limit_outlook(limits, finish_in, now)
        runway = ((100 - pct) / pace) if (pct is not None and pace and pace > 0) else None
        out[which] = {"pct": pct, "resets_at": resets, "per_h": pace,
                      "runway_h": runway, "text": text, "tone": tone}
    return out


def _activity(dash, passes: list) -> dict:
    """The Activity view: Overseer passes, then recent finishes with recaps."""
    out_passes = []
    for rec in (passes or [])[:12]:
        out_passes.append({
            "id": rec.id, "status": rec.status, "started_at": rec.started_at,
            "ended_at": rec.ended_at,
            "reasons": [str(r.get("text") or r.get("key") or "")
                        for r in (rec.reasons or []) if isinstance(r, dict)][:8],
            "summary": clip(rec.summary, _NOTE_CHARS), "saw": rec.saw[:_NOTE_CHARS * 2],
            "did": rec.did[:_NOTE_CHARS * 2], "left": rec.left[:_NOTE_CHARS * 2],
            "question": clip(rec.question, _QUESTION_CHARS), "answer": clip(rec.answer, 300),
        })
    finished = []
    for run in getattr(dash, "history", None) or []:
        if run.running or not run.ended_at:
            continue
        finished.append({"id": run.phase, "c": campaign_of(run.phase), "st": run.status,
                         "at": run.ended_at, "took": run.duration_s if run.started_at else None,
                         "recap": clip(run.summary, 500), "note": clip(run.note, 500)})
        if len(finished) >= 30:
            break
    return {"passes": out_passes, "finished": finished}
