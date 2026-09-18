"""Live probes: what ``claude``, ``tmux`` and ``git`` say right now.

Everything here talks to a subprocess, so every function has two halves: a
``parse_*`` that is pure (and tested) and a caller that shells out. The split
matters because the callers all run on a Textual worker thread — the dashboard
must never block on a ``claude agents`` that takes two seconds — while the
parsing is where the actual bugs live.

Failure is normal here, not exceptional: ``claude`` may not be on PATH, the tmux
server may be gone, a worktree may have been discarded mid-read. Every function
returns an empty/``None`` result in that case; nothing raises.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .data import coerce_ts

#: Probes are polled from a UI worker; a hung one would silently stop refreshing
#: the panel it feeds, so every call is bounded.
TIMEOUT = 8.0


def _run(args: list[str], timeout: float = TIMEOUT, cwd: str | None = None):
    """Run a command, returning the CompletedProcess or ``None`` if it can't run."""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


# -- claude agents --------------------------------------------------------
@dataclass(frozen=True)
class AgentInfo:
    """One entry of ``claude agents --json``."""

    pid: int | None = None
    cwd: str = ""
    kind: str = ""
    started_at: float | None = None
    session_id: str = ""
    name: str = ""
    status: str = "unknown"  # busy | idle | waiting
    waiting_for: str = ""


def parse_agents(text: str) -> list[AgentInfo]:
    """Parse ``claude agents --json`` output.

    Accepts a bare list or a ``{"agents": [...]}`` wrapper, and tolerates both
    ``startedAt``/``started_at`` spellings, because this is an external tool's
    output that this project does not control.
    """
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(obj, dict):
        obj = obj.get("agents") or obj.get("sessions") or []
    if not isinstance(obj, list):
        return []
    out: list[AgentInfo] = []
    for item in obj:
        if not isinstance(item, dict):
            continue
        pid = item.get("pid")
        out.append(
            AgentInfo(
                pid=int(pid) if isinstance(pid, (int, float)) and not isinstance(pid, bool) else None,
                cwd=str(item.get("cwd") or ""),
                kind=str(item.get("kind") or ""),
                started_at=coerce_ts(item.get("startedAt") or item.get("started_at")),
                session_id=str(item.get("sessionId") or item.get("session_id") or ""),
                name=str(item.get("name") or ""),
                status=str(item.get("status") or "unknown"),
                waiting_for=str(item.get("waitingFor") or item.get("waiting_for") or ""),
            )
        )
    return out


def agents() -> list[AgentInfo]:
    """Live claude sessions on this host (``[]`` when claude isn't available)."""
    if not shutil.which("claude"):
        return []
    proc = _run(["claude", "agents", "--json"])
    return parse_agents(proc.stdout) if proc and proc.stdout else []


def match_agent(agents_: list[AgentInfo], phase: str | None, worktree: str | None) -> AgentInfo | None:
    """The agent running ``phase``, matched on cwd first and name second.

    cwd is the reliable key under ``isolation = worktree`` — each worker runs in
    its own worktree path — but under ``isolation = none`` every worker shares the
    project dir, so the session *name* (``worker:<phase>``) is the only
    discriminator. Name matching is bounded to a whole-token match so ``P1``
    cannot claim ``P10``'s session.
    """
    if worktree:
        target = str(Path(worktree))
        for agent in agents_:
            if agent.cwd and (agent.cwd == target or agent.cwd.startswith(target + "/")):
                return agent
    if phase:
        for agent in agents_:
            if phase in re.split(r"[^A-Za-z0-9._/-]+", agent.name):
                return agent
    return None


# -- tmux -----------------------------------------------------------------
@dataclass(frozen=True)
class PaneInfo:
    """What tmux knows about one pane."""

    pane_id: str
    title: str = ""
    command: str = ""
    window_id: str = ""
    window_name: str = ""

    #: tmux's own verdict (``#{pane_dead}``): the command in this pane exited.
    dead: bool = False

    @property
    def alive(self) -> bool:
        """Whether this pane's command is still running.

        A slot whose worker died looks completely normal in ``state.json`` —
        still busy, still holding its phase — and the run simply stops
        advancing. This is the signal that says otherwise.

        It asks tmux (``#{pane_dead}``) rather than matching the command name.
        It used to test ``"claude" in command``, which is wrong twice over:
        ``[worker].worker_cmd`` is configurable, so any project not launching a
        binary called ``claude`` had every live worker reported GONE — and the
        home panel, which decides liveness differently, then disagreed with the
        workers tab about the same slot. A dashboard contradicting itself is
        worse than one that says nothing.

        Asking tmux is only sufficient because ``tmux.harden`` now sets
        ``remain-on-exit on`` before any pane is created: an exited command
        leaves the pane in place and flagged dead, instead of the pane vanishing
        or falling back to a shell.
        """
        return not self.dead


PANE_FORMAT = (
    "#{pane_id}\t#{pane_title}\t#{pane_current_command}"
    "\t#{window_id}\t#{window_name}\t#{pane_dead}"
)


def parse_panes(text: str) -> dict[str, PaneInfo]:
    """Parse tab-separated ``tmux list-panes -F PANE_FORMAT`` output, keyed by pane id."""
    out: dict[str, PaneInfo] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        parts += [""] * (6 - len(parts))
        pane_id = parts[0].strip()
        if not pane_id:
            continue
        out[pane_id] = PaneInfo(
            pane_id=pane_id,
            title=parts[1].strip(),
            command=parts[2].strip(),
            window_id=parts[3].strip(),
            window_name=parts[4].strip(),
            dead=parts[5].strip() == "1",
        )
    return out


def panes() -> dict[str, PaneInfo]:
    """Every pane on the tmux server (``{}`` when there is no server)."""
    proc = _run(["tmux", "list-panes", "-a", "-F", PANE_FORMAT])
    return parse_panes(proc.stdout) if proc and proc.returncode == 0 else {}


def capture(pane_id: str, lines: int = 40) -> str:
    """The last ``lines`` visible rows of a pane ("" when it's gone)."""
    proc = _run(["tmux", "capture-pane", "-p", "-t", pane_id, "-S", f"-{max(0, lines)}"])
    return proc.stdout if proc and proc.returncode == 0 else ""


def jump_to(pane_id: str, window_id: str | None = None) -> bool:
    """Move the attached tmux client to ``pane_id``. False if tmux refused.

    Selecting the window first is required: ``select-pane`` alone only changes the
    active pane *within* its window, so jumping to a worker on another window
    would silently do nothing visible.
    """
    if window_id:
        _run(["tmux", "select-window", "-t", window_id])
    proc = _run(["tmux", "select-pane", "-t", pane_id])
    return bool(proc and proc.returncode == 0)


_CONTEXT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*([kKmM])?\s*/\s*(\d+(?:\.\d+)?)\s*([kKmM])", re.ASCII
)
_UNITS = {"k": 1_000, "m": 1_000_000, "": 1}


def parse_context(text: str) -> tuple[float, float, float] | None:
    """``(used, total, percent)`` from a claude status bar's ``420k/1.0M`` token.

    The *total* is required to carry a ``k``/``M`` suffix. Pane text is full of
    innocent ``1/2``-shaped tokens (dates, "step 1/3", diff stats) and without
    that anchor the context meter reads whichever one happened to scroll past.
    The last match wins, since the status bar is painted at the bottom.
    """
    match = None
    for match in _CONTEXT_RE.finditer(text or ""):
        pass
    if match is None:
        return None
    used = float(match.group(1)) * _UNITS[(match.group(2) or "").lower()]
    total = float(match.group(3)) * _UNITS[match.group(4).lower()]
    if total <= 0:
        return None
    return used, total, min(100.0, 100.0 * used / total)


# -- git ------------------------------------------------------------------
def parse_rev_count(text: str) -> int | None:
    """``git rev-list --count`` output as an int, or ``None`` if it didn't run."""
    try:
        return int((text or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def parse_dirty(text: str) -> int:
    """Number of changed paths in ``git status --porcelain`` output."""
    return sum(1 for line in (text or "").splitlines() if line.strip())


@dataclass(frozen=True)
class RepoStat:
    """A worker's branch, as of the last poll."""

    commits: int | None = None
    dirty: int = 0


def repo_stat(worktree: str | None, main_branch: str) -> RepoStat:
    """Commits ahead of ``main_branch`` and dirty-file count for one worktree."""
    if not worktree or not Path(worktree).is_dir():
        return RepoStat()
    ahead = _run(["git", "-C", worktree, "rev-list", "--count", f"{main_branch}..HEAD"])
    dirty = _run(["git", "-C", worktree, "status", "--porcelain"])
    return RepoStat(
        commits=parse_rev_count(ahead.stdout) if ahead and ahead.returncode == 0 else None,
        dirty=parse_dirty(dirty.stdout) if dirty and dirty.returncode == 0 else 0,
    )


# -- the swarm CLI itself -------------------------------------------------
@dataclass
class CommandResult:
    """The outcome of shelling out to ``swarm <something>``."""

    argv: list[str] = field(default_factory=list)
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    unavailable: bool = False  # the subcommand doesn't exist in this build

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()


_MISSING_COMMAND_MARKERS = ("invalid choice", "no such command", "unknown command")
_MISSING_FLAG_MARKERS = ("unrecognized arguments", "unrecognized argument")


def looks_unavailable(result: CommandResult) -> bool:
    """Whether a non-zero exit means "this build has no such subcommand".

    Several of the dashboard's actions (``doctor``, ``reload``, ``gc``, ``why``,
    ``recap``) exist as modules but are wired into the CLI on their own schedule,
    so any of them can be missing on a given day. argparse answers a missing
    subcommand with exit 2 and ``invalid choice:`` on stderr — telling the owner
    "not available yet" is far better than showing them raw argparse usage and
    letting them conclude the dashboard is broken.
    """
    if result.returncode in (None, 0):
        return False
    haystack = (result.stderr or "").lower()
    return any(marker in haystack for marker in _MISSING_COMMAND_MARKERS)


def looks_missing_flag(result: CommandResult) -> bool:
    """Whether the subcommand exists but rejected a flag we passed.

    Distinct from :func:`looks_unavailable` on purpose. ``swarm doctor --json``
    failing because ``--json`` hasn't been added yet is a completely different
    situation from ``swarm doctor`` not existing: the first is worth retrying
    without the flag, the second is not. Collapsing them would report a working
    doctor as "not available in this build".
    """
    if result.returncode in (None, 0):
        return False
    haystack = (result.stderr or "").lower()
    return any(marker in haystack for marker in _MISSING_FLAG_MARKERS)


def swarm(args: list[str], project_dir: str | None = None, timeout: float = 60.0) -> CommandResult:
    """Shell out to the real ``swarm`` CLI rather than reimplementing an action.

    Actions must go through the same code path the owner would type by hand — a
    dashboard that mutates ``state.json`` itself is a second implementation of
    every rule (the flock discipline, the FIFO poke, the no-supervisor warning)
    and would drift from the real one silently.
    """
    # An editable/uninstalled checkout has no `swarm` on PATH; the module entry
    # point is the same program, so the dashboard works either way.
    base = ["swarm"] if shutil.which("swarm") else [sys.executable, "-m", "swarm_orchestrator"]
    scope = ["--project-dir", project_dir] if project_dir else []
    argv = [*base, *scope, *args]
    proc = _run(argv, timeout=timeout, cwd=project_dir)
    if proc is None:
        return CommandResult(argv=argv, returncode=None, stderr="could not run `swarm`")
    result = CommandResult(
        argv=argv, returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or ""
    )
    result.unavailable = looks_unavailable(result)
    return result


def parse_doctor(text: str) -> list[dict]:
    """Normalise ``swarm doctor --json`` into ``[{name, status, detail}]``.

    The doctor command is being written concurrently, so its exact JSON shape
    isn't settled. Both a bare list of checks and a ``{"checks": [...]}`` wrapper
    are accepted, and each check's status is read from whichever of the plausible
    keys is present — the alternative is a Doctor tab that renders nothing the
    day the sibling agent picks the other spelling.
    """
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return []
    if isinstance(obj, dict):
        obj = obj.get("checks") or obj.get("results") or []
    if not isinstance(obj, list):
        return []
    out: list[dict] = []
    for item in obj:
        if isinstance(item, str):
            out.append({"name": item, "status": "ok", "detail": ""})
            continue
        if not isinstance(item, dict):
            continue
        status = item.get("status") or item.get("level") or item.get("result")
        if status is None and "ok" in item:
            status = "ok" if item["ok"] else "fail"
        out.append(
            {
                "name": str(item.get("name") or item.get("check") or item.get("id") or "?"),
                "status": str(status or "ok").lower(),
                "detail": str(item.get("detail") or item.get("message") or item.get("hint") or ""),
            }
        )
    return out
