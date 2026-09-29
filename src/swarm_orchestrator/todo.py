"""The owner's to-do list: everything that waits on the owner and is not a question.

A question has a door already: the session that asks runs ``swarm waiting`` and
waits in its own window (:mod:`owner`). What has none is the other kind of wait —
the owner has to *do* something: try a feature out by hand, check a result on
their device, run a step and let a night pass. Those are scattered over four
records, and this module is the one reader of all of them:

* **ledger rows** that are the owner's (``[tasks].exclude`` or marked
  ``owner-run``) and ready now — every row they need has landed. An excluded row
  is only a to-do when its own line says the owner does something; the rest of
  that list is standing targets, policies and parked decisions, reported as
  ``left_out`` so the difference is visible, never silently dropped;
* **operator jobs** still queued whose brief says the owner has to check or run
  something on his devices. A job that is running or waiting on an answer is the
  operator's (or a question) and is left alone. A job whose row the owner is
  about to walk anyway (``coral-W33`` needs ``coral-W26``) is folded into that row,
  so one sitting closes both;
* **to-dos a finish sent the owner** because no operator would run them
  (``operator-todo`` pings), the retired ``needs-owner`` finishes, and operator
  jobs the queue gave up on. Each stays on the list until a later operator job
  for its phase or a later ``swarm note`` on it says it was handled;
* **the Overseer's "Left for the owner"**, the only record that is prose. Row ids
  it names are matched to the items above (and promote an excluded row the text
  rules would have left out); a line that names nothing already listed becomes
  an item of its own, "mentioned by the Overseer", pointing at the file.

Worker questions, parked sessions and operator jobs waiting on an answer are
never here: they stay in the needs-you flow.

Pure reading: nothing here writes, pings or raises into a caller.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import ledger as ledger_mod
from . import opqueue, statuses
from . import notes as notes_mod
from . import recap as recap_mod
from . import state as state_mod
from .config import Config

OWNER_ROW = "owner-row"
DEVICE_CHECK = "device-check"
WORKER_TODO = "worker-todo"
GIVEN_UP = "given-up"
OVERSEER = "overseer"

#: A row line that marks the row as the owner's to run.
_OWNER_RUN = re.compile(r"\bowner-run\b", re.I)
#: A row line that says the owner does something (``the owner judges …``).
_OWNER_DOES = re.compile(
    r"\b(?:the )?owner(?:'s [\w-]+)? (?:must|will|picks|judges|decides|checks|tries|saves|"
    r"runs|watches|listens|confirms|verifies|tests)\b", re.I)
#: A row line that says the row is not an action now, whatever else it says.
_NOT_ACTION = re.compile(r"\bowner-(?:optional|scoped)\b|\bdesign-gated\b|\bsuperseded\b", re.I)
#: An operator brief (or its triage) that needs the owner's hands, not his answer:
#: "have the owner run", "Owner must verify", "on the owner's devices".
_JOB_NEEDS_OWNER = re.compile(
    r"\b(?:have|has|needs?|must|asks?|wait(?:s|ing)? (?:on|for)) the owner\b"
    r"|\bowner(?:'s)? (?:must|to|device|devices|phone|laptop|iphone|checks?|verif\w*|runs?)\b",
    re.I)
#: Text that says the item needs time to pass once done: a night, a morning, a week.
_CLOCK = re.compile(
    r"\b(?:a|one|the|each|every) night\b|\bovernight\b|\btomorrow\b|\bnext morning\b"
    r"|\bafter (?:\d{4}-\d{2}-\d{2} )?\d{1,2}:\d{2}\b|\b(?:a|one) week later\b", re.I)
_NOTHING = re.compile(r"^\s*nothing\b", re.I)
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9._-]*[A-Za-z0-9]")
_OVERSEER_RECORD = re.compile(r"^\d{8}T\d{6}Z\.md$")
_LEFT_HEAD = re.compile(r"^##\s+left for the owner\s*$", re.I)
_LEFT = re.compile(r"\bLEFT:\s*(.+)", re.S)

#: Kinds in the order a tie is broken: a row first (it is the ledger's), prose last.
_RANK = {OWNER_ROW: 0, DEVICE_CHECK: 1, WORKER_TODO: 2, GIVEN_UP: 3, OVERSEER: 4}


@dataclass
class Todo:
    """One thing the owner has to do. Everything the guide needs to walk it."""

    id: str
    title: str
    kind: str
    #: Rows (and operator jobs) this unblocks or closes, by id.
    releases: list[str] = field(default_factory=list)
    #: Open rows that wait on it, transitively (0 = it holds nothing up).
    rows_behind: int = 0
    #: Once done, time has to pass (a night, a morning) before it counts.
    needs_time: bool = False
    #: Where it came from, in words: "ledger", "operator job coral-W26", ….
    sources: list[str] = field(default_factory=list)
    #: The full text to read: the row, the brief, the ping, the Overseer's line.
    spec: str = ""
    #: Files to open for more (``path`` or ``path:line``).
    paths: list[str] = field(default_factory=list)
    #: The existing ``swarm`` commands that close it, as templates.
    close: list[str] = field(default_factory=list)
    #: Operator jobs folded in (their ids), closed with it.
    jobs: list[str] = field(default_factory=list)
    since: float = 0.0
    _order: int = 0

    def to_dict(self) -> dict:
        out = asdict(self)
        out.pop("_order", None)
        return out


@dataclass
class TodoList:
    items: list[Todo] = field(default_factory=list)
    #: The owner's rows that are not ready yet: ``{id, title, waits_for}``.
    upcoming: list[dict] = field(default_factory=list)
    #: Owner rows left out, and why: ``{id, why}``.
    left_out: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"count": len(self.items), "items": [i.to_dict() for i in self.items],
                "upcoming": self.upcoming, "left_out": self.left_out,
                "generated_at": time.time()}


def _clip(text: str, n: int = 160) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _row_close(row: str) -> list[str]:
    return [f'swarm record {row} done "<what the owner did and saw>"',
            f'swarm record {row} note "<a partial result, or why it waits>"']


def _job_close(job: str) -> str:
    return f'swarm operator-done {job} "<what the owner checked, and the result>"'


def _handled_close(phase: str) -> list[str]:
    return [f'swarm operator-add "<the to-do, with what the owner said>" --phase {phase}',
            f'swarm note {phase} decision "owner did it: <what he did>"']


# -- readers -------------------------------------------------------------------
def _read_rows(path: Path) -> dict:
    from .web import rows as rows_mod  # pure, and nothing else of the web package

    try:
        return rows_mod.parse(path.read_text(encoding="utf-8", errors="replace"))[0]
    except OSError:
        return {}


def _pings(cfg: Config, kind: str) -> list[dict]:
    try:
        text = (cfg.state_dir / "notifications.jsonl").read_text(encoding="utf-8",
                                                                  errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        if f'"{kind}"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("kind") == kind and rec.get("phase"):
            out.append(rec)
    return out


def latest_overseer(cfg: Config) -> Path | None:
    """The newest Overseer pass record (``<state>/overseer/<pass>.md``)."""
    d = cfg.state_dir / "overseer"
    try:
        names = sorted(p.name for p in d.iterdir() if _OVERSEER_RECORD.match(p.name))
    except OSError:
        return None
    return d / names[-1] if names else None


def left_for_owner(text: str) -> list[str]:
    """The bullets of a pass record's ``## Left for the owner`` section."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = bool(_LEFT_HEAD.match(line.strip()))
            continue
        if not inside or not line.strip():
            continue
        if line.lstrip().startswith(("- ", "* ")) or not out:
            out.append(line.strip().lstrip("-* ").strip())
        else:
            out[-1] += " " + line.strip()
    return [b for b in out if b]


def _handled_after(cfg: Config, phase: str, ts: float, jobs: list[opqueue.Item]) -> bool:
    """A later operator job for ``phase`` (or naming it), or a later note on it."""
    word = re.compile(rf"(?<![\w-]){re.escape(phase)}(?![\w-])")
    for item in jobs:
        if item.queued_at > ts and (opqueue.owning_phase(item.phase) == phase
                                    or word.search(item.note or "")):
            return True
    return any(n.ts > ts for n in notes_mod.load(cfg, phase))


# -- the collector -------------------------------------------------------------
def collect(cfg: Config, st: state_mod.State | None = None) -> TodoList:
    """Every owner to-do, deduplicated and ordered by what it unblocks."""
    st = st if st is not None else state_mod.read(cfg)
    path = cfg.project_dir / cfg.ledger
    graph = ledger_mod.load(path)
    rows = _read_rows(path) if graph else {}
    ticked = ledger_mod.load_ticked(path)
    flying = {s.phase for s in st.busy_slots() if s.phase} | set(st.parked) | set(st.waiting)
    done = ledger_mod.with_ticked(st.done, ticked, flying)
    satisfied = {p for p, s in done.items() if s in statuses.SATISFIES_DEPS}
    excluded = set(cfg.exclude or [])
    order = {p: i for i, p in enumerate(graph)}
    out = TodoList()
    items: dict[str, Todo] = {}
    standing: dict[str, str] = {}

    def line_of(row: str) -> str:
        r = rows.get(row)
        return (r.text.splitlines()[0] if r and r.text else "") or row

    # 1. The owner's rows.
    for row in graph:
        first = line_of(row)
        if row not in excluded and not _OWNER_RUN.search(first):
            continue
        if row in satisfied or row in ticked or row in flying:
            continue
        r = rows.get(row)
        title = r.title if r else row
        action = not _NOT_ACTION.search(first) and (
            bool(_OWNER_RUN.search(first)) or bool(_OWNER_DOES.search(title)))
        unmet = sorted(graph[row] - satisfied, key=lambda p: order.get(p, 0))
        if unmet:
            if action:
                out.upcoming.append({"id": row, "title": title, "waits_for": unmet})
            continue
        if not action:
            standing[row] = ("optional or parked in its row" if _NOT_ACTION.search(first)
                             else "its row names no owner action (a standing target, a"
                             " policy or a decision for later)")
            continue
        items[row] = _row_item(cfg, row, graph, done, excluded, rows, order)

    # 2. Operator jobs: queued ones that need the owner's hands, and ones given up.
    jobs = opqueue.load_all(cfg)
    folds = {dep: row for row, it in items.items() for dep in graph.get(row, ())}
    for item in jobs:
        owner_hands = item.state == opqueue.QUEUED and _JOB_NEEDS_OWNER.search(
            f"{item.note} {item.triage.get('why', '')}")
        given_up = item.state == opqueue.ABANDONED and not item.asked
        if given_up and _handled_after(cfg, opqueue.owning_phase(item.phase),
                                       item.queued_at, [j for j in jobs if j is not item]):
            continue
        if not (owner_hands or given_up):
            continue
        brief = opqueue.item_path(cfg, item.phase)
        host = items.get(folds.get(opqueue.owning_phase(item.phase), ""))
        if owner_hands and host is not None:
            host.jobs.append(item.phase)
            host.releases.append(item.phase)
            host.sources.append(f"operator job {item.phase}")
            host.spec += f"\n\nOperator job {item.phase} (closed with this row):\n{item.note}"
            host.paths.append(str(brief))
            host.close.append(_job_close(item.phase))
            continue
        left = _LEFT.search(item.note or "")
        title = _clip(left.group(1) if left else (item.triage.get("why") or item.note))
        items[item.phase] = Todo(
            id=item.phase, kind=DEVICE_CHECK if owner_hands else GIVEN_UP,
            title=title if owner_hands else f"the operator gave up on it: {title}",
            releases=[item.phase], sources=[f"operator job {item.phase}"],
            spec=item.note + (f"\n\nLast attempt: {item.last_error}" if item.last_error else ""),
            paths=[str(brief)],
            close=([_job_close(item.phase)] if owner_hands
                   else _handled_close(opqueue.owning_phase(item.phase))),
            jobs=[item.phase] if owner_hands else [], since=item.queued_at)

    # 3. To-dos a finish sent the owner, and the retired needs-owner finishes.
    handed: dict[str, tuple[float, str, str]] = {}
    for rec in _pings(cfg, "operator-todo"):
        handed[str(rec["phase"])] = (float(rec.get("ts") or 0.0), str(rec.get("text") or ""),
                                     "a to-do a finished phase sent you")
    for phase, status in st.done.items():
        if status == statuses.NEEDS_OWNER:
            try:
                ts = (cfg.done_dir / f"{phase}.{status}").stat().st_mtime
            except OSError:
                ts = 0.0
            handed.setdefault(phase, (ts, recap_mod.sentinel(cfg, phase)[1],
                                      "a finished phase left it for you"))
    for phase, (ts, text, what) in handed.items():
        if phase in items or _handled_after(cfg, phase, ts, jobs):
            continue
        left = _LEFT.search(text)
        items[phase] = Todo(
            id=phase, kind=WORKER_TODO, title=_clip(left.group(1) if left else text),
            releases=[phase], sources=[what], spec=text,
            paths=[str(cfg.done_dir), str(cfg.state_dir / "notifications.jsonl")],
            close=_handled_close(phase), since=ts)

    # 4. The Overseer's latest "Left for the owner".
    record = latest_overseer(cfg)
    try:
        bullets = left_for_owner(record.read_text(encoding="utf-8")) if record else []
    except OSError:
        bullets = []
    covered = {j: key for key, it in items.items() for j in [key, *it.jobs]}
    for n, bullet in enumerate(bullets, 1):
        ids = list(dict.fromkeys(t.rstrip(".") for t in _TOKEN.findall(bullet)
                                 if t.rstrip(".") in graph))
        loose = []
        for rid in ids:
            if rid in covered:
                items[covered[rid]].sources.append(f"mentioned by the Overseer ({record.name})")
            elif rid in standing:
                items[rid] = _row_item(cfg, rid, graph, done, excluded, rows, order)
                items[rid].sources.append(f"mentioned by the Overseer ({record.name})")
                covered[rid] = rid
                del standing[rid]
            elif rid not in satisfied and rid not in ticked:
                loose.append(rid)
        if loose or (not ids and not _NOTHING.match(bullet)):
            key = f"overseer-{n}"
            items[key] = Todo(
                id=key, kind=OVERSEER, title=_clip(bullet), releases=loose,
                sources=[f"mentioned by the Overseer ({record.name})"], spec=bullet,
                paths=[str(record)], close=[f'swarm record <row> note "<what the owner did>"'],
                since=record.stat().st_mtime if record else 0.0)

    for it in items.values():
        it.sources = list(dict.fromkeys(it.sources))
        it.needs_time = bool(_CLOCK.search(f"{it.title}\n{it.spec}"))
        it._order = order.get(it.id, len(order))
    out.items = sorted(items.values(), key=lambda t: (
        -t.rows_behind, not t.needs_time, -len(t.jobs), _RANK.get(t.kind, 9), t._order, t.since))
    out.left_out = [{"id": r, "why": why} for r, why in standing.items()]
    return out


def _row_item(cfg: Config, row: str, graph, done, excluded, rows, order) -> Todo:
    r = rows.get(row)
    title = r.title if r else row
    from .web import rows as rows_mod

    section = rows_mod.plain(r.heads[-1].text) if r and r.heads else ""
    if section and len(_OWNER_RUN.sub("", title).strip(" .:")) < 20:
        title = f"{title} ({_clip(section, 100)})"
    dependents = sorted((p for p, deps in graph.items() if row in deps and p not in done),
                        key=lambda p: order.get(p, 0))
    spec = (f"Section: {section}\n" if section else "") + (r.text if r else row)
    status, note = recap_mod.sentinel(cfg, row)
    paths = [f"{cfg.project_dir / cfg.ledger}:{r.line}" if r else str(cfg.project_dir / cfg.ledger)]
    if status:
        spec += f"\n\nThe swarm's last attempt ({status}): {note}"
        paths.append(str(cfg.done_dir / f"{row}.{status}"))
    return Todo(id=row, kind=OWNER_ROW, title=title, releases=dependents,
                rows_behind=ledger_mod.blocked_behind(graph, row, done, excluded),
                sources=["ledger"], spec=spec, paths=paths, close=_row_close(row))


# -- text ------------------------------------------------------------------------
def count(cfg: Config) -> int:
    """How many to-dos there are; 0 when the records cannot be read."""
    try:
        return len(collect(cfg).items)
    except Exception:  # noqa: BLE001 - a count on a status line must never fail it
        return 0


def status_line(n: int) -> str:
    return f"owner to-dos: {n} — swarm guide (or g in the TUI)"


def render(todos: TodoList) -> str:
    """``swarm todo``'s text: one block per item, then what is coming and left out."""
    lines = [status_line(len(todos.items))
             + ("" if todos.items else " — nothing waits on you but questions")]
    for n, it in enumerate(todos.items, 1):
        tags = [it.kind]
        if it.needs_time:
            tags.append("needs time to pass")
        lines.append(f"{n:2}. {it.id}  [{' · '.join(tags)}]  {_clip(it.title, 120)}")
        rel = ", ".join(it.releases) or "nothing waits on it"
        behind = f" ({it.rows_behind} rows wait on it)" if it.rows_behind else ""
        lines.append(f"      releases: {rel}{behind}")
        lines.append(f"      from: {'; '.join(it.sources)}")
    if todos.upcoming:
        lines.append("coming later: " + ", ".join(
            f"{u['id']} (waits for {', '.join(u['waits_for'][:3])})" for u in todos.upcoming))
    if todos.left_out:
        lines.append("left out (standing targets, policies, parked or optional): "
                     + ", ".join(x["id"] for x in todos.left_out))
    lines.append("questions stay under needs you (`swarm status`, `n` in the TUI)")
    return "\n".join(lines)
