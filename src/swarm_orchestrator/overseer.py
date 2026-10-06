"""When the Overseer runs: the trigger policy, and the starvation map it reads.

The Overseer is the old LLM master, repurposed. Launching is mechanical now (the
supervisor does it straight from the ledger), so the one Claude session the
supervisor still owns is spent on *judgement*: read a digest of the whole swarm
and of the phases finished since the last look (their recaps, decisions, risks),
then act — retry a failure, clear a stuck slot, reshape the ledger so free slots
have work, queue an operator job, tell the owner what needs them.

This module decides **when**, and nothing else. The supervisor feeds it what it
sees on every wake (:meth:`Policy.observe`) and asks whether a pass is due
(:meth:`Policy.due`); spawning, the digest and the pass record live elsewhere
(:mod:`master`, :mod:`ovdigest`, :mod:`ovrecord`).

Triggers
--------
*Events* — a phase finishing ``fail`` (a ``later`` is not one: it waits for its
date, :func:`ledgerw.dated`); an integration hold no resolver is handling
(below); an owed push its repo's own check refused, or any other still owed
after :data:`PUSH_GRACE_S`; a
cheap doctor check turning FAIL; a session asking the owner for longer than
``[overseer] owner_wait_s`` (once per unanswered question); starvation — free slots and
nothing launchable while non-excluded backlog remains, sustained for
``[overseer] starve_s`` (once per episode); the box running short of RAM, swap,
``/tmp`` or disk for :data:`BOX_S` (once per episode). *The clock* — every
``[overseer] every_s`` a pass that writes the owner their summary
(``swarm overseer-summary``). It is the only pass whose summary goes to the
phone, and it counts from the last such pass, not from the last pass of any
kind: a busy run's event passes never put the summary off. The clock runs with
the Overseer off too; the supervisor then writes the summary itself
(:meth:`Policy.summary_due`). A swarm that stood still since the last summary
gets none: the supervisor starts the clock again instead
(:func:`ovdigest.nothing_to_report`).

No pass is started for a number of finished phases. What a pass does for them
(reads their recaps, files rows for the risks they noted) it does on whichever
pass comes next, the clock's at the latest; the rest of what such a pass did
has a trigger of its own (a ``fail``, starvation, the box).

A merge conflict opens a resolver session, which almost always clears the hold
within a minute; a pass that looks meanwhile finds nothing to do. So a hold
triggers only once the resolver is out of the picture: none was opened (a dirty
tree, a spawn that failed), it gave up (messaged the owner, or said ``resolved``
on an unfinished merge), or the hold has outlived ``[overseer] hold_wait_s``.
The condition is checked again just before a pass starts, and a hold reason
that no longer applies is dropped.

Coalescing
----------
Reasons accumulate in one pending list and a pass takes all of them at once. There
is never more than one pass: a reason that arrives while one runs waits for it to
end. Passes are at least ``[overseer] min_gap_s`` apart (measured start to start,
so a long pass is not followed by a further idle gap) — except for *urgent*
reasons, which only wait for the running pass: a frozen merge queue, a stuck-state
doctor FAIL, sustained starvation and a manual ``swarm overseer --now`` all cost
throughput by the minute.

Everything the policy remembers is persisted in ``<state>/overseer/policy.json``
so a supervisor restart neither re-fires every old failure nor forgets a pending
pass. A first run with no memory *baselines* the finished phases silently — a
long-lived project has hundreds, and none of them is news.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import ledgerw, statuses
from . import state as state_mod
from .config import Config
from .logutil import Log

#: The Overseer's own directory under the state dir: policy memory, digests, records.
DIRNAME = "overseer"

# Reason keys. A pending reason is identified by its key, so the same condition
# asserting on every wake is one reason, not a pile of them.
FAIL = "fail"
HOLD = "hold"
PUSH = "push"
DOCTOR = "doctor"
OWNER = "owner"
STARVE = "starve"
BOX = "box"
SUMMARY = "summary"
MANUAL = "manual"

#: How long a push that failed without the repo's own check refusing it may stay
#: owed before it triggers a pass: the next push, seconds later, settles most.
PUSH_GRACE_S = 120.0

#: How long the box must stay short of RAM, swap, ``/tmp`` or disk before it
#: triggers a pass: one sample taken in the middle of a link step is not danger.
BOX_S = 300.0



def overseer_dir(cfg: Config) -> Path:
    return cfg.state_dir / DIRNAME


@dataclass
class Reason:
    """One thing a pass should look at."""

    key: str
    text: str
    urgent: bool = False
    at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "Reason":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Memory:
    """What the policy carries across wakes and restarts (``policy.json``).

    Every field defaults, and unknown keys are dropped on load, so a file written
    by an older or newer version never stops the policy from running.
    """

    #: Start of the last pass (the min-gap clock runs from here).
    last_pass_at: float = 0.0
    #: How far thaws have moved the clocks' start (``last_pass_at``, else
    #: ``anchor``) along since it was set (:meth:`shift`). The clocks count from
    #: the moved moment; what finished since is read from the real one
    #: (:meth:`Policy.since`).
    frozen_s: float = 0.0
    last_pass_end: float = 0.0
    #: When the policy first looked (supervisor start): where the summary clock
    #: starts until a summary pass has run.
    anchor: float = 0.0
    #: Start of the last pass that wrote the owner their summary (the every-s
    #: clock runs from here).
    last_summary_at: float = 0.0
    #: ``done`` as last observed. None = never observed: the first observation
    #: baselines instead of firing.
    seen_done: dict[str, str] | None = None
    seen_hold: str = ""
    #: When the current hold was first seen, whether it has triggered a pass,
    #: and the phase whose resolver gave up on it.
    hold_since: float = 0.0
    hold_fired: bool = False
    hold_escalated: str = ""
    #: The owed pushes that have triggered a pass, and since when each of the
    #: others has been owed (it triggers if it outlives the grace).
    seen_push: list[str] = field(default_factory=list)
    push_since: dict[str, float] = field(default_factory=dict)
    owner_since: dict[str, float] = field(default_factory=dict)
    owner_fired: list[str] = field(default_factory=list)
    doctor_failing: list[str] = field(default_factory=list)
    starving_since: float = 0.0
    starve_fired: bool = False
    #: Since when the box has been short of something, and whether that
    #: episode has triggered a pass.
    box_since: float = 0.0
    box_fired: bool = False
    pending: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "Memory":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def shift(self, delta: float, now: float) -> None:
        """Move every clock ``delta`` seconds along for a thaw: no pass could
        run and nobody could answer while the sessions stood frozen, so no gap,
        cadence, hold, wait on the owner or starvation got any older."""
        was = self.last_pass_at or self.anchor
        self.last_pass_at = state_mod.moved(self.last_pass_at, delta, now)
        self.anchor = state_mod.moved(self.anchor, delta, now)
        self.frozen_s += (self.last_pass_at or self.anchor) - was
        self.last_summary_at = state_mod.moved(self.last_summary_at, delta, now)
        self.hold_since = state_mod.moved(self.hold_since, delta, now)
        self.starving_since = state_mod.moved(self.starving_since, delta, now)
        self.box_since = state_mod.moved(self.box_since, delta, now)
        self.owner_since = {k: state_mod.moved(v, delta, now)
                            for k, v in self.owner_since.items()}
        self.push_since = {k: state_mod.moved(v, delta, now)
                           for k, v in self.push_since.items()}


def shift(cfg: Config, delta: float, now: float | None = None) -> None:
    """A thaw's move of the policy file (:func:`freezer.rebase`). A running
    supervisor holds the same memory and moves its own (:meth:`Policy.shift`)."""
    policy = Policy(cfg)
    if policy.path.is_file():
        policy.shift(delta, now)


class Policy:
    """The trigger policy for one run. Pure bookkeeping plus a log line per trigger."""

    def __init__(self, cfg: Config, log: Log | None = None) -> None:
        self.cfg = cfg
        self.log = log
        self.mem = self._load()

    # -- persistence -------------------------------------------------------
    @property
    def path(self) -> Path:
        return overseer_dir(self.cfg) / "policy.json"

    def _load(self) -> Memory:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Memory()
        return Memory.from_dict(data) if isinstance(data, dict) else Memory()

    def save(self) -> None:
        """Atomic write; a failure costs memory across a restart, never the run."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(asdict(self.mem), indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # -- the pending list ---------------------------------------------------
    @property
    def pending(self) -> list[Reason]:
        return [Reason.from_dict(d) for d in self.mem.pending]

    def request(self, key: str, text: str, *, urgent: bool = False, now: float | None = None) -> bool:
        """Add a reason for a pass; False when that key is already pending.

        A repeat of a pending key can still upgrade it to urgent — the second
        sighting of a condition may be the one that makes it expensive.
        """
        now = time.time() if now is None else now
        for d in self.mem.pending:
            if d.get("key") == key:
                if urgent and not d.get("urgent"):
                    d["urgent"] = True
                    self.save()
                return False
        self.mem.pending.append(asdict(Reason(key, text, urgent, now)))
        self.save()
        if self.log is not None:
            self.log.line(f"OVERSEER-TRIGGER {key}{' urgent' if urgent else ''} — {text}")
        return True

    def due(self, now: float | None = None, *, running: bool = False) -> bool:
        """Should a pass start now? Never while one runs; urgent ignores the gap."""
        if running or not self.cfg.overseer_enabled or not self.mem.pending:
            return False
        if any(d.get("urgent") for d in self.mem.pending):
            return True
        now = time.time() if now is None else now
        return now - self.mem.last_pass_at >= self.cfg.overseer_min_gap_s

    def shift(self, delta: float, now: float | None = None) -> None:
        self.mem.shift(delta, time.time() if now is None else now)
        self.save()

    def since(self, now: float) -> float:
        """The moment the next pass reads on from, by the wall clock: when the
        last one began (or the policy first looked)."""
        start = self.mem.last_pass_at or self.mem.anchor
        return start - self.mem.frozen_s if start else now

    def begin(self, now: float | None = None) -> list[Reason]:
        """Take every pending reason for a pass that starts now."""
        now = time.time() if now is None else now
        taken = self.pending
        self.mem.pending = []
        self.mem.last_pass_at = now
        self.mem.frozen_s = 0.0
        if any(r.key == SUMMARY for r in taken):
            self.mem.last_summary_at = now
        self.save()
        return taken

    def end(self, now: float | None = None) -> None:
        self.mem.last_pass_end = time.time() if now is None else now
        self.save()

    def requeue(self, reasons: list[Reason]) -> None:
        """Put a failed pass's reasons back — as ordinary, so a pass that will not
        start is retried after the gap rather than in a tight loop."""
        for r in reasons:
            self.request(r.key, r.text, urgent=False, now=r.at or None)

    # -- observation ------------------------------------------------------
    def observe(
        self,
        st,
        now: float | None = None,
        *,
        starving: bool | None = False,
        doctor_fails: dict[str, str] | None = None,
        box: list[str] | None = None,
    ) -> list[str]:
        """Fold one look at the run into the memory; return the keys requested.

        ``st`` is a :class:`state.State`. ``starving`` is the supervisor's own
        verdict (it alone knows what is launching or given up); ``None`` leaves
        the starvation episode as it was. ``doctor_fails``
        maps check name -> detail for the cheap checks that FAILed, or ``None``
        when they were not probed on this wake (so an unprobed wake is never read
        as "all clear"). ``box`` is what the box is short of, in words
        (:func:`ovdigest.resources`' flags), with the same ``None``.
        """
        now = time.time() if now is None else now
        before = json.dumps(asdict(self.mem), sort_keys=True)
        if not self.mem.anchor:
            self.mem.anchor = now
        fired: list[str] = []
        fired += self._observe_done(st.done, now)
        fired += self._observe_hold(st, now)
        fired += self._observe_push(st.push_owed, now)
        fired += self._observe_owner(st, now)
        if starving is not None:
            fired += self._observe_starve(starving, now)
        if doctor_fails is not None:
            fired += self._observe_doctor(doctor_fails, now)
        if box is not None:
            fired += self._observe_box(box, now)
        fired += self._observe_clock(now)
        if json.dumps(asdict(self.mem), sort_keys=True) != before:
            self.save()  # the baselines move even on a wake that fires nothing
        return fired

    def _want(self, key: str, text: str, urgent: bool, now: float) -> list[str]:
        if not self.cfg.overseer_enabled:
            return []
        return [key] if self.request(key, text, urgent=urgent, now=now) else []

    def _observe_done(self, done: dict[str, str], now: float) -> list[str]:
        seen = self.mem.seen_done
        self.mem.seen_done = dict(done)
        if seen is None:
            return []  # first look: baseline, never a flood of history
        out: list[str] = []
        new = [p for p, s in done.items() if seen.get(p) != s]
        waits = ledgerw.dated(self.cfg) if statuses.FAIL in (done[p] for p in new) else {}
        for phase in new:
            status = done[phase]
            if status == statuses.FAIL:
                if phase in waits or ledgerw.later_date(self.cfg, phase):
                    continue  # finished `later`: it waits for a date, nothing failed
                out += self._want(f"{FAIL}:{phase}", f"{phase} finished fail", False, now)
        return out

    def _observe_hold(self, st, now: float) -> list[str]:
        key = f"{st.integ_blocked}:{st.integ_blocked_kind}" if st.integ_blocked else ""
        if key != self.mem.seen_hold:
            self.mem.seen_hold = key
            self.mem.hold_since = now if key else 0.0
            self.mem.hold_fired = False
            self.mem.hold_escalated = ""
        elif key and not self.mem.hold_since:
            self.mem.hold_since = now  # a memory written before the clock existed
        why = self._hold_trigger(st, now)
        if not key or self.mem.hold_fired or not why:
            return []
        self.mem.hold_fired = True
        return self._want(
            f"{HOLD}:{st.integ_blocked}",
            f"integration held on {st.integ_blocked} ({st.integ_blocked_kind}), {why}"
            " — the merge queue is frozen",
            True,
            now,
        )

    def _hold_trigger(self, st, now: float) -> str:
        """Why the current hold needs a pass, or ``""`` while a resolver is on it."""
        phase = st.integ_blocked
        if not phase:
            return ""
        if f"resolve:{phase}" not in st.windows:
            return "no resolver is on it"
        if self.mem.hold_escalated == phase:
            return "the resolver could not fix it"
        age = now - (self.mem.hold_since or now)
        if age >= self.cfg.overseer_hold_wait_s:
            return f"the resolver has not cleared it in {_age(age)}"
        return ""

    def resolver_escalated(self, phase: str) -> None:
        """The resolver on ``phase``'s hold gave up; the next look triggers."""
        if self.mem.hold_escalated != phase:
            self.mem.hold_escalated = phase
            self.save()

    def recheck(self, st, now: float | None = None) -> list[str]:
        """Drop every pending hold reason whose trigger no longer holds; return
        their keys. A reason can wait behind a running pass or the gap, and the
        resolver may clear the hold meanwhile — a pass started for it would only
        find the queue already moving."""
        now = time.time() if now is None else now
        live = f"{HOLD}:{st.integ_blocked}" if self._hold_trigger(st, now) else ""
        dropped = [
            d.get("key", "") for d in self.mem.pending
            if str(d.get("key", "")).startswith(f"{HOLD}:") and d.get("key") != live
        ]
        if not dropped:
            return []
        self.mem.pending = [d for d in self.mem.pending if d.get("key") not in dropped]
        if f"{HOLD}:{st.integ_blocked}" in dropped:
            self.mem.hold_fired = False  # still held: let it trigger again if it must
        self.save()
        if self.log is not None:
            self.log.line(f"OVERSEER-TRIGGER-DROPPED {','.join(dropped)} — no longer applies")
        return dropped

    def _observe_push(self, owed: dict[str, dict], now: float) -> list[str]:
        """An owed push, once per debt: at once when the repo's own pre-push
        check refused it, otherwise only if it is still owed after
        :data:`PUSH_GRACE_S`.

        A check that refused will refuse the same commits again, so someone has
        to act. Anything else (a remote that turned the ref away, a fetch that
        timed out) is usually settled by the next push seconds later, and a pass
        started for it finds nothing owed. A record with no ``since`` counts as
        old."""
        fired = [r for r in self.mem.seen_push if r in owed]
        self.mem.push_since = {}
        out: list[str] = []
        for repo in sorted(set(owed) - set(fired)):
            rec = owed.get(repo) or {}
            since = float(rec.get("since") or 0.0)
            if not rec.get("refused") and now - since < PUSH_GRACE_S:
                self.mem.push_since[repo] = since
                continue
            fired.append(repo)
            out += self._want(
                f"{PUSH}:{Path(repo).name}",
                f"push owed in {Path(repo).name} (since {rec.get('phase')}): {rec.get('reason')}",
                False,
                now,
            )
        self.mem.seen_push = sorted(fired)
        return out

    def _observe_owner(self, st, now: float) -> list[str]:
        """A session asking the owner for too long, once per unanswered question.

        Only a session that is asking now counts (:meth:`state.State.on_owner`):
        one parked and answered is working in its own window, and is forgotten
        here like one that finished. ``waiting`` stores the park *deadline*, so
        the ask began ``park_after`` before it, and a parked session carries the
        moment of its question. One parked by a supervisor that did not record it
        has no timestamp, so the first sighting stands in (kept across restarts in
        the memory).

        A parked session whose question is later than the one remembered asked
        again: that is a new wait, with a new clock, and it may fire again even
        when no look fell between the owner's answer and the next question."""
        on_owner = set(st.on_owner())
        for phase in list(self.mem.owner_since):
            if phase not in on_owner:
                del self.mem.owner_since[phase]
        self.mem.owner_fired = [p for p in self.mem.owner_fired if p in on_owner]
        for phase in on_owner:
            asked = st.asked_at(phase, self.cfg.park_after)
            seen = self.mem.owner_since.get(phase)
            if asked is None:
                self.mem.owner_since.setdefault(phase, now)
            elif phase in st.parked:
                if seen is not None and asked > seen:  # a question after the one timed
                    self.mem.owner_fired = [p for p in self.mem.owner_fired if p != phase]
                self.mem.owner_since[phase] = asked
            else:
                self.mem.owner_since[phase] = asked if seen is None else min(seen, asked)
        out: list[str] = []
        for phase in sorted(on_owner):
            since = self.mem.owner_since[phase]
            if phase in self.mem.owner_fired or now - since < self.cfg.overseer_owner_wait_s:
                continue
            self.mem.owner_fired.append(phase)
            out += self._want(
                f"{OWNER}:{phase}",
                f"{phase} has waited on the owner for {_age(now - since)}",
                False,
                now,
            )
        return out

    def _observe_starve(self, starving: bool, now: float) -> list[str]:
        if not starving:
            self.mem.starving_since = 0.0
            self.mem.starve_fired = False
            return []
        if not self.mem.starving_since:
            self.mem.starving_since = now
        if self.mem.starve_fired or now - self.mem.starving_since < self.cfg.overseer_starve_s:
            return []
        self.mem.starve_fired = True
        return self._want(
            STARVE,
            f"free slots with nothing launchable for {_age(now - self.mem.starving_since)}"
            " while backlog remains",
            True,
            now,
        )

    def _observe_doctor(self, fails: dict[str, str], now: float) -> list[str]:
        """A cheap check going FAIL, once per episode (it must clear to re-fire)."""
        new = [n for n in sorted(fails) if n not in self.mem.doctor_failing]
        self.mem.doctor_failing = sorted(fails)
        out: list[str] = []
        for name in new:
            out += self._want(f"{DOCTOR}:{name}", f"doctor FAIL {name}: {fails[name]}", True, now)
        return out

    def _observe_box(self, flags: list[str], now: float) -> list[str]:
        """The box short of RAM, swap, ``/tmp`` or disk for :data:`BOX_S`, once
        per episode (it must clear to fire again). Not urgent: the pass may
        pause the swarm, and one started every time a build peaks would cost
        more than it saves."""
        if not flags:
            self.mem.box_since = 0.0
            self.mem.box_fired = False
            return []
        if not self.mem.box_since:
            self.mem.box_since = now
        if self.mem.box_fired or now - self.mem.box_since < BOX_S:
            return []
        self.mem.box_fired = True
        return self._want(BOX, f"the box is short: {'; '.join(flags)}", False, now)

    def _observe_clock(self, now: float) -> list[str]:
        """The owner's summary, every ``[overseer] every_s`` since the last one."""
        if not self.summary_due(now):
            return []
        start = self.mem.last_summary_at or self.mem.anchor
        return self._want(
            SUMMARY, f"the owner's summary is due: none for {_age(now - start)}", False, now
        )

    def summary_due(self, now: float) -> bool:
        """Has ``[overseer] every_s`` passed since the last summary (since the
        policy first looked, for the first)? Answers with the Overseer off too."""
        if not self.mem.anchor:
            self.mem.anchor = now
            self.save()
        every = self.cfg.overseer_every_s
        return bool(every) and now - (self.mem.last_summary_at or self.mem.anchor) >= every

    def summarised(self, now: float) -> None:
        """A summary went out with no pass to write it: the clock starts again."""
        self.mem.last_summary_at = now
        self.save()

    # -- timing -----------------------------------------------------------
    def next_deadline(self, now: float | None = None) -> float | None:
        """The next moment a timer (not an event) could make a pass due.

        Only future moments: a past one would clamp the supervisor's ``select``
        timeout to zero and spin it while, say, the init master holds the pane.
        """
        now = time.time() if now is None else now
        stamps: list[float] = []
        start = self.mem.last_summary_at or self.mem.anchor
        if self.cfg.overseer_every_s and start:
            stamps.append(start + self.cfg.overseer_every_s)
        if not self.cfg.overseer_enabled:
            return min((s for s in stamps if s > now), default=None)
        if self.mem.pending:
            stamps.append(self.mem.last_pass_at + self.cfg.overseer_min_gap_s)
        if self.mem.starving_since and not self.mem.starve_fired:
            stamps.append(self.mem.starving_since + self.cfg.overseer_starve_s)
        if self.mem.box_since and not self.mem.box_fired:
            stamps.append(self.mem.box_since + BOX_S)
        for phase, since in self.mem.owner_since.items():
            if phase not in self.mem.owner_fired:
                stamps.append(since + self.cfg.overseer_owner_wait_s)
        if self.mem.seen_hold and not self.mem.hold_fired and self.mem.hold_since:
            stamps.append(self.mem.hold_since + self.cfg.overseer_hold_wait_s)
        stamps.extend(since + PUSH_GRACE_S for since in self.mem.push_since.values())
        future = [s for s in stamps if s > now]
        return min(future) if future else None


# -- the starvation map (pure) ----------------------------------------------
#: Root-blocker kinds, in the order the map lists ties.
BLOCKER_KINDS = ("failed", "excluded", "dated", "parked", "building", "unknown", "ready")


@dataclass
class Blocker:
    """One root blocker and how much of the backlog is stuck behind it."""

    phase: str
    kind: str
    blocks: int
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def starvation_map(
    graph: dict[str, set[str]],
    done: dict[str, str],
    excluded: set[str] | frozenset[str],
    in_flight: dict[str, str],
    *,
    examples: int = 5,
    dated: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """Which roots hold the open backlog back, and how much of it each holds.

    This is the analysis the owner used to do by hand when the swarm starved:
    walk every open, non-excluded phase down its unmet dependencies to the node
    where waiting *ends* — something excluded, failed, waiting for its date
    (``dated``), parked on the owner, building, not in the ledger at all, or
    ready but not launched — and count,
    per such root, how many backlog phases stand behind it transitively.

    ``in_flight`` maps phase -> ``"building"`` (a slot, the merge queue, a
    launch) or ``"parked"`` (waiting or parked on the owner).

    The walk stops at a root: a phase behind an excluded row that is itself
    behind a failed one counts under the excluded row, because that is the one a
    person would have to act on first. A blocked phase no root reaches sits in a
    dependency cycle and is listed as such. Iterative (reverse BFS from each
    root), so a long serial chain cannot hit the recursion limit.

    Returns ``{"backlog", "blocked", "ready", "blockers", "cycle"}`` where
    ``blockers`` is a list of :class:`Blocker` dicts, most-blocking first.
    """
    order = {p: i for i, p in enumerate(graph)}
    landed = {p for p, s in done.items() if s in statuses.SATISFIES_DEPS}

    def kind(p: str) -> str | None:
        if p in landed:
            return None
        if p not in graph:
            return "unknown"
        if p in excluded:
            return "excluded"
        if p in dated:
            return "dated"  # the swarm starts it on its day; it has not failed
        if p in done:
            return "failed"
        state = in_flight.get(p)
        if state is not None:
            return "parked" if state == "parked" else "building"
        return "ready" if graph[p] <= landed else "blocked"

    kinds: dict[str, str | None] = {}
    nodes = set(graph)
    for deps in graph.values():
        nodes |= deps
    for p in nodes:
        kinds[p] = kind(p)
    dependents: dict[str, list[str]] = {}
    for p, deps in graph.items():
        for d in deps:
            dependents.setdefault(d, []).append(p)

    reached: set[str] = set()
    blockers: list[Blocker] = []
    for root in nodes:
        k = kinds[root]
        if k is None or k == "blocked":
            continue
        seen: set[str] = set()
        frontier = [root]
        while frontier:
            nxt: list[str] = []
            for node in frontier:
                for dep in dependents.get(node, ()):
                    if dep not in seen and kinds.get(dep) == "blocked":
                        seen.add(dep)
                        nxt.append(dep)
            frontier = nxt
        reached |= seen
        if seen:
            ex = sorted(seen, key=lambda p: order.get(p, 1 << 30))[:examples]
            blockers.append(Blocker(root, k, len(seen), ex))
    blockers.sort(
        key=lambda b: (-b.blocks, BLOCKER_KINDS.index(b.kind), order.get(b.phase, 1 << 30), b.phase)
    )
    backlog = [p for p in graph if kinds[p] in ("ready", "blocked")]
    blocked = [p for p in backlog if kinds[p] == "blocked"]
    cycle = [p for p in blocked if p not in reached]
    return {
        "backlog": len(backlog),
        "blocked": len(blocked),
        "ready": [p for p in backlog if kinds[p] == "ready"],
        "blockers": [b.to_dict() for b in blockers],
        "cycle": cycle,
    }


def backlog(graph: dict[str, set[str]], done: dict[str, str], excluded, in_flight) -> list[str]:
    """Open work the swarm could still do: in the ledger, never attempted, not
    excluded, not in flight. What starvation is measured against."""
    return [
        p for p in graph if p not in done and p not in excluded and p not in in_flight
    ]


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds / 3600:.1f}h"
