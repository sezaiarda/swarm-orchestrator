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
import time
from pathlib import Path

from . import ledger as ledger_mod
from . import tmux
from .config import Config
from .logutil import Log
from .state import State

_READY_TIMEOUT_S = 30.0
_POLL_S = 0.25

# Env vars a master pane needs so its `swarm` calls find this run. Forwarded on
# the tmux respawn (bare masters inherit the supervisor's env directly).
_FORWARD_ENV = (
    "SWARM_STATE_DIR",
    "SWARM_SLUG",
    "SWARM_DRIVER",
    "SWARM_BIN",
    "SWARM_TG_SINK",
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
    busy_phases = {s.phase for s in st.busy_slots() if s.phase}
    excluded = set(cfg.exclude)
    ready = ledger_mod.ready(graph, st.done, busy_phases, excluded)
    free = st.free_slots()
    launchable = ready[: len(free)]
    return {
        "free_slots": [s.id for s in free],
        "busy_slots": {s.id: s.phase for s in st.busy_slots()},
        "done": dict(st.done),
        "ready": ready,
        "launchable": launchable,
        "ledger": str(ledger_path),
        "master_alive": st.master_alive,
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
        if not self.cfg.master_cmd:
            self._deliver_prompt(master_pane, kind)
        return True

    def _deliver_prompt(self, pane: str, kind: str) -> None:
        """Type the init/step prompt into a freshly launched claude master.

        Unvalidated against real ``claude`` (owner-supervised smoke, step 6);
        the fake-master override never reaches this path.
        """
        prompt_file = (
            Path(__file__).resolve().parent.parent.parent
            / "prompts"
            / f"{kind}_master.md"
        )
        if not prompt_file.is_file():
            self.log.line(f"ACTION prompt-missing {prompt_file}")
            return
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.cfg.ready_marker in tmux.capture(pane):
                break
            time.sleep(_POLL_S)
        tmux.send_literal(pane, prompt_file.read_text(encoding="utf-8"))
        tmux.send_enter(pane)

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
        tmux.send_literal(self.pane, text)
        tmux.send_enter(self.pane)

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
