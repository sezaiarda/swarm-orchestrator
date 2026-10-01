"""Everything the dashboard shows, derived from disk by pure functions.

The rendering layer (:mod:`swarm_orchestrator.tui.app`) is deliberately dumb: it
polls the functions here and paints what comes back. That split exists because
the dashboard reads *seven* independent sources — ``state.json``, the supervisor
log, the ``done/`` sentinels, per-phase recaps, ``notifications.jsonl``, the
ledger, and live ``claude``/``tmux`` probes — every one of which can be missing,
half-written, or written by a newer version of the code than this file. A panel
that raises takes the whole always-on cockpit down with it, so **no function in
this module raises on bad input**; each degrades to an empty/"no data" value and
the caller renders that. It is also the only part worth unit-testing, and it is,
in ``tests/test_tui.py``.

Two forward-compatibility decisions are worth spelling out:

* State is normalised to a plain ``dict`` (see :func:`read_state`) rather than
  passed around as a :class:`~swarm_orchestrator.state.State`. Fields are being
  added to ``Slot``/``State`` concurrently, and ``State.from_dict`` does
  ``Slot(**s)`` — a state file written by a build that knows ``Slot.retiring``
  would raise ``TypeError`` in a dashboard that doesn't. A dict absorbs new keys.
* ``integ_queue`` is read through :func:`normalize_queue`, which accepts both the
  historic ``[phase]`` shape and the ``(phase, status)`` pair shape.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median

from .. import caps
from .. import keep as keep_mod
from .. import ledger as ledger_mod
from .. import ledgerw
from .. import opqueue
from .. import pace as pace_mod
from .. import statuses
from .. import telegram
from .. import logutil
from ..logutil import parse_ts

# Same phase-token shape the ledger accepts, so a positional token is only read
# as a phase when it could actually be one.
_PHASE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/-]*$")

DONE_STATUSES = statuses.ALL
#: Statuses that mean the worker *finished building*; they win over a later
#: ``fail``/``skip`` sentinel for the same phase (mirrors ``gitq.DONE_INTEGRATE``).
COMPLETED_STATUSES = statuses.INTEGRATES


# -- log events -----------------------------------------------------------
@dataclass(frozen=True)
class Event:
    """One parsed supervisor-log line.

    ``phase`` is a *positional guess*: the first token after the verb, when it
    looks like a phase id. That is exact for the lines that carry one
    (``LAUNCH P1 slot=0``, ``EVENT done P1 ok …``) and meaningless noise for the
    prose ones (``MASTER-IDLE paused — holding``). Consumers filter by ``kind``
    first, so a phantom phase on an unrelated kind is never read.
    """

    ts: float | None
    kind: str
    phase: str | None
    status: str | None
    fields: dict[str, str]
    raw: str


def parse_event(line: str) -> Event:
    """Parse one log line into an :class:`Event` (never raises).

    ``EVENT`` is a container verb — ``EVENT done …``, ``EVENT waiting …`` — so it
    is unwrapped and the *inner* verb becomes the kind. Everything else uses its
    own first token (``LAUNCH``, ``PARK``, ``INTEGRATE-MERGED``), lowercased.
    ``k=v`` tokens are collected into ``fields`` generically, so a line that
    grows a new ``k=v`` needs no change here.
    """
    ts, message = parse_ts(line)
    tokens = message.split()
    if not tokens:
        return Event(ts, "", None, None, {}, message)
    kind, rest = tokens[0], tokens[1:]
    if kind == "EVENT" and rest:
        kind, rest = rest[0], rest[1:]
    kind = kind.lower()
    phase = rest[0] if rest and _PHASE_RE.match(rest[0]) else None
    fields = {}
    for tok in rest:
        if "=" in tok:
            key, _, value = tok.partition("=")
            fields.setdefault(key, value)
    status = None
    if kind == "done" and len(rest) > 1 and rest[1] in DONE_STATUSES:
        status = rest[1]
    return Event(ts, kind, phase, status, fields, message)


def parse_events(text: str) -> list[Event]:
    """Parse a whole log body. Blank lines are dropped."""
    return [parse_event(ln) for ln in text.splitlines() if ln.strip()]


class LogTail:
    """Incremental reader for one append-only log.

    The supervisor log is the history source for every graph, and re-reading it
    on each 1s tick is exactly the "redraw everything on a timer" cost the
    dashboard is supposed to avoid. This keeps a byte offset and only decodes
    what was appended. A file that shrank (truncated, rotated, or a fresh state
    dir under the same path) resets the offset instead of returning garbage, and
    a partial trailing line is left unconsumed until its newline arrives.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.events: list[Event] = []
        self._offset = 0
        self._ino: int | None = None

    def poll(self) -> list[Event]:
        """Decode whatever was appended since the last call; returns the new events.

        The first read, and the one after a rotation, starts from the rotated
        files (:func:`logutil.read_all`), so a rotation never empties the history."""
        try:
            st = self.path.stat()
        except OSError:
            return []
        size = st.st_size
        if self._ino is None or st.st_ino != self._ino or size < self._offset:
            self._ino = st.st_ino
            self._offset = 0
            self.events = parse_events(logutil.read_all(self.path, current=False))
        if size == self._offset:
            return []
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except OSError:
            return []
        cut = chunk.rfind("\n")
        if cut < 0:
            return []  # no complete line yet — leave the offset where it was
        complete = chunk[: cut + 1]
        self._offset += len(complete.encode("utf-8", errors="replace"))
        fresh = parse_events(complete)
        self.events.extend(fresh)
        return fresh


# -- state ----------------------------------------------------------------
def read_state(cfg) -> dict | None:
    """The run's state as a plain dict, or ``None`` when there is no run yet.

    Goes through ``state_mod.read`` (which takes the shared flock) so a read can
    never land mid-write, but falls back to a raw ``json.load`` if the typed load
    fails — a state file written by a build that knows a ``Slot`` field this one
    doesn't would otherwise ``TypeError`` and blank the whole dashboard. The
    file is swapped in with ``os.replace``, so the unlocked fallback still sees a
    whole snapshot.

    A missing state file short-circuits to ``None`` rather than going through
    ``state_mod.read``: that call would ``ensure_dirs()`` (the dashboard is
    read-only and must not create a state dir for a project that never ran) and
    then hand back a *synthetic* fresh state, so a swarm that has never started
    would render as a healthy run with N idle slots.
    """
    from .. import state as state_mod  # deferred: keeps import cost off `swarm --help`

    if not Path(cfg.state_path).is_file():
        return None
    try:
        return state_mod.read(cfg).to_dict()
    except Exception:  # noqa: BLE001 - any failure degrades to the raw read
        pass
    try:
        return json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def normalize_queue(raw, statuses: dict | None = None) -> list[tuple[str, str | None]]:
    """``integ_queue`` as ``(phase, status)`` pairs, whatever shape it was stored in.

    Three shapes are in play across the versions this dashboard has to read: the
    historic plain ``["P1"]``, an inline pair (``["P1", "ok"]`` /
    ``{"phase": "P1", "status": "ok"}``), and the shape that actually landed — a
    plain list beside a sibling ``integ_status`` map, passed here as ``statuses``.
    An inline status wins over the side map; a phase in neither reads as ``ok``,
    which is what the queue meant before statuses existed.
    """
    out: list[tuple[str, str | None]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    statuses = statuses if isinstance(statuses, dict) else {}
    for item in raw:
        phase: str | None = None
        status: str | None = None
        if isinstance(item, str):
            phase = item
        elif isinstance(item, dict):
            phase = item.get("phase") if isinstance(item.get("phase"), str) else None
            status = _as_str(item.get("status"))
        elif isinstance(item, (list, tuple)) and item:
            phase = item[0] if isinstance(item[0], str) else None
            status = _as_str(item[1]) if len(item) > 1 else None
        if phase is not None:
            out.append((phase, status or _as_str(statuses.get(phase)) or "ok"))
    return out


def _as_str(value) -> str | None:
    return value if isinstance(value, str) else (None if value is None else str(value))


@dataclass(frozen=True)
class SlotView:
    """One worker slot, joined with everything known about what is in it."""

    id: int
    busy: bool
    phase: str | None
    pane_id: str | None
    branch: str | None
    worktree: str | None
    retiring: bool = False
    started_at: float | None = None
    last_event_at: float | None = None
    live_status: str = "unknown"  # busy | idle | waiting | gone | unknown
    waiting_for: str | None = None
    context_pct: float | None = None
    title: str | None = None

    @property
    def elapsed_s(self) -> float | None:
        return None if self.started_at is None else max(0.0, time.time() - self.started_at)


@dataclass(frozen=True)
class Blocker:
    """Something that will sit forever until the owner acts.

    The single most important thing on the dashboard: today the owner only learns
    about one via a telegram that may have been silently dropped, and a parked
    worker keeps the whole run ``pending`` until it is answered.
    """

    phase: str
    #: waiting | parked | integ | needs-owner | operator-ask | operator-abandoned |
    #: owner-row (an owner-run row that holds other rows up).
    #: Note what is NOT here: a live ``operator`` hand-off. It never needs the
    #: owner — that is the whole point of the status — so it has its own list on
    #: the snapshot and only reaches this one when its session waits on an owner
    #: decision or the queue has given up on it.
    kind: str
    question: str
    since: float | None = None
    deadline: float | None = None
    detail: str = ""


@dataclass(frozen=True)
class Progress:
    """Phase-graph accounting for the overview bar."""

    total: int = 0
    done: int = 0
    failed: int = 0
    running: int = 0
    ready: int = 0
    blocked: int = 0
    excluded: int = 0
    next_up: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def pct(self) -> float:
        return 0.0 if self.total <= 0 else 100.0 * self.done / self.total


#: Why merging stopped, in words, by the kind the supervisor records.
HELD_MERGE = {
    "conflict": "a conflict with main",
    "dirty": "uncommitted changes in the way",
    "push_failed": "a failed push",
}


def held_merge(kind: str | None) -> str:
    """What stopped a merge, for a person: ``conflict`` -> "a conflict with main"."""
    kind = kind or "conflict"
    return HELD_MERGE.get(kind, kind.replace("_", " "))


@dataclass(frozen=True)
class Snapshot:
    """One coherent read of the whole run, ready to render."""

    ok: bool = False
    reason: str = "no run yet"
    slots: list[SlotView] = field(default_factory=list)
    done: dict[str, str] = field(default_factory=dict)
    #: ``done`` plus every row the ledger ticks (status ``ledger``) — the
    #: launcher's view. Campaign counts and the upcoming list read this: on
    #: ``done`` alone a ticked row looks unbuilt and counts as work left.
    landed: dict[str, str] = field(default_factory=dict)
    paused: bool = False
    #: Why a usage cap holds new workers, in plain English; "" when none does.
    usage_hold: str = ""
    #: When the latest-lifting cap that holds it resets; 0.0 when none does.
    hold_until: float = 0.0
    #: ``State.drain``: the run is winding down to a stop (see :mod:`drain`).
    drain: dict = field(default_factory=dict)
    #: ``State.pause_at``: when a scheduled pause happens; 0.0 when none is.
    pause_at: float = 0.0
    finished: bool = False
    master_alive: bool = False
    supervisor_pid: int | None = None
    supervisor_alive: bool = False
    integ_queue: list[tuple[str, str | None]] = field(default_factory=list)
    integ_blocked: str | None = None
    integ_blocked_kind: str | None = None
    integ_blocked_repo: str | None = None
    blockers: list[Blocker] = field(default_factory=list)
    #: The operator hand-off queue, oldest first — work the swarm owes itself.
    operator: list[opqueue.Item] = field(default_factory=list)
    progress: Progress = field(default_factory=Progress)
    windows: dict[str, str] = field(default_factory=dict)
    layout: str | None = None
    started_at: float | None = None
    # Epoch of the last event the supervisor handled. "Nothing has happened for
    # a long time" is the failure this dashboard exists to make visible, and this is
    # the only field that can say it — a live-looking slot grid says nothing.
    last_event_at: float | None = None

    def __post_init__(self) -> None:
        if not self.landed and self.done:
            object.__setattr__(self, "landed", dict(self.done))  # no ledger read

    @property
    def uptime_s(self) -> float | None:
        return None if self.started_at is None else max(0.0, time.time() - self.started_at)


def pid_alive(pid: int | None) -> bool:
    """Whether ``pid`` is a live process (``None``/0 is never alive)."""
    if not pid:
        return False
    try:
        import os

        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def load_graph(cfg) -> dict[str, set[str]]:
    """The phase graph, or an empty one when the ledger is missing/unparseable."""
    try:
        return ledger_mod.load(Path(cfg.project_dir) / cfg.ledger)
    except Exception:  # noqa: BLE001 - a broken ledger must not blank the dashboard
        return {}


def load_ticked(cfg) -> set[str]:
    """The ledger's ``[x]`` rows, or none when the ledger is missing/unreadable."""
    try:
        return ledger_mod.load_ticked(Path(cfg.project_dir) / cfg.ledger)
    except Exception:  # noqa: BLE001 - a broken ledger must not blank the dashboard
        return set()


def load_deferred(cfg) -> dict[str, str]:
    """Open rows that wait for a date still ahead, as the launcher reads them
    (:func:`ledgerw.dated`: the ledger's ``after:`` dates, and a ``later`` finish
    whose report is still queued); none when the ledger is missing/unreadable."""
    try:
        return ledgerw.dated(cfg)
    except Exception:  # noqa: BLE001 - a broken ledger must not blank the dashboard
        return {}


def phase_progress(
    graph: dict[str, set[str]],
    done: dict[str, str],
    busy_phases: set[str],
    excluded: set[str],
    deferred: set[str] | frozenset[str] = frozenset(),
) -> Progress:
    """Count the phase graph into done/running/ready/blocked buckets.

    ``blocked`` is the residual — phases that are neither finished, in flight,
    excluded, nor launchable — which is exactly the set an owner glancing at the
    bar wants to see shrink. The buckets are sets, not subtracted counts: an
    owner-run row the ledger ticks is both excluded and done, and counting it
    twice shrank ``blocked``. ``ready``/``next_up`` reuse
    :func:`swarm_orchestrator.ledger.ready` so the dashboard can never disagree
    with what the master will actually launch — which is why a row waiting for
    its ``after:`` date (``deferred``) is not ready but blocked, as it is there.
    """
    if not graph:
        return Progress(done=len(done), running=len(busy_phases))
    ready = ledger_mod.ready(graph, done, busy_phases, set(excluded) | set(deferred))
    running = {p for p in busy_phases if p in graph}
    # Done the way every other count says it (:mod:`.campaign`): a status that
    # releases dependents. A ``fail`` was attempted, not done.
    done_set = {p for p in graph if done.get(p) in statuses.SATISFIES_DEPS} - running
    # A finished row is done even when it is owner-run, as on the web board.
    excluded_set = {p for p in excluded if p in graph} - done_set - running
    failed = {p for p in graph if done.get(p) == statuses.FAIL} - running - excluded_set
    blocked = set(graph) - done_set - failed - running - set(ready) - excluded_set
    return Progress(
        total=len(graph),
        done=len(done_set),
        failed=len(failed),
        running=len(running),
        ready=len(ready),
        blocked=len(blocked),
        excluded=len(excluded_set),
        next_up=ready[:6],
        issues=ledger_mod.validate(
            graph, {p for p, s in done.items() if s in statuses.SATISFIES_DEPS}),
    )


def build_snapshot(
    cfg,
    state: dict | None,
    graph: dict[str, set[str]] | None = None,
    launch_times: dict[str, float] | None = None,
    questions: dict[str, str] | None = None,
    started_at: float | None = None,
    operator: list | None = None,
    ticked: set[str] | None = None,
    deferred: dict[str, str] | None = None,
) -> Snapshot:
    """Join state + ledger + log-derived timings into one render-ready snapshot.

    ``launch_times``/``questions`` are injected rather than read here so this stays
    pure and testable: the caller owns the incremental log tail and the
    notification index, this owns the joining rules.
    """
    if not state:
        return Snapshot(ok=False, reason="the swarm has not run here yet — `swarm up` starts it")
    graph = graph if graph is not None else {}
    launch_times = launch_times or {}
    questions = questions or {}
    operator = list(operator or [])

    slots: list[SlotView] = []
    for raw in state.get("slots") or []:
        if not isinstance(raw, dict):
            continue
        phase = raw.get("phase")
        slots.append(
            SlotView(
                id=int(raw.get("id", len(slots))),
                busy=bool(raw.get("busy")),
                phase=phase,
                pane_id=raw.get("pane_id"),
                branch=raw.get("branch"),
                worktree=raw.get("worktree"),
                retiring=bool(raw.get("retiring", False)),
                started_at=launch_times.get(phase) if phase else None,
                last_event_at=_as_float(raw.get("last_event_at")),
            )
        )

    # A row that waits for its date has not failed, whatever its record says
    # until the ledger has taken its report: every panel reads this view.
    done = ledgerw.not_failed(
        {k: str(v) for k, v in (state.get("done") or {}).items()}, deferred or {})
    waiting = state.get("waiting") or {}
    parked = list(state.get("parked") or [])
    # A parked session the owner has answered (``State.answered``) is working
    # again in its own window: still in flight, and no longer a question.
    answered = state.get("answered") or {}
    # When a parked session's unanswered question was asked (``State.asked``).
    asked = state.get("asked") or {}
    busy_phases = {s.phase for s in slots if s.busy and s.phase}
    in_flight = busy_phases | set(parked) | {p for p in waiting}
    # The launcher's done view: a ticked row with no record counts as landed.
    landed = ledger_mod.with_ticked(done, ticked or set(), in_flight)

    blockers: list[Blocker] = []
    from .. import state as state_mod  # deferred, like the reader's

    # An operator job's or an Overseer pass's key is listed from its own record
    # (the queue item below, the pass in the timeline), not as a worker.
    def worker(key: str) -> bool:
        return state_mod.waiter(key)[0] == state_mod.WORKER

    for phase, deadline in sorted(waiting.items()):
        if not worker(phase):
            continue
        blockers.append(
            Blocker(
                phase=phase,
                kind="waiting",
                question=questions.get(phase, ""),
                since=launch_times.get(phase),
                deadline=_as_float(deadline),
            )
        )
    for phase in parked:
        if not worker(phase) or phase in answered:
            continue
        blockers.append(
            Blocker(
                phase=phase,
                kind="parked",
                question=questions.get(phase, ""),
                since=_as_float(asked.get(phase)) or launch_times.get(phase),
                detail=f"its worker waits in tmux window {state_mod.wait_window(phase)};"
                " the run cannot finish until you answer",
            )
        )
    blocked_phase = state.get("integ_blocked")
    if blocked_phase:
        kind = state.get("integ_blocked_kind") or "conflict"
        repo = Path(state.get("integ_blocked_repo") or "?").name
        fix = (f"a resolver is fixing it in tmux window resolve-{blocked_phase}"
               if kind == "conflict" else f"fix it, then run `swarm resolved {blocked_phase}`")
        blockers.append(
            Blocker(
                phase=str(blocked_phase),
                kind="integ",
                question=f"{held_merge(kind)} in {repo} stops all merging; {fix}",
                detail=f"`swarm resolved {blocked_phase}` starts merging again",
            )
        )
    # The retired spelling only, and unconditionally: an older run can still hold
    # `needs-owner` finishes, and they genuinely still need them. Its successor
    # `operator` is deliberately absent — it hands its action to a session, so
    # listing it here would refill "needs you" with the exact thing that status
    # exists to keep out of it.
    for phase, status in sorted(done.items()):
        if status == statuses.NEEDS_OWNER:
            blockers.append(
                Blocker(phase=phase, kind=statuses.NEEDS_OWNER,
                        question=questions.get(phase, ""))
            )
    # An operator job reaches the owner in two ways that read very differently
    # to a human: a live session is waiting on their decision (or, from an older
    # version, asked and ended), or the queue tried MAX_ATTEMPTS times and gave up.
    for item in operator:
        if item.state == opqueue.WAITING:
            key = state_mod.waiter_key(state_mod.OPERATOR, item.phase)
            window = state_mod.wait_window(key) if key in parked else "operator"
            blockers.append(
                Blocker(
                    phase=item.phase,
                    kind="operator-ask",
                    question=item.question,
                    since=item.asked_at or item.queued_at or None,
                    detail=f"answer in tmux window {window} · the job: {item.note}",
                )
            )
        elif item.state == opqueue.ABANDONED:
            blockers.append(
                Blocker(
                    phase=item.phase,
                    kind="operator-ask" if item.asked else "operator-abandoned",
                    question=item.last_error if item.asked else item.note,
                    since=item.queued_at or None,
                    detail=item.note if item.asked
                    else (item.last_error or "the operator job gave up after its retries;"
                          " it is yours to do by hand now"),
                )
            )

    # Rows only the owner can do that hold other rows up: nothing else would
    # ever ask about them.
    for row, n in ledger_mod.owner_rows(graph, landed, set(getattr(cfg, "exclude", []) or []),
                                        in_flight):
        blockers.append(
            Blocker(phase=row, kind="owner-row",
                    question=f"only you can do this, and {n} row{' waits' if n == 1 else 's wait'}"
                    f" on it; once done, tick it in the ledger or run `swarm skip {row}`",
                    detail=f"once it is done, tick it in the ledger or run `swarm skip {row}`")
        )

    pid = state.get("supervisor_pid")
    return Snapshot(
        ok=True,
        reason="",
        slots=slots,
        done=done,
        landed=landed,
        paused=bool(state.get("paused")),
        usage_hold=" ".join(caps.describe_hold(state.get("usage_hold") or {}, time.time())),
        hold_until=max((_as_float(h.get("resets_at")) or 0.0
                        for h in (state.get("usage_hold") or {}).values() if isinstance(h, dict)),
                       default=0.0),
        drain=dict(state.get("drain") or {}) if isinstance(state.get("drain"), dict) else {},
        pause_at=_as_float(state.get("pause_at")) or 0.0,
        finished=bool(state.get("finished")),
        master_alive=bool(state.get("master_alive")),
        supervisor_pid=pid if isinstance(pid, int) else None,
        supervisor_alive=pid_alive(pid),
        integ_queue=normalize_queue(state.get("integ_queue"), state.get("integ_status")),
        integ_blocked=_as_str(state.get("integ_blocked")),
        integ_blocked_kind=_as_str(state.get("integ_blocked_kind")),
        integ_blocked_repo=_as_str(state.get("integ_blocked_repo")),
        blockers=blockers,
        operator=operator,
        progress=phase_progress(
            graph,
            landed,
            in_flight,
            set(getattr(cfg, "exclude", []) or []),
            set(deferred or ()),
        ),
        windows=dict(state.get("windows") or {}),
        layout=_as_str(state.get("layout")),
        started_at=started_at,
        last_event_at=_as_float(state.get("last_event_at")) or None,
    )


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# -- notifications --------------------------------------------------------
@dataclass(frozen=True)
class Notification:
    """One line of ``notifications.jsonl`` — why the owner's phone buzzed."""

    ts: float | None
    kind: str
    phase: str | None
    source: str
    text: str
    delivered: bool
    error: str
    raw: dict = field(default_factory=dict)
    #: Why the swarm deliberately did not send it ("" = it tried to). Such a
    #: message was never meant to reach the phone, so it is not a drop.
    suppressed: str = ""

    @property
    def dropped(self) -> bool:
        """It was meant to reach the owner and did not."""
        return not self.delivered and not self.suppressed


def open_drops(notifications: list[Notification], acked: float = 0.0) -> list[Notification]:
    """The pings that never reached the owner and that the owner has not
    acknowledged (``swarm notify --ack``): what the warnings count."""
    return [n for n in notifications or [] if n.dropped and telegram.unacknowledged(n.ts, acked)]


def parse_notification(line: str) -> Notification | None:
    """One JSONL line, or ``None`` if it isn't a usable object.

    The file is appended to by a live process, so the last line can be a partial
    write; a half-object simply drops instead of poisoning the table.
    """
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    return Notification(
        ts=coerce_ts(obj.get("ts")),
        kind=str(obj.get("kind") or ""),
        phase=_as_str(obj.get("phase")),
        source=str(obj.get("source") or ""),
        text=str(obj.get("text") or obj.get("message") or ""),
        # Absent `delivered` means "not recorded", which is closer to a failure
        # than a success — never claim a ping landed when nothing said it did.
        delivered=bool(obj.get("delivered")),
        error=str(obj.get("error") or ""),
        raw=obj,
        suppressed=str(obj.get("suppressed") or ""),
    )


def load_notifications(path: Path, limit: int | None = None) -> list[Notification]:
    """Parse ``notifications.jsonl`` in file order. Missing file ⇒ ``[]``."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = [n for n in (parse_notification(ln) for ln in text.splitlines()) if n]
    return out[-limit:] if limit else out


def coerce_ts(value) -> float | None:
    """Epoch seconds from a float, an int, or an ISO-8601 string. ``None`` if neither.

    The notification writer is a sibling agent's code and may settle on either an
    epoch or an ISO stamp; accepting both costs three lines and avoids a whole
    column reading ``—``.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            try:
                return float(raw)
            except ValueError:
                return None
    return None


def question_index(notifications: list[Notification], sentinels: dict) -> dict[str, str]:
    """Best known "what is this phase asking me?" text, per phase.

    ``swarm waiting`` telegrams the worker's question but deliberately does NOT
    put it in state or over the FIFO, so the notification log is the only place
    the text survives. The completion sentinel's note is the fallback for a
    ``needs-owner`` finish. Later notifications win — a worker may ask twice.
    """
    out: dict[str, str] = {}
    for phase, sentinel in sentinels.items():
        if sentinel.status == "needs-owner" and sentinel.note:
            out[phase] = sentinel.note
    for note in notifications:
        if note.phase and note.text:
            out[note.phase] = note.text
    return out


# -- recaps + sentinels ---------------------------------------------------
@dataclass(frozen=True)
class Recap:
    """A phase's generated recap (``recaps/<phase>.json``)."""

    phase: str
    status: str = ""
    summary: str = ""
    raw: str = ""
    ts: float | None = None
    source: str = ""


def load_recap(recap_dir: Path, phase: str) -> Recap | None:
    """One phase's recap, or ``None`` when the recap feature hasn't written it."""
    try:
        obj = json.loads((Path(recap_dir) / f"{phase}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    return Recap(
        phase=str(obj.get("phase") or phase),
        status=str(obj.get("status") or ""),
        summary=str(obj.get("summary") or ""),
        raw=str(obj.get("raw") or ""),
        ts=coerce_ts(obj.get("ts")),
        source=str(obj.get("source") or ""),
    )


def load_recaps(recap_dir: Path) -> dict[str, Recap]:
    """Every recap on disk, keyed by phase. Missing directory ⇒ ``{}``."""
    out: dict[str, Recap] = {}
    try:
        entries = sorted(Path(recap_dir).glob("*.json"))
    except OSError:
        return out
    for entry in entries:
        recap = load_recap(entry.parent, entry.stem)
        if recap is not None:
            out[recap.phase] = recap
    return out


@dataclass(frozen=True)
class Sentinel:
    """A ``done/<phase>.<status>`` file: the worker's own recap of its run.

    Nothing in the codebase had ever read these back — they existed only so a
    restart could tell a finished phase from an interrupted one. The note body is
    the worker's own words about what it did, which is exactly what the History
    tab wants.
    """

    phase: str
    status: str
    note: str = ""
    mtime: float | None = None


def parse_sentinel(name: str, body: str, mtime: float | None = None) -> Sentinel | None:
    """Decode one sentinel file. ``None`` if the name isn't ``<phase>.<status>``."""
    phase, _, status = name.rpartition(".")
    if not phase or status not in DONE_STATUSES:
        return None
    text = body.strip()
    prefix = f"{phase} {status}"
    if text.startswith(prefix):
        text = text[len(prefix) :].strip()
    return Sentinel(phase=phase, status=status, note=text, mtime=mtime)


def load_sentinels(done_dir: Path) -> dict[str, Sentinel]:
    """Every completion sentinel, keyed by phase.

    A phase can hold several (a ``fail`` attempt then an ``ok`` one); the same
    precedence ``gitq.sentinel_done`` uses applies — a completed build wins over
    a ``fail``/``skip``, and among equals the newest file wins.
    """
    out: dict[str, Sentinel] = {}
    try:
        entries = sorted(Path(done_dir).iterdir())
    except OSError:
        return out
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_file():
                continue
            body = entry.read_text(encoding="utf-8", errors="replace")
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        sentinel = parse_sentinel(entry.name, body, mtime)
        if sentinel is None:
            continue
        prior = out.get(sentinel.phase)
        if prior is not None:
            if prior.status in COMPLETED_STATUSES and sentinel.status not in COMPLETED_STATUSES:
                continue
            if (prior.mtime or 0) > (sentinel.mtime or 0) and (
                prior.status in COMPLETED_STATUSES or sentinel.status not in COMPLETED_STATUSES
            ):
                continue
        out[sentinel.phase] = sentinel
    return out


def load_attempts(done_dir: Path, phase: str) -> list[dict]:
    """A phase's full attempt history from ``done/<phase>.jsonl`` (``[]`` if absent)."""
    try:
        text = (Path(done_dir) / f"{phase}.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# -- worker notes ---------------------------------------------------------
@dataclass(frozen=True)
class Note:
    """One judgement call a worker recorded without pinging anyone.

    ``swarm note`` is the swarm's third register: too small to stop the world
    for, too important to bury in a recap. The dashboard is its intended reader —
    a phase that quietly made six unilateral decisions looks identical to a clean
    one everywhere else.
    """

    phase: str
    kind: str = "decision"  # decision | assumption | risk
    text: str = ""
    ts: float | None = None


def load_notes(notes_dir: Path, phase: str) -> list[Note]:
    """One phase's notes, oldest first. Missing/torn files yield what parsed."""
    try:
        text = (Path(notes_dir) / f"{phase}.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Note] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # a torn final line costs that line, not the file
        if not isinstance(obj, dict):
            continue
        out.append(
            Note(
                phase=str(obj.get("phase") or phase),
                kind=str(obj.get("kind") or "decision"),
                text=str(obj.get("text") or ""),
                ts=coerce_ts(obj.get("ts")),
            )
        )
    return out


def load_all_notes(notes_dir: Path) -> dict[str, list[Note]]:
    """Every phase's notes, keyed by phase. Absent directory yields ``{}``."""
    out: dict[str, list[Note]] = {}
    try:
        entries = sorted(Path(notes_dir).glob("*.jsonl"))
    except OSError:
        return out
    for entry in entries:
        notes = load_notes(entry.parent, entry.stem)
        if notes:
            out[entry.stem] = notes
    return out


# -- history --------------------------------------------------------------
#: A phase run whose claim ended without ``swarm done``: ``swarm up`` rebuilt the
#: slots, the watchdog reaped a dead pane, ``swarm free``/``skip`` let it go, or
#: its worker never started. Not a ``swarm done`` status: it exists only here.
LOST = "lost"
#: How a history status reads, where the status code alone would not say it.
RUN_WORDS = {LOST: "worker gone", statuses.LEDGER: "done elsewhere"}
#: Why a run ended without a report, by its ``RUN-ENDED reason=`` (or by what
#: closed it at read time, for a log written before that line existed).
ENDED_WHY = {
    "restart": "the swarm was restarted (`swarm up`) while it held a slot",
    "reaped": "its pane died and the watchdog freed the slot",
    "freed": "its slot was freed with `swarm free`",
    "skipped": "it was skipped with `swarm skip`",
    "launch-failed": "its worker never started",
    "relaunched": "the phase was started again before this run reported",
    "stale": "no slot holds it any more",
}


#: Lines that freed a claimed slot before ``RUN-ENDED`` existed, by what they mean.
_ENDED_BY = {
    "watchdog-reap": "reaped",
    "launch-fail": "launch-failed",
    "worktree-fail": "launch-failed",
    "launch-error": "launch-failed",
}


def run_word(status: str | None) -> str | None:
    """A history status in the owner's words: ``lost`` reads "worker gone"."""
    return RUN_WORDS.get(status or "", status)


@dataclass(frozen=True)
class PhaseRun:
    """One attempt at one phase, joined across the log, sentinel and recap."""

    phase: str
    status: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    summary: str = ""
    note: str = ""
    parked: bool = False
    slot: str | None = None
    attempts: int = 0
    notes: int = 0
    #: Still live, but not in a slot: ``parked``, ``waiting`` or ``integrating``.
    hold: str = ""
    #: Why its claim ended without a report (an :data:`ENDED_WHY` key), even when
    #: a sentinel or the ledger later said how the phase came out.
    why: str = ""

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None:
            return None
        if self.ended_at is None and self.status is not None:
            return None  # over, and when it ended is not known
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.started_at)

    @property
    def running(self) -> bool:
        return (self.started_at is not None and self.ended_at is None
                and self.status is None and not self.hold)


def _live_holds(state: dict) -> dict[str, str]:
    """Phase -> how the current state holds it: ``running`` (a busy slot, or a
    parked worker the owner has answered, at work in its own window), else
    ``waiting``, ``parked`` or ``integrating``."""
    out: dict[str, str] = {}
    answered = state.get("answered") or {}
    for phase, _ in normalize_queue(state.get("integ_queue")):
        out[phase] = "integrating"
    if isinstance(state.get("integ_blocked"), str):
        out[state["integ_blocked"]] = "integrating"
    for name in ("parked", "waiting"):
        for key in state.get(name) or []:
            if isinstance(key, str) and ":" not in key:  # a worker's key is its phase
                out[key] = "running" if name == "parked" and key in answered else name
    for raw in state.get("slots") or []:
        if isinstance(raw, dict) and raw.get("busy") and isinstance(raw.get("phase"), str):
            out[raw["phase"]] = "running"
    return out


def build_history(
    events: list[Event],
    sentinels: dict[str, Sentinel] | None = None,
    recaps: dict[str, Recap] | None = None,
    done_dir: Path | None = None,
    notes: dict[str, list[Note]] | None = None,
    state: dict | None = None,
    ticked: set[str] | None = None,
) -> list[PhaseRun]:
    """Every phase run ever seen, newest first.

    Runs are paired off the log (``LAUNCH`` opens one, ``EVENT done`` closes it),
    which naturally handles a phase that was retried: each launch opens a fresh
    run. A phase whose launch has rotated out of the log — or that only ever
    produced a sentinel — still appears, with whatever times are known. The recap
    summary and the sentinel note attach to the *last* run of each phase, since
    both are single-slot per phase on disk.

    A claim that ended without ``swarm done`` is closed as :data:`LOST` by its
    ``RUN-ENDED`` line, by the next ``SUPERVISOR-START`` (every start follows
    ``swarm up``, which rebuilds the slots) or by a fresh claim of the same phase.
    With ``state`` given, what is still open is checked against it: a run is
    ``running`` only while a busy slot holds its phase (or it is parked and at
    work on the owner's answer, :func:`_live_holds`), so a log that never
    recorded the end cannot keep a dead worker "running". A lost run that left a
    sentinel reads as that sentinel; one the ledger has ticked since (``ticked``)
    and this swarm holds no report of reads as "done elsewhere".
    """
    sentinels = sentinels or {}
    recaps = recaps or {}
    ordered = sorted(events, key=lambda e: (e.ts is None, e.ts or 0.0))
    open_runs: dict[str, dict] = {}
    runs: list[dict] = []

    def lose(run: dict, ts: float | None, why: str) -> None:
        run.update(ended_at=ts, status=LOST, why=why)
        runs.append(run)

    for ev in ordered:
        if ev.kind in ("launch", "claim") and ev.phase:
            prior = open_runs.get(ev.phase)
            if prior is not None and ev.kind == "claim":
                if prior["by"] == "claim":
                    continue  # the same claim logged twice is one run
                lose(open_runs.pop(ev.phase), None, "relaunched")
            elif prior is not None and prior["by"] == "launch":
                lose(open_runs.pop(ev.phase), None, "relaunched")
            # else: CLAIM then LAUNCH is one run; the launch is when it started.
            open_runs[ev.phase] = {
                "phase": ev.phase,
                "started_at": ev.ts,
                "slot": ev.fields.get("slot"),
                "by": ev.kind,
            }
        elif ev.kind == "done" and ev.phase:
            run = open_runs.pop(ev.phase, {"phase": ev.phase, "started_at": None, "slot": None})
            run["ended_at"] = ev.ts
            run["status"] = ev.status
            run["parked"] = ev.fields.get("parked", "").lower() == "true"
            run["closed"] = "done"
            runs.append(run)
        elif ev.kind == logutil.RUN_ENDED.lower() and ev.phase in open_runs:
            lose(open_runs.pop(ev.phase), ev.ts, ev.fields.get("reason") or "stale")
        elif ev.kind in _ENDED_BY and ev.phase in open_runs:
            # A log from before RUN-ENDED: the line that freed the slot is the end.
            lose(open_runs.pop(ev.phase), ev.ts, _ENDED_BY[ev.kind])
        elif ev.kind == "supervisor-start":
            for phase in list(open_runs):
                lose(open_runs.pop(phase), ev.ts, "restart")

    holds = _live_holds(state) if isinstance(state, dict) else None
    for run in open_runs.values():
        hold = "running" if holds is None else holds.get(run["phase"])
        if hold is None:
            lose(run, None, "stale")
            continue
        if hold != "running":
            run["hold"] = hold
        runs.append(run)

    seen = {r["phase"] for r in runs}
    for phase, sentinel in sentinels.items():
        if phase not in seen:
            runs.append(
                {
                    "phase": phase,
                    "started_at": None,
                    "ended_at": sentinel.mtime,
                    "status": sentinel.status,
                    "slot": None,
                    "closed": "done",
                }
            )

    last_index: dict[str, int] = {}
    for idx, run in enumerate(runs):
        last_index[run["phase"]] = idx

    out: list[PhaseRun] = []
    for idx, run in enumerate(runs):
        phase = run["phase"]
        is_last = last_index.get(phase) == idx
        sentinel = sentinels.get(phase) if is_last else None
        recap = recaps.get(phase) if is_last else None
        status, ended = run.get("status"), run.get("ended_at")
        if run.get("closed") == "done":
            # The SENTINEL wins over the log event, not the other way round.
            # `EVENT done <phase> ok` is not trustworthy as a status: the
            # merge queue used to hardcode "ok" when advancing an integrated
            # phase, so every needs-owner run in an existing log reads as ok
            # (all of them did). The sentinel filename is what the
            # worker itself wrote and is the durable record every other
            # consumer treats as authoritative.
            status = (sentinel.status if sentinel else None) or status
        elif status == LOST and is_last:
            start = run.get("started_at")
            if sentinel and (start is None or (sentinel.mtime or 0.0) >= start):
                # It did report — with the supervisor down, or after its slot went.
                status = sentinel.status
                if sentinel.mtime is not None:
                    ended = sentinel.mtime if ended is None else min(ended, sentinel.mtime)
            elif phase in (ticked or ()):
                status = statuses.LEDGER
        out.append(
            PhaseRun(
                phase=phase,
                status=status,
                started_at=run.get("started_at"),
                ended_at=ended,
                summary=recap.summary if recap else "",
                note=sentinel.note if sentinel else "",
                parked=bool(run.get("parked")),
                slot=run.get("slot"),
                attempts=len(load_attempts(done_dir, phase)) if (done_dir and is_last) else 0,
                notes=len((notes or {}).get(phase, ())) if is_last else 0,
                hold=run.get("hold", ""),
                why=run.get("why", ""),
            )
        )
    out.sort(key=lambda r: (r.ended_at or r.started_at or 0.0), reverse=True)
    return out


def launch_times(events: list[Event]) -> dict[str, float]:
    """Most recent launch timestamp per phase — the basis for every "elapsed"."""
    out: dict[str, float] = {}
    for ev in events:
        if ev.kind in ("launch", "claim") and ev.phase and ev.ts is not None:
            out[ev.phase] = ev.ts
    return out


def run_started_at(events: list[Event]) -> float | None:
    """When the current supervisor came up — the header's uptime clock."""
    for ev in reversed(events):
        if ev.kind == "supervisor-start" and ev.ts is not None:
            return ev.ts
    return None


# -- graph series ---------------------------------------------------------
@dataclass(frozen=True)
class Series:
    """A labelled time series: ``(epoch, value)`` points in ascending time."""

    label: str
    points: list[tuple[float, float]] = field(default_factory=list)
    maximum: float | None = None

    @property
    def values(self) -> list[float]:
        return [v for _, v in self.points]

    @property
    def span(self) -> tuple[float, float] | None:
        return (self.points[0][0], self.points[-1][0]) if self.points else None


def completions_series(events: list[Event]) -> Series:
    """Cumulative phases completed over wall-clock time."""
    points: list[tuple[float, float]] = []
    total = 0
    for ev in sorted(events, key=lambda e: e.ts or 0.0):
        if ev.kind == "done" and ev.ts is not None and ev.status != "fail":
            total += 1
            points.append((ev.ts, float(total)))
    return Series("phases completed", points, maximum=float(total) if total else None)


def occupancy_series(events: list[Event], max_workers: int) -> Series:
    """Busy-slot count over time — the series behind the utilisation number.

    A ``PARK`` frees a slot immediately, and the ``EVENT done`` that follows it
    carries ``freed_slot=None``, so decrementing on both would double-count. The
    ``freed_slot`` field is the authority for whether a ``done`` actually freed
    anything.
    """
    points: list[tuple[float, float]] = []
    busy = 0
    for ev in sorted(events, key=lambda e: e.ts or 0.0):
        if ev.ts is None:
            continue
        if ev.kind == "launch":
            busy += 1
        elif ev.kind == "park":
            busy -= 1
        elif ev.kind == "done":
            if ev.fields.get("freed_slot", "None") == "None":
                continue  # already freed by an earlier PARK
            busy -= 1
        else:
            continue
        busy = max(0, min(busy, max_workers) if max_workers > 0 else max(0, busy))
        points.append((ev.ts, float(busy)))
    return Series("busy slots", points, maximum=float(max_workers) if max_workers else None)


def utilisation(series: Series, max_workers: int, now: float | None = None) -> float:
    """Time-weighted mean occupancy as a fraction of capacity (0..1).

    Time-weighted, not a point average: a swarm that ran 4 workers for a minute
    and 1 for an hour is not 62% utilised, and the point average says it is.
    """
    if not series.points or max_workers <= 0:
        return 0.0
    now = now if now is not None else time.time()
    total_area = 0.0
    for (t0, v0), (t1, _) in zip(series.points, series.points[1:]):
        total_area += v0 * max(0.0, t1 - t0)
    last_t, last_v = series.points[-1]
    total_area += last_v * max(0.0, now - last_t)
    window = max(0.0, now - series.points[0][0])
    if window <= 0:
        return 0.0
    return min(1.0, total_area / (window * max_workers))


def phase_durations(runs: list[PhaseRun], limit: int = 20) -> list[tuple[str, float]]:
    """Finished phases by wall-clock duration, longest first."""
    out = [
        (r.phase, r.duration_s)
        for r in runs
        if r.duration_s is not None and not r.running
    ]
    out.sort(key=lambda item: item[1], reverse=True)
    return out[:limit]


#: Below this many finished phases a median is not a forecast, it is one number
#: wearing a decoration. The screen says so instead of inventing a time.
ETA_MIN_SAMPLES = 3

#: Only recent completions predict the next ones — a campaign's scaffolding waves
#: look nothing like the waves that follow them.
ETA_WINDOW = 20


def typical_durations(runs: list[PhaseRun]) -> list[float]:
    """The recent completed phase durations every forecast is made from."""
    return [
        r.duration_s
        for r in (runs or [])
        if not r.running and r.duration_s and r.status in COMPLETED_STATUSES
    ][:ETA_WINDOW]


def eta_sample(history: list[PhaseRun], epoch: float | None) -> tuple[list[PhaseRun], bool]:
    """``(runs, from_history)``: the phase runs every ETA of the open run is made from.

    Only phases that finished in this run: yesterday's run at four workers on a
    different campaign does not predict today's. Until the run has
    :data:`ETA_MIN_SAMPLES` of its own, the whole history stands in and says so
    (``from_history``) — a borrowed figure is better than none, if it is labelled.
    No epoch (a state dir from before runs existed) keeps the old all-history read.
    """
    history = list(history or [])
    if epoch is None:
        return history, False
    mine = [r for r in history if r.running or (r.ended_at is not None and r.ended_at >= epoch)]
    if len(typical_durations(mine)) >= ETA_MIN_SAMPLES:
        return mine, False
    if len(typical_durations(history)) >= ETA_MIN_SAMPLES:
        return history, True
    return mine, False


def eta_runs_of(dash) -> list[PhaseRun]:
    """What a view should hand the ETA functions: the run's sample if the dash has one."""
    got = getattr(dash, "eta_runs", None)
    return list(got) if got is not None else list(getattr(dash, "history", None) or [])


def finish_times(landed: dict[str, str], ticked: set[str], history: pace_mod.History,
                 runs: list[PhaseRun]) -> tuple[dict[str, float], set[str]]:
    """``(phase -> when it finished, bulk rows)`` for every phase that counts as done.

    Done is the one definition every count uses: a status in ``landed`` that
    releases dependents. The time is the commit that ticked the row in the
    ledger — the same on every machine — or, for a row the swarm holds done that
    the ledger has not ticked, when this machine's run of it ended. A skip the
    ledger does not tick has no time: nobody built it. ``bulk`` are the rows a
    many-row commit ticked, which :func:`pace.measure` leaves out of the pace.
    """
    local: dict[str, float] = {}
    for run in runs or []:  # newest first: the first finish seen is the latest
        if run.status in COMPLETED_STATUSES and run.ended_at is not None:
            local.setdefault(run.phase, run.ended_at)
    out: dict[str, float] = {}
    bulk: set[str] = set()
    for phase, status in landed.items():
        if status not in statuses.SATISFIES_DEPS:
            continue
        tick = history.ticks.get(phase) if phase in ticked else None
        if tick is not None:
            out[phase] = tick[0]
            if tick[1] > pace_mod.BULK:
                bulk.add(phase)
        elif phase in local:
            out[phase] = local[phase]
    return out, bulk


def idle_spans(events: list[Event], now: float | None = None) -> list[tuple[float, float]]:
    """When this machine's log says the swarm could not work.

    Its supervisor was down, the owner paused it (every ``WATCHDOG`` line says
    ``paused=``), or a usage cap held it (``USAGE-HOLD``/``USAGE-RELEASE``, and
    the ``held=`` of every ``USAGE-CHECK``, which is all a restart logs about a
    hold it inherited). A two-day hold is not a slow swarm.
    """
    spans: list[tuple[float, float]] = []
    down = paused = held = False
    since: float | None = None
    for ev in sorted((e for e in events if e.ts is not None), key=lambda e: e.ts):
        kind = ev.kind
        if kind in ("supervisor-start", "supervisor-stop"):
            down = kind == "supervisor-stop"
        elif kind == "watchdog" and "paused" in ev.fields:
            paused = ev.fields["paused"] == "True"
        elif kind in ("usage-hold", "usage-release"):
            held = kind == "usage-hold"
        elif kind == "usage-check" and "held" in ev.fields:
            held = ev.fields["held"] not in ("", "-")
        else:
            continue
        idle = down or paused or held
        if idle and since is None:
            since = ev.ts
        elif not idle and since is not None:
            spans.append((since, ev.ts))
            since = None
    if since is not None:
        spans.append((since, time.time() if now is None else now))
    return spans


def phase_eta(runs: list[PhaseRun], elapsed_s: float | None) -> tuple[float | None, bool]:
    """``(seconds, overrun)`` for one running phase against the typical one.

    Seconds left while it is inside the median, seconds *over* once it is past
    it — an overrun is the early sign of a stuck worker or a phase that should
    have been two, and "0m left" for three hours would hide exactly that.
    """
    seen = typical_durations(runs)
    if elapsed_s is None or len(seen) < ETA_MIN_SAMPLES:
        return None, False
    typical = median(seen)
    return (elapsed_s - typical, True) if elapsed_s > typical else (typical - elapsed_s, False)


def fmt_phase_eta(runs: list[PhaseRun], elapsed_s: float | None) -> str:
    seconds, over = phase_eta(runs, elapsed_s)
    if seconds is None:
        return "—"
    return f"+{fmt_coarse(seconds)} over" if over else f"~{fmt_coarse(seconds)} left"


def fmt_when(ts: float | None, now: float | None = None) -> str:
    """A future moment at a forecast's resolution: ``03:40``, ``Thu 03:40``, ``09-25 03:40``."""
    if ts is None:
        return "—"
    now = time.time() if now is None else now
    try:
        when, today = datetime.fromtimestamp(ts), datetime.fromtimestamp(now)
    except (OSError, OverflowError, ValueError):
        return "—"
    if when.date() == today.date():
        return when.strftime("%H:%M")
    if abs(ts - now) < 6 * 86400:
        return when.strftime("%a %H:%M")
    return when.strftime("%m-%d %H:%M")


def fmt_range(soonest: float, latest: float | None = None) -> str:
    """A forecast at the resolution it is good to: ``~40m``, ``~2–4h``, ``~2–3 days``.

    A pace measured over a day is not good to the minute, so minutes come in
    fives, hours whole and anything past a day and a half in days.
    """
    latest = soonest if latest is None else latest
    if latest < 90 * 60:
        step, unit = 300.0, "m"
    elif latest < 36 * 3600:
        step, unit = 3600.0, "h"
    else:
        step, unit = 86400.0, " days"
    lo = max(1, round(soonest / step)) * step
    hi = max(1, round(latest / step)) * step
    if unit == "m":
        lo, hi = int(lo // 60), int(hi // 60)
    else:
        lo, hi = int(lo // step), int(hi // step)
    if unit == " days" and hi == 1:
        unit = " day"
    return f"~{lo}{unit}" if lo == hi else f"~{lo}–{hi}{unit}"


def fmt_when_range(soonest: float, latest: float, now: float | None = None) -> str:
    """:func:`fmt_when` for a range, to ten minutes: ``18:00–20:00``, ``Wed 13:00–15:00``."""
    ten = 600.0
    a = fmt_when(round(soonest / ten) * ten, now)
    b = fmt_when(round(latest / ten) * ten, now)
    if a == b:
        return a
    day_a, _, _ = a.rpartition(" ")
    day_b, _, clock_b = b.rpartition(" ")
    return f"{a}–{clock_b}" if day_a == day_b else f"{a} – {b}"


# -- meters: what each worker's status line reported ------------------------
#: Past this, every turn re-sends a conversation the audit found wasteful: the
#: phase is a candidate to be split (see prompts/init_master.md, context budget).
CONTEXT_BUDGET = 300_000


@dataclass(frozen=True)
class Meter:
    """One worker's latest status-line figures (written by ``swarm_orchestrator.meters``)."""

    phase: str
    ts: float
    started_at: float | None = None
    context_tokens: float | None = None
    context_window: float | None = None
    peak_tokens: float | None = None
    cost_usd: float | None = None
    duration_ms: float | None = None
    effort: str | None = None
    #: The raw ``{"pct", "resets_at"}`` windows; :func:`load_limits` reads them.
    seven_day: dict | None = field(default=None, compare=False)
    five_hour: dict | None = field(default=None, compare=False)

    @property
    def burn_per_h(self) -> float | None:
        """API-equivalent $/h over the session so far; ``None`` under a minute in."""
        if self.cost_usd is None or not self.duration_ms or self.duration_ms < 60_000:
            return None
        return self.cost_usd / (self.duration_ms / 3_600_000)


def load_meters(meters_dir: Path, cache: dict | None = None) -> dict[str, Meter]:
    """Every ``meters/<phase>.json``.

    With ``cache`` (the caller's, kept between calls) only the files replaced
    since the last call are read. The tap replaces a file atomically, so a new
    one has a new inode, and a directory listing carries inodes for free: the
    dashboard re-read hundreds of files every time any worker's status line moved.
    """
    out: dict[str, Meter] = {}
    try:
        entries = sorted((e.name, e.inode()) for e in os.scandir(meters_dir)
                         if e.name.endswith(".json"))
    except OSError:
        return out
    fresh: dict = {}
    for name, inode in entries:
        got = cache.get(name) if cache is not None else None
        if got is not None and got[0] == inode:
            meter = got[1]
        else:
            meter = _read_meter(meters_dir / name)
        fresh[name] = (inode, meter)
        if meter is not None:
            out[meter.phase] = meter
    if cache is not None:
        cache.clear()
        cache.update(fresh)
    return out


def _read_meter(path: Path) -> Meter | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("phase"), str):
        return None
    num = {k: _as_float(raw.get(k)) for k in (
        "ts", "started_at", "context_tokens", "context_window", "peak_tokens",
        "cost_usd", "duration_ms")}
    return Meter(
        phase=raw["phase"], ts=num["ts"] or 0.0, started_at=num["started_at"],
        context_tokens=num["context_tokens"], context_window=num["context_window"],
        peak_tokens=num["peak_tokens"], cost_usd=num["cost_usd"],
        duration_ms=num["duration_ms"], effort=_as_str(raw.get("effort")),
        seven_day=raw.get("seven_day") if isinstance(raw.get("seven_day"), dict) else None,
        five_hour=raw.get("five_hour") if isinstance(raw.get("five_hour"), dict) else None,
    )


def live_meters(meters: dict[str, Meter], epoch: float | None,
                active: set[str] | frozenset = frozenset()) -> dict[str, Meter]:
    """Meters of phases active in the open run: written since its epoch, or busy now.

    ``meters/`` keeps a file for every phase that ever ran, and a live view that
    globbed them all showed last week's workers' figures after a down/up. A
    worker idling on the owner writes nothing, so a busy phase is kept however
    old its file.
    """
    if epoch is None:
        return dict(meters)
    return {p: m for p, m in meters.items() if m.ts >= epoch or p in active}


@dataclass(frozen=True)
class Limits:
    """The subscription's usage windows, as last reported by any worker."""

    observed_at: float
    week_pct: float
    week_resets_at: float | None = None
    five_pct: float | None = None
    five_resets_at: float | None = None
    #: ``(ts, pct)`` in the current weekly window, oldest first.
    samples: tuple[tuple[float, float], ...] = ()


#: Only the tail of the samples log is read: a pace is a recent slope.
_LIMIT_TAIL_BYTES = 64 * 1024


def load_limits(meters: dict[str, Meter], limits_log: Path, now: float | None = None) -> Limits | None:
    """The freshest weekly figure across workers, with its samples; ``None`` if unknown.

    A figure whose window has already reset says nothing about the new one.
    """
    now = time.time() if now is None else now
    best = None
    for m in meters.values():
        week, five = m.seven_day, m.five_hour
        if not isinstance(week, dict) or _as_float(week.get("pct")) is None:
            continue
        resets = _as_float(week.get("resets_at"))
        if resets is not None and resets <= now:
            continue
        if best is None or m.ts > best[0].ts:
            best = (m, week, five if isinstance(five, dict) else {})
    if best is None:
        return None
    m, week, five = best
    resets = _as_float(week.get("resets_at"))
    samples: list[tuple[float, float]] = []
    try:
        with limits_log.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - _LIMIT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        tail = []
    for line in tail:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        # Rows written before runs existed are ``{ts, pct, resets_at}``; newer
        # ones carry both windows as ``week_pct``/``five_pct``.
        legacy = "week_pct" not in row
        ts = _as_float(row.get("ts"))
        pct = _as_float(row.get("pct" if legacy else "week_pct"))
        row_resets = _as_float(row.get("resets_at" if legacy else "week_resets_at"))
        if ts is not None and pct is not None and row_resets == resets:
            samples.append((ts, pct))
    five_resets = _as_float(five.get("resets_at"))
    five_live = five_resets is None or five_resets > now
    return Limits(
        observed_at=m.ts,
        week_pct=_as_float(week.get("pct")) or 0.0,
        week_resets_at=resets,
        five_pct=_as_float(five.get("pct")) if five_live else None,
        five_resets_at=five_resets if five_live else None,
        samples=tuple(sorted(samples)),
    )


#: The weekly pace is the slope over this much recent history...
PACE_WINDOW_S = 6 * 3600
#: ...and needs at least this span of it to be a slope rather than noise.
PACE_MIN_SPAN_S = 30 * 60


def week_pace(samples, now: float | None = None) -> float | None:
    """Weekly-limit percentage points used per hour, recently; ``None`` if unknowable."""
    now = time.time() if now is None else now
    recent = [(t, p) for t, p in samples if now - t <= PACE_WINDOW_S]
    if len(recent) < 2 or recent[-1][0] - recent[0][0] < PACE_MIN_SPAN_S:
        return None
    (t0, p0), (t1, p1) = recent[0], recent[-1]
    return max(0.0, (p1 - p0) / ((t1 - t0) / 3600))


_UNSET = object()


def limit_outlook(limits: Limits | None, finish_in_s: float | None,
                  now: float | None = None, pace=_UNSET, pace_label: str = "") -> tuple[str, str]:
    """``(text, state)``: where the weekly limit stands, and whether the run beats it.

    The question the owner actually has is not "what percent" but "will this
    campaign finish before the limit stops it, and if not, when does it resume".
    """
    if limits is None:
        return "weekly limit not reported yet", "muted"
    now = time.time() if now is None else now
    pct, resets = limits.week_pct, limits.week_resets_at
    head = f"week {pct:.0f}% · resets {fmt_when(resets, now)}"
    if pct >= 100:
        return f"{head} · limit hit, the run waits for the reset", "bad"
    if pace is _UNSET:
        pace = week_pace(limits.samples, now)
    if pace is None:
        return f"{head} · pace unknown", "info"
    if pace <= 0:
        return f"{head} · flat", "ok"
    full_in = (100 - pct) / pace * 3600
    rate = f"{pace:.1f}%/h{pace_label}"
    if resets is not None and now + full_in >= resets:
        return f"{head} · {rate}, lasts until the reset", "ok"
    if finish_in_s is not None and finish_in_s <= full_in:
        return f"{head} · {rate}, run finishes ~{fmt_coarse(full_in - finish_in_s)} before the limit", "ok"
    if finish_in_s is None:
        return f"{head} · {rate}, limit in ~{fmt_coarse(full_in)}", "warn"
    return f"{head} · {rate}, limit in ~{fmt_coarse(full_in)}, before the run finishes", "bad"


def five_outlook(limits: Limits | None, pace: float | None,
                 now: float | None = None) -> tuple[str, str]:
    """``(text, state)`` for the 5-hour window: where it stands and when it fills."""
    if limits is None or limits.five_pct is None:
        return "5-hour not reported yet", "muted"
    now = time.time() if now is None else now
    pct, resets = limits.five_pct, limits.five_resets_at
    head = f"5-hour {pct:.0f}% · resets {fmt_when(resets, now)}"
    if resets is not None:
        head += f" (in {fmt_coarse(max(0.0, resets - now))})"
    if pct >= 100:
        return f"{head} · limit hit, the run waits for the reset", "bad"
    if pace is None:
        return f"{head} · this run's pace not known yet", "info"
    if pace <= 0:
        return f"{head} · this run 0%/h", "ok"
    full_in = (100 - pct) / pace * 3600
    rate = f"this run {pace:.1f}%/h"
    if resets is not None and now + full_in >= resets:
        return f"{head} · {rate}, lasts until the reset", "ok"
    return f"{head} · {rate}, 100% in ~{fmt_coarse(full_in)}", "warn"


def usage_outlook(limits: Limits | None, run_usage: dict | None,
                  finish_in_s: float | None, now: float | None = None) -> list[tuple[str, str]]:
    """The home page's two usage lines: 5-hour, then weekly, at this run's pace.

    ``run_usage`` is the open run's summary (:func:`swarm_orchestrator.usage.summarize`);
    without one (a state dir from before runs) the weekly line keeps its old
    recent-slope pace and the 5-hour line has none.
    """
    run_usage = run_usage or {}
    five_pace = run_usage.get("five_pct_per_h")
    lines = [five_outlook(limits, five_pace, now)]
    if run_usage:
        text, state = limit_outlook(limits, finish_in_s, now,
                                    pace=run_usage.get("week_pct_per_h"), pace_label=" this run")
    else:
        text, state = limit_outlook(limits, finish_in_s, now)
    lines.append((text, state))
    return lines


def integration_holds(events: list[Event]) -> list[tuple[str, float]]:
    """How long each merge-queue block held the whole queue, longest first.

    A held queue stops *every* integration, not just the conflicted phase, so
    this is the one graph where a single tall bar is a direct throughput loss.
    """
    opened: dict[str, float] = {}
    out: list[tuple[str, float]] = []
    for ev in sorted(events, key=lambda e: e.ts or 0.0):
        if ev.ts is None or not ev.phase:
            continue
        if ev.kind == "integrate-blocked":
            opened[ev.phase] = ev.ts
        elif ev.kind == "resolved":
            start = opened.pop(ev.phase, None)
            if start is not None:
                out.append((ev.phase, max(0.0, ev.ts - start)))
    out.sort(key=lambda item: item[1], reverse=True)
    return out


def completion_density(events: list[Event]) -> list[int]:
    """24 buckets: how many phases completed in each local hour of the day."""
    buckets = [0] * 24
    for ev in events:
        if ev.kind == "done" and ev.ts is not None:
            try:
                buckets[datetime.fromtimestamp(ev.ts).hour] += 1
            except (OSError, OverflowError, ValueError):
                continue
    return buckets


# -- rendering primitives -------------------------------------------------
_BAR_PARTIALS = "▏▎▍▌▋▊▉█"
_SPARK = "▁▂▃▄▅▆▇█"


def bar(value: float, maximum: float, width: int = 24, empty: str = "·") -> str:
    """A unicode block bar with 1/8-cell resolution.

    Eighth-blocks instead of whole cells because the overview bars are short: at
    width 20 a whole-cell bar quantises to 5% steps, which makes "1 of 30 phases
    done" render identically to zero.
    """
    if width <= 0:
        return ""
    if maximum <= 0 or value <= 0:
        return empty * width
    eighths = int(round(min(1.0, value / maximum) * width * 8))
    full, rem = divmod(eighths, 8)
    out = "█" * min(full, width)
    if len(out) < width and rem:
        out += _BAR_PARTIALS[rem - 1]
    return out + empty * (width - len(out))


def spark(values: list[float], width: int | None = None, maximum: float | None = None) -> str:
    """A one-line sparkline. Buckets by *max* when downsampling, so spikes survive."""
    vals = [float(v) for v in values]
    if not vals:
        return ""
    if width and len(vals) > width and width > 0:
        size = len(vals) / width
        vals = [
            max(vals[int(i * size) : max(int((i + 1) * size), int(i * size) + 1)] or [0.0])
            for i in range(width)
        ]
    lo = 0.0 if maximum is not None else min(vals)
    hi = maximum if maximum is not None else max(vals)
    if hi <= lo:
        return _SPARK[0] * len(vals)
    return "".join(
        _SPARK[min(len(_SPARK) - 1, int((v - lo) / (hi - lo) * (len(_SPARK) - 1)))] for v in vals
    )


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(max(0.0, seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def fmt_coarse(seconds: float) -> str:
    """A duration at the resolution a forecast is actually good to.

    Never seconds: ``~1h 50m left`` is a claim about the next two hours, and
    ``~1h49m58s left`` claims to know which second it lands on.
    """
    minutes = int(max(0.0, seconds) // 60)
    if minutes < 1:
        return "< 1m"
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def fmt_clock(ts: float | None) -> str:
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "—"


def fmt_stamp(ts: float | None) -> str:
    if ts is None:
        return "—"
    try:
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


def fmt_ago(ts: float | None, now: float | None = None) -> str:
    if ts is None:
        return "—"
    return f"{fmt_duration((now if now is not None else time.time()) - ts)} ago"


# -- kept processes: what `swarm keep` left running --------------------------
def load_kept(cfg) -> list:
    """Every ``swarm keep`` record, alive or dead, by name; empty on any failure."""
    try:
        return keep_mod.load_all(cfg)
    except Exception:  # noqa: BLE001 - a bad record must not cost the cockpit
        return []


def kept_rows(records, now: float | None = None) -> list[dict]:
    """One plain dict per kept process: what the shells tab and the board show.

    Everything a session starts is reaped when it ends; a kept process is the one
    exception, so it is the one process the owner has to be able to find, read
    ("what is this for?" is the ``why`` its session had to write) and stop. The
    stop command is offered for a dead record too: it is also how the record goes.
    """
    now = time.time() if now is None else now
    out: list[dict] = []
    for rec in records or []:
        try:
            age = rec.age_s(now)
            out.append({
                "name": rec.name,
                "why": rec.why,
                "alive": bool(rec.alive),
                "state": "alive" if rec.alive else "dead",
                "pid": rec.pid,
                "started_at": rec.started_at,
                "age_s": age,
                "age": keep_mod.age_text(age),
                "stale": bool(rec.alive) and age >= keep_mod.STALE_S,
                "by": rec.by,
                "stop": rec.stop_cmd,
                "command": shlex.join(str(a) for a in rec.argv or []),
                "cwd": rec.cwd,
                "log": rec.log,
            })
        except Exception:  # noqa: BLE001 - a malformed record costs its own row
            continue
    return out
