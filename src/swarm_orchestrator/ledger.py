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

from . import statuses

# Re-exported so ``ledger.SATISFIES_DEPS`` keeps meaning what it always has; the
# definition (and the reasoning) lives in :mod:`statuses` now.
SATISFIES_DEPS = statuses.SATISFIES_DEPS

_PHASE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]*$")
# A markdown checklist item: ``- [x] `phase-id` · …`` / ``* [ ] `phase-id```.
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+(.+)$")
_TICKED_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]\s+(.+)$")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
# Fields of a markdown ledger line are separated by ``space MIDDLE-DOT space``
# (``- [x] `id` · needs:`dep` · dir:`d` · …``); dep extraction is scoped to the
# single ``needs:``-prefixed field so ``dir:``/``TAG:`` back-ticks never leak in.
_FIELD_SEP = " · "


def parse(text: str) -> dict[str, set[str]]:
    """Parse ledger text into ``{phase: {deps}}``, auto-detecting its shape.

    Two shapes are supported:

    * **Markdown checklist** — a real project's human/LLM-facing ledger, whose
      phase lines are ``- [x] `frontend-P1` · needs:`frontend-P0` `bundle-v0.1.0` · …``
      amid prose notes. If *any* line is a checklist item the whole file is read
      as markdown: **only** checklist items yield phases (the first back-ticked
      token is the id), so prose notes never leak in as phantom phases. The
      ``needs:`` field's back-ticked tokens become that phase's deps, filtered to
      *known phase ids* — git tags (``bundle-v0.1.0``), the ``needs:—`` root, and
      prose words drop, while cross-repo phase deps are kept.
    * **Bare** — ``P0`` / ``P1 needs:P0,P2 trailing note``, the whitespace-tolerant
      one-line format the hermetic tests (and any project that adopts it) use for
      deterministic gating without an LLM.

    A prose markdown ledger with no checklist items yields no parsed phases.
    """
    lines = text.splitlines()
    if any(_CHECKBOX_RE.match(ln) for ln in lines):
        return _parse_markdown(lines)
    return _parse_bare(lines)


def _parse_markdown(lines: list[str]) -> dict[str, set[str]]:
    """Phase ids (and their ``needs:`` deps) from checklist items only.

    The first back-ticked token of a checklist line is the phase id; the
    ``needs:``-prefixed field carries its dependency tokens
    (``- [ ] `inventory-P4` · needs:`inventory-P3` · …``). Once every phase is known, each
    phase's deps are filtered down to *known phase ids* — git tags
    (``bundle-v0.1.0``), the ``needs:—`` root, and prose words drop, while cross-repo
    phase deps (``I4`` needs ``payments-P4``) are kept. Surrounding prose yields no
    phase, so it can never be misread as a phantom deps-free phase.
    """
    graph: dict[str, set[str]] = {}
    raw_deps: dict[str, set[str]] = {}
    for raw in lines:
        m = _CHECKBOX_RE.match(raw)
        if not m:
            continue
        ids = _BACKTICK_RE.findall(m.group(1))
        if ids and _PHASE_RE.match(ids[0]):
            phase = ids[0]
            graph.setdefault(phase, set())
            raw_deps[phase] = _markdown_needs(m.group(1))
    known = set(graph)
    for phase in graph:
        graph[phase] = {d for d in raw_deps[phase] if d in known}
    return graph


def _markdown_needs(content: str) -> set[str]:
    """Back-ticked tokens of a markdown checklist line's ``needs:`` field.

    Scoped to the single ``·``-separated field beginning with ``needs:`` so a
    sibling ``dir:`…``` / ``TAG:`…``` back-tick is never mistaken for a dep. The
    returned tokens are still *candidates* — :func:`_parse_markdown` filters them
    to known phase ids once the whole graph is built.
    """
    for field in content.split(_FIELD_SEP):
        if field.strip().strip("*").strip().startswith("needs:"):
            return set(_BACKTICK_RE.findall(field))
    return set()


def _parse_bare(lines: list[str]) -> dict[str, set[str]]:
    """The bare one-line format: ``<id> [needs:<deps>] [prose note]`` (tests)."""
    graph: dict[str, set[str]] = {}
    for raw in lines:
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


def ticked(text: str) -> set[str]:
    """Ids of the markdown checklist rows ticked ``[x]`` (none in the bare format)."""
    out: set[str] = set()
    for raw in text.splitlines():
        m = _TICKED_RE.match(raw)
        if not m:
            continue
        ids = _BACKTICK_RE.findall(m.group(1))
        if ids and _PHASE_RE.match(ids[0]):
            out.add(ids[0])
    return out


def load_ticked(path: Path) -> set[str]:
    """:func:`ticked` over a ledger file; empty when it is missing."""
    if not path.is_file():
        return set()
    return ticked(path.read_text(encoding="utf-8"))


def with_ticked(
    done: dict[str, str],
    ticked_ids: set[str] | frozenset[str],
    in_flight=frozenset(),
) -> dict[str, str]:
    """``done`` plus a :data:`statuses.LEDGER` entry for each ticked row it lacks.

    The ledger's ``[x]`` is the owner's statement that a row is built, and the
    web board shows such a row as Done; the launcher reads the same view, so a
    row built before the swarm (or by hand) is neither rebuilt nor left blocking
    its dependents. It is a *view*, never written back: a real record always
    wins, so a ticked row recorded ``fail`` stays failed until ``swarm retry``.
    A phase in flight is its worker's to finish, so a tick that lands while it
    builds does not release its dependents early.
    """
    view = {p: statuses.LEDGER for p in ticked_ids if p not in in_flight}
    view.update(done)
    return view


def validate(graph: dict[str, set[str]], landed: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Return human-readable structural problems that would silently stall a run.

    ``landed`` is the set of phases whose work is already merged (a
    :data:`SATISFIES_DEPS` status). A landed phase can stall nothing: nobody
    waits on it, and its own ``needs:`` are history. So its edges are not
    walked and a cycle that runs through it is not a cycle anyone can be stuck
    in. Without this, ordering edges added to a long-lived ledger after the fact
    (a closed row made to "need" a later open one) read as live cycles, and a master
    told to stop on ``ledger_issues`` halted a run that had nothing wrong with it.

    A self-dependency, a dependency on an unknown phase, or a dependency cycle
    all make the affected phases *never* become ready — an invisible stall that
    otherwise looks like a clean finish. Surfacing them lets the master/owner
    see and fix the ledger instead of the swarm quietly dropping those phases.
    """
    issues: list[str] = []
    known = set(graph)
    graph = {p: deps for p, deps in graph.items() if p not in landed}
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

    A phase is ready when it has not been *attempted*, is not currently busy, is
    not excluded, and every dependency has actually *landed*.

    Those are two different readings of the ``done`` map and conflating them is a
    correctness bug: a phase recorded ``fail`` has been attempted (so it must not
    be silently re-offered — an explicit ``swarm retry`` is the reset) but its work
    was never merged; under ``isolation = worktree`` its branch was discarded
    outright. Testing dependencies against bare membership therefore declared a
    dependent ready and let it build against a ``main`` that provably lacks the
    dependency it needs. Only :data:`SATISFIES_DEPS` statuses release a dependent.

    Order follows the ledger's declaration order for stable, critical-path-ish
    selection.
    """
    satisfied = {p for p, status in done.items() if status in SATISFIES_DEPS}
    attempted = set(done)
    result: list[str] = []
    for phase, deps in graph.items():
        if phase in attempted or phase in busy_phases or phase in excluded:
            continue
        if deps <= satisfied:
            result.append(phase)
    return result


def blocked_behind(
    graph: dict[str, set[str]],
    phase: str,
    done: dict[str, str],
    excluded: set[str] | frozenset[str] = frozenset(),
) -> int:
    """How many open phases stand behind ``phase``, transitively.

    What a question costs while it waits: every phase that needs ``phase`` —
    directly or through another open phase — cannot start until it lands. A
    dependent already in ``done`` (landed, skipped or failed) is not waiting on
    anything and the walk does not go through it; an excluded one will never run,
    so it is not counted either. Iterative, so a long serial chain cannot hit the
    recursion limit.
    """
    dependents: dict[str, list[str]] = {}
    for p, deps in graph.items():
        for d in deps:
            dependents.setdefault(d, []).append(p)
    seen: set[str] = set()
    frontier = [phase]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            for dep in dependents.get(node, ()):
                if dep in seen or dep == phase or dep in done or dep in excluded:
                    continue
                seen.add(dep)
                nxt.append(dep)
        frontier = nxt
    return len(seen)
