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


@dataclass
class State:
    """The whole swarm's durable state (serialised to ``state.json``)."""

    slots: list[Slot] = field(default_factory=list)
    master_alive: bool = False
    master_pane: str | None = None
    windows: dict[str, str] = field(default_factory=dict)
    done: dict[str, str] = field(default_factory=dict)
    finished: bool = False
    supervisor_pid: int | None = None

    # -- slot accounting -------------------------------------------------
    def free_slots(self) -> list[Slot]:
        return [s for s in self.slots if not s.busy]

    def busy_slots(self) -> list[Slot]:
        return [s for s in self.slots if s.busy]

    def any_busy(self) -> bool:
        return any(s.busy for s in self.slots)

    def slot_by_id(self, sid: int) -> Slot | None:
        return next((s for s in self.slots if s.id == sid), None)

    def claim_slot(self, phase: str) -> Slot | None:
        """Mark the first free slot busy for ``phase`` (check-and-set).

        Returns ``None`` if no slot is free OR ``phase`` already occupies a slot
        — one phase never holds two slots, so a duplicate/stale ``swarm launch``
        (or a master acting on a stale context) can't strand a slot that
        ``done`` would never free.
        """
        if any(s.busy and s.phase == phase for s in self.slots):
            return None
        slot = next((s for s in self.slots if not s.busy), None)
        if slot is None:
            return None
        slot.busy = True
        slot.phase = phase
        return slot

    def free_slot_for(self, phase: str) -> Slot | None:
        """Release whichever slot currently runs ``phase``."""
        slot = next((s for s in self.slots if s.busy and s.phase == phase), None)
        if slot is None:
            return None
        slot.busy = False
        slot.phase = None
        return slot

    def mark_done(self, phase: str, status: str) -> None:
        self.done[phase] = status

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
            supervisor_pid=data.get("supervisor_pid"),
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
    """Create a fresh state file for a new run."""
    with transaction(cfg) as state:
        fresh = State.fresh(cfg.max_workers)
        fresh.windows = windows or {}
        fresh.supervisor_pid = None
        state.__dict__.update(fresh.__dict__)
        return state


def path_from_pane(state: State, pane_id: str) -> Slot | None:
    return next((s for s in state.slots if s.pane_id == pane_id), None)
