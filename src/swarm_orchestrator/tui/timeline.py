"""What happened, and what is waiting on the owner — the home screen's two lists.

The history — the decisions the worker took — is the most useful part of the
dashboard, so home now leads with it, in two shapes:

* the **feed**: one list, newest first, of everything a person would want told
  about since they last looked — a phase finishing (with its recap), a call a
  worker made on its own (``swarm note`` decisions, assumptions, risks), an
  answer the owner gave, an operator job's outcome and what an Overseer pass did
  or left for the owner;
* the **needs-you strip**: everything waiting on the owner, with how long it has
  waited and how many phases sit behind it — the two numbers that decide whether
  to get up now.

Pure functions over what :class:`~swarm_orchestrator.tui.dash.Dash` already
holds. Nothing here reads a file: the notes, recaps, sentinels, the operator
queue and the Overseer records are all re-read by the dash only when their mtime
moves, so the feed costs a sort, never a disk walk, on a 2 s tick.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import opqueue, statuses
from ..notes import OWNER_DECISION

#: Rows the feed keeps. Past this it is the History tab's job; the feed is for
#: "what happened while I was away", which is a screenful, not an archive.
FEED_MAX = 60

FINISH = "finish"
OWNER = "owner"
OPERATOR = "operator"
OVERSEER = "overseer"
#: ``swarm note`` kinds, in the order a reader weighs them.
NOTE_KINDS = ("decision", "assumption", "risk")


@dataclass(frozen=True)
class FeedItem:
    """One line of the feed, and what selecting it opens.

    ``phase`` is the phase whose History detail the row opens; ``ref`` names an
    Overseer pass or an operator job for the rows that have no phase of their own.
    """

    ts: float
    kind: str  # finish | decision | assumption | risk | owner | operator | overseer
    phase: str | None = None
    status: str = ""
    text: str = ""
    left: str = ""
    ref: str = ""


def build_feed(history=(), notes=None, operator=(), passes=(), limit: int = FEED_MAX
               ) -> list[FeedItem]:
    """The feed, newest first. Tolerates any source being empty or ``None``.

    Entries with no timestamp are dropped rather than guessed into place: a note
    with no ``ts`` sorted to "now" would sit on top of the feed forever.
    """
    out: list[FeedItem] = []
    for run in history or ():
        if getattr(run, "running", False) or not getattr(run, "ended_at", None):
            continue
        out.append(FeedItem(run.ended_at, FINISH, run.phase, run.status or "",
                            run.summary or run.note or ""))
    for phase, items in (notes or {}).items():
        for note in items or ():
            if not note.ts:
                continue
            kind = OWNER if note.kind == OWNER_DECISION else (
                note.kind if note.kind in NOTE_KINDS else "decision")
            out.append(FeedItem(note.ts, kind, note.phase or phase, "", note.text))
    for item in operator or ():
        if item.state not in opqueue.TERMINAL:
            continue
        ts = item.done_at or item.queued_at
        if not ts:
            continue
        text = (item.outcome or item.note) if item.state == opqueue.DONE else (
            item.last_error or item.note)
        out.append(FeedItem(ts, OPERATOR, opqueue.owning_phase(item.phase), item.state,
                            text, ref=item.phase))
    for rec in passes or ():
        ts = rec.ended_at or rec.started_at
        if not ts:
            continue
        text = rec.summary or rec.did or ("running" if rec.status == "running" else "")
        out.append(FeedItem(ts, OVERSEER, None, rec.status, text, left=rec.left, ref=rec.id))
    out.sort(key=lambda f: f.ts, reverse=True)
    return out[:limit]


# -- needs you ------------------------------------------------------------
@dataclass(frozen=True)
class Need:
    """One thing waiting on the owner."""

    key: str
    kind: str
    phase: str | None
    question: str
    since: float | None
    blocks: int = 0
    slot: int | None = None
    ref: str = ""


#: What each blocker kind is, in the owner's words rather than the state file's.
NEED_LABEL = {
    "waiting": "worker asks",
    "parked": "worker asks (parked)",
    "integ": "merge held",
    statuses.NEEDS_OWNER: "needs you",
    "operator-ask": "operator asks",
    "operator-abandoned": "operator gave up",
    OVERSEER: "overseer asks",
    "owner-row": "yours to do",
}


def blocker_since(blocker, sentinels: dict, asked: dict) -> float | None:
    """When a blocker started waiting, best clock first.

    A blocker that was never launched (a ``needs-owner`` finish, a phase whose
    ``LAUNCH`` rotated out of the log) has no ``since``, and the sentinel — then
    the ping that told the owner about it — is the only clock there is.
    """
    if blocker.since is not None:
        return blocker.since
    sentinel = (sentinels or {}).get(blocker.phase)
    if sentinel is not None and sentinel.mtime:
        return sentinel.mtime
    return (asked or {}).get(blocker.phase)


def dependents(graph: dict) -> dict[str, set[str]]:
    """``phase -> phases that need it`` — the ledger graph turned round."""
    out: dict[str, set[str]] = {}
    for phase, deps in (graph or {}).items():
        for dep in deps or ():
            out.setdefault(dep, set()).add(phase)
    return out


def downstream(rdeps: dict[str, set[str]], done: dict, phase: str) -> int:
    """How many not-yet-landed phases sit behind ``phase``, transitively.

    "Blocks 14 phases" is what turns a question from "later" into "now"; a phase
    whose dependents already landed some other way holds nothing up.
    """
    seen: set[str] = set()
    stack = list(rdeps.get(phase, ()))
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(rdeps.get(p, ()))
    return sum(1 for p in seen if (done or {}).get(p) not in statuses.SATISFIES_DEPS)


def needs_you(dash) -> list[Need]:
    """Everything waiting on the owner, longest-waiting first.

    Worker questions, parked phases, a held merge queue, operator asks and
    give-ups come from the snapshot's blockers (the drawer's own list, so the two
    can never disagree); an Overseer pass that asked and has no answer is added
    from its record.
    """
    snap = getattr(dash, "snapshot", None)
    if snap is None or not getattr(snap, "ok", False):
        return []
    blockers = list(snap.blockers or [])
    passes = [r for r in (getattr(dash, "passes", None) or [])
              if r.status == "running" and r.question and not r.answer]
    if not blockers and not passes:
        return []
    from .data import question_index  # local: data imports nothing from here

    notes = getattr(dash, "notifications", None) or []
    sentinels = getattr(dash, "sentinels", None) or {}
    questions = question_index(notes, sentinels)
    asked = {n.phase: n.ts for n in notes if n.phase and n.ts}
    rdeps = dependents(getattr(dash, "graph", None) or {})
    done = snap.done or {}
    busy = {s.phase: s.id for s in snap.slots if s.busy and s.phase}
    out: list[Need] = []
    for b in blockers:
        owner = opqueue.owning_phase(b.phase)
        out.append(Need(
            key=f"{b.kind}:{b.phase}",
            kind=NEED_LABEL.get(b.kind, b.kind),
            phase=owner,
            question=b.question or questions.get(b.phase, "") or b.detail,
            since=blocker_since(b, sentinels, asked),
            blocks=downstream(rdeps, done, owner),
            slot=busy.get(b.phase),
            ref=b.phase if owner != b.phase else "",
        ))
    for rec in passes:
        out.append(Need(key=f"{OVERSEER}:{rec.id}", kind=NEED_LABEL[OVERSEER], phase=None,
                        question=rec.question, since=rec.asked_at or rec.started_at or None,
                        ref=rec.id))
    out.sort(key=lambda n: (n.since is None, n.since or 0.0))
    return out


# -- what's next ----------------------------------------------------------
@dataclass(frozen=True)
class Upcoming:
    """A phase that has not run yet: ready, or blocked on ``needs``."""

    phase: str
    ready: bool
    needs: tuple[str, ...] = ()


def upcoming(graph: dict, done: dict, busy=(), excluded=(), in_flight=(),
             prefer: str | None = None, limit: int = 8) -> list[Upcoming]:
    """The next phases in line: ready ones first, then the nearest blocked.

    Ledger order within each group, because the swarm launches ready rows in
    ledger order — so this is the actual queue, not a guess at it. ``prefer``
    (the active campaign) floats its own phases up: the owner is watching that
    campaign, and a ready row from an unrelated one is the less useful answer.
    """
    from .campaign import campaign_of  # local: campaign imports data

    done = done or {}
    skip = set(done) | set(busy or ()) | set(excluded or ()) | set(in_flight or ())
    satisfied = {p for p, s in done.items() if s in statuses.SATISFIES_DEPS}
    order = {p: i for i, p in enumerate(graph or {})}
    ready: list[Upcoming] = []
    blocked: list[Upcoming] = []
    for phase, deps in (graph or {}).items():
        if phase in skip:
            continue
        unmet = tuple(sorted(d for d in deps or () if d not in satisfied))
        (blocked if unmet else ready).append(Upcoming(phase, not unmet, unmet))

    def rank(u: Upcoming):
        return (prefer is not None and campaign_of(u.phase) != prefer, len(u.needs),
                order.get(u.phase, 0))

    ready.sort(key=rank)
    blocked.sort(key=rank)
    return (ready + blocked)[:limit]
