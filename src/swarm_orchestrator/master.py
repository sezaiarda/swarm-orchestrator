"""The ``swarm context`` snapshot and the ephemeral master's lifecycle.

The master is spawned/injected/killed *only* by the supervisor, so this module
exposes a :class:`Master` handle the supervisor owns. Two drivers are supported:
``bare`` (a plain subprocess with a stdin pipe — used by the hermetic tests) and
``tmux`` (a pane in the master window). ``build_context`` is read-only and feeds
both the real LLM master (which reasons over the ledger prose) and the fake
master (which ``jq``-selects ``.launchable``).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import launch as launch_mod
from . import ledger as ledger_mod
from . import telegram, tmux
from .config import Config, ready_needle
from .logutil import Log
from .state import State

_READY_TIMEOUT_S = 30.0

# Env vars a master pane needs so its `swarm` calls find this run. Forwarded on
# the tmux respawn (bare masters inherit the supervisor's env directly).
# SWARM_GIT_* are included so a master spawned via the env seam keeps the same
# isolation mode / repo set the run was started with — otherwise its
# `swarm launch` would silently fall back to isolation="none".
_FORWARD_ENV = (
    "SWARM_STATE_DIR",
    "SWARM_SLUG",
    "SWARM_DRIVER",
    "SWARM_BIN",
    "SWARM_TG_SINK",
    "SWARM_GIT_ISOLATION",
    "SWARM_GIT_MAIN",
    "SWARM_GIT_REPOS",
    "FAKE_MASTER_WAIT",
    "SWARM_READY_MARKER",
)


def _master_env(cfg: Config) -> dict[str, str]:
    env = {"SWARM_STATE_DIR": str(cfg.state_dir)}
    for key in _FORWARD_ENV:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


def build_context(cfg: Config, st: State) -> dict:
    """Read-only snapshot the master uses to decide what to launch."""
    ledger_path = cfg.project_dir / cfg.ledger
    graph = ledger_mod.load(ledger_path)
    # A parked or waiting phase is still in flight (its worker owes the owner an
    # answer) but is not in a slot, so exclude it from `ready` too — otherwise the
    # master would relaunch a phase that is already being built off-grid.
    busy_phases = {s.phase for s in st.busy_slots() if s.phase} | set(st.parked) | set(st.waiting)
    excluded = set(cfg.exclude)
    ready = ledger_mod.ready(graph, st.done, busy_phases, excluded)
    free = st.free_slots()
    launchable = [] if st.paused else ready[: len(free)]
    return {
        "free_slots": [s.id for s in free],
        "busy_slots": {s.id: s.phase for s in st.busy_slots()},
        "done": dict(st.done),
        "ready": ready,
        "launchable": launchable,
        "paused": st.paused,
        "ledger": str(ledger_path),
        "master_alive": st.master_alive,
        # Phases whose worker is off-grid awaiting the owner: `waiting` armed a
        # park timer (still holds its slot), `parked` was moved to its own window
        # (slot freed). Both are excluded from `ready`, are never relaunched, and
        # keep the run `pending` until the worker is answered and runs `swarm done`.
        "waiting": sorted(st.waiting),
        "parked": list(st.parked),
        # Structural ledger problems (cycles / self-deps / unknown deps) that
        # would otherwise silently stall the run — surfaced so the master/owner
        # can see them instead of a phase never becoming ready.
        "ledger_issues": ledger_mod.validate(graph),
    }


def master_command(cfg: Config, kind: str) -> str:
    """Shell command that runs the master.

    An explicit ``master_cmd`` (``SWARM_MASTER_CMD`` / config) short-circuits to
    that command — this is how the tests inject ``fake-master.sh``. Otherwise a
    default ``claude`` invocation is returned; the init/step prompt is delivered
    separately by :meth:`Master._spawn_tmux` and pinned during the owner's live
    smoke (real ``claude`` is out of scope for the hermetic tests).
    """
    if cfg.master_cmd:
        return cfg.master_cmd
    model = f" -m {cfg.master_model}" if cfg.master_model else ""
    return f"cd {cfg.project_dir} && exec claude{model}"


class Master:
    """Supervisor-owned handle to the one live master session."""

    def __init__(self, cfg: Config, log: Log) -> None:
        self.cfg = cfg
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.pane: str | None = None

    def is_alive(self) -> bool:
        if self.cfg.driver == "bare":
            return self.proc is not None and self.proc.poll() is None
        return self.pane is not None

    def spawn(self, kind: str, master_pane: str | None = None) -> bool:
        """Start a fresh master (``kind`` is ``init`` or ``step``).

        Returns True on success. A False return means no master is running, so
        the caller must NOT mark the master alive.
        """
        cmd = master_command(self.cfg, kind)
        if self.cfg.driver == "bare":
            ok = self._spawn_bare(cmd)
        else:
            ok = self._spawn_tmux(cmd, kind, master_pane)
        if ok:
            self.log.line(f"ACTION spawn-master kind={kind}")
        return ok

    def _spawn_bare(self, cmd: str) -> bool:
        try:
            self.proc = subprocess.Popen(
                ["/bin/sh", "-c", cmd],
                cwd=str(self.cfg.project_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
            return True
        except OSError as exc:
            self.log.line(f"ACTION spawn-master-failed bare {exc}")
            return False

    def _spawn_tmux(self, cmd: str, kind: str, master_pane: str | None) -> bool:
        if master_pane is None:
            self.log.line("ACTION spawn-master-failed no-pane")
            return False
        self.pane = master_pane
        tmux.respawn_pane(master_pane, cmd, env=_master_env(self.cfg))
        if not self.cfg.master_cmd and not self._deliver_prompt(master_pane, kind):
            # Never primed (boot timed out / stuck on a modal): treat as a spawn
            # failure so the caller does NOT mark the master alive — otherwise the
            # next `done` would inject into a master that never got its prompt.
            self.pane = None
            return False
        return True

    def _deliver_prompt(self, pane: str, kind: str) -> bool:
        """Point a freshly launched claude master at its prompt file.

        Delivered as ONE line with no embedded newlines: tmux ``send-keys``
        submits on every newline, so pasting the multi-line prompt file would
        fire it off line-by-line. We tell the master to *read* the file instead.
        Waits for readiness (dismissing a first-run folder-trust prompt) and
        returns False on timeout instead of blindly typing into a not-ready pane.
        The fake-master override never reaches this path.
        """
        prompt_file = (
            Path(__file__).resolve().parent.parent.parent
            / "prompts"
            / f"{kind}_master.md"
        )
        if not prompt_file.is_file():
            self.log.line(f"ACTION prompt-missing {prompt_file}")
            return False
        if not launch_mod.await_ready(self.cfg, pane, self.log):
            self.log.line("ACTION master-ready-timeout")
            telegram.notify_event(
                "master-not-ready", f"master ({kind}) never became ready", self.log
            )
            return False
        line = (
            f"Read {prompt_file} and follow every instruction in it exactly. "
            f"You are orchestrating the project at {self.cfg.project_dir}."
        )
        if not tmux.send_submit(pane, line):
            self.log.line("ACTION master-submit-lost")
            telegram.notify_event(
                "master-submit-lost", f"master ({kind}) prompt would not submit", self.log
            )
            return False
        return True

    def inject(self, text: str) -> None:
        """Nudge the live master with one line of guidance."""
        if self.cfg.driver == "bare":
            self._inject_bare(text)
        else:
            self._inject_tmux(text)
        self.log.line(f"ACTION inject-master {text}")

    def _inject_bare(self, text: str) -> None:
        if self.proc is None or self.proc.stdin is None:
            self.log.line("ACTION inject-failed no-proc")
            return
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            self.log.line(f"ACTION inject-failed {exc}")

    def _inject_tmux(self, text: str) -> None:
        if self.pane is None:
            self.log.line("ACTION inject-failed no-pane")
            return
        if not tmux.send_submit(self.pane, text):
            self.log.line("ACTION inject-lost")

    def kill(self) -> None:
        """Terminate the master (bare) or clear its pane (tmux)."""
        if self.cfg.driver == "bare":
            self._kill_bare()
        else:
            self._kill_tmux()
        self.log.line("ACTION kill-master")

    def _kill_bare(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None

    def _kill_tmux(self) -> None:
        if self.pane is not None:
            tmux.respawn_pane(self.pane, "exec sleep infinity")
        self.pane = None
