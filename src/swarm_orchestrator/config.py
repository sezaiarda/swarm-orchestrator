"""Configuration loading for swarm-orchestrator.

Reads a per-project ``.swarm.toml`` (stdlib ``tomllib``), applies defaults, and
resolves the runtime state directory under ``~/.local/state``. Test/real seams
(driver, fake commands, telegram sink, readiness marker) are honoured via
environment variables so the hermetic tests never touch tmux or ``claude``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_EXCLUDE: list[str] = []


def _slugify(name: str) -> str:
    """Turn a project directory name into a filesystem-safe slug."""
    keep = [c.lower() if c.isalnum() else "-" for c in name]
    slug = "".join(keep).strip("-")
    return slug or "project"


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
    ledger: str
    roadmap: str
    exclude: list[str]
    telegram_notify: str
    session: str
    driver: str
    master_cmd: str
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
    tmux = data.get("tmux", {})

    driver = os.environ.get("SWARM_DRIVER", swarm.get("driver", "tmux"))
    return Config(
        project_dir=pdir,
        slug=os.environ.get("SWARM_SLUG", swarm.get("slug", _slugify(pdir.name))),
        max_workers=int(swarm.get("max_workers", 4)),
        master_model=str(swarm.get("master_model", "")),
        command_template=str(worker.get("command_template", "/prime {phase}")),
        command_file=str(worker.get("command_file", ".claude/commands/prime.md")),
        env_marker=str(worker.get("env_marker", "SWARM_PHASE")),
        done_hook=str(worker.get("done_hook", 'swarm done "$SWARM_PHASE" ok')),
        worker_cmd=os.environ.get(
            "SWARM_WORKER_CMD", worker.get("worker_cmd", "claude -n worker:{phase}")
        ),
        ready_marker=os.environ.get(
            "SWARM_READY_MARKER", worker.get("ready_marker", "? for shortcuts")
        ),
        ledger=str(tasks.get("ledger", "docs/PHASE-LEDGER.md")),
        roadmap=str(tasks.get("roadmap", "docs/ROADMAP-MASTER.md")),
        exclude=list(tasks.get("exclude", DEFAULT_EXCLUDE)),
        telegram_notify=str(
            telegram.get("notify", "scripts/notify.sh")
        ),
        session=os.environ.get("SWARM_SESSION", tmux.get("session", "swarm")),
        driver=driver,
        master_cmd=os.environ.get("SWARM_MASTER_CMD", swarm.get("master_cmd", "")),
    )
