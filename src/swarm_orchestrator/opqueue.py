"""The durable queue of operator hand-offs, and the triage that times them.

``swarm done <phase> operator "<recap>"`` finishes a phase by handing a concrete
piece of work to a *session* instead of to the owner's phone. That decision is
already made — :func:`launch._route_decision` computes it — but a decision that
lives only in a return value is a decision that survives exactly as long as the
process that made it. The rot this module exists to end is precise: the phase
merges, ``state.json`` records it done, the slot frees, and the one sentence
saying what still has to happen is a file nothing ever opens again.

So the hand-off gets a record of its own, one JSON file per phase under
``<state_dir>/operator/``, written from ``swarm done`` **after** the sentinel and
**before** the FIFO poke. That window is the whole point:

* after the sentinel, because the item is re-derivable from it (:func:`reconcile`
  does exactly that), so a crash in between costs nothing;
* before the poke, because the poke is what makes the supervisor merge the branch
  and free the slot. An item written after it would mean a crash in that window
  leaves the work merged and recorded ``done`` with no item at all — the same rot,
  now invisible.

Bounded, so a stuck queue can never deadlock ``finish``
-------------------------------------------------------
Every lease counts an attempt **in the same write that marks the item running**,
so a session that dies on boot cannot retry forever. Past
:data:`MAX_ATTEMPTS` the item goes terminal ``abandoned`` and the owner is
telegrammed once. That telegram is not a nicety: before this feature an
``operator`` finish reached a human by definition, and replacing a
rotting-but-delivered note with a silently dropped one would be a regression, not
a fix. An ``abandoned`` item blocks nothing.

Triage
------
A cheap headless model call decides *when* the session opens (``now`` or
``later``) and what it may be batched with. It runs detached, worker-side, right
after the item is created, and it **fails toward ``later``**: a timeout, prose, a
non-JSON body or an unknown verb all record ``later``, because ``now`` is the
branch that grants a session docker/systemd/``rm -rf`` authority and a parse
failure is the state of least evidence. The fallback lives here rather than in a
consumer for the same reason :func:`launch._ping_decision` decides before it
sends: by the time anything downstream looks at the item, the call is already
billed and the decision already made.

Nothing here raises into a caller. A worker's ``swarm done`` must not fail
because the queue directory is read-only.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import recap as recap_mod
from . import telegram
from .config import Config

QUEUED = "queued"
RUNNING = "running"
#: The session asked the owner a genuine decision and is waiting in its pane for
#: the answer — lease held, session alive. Not terminal: the answer arrives in the
#: same session, which then carries on (``swarm operator-resumed``).
WAITING = "waiting"
DONE = "done"
ABANDONED = "abandoned"
#: Every state an item can be in, in lifecycle order.
STATES = (QUEUED, RUNNING, WAITING, DONE, ABANDONED)
#: States nothing will ever pick up again.
TERMINAL = frozenset({DONE, ABANDONED})
#: States a live session holds the lease in.
LEASED = frozenset({RUNNING, WAITING})

#: Attempts an item gets before it is abandoned and the owner told. Three is a
#: crash loop, not bad luck.
MAX_ATTEMPTS = 3
#: How long a lease is good for. Past it the item is reclaimable even though the
#: run that took it is still the current one — a session that hangs must not pin
#: its item forever.
LEASE_S = 3600.0
#: Cool-off before a released item is eligible again, so a failure that repeats
#: instantly still spends its three attempts over a useful span rather than in a
#: single second.
RETRY_BACKOFF_S = 300.0
#: The lease a session holds while it waits on the owner. The owner answers when
#: they are at a keyboard, which can be a night away, and a lease that expired
#: meanwhile would let the sweep kill a session that is doing exactly what it
#: should. A week, not forever: a session that truly vanished must still let go.
WAIT_LEASE_S = 7 * 24 * 3600.0

#: ``Item.source`` of a job queued by ``swarm operator-add`` rather than left by a
#: phase's ``swarm done ... operator``. ``""`` (every older item) is the latter.
ADDED = "added"
#: Prefix of an ad-hoc job's id when it names no phase — distinct from every
#: ledger phase id, so the job's file, lease and mirror can never collide with one.
ADHOC_PREFIX = "op-"
#: What an item id may contain: the ledger's phase-id alphabet. The id becomes a
#: file name and a git branch (``swarm/op-<id>``), so nothing wider is safe.
ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

#: The closed vocabulary a triage may batch phases under; it is the same list the
#: prompt offers. Anything else is not a group — see :func:`group_of`.
GROUPS = ("build", "deploy", "config", "cleanup", "docs", "verify")
#: Prefix of a singleton group key. A phase-scoped group can only ever contain
#: that phase, which is what an unrecognised group name must degrade to.
SINGLETON_PREFIX = "phase:"

NOW = "now"
LATER = "later"
#: The seam a hermetic test points at a script instead of a model. Deliberately
#: its own variable and NOT ``SWARM_TG_SINK``: that one is set in every hermetic
#: run and means "route telegrams to a file", not "do not execute autonomously".
TRIAGE_CMD_ENV = "SWARM_TRIAGE_CMD"

_TRIAGE_PROMPT = """\
You are scheduling one piece of leftover work from an automated build swarm. A \
phase finished and handed this over for a session to carry out.

Phase: {phase}
Hand-off note: {note}

Answer with ONE JSON object and nothing else:

{{"when": "now" | "later", "why": "<under 15 words>", "group": "<group>"}}

- "now" means this cannot wait for the rest of the run: something is broken, \
unreleased or unsafe until it is done.
- "later" means it is real work that keeps. Prefer "later" when unsure.
- "group" is one of: {groups} — or "" if none of them fit.

No prose, no markdown, no code fence. Just the object.
"""


@dataclass
class Item:
    """One queued operator hand-off, and everything a drainer needs to run it.

    Serialisable both ways, tolerantly: an item written by an older (or newer)
    version loads with unknown keys dropped and missing ones defaulted, because a
    queue that refuses to load is a queue that has lost its work.
    """

    phase: str
    status: str = ""
    note: str = ""
    queued_at: float = 0.0
    attempts: int = 0
    state: str = QUEUED
    #: Epoch before which nothing may lease this. 0 = eligible now.
    run_after: float = 0.0
    #: Epoch a live lease expires. 0 = not leased.
    lease_until: float = 0.0
    #: ``{when, why, group, at, source}`` once triage has run; ``{}`` until then.
    triage: dict = field(default_factory=dict)
    branch: str = ""
    #: Which run took the lease, so one taken by a run that is gone is knowable.
    run_id: str = ""
    last_error: str = ""
    #: LEGACY: set on items an older version ended with ``operator-ask``, which
    #: abandoned the item instead of waiting. Still read — such an item is an
    #: owner question, not a give-up — but never written any more.
    asked: bool = False
    #: The owner decision a session is (or was last) waiting on.
    question: str = ""
    #: When it asked. 0 = it never has.
    asked_at: float = 0.0
    #: The owner's answer, as the session relayed it on resuming.
    answer: str = ""
    #: The session's one-line account of what it did, from ``operator-done``.
    outcome: str = ""
    done_at: float = 0.0
    #: The job's own workspace mirror under worktree isolation ("" = project dir).
    mirror: str = ""
    #: ``""`` = a phase's ``operator`` finish; :data:`ADDED` = ``swarm operator-add``.
    source: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Item":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("phase", "")
        return cls(**kwargs)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.queued_at)

    def ready(self, now: float | None = None) -> bool:
        """Is this leasable right now?"""
        now = time.time() if now is None else now
        return self.state == QUEUED and self.run_after <= now


def group_of(item: Item) -> str:
    """The batch key for ``item`` — a real group, or a per-phase singleton.

    An unrecognised group name is not a hint to be honoured. It is a model naming
    a bucket that does not exist, and treating it as one would batch unrelated
    phases into a single session holding full authority over all of them. So
    anything off :data:`GROUPS` collapses to a key only this phase can be in.
    """
    name = str((item.triage or {}).get("group", "")).strip().lower()
    if name in GROUPS:
        return name
    return f"{SINGLETON_PREFIX}{item.phase}"


# -- paths ----------------------------------------------------------------
def queue_dir(cfg: Config) -> Path:
    return cfg.operator_dir


def item_path(cfg: Config, phase: str) -> Path:
    return queue_dir(cfg) / f"{phase}.json"


def _run_path(cfg: Config) -> Path:
    return queue_dir(cfg) / "run.id"


# -- run identity ---------------------------------------------------------
def run_id(cfg: Config) -> str:
    """The identity stamped on a lease taken now. ``""`` before any run began."""
    try:
        return _run_path(cfg).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def begin_run(cfg: Config) -> str:
    """Stamp a fresh run identity; every lease taken before this is now stale.

    A pid would be cheaper and wrong — pids are recycled, and the check that
    matters (``was this leased by a run that no longer exists?``) would then
    occasionally answer no about a run that died hours ago.
    """
    token = f"{int(time.time())}-{os.urandom(4).hex()}"
    _write_text(_run_path(cfg), token)
    return token


# -- persistence ----------------------------------------------------------
def _write_text(path: Path, text: str) -> bool:
    """tmp + ``os.replace``. Never raises; a lost write is not a failed phase."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _write(cfg: Config, item: Item) -> bool:
    return _write_text(
        item_path(cfg, item.phase), json.dumps(item.to_dict(), indent=2) + "\n"
    )


def _create(cfg: Config, item: Item) -> bool:
    """Write ``item`` only if no item exists for its phase yet.

    ``os.link`` rather than a plain ``O_EXCL`` open: it is equally exclusive (it
    fails ``EEXIST``) and equally atomic, but the file appears at its final name
    already complete, so a crash mid-write cannot leave a torn item behind for the
    reader to trip over. This is what makes a re-run of ``swarm done``
    (which workers do) queue exactly one hand-off.
    """
    dest = item_path(cfg, item.phase)
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.new")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(item.to_dict(), indent=2) + "\n", encoding="utf-8")
        os.link(tmp, dest)
        return True
    except OSError:
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def load(cfg: Config, phase: str) -> Item | None:
    """The item for ``phase``, or ``None``. Never raises."""
    try:
        data = json.loads(item_path(cfg, phase).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    item = Item.from_dict(data)
    return item if item.phase else None


def load_all(cfg: Config) -> list[Item]:
    """Every item, oldest first. Missing directory or junk cfg yields []."""
    try:
        entries = sorted(queue_dir(cfg).glob("*.json"))
    except (OSError, TypeError):
        return []
    out: list[Item] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        item = load(cfg, entry.stem)
        if item is not None:
            out.append(item)
    out.sort(key=lambda i: i.queued_at)
    return out


def pending(cfg: Config) -> list[Item]:
    """Items still owed work — anything not terminal."""
    return [i for i in load_all(cfg) if not i.terminal]


def ready(cfg: Config, now: float | None = None) -> list[Item]:
    """Items leasable right now, oldest first."""
    now = time.time() if now is None else now
    return [i for i in load_all(cfg) if i.ready(now)]


def next_deadline(cfg: Config, now: float | None = None) -> float | None:
    """When the queue next wants looking at, or ``None`` if it never does.

    A backed-off item and an expiring lease are both timers, and a supervisor
    that only wakes on input would sit past either. Folding this into its
    deadline ``min()`` is the whole reason ``run_after`` is a timestamp rather
    than a flag.
    """
    now = time.time() if now is None else now
    stamps = []
    for item in load_all(cfg):
        if item.state == QUEUED and item.run_after > now:
            stamps.append(item.run_after)
        elif item.state == RUNNING and item.lease_until:
            stamps.append(item.lease_until)
    return min(stamps) if stamps else None


# -- the write side -------------------------------------------------------
def add(
    cfg: Config,
    phase: str,
    *,
    status: str,
    note: str,
    branch: str = "",
    source: str = "",
) -> Item | None:
    """Queue one hand-off; ``None`` when nothing was queued.

    ``None`` covers both refusals, and they are not the same thing:
    ``[operator].enabled = false`` means never write the file at all, and an
    existing item means this ``swarm done`` is a re-run of one already recorded.
    """
    if not cfg.operator_enabled:
        return None
    item = Item(
        phase=phase,
        status=status,
        note=" ".join((note or "").split()),
        queued_at=time.time(),
        branch=branch,
        source=source,
    )
    return item if _create(cfg, item) else None


def _adhoc_ids(phase: str | None, now: float):
    """Candidate ids for an ad-hoc job, first free one wins.

    With ``--phase P`` the job is P's when P has none yet, else ``P-op2``,
    ``P-op3`` …; without one it is ``op-<epoch>`` (``-2`` … on a same-second
    clash). Neither shape is a ledger phase id, so a job never takes over a
    phase's queue file, lease or mirror.
    """
    base = phase or f"{ADHOC_PREFIX}{int(now)}"
    yield base
    sep = "-op" if phase else "-"
    for n in range(2, 1000):
        yield f"{base}{sep}{n}"


_EXTRA_JOB_RE = re.compile(r"^(.+)-op\d+$")


def owning_phase(job: str) -> str:
    """The phase a job id belongs to: ``P`` and ``P-op2`` are both P's.

    The inverse of :func:`_adhoc_ids` for the ``--phase`` shape. An
    ``op-<epoch>`` job belongs to no phase and is its own key.
    """
    m = _EXTRA_JOB_RE.match(job)
    return m.group(1) if m and not job.startswith(ADHOC_PREFIX) else job


def add_adhoc(cfg: Config, brief: str, phase: str | None = None) -> Item | None:
    """Queue a job nobody's ``swarm done`` left: ``swarm operator-add``.

    The same queue, lease and sweep as a phase's hand-off — it is only the way in
    that differs. ``None`` when the operator is off, the brief is empty or the id
    is not a safe name.
    """
    note = " ".join((brief or "").split())
    if not cfg.operator_enabled or not note:
        return None
    if phase is not None and not ID_RE.match(phase):
        return None
    now = time.time()
    for candidate in _adhoc_ids(phase, now):
        if load(cfg, candidate) is not None:
            continue
        item = Item(
            phase=candidate,
            status="operator",
            note=note,
            queued_at=now,
            source=ADDED,
        )
        if _create(cfg, item):
            return item
    return None


def lease(cfg: Config, phase: str, now: float | None = None) -> Item | None:
    """Take ``phase``'s item for execution: ``queued`` -> ``running``.

    The attempt is counted in the *same* write that marks it running. Counting it
    afterwards would mean a session that dies between the two writes is never
    charged for the attempt, which is precisely the crash loop the cap exists to
    stop. Returns ``None`` when there is nothing leasable — including when the cap
    has just abandoned it.
    """
    item = load(cfg, phase)
    now = time.time() if now is None else now
    if item is None or not item.ready(now):
        return None
    if item.attempts >= MAX_ATTEMPTS:
        _abandon(cfg, item, "attempt cap reached before this attempt started")
        return None
    item.attempts += 1
    item.state = RUNNING
    item.run_id = run_id(cfg)
    item.lease_until = now + LEASE_S
    _write(cfg, item)
    return item


def release(
    cfg: Config, phase: str, error: str = "", now: float | None = None
) -> Item | None:
    """Hand a leased item back after an attempt that did not finish it.

    At the cap this abandons instead of requeueing, so the owner is told at the
    moment the queue gives up rather than whenever some later lease happens to
    notice.
    """
    item = load(cfg, phase)
    if item is None or item.terminal:
        return None
    now = time.time() if now is None else now
    item.last_error = " ".join((error or "").split())
    if item.attempts >= MAX_ATTEMPTS:
        return _abandon(cfg, item, item.last_error or "attempt cap reached")
    item.state = QUEUED
    item.run_id = ""
    item.lease_until = 0.0
    item.run_after = now + RETRY_BACKOFF_S
    _write(cfg, item)
    return item


def complete(cfg: Config, phase: str, outcome: str = "") -> Item | None:
    """Mark ``phase``'s hand-off carried out, with the session's own account."""
    item = load(cfg, phase)
    if item is None or item.terminal:
        return None
    item.state = DONE
    item.run_id = ""
    item.lease_until = 0.0
    item.outcome = " ".join((outcome or "").split())
    item.done_at = time.time()
    _write(cfg, item)
    return item


def abandon(cfg: Config, phase: str, reason: str = "") -> Item | None:
    """Give up on ``phase``'s hand-off and tell the owner."""
    item = load(cfg, phase)
    if item is None or item.terminal:
        return None
    return _abandon(cfg, item, reason)


def wait_on_owner(
    cfg: Config, phase: str, question: str, now: float | None = None
) -> tuple[Item | None, bool]:
    """The session hit a genuine decision: park the item on the owner, alive.

    Returns ``(item, fresh)``; ``fresh`` is False when this exact question is
    already the one being waited on, so a re-run cannot ring the owner twice.

    This used to be terminal — the item was abandoned and the session told to
    stop — which threw away a session that had already done the groundwork and
    left the owner's answer with nobody to act on it. Now the session asks in its
    own pane, like a worker does, and the lease is stretched to
    :data:`WAIT_LEASE_S` so neither the sweep nor a restart-free night reclaims
    it while the owner sleeps.
    """
    item = load(cfg, phase)
    if item is None or item.state not in LEASED:
        return None, False
    now = time.time() if now is None else now
    text = " ".join((question or "").split())
    fresh = not (item.state == WAITING and item.question == text)
    item.state = WAITING
    item.question = text
    if fresh:
        item.asked_at = now
    item.lease_until = now + WAIT_LEASE_S
    _write(cfg, item)
    return item, fresh


def resume(
    cfg: Config, phase: str, answer: str = "", now: float | None = None
) -> Item | None:
    """The owner answered: the session carries on under an ordinary lease."""
    item = load(cfg, phase)
    if item is None or item.state != WAITING:
        return None
    now = time.time() if now is None else now
    item.state = RUNNING
    item.answer = " ".join((answer or "").split())
    item.lease_until = now + LEASE_S
    _write(cfg, item)
    return item


def set_mirror(cfg: Config, phase: str, mirror: str) -> Item | None:
    """Record the workspace mirror a job's session runs in."""
    item = load(cfg, phase)
    if item is None:
        return None
    item.mirror = mirror
    _write(cfg, item)
    return item


def _abandon(cfg: Config, item: Item, reason: str) -> Item:
    """The terminal transition, and the one telegram that goes with it.

    Fired on the transition only, so the owner hears about a given hand-off
    exactly once however many times something asks the queue to give up on it.
    """
    item.state = ABANDONED
    item.run_id = ""
    item.lease_until = 0.0
    item.last_error = " ".join((reason or "").split())
    _write(cfg, item)
    tail = f" — {item.note}" if item.note else ""
    telegram.notify(
        cfg.telegram_notify,
        f"swarm: {item.phase} operator hand-off ABANDONED after"
        f" {item.attempts} attempt(s){tail}",
        kind="operator-abandoned",
        phase=item.phase,
        source="opqueue.abandon",
        state_dir=cfg.state_dir,
    )
    return item


def set_triage(
    cfg: Config, phase: str, *, when: str, why: str, group: str, source: str
) -> Item | None:
    """Record a triage decision on an existing item."""
    item = load(cfg, phase)
    if item is None:
        return None
    item.triage = {
        "when": when,
        "why": " ".join((why or "").split()),
        "group": group,
        "at": time.time(),
        "source": source,
    }
    _write(cfg, item)
    return item


# -- recovery -------------------------------------------------------------
def reconcile(cfg: Config, log=None) -> list[str]:
    """Rebuild the queue from what is durable. Returns what changed, for a log.

    Two repairs, and between them the write in ``swarm done`` stops being a
    durability point and becomes a cache:

    * an ``operator`` **sentinel with no item** gets one, read back out of the
      sentinel with :func:`recap.sentinel` — so the crash window between the two
      writes costs nothing;
    * an item left ``running`` by a run that is gone goes back to ``queued`` with
      its attempt count intact (``waiting`` too — its session died with the
      run). Rotating the run id first is what makes "gone"
      decidable: this call happens at ``swarm up``, and a ``swarm up`` IS a new
      run, so every lease predating it belongs to a run that is over.
    """
    if not cfg.operator_enabled:
        return []
    current = begin_run(cfg)
    changed: list[str] = []
    for item in load_all(cfg):
        # A waiting session died with the run too: its question is kept on the
        # item and handed to the next session, which re-asks it if it still
        # stands.
        if item.state in LEASED and item.run_id != current:
            item.state = QUEUED
            item.run_id = ""
            item.lease_until = 0.0
            _write(cfg, item)
            changed.append(f"requeued {item.phase}")
    for phase in _orphan_sentinels(cfg):
        status, note = recap_mod.sentinel(cfg, phase)
        item = Item(
            phase=phase,
            status=status or "operator",
            note=" ".join((note or "").split()),
            queued_at=_sentinel_mtime(cfg, phase),
            branch=f"swarm/{phase}" if cfg.git_isolation == "worktree" else "",
        )
        if _create(cfg, item):
            changed.append(f"rebuilt {phase}")
    if log is not None and changed:
        log.line(f"OPQUEUE-RECONCILE {' '.join(changed)}")
    return changed


def _orphan_sentinels(cfg: Config) -> list[str]:
    """Phases with an ``operator`` sentinel and no item at all."""
    try:
        entries = sorted(cfg.done_dir.glob("*.operator"))
    except (OSError, TypeError):
        return []
    return [e.stem for e in entries if load(cfg, e.stem) is None]


def _sentinel_mtime(cfg: Config, phase: str) -> float:
    """When the phase actually finished — the only ``queued_at`` we can recover."""
    try:
        return (cfg.done_dir / f"{phase}.operator").stat().st_mtime
    except OSError:
        return time.time()


# -- triage ---------------------------------------------------------------
def _decide(raw: str | None) -> tuple[str, str, str]:
    """Parse a triage answer into ``(when, why, group)``; ``later`` on anything odd.

    Every failure — no answer, prose, a fenced block, an unknown verb — lands on
    ``later``, because ``now`` is the branch that opens a session with the owner's
    full authority and a body we could not parse is the state of *least* evidence
    about whether that is warranted.
    """
    if not raw:
        return LATER, "no answer from the triage model", ""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return LATER, "triage answered in prose, not JSON", ""
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return LATER, "triage answer was not valid JSON", ""
    if not isinstance(data, dict):
        return LATER, "triage answer was not an object", ""
    when = str(data.get("when", "")).strip().lower()
    why = str(data.get("why", ""))
    group = str(data.get("group", "")).strip().lower()
    if when == NOW:
        return NOW, why, group
    if when == LATER:
        return LATER, why, group
    return LATER, f"triage said {when!r}, which is not a schedule", group


def triage(cfg: Config, phase: str) -> Item | None:
    """Decide when ``phase``'s session opens, and record it. Never raises.

    Runs detached from ``swarm done`` (see :func:`launch._detach_triage`) for the
    same reason the recap does: a model round-trip on the worker's clock is a
    timeout the worker will read as a failed finish.
    """
    item = load(cfg, phase)
    if item is None:
        return None
    prompt = _TRIAGE_PROMPT.format(
        phase=phase, note=item.note or "(none)", groups=", ".join(GROUPS)
    )
    answer, reason = recap_mod.ask(
        cfg, prompt, seam=TRIAGE_CMD_ENV, model=cfg.operator_triage_model
    )
    when, why, group = _decide(answer)
    return set_triage(
        cfg,
        phase,
        when=when,
        why=why or reason or "",
        group=group,
        source="model" if answer else (reason or "unavailable"),
    )
