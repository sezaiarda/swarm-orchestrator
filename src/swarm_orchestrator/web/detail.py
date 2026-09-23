"""One card, opened: everything the swarm knows about a single phase.

The board carries a line per phase; this is the sheet behind it — the full
ledger row, what it needs (and where each of those stands), what it holds up,
its campaign, every attempt, the recap and the worker's own completion note,
the calls it made on its own (grouped: the owner's answers first), the question
it is waiting on, its operator jobs, and where it is being built.

Built on demand for one phase from the same loaded inputs as the board, plus
the handful of per-phase files the board does not keep (the attempt log and the
last captured turn). Returns ``None`` for an id that is not on the board, so
the server answers 404 rather than inventing a phase.
"""

from __future__ import annotations

import time

from .. import ledger as ledger_mod
from .. import notes as notes_mod
from .. import opqueue
from ..tui.campaign import campaign_of
from ..tui.data import load_attempts
from ..why import _exclude_comment
from .rows import clip

#: Note kinds in the order the sheet shows them: what the owner decided outranks
#: what a worker decided, which outranks what it merely assumed.
NOTE_ORDER = (notes_mod.OWNER_DECISION, "decision", "assumption", "risk")


def phase(cfg, dash, board: dict, rows: dict, metas: dict, pid: str,
          turns: dict | None = None, now: float | None = None) -> dict | None:
    """The detail sheet for ``pid``, or ``None`` when it is not on the board."""
    now = time.time() if now is None else now
    index = {c["id"]: c for col in board.get("columns", []) for c in col["cards"]}
    card = index.get(pid)
    if card is None:
        return None
    if card.get("kind") in ("job", "pass"):
        return _other(dash, card)

    graph = dash.graph or {}
    row = rows.get(pid)
    camp = campaign_of(pid)
    meta = metas.get(camp)
    snap = dash.snapshot
    done = dict(snap.done)
    deps = sorted(graph.get(pid, ()))
    dependents = sorted(p for p, ds in graph.items() if pid in ds)
    excluded = set(getattr(cfg, "exclude", None) or [])
    slot = next((s for s in snap.slots if s.busy and s.phase == pid), None)
    sentinel = (getattr(dash, "sentinels", None) or {}).get(pid)
    recap = (getattr(dash, "recaps", None) or {}).get(pid)
    runs = [r for r in (getattr(dash, "history", None) or []) if r.phase == pid]

    def ref(p: str) -> dict:
        other = index.get(p)
        return {"id": p, "col": other["col"] if other else None,
                "sub": (other or {}).get("sub", ""), "t": (other or {}).get("t", ""),
                "st": done.get(p)}

    out = {
        "id": pid,
        "card": card,
        "campaign": {"name": camp, "what": meta.what if meta else "",
                     "adr": meta.adr if meta else ""},
        "row": {
            "text": row.text if row else "",
            "line": row.line if row else None,
            "checked": row.checked if row else None,
            "dirs": row.dirs if row else [],
            "section": [h.text for h in row.heads if h.level > 1] if row else [],
        },
        "status": done.get(pid),
        "needs": [ref(d) for d in deps],
        "blocks": [ref(d) for d in dependents],
        "blocks_total": ledger_mod.blocked_behind(graph, pid, done, excluded),
        "recap": {"summary": recap.summary, "ts": recap.ts} if recap and recap.summary else None,
        "completion": {"status": sentinel.status, "note": sentinel.note, "ts": sentinel.mtime}
        if sentinel else None,
        "attempts": _attempts(cfg, pid),
        "runs": [{"started": r.started_at, "ended": r.ended_at, "status": r.status,
                  "slot": r.slot, "took": r.duration_s if r.started_at else None,
                  "parked": r.parked} for r in runs[:20]],
        "notes": _notes(dash, pid),
        "question": card.get("q") or "",
        "jobs": [_job(j) for j in (snap.operator or [])
                 if opqueue.owning_phase(j.phase) == pid],
        "excluded_why": _exclude_comment(cfg, pid) if pid in excluded else None,
    }
    if slot is not None:
        out["build"] = {"slot": slot.id, "branch": slot.branch, "worktree": slot.worktree,
                        "since": slot.started_at,
                        "elapsed": max(0.0, now - slot.started_at) if slot.started_at else None}
        turn = (turns or {}).get(pid)
        if turn and turn.get("text"):
            out["build"]["last_turn"] = {"ts": turn.get("ts"), "text": clip(turn["text"], 2000)}
    return out


def _attempts(cfg, pid: str) -> list[dict]:
    """``done/<phase>.jsonl``: every ``swarm done`` it ever recorded, oldest first."""
    out = []
    for rec in load_attempts(cfg.done_dir, pid)[-20:]:
        out.append({"ts": rec.get("ts"), "status": rec.get("status"),
                    "note": clip(str(rec.get("note") or ""), 1500),
                    "verdict": rec.get("verdict")})
    return out


def _notes(dash, pid: str) -> dict[str, list[dict]]:
    """``{kind: [{text, ts}]}``, in :data:`NOTE_ORDER`, empty kinds omitted."""
    grouped: dict[str, list[dict]] = {}
    for note in (getattr(dash, "notes", None) or {}).get(pid, []):
        grouped.setdefault(note.kind, []).append({"text": note.text, "ts": note.ts})
    ordered = {k: grouped.pop(k) for k in NOTE_ORDER if k in grouped}
    ordered.update(grouped)
    return ordered


def _job(item) -> dict:
    triage = item.triage or {}
    return {"id": item.phase, "state": item.state, "note": clip(item.note, 1500),
            "outcome": clip(item.outcome, 1500), "question": clip(item.question, 800),
            "answer": clip(item.answer, 800), "attempts": item.attempts,
            "queued_at": item.queued_at or None, "done_at": item.done_at or None,
            "error": clip(item.last_error, 800),
            "when": triage.get("when"), "why": clip(str(triage.get("why") or ""), 300)}


def _other(dash, card: dict) -> dict:
    """An operator job or an Overseer ask: not a ledger row, but still a card."""
    out = {"id": card["id"], "card": card, "question": card.get("q") or ""}
    if card.get("kind") == "job":
        job = next((j for j in (dash.snapshot.operator or []) if j.phase == card["id"]), None)
        out["jobs"] = [_job(job)] if job else []
    return out
