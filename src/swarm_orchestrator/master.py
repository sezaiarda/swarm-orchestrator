"""The ``swarm context`` snapshot and the one supervisor-owned Claude session.

The master is spawned/injected/killed *only* by the supervisor, so this module
exposes a :class:`Master` handle the supervisor owns. Two drivers are supported:
``bare`` (a plain subprocess with a stdin pipe — used by the hermetic tests) and
``tmux`` (a pane in the master window). ``build_context`` is read-only and feeds
the init master, the Overseer's digest, the owner and the TUI.

The pane runs one of two kinds of pass, never both at once: ``init`` (the
bootstrap: telegram preflight, patch the worker command) once at ``swarm up``,
then ``overseer`` passes for the rest of the run (:mod:`overseer` decides when).
The Overseer is a full, unrestrained session — built by the worker's own shell
builder — because what it is for is acting on what it reads.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

from . import lanes as lanes_mod
from . import launch as launch_mod
from . import ledger as ledger_mod
from . import resolver, telegram, tmux
from .config import Config, ready_needle
from .logutil import Log
from .state import State

_READY_TIMEOUT_S = 30.0

INIT = "init"
OVERSEER = "overseer"
#: The prompt file each kind of pass is pointed at.
_PROMPTS = {INIT: "init_master.md", OVERSEER: "overseer.md"}
#: What the owner reads for each kind of master session.
_NAMES = {INIT: "start-up", OVERSEER: "Overseer"}

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
    """Read-only snapshot of what is ready and where it can go.

    The supervisor's launcher starts ``launchable`` directly; ``swarm context``
    prints the same snapshot for the init master, the owner and the TUI."""
    ledger_path = cfg.project_dir / cfg.ledger
    graph = ledger_mod.load(ledger_path)
    # A parked or waiting phase is still in flight (its worker owes the owner an
    # answer) but is not in a slot, so exclude it from `ready` too — otherwise the
    # master would relaunch a phase that is already being built off-grid. A phase
    # queued or held for merging is finished work: relaunching it would build over
    # the only copy of that work.
    busy_phases = (
        {s.phase for s in st.busy_slots() if s.phase}
        | set(st.parked)
        | set(st.waiting)
        | st.integrating()
    )
    # A row that finished `later` waits in the ledger for its `after:` date.
    deferred = ledger_mod.load_deferred(ledger_path, time.strftime("%Y-%m-%d", time.gmtime()))
    excluded = set(cfg.exclude) | set(deferred)
    # A row ticked `[x]` with no record of ours counts as landed (a view only;
    # `done` below stays the swarm's own records).
    done = ledger_mod.with_ticked(st.done, ledger_mod.load_ticked(ledger_path), busy_phases)
    ready = ledger_mod.ready(graph, done, busy_phases, excluded)
    free = st.free_slots()
    lanes: dict = {"enabled": False}
    issues = ledger_mod.validate(
        graph, {p for p, status in done.items() if status in ledger_mod.SATISFIES_DEPS}
    )
    if cfg.lanes_enabled:
        picked, lanes = _lanes(cfg, st, ready, list(graph))
        issues += lanes.pop("issues")
    else:
        picked = ready
    launchable = [] if st.on_hold else picked[: len(free)]
    return {
        "free_slots": [s.id for s in free],
        "busy_slots": {s.id: s.phase for s in st.busy_slots()},
        "done": dict(st.done),
        "ready": ready,
        "launchable": launchable,
        "paused": st.paused,
        # The usage cap's own hold (see caps.py); only the owner lifts it.
        "usage_hold": dict(st.usage_hold),
        "ledger": str(ledger_path),
        "master_alive": st.master_alive,
        # Phases whose worker is off-grid awaiting the owner: `waiting` armed a
        # park timer (still holds its slot), `parked` was moved to its own window
        # (slot freed). Both are excluded from `ready`, are never relaunched, and
        # keep the run `pending` until the worker is answered and runs `swarm done`.
        "waiting": sorted(st.waiting),
        "parked": list(st.parked),
        # Rows waiting for a date (`after:`), which `ready` leaves out until then.
        "deferred": deferred,
        # Repos merged locally but not yet on origin. Informational: nothing a
        # worker builds waits on origin, so these never gate `ready`.
        "push_owed": {k: v.get("phase") for k, v in st.push_owed.items()},
        # Structural ledger problems (cycles / self-deps / unknown deps, and with
        # lanes on rows whose touches do not parse) that would otherwise silently
        # stall the run — surfaced so the master/owner can see them instead of a
        # phase never becoming ready.
        "ledger_issues": issues,
        # Lanes: which lane each phase in flight holds, and what each ready row
        # that is not launchable waits for. Just ``{"enabled": false}`` when off.
        "lanes": lanes,
    }


def _lanes(cfg: Config, st: State, ready: list[str], order: list[str]) -> tuple[list[str], dict]:
    """The lane scheduler's picks among ``ready``, and the
    context's ``lanes`` block: ``picked``, those picks before the free-slot cap
    (what the supervisor walks), ``held`` by each phase in flight, and ``waits``
    for each ready row held back, with ``why`` ``running`` (a phase in flight or
    launching in this pass holds an overlapping touch), ``reserved`` (an earlier
    ready row waits for it) or ``per_repo`` (the repo is at ``[lanes] per_repo``)."""
    view = launch_mod.lane_view(cfg, st)
    picked, waits = lanes_mod.pick(ready, order, view.held, view.rows, cfg.lanes_per_repo)
    why = {"held": "running", "reserved": "reserved", "per_repo": "per_repo"}
    return picked, {
        "enabled": True,
        "picked": picked,
        "held": {p: sorted(str(t) for t in lane) for p, lane in sorted(view.held.items())},
        "waits": {
            row: {"holder": w.holder, "touch": str(w.touch), "why": why[w.why]}
            for row, w in waits.items()
        },
        "issues": view.issues,
    }


def _override(cfg: Config, kind: str) -> str:
    """The configured command that replaces the built-in ``claude`` for ``kind``.

    An Overseer falls back to ``master_cmd``: it is the master's session
    repurposed, and a project (or test) that swapped the master out swapped out
    the thing that runs in that pane."""
    if kind == OVERSEER:
        return cfg.overseer_cmd or cfg.master_cmd
    return cfg.master_cmd


def master_command(cfg: Config, kind: str, cwd: Path | None = None) -> str:
    """Shell command that runs the master.

    An explicit ``master_cmd`` (``SWARM_MASTER_CMD`` / config) short-circuits to
    that command — this is how the tests inject ``fake-master.sh``. Otherwise a
    default ``claude`` invocation is returned; the prompt is delivered separately
    by :meth:`Master._spawn_tmux` and pinned during the owner's live smoke (real
    ``claude`` is out of scope for the hermetic tests).

    An Overseer gets what a worker gets — in-process teammates, the meters tap,
    the effort level — in ``cwd`` (its own mirror under worktree isolation), and
    runs ``[overseer].model`` or else the master's.
    """
    override = _override(cfg, kind)
    if override:
        return override
    if kind == OVERSEER:
        model = cfg.overseer_model or cfg.master_model
        base = "claude" + (f" --model {shlex.quote(model)}" if model else "") + " -n overseer"
        return launch_mod._worker_shell(cfg, OVERSEER, cwd or cfg.project_dir, base)
    model = f" --model {cfg.master_model}" if cfg.master_model else ""
    return f"cd {cfg.project_dir} && exec claude{model}"


def overseer_brief(
    cfg: Config, pass_id: str, digest: Path, record: Path, cwd: Path | None
) -> str:
    """An Overseer session's brief. Written to a file, never typed into the pane:
    see :func:`overseer_line`."""
    prompt_file = resolver.prompt_path(_PROMPTS[OVERSEER])
    where = (
        f"Your cwd {cwd} is your own full-workspace mirror (branch swarm/{cwd.name}):"
        " commit your ledger edits there; the swarm merges them when you finish."
        if cwd is not None
        else f"Your cwd is the project itself, {cfg.project_dir}; commit there."
    )
    swarm = f"swarm --project-dir {shlex.quote(str(cfg.project_dir))}"
    return (
        f"Read {prompt_file} and follow it exactly. You are the swarm's Overseer, pass"
        f" {pass_id}, for the project at {cfg.project_dir}. Read your digest first: {digest}."
        f" Write your pass record in {record}. {where} Run swarm commands as `{swarm} <command>`."
        f' When the pass is over run `{swarm} overseer-done "<one-line summary>"`.'
    )


def overseer_line(pass_id: str, brief_file: Path) -> str:
    """The one short line the Overseer's pane is handed: a pointer to its brief.

    The brief itself runs ~900 characters on a real project (four long state
    paths), and Claude Code folds pasted text that long into ``[Pasted text #1]``
    — the submit check then never sees the text land, and the pass
    died as "prompt would not submit", the same way operator hand-offs did
    before their brief moved to a file."""
    return (
        f"You are the swarm's Overseer, pass {pass_id}. Read your full brief in"
        f" {brief_file} first, then do exactly what it says."
    )


class Master:
    """Supervisor-owned handle to the one live master session."""

    def __init__(self, cfg: Config, log: Log) -> None:
        self.cfg = cfg
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.pane: str | None = None
        # Prompt deliveries in a row that timed out or would not submit.
        self._timeouts = 0

    def is_alive(self) -> bool:
        """Is a master actually running right now?

        Under tmux this asks the pane, rather than trusting that we once recorded
        an id for it. ``self.pane is not None`` stays true forever after the first
        spawn, so a master whose pane has since died reported alive and every
        nudge was typed into nothing — and callers that skip work when a master is
        alive (the relaunch path, the settled-run finish) skipped it silently.
        A vanished pane makes ``display-message`` exit non-zero; a pane whose
        command exited reports ``pane_dead`` = 1 (we set ``remain-on-exit on``, so
        it lingers visibly instead of disappearing).
        """
        if self.cfg.driver == "bare":
            return self.proc is not None and self.proc.poll() is None
        if self.pane is None:
            return False
        probe = tmux.run(
            ["display-message", "-p", "-t", self.pane, "#{pane_dead}"], check=False
        )
        return probe.returncode == 0 and probe.stdout.strip() != "1"

    def spawn(
        self,
        kind: str,
        master_pane: str | None = None,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        line: str | None = None,
    ) -> bool:
        """Start a fresh master (``kind`` is ``init`` or ``overseer``).

        ``cwd`` is where a built-in session works, ``env`` rides on top of the
        run's own variables, and ``line`` replaces the default prompt line.
        Returns True on success. A False return means no master is running, so
        the caller must NOT mark the master alive.
        """
        cmd = master_command(self.cfg, kind, cwd)
        if self.cfg.driver == "bare":
            ok = self._spawn_bare(cmd, env)
        else:
            ok = self._spawn_tmux(cmd, kind, master_pane, env, line)
        if ok:
            self.log.line(f"ACTION spawn-master kind={kind}")
        return ok

    def _spawn_bare(self, cmd: str, env: dict[str, str] | None = None) -> bool:
        try:
            self.proc = subprocess.Popen(
                ["/bin/sh", "-c", cmd],
                cwd=str(self.cfg.project_dir),
                env={**os.environ, **env} if env else None,
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

    def _spawn_tmux(
        self,
        cmd: str,
        kind: str,
        master_pane: str | None,
        env: dict[str, str] | None = None,
        line: str | None = None,
    ) -> bool:
        if master_pane is None:
            self.log.line("ACTION spawn-master-failed no-pane")
            return False
        self.pane = master_pane
        tmux.respawn_pane(master_pane, cmd, env={**_master_env(self.cfg), **(env or {})})
        if not _override(self.cfg, kind) and not self._deliver_prompt(master_pane, kind, line):
            # Never primed (boot timed out / stuck on a modal): treat as a spawn
            # failure so the caller does NOT mark the master alive — otherwise the
            # next `done` would inject into a master that never got its prompt.
            self.pane = None
            return False
        return True

    def _deliver_prompt(self, pane: str, kind: str, line: str | None = None) -> bool:
        """Point a freshly launched claude master at its prompt file.

        Delivered as ONE line with no embedded newlines: tmux ``send-keys``
        submits on every newline, so pasting the multi-line prompt file would
        fire it off line-by-line. We tell the master to *read* the file instead.
        Waits for readiness (dismissing a first-run folder-trust prompt) and
        returns False on timeout instead of blindly typing into a not-ready pane.
        The fake-master override never reaches this path.
        """
        prompt_file = resolver.prompt_path(_PROMPTS.get(kind, f"{kind}_master.md"))
        if not prompt_file.is_file():
            self.log.line(f"ACTION prompt-missing {prompt_file}")
            return False
        if not launch_mod.await_ready(self.cfg, pane, self.log):
            self.log.line("ACTION master-ready-timeout")
            telegram.notify(
                self.cfg.telegram_notify,
                f"swarm: the {_NAMES.get(kind, kind)} session would not start (it never"
                " became ready)."
                " Workers carry on; if this keeps happening, check the overseer window.",
                kind="master-timeout",
                source="master._deliver_prompt",
                suppressed=self._timeout_hold(),
            )
            return False
        line = line or (
            f"Read {prompt_file} and follow every instruction in it exactly. "
            f"You are orchestrating the project at {self.cfg.project_dir}."
        )
        if not tmux.send_submit(pane, line):
            self.log.line("ACTION master-submit-lost")
            telegram.notify(
                self.cfg.telegram_notify,
                f"swarm: the {_NAMES.get(kind, kind)} session started but would not take"
                " its instructions."
                " Workers carry on; if this keeps happening, check the overseer window.",
                kind="master-timeout",
                source="master._deliver_prompt",
                suppressed=self._timeout_hold(),
            )
            return False
        self._timeouts = 0
        return True

    def _timeout_hold(self) -> str | None:
        """Count one more failed delivery; the hold reason unless it makes a streak.

        A single one is retried by the next pass and is not the owner's problem;
        :data:`telegram.STREAK` in a row is a master that cannot start at all."""
        self._timeouts += 1
        if self._timeouts % telegram.STREAK == 0:
            return None
        return telegram.hold(
            self.cfg, f"failure {self._timeouts} in a row; you hear at {telegram.STREAK}")

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

    def detach(self) -> None:
        """Let go of a session that goes on without the master pane: an Overseer
        pass parked on the owner in its own window. It is no longer this master,
        so a later :meth:`kill` must not reach it; its own end reaps it."""
        self.proc = None
        self.pane = None
        self.log.line("ACTION detach-master")

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
