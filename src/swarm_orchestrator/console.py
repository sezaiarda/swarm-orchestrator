"""The owner console: the owner's own Claude session, in the swarm's tmux session.

``swarm up`` opens a ``console`` window between the dashboard and the overseer.
Its pane runs a small keeper (``swarm _console-pane``, :func:`run_pane`) that
starts ``claude`` in the project directory and, when the owner ``/exit``s it,
stays on screen with one idle line instead of relaunching: the owner closed it
on purpose. Enter in the pane, ``o`` in the dashboard or ``swarm console``
(:func:`open_console`) opens it again; while it runs they only move the owner
there, so there is never a second ``claude``.

The conversation carries over. Its id is chosen here and kept in
``<state>/console.json``: a launch passes ``--resume <id>`` once Claude Code has
a transcript for it and ``--session-id <id>`` before, so ``swarm up`` picks the
same conversation up again and never another session's (``--continue`` would
take the newest in the directory, which can be an Overseer pass).
``swarm console --new`` stores a fresh id.

It is not a worker. It carries no phase marker and no ``SWARM_SESSION_ID``, so no
``Stop`` hook recap, slot, ETA, usage-per-phase or session reaper ever counts it;
it holds no ``@swarm_slot`` pane, so no watchdog looks at it. It does carry the
run's ``SWARM_STATE_DIR``, so its ``swarm`` commands find this run and
``swarm down`` ends it with the rest (the transcript stays, to resume). It keeps
the owner's own settings and hooks, and its primer (``prompts/console.md``, the
CLI's commands, the project's ``[console] prompt_file``) is appended to Claude
Code's system prompt.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import subprocess
import sys
import termios
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import launch as launch_mod
from . import procs, resolver
from . import session as session_mod
from . import state as state_mod
from . import tmux
from .config import Config, load

WINDOW = "console"
PROMPT = "console.md"
RECORD = "console.json"

# What :func:`open_console` did.
OPENED = "opened"  # the window was missing (or its keeper dead) and is started
REOPENED = "reopened"  # the idle keeper was told to start claude again
FOCUSED = "focused"  # claude is running: the owner was only moved there

# What the console's pane is doing (:func:`pane_state`).
RUNNING = "running"
IDLE = "idle"
DEAD = "dead"

#: The line the keeper shows once claude has exited.
IDLE_LINE = ("console closed. Press Enter here, or o in the dashboard, to reopen it"
             " (`swarm console --new` starts a fresh conversation).")

#: Variables that make a process a swarm session (or a phase's worker): the
#: console must carry none of them, whatever the tmux server's environment holds.
SESSION_MARKERS = frozenset({
    procs.SESSION_ENV, "SWARM_PHASE", "SWARM_WORKTREE", "SWARM_TOUCHES", "SWARM_MAIN",
    "SWARM_OPERATOR_JOB", "SWARM_OVERSEER_PASS", "SWARM_MASTER_KIND", "SWARM_ASK",
})

#: Commands that belong to the swarm's own sessions or to the owner's terminal.
#: Everything else the CLI has is listed in the primer as the console's.
NOT_YOURS = frozenset({
    "up", "tui", "web", "telegram-bot", "done", "waiting", "resumed", "widen",
    "resolved", "master-idle", "bootstrap", "operator-triage", "operator-done",
    "overseer-done", "big-picture-done",
})


class ConsoleError(Exception):
    """The console cannot be opened here (no swarm session, the bare driver, off)."""


# -- the conversation id ------------------------------------------------------
def _record_path(cfg: Config) -> Path:
    return Path(cfg.state_dir) / RECORD


def session_id(cfg: Config) -> str | None:
    """The console conversation's id, or None before the first one."""
    try:
        data = json.loads(_record_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sid = data.get("session_id") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) and sid else None


def new_session_id(cfg: Config) -> str:
    """Choose and store a fresh conversation id; the next launch starts it."""
    sid = str(uuid.uuid4())
    path = _record_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"session_id": sid, "since": time.time()}), encoding="utf-8")
    os.replace(tmp, path)
    return sid


def _claude_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def has_transcript(sid: str) -> bool:
    """Whether Claude Code has written the conversation ``sid`` (so it can resume).

    Looked up in every project directory, not in one derived from the cwd: the
    encoding of a path into a directory name is Claude Code's, and may change."""
    projects = _claude_dir() / "projects"
    try:
        return any(projects.glob(f"*/{sid}.jsonl"))
    except OSError:
        return False


# -- the primer ---------------------------------------------------------------
def _subcommands() -> list[tuple[str, str]]:
    """``(name, one-line help)`` for every public subcommand, in the CLI's order."""
    from . import cli

    sub = next(a for a in cli._build_parser()._actions
               if isinstance(a, argparse._SubParsersAction))
    return [(a.dest, " ".join((a.help or "").split())) for a in sub._choices_actions
            if not a.dest.startswith("_")]


def commands_block() -> tuple[str, str]:
    """The primer's command list (one line each) and the ones that are not its."""
    mine, theirs = [], []
    for name, help_ in _subcommands():
        if name in NOT_YOURS:
            theirs.append(f"`{name}`")
        else:
            mine.append(f"- `{name}` — {help_}")
    return "\n".join(mine), ", ".join(theirs)


def _project_primer(cfg: Config) -> str:
    """The project's own addition (``[console] prompt_file``), or ""."""
    if not cfg.console_prompt_file:
        return ""
    path = Path(cfg.console_prompt_file).expanduser()
    if not path.is_absolute():
        path = cfg.project_dir / path
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        print(f"swarm console: [console] prompt_file {path} cannot be read; left out",
              file=sys.stderr)
        return ""
    return f"\n\n## This project\n\n{text}\n" if text else ""


def primer(cfg: Config) -> str:
    """The text appended to the console's system prompt."""
    try:
        template = resolver.prompt_path(PROMPT).read_text(encoding="utf-8")
    except OSError:  # a broken install must not cost the owner the console
        template = ("You are the owner's console for the swarm building {{project}}."
                    " Run swarm commands as `{{swarm}} <command>`.\n\n{{commands}}\n")
    mine, theirs = commands_block()
    swarm = f"swarm --project-dir {shlex.quote(str(cfg.project_dir))}"
    values = {
        "project": str(cfg.project_dir),
        "session": cfg.session,
        "state_dir": str(cfg.state_dir),
        "ledger": cfg.ledger,
        "history": cfg.history_dir,
        "lessons": cfg.lessons,
        "big_picture": cfg.big_picture_doc,
        "log": str(cfg.supervisor_log),
        "swarm": swarm,
        "commands": mine,
        "session_commands": theirs,
    }
    for key, val in values.items():
        template = template.replace("{{" + key + "}}", val)
    return template.rstrip() + "\n" + _project_primer(cfg)


# -- the claude command and its environment ----------------------------------
def claude_argv(cfg: Config, sid: str, resume: bool) -> list[str]:
    """The console's ``claude`` invocation (``[console] cmd`` replaces the base)."""
    argv = shlex.split(cfg.console_cmd or "claude")
    if cfg.console_model:
        argv += ["--model", cfg.console_model]
    if not launch_mod.names_itself(shlex.join(argv)):
        argv += ["-n", launch_mod.session_name(WINDOW)]
    argv += ["--resume", sid] if resume else ["--session-id", sid]
    return argv + ["--append-system-prompt", primer(cfg)]


def claude_env(cfg: Config, base: dict[str, str] | None = None) -> dict[str, str]:
    """``base`` (this environment) without any session marker, plus the run's
    ``SWARM_STATE_DIR`` so the console's ``swarm`` commands find this run."""
    base = dict(os.environ if base is None else base)
    drop = SESSION_MARKERS | {cfg.env_marker}
    env = {k: v for k, v in base.items() if k not in drop}
    env["SWARM_STATE_DIR"] = str(cfg.state_dir)
    return env


def pane_command(cfg: Config) -> str:
    """What the console's pane runs: the keeper, in the project directory, rid of
    any session marker the tmux server's own environment may carry (a server
    started from inside a worker would hand its phase to every new pane)."""
    project = shlex.quote(str(cfg.project_dir))
    unset = " ".join(f"-u {k}" for k in sorted(SESSION_MARKERS | {cfg.env_marker}))
    return (f"cd {project} && exec env {unset} {shlex.quote(sys.executable)}"
            f" -m swarm_orchestrator --project-dir {project} _console-pane")


def pane_env(cfg: Config) -> dict[str, str]:
    """The run's own variables for the keeper (a pane otherwise gets the tmux
    server's environment, which may predate this run), markers excluded."""
    env = {k: v for k, v in os.environ.items()
           if k.startswith("SWARM_") and k not in SESSION_MARKERS and k != cfg.env_marker}
    env["SWARM_STATE_DIR"] = str(cfg.state_dir)
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        env["CLAUDE_CONFIG_DIR"] = os.environ["CLAUDE_CONFIG_DIR"]
    return env


# -- the keeper (runs in the pane) --------------------------------------------
def _run_claude(argv: list[str], env: dict[str, str], cwd: Path) -> int:
    """Run claude in the foreground and wait for it, whatever Ctrl-C does meanwhile."""
    try:
        proc = subprocess.Popen(argv, env=env, cwd=str(cwd))
    except OSError as exc:
        print(f"swarm console: could not start {argv[0]}: {exc}", file=sys.stderr)
        return 127
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue


def _discard_typeahead() -> None:
    """Drop input typed (or sent) before the idle line: only an Enter pressed
    after it may reopen the console."""
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (OSError, ValueError, termios.error):
        pass


def _await_enter() -> bool:
    """Show the idle line and wait for Enter. False when stdin is gone."""
    _discard_typeahead()
    while True:
        print(f"\n{IDLE_LINE}", flush=True)
        try:
            return bool(sys.stdin.readline())
        except KeyboardInterrupt:
            continue


def run_pane(project_dir: Path, explicit: str | None = None) -> int:
    """The keeper: start claude, and once it exits wait for the owner to reopen it.

    Never relaunches on its own, so a claude that exits at once cannot loop. The
    config is read again before each launch, so a ``[console]`` edit reaches the
    next one."""
    while True:
        cfg = load(explicit=explicit, project_dir=str(project_dir))
        sid = session_id(cfg) or new_session_id(cfg)
        resume = has_transcript(sid)
        verb = "resuming" if resume else "starting"
        print(f"swarm console: {verb} conversation {sid}", flush=True)
        rc = _run_claude(claude_argv(cfg, sid, resume), claude_env(cfg), cfg.project_dir)
        if rc not in (0, 130):
            print(f"swarm console: claude exited with status {rc}", flush=True)
        if not _await_enter():
            return 0


# -- opening it from the dashboard or the CLI ---------------------------------
@contextmanager
def _locked(cfg: Config):
    path = Path(cfg.state_dir) / "console.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def start_window(cfg: Config, after: str | None = None) -> tuple[str, str]:
    """Open the console window (after window ``after`` if given) and start its
    keeper. Returns ``(window id, pane id)``."""
    win = tmux.new_window(cfg.session, WINDOW, after=after)
    pane = tmux.list_panes(win)[0]
    start_keeper(cfg, pane)
    return win, pane


def start_keeper(cfg: Config, pane: str) -> None:
    tmux.respawn_pane(pane, pane_command(cfg), env=pane_env(cfg))


def pane_state(pane: str) -> str:
    """:data:`RUNNING` while the keeper has a child (claude), :data:`IDLE` while it
    waits on Enter, :data:`DEAD` when the pane's process is gone or unknown."""
    out = tmux.run(["display-message", "-p", "-t", pane, "#{pane_dead} #{pane_pid}"])
    dead, _, pid = out.stdout.strip().partition(" ")
    if out.returncode != 0 or dead != "0" or not pid.isdigit():
        return DEAD
    keeper = int(pid)
    return RUNNING if keeper in procs.table().values() else IDLE


def focus(win: str) -> None:
    """Make ``win`` the session's current window, which moves an attached client."""
    tmux.run(["select-window", "-t", win])


def _check(cfg: Config) -> None:
    if cfg.driver != "tmux":
        raise ConsoleError("the console needs the tmux driver")
    if not cfg.console_enabled:
        raise ConsoleError("the console is off ([console] enabled = false)")
    if not tmux.session_exists(cfg.session):
        raise ConsoleError(f"the swarm's tmux session {cfg.session!r} is not running"
                           " (`swarm up` starts it)")
    if not session_mod.owns_session(cfg, state_mod.read(cfg).windows):
        raise ConsoleError(f"tmux session {cfg.session!r} is not this swarm's")


def open_console(cfg: Config, new: bool = False) -> tuple[str, str]:
    """``(OPENED | REOPENED | FOCUSED, window id)``: bring the console up, or move
    the owner to the running one. ``new`` starts a fresh conversation, and is
    refused while one is running (:class:`ConsoleError`): ending it is the
    owner's ``/exit``, never ours."""
    _check(cfg)
    with _locked(cfg):
        win = tmux.find_window(cfg.session, WINDOW)
        pane = tmux.list_panes(win)[0] if win else None
        state = pane_state(pane) if pane else DEAD
        if state == RUNNING:
            focus(win)
            if new:
                raise ConsoleError("the console is running; /exit it there first, then"
                                   " `swarm console --new`")
            return FOCUSED, win
        if new:
            new_session_id(cfg)
        if win is None or pane is None:
            win, pane = start_window(cfg, after=tmux.find_window(cfg.session, "dash"))
            what = OPENED
        elif state == DEAD:
            start_keeper(cfg, pane)
            what = OPENED
        else:
            tmux.send_enter(pane)  # the idle keeper reads it and starts claude
            what = REOPENED
        focus(win)
    return what, win


def open_words(cfg: Config, new: bool = False) -> str:
    """:func:`open_console`, said in words."""
    what, _ = open_console(cfg, new)
    if what == FOCUSED:
        return f"the console is running — moved you to tmux window {WINDOW}"
    fresh = "a new conversation" if new else "the console"
    return f"opened {fresh} in tmux window {WINDOW}"
