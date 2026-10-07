"""Batch claims: which ready rows ride with a seed row in one worker session.

A worker starts cold (spec, ledger, ADRs), so related rows run by one session
share that cost. The supervisor picks a *seed* exactly as before (the first
launchable row in ledger order); this module picks the rows that join it:

* :func:`candidates` is the one source of truth for eligibility: open, not busy, not excluded, same model as the seed, not
  solo-class, lane-compatible with what is in flight, and every ``needs`` done or
  itself a candidate (so a chain successor may ride after its need).
* :func:`greedy` is the deterministic rule: affinity-ordered growth
  under ``[batch]`` caps, then topological order inside the batch.
* :func:`choose` asks ``[batch].model`` to pick among the candidates,
  validates the answer with :func:`check`, and falls back to
  :func:`greedy` on any failure.
* :func:`still_free` is the re-check the caller runs under its state lock just
  before claiming; :func:`plan` is the dry run ``swarm batch`` prints.

Size proxy (``pts``): there is no brief locator in the codebase, so a row's
size is ``max(3, row text KB) + 0.5 * len(touches)``, where the row text is its
ledger line plus indented continuation lines and 3.0 is the floor
for a row with no brief. Coarse on purpose; ``[batch].max_points`` is the knob.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from . import lanes as lanes_mod
from . import lean
from . import ledger as ledger_mod
from . import ledgerw
from . import models as models_mod
from . import state as state_mod
from . import statuses
from .config import Config

#: Hard ceiling on rows per batch, whatever ``[batch].max_rows`` says.
HARD_MAX_ROWS = 5
#: Candidates sent to the model (ledger order, truncated).
MAX_CANDIDATES = 40
#: ``pts`` at or above this: the row runs alone.
OVERSIZE_PTS = 26.0
#: ``pts`` at or above this: placed after the small rows of its depth.
LARGE_PTS = 15.0
#: Floor of the size proxy: the size of a row with no brief.
BASE_PTS = 3.0
TITLE_CHARS = 90
TOUCH_PATHS = 4
REASON_CHARS = 200

MODEL = "model"
FALLBACK = "fallback"
SINGLE = "single"

#: A batch's place in the merge queue (``State.integ_status``), beside the
#: operator job's and the Overseer pass's: its seed's branch, landing every row.
INTEG_STATUS = "batch"
#: What ``State.batch_done`` says of a row handed back unbuilt (``swarm
#: unbatch``): nothing is recorded for it, and it is ready again.
RELEASED = "released"


@dataclass
class Batch:
    rows: list[str]  # execution order; rows[0] == seed
    reason: str
    source: str  # "model" | "fallback" | "single" (nothing to batch / batching off)


@dataclass
class _Row:
    id: str
    touches: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    title: str = ""
    size: int = 0  # bytes of the row's ledger text


@dataclass
class _View:
    """One read of the ledger and the state, everything the rule asks."""

    order: dict[str, int]
    graph: dict[str, set[str]]
    landed: set[str]
    pool: set[str]  # open rows that may join a batch (seed aside)
    rows: dict[str, _Row]
    model: dict[str, str]  # "" = the swarm's own
    lanes_on: bool = False
    lanes: dict[str, frozenset[lanes_mod.Touch]] = field(default_factory=dict)
    held: dict[str, frozenset[lanes_mod.Touch]] = field(default_factory=dict)
    commons: list[str] = field(default_factory=list)
    per_repo: int = 1
    seed: str = ""
    cfg: Config | None = None
    _solo: dict[str, str] = field(default_factory=dict)

    def solo(self, row: str) -> str:
        """Why ``row`` must run alone, or "" (cached: it reads history files)."""
        if row not in self._solo:
            self._solo[row] = _solo_why(self.cfg, self, row)
        return self._solo[row]


# -- reading ------------------------------------------------------------------
def _batch_rows(st) -> set[str]:
    """Every row of every open batch (read defensively: older State has none)."""
    fn = getattr(st, "batch_rows", None)
    if callable(fn):
        return set(fn())
    return {r for rows in (getattr(st, "batches", None) or {}).values() for r in rows}


def _busy(st) -> set[str]:
    """Rows in flight: in a slot, waiting, parked, merging, launching, or in a batch."""
    out = {s.phase for s in st.busy_slots() if s.phase}
    for key in [*st.waiting, *st.parked]:
        kind, ident = state_mod.waiter(key)
        if kind == state_mod.WORKER:
            out.add(ident)
    return out | st.integrating() | set(getattr(st, "launching", ()) or ()) | _batch_rows(st)


def _ledger_text(cfg: Config) -> str:
    try:
        return (cfg.project_dir / cfg.ledger).read_text(encoding="utf-8")
    except OSError:
        return ""


def _rows(text: str) -> dict[str, _Row]:
    from .web import rows as rows_mod  # pure, and nothing else of the web package

    try:
        parsed, _ = rows_mod.parse(text)
    except ValueError:
        parsed = {}
    dirs = ledger_mod.dirs(text)
    return {rid: _Row(rid, list(r.touches), dirs.get(rid, []), r.title, len(r.text.encode()))
            for rid, r in parsed.items()}


def _released_path(cfg: Config, row: str):
    return cfg.state_dir / "batch" / f"{row}.released"


def release(cfg: Config, row: str, why: str) -> None:
    """Remember that a batch handed ``row`` back (``swarm unbatch``): it runs
    alone from now on. Best-effort; the reason is kept for whoever looks."""
    path = _released_path(cfg, row)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(why + "\n", encoding="utf-8")
    except OSError:
        pass


def _failed_before(cfg: Config, row: str) -> bool:
    """A ``fail`` (``blocked``/``later`` are recorded as one) in the row's
    history, or a batch that handed it back."""
    if _released_path(cfg, row).exists():
        return True
    try:
        lines = (cfg.done_dir / f"{row}.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("status") == statuses.FAIL:
            return True
    return False


def _view(cfg: Config, st, seed: str, taken=frozenset(), *, lanes: bool = True,
          groups: dict[str, list[str]] | None = None) -> _View:
    text = _ledger_text(cfg)
    graph = ledger_mod.parse(text)
    busy = _busy(st) - {seed}
    taken = set(taken) - {seed}
    done = ledger_mod.with_ticked(st.done, ledger_mod.ticked(text), busy)
    landed = {p for p, s in done.items() if s in statuses.SATISFIES_DEPS}
    excluded = set(cfg.exclude) | set(ledgerw.dated(cfg))
    pool = {p for p in graph if p not in done and p not in busy and p not in excluded
            and p not in taken and p != seed}
    over = models_mod.overrides(cfg, text)
    model = {p: over.get(p, "") for p in graph}
    v = _View({p: i for i, p in enumerate(graph)}, graph, landed, pool, _rows(text), model,
              seed=seed, cfg=cfg)
    if lanes and cfg.lanes_enabled:
        from . import launch as launch_mod  # launch may import this module

        lv = launch_mod.lane_view(cfg, st)
        v.lanes_on = True
        v.lanes = lv.rows
        v.commons = list(cfg.lanes_commons)
        v.per_repo = max(1, cfg.lanes_per_repo)
        held = {p: lane for p, lane in lv.held.items() if p != seed}
        # Rows the caller is launching hold their lanes too. An open batch (and
        # each of the dry run's ``groups``) holds one union lane, under its seed,
        # so it counts once per repo against ``[lanes] per_repo``.
        loose = taken - set(held)
        batches = {**(getattr(st, "batches", None) or {}), **(groups or {})}
        for key, rows in batches.items():
            parts = [held.pop(r, frozenset()) for r in {key, *rows}]
            parts += [v.lanes.get(r, frozenset()) for r in rows]
            held[key] = frozenset().union(*parts)
            loose -= {key, *rows}
        for p in loose:
            lane = v.lanes.get(p)
            if lane is not None:
                held[p] = lane
        v.held = held
    return v


# -- the row's features -------------------------------------------------
def family(row: str) -> str:
    return row.split("-", 1)[0]


def _touches(v: _View, row: str) -> list[str]:
    r = v.rows.get(row)
    return list(r.touches) if r else []


def code_repos(v: _View, row: str) -> set[str]:
    """Repos the row's touches name, without ``.`` and ``@resource``; with no
    touches, its ``dir:`` without ``.``."""
    r = v.rows.get(row)
    if r is None:
        return set()
    if r.touches:
        out = {t.split("/", 1)[0] for t in r.touches}
    else:
        out = {d.rstrip("/") for d in r.dirs}
    return {x for x in out if x and x != "." and not x.startswith("@")}


def _subs(v: _View, row: str) -> set[str]:
    """Where its umbrella (``./``) touches sit: ``docs/<x>``, ``tasks/<x>`` or the
    first path component."""
    out = set()
    for t in _touches(v, row):
        if not t.startswith("./"):
            continue
        parts = t[2:].split("/")
        if parts[0] in ("docs", "tasks") and len(parts) > 1 and "*" not in parts[1]:
            out.add(f"{parts[0]}/{parts[1]}")
        elif parts[0] and "*" not in parts[0]:
            out.add(parts[0])
    return out


def _dir3(touch: str) -> str:
    parts = touch.split("/")
    return "/".join(parts[:-1][:3]) or parts[0]


def pts(v: _View, row: str) -> float:
    """The size proxy (see the module doc)."""
    r = v.rows.get(row)
    if r is None:
        return BASE_PTS
    return max(BASE_PTS, r.size / 1000) + 0.5 * len(r.touches)


def _solo_why(cfg: Config, v: _View, row: str) -> str:
    """Why ``row`` must run alone, or ""."""
    r = v.rows.get(row)
    touches = r.touches if r else []
    if "./**" in touches or (r and not touches and "." in [d.rstrip("/") for d in r.dirs]):
        return "touches the whole umbrella"
    n = len(code_repos(v, row))
    if n >= 3:
        return f"cross-cutting ({n} repos)"
    if pts(v, row) >= OVERSIZE_PTS:
        return f"oversize ({pts(v, row):.0f} pts)"
    if _failed_before(cfg, row):
        return "failed before"
    if models_mod.handup(cfg, row) is not None:
        return "handed up before"
    return ""


def _limits(cfg: Config) -> tuple[int, float, int]:
    return (max(1, min(HARD_MAX_ROWS, cfg.batch_max_rows)), float(cfg.batch_max_points),
            max(1, cfg.batch_max_repos))


# -- eligibility ------------------------------------------------------
def _lane_ok(v: _View, row: str, *, as_seed: bool = False) -> bool:
    """Not lane-blocked: its lane parses, overlaps nothing in flight, and adds no
    repo that is already at ``[lanes] per_repo`` (the batch counts once per repo,
    so a candidate is never counted again in the seed's own repos)."""
    if not v.lanes_on:
        return True
    lane = v.lanes.get(row)
    if lane is None:
        return False  # its touches do not parse: it never launches
    for holder in sorted(v.held):
        if lanes_mod.collide(lane, v.held[holder], v.commons) is not None:
            return False
    seed_repos = frozenset() if as_seed else lanes_mod.repos_of(
        lanes_mod.owned(v.lanes.get(v.seed, frozenset()), v.commons))
    counts: dict[str, int] = {}
    for theirs in v.held.values():
        for repo in lanes_mod.repos_of(lanes_mod.owned(theirs, v.commons)):
            counts[repo] = counts.get(repo, 0) + 1
    return all(counts.get(repo, 0) < v.per_repo
               for repo in lanes_mod.repos_of(lanes_mod.owned(lane, v.commons)) - seed_repos)


def _eligible(v: _View, seed: str) -> list[str]:
    """Every row that may ride with ``seed``, in ledger order (no truncation)."""
    if seed not in v.graph or v.solo(seed):
        return []
    want = v.model.get(seed, "")
    base = {p for p in v.pool
            if v.model.get(p, "") == want and not v.solo(p) and _lane_ok(v, p)}
    ok: set[str] = set()
    grew = True
    while grew:
        grew = False
        for p in base - ok:
            if all(d in v.landed or d == seed or d in ok for d in v.graph[p]):
                ok.add(p)
                grew = True
    return sorted(ok, key=v.order.__getitem__)


def candidates(cfg: Config, st, seed: str, *, taken=frozenset()) -> list[str]:
    """Rows eligible to ride with ``seed``, ledger order, at most
    :data:`MAX_CANDIDATES`. Empty when ``seed`` itself must run alone.

    ``taken``: rows the caller is launching or backing off; they are left out,
    and their lanes count as held."""
    return _eligible(_view(cfg, st, seed, taken), seed)[:MAX_CANDIDATES]


# -- the deterministic rule -----------------------------------------
def _compatible(cfg: Config, v: _View, batch: list[str], row: str) -> bool:
    max_rows, max_points, max_repos = _limits(cfg)
    if len(batch) >= max_rows:
        return False
    if any(d not in v.landed and d not in batch for d in v.graph[row]):
        return False
    if sum(pts(v, r) for r in batch) + pts(v, row) > max_points:
        return False
    have = set().union(*(code_repos(v, r) for r in batch))
    mine = code_repos(v, row)
    fams = {family(r) for r in batch}
    if have and mine:
        return bool(have & mine or family(row) in fams) and len(have | mine) <= max_repos
    if not have and not mine:
        subs = set().union(*(_subs(v, r) for r in batch))
        return family(row) in fams or bool(subs & _subs(v, row))
    return len(have | mine) <= max_repos


def _affinity(v: _View, batch: list[str], row: str) -> int:
    fams = {family(r) for r in batch}
    paths = {t for r in batch for t in _touches(v, r)}
    dirs = {_dir3(t) for t in paths}
    mine = _touches(v, row)
    return (3 * (family(row) in fams)
            + min(3, len(paths & set(mine)))
            + min(2, len(dirs & {_dir3(t) for t in mine}))
            + 4 * bool(v.graph[row] & set(batch)))


def _order(v: _View, batch: list[str]) -> list[str]:
    """Seed first; then dependencies first (depth), small before large, rows of
    one repo together, ledger order."""
    seed, rest = batch[0], batch[1:]
    inside = set(batch)
    depth: dict[str, int] = {seed: 0}

    def d(r: str) -> int:
        if r not in depth:
            depth[r] = 0
            depth[r] = 1 + max((d(x) for x in v.graph[r] & inside), default=-1)
        return depth[r]

    repo_rank: dict[str, int] = {}
    for r in sorted(batch, key=lambda r: (r != seed, v.order[r])):
        repo_rank.setdefault(min(code_repos(v, r), default=""), len(repo_rank))
    rest.sort(key=lambda r: (d(r), pts(v, r) >= LARGE_PTS,
                             repo_rank[min(code_repos(v, r), default="")], v.order[r]))
    return [seed, *rest]


def _describe(v: _View, rows: list[str]) -> str:
    fams = sorted({family(r) for r in rows})
    repos = sorted(set().union(*(code_repos(v, r) for r in rows))) or ["."]
    total = sum(pts(v, r) for r in rows)
    return f"{len(rows)} rows; family {'+'.join(fams)}; repos {'+'.join(repos)}; {total:.0f} pts"


def _greedy(cfg: Config, v: _View, seed: str, cands: list[str]) -> Batch:
    batch = [seed]
    left = [c for c in cands if c != seed]
    while True:
        fit = [c for c in left if _compatible(cfg, v, batch, c)]
        if not fit:
            break
        best = max(fit, key=lambda c: (_affinity(v, batch, c), -v.order[c]))
        batch.append(best)
        left.remove(best)
    if len(batch) == 1:
        why = v.solo(seed)
        return Batch([seed], f"runs alone: {why}" if why else "no related ready row", SINGLE)
    rows = _order(v, batch)
    return Batch(rows, _describe(v, rows), FALLBACK)


def greedy(cfg: Config, st, seed: str, cands: list[str]) -> Batch:
    """The deterministic batch for ``seed`` among ``cands``."""
    return _greedy(cfg, _view(cfg, st, seed), seed, cands)


# -- validation -----------------------------------------------------------
def _check(cfg: Config, v: _View, seed: str, rows: list[str], eligible: list[str]) -> str | None:
    max_rows, max_points, max_repos = _limits(cfg)
    if not rows:
        return "empty batch"
    if not all(isinstance(r, str) for r in rows):
        return "an id is not a string"
    if len(rows) > max_rows:
        return f"{len(rows)} rows > max_rows {max_rows}"
    if len(set(rows)) != len(rows):
        return "duplicate id"
    if rows[0] != seed:
        return f"seed {seed} not first"
    if seed not in v.graph:
        return f"unknown id {seed}"
    for r in rows[1:]:
        if r not in v.graph:
            return f"unknown id {r}"
    if len({v.model.get(r, "") for r in rows}) > 1:
        return "mixed model"
    if len(rows) > 1:
        for r in rows:
            if v.solo(r):
                return f"{r} must run alone ({v.solo(r)})"
    ok = set(eligible)
    for r in rows[1:]:
        if r not in ok:
            return f"{r} is not eligible"
    for i, r in enumerate(rows):
        for need in sorted(v.graph[r]):
            if need not in v.landed and need not in rows[:i]:
                return f"{r} needs {need} before it"
    total = sum(pts(v, r) for r in rows)
    if total > max_points:
        return f"{total:.0f} pts > max_points {max_points:.0f}"
    repos = set().union(*(code_repos(v, r) for r in rows))
    if len(repos) > max_repos:
        return f"{len(repos)} repos > max_repos {max_repos}"
    if v.lanes_on:
        for r in rows:
            lane = v.lanes.get(r)
            if lane is None:
                return f"{r} has no lane"
            for holder in sorted(v.held):
                if lanes_mod.collide(lane, v.held[holder], v.commons) is not None:
                    return f"{r} lane-busy [{holder}]"
    return None


def check(cfg: Config, st, seed: str, rows: list[str], *, taken=frozenset()) -> str | None:
    """The model's answer checked against the live ledger and ``st``: None when ``rows`` is a valid
    batch for ``seed``, else a short reason."""
    v = _view(cfg, st, seed, taken)
    return _check(cfg, v, seed, list(rows), _eligible(v, seed)[:MAX_CANDIDATES])


# -- the model call ---------------------------------------------------
PROMPT = """\
You group build tasks ("phases") for ONE coding worker. A worker starts cold (reads the spec, ledger and ADRs first), so grouping related
phases saves that cost; unrelated phases waste it. Choose a batch from the rows in INPUT.

Hard rules:
- The batch must start with the seed row.
- 1 to max_rows rows; sum of pts <= max_points; at most max_repos distinct entries across all "repos".
- Only use ids that appear in rows. Do not invent ids.
- If a row's "needs" lists an id, that id must be in the batch BEFORE it, or the row cannot be in the batch.
- Order the batch so dependencies come first, then the smaller rows before the larger ones.
- All rows in a batch share the same "model" (INPUT already ensures this; do not mix).

Preferences, in order: same family (fam); same repo; rows whose touch paths overlap or sit in the same directories; a row that is a
follow-up (F) of a W row already in the batch; a docs-only row whose docs belong to the batch. Aim for 3 to 5 rows when related rows exist.
A single row is correct only if nothing in INPUT is related to the seed. Never pad a batch with unrelated rows to reach 5.

Reply with ONLY one JSON object, no prose, no code fence:
{"batch":["<id>",...],"reason":"<one line, under 140 characters>"}

INPUT:
"""


def _touch_globs(touches: list[str]) -> list[str]:
    out: list[str] = []
    for t in touches:
        parts = t.split("/")
        g = t if len(parts) < 2 or parts[-1] == lanes_mod.DEEP else "/".join(parts[:-1]) + "/*"
        if g not in out:
            out.append(g)
    return out[:TOUCH_PATHS]


def _input(cfg: Config, v: _View, seed: str, cands: list[str]) -> dict:
    max_rows, max_points, max_repos = _limits(cfg)
    listed = [seed, *[c for c in cands if c != seed]]
    inside = set(listed)
    own = models_mod.default(cfg) or "default"
    rows = []
    for r in listed:
        title = v.rows[r].title if r in v.rows else ""
        rows.append({
            "id": r, "fam": family(r), "repos": sorted(code_repos(v, r)),
            "pts": round(pts(v, r), 1), "model": v.model.get(r, "") or own,
            "needs": sorted(d for d in v.graph[r] if d in inside),
            "title": title[:TITLE_CHARS], "touch": _touch_globs(_touches(v, r)),
        })
    return {"max_rows": max_rows, "max_points": max_points, "max_repos": max_repos,
            "seed": seed, "rows": rows}


def prompt(cfg: Config, v: _View, seed: str, cands: list[str]) -> str:
    return PROMPT + json.dumps(_input(cfg, v, seed, cands), separators=(",", ":"))


def _ask_model(cfg: Config, text: str) -> tuple[str | None, str, float | None]:
    """One ``claude -p`` call: ``(answer, why it failed, cost in USD)``. Never
    raises. The test seam: hermetic runs (``SWARM_TG_SINK`` set) never spawn
    claude, and tests monkeypatch this function."""
    if os.environ.get("SWARM_TG_SINK"):
        return None, "hermetic", None
    if shutil.which("claude") is None:
        return None, "no-claude-binary", None
    from .recap import _child_env

    cwd = cfg.project_dir if cfg.project_dir.is_dir() else cfg.state_dir
    try:
        proc = subprocess.run(
            ["claude", "-p", text, "--model", cfg.batch_model, "--output-format", "json",
             "--tools", "", *lean.args(cfg, cwd)],
            cwd=str(cwd), capture_output=True, text=True,
            timeout=max(1, cfg.batch_timeout_s), env=_child_env(cfg),
        )
    except subprocess.TimeoutExpired:
        return None, "model-timeout", None
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"model-spawn-failed: {exc}", None
    if proc.returncode != 0:
        return None, f"model-exit-{proc.returncode}", None
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return None, "model-unparsable", None
    if not isinstance(data, dict):
        return None, "model-unparsable", None
    cost = data.get("total_cost_usd")
    cost = float(cost) if isinstance(cost, (int, float)) else None
    if data.get("is_error"):
        return None, "model-error", cost
    result = data.get("result")
    if not isinstance(result, str) or not result.strip():
        return None, "model-empty", cost
    return result, "", cost


def parse_answer(text: str) -> tuple[list[str], str] | str:
    """``(batch, reason)`` from the model's answer, or why it does not parse."""
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rstrip()
        if body.endswith("```"):
            body = body[:-3]
    try:
        data = json.loads(body)
    except ValueError:
        return "answer is not JSON"
    if not isinstance(data, dict):
        return "answer is not a JSON object"
    batch, reason = data.get("batch"), data.get("reason")
    if not isinstance(batch, list) or not all(isinstance(r, str) for r in batch):
        return "batch is not a list of ids"
    if not isinstance(reason, str):
        return "reason is not a string"
    return batch, " ".join(reason.split())[:REASON_CHARS]


def choose(cfg: Config, st, seed: str, *, taken=frozenset(), log=None) -> Batch:
    """The batch ``seed`` launches with. Batching off or nothing eligible: just
    the seed. Else ``[batch].model`` picks among :func:`candidates` and
    :func:`check` validates the pick; any failure (and ``model = ""``) takes
    :func:`greedy`, logging ``BATCH-FALLBACK <seed> <why>``."""
    if not cfg.batch_enabled:
        return Batch([seed], "batching off", SINGLE)
    v = _view(cfg, st, seed, taken)
    if v.solo(seed):
        return Batch([seed], f"runs alone: {v.solo(seed)}", SINGLE)
    cands = _eligible(v, seed)[:MAX_CANDIDATES]
    if not cands:
        return Batch([seed], "no related ready row", SINGLE)
    if not cfg.batch_model:
        return _greedy(cfg, v, seed, cands)
    answer, why, cost = _ask_model(cfg, prompt(cfg, v, seed, cands))
    if cost is not None and log is not None:
        log.line(f"BATCH-MODEL {seed} model={cfg.batch_model} cost=${cost:.4f}")
    if answer is not None:
        got = parse_answer(answer)
        if isinstance(got, str):
            why = got
        else:
            rows, reason = got
            live = _view(cfg, st, seed, taken)  # the ledger may have moved meanwhile
            why = _check(cfg, live, seed, rows, _eligible(live, seed)[:MAX_CANDIDATES]) or ""
            if not why:
                return Batch(rows, reason or _describe(live, rows), MODEL)
            v = live
    if log is not None and why != "hermetic":
        log.line(f"BATCH-FALLBACK {seed} {why}")
    return _greedy(cfg, v, seed, _eligible(v, seed)[:MAX_CANDIDATES])


# -- the claim-time re-check, and the dry run ----------------------------------
def still_free(cfg: Config, st, rows: list[str], *, taken=frozenset()) -> list[str]:
    """``rows`` minus what can no longer join, re-read under the caller's state
    lock just before claiming. ``rows[0]`` (the seed) always stays; a row now
    busy, taken, done, excluded or gone is dropped, and so is every row whose
    in-batch need was dropped. Reads the ledger only (no lane or git I/O)."""
    if not rows:
        return []
    seed = rows[0]
    v = _view(cfg, st, seed, taken, lanes=False)
    kept = [seed]
    for r in rows[1:]:
        if r not in v.pool:
            continue
        if all(d in v.landed or d in kept for d in v.graph[r]):
            kept.append(r)
    return kept


def plan(cfg: Config, st) -> list[Batch]:
    """What :func:`greedy` would form for the current ready set, seeds in launch
    order: ledger order, each seed launching only when its lane is free of what
    is in flight and of the batches formed before it (each batch holds one
    union lane). A dry run: no model call, no free-slot cap, nothing written."""
    first = _view(cfg, st, "", lanes=False)
    ready = [p for p in first.graph if p in first.pool and first.graph[p] <= first.landed]
    groups: dict[str, list[str]] = {}
    out: list[Batch] = []
    for seed in ready:
        taken = {r for rows in groups.values() for r in rows}
        if seed in taken:
            continue
        v = _view(cfg, st, seed, taken, groups=groups)
        if not _lane_ok(v, seed, as_seed=True):
            continue  # it waits for a lane
        if not cfg.batch_enabled:
            batch = Batch([seed], "batching off", SINGLE)
        else:
            batch = _greedy(cfg, v, seed, _eligible(v, seed)[:MAX_CANDIDATES])
        out.append(batch)
        groups[seed] = batch.rows
    return out
