"""Dependency resolver over a phase ledger.

This is a deliberately small, explicit mini-parser used to compute the *ready*
set (phases whose dependencies are all done). Real projects let the LLM master
reason over the ledger prose directly; this resolver understands one documented
line format so the hermetic tests (and any project that adopts it) get
deterministic gating without an LLM:

    P0
    P1 needs:P0
    P4 needs:P1,P2   optional trailing note

Rules: the first whitespace token on a non-blank, non ``#`` line is the phase
id; an optional ``needs:`` token carries comma-separated dependency ids. Lines
that do not start with a bare identifier token are ignored, so a prose markdown
ledger simply yields no parsed deps rather than garbage.
"""

from __future__ import annotations

import re
from pathlib import Path

_PHASE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]*$")


def parse(text: str) -> dict[str, set[str]]:
    """Parse ledger text into ``{phase: {deps}}``."""
    graph: dict[str, set[str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        phase = tokens[0]
        if not _PHASE_RE.match(phase):
            continue
        deps: set[str] = set()
        for tok in tokens[1:]:
            if tok.startswith("needs:"):
                body = tok[len("needs:") :]
                deps.update(d for d in body.split(",") if d)
        graph[phase] = deps
    return graph


def load(path: Path) -> dict[str, set[str]]:
    """Parse a ledger file, or return an empty graph if it is missing."""
    if not path.is_file():
        return {}
    return parse(path.read_text(encoding="utf-8"))


def ready(
    graph: dict[str, set[str]],
    done: dict[str, str],
    busy_phases: set[str],
    excluded: set[str],
) -> list[str]:
    """Phases that can be launched right now.

    A phase is ready when it is not done, not currently busy, not excluded, and
    every dependency is present in ``done``. Order follows the ledger's
    declaration order for stable, critical-path-ish selection.
    """
    done_set = set(done)
    result: list[str] = []
    for phase, deps in graph.items():
        if phase in done_set or phase in busy_phases or phase in excluded:
            continue
        if deps <= done_set:
            result.append(phase)
    return result
