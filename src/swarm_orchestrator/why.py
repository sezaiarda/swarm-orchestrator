"""``swarm why <phase>`` — why is this phase not running?

``swarm context`` answers "what is ready" with a bare list. When that list is
empty — the single most common way a run looks stuck — it says nothing at all
about *why*, and the master (whose ``/prime`` already tells workers ``swarm why``
exists) has no way to tell "the ledger never mentioned this phase" from "it
finished an hour ago" from "one ancestor eight levels down failed and took the
whole subtree with it".

:func:`explain` classifies one phase against the ledger + live state, and when it
is blocked it walks the dependency graph to the **root cause**: the deepest
unmet ancestor that itself has nothing unmet. That is the single phase you can
actually act on; every other name in the tree is just waiting on it.

Dependency satisfaction here is :data:`ledger.SATISFIES_DEPS` — imported, never
re-declared, so the resolver and this explanation cannot drift into disagreeing
about what "blocked" means. It is not bare
membership in the ``done`` map: a phase recorded ``fail`` was *attempted* but its
branch was discarded, so it releases nothing. That distinction is the whole point
of the tree — a ``fail`` root reads as "one phase failed", not "these six phases
are mysteriously not ready".
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import caps
from . import freezer
from . import master as master_mod
from . import ledger as ledger_mod
from . import ledgerw
from . import state as state_mod
from . import statuses
from .config import Config
from .state import State

# Classification keys. Stable — ``--json`` consumers (and the master) key on them.
UNKNOWN = "not-in-ledger"
EXCLUDED = "excluded"
BUSY = "busy"
INTEGRATING = "integrating"
INTEG_BLOCKED = "integ-blocked"
PARKED = "parked"
WAITING = "waiting"
DONE = "done"
BLOCKED = "blocked"
DEFERRED = "deferred"
READY = "ready"

# Node labels used only in the rendered tree (a superset of the keys above:
# a dep can also be an already-landed ancestor we do not recurse into).
_SATISFIED = "satisfied"
_CYCLE = "cycle"
_SHOWN = "shown"


@dataclass
class Node:
    """One phase in the rendered dependency tree."""

    phase: str
    state: str
    detail: str = ""
    root: bool = False
    children: list["Node"] = field(default_factory=list)


@dataclass
class Explanation:
    """Why ``phase`` is not running, and what to do about it."""

    phase: str
    reason: str
    detail: str
    status: str | None = None  # the recorded `done` status, when reason == DONE
    unmet: list[str] = field(default_factory=list)  # direct deps not yet landed
    deps: list[str] = field(default_factory=list)  # every declared dep
    roots: list[str] = field(default_factory=list)  # actionable root causes
    root_cause: str | None = None  # the single root, when there is exactly one
    root_detail: str = ""  # that root's own one-line classification
    tree: Node | None = None
    issues: list[str] = field(default_factory=list)  # ledger cycles / unknown deps

    def to_dict(self) -> dict:
        return asdict(self)


def explain(cfg: Config, phase: str, st: State | None = None) -> Explanation:
    """Classify ``phase`` against the ledger and the live run.

    ``st`` is read under the shared state lock when not supplied. The order of
    the checks is the order a human would ask them in — "does this phase even
    exist" before "is it done" before "what is it waiting for" — so the first
    answer that fits is also the most useful one.
    """
    if st is None:
        st = state_mod.read(cfg)
    path = cfg.project_dir / cfg.ledger
    graph = ledger_mod.load(path)
    # Read the done map the launcher reads: a ticked row it holds no record of is
    # landed. A copy, so the caller's state is never touched.
    flying = {s.phase for s in st.busy_slots() if s.phase} | set(st.parked) | set(st.waiting)
    # The launcher's own reading of the dates still ahead, today in UTC. A row
    # that finished `later` waits for one: its record is not a failure.
    dated = ledgerw.dated(cfg)
    st = dataclasses.replace(
        st, done=ledger_mod.with_ticked(
            ledgerw.not_failed(st.done, dated), ledger_mod.load_ticked(path), flying)
    )
    exp = _classify(cfg, phase, st, graph, dated)
    if exp.reason != BLOCKED:
        return exp

    exp.deps = sorted(graph.get(phase, set()))
    exp.unmet = _unmet(graph, st.done, phase)
    exp.roots = _roots(graph, st.done, phase, {phase})
    exp.tree = _tree(cfg, st, graph, phase, set(exp.roots), {phase}, dated=dated)
    exp.issues = _issues_for(
        graph, _tree_phases(exp.tree), {p for p, status in st.done.items() if status in ledger_mod.SATISFIES_DEPS}
    )
    if len(exp.roots) == 1:
        exp.root_cause = exp.roots[0]
        exp.root_detail = _classify(cfg, exp.root_cause, st, graph, dated).detail
    elif not exp.roots:
        # Every unmet dep was already on the stack: the phase sits in a cycle and
        # can never become ready. `validate` names the cycle; say so plainly.
        exp.detail = (
            f"{exp.detail}; every unmet dependency is part of a dependency cycle,"
            " so it can never become ready — fix the ledger"
        )
    return exp


# -- classification (non-recursive: one phase, one answer) ----------------
def _classify(cfg: Config, phase: str, st: State, graph: dict[str, set[str]],
              dated: dict[str, str] | None = None) -> Explanation:
    """The single best one-line answer for ``phase``, without walking deps.
    ``dated`` are the rows whose ``after:`` date is still ahead."""
    if phase not in graph:
        known = len(graph)
        where = cfg.project_dir / cfg.ledger
        hint = (
            f" — {where} parsed {known} phases; check the spelling or add a"
            " checklist line for it"
            if known
            else f" — {where} parsed no phases at all (missing, or all prose)"
        )
        return Explanation(phase, UNKNOWN, f"not in the ledger{hint}")

    if phase in set(cfg.exclude):
        note = _exclude_comment(cfg, phase)
        tail = f' — the config note says: "{note}"' if note else ""
        already = f" (already recorded `{st.done[phase]}`)" if phase in st.done else ""
        return Explanation(
            phase, EXCLUDED, f"yours to do: the config keeps the swarm off it{already}{tail}"
        )

    slot = next((s for s in st.busy_slots() if s.phase == phase), None)
    if slot is not None:
        return Explanation(
            phase, BUSY, f"running right now in slot {slot.id} — it is not stuck"
        )

    if st.integ_blocked == phase:
        kind = (st.integ_blocked_kind or "held").replace("_", " ")
        where = Path(st.integ_blocked_repo).name if st.integ_blocked_repo else "?"
        return Explanation(
            phase,
            INTEG_BLOCKED,
            f"its worker finished, but merging it stopped ({kind} in {where}) and"
            f" nothing else can land until it is fixed; then `swarm resolved {phase}`",
        )
    if phase in st.integ_queue:
        pos = st.integ_queue.index(phase)
        ahead = f", {pos} ahead of it" if pos else ", at the head"
        held = f" (merging is stopped at {st.integ_blocked})" if st.integ_blocked else ""
        return Explanation(
            phase,
            INTEGRATING,
            f"its worker finished; waiting in the merge queue{ahead}{held}",
        )

    if phase in st.parked and not st.asking(phase):
        return Explanation(
            phase,
            PARKED,
            "its worker is working on your answer in tmux window"
            f" {state_mod.wait_window(phase)} — it is not stuck, and not waiting on you",
        )
    if phase in st.parked:
        return Explanation(
            phase,
            PARKED,
            "waiting on YOU — its worker asked you a question and waits in tmux"
            f" window {state_mod.wait_window(phase)}; answer it there",
        )
    if phase in st.waiting:
        return Explanation(
            phase,
            WAITING,
            "waiting on YOU — its worker asked you a question in its pane and"
            " keeps its worker slot until you answer (in"
            f" {max(0, int(st.waiting[phase] - freezer.state_now(st.frozen)))}s it moves to its"
            " own window)",
        )

    if phase in st.done:
        status = st.done[phase]
        if status == statuses.LEDGER:
            detail = "ticked in the ledger — nothing left to run"
        elif status in ledger_mod.SATISFIES_DEPS:
            tail = "" if status == "ok" else f" ({status})"
            detail = f"already done{tail} — nothing left to run"
        else:
            detail = (
                f"recorded `{status}`: it was tried and its work was set aside,"
                f" so it is not re-offered — `swarm retry {phase}` to try again"
            )
        return Explanation(phase, DONE, detail, status=status)

    unmet = _unmet(graph, st.done, phase)
    if unmet:
        total = len(graph[phase])
        return Explanation(
            phase, BLOCKED, f"blocked: {len(unmet)} of the {total} rows it needs are not built yet"
        )
    if phase in (dated or {}):
        return Explanation(
            phase, DEFERRED,
            f"waits until {dated[phase]}: its `after:` date is still ahead, and the swarm"
            " starts it on that day, not before",
        )

    if st.frozen:
        return Explanation(
            phase, READY, "ready — but the swarm is FROZEN: nothing runs (`swarm thaw`)"
        )
    if st.paused:
        return Explanation(
            phase, READY, "ready — but the swarm is PAUSED (`swarm resume`)"
        )
    if st.usage_hold:
        held = " ".join(caps.describe_hold(st.usage_hold, time.time()))
        return Explanation(phase, READY, f"ready — but {held[0].lower()}{held[1:]}")
    if st.drain:
        return Explanation(
            phase, READY,
            "ready — but the swarm is draining to a stop (`swarm down --cancel`)",
        )
    if cfg.lanes_enabled:
        waits = _lane_wait(cfg, phase, st)
        if waits:
            return Explanation(phase, READY, f"ready — but {waits}")
    if not st.free_slots():
        return Explanation(
            phase,
            READY,
            f"ready — but all {len(st.slots)} slots are busy; it starts as soon as"
            " a worker is free",
        )
    return Explanation(
        phase, READY, f"ready NOW — nothing is blocking it (`swarm launch {phase}`)"
    )


# -- dependency walk ------------------------------------------------------
def _lane_wait(cfg: Config, phase: str, st: State) -> str | None:
    """What a ready row waits for under lanes, or ``None`` when its
    lane is free: the phase holding an overlapping touch and what that phase is
    doing, the row that reserved it, or a repo at ``[lanes] per_repo``."""
    ctx = master_mod.build_context(cfg, st)
    bad = next((i for i in ctx["ledger_issues"] if i.startswith(f"{phase}: bad touches")), None)
    if bad:
        return f"it never launches: {bad.split(': ', 1)[1]}"
    w = ctx["lanes"]["waits"].get(phase)
    if w is None:
        return None
    holder, touch = w["holder"], w["touch"]
    if w["why"] == "per_repo":
        repo = touch.split("/", 1)[0]
        return (f"waits for its repo: `{repo}` already has {cfg.lanes_per_repo} phases in"
                f" flight (`[lanes] per_repo`), among them `{holder}`")
    doing = "reserved" if w["why"] == "reserved" else _holder_state(st, holder)
    return f"waits for its lane: `{touch}` is held by `{holder}` ({doing})"


def _holder_state(st: State, holder: str) -> str:
    if holder in st.waiting:
        return "waiting on the owner"
    if holder in st.parked:
        return "parked" if st.asking(holder) else "working in its own window"
    if holder in st.integrating():
        return "merging"
    return "running"


def _unmet(graph: dict[str, set[str]], done: dict[str, str], phase: str) -> list[str]:
    """Declared deps of ``phase`` whose work has not landed on main.

    ``fail`` is deliberately *not* satisfying (see :data:`ledger.SATISFIES_DEPS`):
    a failed phase's branch was discarded, so building on it would build against
    a main that provably lacks it.
    """
    return sorted(
        d
        for d in graph.get(phase, set())
        if done.get(d) not in ledger_mod.SATISFIES_DEPS
    )


def _roots(
    graph: dict[str, set[str]], done: dict[str, str], phase: str, seen: set[str]
) -> list[str]:
    """The deepest unmet ancestors of ``phase`` that have nothing unmet themselves.

    Those are the only phases anyone can act on. A dep that is not in the graph at
    all (a typo'd ``needs:``) has no deps and so surfaces as its own root, which is
    exactly the diagnosis. ``seen`` breaks cycles: a phase already on the stack
    contributes no root, so a fully cyclic subtree returns ``[]`` and the caller
    reports the cycle instead of a bogus root. ``seen`` is shared across the
    whole walk, not per path: a phase reached twice (a diamond, or the long
    ordering-only chains a ledger builds) already gave its roots the first time,
    and re-walking it per path is exponential in the chain length.
    """
    unmet = _unmet(graph, done, phase)
    if not unmet:
        return [phase]  # nothing left below it: this is where the waiting ends
    out: list[str] = []
    for dep in unmet:
        if dep in seen:
            continue  # a cycle edge, or already walked: no new root
        seen.add(dep)
        out.extend(_roots(graph, done, dep, seen))
    return list(dict.fromkeys(out))


def _tree(
    cfg: Config,
    st: State,
    graph: dict[str, set[str]],
    phase: str,
    roots: set[str],
    seen: set[str],
    expanded: set[str] | None = None,
    dated: dict[str, str] | None = None,
) -> Node:
    """The blocking sub-tree under ``phase``.

    Only *unmet* deps are expanded — a landed dep is shown as one satisfied leaf
    rather than dragging its whole history in, so the tree is exactly the set of
    phases still owed. Each phase is expanded once: a later path to it is one
    ``shown`` leaf, or a ledger's long shared chains blow the tree up
    exponentially. ``seen`` stays the path, so a real cycle is still named.
    """
    if expanded is None:
        expanded = set()
    expanded.add(phase)
    exp = _classify(cfg, phase, st, graph, dated)
    # A `done` node's *status* is the whole diagnosis (`fail` vs `ok`), so it goes
    # in the label rather than hiding one level down in the detail text.
    label = f"{DONE}:{exp.status}" if exp.reason == DONE else exp.reason
    node = Node(phase, label, exp.detail, root=phase in roots)
    for dep in sorted(graph.get(phase, set())):
        if st.done.get(dep) in ledger_mod.SATISFIES_DEPS:
            node.children.append(
                Node(dep, _SATISFIED, f"landed (`{st.done[dep]}`)")
            )
        elif dep in seen:
            node.children.append(Node(dep, _CYCLE, "already above in this tree"))
        elif dep in expanded:
            node.children.append(Node(dep, _SHOWN, "expanded elsewhere in this tree"))
        else:
            node.children.append(
                _tree(cfg, st, graph, dep, roots, seen | {dep}, expanded, dated)
            )
    return node


# -- the `[tasks].exclude` comment ---------------------------------------
def _exclude_comment(cfg: Config, phase: str) -> str | None:
    """The config comment explaining why ``phase`` is excluded, if there is one.

    ``exclude = ["P7"]`` on its own tells you nothing; the reason is almost always
    written next to it as a comment, and that comment is the actual answer to
    "why isn't P7 running". Best-effort textual scrape of the project's
    ``.swarm.toml`` (``Config`` does not keep the file it loaded): the trailing
    comment on the phase's own line wins, else the comment block directly above
    the ``exclude =`` assignment.
    """
    path = cfg.project_dir / ".swarm.toml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    start = next(
        (i for i, ln in enumerate(lines) if ln.split("#")[0].strip().startswith("exclude")
         and "=" in ln.split("#")[0]),
        None,
    )
    if start is None:
        return None

    depth, end = 0, start
    for i in range(start, len(lines)):
        depth += lines[i].count("[") - lines[i].count("]")
        end = i
        if depth <= 0:  # the list closed (same line for a one-line exclude)
            break
    for i in range(start, end + 1):
        pos = lines[i].find(f'"{phase}"')
        if pos < 0:
            pos = lines[i].find(f"'{phase}'")
        if pos < 0:
            continue
        hash_at = lines[i].find("#", pos)
        if hash_at >= 0:
            return lines[i][hash_at + 1 :].strip() or None

    above: list[str] = []
    for i in range(start - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped.startswith("#"):
            break
        above.insert(0, stripped.lstrip("#").strip())
    above = [ln for ln in above if ln]
    # A real project's block covers every excluded phase at once (a typical one
    # names three, each wrapped over several lines), so returning all of it as the
    # answer for one phase buries that phase's own entry. Capture from the line
    # that *begins* this phase's entry until the line that begins the next one:
    # anchoring on the start of the line is what stops a prose cross-reference to
    # another excluded phase ("exactly like jade-W14") from truncating the entry
    # it appears in.
    others = [p for p in cfg.exclude if p != phase]
    heads = [i for i, ln in enumerate(above) if _entry_for(ln, phase)]
    if heads:
        mine = [above[heads[0]]]
        for line in above[heads[0] + 1 :]:
            if any(_entry_for(line, other) for other in others):
                break
            mine.append(line)
    else:
        mine = [ln for ln in above if phase in ln]
    joined = " ".join(mine or above)
    return _clip(joined) or None


def _entry_for(line: str, phase: str) -> bool:
    """Whether this comment line *begins* ``phase``'s entry (rather than merely
    mentioning it), after any list bullet."""
    return line.lstrip("#-*• \t").startswith(phase)


def _clip(text: str, width: int = 240) -> str:
    """A config comment long enough to need scrolling is not a one-line answer."""
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


# -- rendering ------------------------------------------------------------
def render(exp: Explanation, show_tree: bool = False) -> str:
    """The human answer. ``show_tree`` adds the indented dependency tree."""
    out = [f"{exp.phase}: {exp.detail}"]
    if exp.reason != BLOCKED:
        return "\n".join(out)

    if exp.unmet:
        out.append(f"  unmet: {' '.join(exp.unmet)}")
    if show_tree and exp.tree is not None:
        out.append("")
        out.extend(_tree_lines(exp.tree, "", True, top=True))
        out.append("")
    if exp.root_cause:
        out.append(
            f"root cause: {exp.root_cause} — {exp.root_detail}"
        )
        out.append("everything below waits on one phase.")
    elif exp.roots:
        out.append(f"root causes: {', '.join(exp.roots)}")
    for issue in exp.issues:
        out.append(f"ledger issue: {issue}")
    return "\n".join(out)


def _tree_lines(node: Node, prefix: str, last: bool, top: bool = False) -> list[str]:
    mark = "  <- root cause" if node.root else ""
    if top:
        head = f"{node.phase}  [{node.state}]{mark}"
        child_prefix = ""
    else:
        head = f"{prefix}{'`- ' if last else '|- '}{node.phase}  [{node.state}]{mark}"
        child_prefix = prefix + ("   " if last else "|  ")
    lines = [head]
    for i, child in enumerate(node.children):
        lines.extend(
            _tree_lines(child, child_prefix, i == len(node.children) - 1)
        )
    return lines


def _tree_phases(node: Node) -> set[str]:
    return {node.phase}.union(*(_tree_phases(c) for c in node.children)) if node.children else {node.phase}


def _issues_for(
    graph: dict[str, set[str]], phases: set[str], landed: set[str] | frozenset[str] = frozenset()
) -> list[str]:
    """Structural ledger problems (cycles / unknown / self deps) that touch this
    tree. ``validate`` reports the whole ledger; a phase-token intersection keeps
    a large project's unrelated problems out of one ``swarm why``."""
    out = []
    for issue in ledger_mod.validate(graph, landed):
        if set(issue.replace("->", " ").split()) & phases:
            out.append(issue)
    return out
