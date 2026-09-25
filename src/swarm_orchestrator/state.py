"""Persistent swarm state with flock-guarded read-modify-write.

State is the single source of truth for slot accounting. Slots are a fixed set
of ``max_workers`` entries, each pinned to a tmux pane id; a slot is *busy* when
it carries a phase. We never count raw tmux panes — teammate panes therefore can
never corrupt accounting. All mutation goes through :func:`transaction`, which
holds an exclusive ``flock`` for the duration, so ``swarm launch`` claims a slot
check-and-set (re-verify free, claim, release) before it ever respawns a pane.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from .config import Config


@dataclass
class Slot:
    """One fixed worker slot pinned to a tmux pane."""

    id: int
    pane_id: str | None = None
    busy: bool = False
    phase: str | None = None
    worktree: str | None = None  # isolation=worktree: the phase's umbrella worktree path
    branch: str | None = None  # isolation=worktree: swarm/<phase>
    # Marked for removal by a LIVE ``max_workers`` shrink (:meth:`State.resize`).
    # A retiring slot is invisible to ``free_slots``/``claim_slot`` so nothing new
    # lands in it, but a busy one is never evicted — it drains and is then dropped
    # by :meth:`State.reap_retired`.
    retiring: bool = False


@dataclass
class State:
    """The whole swarm's durable state (serialised to ``state.json``)."""

    slots: list[Slot] = field(default_factory=list)
    master_alive: bool = False
    master_pane: str | None = None
    dash_pane: str | None = None
    windows: dict[str, str] = field(default_factory=dict)
    done: dict[str, str] = field(default_factory=dict)
    finished: bool = False
    paused: bool = False
    supervisor_pid: int | None = None
    # isolation=worktree merge-queue (orthogonal to the master lifecycle). Phases
    # whose ``ok`` worker finished queue in ``integ_queue`` and are integrated
    # into the project ``main`` one-at-a-time by the supervisor. A merge conflict
    # parks the head phase in ``integ_blocked`` and HOLDS the whole queue (no
    # further integrations) until a transient resolver signals
    # ``swarm resolved <phase>`` — so the FIFO reader never blocks on the human.
    integ_queue: list[str] = field(default_factory=list)
    # Completion status (``ok`` / ``needs-owner``) of each queued phase, keyed by
    # phase. The queue itself stays a plain list of ids (it is what ``swarm status``
    # prints), so the status rides alongside it instead of inside it. Kept in sync
    # by :meth:`integ_push` / :meth:`integ_pop` — never mutate ``integ_queue``
    # directly. A phase missing here reads as ``ok`` (pre-status state files).
    integ_status: dict[str, str] = field(default_factory=dict)
    integ_blocked: str | None = None
    integ_blocked_repo: str | None = None  # repo path a CONFLICT/DIRTY hold is in
    integ_blocked_kind: str | None = None  # conflict | dirty | push_failed (legacy)
    # Repos whose main is merged locally but not yet on origin, keyed by repo
    # path: ``{"phase", "reason", "since", "refused", "tried"}``. A failed push no
    # longer holds the queue (workers branch from local main, so nothing waits on
    # origin); it is owed instead, retried after each integration and on the
    # watchdog, and cleared the moment origin has it. Optional on both sides of a
    # version skew: an older state file loads with none, and an older supervisor
    # drops the key on its next write — the next failed push records it again.
    push_owed: dict[str, dict] = field(default_factory=dict)
    # A worker that needs the owner self-reports via `swarm waiting`. Its phase is
    # recorded in ``waiting`` with a park DEADLINE (epoch seconds, so it survives a
    # supervisor restart); when the deadline fires the supervisor ``park``s it —
    # moving its live pane to its own window and freeing the grid slot — and the
    # phase moves to ``parked``. A ``parked`` worker owes the owner an answer but
    # no longer holds a slot; it clears only on ``swarm done``.
    waiting: dict[str, float] = field(default_factory=dict)
    parked: list[str] = field(default_factory=list)
    # Live pane arrangement of the worker windows, set by ``swarm layout``.
    # ``None`` = follow ``[tmux].layout``; a value here wins for the rest of the
    # run (so a park re-tidy can't revert what the owner just chose). Reset on
    # every ``up``, since the config is re-read there.
    layout: str | None = None
    # Epoch seconds of the last event the supervisor handled. The supervisor's
    # watchdog measures a stall against this; without it state.json carries no
    # timestamp at all, so a long silence was unobservable.
    last_event_at: float = 0.0
    # The one operator session, held under a LEASE rather than a pane probe.
    # ``operator_pane`` is durable (written by ``session.setup`` beside
    # ``master_pane``); the other two are the lease and are cleared when the
    # session ends. ``init_state`` wipes all three on ``up``, which is correct: a
    # supervisor restart means no operator is running.
    operator_phase: str | None = None
    operator_lease_until: float = 0.0
    operator_pane: str | None = None
    # The Overseer pass running in the master pane, if any, and the moment it is
    # killed as hung. The deadline stretches while the pass waits on the owner
    # (`swarm overseer-ask`) and resets when they answer. Both are wiped by
    # ``init_state`` on ``up``: a restart means no pass is running. Optional on
    # both sides of a version skew, like ``push_owed``.
    overseer_pass: str | None = None
    overseer_deadline: float = 0.0
    # The open run (``swarm up`` → ``swarm down``, or a ``swarm reset``): the
    # epoch every live ETA and usage figure counts from. A mirror for readers of
    # this file only — the authority is ``history/current.json`` (see
    # :mod:`runs`), because a supervisor on older code rewrites this file without
    # keys it does not know. Optional on both sides, like ``push_owed``.
    run_id: str | None = None
    run_epoch: float = 0.0
    # True from ``swarm up`` until the supervisor stops holding the first launch
    # for the init pass (it idled, died, or never started). Written for
    # ``swarm doctor``: free slots beside ready phases are the plan in that
    # window, not a lost nudge, and only the supervisor's memory knew it.
    bootstrapping: bool = False

    # -- slot accounting -------------------------------------------------
    def free_slots(self) -> list[Slot]:
        return [s for s in self.slots if not s.busy and not s.retiring]

    def busy_slots(self) -> list[Slot]:
        return [s for s in self.slots if s.busy]

    def any_busy(self) -> bool:
        return any(s.busy for s in self.slots)

    def pending(self) -> bool:
        """True while any phase still owes work: a busy slot, a worker waiting on
        the owner, or a parked worker. The finish guard uses this instead of
        ``any_busy`` so a parked worker (which has freed its slot) still blocks
        finish until it runs ``swarm done``."""
        return self.any_busy() or bool(self.parked) or bool(self.waiting)

    def slot_by_id(self, sid: int) -> Slot | None:
        return next((s for s in self.slots if s.id == sid), None)

    def claim_slot(self, phase: str) -> Slot | None:
        """Mark the first free slot busy for ``phase`` (check-and-set).

        Returns ``None`` if no slot is free OR ``phase`` already occupies a slot
        — one phase never holds two slots, so a duplicate/stale ``swarm launch``
        (or a master acting on a stale context) can't strand a slot that
        ``done`` would never free. Two concurrent phases *may* target the same
        repo: each builds in its own worktree/branch, so there is no per-repo cap.

        A ``parked`` or ``waiting`` phase counts as occupied even though a parked
        one holds no slot: its worker is alive off-grid on ``swarm/<phase>``. Only
        consulting ``busy`` let a manual ``swarm launch <phase>`` claim a second
        slot for it and hand ``worktree_add`` a branch it then force-deletes out
        from under the live worker.
        """
        if phase in self.parked or phase in self.waiting:
            return None
        if any(s.busy and s.phase == phase for s in self.slots):
            return None
        slot = next((s for s in self.slots if not s.busy and not s.retiring), None)
        if slot is None:
            return None
        slot.busy = True
        slot.phase = phase
        slot.worktree = None
        slot.branch = None
        return slot

    def free_slot_for(self, phase: str) -> Slot | None:
        """Release whichever slot currently runs ``phase``."""
        slot = next((s for s in self.slots if s.busy and s.phase == phase), None)
        if slot is None:
            return None
        slot.busy = False
        slot.phase = None
        slot.worktree = None
        slot.branch = None
        return slot

    def mark_done(self, phase: str, status: str) -> None:
        self.done[phase] = status

    def park(self, phase: str) -> None:
        """Move a waiting phase off the grid into the parked set.

        Frees its busy slot (``free_slot_for`` nulls the phase but keeps the slot's
        ``pane_id``, so a replacement worker can respawn into it) and records the
        phase as parked — its worker keeps building/waiting on ``swarm/<phase>`` in
        its own window. Idempotent on the parked list."""
        self.free_slot_for(phase)
        self.waiting.pop(phase, None)
        if phase not in self.parked:
            self.parked.append(phase)

    def clear_pending(self, phase: str) -> bool:
        """Drop a finished phase from the waiting/parked tracking. Returns whether
        it had been parked (so the caller can close its ``wait:<phase>`` window)."""
        self.waiting.pop(phase, None)
        if phase in self.parked:
            self.parked.remove(phase)
            return True
        return False

    def clear_phase(self, phase: str, status: str | None = None) -> bool:
        """Drop every trace of an in-flight phase, optionally recording a status.

        ``clear_pending`` only clears the waiting/parked tracking and is reached
        solely from the supervisor's ``done`` path, so an out-of-band terminal
        command (``swarm skip`` / ``swarm free``) on a phase that was ``parked`` or
        ``waiting`` left it pending forever: ``pending()`` stayed true, the finish
        guard never released, and no CLI could clear it. This is the one call that
        settles all four places a phase can be recorded — ``done``, its slot,
        ``waiting`` and ``parked``. Returns whether it had been parked (so the
        caller can close its ``wait:<phase>`` window)."""
        if status is not None:
            self.mark_done(phase, status)
        self.free_slot_for(phase)
        return self.clear_pending(phase)

    # -- the operator session (exactly one, lease-guarded) ---------------
    def operator_busy(self, now: float) -> bool:
        """Is a live, unexpired operator lease held right now?"""
        return self.operator_phase is not None and self.operator_lease_until > now

    def claim_operator(self, phase: str, until: float, now: float) -> bool:
        """Take the single operator lease for ``phase`` (check-and-set).

        A lease, never a pane probe. ``session.setup`` leaves the operator window
        holding ``sleep infinity``, which is a live pane — so an ``operator_alive``
        written the way :meth:`master.Master.is_alive` is would answer True before
        any session had ever run, and the operator would never launch at all. That
        fails closed *and* silent, the exact inverse of the bug that docstring
        records. An expired lease is reclaimable for the same reason a hung item
        is: a session that dies must not pin the queue forever.
        """
        if self.operator_busy(now):
            return False
        self.operator_phase = phase
        self.operator_lease_until = until
        return True

    def release_operator(self) -> str | None:
        """Drop the lease; returns the phase it held (``None`` if it held none).

        Leaves ``operator_pane`` alone — that is the window's durable pane id, not
        part of the lease, and the caller still has to clear the pane with it.
        """
        phase = self.operator_phase
        self.operator_phase = None
        self.operator_lease_until = 0.0
        return phase

    # -- live resize (max_workers changed mid-run) -----------------------
    def resize(self, n: int) -> tuple[int, int]:
        """Change the worker count in place; return ``(added, retiring)``.

        Growing appends fresh slots. Shrinking must never evict a live worker, so
        the surplus is only *marked* ``retiring``: free ones are dropped by the
        next :meth:`reap_retired`, busy ones keep running and are dropped when
        their phase completes. Busy slots are kept in preference to free ones, so
        a shrink costs nothing in flight."""
        for slot in self.slots:
            slot.retiring = False
        ranked = sorted(self.slots, key=lambda s: (not s.busy, s.id))
        for slot in ranked[n:]:
            slot.retiring = True
        added = 0
        next_id = max((s.id for s in self.slots), default=-1) + 1
        while len(ranked) + added < n:
            self.slots.append(Slot(id=next_id + added))
            added += 1
        return added, sum(1 for s in self.slots if s.retiring)

    def reap_retired(self) -> list[int]:
        """Drop retiring slots that have gone idle; return the reaped slot ids."""
        gone = [s.id for s in self.slots if s.retiring and not s.busy]
        if gone:
            self.slots = [s for s in self.slots if not (s.retiring and not s.busy)]
        return gone

    # -- merge queue (phase ids in `integ_queue`, statuses alongside) -----
    def integ_push(self, phase: str, status: str) -> None:
        """Queue ``phase`` for integration, remembering the status it finished
        with. A phase already queued keeps its original position and status."""
        if phase in self.integ_queue:
            return
        self.integ_queue.append(phase)
        self.integ_status[phase] = status

    def integ_head(self) -> tuple[str, str] | None:
        """The phase at the head of the merge queue and the status it reported,
        or ``None`` when the queue is empty."""
        if not self.integ_queue:
            return None
        phase = self.integ_queue[0]
        return phase, self.integ_status.get(phase, "ok")

    def integ_pop(self, phase: str) -> bool:
        """Drop ``phase`` from the head of the queue (and its status)."""
        if not self.integ_queue or self.integ_queue[0] != phase:
            return False
        self.integ_queue.pop(0)
        self.integ_status.pop(phase, None)
        return True

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        slots = [Slot(**s) for s in data.get("slots", [])]
        return cls(
            slots=slots,
            master_alive=data.get("master_alive", False),
            master_pane=data.get("master_pane"),
            dash_pane=data.get("dash_pane"),
            windows=data.get("windows", {}),
            done=data.get("done", {}),
            finished=data.get("finished", False),
            paused=data.get("paused", False),
            supervisor_pid=data.get("supervisor_pid"),
            integ_queue=list(data.get("integ_queue", [])),
            integ_status=dict(data.get("integ_status", {})),
            integ_blocked=data.get("integ_blocked"),
            integ_blocked_repo=data.get("integ_blocked_repo"),
            integ_blocked_kind=data.get("integ_blocked_kind"),
            push_owed=dict(data.get("push_owed") or {}),
            waiting=dict(data.get("waiting", {})),
            parked=list(data.get("parked", [])),
            layout=data.get("layout"),
            last_event_at=float(data.get("last_event_at", 0.0)),
            operator_phase=data.get("operator_phase"),
            operator_lease_until=float(data.get("operator_lease_until", 0.0)),
            operator_pane=data.get("operator_pane"),
            overseer_pass=data.get("overseer_pass"),
            overseer_deadline=float(data.get("overseer_deadline") or 0.0),
            run_id=data.get("run_id"),
            run_epoch=float(data.get("run_epoch") or 0.0),
            bootstrapping=bool(data.get("bootstrapping", False)),
        )

    @classmethod
    def fresh(cls, max_workers: int) -> "State":
        return cls(slots=[Slot(id=i) for i in range(max_workers)])


def _load(cfg: Config) -> State:
    path = cfg.state_path
    if not path.is_file():
        return State.fresh(cfg.max_workers)
    with path.open("r", encoding="utf-8") as fh:
        return State.from_dict(json.load(fh))


def _save(cfg: Config, state: State) -> None:
    path = cfg.state_path
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state.to_dict(), fh, indent=2)
    os.replace(tmp, path)  # atomic swap


def read(cfg: Config) -> State:
    """Read state under a shared lock (no mutation)."""
    cfg.ensure_dirs()
    with cfg.lock_path.open("w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_SH)
        try:
            return _load(cfg)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


@contextmanager
def transaction(cfg: Config) -> Iterator[State]:
    """Exclusive read-modify-write. The lock is held for the whole block."""
    cfg.ensure_dirs()
    with cfg.lock_path.open("w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        try:
            state = _load(cfg)
            yield state
            _save(cfg, state)
        finally:
            fcntl.flock(lockf, fcntl.LOCK_UN)


def init_state(cfg: Config, windows: dict[str, str] | None = None) -> State:
    """Rebuild the run's slots from config, preserving completed-phase progress.

    ``swarm up`` re-derives the slot list from ``max_workers`` (so a changed
    worker count / isolation takes effect on the next boot — the only supported
    way to resize a swarm), but carries the ``done`` record over from any existing
    state file. Reboot is the documented way to change the slot count, so it must
    not re-run already-finished phases. A genuinely clean slate = delete the state
    dir. First boot has no prior file, so ``done`` starts empty as before. Owed
    pushes carry over for the same reason: the unpushed commits are still there.
    """
    with transaction(cfg) as state:
        prior_done = dict(state.done)
        fresh = State.fresh(cfg.max_workers)
        fresh.windows = windows or {}
        fresh.supervisor_pid = None
        fresh.done = prior_done
        # A restart does not push anything, so a debt survives it like `done` does.
        fresh.push_owed = dict(state.push_owed)
        fresh.bootstrapping = True  # the supervisor clears it (see the field)
        state.__dict__.update(fresh.__dict__)
        return state


def path_from_pane(state: State, pane_id: str) -> Slot | None:
    return next((s for s in state.slots if s.pane_id == pane_id), None)
