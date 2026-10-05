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
2. **Building** — it holds a slot, or it is parked and the owner has answered
   it: it works on in its own window, and the card says which.
3. **Merging / held** — in the merge queue, holding it, or pushed-but-owed.
4. **Operator** — a hand-off job queued or running for it.
5. **Done** — landed (``ok`` / ``operator`` / ``skip``), or ticked in the
   ledger (an owner-run row the owner ticked too); the card says which.
6. **Excluded** — in ``[tasks].exclude`` (owner-run rows), and not done.
7. **Failed** — recorded ``fail``.
8. **Blocked** — a dependency has not landed; the card names the root. A row
   that waits for a date still ahead (:func:`ledgerw.dated`, as the dash read
   it) is blocked too, and says until when; so does a card behind such a row.
   A ``later`` finish is one of these from the moment it is reported, never
   *Failed*: the dash's snapshot drops its record (:func:`ledgerw.not_failed`).
9. **Ready** — nothing stands in its way.

Dependency satisfaction is the launcher's own (:data:`statuses.SATISFIES_DEPS`
over :func:`ledger.with_ticked`'s view of the ``done`` map), so *Blocked* here is exactly what the supervisor will not
launch — the board can never disagree with the scheduler about what is ready.
"""

from __future__ import annotations

import time
from statistics import median

from .. import bigpic, ledger, opqueue, statuses
from .. import models as models_mod
from ..drain import line as drain_line
from ..freezer import line as frozen_line
from .. import restart as restart_mod
from ..overseer import starvation_map
from ..tui import books as books_mod
from ..tui.campaign import campaign_of
from ..tui.data import (
    LOST, five_outlook, held_merge, kept_rows, limit_outlook, run_word, typical_durations,
)
from . import lifecycle, usagechart
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
    (NEEDS_YOU, "Needs you", "waiting on you"),
    (BLOCKED, "Blocked", "waits for work that is not built yet"),
    (READY, "Ready", "starts when a worker is free"),
    (BUILDING, "Building", "a worker is on it"),
    (MERGING, "Merging / held", "finished, landing on main"),
    (OPERATOR, "Operator", "a follow-up job runs after it"),
    (DONE, "Done", "built and on main"),
    (FAILED, "Failed", "tried; its work was set aside"),
    (EXCLUDED, "Excluded", "yours to do; the swarm never starts these"),
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
    # Parked sessions the owner has answered (``State.answered``): at work again.
    answered = dict(state.get("answered") or {})
    busy = {s.phase: s for s in snap.slots if s.busy and s.phase}
    queue = [p for p, _ in snap.integ_queue]
    owed = {str(rec.get("phase")): (repo, rec)
            for repo, rec in (state.get("push_owed") or {}).items() if isinstance(rec, dict)}
    questions = {b.phase: b for b in snap.blockers}
    dated = dict(getattr(dash, "deferred", None) or {})
    jobs: dict[str, list] = {}
    for item in snap.operator or []:
        jobs.setdefault(opqueue.owning_phase(item.phase), []).append(item)
    flying = _in_flight(snap, waiting, parked, answered)
    # The launcher's view: a ticked row with no record of ours has landed.
    view = ledger.with_ticked(done, {p for p, r in rows.items() if r.checked}, flying)
    satisfied = {p for p, s in view.items() if s in statuses.SATISFIES_DEPS}

    starve = starvation_map(graph, view, excluded, flying, examples=len(graph) + 1,
                            dated=set(dated))
    roots_of: dict[str, list[dict]] = {}
    for b in starve["blockers"]:
        for p in b["examples"]:
            roots_of.setdefault(p, []).append(b)

    own_model, named, by_model = _row_models(cfg, rows)
    cards: dict[str, dict] = {}
    for pid in graph:
        row = rows.get(pid)
        card = {"id": pid, "c": campaign_of(pid), "t": row.title if row else ""}
        if row is not None and row.dirs:
            card["r"] = row.dirs
        if by_model:
            card["m"] = named.get(pid) or own_model or "default"
        col, extra = _place(pid, graph, view, satisfied, excluded, waiting, parked, busy,
                            queue, snap, owed, jobs, questions, roots_of, dated, answered)
        card["col"] = col
        card.update({k: v for k, v in extra.items() if v not in (None, "", [], {})})
        cards[pid] = card
    started = {pid: slot.started_at for pid, slot in busy.items()}
    started.update(_working_since(dash, [p for p in parked if p in answered]))
    _building_extras(cards, started, dash, turns or {})
    _done_times(cards, dash)
    _row_etas(cards, getattr(dash, "forecast", None))
    extra_cards = _job_cards(jobs, graph, parked) + _pass_cards(passes, parked)

    columns = []
    for key, title, hint in COLUMNS:
        mine = [c for c in cards.values() if c["col"] == key] + [
            c for c in extra_cards if c["col"] == key]
        columns.append({"key": key, "title": title, "hint": hint,
                        "cards": _order(key, mine, queue, graph)})

    header = _header(cfg, dash, cards, extra_cards, passes, now)
    if by_model:
        header["models"] = _model_counts(cfg, cards, own_model)
    return {
        "generated_at": now,
        "project": lifecycle.display_name(cfg),
        "header": header,
        "columns": columns,
        "campaigns": _campaigns(cards, metas, getattr(dash, "forecast", None), now),
        "activity": _activity(dash, passes),
        # Landed rows stall nothing, so a cycle through one is history, not an issue.
        "issues": ledger.validate(graph, satisfied)[:20],
        "cycle": starve.get("cycle", [])[:50],
        "kept": _kept(dash, now),
    }


def _row_models(cfg, rows: dict) -> tuple[str, dict[str, str], bool]:
    """``(the swarm's own model, {row: model}, whether any row names a model)``:
    the rows whose ``model:`` names another model than the swarm's own and that
    were not handed back. A ledger that names no model shows none."""
    if not getattr(cfg, "worker_cmd", None) or not getattr(cfg, "state_dir", None):
        return "", {}, False
    own = models_mod.default(cfg)
    gone = models_mod.handed_up(cfg)
    named = {p: r.model for p, r in rows.items()
             if getattr(r, "model", "") and r.model != own and p not in gone}
    return own, named, any(getattr(r, "model", "") for r in rows.values())


def _model_counts(cfg, cards: dict, own: str) -> dict:
    """How the rows split by the model that builds them: built, at work, to go.
    A row handed back counts under the swarm's own model, which built (or will
    build) it; ``handed_up`` says how many were."""
    by: dict[str, dict[str, int]] = {}
    for c in cards.values():
        if c["col"] == EXCLUDED:
            continue
        acc = by.setdefault(c.get("m") or own or "default", {"done": 0, "running": 0, "left": 0})
        if c["col"] in (DONE, OPERATOR):
            acc["done"] += 1
        elif c["col"] in (BUILDING, MERGING):
            acc["running"] += 1
        else:
            acc["left"] += 1
    order = sorted(by, key=lambda m: (m != (own or "default"), m))
    return {"own": own or "default", "handed_up": len(models_mod.handed_up(cfg)),
            "rows": [{"model": m, **by[m]} for m in order]}


#: What the board says about a kept process. Not its command line, cwd or log
#: path: the one line of why is what a phone needs, and the rest stays on the box.
_KEPT_KEYS = ("name", "why", "alive", "pid", "started_at", "by", "stop", "stale")


def _kept(dash, now: float) -> list[dict]:
    """``swarm keep`` records, read-only — the processes left running on purpose."""
    return [{k: row[k] for k in _KEPT_KEYS}
            for row in kept_rows(getattr(dash, "kept", None) or [], now)]


def _row_etas(cards: dict, fc) -> None:
    """Each open row's own finish range, ``[p50, p85]``, from the forecast."""
    for book in getattr(fc, "books", None) or ():
        for row in book.rows:
            card, r = cards.get(row.id), row.finish
            if card is None or r is None or r.p50 == float("inf"):
                continue
            card["eta"] = [round(r.p50), round(r.p85) if r.p85 != float("inf") else None]


def _in_flight(snap, waiting: dict, parked: list, answered=()) -> dict[str, str]:
    """``phase -> building|parked``, the shape :func:`starvation_map` takes.
    ``parked`` is a session that waits on the owner; a parked one the owner has
    ``answered`` is building.

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
        out[p] = "building" if p in answered and p not in waiting else "parked"
    return out


def _working_since(dash, working: list) -> dict:
    """When each parked phase that is at work again was launched: its open run
    in the history, the same clock a card in a slot shows."""
    open_runs = {}
    for run in getattr(dash, "history", None) or []:  # newest first
        if run.ended_at is None and run.status is None:
            open_runs.setdefault(run.phase, run.started_at)
    return {pid: open_runs.get(pid) for pid in working}


def _place(pid, graph, done, satisfied, excluded, waiting, parked, busy, queue, snap,
           owed, jobs, questions, roots_of, dated=None, answered=()) -> tuple[str, dict]:
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
        return NEEDS_YOU, {"sub": "asks you · answer in its worker pane",
                           "q": clip(blocker.question if blocker else "", _QUESTION_CHARS),
                           "since": blocker.since if blocker else None,
                           "parks_at": waiting.get(pid)}
    if pid in parked and pid in answered:
        return BUILDING, {"sub": f"works on your answer in tmux window {_wait_window(pid)}"}
    if pid in parked:
        return NEEDS_YOU, {"sub": f"asks you · answer in tmux window {_wait_window(pid)}",
                           "q": clip(blocker.question if blocker else "", _QUESTION_CHARS),
                           "since": blocker.since if blocker else None}
    if status == statuses.NEEDS_OWNER:
        return NEEDS_YOU, {"sub": "built, and left you a task",
                           "q": clip(blocker.question if blocker else "", _QUESTION_CHARS)}
    if ask is not None or (gave_up is not None and gave_up.asked):
        job = ask or gave_up
        return NEEDS_YOU, {"sub": _job_asks(job, parked), "job": job.phase,
                           "q": clip(job.question or job.last_error, _QUESTION_CHARS),
                           "since": job.asked_at or job.queued_at or None}
    if gave_up is not None:
        return NEEDS_YOU, {"sub": "operator job gave up; yours to do by hand",
                           "job": gave_up.phase,
                           "q": clip(gave_up.last_error or gave_up.note, _QUESTION_CHARS)}
    if pid in busy:
        return BUILDING, {"slot": busy[pid].id}
    if snap.integ_blocked == pid:
        return MERGING, {"sub": f"merging stopped: {held_merge(snap.integ_blocked_kind)}",
                         "held": True}
    if pid in queue:
        pos = queue.index(pid)
        return MERGING, {"sub": "next to merge" if pos == 0 else f"waiting to merge, #{pos + 1}"}
    if pid in owed:
        return MERGING, {"sub": "merged; the push is retried", "held": True}
    live = [j for j in mine if j.state in (opqueue.QUEUED, opqueue.RUNNING)]
    if live:
        return OPERATOR, {"sub": f"operator {opqueue.standing(live[0], why=False)}",
                          "job": live[0].phase}
    if status in statuses.SATISFIES_DEPS:
        if status == statuses.SKIP:
            sub = "skipped"
        elif status == statuses.LEDGER:
            sub = "ticked in the ledger"
        elif status == statuses.OPERATOR:
            sub = "built · operator job done" if mine and all(
                j.state == opqueue.DONE for j in mine) else "built · operator job to come"
        else:
            sub = "built"
        return DONE, {"sub": sub, "st": status}
    if blocker is not None and blocker.kind == "owner-row":
        return NEEDS_YOU, {"sub": "only you can do this", "q": blocker.question}
    if pid in excluded:
        return EXCLUDED, {"sub": "yours to do"}
    if status == statuses.FAIL:
        return FAILED, {"st": status, "sub": "stopped; its work was set aside"}
    unmet = sorted(d for d in graph.get(pid, ()) if d not in satisfied)
    if unmet:
        roots = sorted(roots_of.get(pid, []), key=lambda b: -b["blocks"])
        extra = {"unmet": len(unmet), "needs": unmet[:6]}
        if roots:
            extra.update(root=roots[0]["phase"], root_kind=roots[0]["kind"],
                         root_blocks=roots[0]["blocks"])
            if roots[0]["kind"] == "dated":
                extra["root_sub"] = f"which waits until {dated[roots[0]['phase']]}"
            if len(roots) > 1:
                extra["roots"] = [b["phase"] for b in roots[:6]]
        else:
            extra["sub"] = "dependency cycle: the ledger needs fixing"
        return BLOCKED, extra
    if pid in (dated or {}):
        return BLOCKED, {"sub": f"waits until {dated[pid]}"}
    return READY, {}


def _wait_window(key: str) -> str:
    from ..state import wait_window  # local: the board is otherwise state-free

    return wait_window(key)


def _job_asks(job, parked: list) -> str:
    """An operator job's ask, and the tmux window it waits in."""
    from ..state import OPERATOR as KIND, waiter_key

    if job.state != opqueue.WAITING:
        return "operator job asked you, then ended"
    key = waiter_key(KIND, job.phase)
    window = _wait_window(key) if key in parked else "operator"
    return f"operator job asks you · answer in tmux window {window}"


def _building_extras(cards: dict, started: dict, dash, turns: dict) -> None:
    """Elapsed, context and the worker's own last words, for every card a worker
    is on. ``started`` maps each to when its worker was launched, if known."""
    meters = getattr(dash, "meters", None) or {}
    # The phase every busy card is measured against (the client draws elapsed
    # from ``since`` itself, so the board does not change every second).
    seen = typical_durations(getattr(dash, "eta_runs", None) or [])
    typical = median(seen) if seen else None
    for pid, since in started.items():
        card = cards.get(pid)
        if card is None:
            continue
        m = meters.get(pid)
        if since:
            card["since"] = since
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


def _job_cards(jobs: dict, graph: dict, parked: list = ()) -> list[dict]:
    """Operator jobs that belong to no ledger phase (``swarm operator-add``)."""
    out = []
    for owner, items in jobs.items():
        if owner in graph:
            continue
        for job in items:
            base = {"id": job.phase, "kind": "job", "c": "operator",
                    "t": clip(job.note, 160), "job": job.phase}
            if job.state == opqueue.WAITING or (job.state == opqueue.ABANDONED and job.asked):
                out.append({**base, "col": NEEDS_YOU, "sub": _job_asks(job, parked),
                            "q": clip(job.question or job.last_error, _QUESTION_CHARS)})
            elif job.state == opqueue.ABANDONED:
                out.append({**base, "col": NEEDS_YOU,
                            "sub": "operator job gave up; yours to do by hand",
                            "q": clip(job.last_error, _QUESTION_CHARS)})
            elif job.state in (opqueue.QUEUED, opqueue.RUNNING):
                out.append({**base, "col": OPERATOR,
                            "sub": f"operator {opqueue.standing(job, why=False)}"})
    return out


def _pass_cards(passes: list, parked: list = ()) -> list[dict]:
    """An Overseer pass waiting on the owner is a question like any other."""
    out = []
    for rec in passes or []:
        if rec.status == "running" and rec.question and not rec.answer:
            key = f"overseer:{rec.id}"
            window = _wait_window(key) if key in parked else "overseer"
            out.append({"id": key, "kind": "pass", "c": "overseer",
                        "t": "the Overseer asks you", "col": NEEDS_YOU,
                        "sub": f"answer in tmux window {window}",
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
    if key == BUILDING:  # the slots in order, then the ones in a window of their own
        return sorted(cards, key=lambda c: ("slot" not in c, c.get("slot", 0)))
    if key == MERGING:
        return sorted(cards, key=lambda c: (queue.index(c["id"]) if c["id"] in queue else -1))
    return sorted(cards, key=lambda c: (pos.get(c["id"], 1 << 30), c["id"]))


def _campaigns(cards: dict, metas: dict, fc=None, now: float = 0.0) -> list[dict]:
    """Campaign header cards: what it is, its progress, its phases by column,
    and when the forecast ``fc`` says it finishes."""
    books = {b.name: b for b in (fc.books if fc is not None else ())}
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
            "eta": _book_eta(books.get(name), fc, now),
            "rank": (0 if active else 1 if counts.get(READY) else 2 if open_ else 3,
                     g["open_at"], g["first"]),
        })
    out.sort(key=lambda c: c["rank"])
    for c in out:
        c.pop("rank")
    return out


def _book_eta(book, fc, now: float) -> dict | None:
    """One campaign's finish, as the TUI's phase-book list says it."""
    if book is None:
        return None
    r = book.finish
    return {"text": books_mod.phrase(r, fc, now), "left": book.left, "behind": book.behind,
            "running": book.running, "ready": book.ready, "grows": book.grows,
            "p50": _at(r.p50) if r else None, "p85": _at(r.p85) if r else None,
            "p95": _at(r.p95) if r else None}


def _at(ts: float) -> float | None:
    """A finish time for JSON: ``None`` where there is none in sight."""
    return ts if ts != float("inf") else None


def _restart(dash, now: float) -> dict:
    """The ``swarm restart`` plan for the header: its line (clock time only, so
    a quiet board does not change as time passes) and how far it has got."""
    plan = getattr(dash, "restart", None) or {}
    text = restart_mod.line(
        plan, *restart_mod.counts_of(getattr(dash, "_state", None) or {}), now,
        relative=False) if plan else ""
    return {"restart": text, "restart_stage": plan.get("stage", "") if text else ""}


def _header(cfg, dash, cards: dict, extra: list, passes: list, now: float) -> dict:
    snap = dash.snapshot
    cols: dict[str, int] = {}
    for c in list(cards.values()) + extra:
        cols[c["col"]] = cols.get(c["col"], 0) + 1
    workers = len(snap.slots) or int(getattr(cfg, "max_workers", 0) or 0)
    remaining = sum(1 for c in cards.values() if c["col"] in OPEN)
    # The forecast is the TUI's, word for word (:mod:`swarm_orchestrator.eta`):
    # clock times, not seconds from now, so it only changes when it is remade.
    fc = getattr(dash, "forecast", None)
    seconds = None
    eta = {"remaining": remaining, "text": "working out when…", "basis": "", "waiting": "",
           "critical": [], "floor": True, "p50": None, "p85": None, "p95": None}
    if fc is not None:
        r = fc.overall
        eta.update(text=books_mod.overall_line(fc, now), basis=books_mod.basis_line(fc, now),
                   waiting=books_mod.waiting_line(fc), critical=list(fc.critical),
                   floor=fc.floor, p50=_at(r.p50) if r else None,
                   p85=_at(r.p85) if r else None, p95=_at(r.p95) if r else None)
        if eta["p85"] is not None:
            # From when it was made, not from now: a quiet board must not change
            # (and wake every phone) just because time passed.
            seconds = max(0.0, eta["p85"] - fc.made_at)
    run = getattr(dash, "run", None) or None
    usage = getattr(dash, "usage", None) or {}
    last = passes[0] if passes else None
    counted = [c for c in cards.values() if c["col"] != EXCLUDED]
    return {
        "counts": cols,
        "progress": {"done": sum(1 for c in counted if c["col"] in (DONE, OPERATOR)),
                     "total": len(counted)},
        "next_cap": _next_cap(cfg, dash, eta.get("p50"), now),
        "needs_you": cols.get(NEEDS_YOU, 0),
        "slots": {"busy": sum(1 for s in snap.slots if s.busy), "total": workers},
        "paused": bool(snap.paused),
        "usage_hold": snap.usage_hold,
        "drain": drain_line(snap.drain),
        "frozen": frozen_line(snap.frozen, now),
        **_restart(dash, now),
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


def _next_cap(cfg, dash, done_at: float | None, now: float) -> dict | None:
    """The first usage cap the run reaches (:func:`usagechart.next_cap`)."""
    try:
        wins = usagechart.windows(
            cfg, getattr(dash, "samples", None) or [], burn=getattr(dash, "burn", None) or {},
            busy=sum(1 for s in dash.snapshot.slots if s.busy),
            run_usage=getattr(dash, "usage", None), hold=getattr(dash, "usage_state", ({}, {}))[0],
            override=getattr(dash, "usage_state", ({}, {}))[1], now=now)
    except (AttributeError, TypeError, ValueError):
        return None
    return usagechart.next_cap(wins, done_at)


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
        # A claim that ended without a report is not a finish.
        if run.running or run.hold or not run.ended_at or run.status == LOST:
            continue
        finished.append({"id": run.phase, "c": campaign_of(run.phase), "st": run_word(run.status),
                         "at": run.ended_at, "took": run.duration_s if run.started_at else None,
                         "recap": clip(run.summary, 500), "note": clip(run.note, 500)})
        if len(finished) >= 30:
            break
    pings = []
    for n in reversed(getattr(dash, "notifications", None) or []):
        pings.append({"ts": n.ts, "kind": n.kind, "phase": n.phase, "text": clip(n.text, 600),
                      "delivered": n.delivered, "suppressed": clip(n.suppressed, 200)})
        if len(pings) >= 40:
            break
    return {"passes": out_passes, "finished": finished, "pings": pings}
