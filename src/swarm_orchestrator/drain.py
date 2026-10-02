"""``swarm down --drain``: stop once the running work is finished, then maybe run a command.

The drain is a hold in ``state.json`` (``State.drain``): nothing new launches,
no operator job or Overseer pass opens, and the supervisor watches what is still
running. Once nothing it waits for is left it pings the owner and starts
``swarm _drain-down`` detached, which runs the ordinary ``swarm down`` and then
the owner's after-command, if any, in a session of its own.

Waited for: workers at work (a finished worker holds its slot through its done
grace), launches in flight, the merge queue while it moves, a merge-conflict
resolver, a running operator job, the init pass and an Overseer pass. A parked
session the owner has answered is at work too, in a window of its own: it holds
no slot, so it is waited for by name. Not waited for: anything waiting on the
owner — a worker that asked, a parked one that still asks, an operator job that
asked, a queue held on a dirty tree. The down takes those as they are; it keeps
their work, and ends the sessions, so what they asked is gone from the screen.
``swarm down --drain`` says so when it is scheduled.

``swarm restart --full`` drains the same way and is the safe form of the old
``--then 'swarm up'``: it refuses while a session waits on the owner unless told
to wait for the answers (``questions = "wait"`` in the drain: they are counted
here) or to carry the sessions across the restart (``"keep"``, see
:mod:`restart`).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import opqueue
from . import state as state_mod
from .config import Config

#: Where the after-command's output goes: it outlives the swarm, so no pane has it.
AFTER_LOG = "after-down.log"
#: Where the detached down step logs what ``swarm down`` printed.
DOWN_LOG = "drain-down.log"


def waiting_for(cfg: Config, st: state_mod.State, *, launching: int = 0,
                overseer: bool = False, init_pass: bool = False,
                now: float | None = None) -> list[str]:
    """What the stop still waits for, in words (``[]`` = nothing: stop now).

    ``launching``, ``overseer`` and ``init_pass`` are the supervisor's own
    knowledge; ``state.json`` alone cannot tell them."""
    now = time.time() if now is None else now
    # A parked session the owner has answered works on in its own window. No
    # slot shows it, so each is named: the reader must be able to find it.
    apart = [state_mod.waiter(key) for key in st.working_parked()]
    own = [ident for kind, ident in apart if kind == state_mod.WORKER]
    workers = (sum(1 for s in st.busy_slots() if s.phase not in st.waiting) + launching
               + len(own))
    out: list[str] = []
    if workers:
        text = f"{workers} worker{'' if workers == 1 else 's'}"
        if own:
            where = "its own window" if len(own) == 1 else "their own windows"
            text += f" ({_join(own)} in {where})"
        out.append(text)
    if st.integ_queue and st.integ_blocked is None:
        out.append("the merge queue")
    if st.integ_blocked is not None and f"resolve:{st.integ_blocked}" in st.windows:
        out.append("a merge-conflict resolver")
    if st.operator_busy(now) and not _operator_asking(cfg, st.operator_phase):
        out.append("an operator job")
    out.extend(f"operator job {ident} in its own window"
               for kind, ident in apart if kind == state_mod.OPERATOR)
    if init_pass:
        out.append("the start-up pass")
    if overseer:
        out.append("an Overseer pass")
    if any(kind == state_mod.OVERSEER for kind, _ in apart):
        out.append("an Overseer pass in its own window")
    if st.drain.get("questions") == "wait":
        # A restart told to wait for the answers: every session that waits on
        # the owner holds the stop until it is answered and finishes.
        asked = set(st.on_owner())
        if st.operator_phase and _operator_asking(cfg, st.operator_phase):
            asked.add(state_mod.waiter_key(state_mod.OPERATOR, st.operator_phase))
        if asked:
            out.append(f"{len(asked)} question{'' if len(asked) == 1 else 's'}")
    return out


def _operator_asking(cfg: Config, phase: str | None) -> bool:
    """The live operator job is waiting on the owner, so it holds nothing up."""
    item = opqueue.load(cfg, phase) if phase else None
    return item is not None and item.state == opqueue.WAITING


def line(drain: dict) -> str:
    """The drain in plain English, for the dashboard, the board and ``swarm status``."""
    if not drain:
        return ""
    waits = list(drain.get("waiting") or [])
    end = "restart" if drain.get("restart") else "stop"
    if drain.get("stopping_at"):
        text = ("Draining: done waiting, restarting the swarm now" if drain.get("restart")
                else "Draining: done waiting, stopping the swarm now")
    elif waits:
        text = f"Draining: waiting for {_join(waits)}, then {end}"
    else:
        text = f"Draining: {end} once the supervisor has looked"
    if drain.get("then"):
        text += f", then: {drain['then']}"
    return text


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


_SUDO = re.compile(r"(^|[\s;&|(`])sudo(\s|$)")


def sudo_warning(command: str) -> str | None:
    """Why ``command`` would stall on a password prompt, or None.

    The after-command runs with no terminal, so a ``sudo`` that wants a password
    just fails. Checked when the drain is scheduled, while someone is there."""
    if not command or not _SUDO.search(command):
        return None
    try:
        ok = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if ok:
        return None
    return ("the after-command uses sudo, and sudo would ask for a password here; it will"
            " run with no terminal, so that sudo will fail")


def spawn_down(cfg: Config) -> bool:
    """Start ``swarm _drain-down`` in a session of its own; the caller carries on.

    Detached because ``swarm down`` stops the supervisor that calls this, and
    the tmux session a dashboard caller lives in."""
    argv = [sys.executable, "-m", "swarm_orchestrator", "--project-dir", str(cfg.project_dir)]
    if getattr(cfg, "config_file", None):
        argv += ["--config", str(cfg.config_file)]
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (cfg.log_dir / DOWN_LOG).open("a", encoding="utf-8") as out:
            subprocess.Popen(
                [*argv, "_drain-down"],
                cwd=str(cfg.project_dir),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError:
        return False
    return True


def run_after(cfg: Config, command: str) -> Path | None:
    """Run the owner's after-command, detached from everything the swarm owns.

    In a transient ``systemd-run --user --scope`` where one can be made, so it
    leaves the tmux server's and the login's process group and cgroup alike;
    else plainly in a new session. Either way it keeps this process's working
    directory and environment, and its output is appended to the log returned."""
    log = cfg.log_dir / AFTER_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = ["/bin/bash", "-c", command]
    if _can_scope():
        argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", *argv]
    try:
        with log.open("a", encoding="utf-8") as out:
            out.write(f"\n== {time.strftime('%Y-%m-%d %H:%M:%S')} $ {command}\n")
            out.flush()
            subprocess.Popen(
                argv, cwd=str(cfg.project_dir),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError:
        return None
    return log


def _can_scope() -> bool:
    if shutil.which("systemd-run") is None or not os.environ.get("XDG_RUNTIME_DIR"):
        return False
    try:
        probe = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet", "--collect", "true"],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0
