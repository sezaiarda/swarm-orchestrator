"""Configuration loading for swarm-orchestrator.

Reads a per-project ``.swarm.toml`` (stdlib ``tomllib``), applies defaults, and
resolves the runtime state directory under ``~/.local/state``. Test/real seams
(driver, fake commands, telegram sink, readiness marker) are honoured via
environment variables so the hermetic tests never touch tmux or ``claude``.

Every setting is declared once, as a field of :class:`Config` carrying its
:class:`Setting`: table, key, default, env override, validation, what a live
reload does with it and one line of what it is for. :func:`load`, ``reload.py``
and the TUI config form all read :data:`SETTINGS`; nothing restates it.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Callable

from . import machine, tmux
from .tmux import AUTO_LAYOUT, LAYOUTS, normalize_layout


#: The file that makes a folder a swarm project.
CONFIG_NAME = ".swarm.toml"


class WrongSwarm(ValueError):
    """The command names one project and ``SWARM_STATE_DIR`` another's run state."""


class NoProject(ValueError):
    """The command names no swarm project; the message says where it looked."""


def _slugify(name: str) -> str:
    """Turn a project directory name into a filesystem-safe slug."""
    keep = [c.lower() if c.isalnum() else "-" for c in name]
    slug = "".join(keep).strip("-")
    return slug or "project"


def _default_slug(pdir: Path) -> str:
    """A slug that is unique to the *full* project path, not just its basename.

    Two projects that share a folder name in different parents (``a/myproject``
    and ``b/myproject``) must not collapse to the same runtime state dir — that
    would make them share ``state.json``, the FIFO, and per-phase worktrees. We
    suffix the basename slug with a short hash of the resolved path so distinct
    projects never collide, while the same project is always stable.
    """
    digest = hashlib.sha1(str(pdir.resolve()).encode()).hexdigest()[:8]
    return f"{_slugify(pdir.name)}-{digest}"


def session_default(name: str) -> str:
    """The tmux session a swarm called ``name`` gets when ``[tmux].session``
    names none."""
    return _slugify(name) or "swarm"


#: What ``claude --effort`` accepts (CLI 2.1.276). "" means pass nothing.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

#: ``[usage].rules``: which window, at what percentage, does what.
USAGE_WINDOWS = ("week", "five_hour")
USAGE_ACTIONS = ("pause", "down")
USAGE_RULES_DEFAULT = (
    {"window": "week", "at": 60, "action": "pause"},
    {"window": "week", "at": 70, "action": "down"},
    {"window": "five_hour", "at": 90, "action": "pause"},
)


def _usage_rules(raw: object) -> list[dict]:
    """Validated ``[usage].rules``. A malformed rule fails the load, like a bad
    effort: a cap that silently does not apply is worse than a loud error."""
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"[usage].rules must be a list of tables, got {raw!r}")
    out = []
    for i, rule in enumerate(raw):
        where = f"[usage].rules[{i}]"
        if not isinstance(rule, dict):
            raise ValueError(f"{where} must be a table, got {rule!r}")
        window, action, at = rule.get("window"), rule.get("action"), rule.get("at")
        if window not in USAGE_WINDOWS:
            raise ValueError(f"{where}.window must be one of {', '.join(USAGE_WINDOWS)}, "
                             f"got {window!r}")
        if action not in USAGE_ACTIONS:
            raise ValueError(f"{where}.action must be one of {', '.join(USAGE_ACTIONS)}, "
                             f"got {action!r}")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or not 0 < at <= 100:
            raise ValueError(f"{where}.at must be a percentage above 0 and at most 100, "
                             f"got {at!r}")
        out.append({"window": window, "at": at, "action": action})
    return out


def _effort(value: object) -> str:
    """A validated ``[worker].effort``; a typo fails at load, not in every pane."""
    level = str(value or "").strip().lower()
    if level and level not in EFFORTS:
        raise ValueError(f"[worker].effort must be one of {', '.join(EFFORTS)} or \"\", got {value!r}")
    return level


def _workers(value: object) -> int:
    """``[swarm].max_workers``: a swarm with < 1 slots can never claim a phase and
    would stall in silence, so one slot is the floor and below it fails the load."""
    n = int(value)
    if n < 1:
        raise ValueError(f"[swarm].max_workers must be >= 1, got {n}")
    return n


def _str_table(value: object) -> dict[str, str]:
    return {str(k): str(v) for k, v in (value or {}).items()}


def _int_env(name: str | None, value: object, default: int, minimum: int) -> int:
    """An int config value, floored at ``minimum``: the env override, else the
    config value, else the code default — the first that parses. ``default`` is
    always a valid int, so a malformed env override *or* a wrong-type config
    value degrades to it instead of crashing ``load()`` for every command."""
    for candidate in (os.environ.get(name) if name else None, value, default):
        if candidate is None:
            continue
        try:
            return max(minimum, int(candidate))
        except (TypeError, ValueError):
            continue
    return max(minimum, default)


def _bool_env(name: str | None, default: bool) -> bool:
    raw = os.environ.get(name) if name else None
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


# -- the schema -------------------------------------------------------------
# Reload classes (what `swarm reload` does with an edit; see reload.py):
HOT = "hot"  # applied to the running swarm now
NEXT = "next"  # reaches the next worker/master/session launched
RESTART = "restart"  # refused and held; needs `swarm down` / `swarm up`

# Value kinds. They pick the parser here and the widget in the TUI form.
INT = "int"
STR = "str"
BOOL = "bool"
LIST = "list"
CHOICE = "choice"
FROZEN = "frozen"  # not a file scalar: the form shows it, never edits it

CLI = "(cli)"  # the table of a value that comes from the command line, not the file


@dataclass(frozen=True)
class Setting:
    """One ``.swarm.toml`` key: how it is read, what it is for, and what a live
    reload does with it (``klass``, ``why``, and a ``gate`` that ``reload.py``
    resolves against the running swarm)."""

    table: str
    key: str
    default: Any  # a value, or a callable of the project dir
    klass: str
    doc: str  # one line for the TUI form
    why: str  # why this reload class: the form's tooltip and `swarm reload`'s reason
    kind: str
    env: str | None = None
    minimum: int | None = None
    choices: tuple[str, ...] = ()
    gate: str | None = None
    parse: Callable[[Any], Any] | None = None
    name: str = ""  # the Config field; filled in from the class

    @property
    def section(self) -> str:
        """``[swarm]`` — or ``(cli)``, for a value the file does not hold."""
        return self.table if self.table == CLI else f"[{self.table}]"

    @property
    def numeric(self) -> bool:
        """An int: a malformed env override does *not* shadow the file (see
        :func:`_int_env`, which walks past it)."""
        return self.kind == INT

    def read(self, table: dict, pdir: Path) -> Any:
        """This setting's value: the env override, else the file, else the default."""
        default = self.default(pdir) if callable(self.default) else self.default
        env = os.environ.get(self.env) if self.env else None
        if self.kind == INT and self.parse is None:
            return _int_env(self.env, table.get(self.key), default, self.minimum or 0)
        if self.kind == BOOL:
            return _bool_env(self.env, table.get(self.key, default))
        raw = table.get(self.key, default) if env is None else env
        if self.parse is not None:
            return self.parse(raw)
        if self.kind == LIST:
            if env is not None:
                return [s.strip() for s in env.split(",") if s.strip()]
            return [str(v) for v in raw]
        return str(raw)


_KINDS = {bool: BOOL, int: INT, str: STR, list: LIST}


def _k(table: str, key: str, default: Any, klass: str, *, doc: str, why: str,
       kind: str | None = None, **rest: Any) -> Any:
    """A :class:`Config` field declaring its :class:`Setting`. The kind follows
    the default's type unless it is given (or ``choices`` make it a CHOICE)."""
    kind = kind or (CHOICE if rest.get("choices") else _KINDS[type(default)])
    return field(metadata={"setting": Setting(table, key, default, klass, doc, why, kind,
                                              **rest)})


@dataclass
class Config:
    """Resolved configuration + runtime paths for one project.

    Declared in the order the TUI form draws it: by table, in file order.
    """

    # -- [swarm] ----------------------------------------------------------
    # What the owner reads, never what the run is keyed by: the state dir, the
    # worktrees and SWARM_PROJECT follow the folder, so a rename moves nothing.
    name: str = _k(
        "swarm", "name", lambda pdir: pdir.name, HOT, env="SWARM_NAME", kind=STR,
        doc="the swarm's display name; \"\" = the folder's",
        why="pings, status and the web board read it each time they show it; the"
            " dashboard shows it from its next start, and the tmux session"
            " follows it only at a full restart")
    max_workers: int = _k(
        "swarm", "max_workers", 4, HOT, minimum=1, gate="resize", parse=_workers,
        doc="phases in flight at once (one pane each)",
        why="slots are plain state records; growing appends, shrinking retires")
    # On by default: without it a supervisor that died stayed down for hours
    # with slots marked busy and nothing able to notice. 0 = purely event-driven.
    watchdog_s: int = _k(
        "swarm", "watchdog_s", 300, HOT, env="SWARM_WATCHDOG", minimum=0,
        gate="watchdog-refresh",
        doc="seconds between liveness sweeps; 0 = off",
        why="the supervisor caches it as `self.watchdog_s` in __init__, so a"
            " reload must refresh that attribute too — swapping `self.cfg` alone"
            " leaves the old sweep interval running")
    master_model: str = _k(
        "swarm", "master_model", "", NEXT,
        doc='model for the master session; "" inherits',
        why="baked into the master's command line when the pane is respawned")
    master_cmd: str = _k(
        "swarm", "master_cmd", "", NEXT, env="SWARM_MASTER_CMD",
        doc='command the master pane runs; "" = the built-in',
        why="baked into the master's command line when the pane is respawned")
    resolver_cmd: str = _k(
        "swarm", "resolver_cmd", "", NEXT, env="SWARM_RESOLVER_CMD",
        doc="command that opens a conflict-resolver session",
        why="read once, when a conflict opens a resolver pane; an already-open"
            " resolver keeps the command it was spawned with")
    # Resolving is a short mechanical splice; it need not pay for the strongest model.
    resolver_model: str = _k(
        "swarm", "resolver_model", "sonnet", NEXT,
        doc='model for the resolver; "" inherits',
        why="baked into the resolver's command line when a conflict opens its pane")
    # The owner's personal setup (CLAUDE.md, memory, plugins, user skills, MCP)
    # is not the project's: see lean.py.
    lean_sessions: bool = _k(
        "swarm", "lean_sessions", True, NEXT, env="SWARM_LEAN_SESSIONS",
        doc="sessions skip your personal ~/.claude setup",
        why="baked into each session's command line when it is spawned")
    driver: str = _k(
        "swarm", "driver", "tmux", RESTART, env="SWARM_DRIVER", choices=("tmux", "bare"),
        doc="tmux = real panes; bare = headless test driver",
        why="the current topology was built by the old driver; swapping it mid-run"
            " leaves panes nothing can address")
    slug: str = _k(
        "swarm", "slug", _default_slug, RESTART, env="SWARM_SLUG", kind=STR,
        doc="names this run's state dir — its identity on disk",
        why="the slug IS the state dir: a new one is a new, empty run while the"
            " supervisor still holds the old FIFO and flock")

    # -- [worker] ---------------------------------------------------------
    # The delay lives in the worker's own `swarm done`, so the single-threaded
    # supervisor loop never blocks on it.
    done_grace_s: int = _k(
        "worker", "done_grace_s", 0, HOT, env="SWARM_DONE_GRACE", minimum=0,
        doc="seconds a worker holds its slot after `done`",
        why="read by each worker's own `swarm done` when it runs, from the"
            " project's file (not its mirror's copy)")
    park_after: int = _k(
        "worker", "park_after", 120, HOT, env="SWARM_PARK_AFTER", minimum=0,
        gate="park-shift",
        doc="seconds a worker may wait on you before parking",
        why="the supervisor reads it when it arms a park timer")
    # See stallcheck.py: a busy worker silent this long gets a model look.
    stall_check_s: int = _k(
        "worker", "stall_check_s", 3600, HOT, minimum=0,
        doc="silent seconds before a stall check; 0 = off",
        why="the supervisor reads it on each watchdog sweep")
    stall_model: str = _k(
        "worker", "stall_model", "haiku", HOT,
        doc="model that judges whether a silent worker is stuck",
        why="read when a stall check starts")
    command_template: str = _k(
        "worker", "command_template", "/prime {phase}", NEXT,
        doc="prime line typed into a new pane ({phase} expands)",
        why="the prime line is typed into a worker's pane once, at launch")
    command_file: str = _k(
        "worker", "command_file", ".claude/commands/prime.md", NEXT,
        doc="slash-command file the init master patches",
        why="resolved while composing the launch line for a new worker")
    worker_cmd: str = _k(
        "worker", "worker_cmd", "claude", NEXT, env="SWARM_WORKER_CMD",
        doc="command each worker pane is launched with",
        why="it is the command a pane is respawned with")
    # Merged over user settings on each worker's `claude` so a worker's own
    # teammates run in-process — no extra tmux panes.
    worker_settings: str = _k(
        "worker", "worker_settings", '{"teammateMode":"in-process"}', NEXT,
        env="SWARM_WORKER_SETTINGS",
        doc="settings JSON merged into each worker's `claude`",
        why="merged into the `claude` invocation at spawn time")
    # Pinned rather than inherited: a worker otherwise runs at whatever the
    # owner's own settings say today, and a bump there would make every phase dearer.
    worker_effort: str = _k(
        "worker", "effort", "high", NEXT, env="SWARM_WORKER_EFFORT",
        choices=("", *EFFORTS), parse=_effort,
        doc='claude --effort per worker; "" = inherit yours',
        why="passed as `claude --effort` on the command line a pane is spawned with")
    worker_sonnet_effort: str = _k(
        "worker", "sonnet_effort", "", NEXT,
        choices=("", *EFFORTS), parse=_effort,
        doc='claude --effort for a Sonnet lead; "" = effort',
        why="passed as `claude --effort` on the command line a pane is spawned with")
    # Every subagent a swarm session starts (a lead's builders, an Explore, the
    # operator's or the Overseer's helpers) runs on these: see subagents.py.
    subagent_model: str = _k(
        "worker", "subagent_model", "claude-sonnet-5-5", NEXT,
        doc="model of every subagent a swarm session starts",
        why="baked into a session's command line when its pane is spawned")
    subagent_effort: str = _k(
        "worker", "subagent_effort", "high", NEXT, choices=("", *EFFORTS), parse=_effort,
        doc='effort of every subagent; "" = the session\'s own',
        why="baked into a session's command line when its pane is spawned")
    env_marker: str = _k(
        "worker", "env_marker", "SWARM_PHASE", NEXT,
        doc="env var carrying the phase name into the worker",
        why="the variable name is written into the worker's environment at spawn;"
            " a live worker still answers to the old one")
    # "" => auto: match the running claude version (see ready_needle).
    ready_marker: str = _k(
        "worker", "ready_marker", "", NEXT, env="SWARM_READY_MARKER",
        doc='"pane booted" banner; "" = the claude version',
        why="only consulted while waiting for a freshly spawned pane to boot")

    # -- [tasks] ----------------------------------------------------------
    ledger: str = _k(
        "tasks", "ledger", "docs/PHASE-LEDGER.md", HOT,
        doc="phase ledger the master reads (project-relative)",
        why="build_context loads the ledger from disk on every pass")
    exclude: list[str] = _k(
        "tasks", "exclude", [], HOT,
        doc="phases the swarm must never launch (comma-sep)",
        why="ledger.ready() takes the exclusion set as an argument on every pass")
    history_dir: str = _k(
        "tasks", "history", "docs/phases", HOT,
        doc="where each phase family's history is filed",
        why="the ledger writer resolves the history path on every flush")
    history_split_kb: int = _k(
        "tasks", "history_split_kb", 256, HOT, minimum=0,
        doc="a family file past this splits per phase (KB)",
        why="checked each time a family file is appended to")
    lessons: str = _k(
        "tasks", "lessons", "tasks/lessons.md", HOT,
        doc="file `swarm lesson` appends to (project-relative)",
        why="the ledger writer resolves the lessons path on every flush")
    ledger_gate: str = _k(
        "tasks", "ledger_gate", "", HOT,
        doc='command that checks the ledger; "" = none',
        why="run each time a follow-up row is applied")
    ledger_batch_s: int = _k(
        "tasks", "ledger_batch_s", 1800, HOT, minimum=0,
        doc="a note or lesson waits this long (s); 0 = at once",
        why="the supervisor passes it to the ledger writer on every flush")
    ledger_in_merge: bool = _k(
        "tasks", "ledger_in_merge", True, HOT,
        doc="a phase's tick rides in its own merge commit",
        why="the supervisor reads it each time a phase lands")

    # -- [telegram] -------------------------------------------------------
    # The bot is the machine's (one sender and one listener for every swarm),
    # so its script and its credentials are in ``machine.toml``
    # (:class:`machine.Settings`). What is a project's own is whether the bot
    # answers for this swarm.
    telegram_commands: bool = _k(
        "telegram", "commands", True, RESTART, env="SWARM_TG_COMMANDS",
        doc="the machine's bot answers for this swarm",
        why="`swarm up` decides whether to start the listener, and the listener"
            " reads what the supervisor recorded at its start")

    # -- [tmux] -----------------------------------------------------------
    # The swarm's own name, not a generic `swarm`: it is what `tmux ls` shows,
    # and one box can host several runs at once. Unset, it follows [swarm].name
    # (see `load`), but a live run keeps the session it was started in.
    session: str = _k(
        "tmux", "session", lambda pdir: session_default(pdir.name), RESTART,
        env="SWARM_SESSION", kind=STR,
        doc="tmux session name; default follows [swarm].name",
        why="every pane and window id recorded in state.json belongs to the old"
            " tmux session")
    tmux_layout: str = _k(
        "tmux", "layout", AUTO_LAYOUT, HOT, env="SWARM_LAYOUT", gate="layout-pinned",
        choices=tuple(LAYOUTS), parse=lambda v: normalize_layout(str(v)),
        doc="pane arrangement (auto = 1 full/2 cols/3+ tiled)",
        why="the layout is re-applied whenever worker panes are re-tidied")
    tmux_panes_per_window: int = _k(
        "tmux", "panes_per_window", 4, RESTART, env="SWARM_PANES_PER_WINDOW", minimum=1,
        doc="worker panes per window before workers-2",
        why="the worker windows were paged at `swarm up`; a slot added later"
            " would follow a different page size than the panes already there")

    # -- [build] ----------------------------------------------------------
    # What describes this project's builds. The gate they queue at is the
    # machine's, and so are its limits: `machine.toml` (see MACHINE_KEYS).
    build_jobs: int = _k(
        "build", "jobs", 6, NEXT, env="SWARM_BUILD_JOBS", minimum=0,
        doc="CARGO_BUILD_JOBS handed to each build",
        why="launch._worker_env freezes SWARM_BUILD_JOBS and CARGO_BUILD_JOBS into"
            " each worker's environment at spawn")
    build_cache: bool = _k(
        "build", "cache", True, NEXT, env="SWARM_BUILD_CACHE",
        doc="share one cargo target cache across worktrees",
        why="only read while linking a new worktree's target/ at worktree_add")
    build_heavy: list[str] = _k(
        "build", "heavy", [], HOT, env="SWARM_BUILD_HEAVY",
        doc="extra command patterns that always queue",
        why="every `swarm build` call reads it before classifying its command")
    build_light: list[str] = _k(
        "build", "light", [], HOT, env="SWARM_BUILD_LIGHT",
        doc="extra command patterns that skip the queue",
        why="every `swarm build` call reads it before classifying its command")

    # -- [gc] -------------------------------------------------------------
    # Nothing else prunes the build caches. Every 15 minutes, because a busy run
    # writes a whole superseded generation of a repo's units per phase.
    gc_auto: bool = _k(
        "gc", "auto", True, HOT, env="SWARM_GC_AUTO",
        doc="prune build output / dead mirrors automatically",
        why="the supervisor's gc scheduler reads it on every wake; a gc already"
            " running is left to finish")
    gc_every_s: int = _k(
        "gc", "every_s", 900, HOT, env="SWARM_GC_EVERY", minimum=0,
        doc="auto gc at most this often (s); 0 = idle-only",
        why="the last-run clock is compared against it on every wake")
    gc_idle_s: int = _k(
        "gc", "idle_s", 1800, HOT, env="SWARM_GC_IDLE", minimum=0,
        doc="also once per idle stretch this long (s); 0 = off",
        why="the idle episode's age is compared against it on every wake")
    # With several workers queued on one build slot the slot is almost never free
    # at the instant gc looks, so a gc that did not wait skipped most of its tries
    # and the disk guard's whole-tree eviction did the pruning.
    gc_wait_s: int = _k(
        "gc", "wait_s", 600, HOT, env="SWARM_GC_WAIT", minimum=0,
        doc="auto gc waits this long (s) in the build queue",
        why="read when the next automatic gc joins the build queue")
    # While it waits, builds pass it: a gc that held every waiter back for its
    # whole wait kept free slots idle behind one long build. But with two slots
    # under load the gate is never empty by itself, so for the end of the wait
    # it is passed no more, and the builds alive run out.
    gc_hold_s: int = _k(
        "gc", "hold_s", 120, HOT, env="SWARM_GC_HOLD", minimum=0,
        doc="of that wait, the last N s hold builds back",
        why="read when the next gc joins the build queue")
    # Three days keeps every dependency a phase in the current campaign built.
    gc_keep_days: int = _k(
        "gc", "keep_days", 3, HOT, env="SWARM_GC_KEEP_DAYS", minimum=1,
        doc="keep build output used within N days",
        why="read when the next gc builds its plan")
    gc_attic_days: int = _k(
        "gc", "attic_days", 30, HOT, env="SWARM_GC_ATTIC_DAYS", minimum=1,
        doc="keep set-aside work (swarm-attic refs) N days",
        why="read when the next gc builds its plan")

    # -- [backup] ---------------------------------------------------------
    backup_every_s: int = _k(
        "backup", "every_s", 1800, HOT, env="SWARM_BACKUP_EVERY", minimum=0,
        doc="back up unmerged work to origin every N s; 0 = off",
        why="the last-pass clock is compared against it on every wake; a pass"
            " already running is left to finish")
    backup_on_down: bool = _k(
        "backup", "on_down", True, HOT, env="SWARM_BACKUP_ON_DOWN",
        doc="push unmerged work to origin on `swarm down`",
        why="read by `swarm down` itself, which loads the file afresh")

    # -- [resources] ------------------------------------------------------
    resources_enabled: bool = _k(
        "resources", "enabled", True, HOT, env="SWARM_RESOURCES",
        doc="sample host, builds and workers into meters/",
        why="the sampler thread reads it before every step; off leaves the"
            " thread idle, on resumes it")
    resources_idle_s: int = _k(
        "resources", "idle_s", 600, HOT, env="SWARM_RESOURCES_IDLE", minimum=60,
        doc="warn when a build holds a slot idle this long (s)",
        why="the sampler reads it on every sample")
    resources_vhdx: str = _k(
        "resources", "vhdx", "", RESTART, env="SWARM_RESOURCES_VHDX",
        doc="WSL virtual disk to measure; \"\" = find it",
        why="the sampler resolves the virtual disk once, when the supervisor"
            " starts it")

    # -- [git] ------------------------------------------------------------
    git_isolation: str = _k(
        "git", "isolation", "none", RESTART, env="SWARM_GIT_ISOLATION",
        choices=("worktree", "none"),
        doc="worktree = own mirror + queue; none = in place",
        why="live worktrees and a populated merge queue only make sense under the"
            " isolation mode that created them")
    git_main_branch: str = _k(
        "git", "main_branch", "master", HOT, env="SWARM_GIT_MAIN", gate="in-flight",
        doc="branch the integrator merges phase branches into",
        why="the integrator reads it per merge — but a phase mirrored off the old"
            " main must not then be merged into a different one")
    # Default "*": every git repo directly under the project root (a monorepo of
    # repos); a single-repo project matches nothing and mirrors only the umbrella.
    git_repos: list[str] = _k(
        "git", "repos", ["*"], HOT, env="SWARM_GIT_REPOS", gate="repos-shrink",
        doc="globs picking the child repos a mirror includes",
        why="the repo set is globbed per worktree_add; adding is safe, but a repo"
            " dropped between a phase's worktree_add and its integrate is never"
            " visited, so its branch leaks and its commits never merge")
    git_auto_resolve: dict[str, str] = _k(
        "git", "auto_resolve", {}, HOT, kind=FROZEN, parse=_str_table,
        doc="glob -> union|keyed:<re>, tried before a resolver",
        why="gitq._auto_resolve reads the strategy table at the moment a conflict"
            " happens, so the next conflict uses the new rules — it cannot"
            " retroactively fix an integration that is already held; retry that one"
            " with `swarm resolved <phase>` once the tree is clean")
    git_auto_resolve_check: dict[str, str] = _k(
        "git", "auto_resolve_check", {}, HOT, kind=FROZEN, parse=_str_table,
        doc="glob -> command that must pass after one",
        why="gitq._auto_resolve reads the check table right after it settles a"
            " conflict, so the next automatic merge runs the new checks")
    git_post_merge: dict[str, str] = _k(
        "git", "post_merge", {}, HOT, kind=FROZEN, parse=_str_table,
        doc="repo -> command run on its main before a push",
        why="gitq._post_merge reads the table before every push of a merge, so"
            " the next one runs the new command")
    git_post_merge_if: dict[str, str] = _k(
        "git", "post_merge_if", {}, HOT, kind=FROZEN, parse=_str_table,
        doc="repo -> quick test: is post_merge needed?",
        why="gitq._post_merge reads the table before every push of a merge")
    git_post_merge_timeout_s: int = _k(
        "git", "post_merge_timeout_s", 300, HOT, minimum=1,
        doc="seconds a post_merge may queue, and then run",
        why="read by every post-merge command when it starts")

    # -- [lanes] ----------------------------------------------------------
    # Lane parallelism: rows that touch different files run at the same time. Off by
    # default, and off it writes nothing new to state.json, so a supervisor on
    # the previous release reads what this CLI writes.
    lanes_enabled: bool = _k(
        "lanes", "enabled", False, RESTART, env="SWARM_LANES",
        doc="schedule by each row's touches, not its repo",
        why="the running supervisor snapshots lanes only for phases it launched"
            " with them on; switching mid-run leaves phases in flight with no lane")
    lanes_per_repo: int = _k(
        "lanes", "per_repo", 2, HOT, minimum=1,
        doc="phases in flight at once in one repo",
        why="read by every launch decision")
    lanes_commons: list[str] = _k(
        "lanes", "commons", [], HOT,
        doc="globs any row may edit without declaring them",
        why="read by every launch decision and every landing")
    lanes_resources: list[str] = _k(
        "lanes", "resources", [], HOT,
        doc="non-file lanes a row may touch as @<name>",
        why="read by every launch decision")
    lanes_external: dict[str, str] = _k(
        "lanes", "external", {}, HOT, kind=FROZEN, parse=_str_table,
        doc="name -> path of a repo the swarm does not mirror",
        why="read by every launch decision")
    lanes_check: dict[str, str] = _k(
        "lanes", "check", {}, HOT, kind=FROZEN, parse=_str_table,
        doc='repo -> command a landing re-tests; "*" default',
        why="read by every landing, so the next one runs the new command")
    lanes_check_timeout_s: int = _k(
        "lanes", "check_timeout_s", 2700, HOT, minimum=1,
        doc="seconds before a landing check counts as red",
        why="read by every landing check when it starts")
    lanes_prepare: dict[str, str] = _k(
        "lanes", "prepare", {}, HOT, kind=FROZEN, parse=_str_table,
        doc="repo -> command run before its landing check",
        why="read by every landing check when it starts, so the next one runs"
            " the new command")
    lanes_prepare_if: dict[str, str] = _k(
        "lanes", "prepare_if", {}, HOT, kind=FROZEN, parse=_str_table,
        doc="repo -> quick test: is prepare needed?",
        why="read by every landing check when it starts")
    lanes_prepare_timeout_s: int = _k(
        "lanes", "prepare_timeout_s", 600, HOT, minimum=1,
        doc="seconds a prepare command may run",
        why="read by every landing check when it starts")

    # -- [batch] ----------------------------------------------------------
    # Batch claims: a slot takes up to max_rows related ready rows (one family,
    # one repo, a chain) and one lead session builds them in order (batcher.py).
    batch_enabled: bool = _k(
        "batch", "enabled", True, HOT, env="SWARM_BATCHING",
        doc="let one worker claim related ready rows",
        why="read each time a slot is filled")
    batch_min_rows: int = _k(
        "batch", "min_rows", 2, HOT, minimum=1,
        doc="fewest rows per batch when a partner fits",
        why="read each time a slot is filled")
    batch_max_rows: int = _k(
        "batch", "max_rows", 5, HOT, minimum=1,
        doc="rows per batch (at most 5)",
        why="read each time a slot is filled")
    batch_max_points: int = _k(
        "batch", "max_points", 60, HOT, minimum=1,
        doc="size cap of a batch, in row points",
        why="read each time a slot is filled")
    batch_max_repos: int = _k(
        "batch", "max_repos", 3, HOT, minimum=1,
        doc="code repos one batch may span",
        why="read each time a slot is filled")
    batch_model: str = _k(
        "batch", "model", "claude-sonnet-5-5", HOT,
        doc='model that picks a batch; "" = rule only',
        why="read each time a slot is filled")
    batch_timeout_s: int = _k(
        "batch", "timeout_s", 60, HOT, minimum=1,
        doc="seconds the batch model call may take",
        why="read each time a slot is filled")

    # -- [operator] -------------------------------------------------------
    # Positive opt-in: the only thing between a test suite and an autonomous
    # session holding the owner's authority. Off, the queue is never written.
    operator_enabled: bool = _k(
        "operator", "enabled", False, HOT, env="SWARM_OPERATOR",
        doc="arm autonomous sessions for `operator` finishes",
        why="the gate is read by each worker's own `swarm done` when it runs,"
            " from the project's file (not its mirror's copy)")
    operator_cmd: str = _k(
        "operator", "cmd", "", NEXT, env="SWARM_OPERATOR_CMD",
        doc='command an operator session runs; "" = built-in',
        why="read once, when a queued hand-off opens an operator pane; an"
            " already-open session keeps the command it was spawned with")
    operator_model: str = _k(
        "operator", "model", "claude-sonnet-5-5", NEXT,
        doc='model for an operator session; "" inherits',
        why="baked into the operator session's command line when its pane is"
            " spawned")
    operator_effort: str = _k(
        "operator", "effort", "high", NEXT, choices=("", *EFFORTS), parse=_effort,
        doc='operator --effort; "" = [worker].effort',
        why="baked into the operator session's command line when its pane is"
            " spawned")
    # An alias, never a dated build: triage runs unattended, and a pinned
    # snapshot's retirement would silently send every hand-off to `later`.
    operator_triage_model: str = _k(
        "operator", "triage_model", "haiku", HOT,
        doc="model that decides now vs later",
        why="triage is spawned by the worker's `swarm done`, which reads the"
            " project's file when it runs")
    # With the backlog deep and lanes on, a slot no launchable phase wants never
    # comes, so a `later` job had no other way out: live-box rolls waited
    # a long time. The session takes no worker slot, so this costs no build.
    operator_later_wait_s: int = _k(
        "operator", "later_wait_s", 10800, HOT, minimum=0,
        doc="seconds a `later` job waits at most; 0 = no cap",
        why="read by the supervisor's queue sweep on every wake")

    # -- [overseer] -------------------------------------------------------
    overseer_enabled: bool = _k(
        "overseer", "enabled", True, HOT, env="SWARM_OVERSEER",
        doc="run periodic Overseer review passes",
        why="the supervisor's trigger policy reads it on every wake; a pass"
            " already running is left to finish")
    overseer_cmd: str = _k(
        "overseer", "cmd", "", NEXT, env="SWARM_OVERSEER_CMD",
        doc='command an Overseer pass runs; "" = built-in',
        why="baked into the Overseer pane's command line when a pass is spawned")
    overseer_model: str = _k(
        "overseer", "model", "claude-sonnet-5-5", NEXT,
        doc='model for the Overseer; "" = master_model',
        why="baked into the Overseer session's command line when a pass is spawned")
    overseer_effort: str = _k(
        "overseer", "effort", "high", NEXT, choices=("", *EFFORTS), parse=_effort,
        doc='Overseer --effort; "" = [worker].effort',
        why="baked into the Overseer session's command line when a pass is spawned")
    overseer_min_gap_s: int = _k(
        "overseer", "min_gap_s", 600, HOT, env="SWARM_OVERSEER_MIN_GAP", minimum=0,
        doc="min seconds between non-urgent passes",
        why="read each time the policy asks whether a pass is due")
    overseer_every_s: int = _k(
        "overseer", "every_s", 14400, HOT, env="SWARM_OVERSEER_EVERY", minimum=0,
        doc="your summary this often (s); 0 = never",
        why="the summary clock is compared against it on every wake")
    overseer_owner_wait_s: int = _k(
        "overseer", "owner_wait_s", 3600, HOT, env="SWARM_OVERSEER_OWNER_WAIT", minimum=0,
        doc="owner-wait age that triggers a pass (s)",
        why="each waiting phase's age is compared against it on every wake")
    overseer_starve_s: int = _k(
        "overseer", "starve_s", 600, HOT, env="SWARM_OVERSEER_STARVE", minimum=0,
        doc="idle-slot starvation before a pass (s)",
        why="the starvation episode's age is compared against it on every wake")
    overseer_hold_wait_s: int = _k(
        "overseer", "hold_wait_s", 600, HOT, env="SWARM_OVERSEER_HOLD_WAIT", minimum=0,
        doc="merge hold a resolver has before a pass (s)",
        why="the held merge's age is compared against it on every wake")
    # A hung pass must never hold the pane forever: past this it is killed and
    # its committed work landed.
    overseer_timeout_s: int = _k(
        "overseer", "timeout_s", 2700, NEXT, env="SWARM_OVERSEER_TIMEOUT", minimum=1,
        doc="seconds before a hung pass is killed",
        why="a pass's deadline is fixed when it starts; the next pass gets the new one")

    # -- [usage] ----------------------------------------------------------
    usage_enabled: bool = _k(
        "usage", "enabled", True, HOT, env="SWARM_USAGE",
        doc="pause or stop the swarm at usage limits",
        why="a reload re-checks the caps at once; turning them off lifts a usage hold")
    usage_check_s: int = _k(
        "usage", "check_s", 600, HOT, env="SWARM_USAGE_CHECK", minimum=60,
        doc="seconds between usage checks",
        why="the last check's clock is compared against it on every wake")
    usage_stale_s: int = _k(
        "usage", "stale_s", 1800, HOT, env="SWARM_USAGE_STALE", minimum=300,
        doc="a reading older than this is not trusted (s)",
        why="read by every usage check")
    usage_rules: list[dict] = _k(
        "usage", "rules", USAGE_RULES_DEFAULT, HOT, kind=FROZEN, parse=_usage_rules,
        doc="window, percent and action of each cap",
        why="a reload re-checks the caps at once, so a raised limit lifts its hold")

    # -- [big_picture] ----------------------------------------------------
    big_picture_every: int = _k(
        "big_picture", "every", 10, HOT, env="SWARM_BIG_PICTURE_EVERY", minimum=0,
        doc="refresh every N integrated phases; 0 = off",
        why="the integrated-phase counter is compared against it on every wake")
    big_picture_max_age_h: int = _k(
        "big_picture", "max_age_h", 0, HOT, env="SWARM_BIG_PICTURE_MAX_AGE_H", minimum=0,
        doc="refresh a doc this many hours old; 0 = off",
        why="the doc's age is compared against it on every wake")
    big_picture_doc: str = _k(
        "big_picture", "doc", "docs/BIG-PICTURE.md", HOT,
        doc="the doc's path inside the project",
        why="read when a pass is briefed and when its doc lands; a pass already"
            " running lands at the new path")
    big_picture_model: str = _k(
        "big_picture", "model", "opus", NEXT,
        doc='model for a big-picture pass; "" inherits',
        why="baked into the session's command line when a pass is spawned")
    big_picture_cmd: str = _k(
        "big_picture", "cmd", "", NEXT, env="SWARM_BIG_PICTURE_CMD",
        doc='command a big-picture pass runs; "" = built-in',
        why="baked into the session's command line when a pass is spawned")

    # -- [tui] ------------------------------------------------------------
    tui_autostart: bool = _k(
        "tui", "autostart", True, RESTART, env="SWARM_TUI_AUTOSTART",
        doc="open this dashboard automatically at `swarm up`",
        why="the dashboard pane is created once, by session.setup at `swarm up`;"
            " there is no later moment a reload could reach")
    tui_cmd: str = _k(
        "tui", "cmd", "swarm tui", RESTART, env="SWARM_TUI_CMD",
        doc="command the dashboard pane is respawned with",
        why="the dashboard pane is respawned once, by session.setup at `swarm up`;"
            " a live dash keeps the command it was started with")

    # -- [console] --------------------------------------------------------
    # The owner's own Claude session, in the window beside the dashboard. Never a
    # worker: no phase marker, no slot, no reaper while the swarm runs.
    console_enabled: bool = _k(
        "console", "enabled", True, RESTART, env="SWARM_CONSOLE",
        doc="open the owner console window at `swarm up`",
        why="the console window is created once, by session.setup at `swarm up`;"
            " there is no later moment a reload could reach")
    console_prompt_file: str = _k(
        "console", "prompt_file", "", NEXT,
        doc='project primer appended to the console\'s; "" none',
        why="read each time the console's `claude` starts; a running session keeps"
            " the primer it started with")
    console_model: str = _k(
        "console", "model", "", NEXT,
        doc='model for the console session; "" inherits',
        why="baked into the console's command line each time its `claude` starts")
    console_cmd: str = _k(
        "console", "cmd", "", NEXT, env="SWARM_CONSOLE_CMD",
        doc='base command of the console; "" = claude',
        why="read each time the console's `claude` starts; a running session keeps"
            " the command it started with")

    # -- [web] ------------------------------------------------------------
    # The board is the machine's (one address for every swarm), so its host and
    # port are in ``machine.toml`` (:class:`machine.Settings`). What is a
    # project's own is whether this swarm is on it.
    web_enabled: bool = _k(
        "web", "enabled", True, RESTART, env="SWARM_WEB",
        doc="show this swarm on the machine's web board",
        why="`swarm up` decides whether to start the board, and the board reads what"
            " the supervisor recorded at its start")

    # -- (cli) ------------------------------------------------------------
    project_dir: Path = _k(
        CLI, "--project-dir", None, RESTART, kind=FROZEN,
        doc="the project root — --project-dir or the cwd",
        why="every worktree, lock and repo path is derived from it; the live"
            " worktrees are under the old one")

    state_dir: Path = field(init=False)
    #: The session the file asks for. It differs from :attr:`session` only while
    #: a live run is still in the session an earlier ``[swarm].name`` gave it;
    #: "" (a config not made by :func:`load`) means no such difference.
    session_wanted: str = field(init=False, default="", compare=False)

    def __post_init__(self) -> None:
        base = os.environ.get("SWARM_STATE_DIR")
        if base:
            self.state_dir = Path(base).expanduser()
        else:
            self.state_dir = machine.state_root() / self.slug

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "state.json.lock"

    @property
    def fifo_path(self) -> Path:
        return self.state_dir / "control.fifo"

    @property
    def done_dir(self) -> Path:
        return self.state_dir / "done"

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def supervisor_log(self) -> Path:
        return self.log_dir / "supervisor.log"

    @property
    def wt_dir(self) -> Path:
        """Where per-phase worktrees live (``isolation = worktree`` only)."""
        return self.state_dir / "wt"

    @property
    def git_lock_dir(self) -> Path:
        """Where per-repo integration ``flock`` files live."""
        return self.state_dir / "git"

    @property
    def buildsem_dir(self) -> Path:
        """Where the build gate lives: seats, slots, queue, lock and event log.
        In the machine directory, so every swarm on the machine queues at the
        one gate; it follows this run's state dir, as that directory does."""
        return machine.directory(self.state_dir) / "buildsem"

    # -- the machine's build limits (machine.toml [build]) -----------------
    # Read through to the file each time, never copied: one limit for the
    # machine means every process of every swarm sees the same one, and an edit
    # reaches a supervisor that has been up for a week as it reaches the next
    # `swarm build`.
    @property
    def machine(self) -> machine.Settings:
        """The machine's settings as ``machine.toml`` has them now."""
        return machine_settings()

    @property
    def build_max_concurrent(self) -> int:
        return self.machine.build_max_concurrent

    @property
    def build_short_s(self) -> int:
        return self.machine.build_short_s

    @property
    def build_overtake(self) -> int:
        return self.machine.build_overtake

    @property
    def build_idle_yield_s(self) -> int:
        return self.machine.build_idle_yield_s

    @property
    def build_idle_yield_max(self) -> int:
        return self.machine.build_idle_yield_max

    @property
    def build_pair(self) -> str:
        return self.machine.build_pair

    @property
    def build_alone(self) -> list[str]:
        return self.machine.build_alone

    @property
    def operator_dir(self) -> Path:
        """Where the durable operator hand-off queue lives (one JSON per phase)."""
        return self.state_dir / "operator"

    @property
    def tmp_dir(self) -> Path:
        """Per-session ``TMPDIR`` roots (``tmp/<phase|op-job|ovs-pass>``).

        On disk, beside the run, because ``/tmp`` can be a RAM-backed
        tmpfs: one worker's scratch cargo target there plus Claude Code's
        own diff cache can fill it and push swap to the limit."""
        return self.state_dir / "tmp"

    def session_tmp(self, name: str) -> Path | None:
        """``tmp/<name>`` for one session, or ``None`` for a name that could
        escape :attr:`tmp_dir` (it is created and later ``rmtree``'d)."""
        if not name or "/" in name or name in (".", ".."):
            return None
        return self.tmp_dir / name

    @property
    def build_cache_dir(self) -> Path:
        """Shared, per-repo cargo ``target`` cache (symlinked into each worktree),
        so unchanged crates aren't recompiled from scratch in every worktree."""
        return self.state_dir / "cache" / "target"

    def ensure_dirs(self) -> None:
        """Create the state/done/log directories if absent."""
        for d in (self.state_dir, self.done_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)


#: Every setting, by Config field name, in form order.
SETTINGS: dict[str, Setting] = {
    f.name: replace(f.metadata["setting"], name=f.name) for f in fields(Config) if f.init
}


#: The keys that are the machine's, as ``(table, key)``: every one
#: ``machine.toml`` declares. Most were a project's once (the build gate's
#: limits, where the web board listens, the Telegram sender) and each is now one
#: thing for every swarm on the machine.
MACHINE_KEYS = tuple((k.table, k.key) for k in machine.keys())

# The last reading of machine.toml: what the file was (path, mtime, size), and
# what it said, or the error it gave.
_machine_read: tuple[tuple, machine.Settings | None, machine.SettingsError | None] | None = None


def machine_settings(sound: bool = False) -> machine.Settings:
    """The machine's settings as ``machine.toml`` has them now. The file is
    read again whenever it has changed, so a process that stays up follows an
    edit without a reload.

    A file that does not read as settings raises :class:`machine.SettingsError`.
    :func:`load` asks for a ``sound`` file, so every command, and a reload, says
    so before it does anything. Otherwise a process that had read the file while
    it was sound keeps what it read then: a supervisor must not fall over, or
    drop to the defaults, because of a typo made while it runs."""
    global _machine_read
    path = machine.settings_path()
    try:
        st = path.stat()
        stamp = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = (str(path), 0, -1)
    seen = _machine_read
    if seen is None or seen[0] != stamp:
        try:
            seen = (stamp, machine.settings(path), None)
        except machine.SettingsError as exc:
            kept = seen[1] if seen is not None and seen[0][0] == stamp[0] else None
            seen = (stamp, kept, exc)
        _machine_read = seen
    if seen[2] is not None and (sound or seen[1] is None):
        raise seen[2]
    return seen[1]


def _refuse_machine_keys(cfg_file: Path, data: dict) -> None:
    """A project file that still sets one of the machine's keys is an error,
    never a key read from the wrong place or passed over in silence: the owner
    who wrote ``max_concurrent = 1`` or ``port = 8780`` there believes it is in
    force."""
    found = [f"[{table}].{key}" for table, key in MACHINE_KEYS
             if isinstance(data.get(table), dict) and key in data[table]]
    if not found:
        return
    one = len(found) == 1
    raise ValueError(
        f"{cfg_file}: {', '.join(found)} {'is' if one else 'are'} not a project's to set."
        f" {'It is' if one else 'They are'} the same for every swarm on this machine (one"
        f" build gate, one web board, one Telegram bot), so {'it is a machine setting' if one else 'they are machine settings'}:"
        f" move {'it' if one else 'them'} to the same table of {machine.settings_path()}"
        f" and delete {'it' if one else 'them'} here")


def _find_config_file(explicit: str | None, project_dir: Path) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    candidate = project_dir / CONFIG_NAME
    return candidate if candidate.is_file() else None


def load(explicit: str | None = None, project_dir: str | None = None) -> Config:
    """Load config from ``.swarm.toml`` (if present) plus env overrides.

    ``project_dir`` defaults to the current working directory. Missing tables
    fall back to documented defaults so the tool works with a minimal file.
    """
    pdir = Path(project_dir).expanduser().resolve() if project_dir else Path.cwd()
    cfg_file = _find_config_file(explicit, pdir)
    data: dict = {}
    if cfg_file is not None:
        with cfg_file.open("rb") as fh:
            data = tomllib.load(fh)
        _refuse_machine_keys(cfg_file, data)
    machine_settings(sound=True)  # a machine.toml that does not read stops every command
    values = {name: s.read(data.get(s.table, {}), pdir)
              for name, s in SETTINGS.items() if s.table != CLI}
    values["name"] = values["name"].strip() or pdir.name
    follows = ("session" not in data.get("tmux", {})
               and os.environ.get(SETTINGS["session"].env) is None)
    if follows:
        values["session"] = session_default(values["name"])
    if values["slug"] == machine.DIR_NAME:
        raise ValueError(f"[swarm].slug must not be {machine.DIR_NAME!r}: that folder of the"
                         " state root is the machine's own, shared by every swarm")
    cfg = Config(project_dir=pdir, **values)
    _refuse_other_state(cfg)
    cfg.session_wanted = cfg.session
    if follows:
        cfg.session = _live_session(cfg) or cfg.session
    return cfg


def recorded(state_dir: Path) -> dict:
    """What the last supervisor of the run in ``state_dir`` recorded about it
    (``config.json``: every setting it ran on, ``project_dir`` among them);
    ``{}`` when no supervisor ever started there."""
    try:
        last = json.loads((state_dir / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return last if isinstance(last, dict) else {}


def _refuse_other_state(cfg: Config) -> None:
    """Refuse to pair a project with another project's run state.

    A state dir belongs to the project its last supervisor recorded there
    (:func:`recorded`), for as long as that project's folder exists: a project
    that was moved with its ``[swarm].slug`` pinned takes its state along.

    Every session a swarm launches carries its ``SWARM_STATE_DIR``, and that
    variable decides where a command reads and writes whatever project the
    command names. So ``swarm --project-dir B down`` typed in a session of swarm
    A would stop A under B's name. Two projects given one slug by hand share a
    state dir the same way. Either is refused before the command touches
    anything.
    """
    owner = recorded(cfg.state_dir).get("project_dir")
    if not isinstance(owner, str) or not owner:
        return
    was = Path(owner)
    if not was.is_dir() or was.resolve() == cfg.project_dir.resolve():
        return
    if os.environ.get("SWARM_STATE_DIR"):
        raise WrongSwarm(
            f"this command names the project {cfg.project_dir}, but SWARM_STATE_DIR"
            f" ({cfg.state_dir}) is the run state of {was}. A session of one swarm cannot"
            " act on another (`swarm ls` shows how the others stand): run the command"
            " from a shell outside the swarm, or with"
            " `env -u SWARM_STATE_DIR -u SWARM_PROJECT`")
    raise WrongSwarm(
        f"the project {cfg.project_dir} has the slug {cfg.slug!r}, and so has {was}, whose"
        f" run state is in {cfg.state_dir}. Two swarms cannot share a state dir: give one"
        " of them another `slug` under [swarm] in its .swarm.toml")


def _live_session(cfg: Config) -> str | None:
    """The tmux session a running swarm is in, when it is not the one the name
    gives now; None when there is no such session.

    A session cannot be renamed under a run: every pane and window id in
    ``state.json`` is in it. So after ``[swarm].name`` changes, the session the
    last supervisor recorded (``config.json``) stays this run's for as long as
    tmux still has it carrying this run's state dir, and every ``swarm`` command
    keeps addressing it: ``swarm down`` ends it, and the ``swarm up`` after that
    creates the new one.
    """
    if cfg.driver != "tmux":
        return None
    was = recorded(cfg.state_dir).get("session")
    if not isinstance(was, str) or not was or was == cfg.session:
        return None
    try:
        owner = tmux.session_owner(was)
    except (OSError, subprocess.SubprocessError):
        return None
    return was if owner == str(cfg.state_dir) else None


def find_project(project_dir: str | None = None, explicit: str | None = None) -> Path:
    """The project a ``swarm`` command runs on; :class:`NoProject` when there is
    none, with where it looked.

    ``--project-dir`` names it, else the session the command runs in
    (``SWARM_PROJECT``), else the folder it is typed in. A named project is
    taken as it is: the directory must be there and hold a ``.swarm.toml``
    (``--config`` names another file for it). From the cwd the project is the
    nearest folder that holds one, this one or one above it, so a command typed
    in a subfolder or a component repo still reaches its project.

    A folder with no ``.swarm.toml`` is not a project. Loading one anyway gave
    every setting its default and a state dir of its own: a command one folder
    off, in a renamed folder or with a mistyped ``--project-dir`` addressed a
    new, empty swarm in silence, and a ``swarm build`` there ran on a build gate
    of its own.

    A session's cwd is wherever its work is: its mirror, a component repo
    inside the mirror, a checkout of an external repo. None of those is its
    project (a mirror's ledger is the copy branched at launch), which is why a
    session is told (:func:`launch.session_env`) and never searches.
    """
    file = Path(explicit).expanduser() if explicit else None
    if file is not None and not file.is_file():
        raise NoProject(f"--config names {file}, which is not a file")
    how = "--project-dir" if project_dir else "SWARM_PROJECT"
    named = project_dir or os.environ.get("SWARM_PROJECT")
    if named:
        path = Path(named).expanduser()
        if not path.is_dir():
            raise NoProject(f"{how} names {path}, which is not a directory")
        path = path.resolve()
        if file is None and not (path / CONFIG_NAME).is_file():
            raise NoProject(
                f"{how} names {path}, which holds no {CONFIG_NAME}: it is not a swarm"
                " project (a named project is taken as it is; the folders above it are"
                " not searched)")
        return path
    cwd = Path.cwd()
    if file is not None:
        return cwd
    for folder in (cwd, *cwd.parents):
        if (folder / CONFIG_NAME).is_file():
            return folder
    raise NoProject(
        f"no {CONFIG_NAME} in {cwd} or in any folder above it: this is not a swarm"
        " project. Run the command in the project, or name it with --project-dir"
        " (`swarm ls` lists the swarms on this machine)")


def claude_version() -> str:
    """Best-effort ``claude --version`` token (e.g. ``2.1.201``), else ''."""
    try:
        r = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    parts = (r.stdout or "").split()
    return parts[0] if parts else ""


def ready_needle(cfg: Config) -> str:
    """A string meaning "claude has booted" in a pane.

    An explicit ``ready_marker`` wins (tests inject a fake banner via
    ``SWARM_READY_MARKER``). Otherwise match the running claude version — the
    boot banner prints ``Claude Code vX.Y.Z`` — falling back to the
    always-present ``Claude Code``.
    """
    if cfg.ready_marker:
        return cfg.ready_marker
    return claude_version() or "Claude Code"
