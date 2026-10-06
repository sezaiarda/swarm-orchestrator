"""The owner's guide: a chat session that walks the owner through his to-dos.

``swarm guide`` (``g`` in the TUI) opens a Claude session in a tmux window of its
own, ``guide``, in the swarm's session, and moves the owner there. It reads
``swarm todo --json`` and takes him through each item in plain words; when he
reports back it does the bookkeeping with the swarm's own commands (``record``,
``operator-done``, ``follow-up``, ``lesson``) and nothing heavier — see
``prompts/owner_guide.md``.

One window, never two: pressing the key while the session is alive only focuses
it. The window closes when the session ends (``remain-on-exit`` is off for it
alone), so the next press starts a fresh one. The open is serialised by a lock
so two presses in quick succession cannot both start a session.

Launched like the operator and the big-picture pass: a worker-built ``claude``
(in-process teammates, the meters tap, the effort level) on the operator's model,
cwd the project, the session env every swarm session gets, and a one-line
pointer to the prompt typed once the banner shows.
"""

from __future__ import annotations

import fcntl
import shlex
import time
from contextlib import contextmanager
from pathlib import Path

from . import launch as launch_mod
from . import resolver
from . import session as session_mod
from . import tmux
from .config import Config
from .logutil import Log

WINDOW = "guide"
PROMPT = "owner_guide.md"
KIND = "guide"

OPENED = "opened"
FOCUSED = "focused"


class GuideError(Exception):
    """The guide cannot be opened here (no tmux session, the bare driver)."""


def command(cfg: Config) -> str:
    """The session's shell command: a worker-built ``claude`` in the project."""
    model = cfg.operator_model or cfg.master_model
    base = "claude" + (f" --model {shlex.quote(model)}" if model else "")
    return launch_mod._worker_shell(cfg, WINDOW, cfg.project_dir, base,
                                    name=launch_mod.session_name(WINDOW))


def env(cfg: Config) -> dict[str, str]:
    out = launch_mod.session_env(cfg, None, tmp=WINDOW,
                                 session=f"{KIND}:{time.strftime('%Y%m%dT%H%M%S')}")
    out["SWARM_PROJECT"] = str(cfg.project_dir)
    return out


def pane_line(cfg: Config) -> str:
    """The one line that starts the session. One line: send-keys submits on newline."""
    swarm = f"swarm --project-dir {shlex.quote(str(cfg.project_dir))}"
    return (f"You are the swarm's owner guide. Read {resolver.prompt_path(PROMPT)} and follow"
            f" it exactly. Run every swarm command as `{swarm} <command>`; start with"
            f" `{swarm} todo --json`.")


@contextmanager
def _locked(cfg: Config):
    path = Path(cfg.state_dir) / "guide.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def find(cfg: Config) -> str | None:
    """The live guide window's id, or ``None``; a dead one left behind is removed."""
    win = tmux.find_window(cfg.session, WINDOW)
    if win is None:
        return None
    if tmux.window_alive(win):
        return win
    tmux.kill_window(win)
    return None


def focus(win: str) -> None:
    """Make ``win`` the session's current window, which moves an attached client."""
    tmux.run(["select-window", "-t", win])


def open_window(cfg: Config) -> tuple[str, str]:
    """``(OPENED | FOCUSED, window id)``: start the guide, or focus the live one.

    Raises :class:`GuideError` when there is nowhere to open it. The session is
    started here but only told what it is (the prompt line) by :func:`deliver`,
    which waits for its banner — call that off the UI thread.
    """
    if cfg.driver != "tmux":
        raise GuideError("the guide needs the tmux driver; run it by hand:"
                         f" {command(cfg)}")
    if not tmux.session_exists(cfg.session):
        raise GuideError(f"the swarm's tmux session {cfg.session!r} is not running"
                         " (`swarm up` starts it)")
    if not session_mod.owns_session(cfg):
        raise GuideError(f"tmux session {cfg.session!r} is not this swarm's")
    with _locked(cfg):
        win = find(cfg)
        if win is not None:
            focus(win)
            return FOCUSED, win
        launch_mod.pretrust_dir(cfg.project_dir, _log(cfg))
        win = tmux.new_window(cfg.session, WINDOW)
        # The swarm keeps dead panes on screen (tmux.harden); this one closes with
        # its session, so a finished guide never lingers and the next press is fresh.
        tmux.run(["set-option", "-w", "-t", win, "remain-on-exit", "off"])
        pane = tmux.list_panes(win)[0]
        tmux.respawn_pane(pane, command(cfg), env=env(cfg))
        focus(win)
    return OPENED, win


def deliver(cfg: Config, win: str) -> bool:
    """Wait for the session's banner, then type its one line. False on failure."""
    log = _log(cfg)
    if not resolver.prompt_path(PROMPT).is_file():
        log.line(f"GUIDE-PROMPT-MISSING {resolver.prompt_path(PROMPT)}")
        return False
    panes = tmux.list_panes(win)
    if not panes or not launch_mod.await_ready(cfg, panes[0], log):
        log.line("GUIDE-READY-TIMEOUT")
        return False
    if not tmux.send_submit(panes[0], pane_line(cfg)):
        log.line("GUIDE-SUBMIT-LOST")
        return False
    log.line(f"GUIDE-OPENED {win}")
    return True


def open_guide(cfg: Config) -> str:
    """Open or focus the guide and, when new, start it. Returns what it did, in words."""
    what, win = open_window(cfg)
    if what == FOCUSED:
        return f"the guide is already open — moved you to tmux window {WINDOW}"
    if not deliver(cfg, win):
        return f"opened tmux window {WINDOW}, but the session did not take its first line"
    return f"opened the guide in tmux window {WINDOW}"


def _log(cfg: Config) -> Log:
    return Log(cfg.supervisor_log)
