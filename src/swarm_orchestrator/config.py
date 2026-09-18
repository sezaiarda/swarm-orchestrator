"""Configuration loading for swarm-orchestrator.

Reads a per-project ``.swarm.toml`` (stdlib ``tomllib``), applies defaults, and
resolves the runtime state directory under ``~/.local/state``. Test/real seams
(driver, fake commands, telegram sink, readiness marker) are honoured via
environment variables so the hermetic tests never touch tmux or ``claude``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .tmux import AUTO_LAYOUT, normalize_layout

DEFAULT_EXCLUDE: list[str] = []


def _slugify(name: str) -> str:
    """Turn a project directory name into a filesystem-safe slug."""
    keep = [c.lower() if c.isalnum() else "-" for c in name]
    slug = "".join(keep).strip("-")
    return slug or "project"


_REPO_NOTIFY = Path(__file__).resolve().parent.parent.parent / "scripts" / "notify.sh"


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


@dataclass
class Config:
    """Resolved configuration + runtime paths for one project."""

    project_dir: Path
    slug: str
    max_workers: int
    master_model: str
    command_template: str
    command_file: str
    env_marker: str
    done_hook: str
    worker_cmd: str
    ready_marker: str
    worker_settings: str
    worker_effort: str
    done_grace_s: int
    park_after: int
    ledger: str
    roadmap: str
    exclude: list[str]
    telegram_notify: str
    tui_autostart: bool
    tui_cmd: str
    session: str
    tmux_layout: str
    driver: str
    master_cmd: str
    resolver_cmd: str
    watchdog_s: int
    git_isolation: str
    git_main_branch: str
    git_repos: list[str]
    git_auto_resolve: dict[str, str]
    build_max_concurrent: int
    build_jobs: int
    build_cache: bool
    operator_enabled: bool
    operator_cmd: str
    operator_model: str
    operator_triage_model: str
    state_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        base = os.environ.get("SWARM_STATE_DIR")
        if base:
            self.state_dir = Path(base).expanduser()
        else:
            root = Path(
                os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
            )
            self.state_dir = root / "swarm-orchestrator" / self.slug

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
        """Where the ``swarm build`` semaphore slot files live (one flock each)."""
        return self.state_dir / "buildsem"

    @property
    def operator_dir(self) -> Path:
        """Where the durable operator hand-off queue lives (one JSON per phase)."""
        return self.state_dir / "operator"

    @property
    def build_cache_dir(self) -> Path:
        """Shared, per-repo cargo ``target`` cache (symlinked into each worktree),
        so unchanged crates aren't recompiled from scratch in every worktree."""
        return self.state_dir / "cache" / "target"

    def ensure_dirs(self) -> None:
        """Create the state/done/log directories if absent."""
        for d in (self.state_dir, self.done_dir, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)


def _find_config_file(explicit: str | None, project_dir: Path) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    candidate = project_dir / ".swarm.toml"
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

    swarm = data.get("swarm", {})
    worker = data.get("worker", {})
    tasks = data.get("tasks", {})
    telegram = data.get("telegram", {})
    tui = data.get("tui", {})
    tmux = data.get("tmux", {})
    git = data.get("git", {})
    build = data.get("build", {})
    operator = data.get("operator", {})

    driver = os.environ.get("SWARM_DRIVER", swarm.get("driver", "tmux"))
    max_workers = int(swarm.get("max_workers", 4))
    if max_workers < 1:
        # A swarm with < 1 slots can never claim a phase and would stall in
        # silence. One slot is the floor; anything below it is a misconfig.
        raise ValueError(f"[swarm].max_workers must be >= 1, got {max_workers}")
    return Config(
        project_dir=pdir,
        slug=os.environ.get("SWARM_SLUG", swarm.get("slug", _default_slug(pdir))),
        max_workers=max_workers,
        master_model=str(swarm.get("master_model", "")),
        command_template=str(worker.get("command_template", "/prime {phase}")),
        command_file=str(worker.get("command_file", ".claude/commands/prime.md")),
        env_marker=str(worker.get("env_marker", "SWARM_PHASE")),
        done_hook=str(worker.get("done_hook", 'swarm done "$SWARM_PHASE" ok')),
        worker_cmd=os.environ.get(
            "SWARM_WORKER_CMD", worker.get("worker_cmd", "claude -n worker:{phase}")
        ),
        ready_marker=os.environ.get(
            # "" => auto: match the running claude version (see ready_needle).
            # An explicit value (config or SWARM_READY_MARKER) overrides.
            "SWARM_READY_MARKER", worker.get("ready_marker", "")
        ),
        worker_settings=os.environ.get(
            # Merged over user settings on each worker's `claude` (see
            # _worker_shell) so a worker's own teammates run in-process — no
            # extra tmux panes. Set to "" to disable (e.g. a non-claude worker).
            "SWARM_WORKER_SETTINGS",
            worker.get("worker_settings", '{"teammateMode":"in-process"}'),
        ),
        # `claude --effort` for every worker. Pinned rather than inherited: a
        # worker otherwise runs at whatever ~/.claude/settings.json says today,
        # and an owner who bumps their own sessions to xhigh would silently make
        # every phase ~35-60% dearer. "" = inherit the user's setting.
        worker_effort=_effort(os.environ.get("SWARM_WORKER_EFFORT", worker.get("effort", "high"))),
        # Seconds a worker holds its slot after signalling `done` before the
        # supervisor is poked to reclaim it (a "finish buffer" so the worker can
        # flush last work). 0 (the default) = advance immediately, unchanged
        # behaviour. The delay lives in the worker's own `swarm done`, so the
        # single-threaded supervisor loop never blocks.
        done_grace_s=_int_env(
            "SWARM_DONE_GRACE", worker.get("done_grace_s"), 0, minimum=0
        ),
        # Seconds a worker may sit `swarm waiting` on the owner before the
        # supervisor parks it (moves its live pane to its own window and frees the
        # grid slot for a replacement). 120 (the default) balances "give the owner
        # a chance to answer in place" against "don't strand a slot"; 0 disables
        # parking entirely (a waiting worker just holds its slot as before).
        park_after=_int_env(
            "SWARM_PARK_AFTER", worker.get("park_after"), 120, minimum=0
        ),
        ledger=str(tasks.get("ledger", "docs/PHASE-LEDGER.md")),
        roadmap=str(tasks.get("roadmap", "docs/ROADMAP-MASTER.md")),
        exclude=list(tasks.get("exclude", DEFAULT_EXCLUDE)),
        tui_autostart=_bool_env("SWARM_TUI_AUTOSTART", tui.get("autostart", True)),
        tui_cmd=os.environ.get("SWARM_TUI_CMD", str(tui.get("cmd", "swarm tui"))),
        telegram_notify=str(
            # The swarm's OWN sender, resolved from this package: a default
            # that pointed at some other bot would put two audiences on one
            # channel.
            telegram.get("notify", str(_REPO_NOTIFY))
        ),
        session=os.environ.get(
            "SWARM_SESSION",
            # Default to the project's own name (``myproject``), not a generic
            # ``swarm``: the session name is what you see in `tmux ls` and in the
            # status bar, and one box can host several runs at once. An explicit
            # [tmux].session still wins.
            tmux.get("session", _slugify(pdir.name) or "swarm"),
        ),
        # How the worker windows arrange their slot panes ("auto" = the historic
        # 1-full / 2-side-by-side / 3-4-tiled rule). `swarm layout <name>` flips
        # it live for the running session; this is the boot default.
        tmux_layout=normalize_layout(
            str(os.environ.get("SWARM_LAYOUT", tmux.get("layout", AUTO_LAYOUT)))
        ),
        driver=driver,
        master_cmd=os.environ.get("SWARM_MASTER_CMD", swarm.get("master_cmd", "")),
        resolver_cmd=os.environ.get(
            "SWARM_RESOLVER_CMD", swarm.get("resolver_cmd", "")
        ),
        # Seconds between the supervisor's liveness reconcile sweeps: frees a slot
        # whose worker pane died, re-nudges an idle master, finishes a settled run.
        # 0 disables it and restores the purely event-driven loop the module
        # docstring describes. It defaults ON because the alternative is the
        # measured failure: a supervisor that exited and stayed down for hours
        # with two slots marked busy and nothing able to notice.
        watchdog_s=_int_env("SWARM_WATCHDOG", swarm.get("watchdog_s"), 300, minimum=0),
        git_isolation=os.environ.get(
            # "none" (default) == today's behavior: workers commit main in place.
            # "worktree" opts into isolated worktrees + the serialized merge-queue.
            "SWARM_GIT_ISOLATION", str(git.get("isolation", "none"))
        ),
        # path-glob -> "union" | "keyed:<regex>". Tried before a resolver session
        # is spawned; anything unmatched or genuinely conflicting falls through.
        git_auto_resolve={
            str(k): str(v) for k, v in (git.get("auto_resolve") or {}).items()
        },
        git_main_branch=os.environ.get(
            "SWARM_GIT_MAIN", str(git.get("main_branch", "master"))
        ),
        git_repos=_git_repos(git),
        build_max_concurrent=_int_env(
            "SWARM_BUILD_MAX", build.get("max_concurrent"), 2, minimum=0
        ),
        build_jobs=_int_env("SWARM_BUILD_JOBS", build.get("jobs"), 6, minimum=0),
        build_cache=_bool_env("SWARM_BUILD_CACHE", build.get("cache", True)),
        # Positive opt-in, and the only thing between a test suite and an
        # autonomous session holding the owner's full authority. False means the
        # queue is never even written -- a queue nothing will drain is worse than
        # no queue, because it looks like the hand-off was recorded.
        operator_enabled=_bool_env("SWARM_OPERATOR", operator.get("enabled", False)),
        operator_cmd=os.environ.get(
            "SWARM_OPERATOR_CMD", str(operator.get("cmd", ""))
        ),
        operator_model=str(operator.get("model", "")),
        # An ALIAS, never a dated build (recap.MODEL says why): triage runs
        # unattended on every operator finish, so pinning it to a snapshot means
        # the day that snapshot retires every hand-off silently falls to `later`.
        # Spelled out rather than imported from `recap` -- recap imports us.
        operator_triage_model=str(operator.get("triage_model", "claude-haiku-4-5")),
    )


#: What ``claude --effort`` accepts (CLI 2.1.276). "" means pass nothing.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _effort(value: object) -> str:
    """A validated ``[worker].effort``; a typo fails at load, not in every pane."""
    level = str(value or "").strip().lower()
    if level and level not in EFFORTS:
        raise ValueError(f"[worker].effort must be one of {', '.join(EFFORTS)} or \"\", got {value!r}")
    return level


def _int_env(name: str, value: object, default: int, minimum: int) -> int:
    """An int config value, floored at ``minimum``: the env override, else the
    config value, else the code default — the first that parses. ``default`` is
    always a valid int, so a malformed env override *or* a wrong-type config
    value degrades to it instead of crashing ``load()`` for every command."""
    for candidate in (os.environ.get(name), value, default):
        if candidate is None:
            continue
        try:
            return max(minimum, int(candidate))
        except (TypeError, ValueError):
            continue
    return max(minimum, default)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def _git_repos(git: dict) -> list[str]:
    """Globs (relative to the project root) that select the component repos a
    phase's worktree mirror should include, besides the umbrella itself.

    Default ``["*"]`` = every independent git repo that is a direct child of the
    project root (matches a monorepo-of-repos like myproject). Set explicitly for
    nested layouts, e.g. ``repos = ["*", "packages/*"]``. ``SWARM_GIT_REPOS`` (a
    comma-separated list) overrides for tests/one-offs. A single-repo project
    simply matches nothing here and gets a one-repo (umbrella-only) mirror.
    """
    env = os.environ.get("SWARM_GIT_REPOS")
    if env is not None:
        return [s.strip() for s in env.split(",") if s.strip()]
    return [str(p) for p in git.get("repos", ["*"])]


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
