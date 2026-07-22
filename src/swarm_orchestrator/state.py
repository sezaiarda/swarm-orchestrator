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


@dataclass
class State:
    """The whole swarm's durable state (serialised to ``state.json``)."""

    slots: list[Slot] = field(default_factory=list)
    master_alive: bool = False
    master_pane: str | None = None
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
    integ_blocked: str | None = None
    integ_blocked_repo: str | None = None  # repo path a CONFLICT/DIRTY hold is in
    integ_blocked_kind: str | None = None  # conflict | dirty | push_failed
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

    # -- slot accounting -------------------------------------------------
    def free_slots(self) -> list[Slot]:
        return [s for s in self.slots if not s.busy]

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
        """
        if any(s.busy and s.phase == phase for s in self.slots):
            return None
        slot = next((s for s in self.slots if not s.busy), None)
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
            windows=data.get("windows", {}),
            done=data.get("done", {}),
            finished=data.get("finished", False),
            paused=data.get("paused", False),
            supervisor_pid=data.get("supervisor_pid"),
            integ_queue=list(data.get("integ_queue", [])),
            integ_blocked=data.get("integ_blocked"),
            integ_blocked_repo=data.get("integ_blocked_repo"),
            integ_blocked_kind=data.get("integ_blocked_kind"),
            waiting=dict(data.get("waiting", {})),
            parked=list(data.get("parked", [])),
            layout=data.get("layout"),
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
    dir. First boot has no prior file, so ``done`` starts empty as before.
    """
    with transaction(cfg) as state:
        prior_done = dict(state.done)
        fresh = State.fresh(cfg.max_workers)
        fresh.windows = windows or {}
        fresh.supervisor_pid = None
        fresh.done = prior_done
        state.__dict__.update(fresh.__dict__)
        return state


def path_from_pane(state: State, pane_id: str) -> Slot | None:
    return next((s for s in state.slots if s.pane_id == pane_id), None)
