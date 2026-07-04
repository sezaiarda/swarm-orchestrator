"""Dependency resolver over a phase ledger.

This is a deliberately small, explicit mini-parser used to compute the *ready*
set (phases whose dependencies are all done). Real projects let the LLM master
reason over the ledger prose directly; this resolver understands one documented
line format so the hermetic tests (and any project that adopts it) get
deterministic gating without an LLM:

    P0
    P1 needs:P0
    P4 needs:P1,P2   optional trailing note
    P5 needs: P1, P2   (whitespace after the colon / commas is tolerated)

Rules: the first whitespace token on a non-blank, non ``#`` line is the phase
id; a ``needs:`` token starts a comma-separated dependency list that may span
following whitespace-separated tokens. The list ends at the first token that is
not a continuation of it (no trailing comma / not a bare id), so an optional
trailing prose note is preserved. Lines that do not start with a bare identifier
token are ignored, so a prose markdown ledger simply yields no parsed deps.
"""

from __future__ import annotations

import re
from pathlib import Path

_PHASE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]*$")


def parse(text: str) -> dict[str, set[str]]:
    """Parse ledger text into ``{phase: {deps}}`` (whitespace-tolerant needs)."""
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
        in_needs = False
        for tok in tokens[1:]:
            t = tok
            if not in_needs:
                if not t.startswith("needs:"):
                    continue
                in_needs = True
                t = t[len("needs:") :]
            if t == "":
                # A bare ``needs:`` token — the dependencies are in the tokens
                # that follow it (``needs: P1, P2``); keep consuming.
                continue
            parts = [p for p in t.split(",") if p]
            if parts and all(_PHASE_RE.match(p) for p in parts):
                deps.update(parts)
                if not t.endswith(","):
                    # No trailing comma ⇒ the list ended on this token; any
                    # following tokens are an optional prose note, not deps.
                    in_needs = False
            else:
                # First non-dependency token ends the list (trailing note).
                in_needs = False
        graph[phase] = deps
    return graph


def load(path: Path) -> dict[str, set[str]]:
    """Parse a ledger file, or return an empty graph if it is missing."""
    if not path.is_file():
        return {}
    return parse(path.read_text(encoding="utf-8"))


def validate(graph: dict[str, set[str]]) -> list[str]:
    """Return human-readable structural problems that would silently stall a run.

    A self-dependency, a dependency on an unknown phase, or a dependency cycle
    all make the affected phases *never* become ready — an invisible stall that
    otherwise looks like a clean finish. Surfacing them lets the master/owner
    see and fix the ledger instead of the swarm quietly dropping those phases.
    """
    issues: list[str] = []
    known = set(graph)
    for phase, deps in graph.items():
        for d in sorted(deps):
            if d == phase:
                issues.append(f"self-dependency: {phase} needs itself")
            elif d not in known:
                issues.append(f"unknown dependency: {phase} needs {d} (no such phase)")

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {p: WHITE for p in graph}
    seen_cycles: list[str] = []

    def dfs(u: str, stack: list[str]) -> None:
        color[u] = GRAY
        for v in sorted(graph.get(u, set())):
            if v not in graph:
                continue
            if color[v] == GRAY:
                cyc = stack[stack.index(v) :] + [v]
                seen_cycles.append(" -> ".join(cyc))
            elif color[v] == WHITE:
                dfs(v, stack + [v])
        color[u] = BLACK

    for p in sorted(graph):
        if color[p] == WHITE:
            dfs(p, [p])
    for c in dict.fromkeys(seen_cycles):
        issues.append(f"dependency cycle: {c}")
    return issues


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
