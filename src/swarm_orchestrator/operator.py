"""The operator session: the thing that finally drains the hand-off queue.

``swarm done <phase> operator "<recap>"`` writes a sentinel, queues a bounded
item (:mod:`opqueue`) and telegrams nobody. Up to here the hand-off is recorded
and nothing acts on it — which is the same rot the queue was built to end, just
one file further along. This module is the consumer: it opens **one** ``claude``
session, in the ``operator`` window ``session.setup`` created, pointed at
``prompts/operator.md`` and briefed with the recap, holding the owner's own
authority on the host.

Host-direct, not a worktree
---------------------------
Its cwd is the project itself. Every recorded hand-off has been a docker /
systemd / filesystem action, none of which a mirror can perform. If a session
decides code must change it may cut a branch, but **never** under ``swarm/*``:
:func:`gitq.reconcile` discards any ``swarm/*`` branch with no ``done/``
sentinel, so an operator branch there is indistinguishable from an interrupted
phase and is deleted on the next ``swarm up``.

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

from . import launch as launch_mod
from . import opqueue
from . import resolver
from . import state as state_mod
from . import tmux
from .config import Config
from .logutil import Log

_FORWARD_ENV = (
    "SWARM_STATE_DIR",
    "SWARM_SLUG",
    "SWARM_DRIVER",
    "SWARM_BIN",
    "SWARM_TG_SINK",
    "SWARM_GIT_ISOLATION",
    "SWARM_GIT_MAIN",
    "SWARM_GIT_REPOS",
    "SWARM_READY_MARKER",
)


def _operator_env(cfg: Config) -> dict[str, str]:
    env = {"SWARM_STATE_DIR": str(cfg.state_dir)}
    for key in _FORWARD_ENV:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


# -- the session ----------------------------------------------------------
def operator_command(cfg: Config) -> str:
    """Shell command that runs the session, host-direct in the project dir."""
    if cfg.operator_cmd:
        return cfg.operator_cmd
    model = f" -m {cfg.operator_model}" if cfg.operator_model else ""
    return f"cd {shlex.quote(str(cfg.project_dir))} && exec claude{model}"


def spawn(cfg: Config, item: opqueue.Item, pane: str | None, log: Log) -> bool:
    """Open the session in ``pane``; True on success (no-op on the bare driver)."""
    if cfg.driver != "tmux":
        log.line(f"OPERATOR-SPAWN skipped driver={cfg.driver} {item.phase}")
        return False
    if pane is None:
        log.line(f"OPERATOR-SPAWN-FAIL {item.phase} no-pane")
        return False
    tmux.respawn_pane(pane, operator_command(cfg), env=_operator_env(cfg))
    if not cfg.operator_cmd and not _deliver(cfg, pane, item, log):
        return False
    log.line(f"OPERATOR-SPAWN {item.phase} pane={pane}")
    return True


def _deliver(cfg: Config, pane: str, item: opqueue.Item, log: Log) -> bool:
    """Point a freshly launched session at its prompt and its brief.

    ONE line, no embedded newlines: tmux ``send-keys`` submits on every newline.
    The recap rides along in it because the recap IS the brief — nothing else is
    handed over — and it is already whitespace-collapsed by :func:`opqueue.add`.
    """
    prompt_file = resolver.prompt_path("operator.md")
    if not prompt_file.is_file():
        log.line(f"OPERATOR-PROMPT-MISSING {prompt_file}")
        return False
    if not launch_mod.await_ready(cfg, pane, log):
        log.line(f"OPERATOR-READY-TIMEOUT {item.phase}")
        return False
    line = (
        f"Read {prompt_file} and follow it exactly. You are carrying out the "
        f"hand-off left by phase {item.phase} of the project at {cfg.project_dir}, "
        f"on the owner's behalf. The brief, in full: {item.note} — that is "
        f"everything you were given. When it is carried out run "
        f"`swarm operator-done {item.phase}`; if the brief is ambiguous run "
        f'`swarm operator-ask {item.phase} "<your question>"` rather than guess.'
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
    if cfg.driver == "tmux" and not spawn(cfg, item, pane, log):
        opqueue.release(cfg, phase, "the operator session would not start")
        _drop_lease(cfg)
        return False
    log.line(f"OPERATOR-DISPATCH {phase} attempt={item.attempts} {reason}".rstrip())
    return True


def release(cfg: Config, log: Log) -> str | None:
    """End the current session: drop the lease, return the pane to idle.

    Returns the phase the lease held. Idempotent, and safe to call on a run that
    never opened one — which is what lets the supervisor's ``finally`` call it
    unconditionally rather than deciding whether it has to.
    """
    with state_mod.transaction(cfg) as st:
        pane = st.operator_pane
        phase = st.release_operator()
    if phase is None:
        return None
    if cfg.driver == "tmux" and pane:
        # Respawn, not kill: the window belongs to `session.setup`, so it goes
        # back to holding `sleep infinity` with its pane id still valid.
        tmux.respawn_pane(pane, "exec sleep infinity")
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
    """
    return sorted(
        i.phase
        for i in opqueue.pending(cfg)
        if i.state == opqueue.RUNNING or i.attempts < opqueue.MAX_ATTEMPTS
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
    :func:`sweep` instead.
    """
    item = opqueue.load(cfg, phase)
    if item is None or item.terminal:
        return False
    if deferred(item):
        log.line(f"OPERATOR-DEFERRED {phase}")
        return False
    return dispatch(cfg, phase, log, reason=f"{phase} merged")


def sweep(cfg: Config, log: Log, now: float | None = None) -> bool:
    """Reclaim dead leases, then open a session for the oldest due hand-off.

    Runs on EVERY supervisor wake, alongside the park deadlines — not from
    ``_watchdog_tick``. That returns early while ``idle < watchdog_s`` and
    ``_handle`` refreshes the idle clock on every FIFO line, so a swarm that is
    moving never reaches the quiet point and the queue would drain only once the
    run was already over.
    """
    if not cfg.operator_enabled:
        return False
    now = time.time() if now is None else now
    _reclaim(cfg, log, now)
    due = opqueue.ready(cfg, now)
    if not due:
        return False
    return dispatch(cfg, due[0].phase, log, reason="queue swept")


def _reclaim(cfg: Config, log: Log, now: float) -> None:
    """Hand back a lease whose session outlived it, on both sides of the pair.

    The item lease and the state lease are taken from the same clock with the
    same span, so they expire together; releasing only one of them would leave
    either an item nothing can pick up or a window nothing can open.
    """
    with state_mod.transaction(cfg) as st:
        stale = st.operator_phase if (
            st.operator_phase is not None and not st.operator_busy(now)
        ) else None
    if stale is not None:
        log.line(f"OPERATOR-LEASE-EXPIRED {stale}")
        release(cfg, log)
    for item in opqueue.load_all(cfg):
        if item.state == opqueue.RUNNING and 0 < item.lease_until <= now:
            opqueue.release(cfg, item.phase, "the operator session outlived its lease")
            log.line(f"OPERATOR-REQUEUED {item.phase}")
