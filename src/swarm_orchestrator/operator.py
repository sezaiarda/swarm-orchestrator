"""The operator session: the thing that finally drains the hand-off queue.

``swarm done <phase> operator "<recap>"`` writes a sentinel, queues a bounded
item (:mod:`opqueue`) and telegrams nobody. Up to here the hand-off is recorded
and nothing acts on it — which is the same rot the queue was built to end, just
one file further along. This module is the consumer: it opens **one** ``claude``
session, in the ``operator`` window ``session.setup`` created, pointed at
``prompts/operator.md`` and briefed with the recap, holding the owner's own
authority on the host.

Where it runs
-------------
Under ``isolation = "none"`` its cwd is the project itself, like every worker's.
Under ``isolation = "worktree"`` it gets a full-workspace mirror of its own,
built by the same :func:`gitq.worktree_add` a phase uses but named
``op-<id>`` (:func:`mirror_name`, branch ``swarm/op-<id>``) so it can never be
mistaken for — or collide with — a phase's. It commits there like a worker, and
``operator-done`` sends the branch through the ordinary integration queue: the
same merge, the same keyed/union auto-resolve, the same resolver on a real
conflict, the same owed push when the push fails, and the mirror is dropped once
it merges. The earlier rule — cut ``operator/<phase>`` in the project dir and
push only if asked — left an operator's code change on a branch nothing ever
merged.

Host actions (docker, systemd, the live host) do not care which checkout they are
run from. Untracked host files (secrets, an ``.env``) are not in a mirror;
``SWARM_PROJECT`` names the canonical tree they live in.

Liveness is a LEASE, never a pane probe
---------------------------------------
The obvious ``operator_alive()`` — ask tmux whether the pane is dead, the way
:meth:`master.Master.is_alive` does — is wrong here and wrong in the worst
direction. ``session.setup`` leaves that window holding ``sleep infinity``, which
is a perfectly live pane, so the probe answers True before any session has ever
run and the operator never launches: no session, no telegram, no log line anyone
would read. So exclusion is a compare-and-set lease in ``state.json``
(:meth:`state.State.claim_operator`), taken under the same exclusive ``flock``
every other claim uses and therefore correct across processes.
"""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

from . import gitq
from . import launch as launch_mod
from . import opqueue
from . import resolver
from . import session as session_mod
from . import state as state_mod
from . import tmux
from .config import Config
from .logutil import Log

#: Env overrides the supervisor itself may have been started with; a ``swarm``
#: run inside the session must resolve the same config, so they ride along on top
#: of :func:`launch.session_env` (which forwards only what every worker needs).
_FORWARD_ENV = (
    "SWARM_GIT_ISOLATION",
    "SWARM_GIT_MAIN",
    "SWARM_GIT_REPOS",
    "SWARM_READY_MARKER",
)
#: The job id a session is carrying out, so a `swarm operator-*` call inside it
#: never has to guess which item it means.
JOB_ENV = "SWARM_OPERATOR_JOB"
#: The status an operator mirror rides the integration queue under. Not a
#: `swarm done` status: the supervisor keys on it to land the branch WITHOUT
#: recording a phase done — the job is not a ledger phase.
INTEG_STATUS = "operator-job"


def mirror_name(job: str) -> str:
    """The mirror (and ``swarm/<name>`` branch) an operator job works in.

    ``op-`` prefixed so it is never a phase's own mirror name; an ad-hoc id
    already carries the prefix and is used as-is rather than doubled.
    """
    return job if job.startswith(opqueue.ADHOC_PREFIX) else f"{opqueue.ADHOC_PREFIX}{job}"


def _operator_env(cfg: Config, job: str, mirror: Path | None = None) -> dict[str, str]:
    env = launch_mod.session_env(cfg, mirror, tmp=mirror_name(job), session=f"operator:{job}")
    for key in _FORWARD_ENV:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env[JOB_ENV] = job
    return env


# -- the session ----------------------------------------------------------
def operator_command(cfg: Config, job: str = "", cwd: Path | None = None) -> str:
    """Shell command that runs the session in ``cwd`` (default: the project).

    Built by the worker's own builder (:func:`launch._worker_shell`) with the
    operator's model swapped in, so it gets exactly what a worker gets —
    in-process teammates, the meters tap, the effort level — and nothing less:
    it is a full, unrestrained session.
    """
    if cfg.operator_cmd:
        return cfg.operator_cmd
    model = f" --model {shlex.quote(cfg.operator_model)}" if cfg.operator_model else ""
    name = f" -n {shlex.quote(f'operator:{job}')}" if job else ""
    where = cwd or cfg.project_dir
    return launch_mod._worker_shell(cfg, job or "operator", where, f"claude{model}{name}")


def prepare_mirror(cfg: Config, item: opqueue.Item, log: Log) -> Path | None:
    """The job's own workspace mirror, or ``None`` when it runs in the project.

    Reused when a previous attempt left one: that mirror may hold commits the
    job already made, and :func:`gitq.worktree_add` starts from a clean slate by
    deleting any leftover — the one thing a retry must not do. Raises
    :class:`gitq.GitError` when a fresh one cannot be built.
    """
    if cfg.git_isolation != "worktree":
        return None
    name = mirror_name(item.phase)
    path = cfg.wt_dir / name
    if path.is_dir() and gitq.branch_exists(cfg.project_dir, f"swarm/{name}"):
        log.line(f"OPERATOR-MIRROR-REUSE {item.phase} {path}")
    else:
        path = gitq.worktree_add(cfg, name, log)
    opqueue.set_mirror(cfg, item.phase, name)
    launch_mod.pretrust_dir(path, log)
    return path


def spawn(
    cfg: Config, item: opqueue.Item, pane: str | None, log: Log, cwd: Path | None = None
) -> bool:
    """Open the session in ``pane``; True on success (no-op on the bare driver)."""
    if cfg.driver != "tmux":
        log.line(f"OPERATOR-SPAWN skipped driver={cfg.driver} {item.phase}")
        return False
    if pane is None:
        log.line(f"OPERATOR-SPAWN-FAIL {item.phase} no-pane")
        return False
    tmux.respawn_pane(
        pane,
        operator_command(cfg, item.phase, cwd),
        env=_operator_env(cfg, item.phase, cwd),
    )
    if not cfg.operator_cmd and not _deliver(cfg, pane, item, log, cwd):
        return False
    log.line(f"OPERATOR-SPAWN {item.phase} pane={pane}")
    return True


def brief(cfg: Config, item: opqueue.Item, cwd: Path | None = None) -> str:
    """The one line a fresh session is handed: prompt, job, brief, exits.

    ONE line, no embedded newlines: tmux ``send-keys`` submits on every newline.
    The note rides along in it because it IS the brief, already
    whitespace-collapsed by :func:`opqueue.add`. A question an earlier attempt
    asked is handed on too — that session died, and its question may still stand.
    """
    prompt_file = resolver.prompt_path("operator.md")
    job = item.phase
    origin = (
        "an ad-hoc job queued with `swarm operator-add`"
        if item.source == opqueue.ADDED
        else f"the hand-off left by phase {job}"
    )
    where = (
        f"Your cwd {cwd} is your own full-workspace mirror (branch swarm/{mirror_name(job)});"
        f" commit your changes there — the swarm merges them when you finish."
        f" Untracked host files (secrets, .env) are in the canonical project {cfg.project_dir}."
        if cwd is not None
        else f"Your cwd is the project itself, {cfg.project_dir}."
    )
    note = item.note if item.note.endswith((".", "!", "?")) else f"{item.note}."
    earlier = ""
    if item.question:
        answered = f" The owner answered: {item.answer}" if item.answer else " It was not answered."
        earlier = f" An earlier attempt asked the owner: {item.question}.{answered}"
    if item.last_error:
        earlier += f" The last attempt ended: {item.last_error.rstrip('.')}."
    return (
        f"Read {prompt_file} and follow it exactly. You are operator job {job}:"
        f" {origin}, in the project at {cfg.project_dir}, on the owner's behalf."
        f" {where} The brief, in full: {note}{earlier} When the job is"
        f' finished run `swarm operator-done {job} "<one-line outcome>"`, adding'
        f" `--attention` only if the owner must act, something is still owed or a"
        f" check came back bad (see the prompt). If what the job waits on has not"
        f' happened yet, run `swarm operator-done {job} "<why>" --not-before <when>`'
        f" instead. If you hit a genuine decision, run"
        f' `swarm waiting {job} "<the question, one line>"`, ask it with'
        f' AskUserQuestion, then `swarm resumed {job} "<answer>"`.'
        " Anything for the owner is plain English about the system, not the code."
    )


def _deliver(
    cfg: Config, pane: str, item: opqueue.Item, log: Log, cwd: Path | None = None
) -> bool:
    """Point a freshly launched session at its prompt and its brief."""
    prompt_file = resolver.prompt_path("operator.md")
    if not prompt_file.is_file():
        log.line(f"OPERATOR-PROMPT-MISSING {prompt_file}")
        return False
    if not launch_mod.await_ready(cfg, pane, log):
        log.line(f"OPERATOR-READY-TIMEOUT {item.phase}")
        return False
    # The brief goes to a file and the pane gets one short line pointing at it.
    # Typed straight in, a long brief (1-2 KB is normal for a hand-off) is folded
    # by Claude Code into "[Pasted text #1]" and the Enter never lands, so the
    # submit check fails every time: phases were
    # abandoned after three attempts without a session ever seeing their brief.
    brief_file = opqueue.item_path(cfg, item.phase).with_suffix(".brief.md")
    try:
        brief_file.parent.mkdir(parents=True, exist_ok=True)
        brief_file.write_text(brief(cfg, item, cwd) + "\n", encoding="utf-8")
    except OSError as exc:
        log.line(f"OPERATOR-BRIEF-WRITE-FAILED {item.phase} {exc}")
        return False
    line = (
        f"You are swarm operator job {item.phase}. Read your full brief in"
        f" {brief_file} first, then do exactly what it says."
    )
    if not tmux.send_submit(pane, line):
        log.line(f"OPERATOR-SUBMIT-LOST {item.phase}")
        return False
    return True


# -- the lease ------------------------------------------------------------
def _drop_lease(cfg: Config) -> None:
    with state_mod.transaction(cfg) as st:
        st.release_operator()


def dispatch(cfg: Config, phase: str, log: Log, *, reason: str = "") -> bool:
    """Open a session for ``phase``. False means nothing was dispatched.

    Claim, then spawn: the compare-and-set runs inside one
    :func:`state.transaction` and the pane work happens *outside* it, so a
    ``claude`` boot that takes half a minute never holds the state lock every
    other command needs. Both callers — the ``operator`` FIFO verb and the
    queue sweep — take this same claim, which is what makes a double dispatch
    impossible rather than merely unlikely.
    """
    if not cfg.operator_enabled:
        log.line(f"OPERATOR-SKIP {phase} disabled")
        return False
    if state_mod.read(cfg).drain:
        # Winding down to a stop: the job stays queued for the next `swarm up`.
        log.line(f"OPERATOR-HELD {phase} draining")
        return False
    now = time.time()
    with state_mod.transaction(cfg) as st:
        took = st.claim_operator(phase, now + opqueue.LEASE_S, now)
        held = None if took else st.operator_phase
        pane = st.operator_pane
    if held is not None:
        log.line(f"OPERATOR-BUSY {phase} held-by={held}")
        return False
    item = opqueue.lease(cfg, phase, now)
    if item is None:
        # Nothing leasable: no item, already terminal, backed off, or just
        # abandoned at the cap. Give the lease straight back — holding it would
        # keep the next real hand-off out for an hour over a no-op.
        _drop_lease(cfg)
        log.line(f"OPERATOR-NOTHING {phase}")
        return False
    try:
        cwd = prepare_mirror(cfg, item, log)
    except gitq.GitError as exc:
        opqueue.release(cfg, phase, f"its workspace mirror could not be built: {exc}")
        _drop_lease(cfg)
        log.line(f"OPERATOR-MIRROR-FAIL {phase} {exc}")
        return False
    if cfg.driver == "tmux" and not spawn(cfg, item, pane, log, cwd):
        opqueue.release(cfg, phase, "the operator session would not start")
        _drop_lease(cfg)
        return False
    log.line(f"OPERATOR-DISPATCH {phase} attempt={item.attempts} {reason}".rstrip())
    return True


def hold_lease(cfg: Config, job: str, until: float) -> bool:
    """Move the state lease's expiry for ``job``'s live session to ``until``.

    The item and the state lease must expire together (see :func:`_reclaim`), so
    whatever stretches one — waiting on the owner — stretches the other. False
    when the lease is not ``job``'s: a CLI run after the session was reclaimed
    must not resurrect it.
    """
    with state_mod.transaction(cfg) as st:
        if st.operator_phase != job:
            return False
        st.operator_lease_until = until
    return True


def integration_for(cfg: Config, job: str) -> str | None:
    """The mirror a finished job's commits must be merged from, if any.

    ``None`` under ``isolation = none`` (the job worked in the project itself)
    and when the mirror is already gone — nothing left to land.
    """
    if cfg.git_isolation != "worktree":
        return None
    name = job_mirror(cfg, job)
    if not gitq.branch_exists(cfg.project_dir, f"swarm/{name}"):
        return None
    return name


def job_mirror(cfg: Config, job: str) -> str:
    """The mirror name ``job`` works (or worked) in: its record's, else the default."""
    item = opqueue.load(cfg, job)
    return item.mirror if item is not None and item.mirror else mirror_name(job)


#: What :func:`mirror_plan` asks ``swarm up``'s reconcile to do with a mirror.
KEEP = "keep"
INTEGRATE = "integrate"


def mirror_plan(cfg: Config) -> dict[str, str]:
    """``{mirror: KEEP | INTEGRATE}`` for every operator mirror worth saving.

    ``gitq.reconcile`` discards any ``swarm/*`` branch with no completion
    sentinel as an interrupted phase — which, for an operator mirror, would
    delete commits a job already made. A live job keeps its mirror (its next
    attempt reuses it); a finished one whose merge never happened is integrated.
    An abandoned job's mirror is left to the default: discarded — the owner was
    told the job was given up.
    """
    plan: dict[str, str] = {}
    for item in opqueue.load_all(cfg):
        if not item.mirror:
            continue
        if item.state == opqueue.DONE:
            plan[item.mirror] = INTEGRATE
        elif not item.terminal:
            plan[item.mirror] = KEEP
    return plan


def release(cfg: Config, log: Log, job: str | None = None) -> str | None:
    """End the session in the operator window: drop the lease, return the pane
    to idle.

    Returns the phase the lease held. Idempotent, and safe to call on a run that
    never opened one — which is what lets the supervisor's ``finally`` call it
    unconditionally rather than deciding whether it has to. With ``job``, only
    that job's session is ended: a parked job finishing in its own window must
    never end the next job, which has the operator window by then.
    """
    with state_mod.transaction(cfg) as st:
        if job is not None and st.operator_phase != job:
            return None
        pane = st.operator_pane
        phase = st.release_operator()
    if phase is None:
        return None
    if cfg.driver == "tmux" and pane:
        # Respawn, not kill: the window belongs to `session.setup`, so it goes
        # back to holding `sleep infinity` with its pane id still valid.
        tmux.respawn_pane(pane, "exec sleep infinity")
    # The job is over (done, lease expired, or the run stopping): so is every
    # process its session started, a detached one included (`swarm keep` aside).
    session_mod.reap_session(cfg, "operator", phase, log)
    log.line(f"OPERATOR-RELEASE {phase}")
    return phase


# -- what the run still owes ----------------------------------------------
def blocking(cfg: Config) -> list[str]:
    """Phases whose hand-off must stop the run from finishing.

    Checked *beside* ``State.pending()`` and deliberately not inside it:
    ``pending`` is a pure ``State`` method that cannot read the filesystem queue,
    and folding this in would make a non-empty queue both the reason to open a
    session and the reason the launch path returns early — a deadlock, not a
    guard.

    Bounded by the queue's own attempt cap, so an item that has burned it (and
    therefore already reached the owner, or is one lease from doing so) stops
    blocking. A blocker with no bound is just a hang with a better name.

    A job waiting on the owner blocks too: its session is alive, in the operator
    window or parked in its own, and finishing the run would kill it with the
    question unanswered.
    """
    return sorted(
        i.phase
        for i in opqueue.pending(cfg)
        if i.state in opqueue.LEASED or i.attempts < opqueue.MAX_ATTEMPTS
    )


def outstanding(cfg: Config) -> list[str]:
    """Every phase still owed a hand-off, bounded or not — for the finish report."""
    return sorted(i.phase for i in opqueue.pending(cfg))


# -- the drains -----------------------------------------------------------
def deferred(item: opqueue.Item) -> bool:
    """Did triage ask for this to wait rather than open the moment work lands?"""
    return str((item.triage or {}).get("when", "")) == opqueue.LATER


def on_finished(cfg: Config, phase: str, log: Log) -> bool:
    """The hook ``supervisor._advance_done`` calls once ``phase`` is merged.

    Here rather than in ``launch.done`` for two reasons: ``swarm done`` has to
    return to its worker immediately, and a session opened before the merge would
    act on a phantom — the work it was briefed about would not be in ``main`` yet.

    A ``later`` triage is the one thing that holds it back; that item drains from
    :func:`sweep` once a worker slot is free and no phase wants it instead, or
    once it has waited ``[operator].later_wait_s``.
    """
    item = opqueue.load(cfg, phase)
    if item is None or item.terminal:
        return False
    if deferred(item):
        log.line(f"OPERATOR-DEFERRED {phase}")
        return False
    return dispatch(cfg, phase, log, reason=f"{phase} merged")


def on_poke(cfg: Config, phase: str, log: Log) -> bool:
    """Open ``phase``'s session on request: a ``now`` triage, or ``swarm operator``.

    Now, but never before the phase's work is on main — the same rule
    :func:`on_finished` and :func:`sweep` keep. A triage can answer while its
    phase still sits in the merge queue; the job is then held, and
    :func:`on_finished` opens it the moment the merge lands.
    """
    if _in_flight(state_mod.read(cfg), phase):
        log.line(f"OPERATOR-HELD {phase} not merged yet")
        return False
    return dispatch(cfg, phase, log, reason="poked")


def sweep(
    cfg: Config, log: Log, now: float | None = None, *, room=None
) -> bool:
    """Reclaim dead leases, then open a session for the oldest due hand-off.

    Runs on EVERY supervisor wake, alongside the park deadlines — not from
    ``_watchdog_tick``. That returns early while ``idle < watchdog_s`` and
    ``_handle`` refreshes the idle clock on every FIFO line, so a swarm that is
    moving never reaches the quiet point and the queue would drain only once the
    run was already over.

    Oldest first, one at a time, and never a phase's hand-off while that phase
    is still building or merging: the job is briefed about work that must already
    be in main, and under worktree isolation its mirror branches from main.

    A job triaged ``later`` keeps, so it opens only once ``room()`` says a worker
    slot is free and no launchable phase is waiting for it (the supervisor's own
    verdict, which only it can give). Other phases may still be building or
    merging: the session runs in its own window and takes no slot, so waiting
    for the whole run to go quiet only piled the queue up.

    Nor does it wait past ``[operator].later_wait_s``: with ready rows keeping
    every slot busy, ``room()`` stays False for as long as the backlog lasts,
    and ``later`` came to mean never. The oldest ``later`` job that
    has waited that long opens without ``room``. Without either, it is not
    opened here at all.
    """
    if not cfg.operator_enabled:
        return False
    now = time.time() if now is None else now
    _reclaim(cfg, log, now)
    st = state_mod.read(cfg)
    due = [i for i in opqueue.ready(cfg, now) if not _in_flight(st, i.phase)]
    reason = "queue swept"
    pick = next((i for i in due if not deferred(i)), None)
    if pick is None:
        pick = next((i for i in due if _overdue(cfg, i, now)), None)
        if pick is not None:
            reason = f"queue swept, later job waited {pick.age_s(now) / 3600:.1f}h"
    if pick is None and due and room is not None and room():
        pick = due[0]
    if pick is None:
        return False
    return dispatch(cfg, pick.phase, log, reason=reason)


def _overdue(cfg: Config, item: opqueue.Item, now: float) -> bool:
    """Has a ``later`` job waited out ``[operator].later_wait_s`` (0 = never)?"""
    wait = cfg.operator_later_wait_s
    return wait > 0 and item.age_s(now) >= wait


def _in_flight(st: state_mod.State, phase: str) -> bool:
    """Is ``phase`` still building or waiting to merge?"""
    return (
        phase in st.integ_queue
        or st.integ_blocked == phase
        or any(s.busy and s.phase == phase for s in st.slots)
    )


def _reclaim(cfg: Config, log: Log, now: float) -> None:
    """Hand back a lease whose session outlived it, on both sides of the pair.

    The item lease and the state lease are taken from the same clock with the
    same span, so they expire together; releasing only one of them would leave
    either an item nothing can pick up or a window nothing can open.

    A session waiting on the owner holds :data:`opqueue.WAIT_LEASE_S` on both
    sides (:func:`hold_lease`), so it is only reclaimed if it has waited out that
    too — never merely because the owner has not reached a keyboard yet. A
    parked job holds only the item side; reclaiming it also drops it from the
    parked set, or the run could never finish.
    """
    with state_mod.transaction(cfg) as st:
        stale = st.operator_phase if (
            st.operator_phase is not None and not st.operator_busy(now)
        ) else None
    if stale is not None:
        log.line(f"OPERATOR-LEASE-EXPIRED {stale}")
        release(cfg, log)
    for item in opqueue.load_all(cfg):
        if item.state in opqueue.LEASED and 0 < item.lease_until <= now:
            opqueue.release(cfg, item.phase, "the operator session outlived its lease")
            key = state_mod.waiter_key(state_mod.OPERATOR, item.phase)
            with state_mod.transaction(cfg) as st:
                st.clear_pending(key)
                win = st.windows.pop(state_mod.wait_window(key), None)
            if win and cfg.driver == "tmux":
                tmux.kill_window(win)
            log.line(f"OPERATOR-REQUEUED {item.phase}")
