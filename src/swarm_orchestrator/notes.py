"""The silent third register: a worker's decisions, logged without a ping.

A worker has only ever had two ways to talk to the owner, and their costs are
wildly asymmetric. ``swarm waiting`` costs a telegram, a park deadline, the grid
slot, a pane relocated into its own window, and an unbounded stall. ``swarm done
ok "btw I decided X"`` costs nothing and ends the session. Any worker optimising
for completion picks the second, so spec-level contradictions get resolved
unilaterally and buried in recaps instead of being asked about.

Burying them was not concealment. It was the only channel available, and it is
write-only: :func:`swarm_orchestrator.gitq.sentinel_done` parses the sentinel
*filename* and never opens the file, so nothing in the tool has ever read a recap
back. A decision recorded in a recap is written to disk and read by nobody.

This module is the missing middle. A note pings nothing, parks nothing and costs
no slot; it records "I decided X, reversible via Y" where the owner can review it
in a batch afterwards (``swarm report --decisions``, the dashboard's History tab,
and one summary line in the finish telegram). That keeps ``needs-owner`` for what
it is for: a worker asking a question that genuinely needs the
owner to answer — instead of it absorbing every judgement call a
worker wants on the record.

Storage is one JSONL file per phase under ``<state_dir>/notes/``: append-only, so
two concurrent writes cannot lose each other and a crash mid-write costs at most
the final line.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

KINDS = ("decision", "assumption", "risk")

#: The owner's answer to a question a session asked, recorded by ``swarm
#: resumed`` (a worker's, an operator job's or the Overseer's). Not in :data:`KINDS`
#: because no session writes one with ``swarm note``: it is the owner's call,
#: relayed, and the history is only worth reading if the two never blur.
OWNER_DECISION = "owner_decision"
ALL_KINDS = KINDS + (OWNER_DECISION,)

#: The notes key the Overseer's owner answers are filed under. One fixed key, not
#: the pass id: its calls are one running history, and a key per pass would
#: scatter them across the report.
OVERSEER = "overseer"


@dataclass
class Note:
    """One recorded judgement call."""

    phase: str
    kind: str
    text: str
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"ts": self.ts, "phase": self.phase, "kind": self.kind, "text": self.text}


def _notes_dir(cfg: Config) -> Path:
    return cfg.state_dir / "notes"


def add(cfg: Config, phase: str, text: str, kind: str = "decision") -> Note:
    """Append a note. Never raises — a lost note must not fail a worker's turn.

    An identical ``(phase, kind, text)`` already on file is returned instead of
    written again: a session that retries a command it was unsure landed would
    otherwise say the same thing twice in the history, and the recorded runs had
    exactly those duplicates.
    """
    if kind not in ALL_KINDS:
        kind = "decision"
    note = Note(phase=phase, kind=kind, text=text.strip())
    for prior in load(cfg, phase):
        if prior.kind == note.kind and prior.text == note.text:
            return prior
    try:
        d = _notes_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        with (d / f"{phase}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(note.as_dict(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    return note


def load(cfg: Config, phase: str) -> list[Note]:
    """Every note for one phase, oldest first. Missing/partial files yield []."""
    path = _notes_dir(cfg) / f"{phase}.jsonl"
    out: list[Note] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            out.append(
                Note(
                    phase=str(d.get("phase", phase)),
                    kind=str(d.get("kind", "decision")),
                    text=str(d.get("text", "")),
                    ts=float(d.get("ts", 0.0)),
                )
            )
        except (ValueError, TypeError):
            continue  # a torn final line costs that line, not the file
    return out


def load_all(cfg: Config) -> dict[str, list[Note]]:
    """Every phase's notes, keyed by phase. Absent directory yields {}."""
    d = _notes_dir(cfg)
    if not d.is_dir():
        return {}
    out: dict[str, list[Note]] = {}
    for entry in sorted(d.glob("*.jsonl")):
        notes = load(cfg, entry.stem)
        if notes:
            out[entry.stem] = notes
    return out


def owner_answer(cfg: Config, phase: str, answer: str, question: str = "") -> Note | None:
    """Record the owner's answer to ``phase``'s question as an ``owner_decision``.

    The answer used to live only in the session that asked, so the history said
    what workers decided and never what the owner did. ``question`` is kept as a
    short tail for context; an empty answer records nothing (there is nothing to
    read back).
    """
    text = " ".join((answer or "").split())
    if not text:
        return None
    asked = " ".join((question or "").split())
    if asked:
        asked = asked if len(asked) <= 200 else asked[:199].rstrip() + "…"
        text = f"{text} (asked: {asked})"
    return add(cfg, phase, text, OWNER_DECISION)
