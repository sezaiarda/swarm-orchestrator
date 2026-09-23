"""``swarm reload`` — the pure policy layer for re-reading ``.swarm.toml`` live.

Today the only way to change anything is ``swarm down`` / ``swarm up``, which
throws away every live worker. But "reload the config" is not one operation: each
setting has its own answer to *when does this actually take effect*, and getting
that wrong is worse than not reloading at all — a half-applied reload leaves the
run in a state neither the old nor the new config describes.

So this module decides, and decides only. It is pure and side-effect free, in the
same spirit as :func:`ledger.ready` and :func:`session.plan_worker_windows`: the
supervisor and the CLI own the doing. :data:`POLICY` classifies **every** ``init``
field of :class:`Config` into one of three classes, :func:`diff` turns two loaded
configs plus a description of the live run into a list of :class:`Change`, and
:func:`hold_over` produces the config that is actually safe to adopt.

The three classes:

``HOT``
    Safe to apply to the running swarm now, because every consumer re-reads it
    each time it is used.

``NEXT``
    Accepted, but it cannot reach anything already running — it takes effect for
    the next worker or master launched. This is a *structural* property, not
    caution: :func:`launch._worker_env` freezes ``SWARM_BUILD_MAX`` /
    ``SWARM_BUILD_JOBS`` / ``CARGO_BUILD_JOBS`` into a worker's environment at
    spawn time and :func:`config._int_env` gives the environment strict precedence
    over the file, so a running worker holds a private copy no reload can reach.
    The same is true of everything delivered into a pane at launch.

``RESTART``
    Refused. These name the run itself: the supervisor holds an open fd on
    ``control.fifo`` for its entire life, the flock is on ``lock_path``, live
    worktrees exist under the old ``wt_dir``, and every worker was handed
    ``SWARM_STATE_DIR`` at launch. Changing ``slug`` / ``state_dir`` /
    ``project_dir`` / ``driver`` / ``[tmux].session`` mid-run does not move the
    run — it forks it in two, and the half still holding the FIFO wins.

Two behaviours are worth calling out because they are where a naive reload does
damage:

**Shrinking ``max_workers`` is retire-then-reap, never delete.** A busy slot
outside the new cap keeps its pane and finishes normally; its record is dropped
only once its phase completes. :func:`retire_plan` splits a shrink into exactly
those two sets. Deleting the record while the worker was still in it would make
its eventual ``swarm done`` fall through :meth:`Supervisor._advance_done`'s
already-done branch as a DONE-DUPLICATE no-op — stranding the phase and leaking
its worktree.

**Env overrides keep winning after a reload.** :func:`config.load` re-reads them
every time, so where a ``SWARM_*`` variable is set, editing the file changes
nothing at all. That is the one failure mode a diff must never render as silence,
so those fields are reported explicitly as :data:`ENV` naming the variable.
"""

from __future__ import annotations

import dataclasses
import os
import time
from dataclasses import dataclass, field, fields, replace
from typing import Any

from .config import Config
from .state import State

HOT = "hot"
NEXT = "next"
RESTART = "restart"
# Not a policy class — an *outcome*: the file changed (or could) but a SWARM_*
# variable shadows it, so the reload provably does nothing.
ENV = "env"

# Gate names, resolved against the live run in :func:`diff`.
GATE_RESIZE = "resize"
GATE_PARK = "park-shift"
GATE_LAYOUT = "layout-pinned"
GATE_IN_FLIGHT = "in-flight"
GATE_REPOS = "repos-shrink"
GATE_WATCHDOG = "watchdog-refresh"


@dataclass(frozen=True)
class Policy:
    """How one config field behaves under a live reload."""

    name: str
    section: str
    key: str
    klass: str
    env: str | None
    why: str
    gate: str | None = None
    numeric: bool = False  # an int field: a malformed env override does NOT shadow


def _p(name, section, key, klass, env, why, gate=None, numeric=False) -> Policy:
    return Policy(name, section, key, klass, env, why, gate, numeric)


POLICY: dict[str, Policy] = {
    p.name: p
    for p in (
        # -- run identity: changing these forks the run ---------------------
        _p("project_dir", "(cli)", "--project-dir", RESTART, None,
           "every worktree, lock and repo path is derived from it; the live"
           " worktrees are under the old one"),
        _p("slug", "[swarm]", "slug", RESTART, "SWARM_SLUG",
           "the slug IS the state dir: a new one is a new, empty run while the"
           " supervisor still holds the old FIFO and flock"),
        _p("driver", "[swarm]", "driver", RESTART, "SWARM_DRIVER",
           "the live topology was built by the old driver; swapping it mid-run"
           " leaves panes nothing can address"),
        _p("session", "[tmux]", "session", RESTART, "SWARM_SESSION",
           "every pane and window id recorded in state.json belongs to the old"
           " tmux session"),
        _p("git_isolation", "[git]", "isolation", RESTART, "SWARM_GIT_ISOLATION",
           "live worktrees and a populated merge queue only make sense under the"
           " isolation mode that created them"),
        _p("tui_autostart", "[tui]", "autostart", RESTART, "SWARM_TUI_AUTOSTART",
           "the dashboard pane is created once, by session.setup at `swarm up`;"
           " there is no later moment a reload could reach"),
        _p("tui_cmd", "[tui]", "cmd", RESTART, "SWARM_TUI_CMD",
           "the dashboard pane is respawned once, by session.setup at `swarm up`;"
           " a live dash keeps the command it was started with"),

        # -- hot: re-read at every use --------------------------------------
        _p("max_workers", "[swarm]", "max_workers", HOT, None,
           "slots are plain state records; growing appends, shrinking retires",
           gate=GATE_RESIZE),
        _p("park_after", "[worker]", "park_after", HOT, "SWARM_PARK_AFTER",
           "the supervisor reads it when it arms a park timer",
           gate=GATE_PARK, numeric=True),
        _p("watchdog_s", "[swarm]", "watchdog_s", HOT, "SWARM_WATCHDOG",
           "the supervisor caches it as `self.watchdog_s` in __init__, so a"
           " reload must refresh that attribute too — swapping `self.cfg` alone"
           " leaves the old sweep interval running",
           gate=GATE_WATCHDOG, numeric=True),
        _p("git_auto_resolve", "[git]", "auto_resolve", HOT, None,
           "gitq._auto_resolve reads the strategy table at the moment a conflict"
           " happens, so the next conflict uses the new rules — it cannot"
           " retroactively fix an integration that is already held; retry that one"
           " with `swarm resolved <phase>` once the tree is clean"),
        _p("ledger", "[tasks]", "ledger", HOT, None,
           "build_context loads the ledger from disk on every pass"),
        _p("roadmap", "[tasks]", "roadmap", HOT, None,
           "only ever handed to the master as a path, read fresh each time"),
        _p("exclude", "[tasks]", "exclude", HOT, None,
           "ledger.ready() takes the exclusion set as an argument on every pass"),
        _p("telegram_notify", "[telegram]", "notify", HOT, None,
           "the supervisor resolves the notifier per ping; note that a running"
           " worker's own `swarm done` ping uses the copy in its worktree mirror"),
        _p("tmux_layout", "[tmux]", "layout", HOT, "SWARM_LAYOUT",
           "the layout is re-applied whenever worker panes are re-tidied",
           gate=GATE_LAYOUT),
        _p("git_main_branch", "[git]", "main_branch", HOT, "SWARM_GIT_MAIN",
           "the integrator reads it per merge — but a phase mirrored off the old"
           " main must not then be merged into a different one",
           gate=GATE_IN_FLIGHT),
        _p("git_repos", "[git]", "repos", HOT, "SWARM_GIT_REPOS",
           "the repo set is globbed per worktree_add; adding is safe, but a repo"
           " dropped between a phase's worktree_add and its integrate is never"
           " visited, so its branch leaks and its commits never merge",
           gate=GATE_REPOS),

        # -- next launch: frozen into a pane or an environment at spawn ------
        _p("master_model", "[swarm]", "master_model", NEXT, None,
           "baked into the master's command line when the pane is respawned"),
        _p("master_cmd", "[swarm]", "master_cmd", NEXT, "SWARM_MASTER_CMD",
           "baked into the master's command line when the pane is respawned"),
        _p("resolver_cmd", "[swarm]", "resolver_cmd", NEXT, "SWARM_RESOLVER_CMD",
           "read once, when a conflict opens a resolver pane; an already-open"
           " resolver keeps the command it was spawned with"),
        _p("command_template", "[worker]", "command_template", NEXT, None,
           "the prime line is typed into a worker's pane once, at launch"),
        _p("command_file", "[worker]", "command_file", NEXT, None,
           "resolved while composing the launch line for a new worker"),
        _p("env_marker", "[worker]", "env_marker", NEXT, None,
           "the variable name is written into the worker's environment at spawn;"
           " a live worker still answers to the old one"),
        _p("done_hook", "[worker]", "done_hook", NEXT, None,
           "delivered to the worker at launch as the command it must run"),
        _p("worker_cmd", "[worker]", "worker_cmd", NEXT, "SWARM_WORKER_CMD",
           "it is the command a pane is respawned with"),
        _p("ready_marker", "[worker]", "ready_marker", NEXT, "SWARM_READY_MARKER",
           "only consulted while waiting for a freshly spawned pane to boot"),
        _p("worker_settings", "[worker]", "worker_settings", NEXT,
           "SWARM_WORKER_SETTINGS",
           "merged into the `claude` invocation at spawn time"),
        _p("worker_effort", "[worker]", "effort", NEXT, "SWARM_WORKER_EFFORT",
           "passed as `claude --effort` on the command line a pane is spawned with"),
        _p("done_grace_s", "[worker]", "done_grace_s", NEXT, "SWARM_DONE_GRACE",
           "`swarm done` runs in the worker's cwd — its worktree mirror — so it"
           " reads the .swarm.toml copy branched at launch, not this one",
           numeric=True),
        _p("build_max_concurrent", "[build]", "max_concurrent", NEXT,
           "SWARM_BUILD_MAX",
           "launch._worker_env freezes SWARM_BUILD_MAX into each worker's"
           " environment, and _int_env gives the environment strict precedence",
           numeric=True),
        _p("build_jobs", "[build]", "jobs", NEXT, "SWARM_BUILD_JOBS",
           "launch._worker_env freezes SWARM_BUILD_JOBS and CARGO_BUILD_JOBS into"
           " each worker's environment at spawn", numeric=True),
        _p("build_cache", "[build]", "cache", NEXT, "SWARM_BUILD_CACHE",
           "only read while linking a new worktree's target/ at worktree_add"),
        _p("operator_enabled", "[operator]", "enabled", NEXT, "SWARM_OPERATOR",
           "the gate is read by the worker's own `swarm done`, which runs in its"
           " worktree mirror — so it reads the .swarm.toml copy branched at"
           " launch, not this one"),
        _p("operator_cmd", "[operator]", "cmd", NEXT, "SWARM_OPERATOR_CMD",
           "read once, when a queued hand-off opens an operator pane; an"
           " already-open session keeps the command it was spawned with"),
        _p("operator_model", "[operator]", "model", NEXT, None,
           "baked into the operator session's command line when its pane is"
           " spawned"),
        _p("operator_triage_model", "[operator]", "triage_model", NEXT, None,
           "triage is spawned by the worker's `swarm done` from its worktree"
           " mirror, so it reads the branched .swarm.toml copy"),
        _p("overseer_enabled", "[overseer]", "enabled", HOT, "SWARM_OVERSEER",
           "the supervisor's trigger policy reads it on every wake; a pass"
           " already running is left to finish"),
        _p("overseer_cmd", "[overseer]", "cmd", NEXT, "SWARM_OVERSEER_CMD",
           "baked into the Overseer pane's command line when a pass is spawned"),
        _p("overseer_model", "[overseer]", "model", NEXT, None,
           "baked into the Overseer session's command line when a pass is spawned"),
        _p("overseer_min_gap_s", "[overseer]", "min_gap_s", HOT, "SWARM_OVERSEER_MIN_GAP",
           "read each time the policy asks whether a pass is due", numeric=True),
        _p("overseer_every_finished", "[overseer]", "every_finished", HOT,
           "SWARM_OVERSEER_EVERY_FINISHED",
           "the finished-phase counter is compared against it on every wake",
           numeric=True),
        _p("overseer_every_s", "[overseer]", "every_s", HOT, "SWARM_OVERSEER_EVERY",
           "the cadence clock is compared against it on every wake", numeric=True),
        _p("overseer_owner_wait_s", "[overseer]", "owner_wait_s", HOT,
           "SWARM_OVERSEER_OWNER_WAIT",
           "each waiting phase's age is compared against it on every wake",
           numeric=True),
        _p("overseer_starve_s", "[overseer]", "starve_s", HOT, "SWARM_OVERSEER_STARVE",
           "the starvation episode's age is compared against it on every wake",
           numeric=True),
        _p("overseer_timeout_s", "[overseer]", "timeout_s", NEXT, "SWARM_OVERSEER_TIMEOUT",
           "a pass's deadline is fixed when it starts; the next pass gets the new one",
           numeric=True),
    )
}


@dataclass
class Facts:
    """What the live run looks like, as far as reload policy is concerned."""

    busy: dict[int, str] = field(default_factory=dict)  # slot id -> phase
    parked: list[str] = field(default_factory=list)
    waiting: dict[str, float] = field(default_factory=dict)  # phase -> deadline
    integ_queue: list[str] = field(default_factory=list)
    integ_blocked: str | None = None
    layout: str | None = None  # st.layout: `swarm layout` pinned this run
    paused: bool = False
    finished: bool = False
    env: dict[str, str] = field(default_factory=dict)
    now: float = 0.0

    @classmethod
    def from_state(
        cls, st: State, env: dict[str, str] | None = None, now: float | None = None
    ) -> "Facts":
        return cls(
            busy={s.id: s.phase for s in st.busy_slots() if s.phase},
            parked=list(st.parked),
            waiting=dict(st.waiting),
            integ_queue=list(st.integ_queue),
            integ_blocked=st.integ_blocked,
            layout=st.layout,
            paused=st.paused,
            finished=st.finished,
            env=dict(os.environ) if env is None else dict(env),
            now=time.time() if now is None else now,
        )

    def in_flight(self) -> list[str]:
        """Phases that would be crossed by a mid-run change: in a slot, off-grid
        awaiting the owner, or somewhere in the merge queue."""
        out = list(self.busy.values()) + list(self.parked) + list(self.waiting)
        out += list(self.integ_queue)
        if self.integ_blocked:
            out.append(self.integ_blocked)
        return sorted(dict.fromkeys(out))


@dataclass
class Change:
    """One field's verdict: what it was, what it would be, and what happens."""

    name: str
    section: str
    key: str
    klass: str  # the POLICY class
    effective: str  # what actually happens: HOT / NEXT / RESTART / ENV
    old: Any = None
    new: Any = None
    effect: str = ""
    env: str | None = None
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass
class ReloadPlan:
    """The whole verdict plus the config it is safe to adopt."""

    changes: list[Change] = field(default_factory=list)
    cfg: Config | None = None  # the held-over config (RESTART fields reverted)
    facts: Facts = field(default_factory=Facts)

    def refused(self) -> list[Change]:
        return [c for c in self.changes if c.effective == RESTART]

    def to_dict(self) -> dict:
        return {
            "changes": [c.to_dict() for c in self.changes],
            "refused": [c.name for c in self.refused()],
            "in_flight": self.facts.in_flight(),
        }


# -- the diff -------------------------------------------------------------
def diff(old_cfg: Config, new_cfg: Config, facts: Facts) -> list[Change]:
    """Classify every difference between two loaded configs against the live run.

    Two kinds of entry come back. Fields whose value actually changed get their
    :data:`POLICY` class, possibly downgraded by a gate. Fields that did *not*
    change but are pinned by a ``SWARM_*`` variable get an :data:`ENV` entry
    anyway — because a config edit to one of those is invisible here by
    construction (``load()`` applied the same override to both sides), and
    reporting nothing is exactly the silence this is meant to prevent.
    """
    out: list[Change] = []
    for name, policy in POLICY.items():
        old = getattr(old_cfg, name)
        new = getattr(new_cfg, name)
        shadow = _shadow(policy, facts.env)
        if old == new:
            if shadow is not None:
                out.append(_env_change(policy, old, shadow))
            continue
        change = Change(
            name=name,
            section=policy.section,
            key=policy.key,
            klass=policy.klass,
            effective=policy.klass,
            old=_plain(old),
            new=_plain(new),
            env=policy.env,
        )
        _apply_gate(change, policy, old, new, facts)
        if shadow is None and policy.env and policy.env in facts.env:
            # The value differs even though the variable is set: only `_int_env`
            # does that, by walking past an override it cannot parse. Say so — the
            # file won this once, and it will stop winning the moment the variable
            # is corrected.
            change.effect += (
                f" (note: {policy.env} is set but unparseable, so the file value"
                " wins)"
            )
        out.append(change)
    return out


def _env_change(policy: Policy, value: Any, var: str) -> Change:
    return Change(
        name=policy.name,
        section=policy.section,
        key=policy.key,
        klass=policy.klass,
        effective=ENV,
        old=_plain(value),
        new=_plain(value),
        env=var,
        effect=(
            f"pinned to {value!r} by {var}; load() re-reads the environment, so"
            f" editing {policy.section}.{policy.key} has no effect until it is unset"
        ),
    )


def _apply_gate(
    change: Change, policy: Policy, old: Any, new: Any, facts: Facts
) -> None:
    """Resolve a policy's gate against the live run, filling effect + actions."""
    if policy.klass == RESTART:
        change.effect = f"REFUSED — {policy.why}; restart the run to change it"
        return

    if policy.gate == GATE_RESIZE:
        _gate_resize(change, int(old), int(new), facts)
        return
    if policy.gate == GATE_PARK:
        _gate_park(change, int(old), int(new), facts)
        return
    if policy.gate == GATE_LAYOUT:
        if facts.layout is not None:
            change.effective = NEXT
            change.effect = (
                f"`swarm layout {facts.layout}` pinned this run's layout, and that"
                " wins for the rest of it; the new value applies at the next"
                " `swarm up`"
            )
            return
        change.effect = "worker panes are re-tidied into the new layout"
        return
    if policy.gate == GATE_IN_FLIGHT:
        flight = facts.in_flight()
        if flight:
            change.effective = RESTART
            change.effect = (
                f"REFUSED while {len(flight)} phase(s) are in flight"
                f" ({', '.join(flight[:4])}) — {policy.why}"
            )
            return
        change.effect = f"applied now; nothing is in flight ({policy.why})"
        return
    if policy.gate == GATE_REPOS:
        _gate_repos(change, list(old), list(new), facts, policy)
        return
    if policy.gate == GATE_WATCHDOG:
        change.actions = [f"set supervisor.watchdog_s = {new}"]
        change.effect = (
            f"the stall watchdog sweeps every {new}s"
            if new
            else "the stall watchdog is disabled"
        ) + " — the supervisor's cached copy must be refreshed, not just its cfg"
        return

    if policy.klass == NEXT:
        change.effect = f"takes effect at the next launch — {policy.why}"
    else:
        change.effect = f"applied now — {policy.why}"


def _gate_resize(change: Change, old: int, new: int, facts: Facts) -> None:
    """``max_workers`` is always applied — but a shrink retires, never deletes."""
    if new > old:
        change.actions = [f"add {new - old} free slot(s)"]
        change.effect = f"{new - old} new slot(s) become claimable immediately"
        return
    drop, retire = retire_plan(old, new, facts)
    change.actions = [f"drop idle slot {sid}" for sid in drop] + [
        f"retire slot {sid} (phase {facts.busy[sid]}) — it keeps its pane and is"
        " reaped when the phase completes"
        for sid in retire
    ]
    change.effect = (
        f"shrink {old} -> {new}: {len(drop)} idle slot(s) dropped now,"
        f" {len(retire)} busy slot(s) retired. A busy slot is NEVER deleted —"
        " its `swarm done` would degrade into a DONE-DUPLICATE no-op, stranding"
        " the phase and leaking its worktree"
    )


def _gate_park(change: Change, old: int, new: int, facts: Facts) -> None:
    """``park_after`` also moves the deadlines already armed."""
    shifts = park_shift(old, new, facts)
    if new == 0:
        change.actions = [f"disarm park timer for {p}" for p in sorted(facts.waiting)]
        change.effect = (
            "parking disabled; already-armed deadlines are dropped and a waiting"
            " worker simply holds its slot"
        )
        return
    change.actions = [
        f"{p}: park at +{max(0, int(ts - facts.now))}s" for p, ts in sorted(shifts.items())
    ]
    change.effect = (
        f"new waits park after {new}s; the {len(shifts)} deadline(s) already armed"
        f" shift by {new - old:+d}s (they are stored absolute), floored at now+1s"
    )


def _gate_repos(
    change: Change, old: list[str], new: list[str], facts: Facts, policy: Policy
) -> None:
    """Growing ``[git].repos`` is free; shrinking it mid-flight leaks a branch."""
    dropped = [g for g in old if g not in new]
    flight = facts.in_flight()
    if dropped and flight:
        change.effective = RESTART
        change.effect = (
            f"REFUSED — {', '.join(dropped)} would be dropped while"
            f" {len(flight)} phase(s) are in flight; a repo removed between a"
            " phase's worktree_add and its integrate is never visited, so its"
            " branch leaks and its commits never merge"
        )
        return
    if dropped:
        change.effect = f"applied now ({', '.join(dropped)} dropped); nothing in flight"
        return
    change.effect = "applied now — growing the repo set is always safe"


def _shadow(policy: Policy, env: dict[str, str]) -> str | None:
    """The ``SWARM_*`` variable that overrides this field, if it is really set.

    A numeric field is only shadowed by a value that *parses*: :func:`config._int_env`
    walks past a malformed override to the file value, so calling it a shadow
    would be a lie in exactly the case an owner most needs the truth.
    """
    if policy.env is None or policy.env not in env:
        return None
    if policy.numeric:
        try:
            int(env[policy.env])
        except (TypeError, ValueError):
            return None
    return policy.env


def _plain(value: Any) -> Any:
    """A JSON-safe view of a config value (``Path`` renders as its string)."""
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    return value if isinstance(value, (int, float, bool, str, type(None))) else str(value)


# -- pure helpers the supervisor executes ---------------------------------
def retire_plan(old: int, new: int, facts: Facts) -> tuple[list[int], list[int]]:
    """Split a ``max_workers`` shrink into ``(drop_now, retire)`` slot ids.

    Busy slots are kept in preference to idle ones — the same ranking
    :meth:`State.resize` uses — so a shrink costs nothing in flight and only
    retires a busy slot when the new cap is below the number of live workers.
    """
    ids = sorted(set(range(old)) | set(facts.busy))
    ranked = sorted(ids, key=lambda sid: (sid not in facts.busy, sid))
    surplus = ranked[new:]
    return (
        sorted(sid for sid in surplus if sid not in facts.busy),
        sorted(sid for sid in surplus if sid in facts.busy),
    )


def park_shift(old: int, new: int, facts: Facts) -> dict[str, float]:
    """Where each already-armed park deadline moves to.

    Deadlines are stored absolute (so they survive a supervisor restart), which
    means a changed ``park_after`` does not reach them on its own: a worker armed
    under a 600s policy would still park at its old instant under a new 60s one.
    Shifting by the delta keeps the promise the owner just made. The floor of
    ``now + 1s`` matters when the value shrinks a lot — otherwise every waiting
    worker is parked in the same instant the reload lands, mid-question. A new
    value of ``0`` disables parking, so the deadlines are dropped entirely.
    """
    if new == 0:
        return {}
    delta = new - old
    return {
        phase: max(facts.now + 1.0, deadline + delta)
        for phase, deadline in facts.waiting.items()
    }


def hold_over(new_cfg: Config, changes: list[Change]) -> Config:
    """The config that is actually safe to adopt: every RESTART field reverted.

    A reload is all-or-nothing per field. Handing the supervisor a config where
    ``slug`` moved but the FIFO did not — or where ``git.main_branch`` changed
    under a phase whose mirror was branched off the old one — is worse than
    refusing, because nothing downstream re-checks. Reverting here means a refusal
    is a *fact about the object* the rest of the process is holding, not a warning
    somebody has to remember to honour.
    """
    revert = {c.name: c.old for c in changes if c.effective == RESTART}
    if not revert:
        return new_cfg
    # `old` was flattened for JSON; restore the declared type before replacing.
    typed = {
        name: _retype(getattr(new_cfg, name), value) for name, value in revert.items()
    }
    return replace(new_cfg, **typed)


def _retype(current: Any, value: Any) -> Any:
    from pathlib import Path

    if isinstance(current, Path):
        return Path(str(value))
    if isinstance(current, list):
        return list(value)
    if isinstance(current, dict):
        return dict(value)
    return value


def plan(old_cfg: Config, new_cfg: Config, facts: Facts) -> ReloadPlan:
    """:func:`diff` plus :func:`hold_over` — the whole verdict in one object."""
    changes = diff(old_cfg, new_cfg, facts)
    return ReloadPlan(changes=changes, cfg=hold_over(new_cfg, changes), facts=facts)


# -- rendering ------------------------------------------------------------
def render(payload: ReloadPlan) -> str:
    """The reload diff, grouped by what actually happens to the running swarm."""
    real = [c for c in payload.changes if c.effective != ENV and c.old != c.new]
    pinned = [c for c in payload.changes if c.effective == ENV]
    out: list[str] = []

    if not real:
        out.append("swarm reload: no config changes")
    for klass, title in (
        (HOT, "applied now"),
        (NEXT, "at the next worker/master launch"),
        (RESTART, "REFUSED (held at the old value — restart to change)"),
    ):
        group = [c for c in real if c.effective == klass]
        if not group:
            continue
        out.append(f"{title}:")
        for c in group:
            out.append(f"  {c.section}.{c.key}: {_fmt(c.old)} -> {_fmt(c.new)}")
            out.append(f"      {c.effect}")
            for action in c.actions:
                out.append(f"      - {action}")
        out.append("")

    if pinned:
        out.append("pinned by the environment (a file edit here does nothing):")
        for c in pinned:
            out.append(f"  {c.section}.{c.key}: {c.env}={_fmt(c.old)}")
        out.append("")

    flight = payload.facts.in_flight()
    if flight:
        out.append(f"in flight: {', '.join(flight)}")
    return "\n".join(out).rstrip()


def _fmt(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}={v}" for k, v in sorted(value.items())) + "}"
    return str(value)


def coverage_gap() -> set[str]:
    """Config fields :data:`POLICY` does not classify. Empty is the only healthy
    answer; the policy test asserts it, so a new field cannot be added to
    :class:`Config` without someone deciding what a reload should do with it."""
    return {f.name for f in fields(Config) if f.init} ^ set(POLICY)
