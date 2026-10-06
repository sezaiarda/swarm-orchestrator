"""The machine layer: what describes this computer rather than one project.

Several swarms can run on one machine. Each has its own project, its own
``.swarm.toml`` and its own state dir. Three things belong to none of them, and
this module owns all three:

* **The machine directory**, ``<state root>/machine/`` (:func:`directory`): run
  state every swarm on the machine shares. It sits in the state root, beside
  the swarms' own state dirs, so whatever moves the state root moves it too.
* **The machine settings**, ``$XDG_CONFIG_HOME/swarm-orchestrator/machine.toml``
  (:class:`Settings`, :func:`settings`): what is true of the box whichever
  project asks. A project's ``.swarm.toml`` never holds these.
* **The registry** (:func:`swarms`): every swarm on the machine, read from the
  state root. No file lists them; a state dir is the record, so a swarm cannot
  be running and missing from the list.

Importing this module pulls in no other part of the package: ``config`` resolves
the state root through it. The registry reads each swarm through the modules
that own what it reads, imported when it is asked.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

#: The folder name the tool uses under ``$XDG_STATE_HOME`` and ``$XDG_CONFIG_HOME``.
APP = "swarm-orchestrator"
#: The folder in the state root that is the machine's own, never a swarm's.
DIR_NAME = "machine"
#: The machine settings file, in ``$XDG_CONFIG_HOME/swarm-orchestrator``.
SETTINGS_NAME = "machine.toml"


# -- where things are ---------------------------------------------------------
def state_root() -> Path:
    """Where the swarms on this machine keep their runs, one state dir each:
    ``$XDG_STATE_HOME/swarm-orchestrator`` (``~/.local/state/…`` by default).

    Inside a session it is the folder that session's own state dir is in
    (``SWARM_STATE_DIR``). The two are one place unless that variable was
    pointed elsewhere by hand; then the run is there, and so is what it shares.
    """
    named = os.environ.get("SWARM_STATE_DIR")
    if named:
        return Path(named).expanduser().parent
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base).expanduser() / APP


def directory(state_dir: Path | None = None) -> Path:
    """The machine directory: of this machine, or (given a swarm's ``state_dir``)
    the one beside that swarm's state, which is the same place for every swarm
    on the machine. Not created here; whoever keeps something in it makes it."""
    root = Path(state_dir).parent if state_dir is not None else state_root()
    return root / DIR_NAME


def settings_path() -> Path:
    """Where the machine settings are read from."""
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base).expanduser() / APP / SETTINGS_NAME


# -- the settings -------------------------------------------------------------
class SettingsError(ValueError):
    """``machine.toml`` does not read as machine settings; the message names the
    file and the key."""


@dataclass(frozen=True)
class Key:
    """One ``machine.toml`` key: where it is, its default and what it is for.

    The default's type is the key's type (bool, int, str, or a list of
    strings). ``env`` names a variable that, when set, beats the file."""

    table: str
    key: str
    default: Any
    doc: str
    env: str | None = None
    minimum: int | None = None
    choices: tuple[str, ...] = ()
    name: str = ""  # the Settings field; filled in from the class

    @property
    def where(self) -> str:
        return f"[{self.table}].{self.key}"

    def read(self, table: dict, source: str) -> Any:
        """This key's value: the env override, else the file's, else the default.
        A value of the wrong type or out of range is an error, never a default:
        a limit that quietly does not apply is worse than a command that stops."""
        raw = os.environ.get(self.env) if self.env else None
        if raw is not None:
            return self._check(self._parse(raw), f"{self.env}={raw!r}")
        if self.key not in table:
            return list(self.default) if isinstance(self.default, list) else self.default
        return self._check(table[self.key], f"{source}: {self.where}")

    def _parse(self, raw: str) -> Any:
        if isinstance(self.default, bool):
            return raw.strip().lower() not in ("", "0", "false", "no", "off")
        if isinstance(self.default, int):
            try:
                return int(raw)
            except ValueError:
                return raw  # refused by _check, in its own words
        if isinstance(self.default, list):
            return [s.strip() for s in raw.split(",") if s.strip()]
        return raw

    def _check(self, value: Any, what: str) -> Any:
        kind = type(self.default)
        ok = isinstance(value, kind) and not (kind is int and isinstance(value, bool))
        if ok and kind is list:
            ok = all(isinstance(v, str) for v in value)
        if not ok:
            want = "a list of strings" if kind is list else {
                bool: "true or false", int: "a whole number", str: "a string"}[kind]
            raise SettingsError(f"{what} must be {want}, got {value!r}")
        if self.minimum is not None and value < self.minimum:
            raise SettingsError(f"{what} must be at least {self.minimum}, got {value!r}")
        if self.choices and value not in self.choices:
            raise SettingsError(
                f"{what} must be one of {', '.join(self.choices)}, got {value!r}")
        return list(value) if kind is list else value


def setting(table: str, key: str, default: Any, doc: str, **rest: Any) -> Any:
    """A :class:`Settings` field declaring its :class:`Key`."""
    declared = {"metadata": {"key": Key(table, key, default, doc, **rest)}}
    if isinstance(default, list):
        return field(default_factory=lambda: list(default), **declared)
    return field(default=default, **declared)


@dataclass(frozen=True)
class Settings:
    """The machine's settings: one field per key, grouped by table, in the
    order ``docs/config.md`` lists them.

    Adding one is a field here, declared with :func:`setting`, and a row in the
    machine table of ``docs/config.md`` (a test holds the two together). The
    loader, the default, the type check and the error message all follow from
    the declaration; nothing else lists the keys.

    """

    # -- [build]: the one build gate every swarm on the machine queues at ----
    build_max_concurrent: int = setting(
        "build", "max_concurrent", 2,
        "heavy `swarm build` runs at once on this machine, whichever swarm"
        " started them; 0 = no gate", minimum=0)
    build_short_s: int = setting(
        "build", "short_s", 60,
        "a build that usually runs at most this long (s) is short", minimum=0)
    build_overtake: int = setting(
        "build", "overtake", 2,
        "short builds that may pass a long one; 0 = first come, first served", minimum=0)
    build_idle_yield_s: int = setting(
        "build", "idle_yield_s", 150,
        "a holder idle this long (s) stops counting; 0 = off", minimum=0)
    build_idle_yield_max: int = setting(
        "build", "idle_yield_max", 2,
        "most idle holders set aside at once", minimum=0)
    build_pair: str = setting(
        "build", "pair", "any",
        "which builds may run side by side", choices=("any", "distinct-repo"))
    build_alone: list[str] = setting(
        "build", "alone",
        ["docker", "docker-compose", "docker-buildx", "podman", "podman-compose",
         "buildah", "nerdctl", "buildctl"],
        "commands no build runs beside, under pair = \"distinct-repo\"")

    # -- [web]: the one board that shows every swarm on the machine ----------
    # One address and one port, whichever swarm asks, so neither has a
    # variable: a port one shell could set for itself would send that shell's
    # commands looking for the board where it is not. Open to the network by
    # the owner's choice: it serves computed JSON only and redacts anything
    # secret-shaped.
    web_host: str = setting(
        "web", "host", "0.0.0.0", "the address the board binds; 0.0.0.0 is every interface")
    web_port: int = setting(
        "web", "port", 8765, "the port the machine's one board listens on", minimum=1)

    # -- [telegram]: the one bot that speaks for every swarm on the machine --
    # One bot, so one sender and one credentials file, and neither has a
    # variable: a file one shell named for itself would have that shell's
    # swarm send from another bot than the one the machine's listener polls.
    telegram_notify: str = setting(
        "telegram", "notify", "",
        "the script that sends every swarm's messages, called with the message as"
        " $1; \"\" = the bundled scripts/notify.sh")
    telegram_env: str = setting(
        "telegram", "env", "",
        "the file the bot's token and chat id are in; \"\" = .env in the folder"
        " above the script's")


def keys(schema: type = Settings) -> list[Key]:
    """Every key ``schema`` declares, in order."""
    return [replace(f.metadata["key"], name=f.name) for f in fields(schema)]


def settings(path: Path | None = None, schema: type = Settings) -> Any:
    """The machine settings: ``machine.toml`` (:func:`settings_path`, or
    ``path``) over the defaults. A missing file is all defaults.

    Raises :class:`SettingsError` for a file that is not TOML, a value of the
    wrong type, and a table or key the schema does not have. The last is
    stricter than ``.swarm.toml`` on purpose: this file holds the limits that
    protect the machine, and a mistyped key would otherwise leave the default
    in force with nothing to say so.
    """
    path = settings_path() if path is None else Path(path)
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        data = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"{path}: {exc}") from exc
    declared = keys(schema)
    tables: dict[str, set[str]] = {}
    for key in declared:
        tables.setdefault(key.table, set()).add(key.key)
    for table, body in data.items():
        if table not in tables or not isinstance(body, dict):
            raise SettingsError(
                f"{path}: [{table}] is not a table of {SETTINGS_NAME}"
                f" (it has: {_listing(f'[{t}]' for t in tables)})")
        for name in body:
            if name not in tables[table]:
                raise SettingsError(
                    f"{path}: [{table}].{name} is not a machine setting"
                    f" ([{table}] has: {_listing(tables[table])})")
    return schema(**{key.name: key.read(data.get(key.table, {}), str(path))
                     for key in declared})


def _listing(names) -> str:
    return ", ".join(sorted(names)) or "none yet"


# -- the registry -------------------------------------------------------------
#: What a state dir holds from the first command that wrote to it. A folder in
#: the state root with none of them is not a swarm's.
_MARKS = ("config.json", "state.json", "state.json.lock", "done", "logs", "buildsem")

# What `Swarm.status` says.
RUNNING = "running"  # a supervisor is reading its control FIFO
PAUSED = "paused"  # running, and told to launch nothing new
HELD = "held"  # running, and held back by a usage cap
FROZEN = "frozen"  # `swarm freeze`: every session stopped in place
FINISHED = "finished"  # the run ended by itself; no supervisor
STOPPED = "stopped"  # no supervisor, and the run is not finished
STALE = "stale"  # its project folder is gone
EMPTY = "empty"  # no supervisor ever ran here, so no project is recorded


@dataclass(frozen=True)
class Swarm:
    """One swarm on this machine, as its state dir shows it right now."""

    slug: str  # the state dir's name: unique on the machine
    state_dir: Path
    name: str  # what the owner reads; the slug when none was recorded
    project_dir: Path | None  # None: no supervisor ever recorded one
    session: str  # its tmux session, "" when none was recorded
    running: bool = False
    paused: bool = False
    frozen: bool = False
    held: bool = False  # by a usage cap
    finished: bool = False
    #: ``done`` / ``running`` / ``open`` (not done) / ``total`` ledger rows, counted
    #: as `swarm status` counts them; None when the ledger cannot be read.
    phases: dict[str, int] | None = None
    asking: int = 0  # sessions waiting for the owner's answer
    todos: int = 0  # owner to-dos that are not a question (`swarm todo`)
    problem: str = ""  # why the counts are missing, when they are

    @property
    def stale(self) -> bool:
        """Its project folder is gone: no command can name this swarm any more."""
        return self.project_dir is not None and not self.project_dir.is_dir()

    @property
    def needs_owner(self) -> int:
        """How many things wait on the owner here: questions and to-dos."""
        return self.asking + self.todos

    @property
    def status(self) -> str:
        """One word for the whole swarm (see the constants above)."""
        if self.project_dir is None:
            return EMPTY
        if self.stale:
            return STALE
        if self.frozen:
            return FROZEN
        if self.running:
            return HELD if self.held else PAUSED if self.paused else RUNNING
        return FINISHED if self.finished else STOPPED

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(state_dir=str(self.state_dir),
                    project_dir=str(self.project_dir) if self.project_dir else None,
                    stale=self.stale, status=self.status, needs_owner=self.needs_owner)
        return data


def state_dirs(root: Path | None = None) -> list[Path]:
    """Every swarm's state dir under the state root, by name."""
    root = state_root() if root is None else Path(root)
    try:
        found = [p for p in root.iterdir() if p.is_dir() and p.name != DIR_NAME]
    except OSError:
        return []
    return sorted(p for p in found if any((p / mark).exists() for mark in _MARKS))


def swarm_config(state_dir: Path):
    """The :class:`config.Config` of the swarm that owns ``state_dir``, bound to
    that state dir whatever this process's own environment names; None when no
    supervisor recorded one there, or its project is gone.

    It is the config the swarm's last supervisor ran on (``config.json``), so it
    is right for a running swarm even while its ``.swarm.toml`` is mid-edit."""
    from . import reload as reload_mod

    cfg = reload_mod.recorded_cfg(Path(state_dir))
    return cfg if cfg is not None and cfg.project_dir.is_dir() else None


def look(state_dir: Path) -> Swarm:
    """What ``state_dir`` shows of its swarm. Reads only, takes no lock, and
    never raises: one unreadable swarm must not hide the others."""
    from . import config as config_mod
    from . import procs

    state_dir = Path(state_dir)
    record = config_mod.recorded(state_dir)
    project = record.get("project_dir")
    base = {
        "slug": state_dir.name,
        "state_dir": state_dir,
        "name": str(record.get("name") or state_dir.name),
        "project_dir": Path(project) if isinstance(project, str) and project else None,
        "session": str(record.get("session") or ""),
        "running": procs.fifo_has_reader(state_dir / "control.fifo"),
    }
    try:
        return Swarm(**base, **_standing(state_dir))
    except Exception as exc:  # noqa: BLE001 - a listing reports; it does not crash
        return Swarm(**base, problem=f"{type(exc).__name__}: {exc}")


def _standing(state_dir: Path) -> dict:
    """The part of a :class:`Swarm` that comes from its state and its ledger."""
    from . import state as state_mod
    from . import todo as todo_mod
    from .tui import campaign

    try:
        raw = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    st = state_mod.State.from_dict(raw) if raw else None
    out: dict = {}
    if st is not None:
        out.update(paused=st.paused, frozen=bool(st.frozen), held=bool(st.usage_hold),
                   finished=st.finished, asking=len(st.on_owner()))
    cfg = swarm_config(state_dir)
    if cfg is None:
        return out
    st = st if st is not None else state_mod.State.fresh(cfg.max_workers)
    n = campaign.counts(cfg, st)
    out["phases"] = {"done": n["done"], "running": n["running"],
                     "open": n["total"] - n["done"], "total": n["total"]}
    out["todos"] = todo_mod.count(cfg, st)
    return out


def swarms(root: Path | None = None) -> list[Swarm]:
    """Every swarm on this machine, by name: running or not, and stale ones too
    (a state dir whose project is gone is shown, never hidden)."""
    return sorted((look(d) for d in state_dirs(root)), key=lambda s: (s.name, s.slug))


# -- `swarm ls` ---------------------------------------------------------------
def _short(path: Path) -> str:
    """``path`` with the home folder written ``~``."""
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _row(s: Swarm) -> list[str]:
    n = s.phases
    counts = [str(n[k]) for k in ("done", "running", "open")] if n else ["-", "-", "-"]
    where = _short(s.project_dir) if s.project_dir else f"? ({_short(s.state_dir)})"
    if s.stale:
        where += " (gone)"
    # A supervisor that outlived its project folder is the one thing worth
    # seeing twice.
    status = f"{s.status}+running" if s.stale and s.running else s.status
    return [s.name, status, *counts, str(s.needs_owner), s.session or "-", where]


def render(found: list[Swarm]) -> str:
    """``swarm ls``'s table: one line per swarm."""
    if not found:
        return f"no swarms on this machine (nothing under {_short(state_root())})"
    head = ["SWARM", "STATUS", "DONE", "RUNNING", "OPEN", "NEEDS YOU", "SESSION", "PROJECT"]
    rows = [head, *(_row(s) for s in found)]
    widths = [max(len(r[i]) for r in rows) for i in range(len(head) - 1)]
    lines = ["  ".join([*(c.ljust(w) for c, w in zip(r, widths)), r[-1]]).rstrip()
             for r in rows]
    notes = [f"{s.name}: counts unavailable ({s.problem})" for s in found if s.problem]
    return "\n".join([*lines, *notes])


def listing(found: list[Swarm]) -> dict:
    """``swarm ls --json``."""
    return {"state_root": str(state_root()), "machine_dir": str(directory()),
            "settings": str(settings_path()), "swarms": [s.to_dict() for s in found]}
