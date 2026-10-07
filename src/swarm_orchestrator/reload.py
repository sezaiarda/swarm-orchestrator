"""``swarm reload`` — the pure policy layer for re-reading ``.swarm.toml`` live.

Today the only way to change anything is ``swarm down`` / ``swarm up``, which
throws away every live worker. But "reload the config" is not one operation: each
setting has its own answer to *when does this actually take effect*, and getting
that wrong is worse than not reloading at all — a half-applied reload leaves the
run in a state neither the old nor the new config describes.

So this module decides, and decides only. It is pure and side-effect free, in the
same spirit as :func:`ledger.ready` and :func:`session.plan_worker_windows`: the
supervisor and the CLI own the doing. Every setting in :data:`config.SETTINGS`
carries one of three classes (and, where the live run decides, a gate that is
resolved here); :func:`diff` turns two loaded
configs plus a description of the live run into a list of :class:`Change`, and
:func:`hold_over` produces the config that is actually safe to adopt.

The three classes:

``HOT``
    Safe to apply to the running swarm now, because every consumer re-reads it
    each time it is used.

``NEXT``
    Accepted, but it cannot reach anything already running — it takes effect for
    the next worker or master launched. This is a *structural* property, not
    caution: :func:`launch._worker_env` freezes ``SWARM_BUILD_JOBS`` /
    ``CARGO_BUILD_JOBS`` into a worker's environment at spawn time and :func:`config._int_env` gives the environment strict precedence
    over the file, so a running worker holds a private copy no reload can reach.
    The same is true of everything delivered into a pane at launch.

``RESTART``
    Refused. These name the run itself: the supervisor holds an open fd on
    ``control.fifo`` for its entire life, the flock is on ``lock_path``, live
    worktrees exist under the old ``wt_dir``, and every worker was handed
    ``SWARM_STATE_DIR`` at launch. Changing ``slug`` / ``state_dir`` /
    ``project_dir`` / ``driver`` / ``[tmux].session`` mid-run does not move the
    run — it forks it in two, and the half still holding the FIFO wins. The
    session's default follows ``[swarm].name``, which is ``HOT``: the name is
    applied and the session rename it implies is reported as refused
    (:func:`_session_follows_name`).

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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .config import HOT, NEXT, RESTART, SETTINGS, Config, Setting, recorded, session_default
from .state import State

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
GATES = (GATE_RESIZE, GATE_PARK, GATE_LAYOUT, GATE_IN_FLIGHT, GATE_REPOS, GATE_WATCHDOG)


@dataclass
class Facts:
    """What the live run looks like, as far as reload policy is concerned."""

    busy: dict[int, str] = field(default_factory=dict)  # slot id -> phase
    riders: list[str] = field(default_factory=list)  # in a batch, in its seed's slot
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
            riders=sorted(r for seed, rows in st.batches.items() for r in rows if r != seed),
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
        out = list(self.busy.values()) + self.riders + list(self.parked) + list(self.waiting)
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
    klass: str  # the setting's reload class
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
    reload class, possibly downgraded by a gate. Fields that did *not*
    change but are pinned by a ``SWARM_*`` variable get an :data:`ENV` entry
    anyway — because a config edit to one of those is invisible here by
    construction (``load()`` applied the same override to both sides), and
    reporting nothing is exactly the silence this is meant to prevent.
    """
    out: list[Change] = []
    for name, policy in SETTINGS.items():
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
        if (name == "session" and old_cfg.name != new_cfg.name
                and new == session_default(new_cfg.name)):
            change.effect = _follows_name(policy)
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
    pending = _session_follows_name(old_cfg, new_cfg)
    if pending is not None:
        out.append(pending)
    return out


def _session_follows_name(old_cfg: Config, new_cfg: Config) -> Change | None:
    """The rename ``[swarm].name`` asks of the tmux session, which a run cannot
    take: None unless the file's session is not the one the run is in.

    :func:`config.load` keeps a live run's session (so the two configs agree
    on it and the loop above is silent), and records the one the name now gives
    as ``session_wanted``. Reported as the refusal it is, each reload, until the
    restart that renames it — like an edit to ``[tmux].session`` itself.
    """
    wanted = getattr(new_cfg, "session_wanted", "") or new_cfg.session
    if wanted == new_cfg.session or old_cfg.session != new_cfg.session:
        return None
    policy = SETTINGS["session"]
    return Change(
        name="session",
        section=policy.section,
        key=policy.key,
        klass=policy.klass,
        effective=RESTART,
        old=new_cfg.session,
        new=wanted,
        env=policy.env,
        effect=_follows_name(policy),
    )


def _follows_name(policy: Setting) -> str:
    return (
        "REFUSED — the session takes its name from [swarm].name, and"
        f" {policy.why}; `swarm down` and `swarm up` (or `swarm restart --full`)"
        " rename it"
    )


def _env_change(policy: Setting, value: Any, var: str) -> Change:
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
    change: Change, policy: Setting, old: Any, new: Any, facts: Facts
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
    change: Change, old: list[str], new: list[str], facts: Facts, policy: Setting
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


def _shadow(policy: Setting, env: dict[str, str]) -> str | None:
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


def snapshot_cfg(cfg: Config) -> Config | None:
    """The config the running supervisor is actually using, or None.

    Written by the supervisor at startup and after every reload. It matters
    because ``load()`` layers SWARM_* env overrides from *that* process's
    environment: a CLI re-reading the file would compute the wrong "before" for
    every overridden field, and once the file has been edited it cannot see the
    old values at all. A supervisor that takes a run over (``swarm restart``)
    reads it for the same reason: what the last one ran on is its "before".
    """
    return recorded_cfg(cfg.state_dir)


def recorded_cfg(state_dir: Path) -> Config | None:
    """:func:`snapshot_cfg` for the run in ``state_dir``, whichever project and
    environment asks: the config its last supervisor recorded, bound to that
    state dir. None when no supervisor ever started there, or the record does
    not read as a config."""
    raw = recorded(state_dir)
    project = raw.get("project_dir")
    if not isinstance(project, str) or not project:
        return None
    kwargs = {}
    for f in dataclasses.fields(Config):
        if not f.init:
            continue
        name = f.name
        if name not in raw:
            # A setting added after this supervisor started: it runs on the
            # default, so that is its "before".
            setting = SETTINGS.get(name)
            if setting is None:
                return None
            d = setting.default
            kwargs[name] = d(Path(project)) if callable(d) else d
            continue
        val = raw[name]
        kwargs[name] = Path(val) if name in ("project_dir",) and isinstance(val, str) else val
    try:
        cfg = Config(**kwargs)
    except (TypeError, ValueError):
        return None
    cfg.state_dir = Path(state_dir)
    return cfg


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

