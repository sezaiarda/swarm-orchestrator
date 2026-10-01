"""``swarm doctor`` — what is wrong with this swarm *right now*.

The supervisor is deliberately minimal: it reacts to FIFO events and a few timed
wakes (park deadlines, launch back-off, the watchdog sweep that reaps dead panes,
the Overseer and operator timers, gc) and logs what it does, and that is all.
Every failure mode below shares one property —
**it produces no log line at all**, so the run looks healthy from every angle the
tool currently offers. A stale ``supervisor_pid`` reads as alive. A worker whose
``/prime`` was never delivered logs a clean ``LAUNCH``. A held integration
telegrams once and is silent forever. A ``notify.sh`` that fails on every send is
indistinguishable from one that works.

This module packages the conditions an owner would otherwise watch for with an
ad-hoc script kept in a state directory. The condition list is the load-bearing
part; on top of it sit the checks such a script has no way to make from outside
the process (FIFO readership, stray supervisors, sentinel-vs-state disagreement,
the notification ledger).

Everything here is **read-only** with respect to the project: it never touches
git, never sends anything, never mutates run state. A check that knows the fix
says so in ``fix_hint`` and leaves the decision to the owner. The single write it
makes is its own disk-growth sample inside the state dir, which is how a *rate*
is observable at all from a one-shot command.

Every probe degrades to a non-answer rather than a wrong answer: a git call that
fails, a tmux that is absent, a ``du`` that times out all report "unknown" and
stay ``ok``. A monitor that cries wolf is a monitor the owner learns to ignore,
which is exactly how a real stall goes unnoticed for an hour.

Exit-code convention for the CLI wrapper: ``0`` when nothing failed, ``1`` when
any check is ``fail``. **Warnings alone still exit 0** — a warn is "look at this
when you get a chance", and wiring it to a non-zero exit would make every
scripted caller treat the normal state of a busy swarm as an error.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NamedTuple

from . import backup as backup_mod
from . import caps
from . import gc as gc_mod
from . import gitq
from . import keep as keep_mod
from . import launch as launch_mod
from . import ledger as ledger_mod
from . import ledgerw
from . import opqueue
from . import pushowed
from . import restart as restart_mod
from . import state as state_mod
from . import statuses
from . import telegram, tgbot
from .config import Config
from .logutil import parse_ts, read_all
from .master import build_context
from .resources import view as resources_view
from .state import State
from .web import lifecycle as web_lifecycle

OK = "ok"
WARN = "warn"
FAIL = "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

# A busy slot whose worktree holds zero bytes of work this long after LAUNCH is
# the signature of a `/prime` that never arrived. Left alone it sits idle for
# hours; a watcher polling every half hour needs two ticks to notice.
# 20 minutes is well past any plausible cold start and still catches it early.
_IDLE_GRACE_S = 20 * 60
# How long a claimed slot may wait for its worker before the claim reads as a
# launch that died. A launch claims the slot, builds the phase's mirror, then
# awaits the boot at most twice (``launch.READY_TIMEOUT_S`` each) and sends the
# command. The mirror has no limit of its own, so it gets as long as both boots.
_START_GRACE_S = 4 * launch_mod.READY_TIMEOUT_S
# A held integration freezes the entire queue — every slot drains and none refill.
# It is worth an alert almost immediately.
_BLOCKED_WARN_S = 5 * 60
# The owner being the blocker is normal for a while, then it is the run stalling.
_WAIT_WARN_S = 15 * 60
#: An operator job waiting on the owner longer than this is worth a line. Longer
#: than a worker's: the operator blocks no phase, only the next operator job.
_OPERATOR_WAIT_WARN_S = 3600
# No supervisor event at all for this long, with work still in flight.
_STALL_WARN_S = 90 * 60

_DISK_WARN_BYTES = 40 * 1024**3
_GROWTH_WARN_BYTES_PER_H = 5 * 1024**3
_DU_TIMEOUT_S = 20.0
_GIT_TIMEOUT_S = 30.0
#: Where an external-repo row keeps its own worktree, one per phase.
_LANE_ROOT = Path.home() / ".cache" / "swarm-lanes"
_TMUX_TIMEOUT_S = 10.0
_SAMPLE_NAME = ".doctor-disk.json"
#: A tmpfs ``/tmp`` fuller than this is RAM the box does not have spare.
_TMP_WARN_PCT = 80.0
_TMP_ROOT = Path("/tmp")

_SENTINEL_STATUSES = statuses.ALL


@dataclass(frozen=True)
class Check:
    """One diagnosis. ``fix_hint`` is a command or action, never applied for you."""

    name: str
    status: str
    detail: str
    fix_hint: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# -- probes ---------------------------------------------------------------
def _read_state(cfg: Config) -> State | None:
    """State, retried on a torn read.

    ``state.json`` is replaced atomically under flock, but ``doctor`` is the one
    reader that runs *while* things are going wrong, so it retries rather than
    reporting a transient decode error as a fault.
    """
    for _ in range(3):
        try:
            return state_mod.read(cfg)
        except (OSError, ValueError):
            time.sleep(0.4)
    return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False
    return True


def _fifo_has_reader(fifo: Path) -> bool:
    """True when *something* holds the control FIFO open for reading.

    This is the check that actually answers "can a ``swarm done`` poke be
    delivered". A recorded pid proves only that a process with that number
    exists — the number is reused, and a supervisor can die leaving its pid
    recorded and (worse) re-assigned. Opening ``O_WRONLY|O_NONBLOCK`` succeeds
    only if a reader is attached; otherwise it is ``ENXIO``.
    """
    if not fifo.exists():
        return False
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(fd)
    return True


def _pid_holds_fifo(pid: int | None, fifo: Path) -> bool | None:
    """Whether ``pid`` is the process holding the FIFO. ``None`` = can't tell.

    Reads ``/proc/<pid>/fd``. Combined with :func:`_fifo_has_reader` this is what
    separates "the supervisor is running" from "*a* supervisor is running" — the
    stray-co-reader case, where two supervisors race every event and each poke is
    seen by only one of them.
    """
    if not pid:
        return None
    target = str(fifo.resolve()) if fifo.exists() else str(fifo)
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            if os.readlink(entry) == target:
                return True
        except OSError:
            continue
    return False


def _pane_cmd(pane_id: str) -> str:
    """The command running in a pane; ``gone`` if the pane is dead, ``?`` if tmux
    could not be asked (never report on a failed probe)."""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_current_command}"],
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return "?"
    return (out.stdout.strip() or "gone") if out.returncode == 0 else "gone"


def _git_lines(path: Path, *args: str) -> int | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return len([ln for ln in out.stdout.splitlines() if ln.strip()])


def _mirror_dirs(cfg: Config, phase: str, umbrella: Path) -> list[Path]:
    """The umbrella worktree plus every component repo mirrored inside it.

    A phase's code usually lives in a *sibling* repo, not the umbrella, and a
    nested repo is invisible to the umbrella's ``git status``. Checking only the
    umbrella would report a hard-working phase as having written nothing.
    """
    dirs = [umbrella]
    project = cfg.project_dir.resolve()
    for repo in gitq.discovered_repos(cfg):
        try:
            rel = repo.resolve().relative_to(project)
        except ValueError:
            continue
        nested = umbrella / rel
        if nested.is_dir():
            dirs.append(nested)
    return dirs


def _worktree_activity(cfg: Config, phase: str, worktree: str) -> int:
    """Bytes-of-work proxy for a phase: dirty files + commits ahead of main.

    Zero means the worker has written literally nothing, anywhere in its mirror.
    ``-1`` means we could not tell (no git, not a repo) and nothing is reported.

    Deliberately not read off the pane: a ``/prime`` line scrolls out of tmux's
    scrollback on a long session, so its absence proves nothing. Written bytes
    prove work.
    """
    root = Path(worktree)
    if not root.is_dir():
        return -1
    total = 0
    known = False
    for path in _mirror_dirs(cfg, phase, root):
        dirty = _git_lines(path, "status", "--porcelain")
        if dirty is None:
            continue
        known = True
        total += dirty
        ahead = _git_lines(
            path, "log", "--oneline", f"{cfg.git_main_branch}..HEAD"
        )
        total += ahead or 0
    return total if known else -1


def _lane_activity(cfg: Config, phase: str, row_dirs: dict[str, list[str]], since: float) -> int:
    """Work an external-repo row wrote outside its mirror.

    Such a row never touches its umbrella mirror: it builds in its own worktree
    at ``~/.cache/swarm-lanes/<phase>`` on ``lane/<phase>`` and lands that branch
    in the repo itself, then removes the worktree. Its work is the worktree's
    dirty files, the branch's commits ahead of the repo's checked-out branch, and,
    once landed, the commits since launch whose message cites the phase — so the
    row's own ``swarm doctor`` proof after landing still sees it. ``0`` for a row
    whose ``dir:`` names no ``[lanes] external`` repo.
    """
    total = 0
    lane = _LANE_ROOT / phase
    for name in ledger_mod.home(phase, row_dirs):
        path = cfg.lanes_external.get(name)
        if path is None:
            continue
        repo = Path(path).expanduser()
        if lane.is_dir():
            total += _git_lines(lane, "status", "--porcelain") or 0
        total += _git_lines(repo, "log", "--oneline", f"HEAD..lane/{phase}") or 0
        total += _cited_commits(repo, phase, since)
    return total


def _cited_commits(repo: Path, phase: str, since: float) -> int:
    """Commits on ``repo``'s checked-out branch since ``since`` that cite ``phase``
    as a whole id (``lane-W1`` is not cited by ``lane-W10``)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", f"--since=@{int(since)}", "--format=%B%x00", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if out.returncode != 0:
        return 0
    cite = re.compile(rf"(?<![\w-]){re.escape(phase)}(?![\w-])")
    return sum(1 for body in out.stdout.split("\0") if cite.search(body))


def _row_dirs(cfg: Config) -> dict[str, list[str]]:
    try:
        return ledger_mod.dirs((cfg.project_dir / cfg.ledger).read_text(encoding="utf-8"))
    except OSError:
        return {}


def _log_ts(cfg: Config, prefix: str) -> float | None:
    """Wall-clock time of the last supervisor-log line starting with ``prefix``.

    The log is the only place a per-phase *timestamp* exists — state.json records
    what is happening, never when it started — so ages ("blocked for 4h") come
    from here. Reads the tail only; the log grows unbounded over a campaign.
    """
    for line in reversed(_log_tail(cfg)):
        ts, message = parse_ts(line)
        if message.startswith(prefix):
            return ts
    return None


def _log_tail(cfg: Config) -> list[str]:
    # The newest rotated file too, so a rotation a minute ago loses nothing.
    return read_all(cfg.supervisor_log, keep=1).splitlines()[-4000:]


def _unlaunched(cfg: Config, phase: str) -> float | None:
    """When ``phase`` was last claimed, if no ``LAUNCH`` line follows that claim.

    ``launch.launch_outcome`` logs ``CLAIM`` once the slot is the phase's and
    ``LAUNCH`` once its worker has been sent its command. In between the slot
    is busy while the mirror is built and the session boots, and its pane runs
    the idle placeholder, then a shell, then the worker. ``None`` once
    launched, and when the log's tail holds no claim.
    """
    for line in reversed(_log_tail(cfg)):
        ts, message = parse_ts(line)
        if message.startswith(f"LAUNCH {phase} "):
            return None
        if message.startswith(f"CLAIM {phase} "):
            return ts
    return None


def _dir_size(path: Path) -> int | None:
    """``du -sb`` with a timeout. ``None`` when it is too slow to be worth it —
    a multi-gigabyte cargo cache can take longer to measure than doctor should run.
    """
    if not path.exists():
        return 0
    try:
        out = subprocess.run(
            ["du", "-sb", str(path)],
            capture_output=True,
            text=True,
            timeout=_DU_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        return int(out.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def _notifications(cfg: Config) -> list[dict]:
    """Rows of the notification ledger, oldest first. ``[]`` if it has none."""
    path = cfg.state_dir / telegram.LEDGER_NAME
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _sentinels(cfg: Config) -> dict[str, str]:
    """``{phase: status}`` from the durable ``done/`` sentinels."""
    out: dict[str, str] = {}
    try:
        entries = list(cfg.done_dir.iterdir())
    except OSError:
        return out
    for entry in entries:
        if entry.name.startswith(".") or "." not in entry.name:
            continue
        phase, _, status = entry.name.rpartition(".")
        if phase and status in _SENTINEL_STATUSES:
            out[phase] = status
    return out


def _human_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _human_bytes(n: int | None) -> str:
    if n is None:
        return "unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n} B"


# -- individual checks ----------------------------------------------------
def _check_supervisor(cfg: Config, st: State) -> list[Check]:
    """Recorded pid, actual FIFO readership, and any stray second supervisor.

    Three separate facts that today are conflated into one. They disagree in
    exactly the cases that matter: a dead supervisor whose pid number got reused
    reads as healthy; a supervisor that lost its FIFO cannot receive a single
    ``swarm done``; and a surviving detached supervisor from a previous ``up``
    silently splits every event between two readers.
    """
    pid = st.supervisor_pid
    alive = _pid_alive(pid)
    reader = _fifo_has_reader(cfg.fifo_path)
    holds = _pid_holds_fifo(pid, cfg.fifo_path) if alive else None
    checks: list[Check] = []

    plan = restart_mod.load(cfg)
    if not alive and plan.get("stage") in (restart_mod.STOPPING, restart_mod.RESTARTING) \
            and _pid_alive(plan.get("runner_pid")):
        # `swarm restart` is between two supervisors: it holds the FIFO itself,
        # so no poke is lost, and the next supervisor is a moment away.
        why = "a restart is replacing the supervisor right now"
        return [Check("supervisor.pid", OK, why), Check("supervisor.fifo", OK, why),
                Check("supervisor.stray", OK, why)]

    if st.finished and not st.pending():
        # `finished` is the supervisor's own exit flag: it stops the loop right
        # after setting it, so a dead pid here is the DESIGNED end state, not a
        # fault. Without this guard a completed run looks identical to a
        # supervisor that died mid-flight, and the check cries wolf forever.
        checks.append(
            Check("supervisor.pid", OK, f"run finished; supervisor pid={pid} exited by design")
        )
    elif not pid:
        checks.append(
            Check(
                "supervisor.pid",
                FAIL,
                "no supervisor pid recorded and the run is not finished",
                "swarm up",
            )
        )
    elif not alive:
        checks.append(
            Check(
                "supervisor.pid",
                FAIL,
                f"supervisor pid {pid} is gone with work still in flight — "
                "`swarm done` pokes hit ENXIO and vanish (sentinels survive)",
                "swarm restart  # starts a supervisor that takes the run over as it is:"
                " workers and parked sessions stay (`swarm down && swarm up` closes them)",
            )
        )
    else:
        checks.append(Check("supervisor.pid", OK, f"pid {pid} alive"))

    if not cfg.fifo_path.exists():
        checks.append(
            Check(
                "supervisor.fifo",
                OK if st.finished else FAIL,
                f"control FIFO missing: {cfg.fifo_path}",
                None if st.finished else "swarm up",
            )
        )
    elif reader:
        checks.append(Check("supervisor.fifo", OK, "control FIFO has a reader attached"))
    elif st.finished:
        checks.append(
            Check("supervisor.fifo", OK, "control FIFO has no reader (run finished — expected)")
        )
    else:
        checks.append(
            Check(
                "supervisor.fifo",
                FAIL,
                "control FIFO has NO reader — every poke is silently dropped"
                + (f" (pid {pid} is alive but not reading it)" if alive else ""),
                "swarm down && swarm up",
            )
        )

    if reader and holds is False:
        checks.append(
            Check(
                "supervisor.stray",
                FAIL,
                f"a supervisor holds the FIFO but it is NOT pid {pid} — two "
                "supervisors are racing; each event reaches only one of them",
                f"find it (`pgrep -fa _supervise`), kill it, then re-check pid {pid}",
            )
        )
    elif reader and not alive and pid:
        checks.append(
            Check(
                "supervisor.stray",
                FAIL,
                f"the FIFO has a reader but recorded pid {pid} is dead — a stray "
                "supervisor from an earlier `up` co-opted this run",
                "pgrep -fa _supervise",
            )
        )
    else:
        checks.append(Check("supervisor.stray", OK, "no stray supervisor detected"))
    return checks


class PaneProbe(NamedTuple):
    """What one pass over the slots' panes found (:func:`_dead_panes`)."""

    dead: list[tuple[str, str]]  # (phase, what its pane runs) per dead worker
    unknown: int  # panes tmux could not be asked about
    busy: int  # busy slots with a pane, landing ones included
    dead_free: list[str]  # free slots with nowhere to launch into
    landing: list[str]  # busy phases whose worker has reported
    starting: list[str]  # busy phases claimed moments ago, their worker not launched yet


def _landing(st: State) -> set[str]:
    """Phases whose finished work is on its way to main: queued or held for
    merging, or holding a landing lock. Their status reaches the ``done`` map
    when they land."""
    return st.integrating() | set(st.landing)


def _reported(cfg: Config, st: State, phase: str) -> bool:
    """``phase``'s worker has run ``swarm done`` in this attempt.

    The supervisor then puts the slot's pane back on ``sleep`` and keeps the
    slot until the work has landed (:meth:`Supervisor._end_worker`), which is
    minutes while a landing check runs. A sentinel counts only when written
    since the phase was last claimed, the supervisor's own rule
    (``_unread_report``): one left by an earlier attempt says nothing about
    the worker running now.
    """
    if phase in _landing(st):
        return True
    written: list[float] = []
    for status in _SENTINEL_STATUSES:
        try:
            written.append((cfg.done_dir / f"{phase}.{status}").stat().st_mtime)
        except OSError:
            continue
    if not written:
        return False
    claimed = _log_ts(cfg, f"CLAIM {phase} ")
    return claimed is not None and max(written) >= claimed - 1.0


def _dead_panes(cfg: Config, st: State) -> PaneProbe:
    """Every slot's pane, probed once.

    Shared by :func:`_check_panes` and :func:`_check_watchdog` so a swarm with
    four busy slots costs four ``tmux`` calls, not eight.

    Every slot is probed, not only the busy ones. A *free* slot with no pane (or
    a pane tmux no longer knows) is invisible until something launches into it —
    and then every launch that picks it fails ``no-pane``: that is what a
    ``swarm reload`` growing ``max_workers`` left behind before it created panes.
    A free pane only has to exist; it runs the ``sleep`` placeholder, not claude.

    A busy slot whose worker has reported (:func:`_reported`) is not probed:
    its pane runs the same placeholder by design, and it is no dead worker.
    Neither is one claimed within :data:`_START_GRACE_S` whose worker has not
    been launched yet (:func:`_unlaunched`). Past that it is probed like any
    other, so a launch that died is not hidden.
    """
    busy = [s for s in st.busy_slots() if s.pane_id]
    if cfg.driver != "tmux":
        return PaneProbe([], 0, len(busy), [], [], [])
    dead: list[tuple[str, str]] = []
    dead_free: list[str] = []
    landing: list[str] = []
    starting: list[str] = []
    unknown = 0
    now = time.time()
    for slot in busy:
        if slot.phase and _reported(cfg, st, slot.phase):
            landing.append(slot.phase)
            continue
        claimed = _unlaunched(cfg, slot.phase) if slot.phase else None
        if claimed is not None and now - claimed < _START_GRACE_S:
            starting.append(slot.phase or "")
            continue
        cmd = _pane_cmd(slot.pane_id or "")
        if cmd == "?":
            unknown += 1
        elif cmd != "claude":
            what = f"slot {slot.id} ({slot.phase}) pane {slot.pane_id} runs {cmd!r}"
            if claimed is not None:
                what += f", claimed {_human_age(now - claimed)} ago and never launched"
            dead.append((slot.phase or "", what))
    for slot in st.slots:
        if slot.busy or slot.retiring:
            continue
        if not slot.pane_id:
            dead_free.append(f"free slot {slot.id} has no pane")
            continue
        cmd = _pane_cmd(slot.pane_id)
        if cmd == "?":
            unknown += 1
        elif cmd == "gone":
            dead_free.append(f"free slot {slot.id} pane {slot.pane_id} is gone")
    return PaneProbe(dead, unknown, len(busy), dead_free, landing, starting)


def _check_panes(cfg: Config, st: State, probe: PaneProbe) -> Check:
    """Every busy slot's pane still exists and still runs ``claude``, and every
    free slot still has a pane to launch into.

    A worker that crashed or was killed never runs ``swarm done``, so its slot
    stays busy forever and the run can neither progress nor finish. A worker
    that did report keeps its slot while its work lands, and is counted apart;
    so is a slot claimed moments ago, whose worker is still being started.
    """
    if cfg.driver != "tmux":
        return Check("slots.panes", OK, f"driver={cfg.driver}; no panes to check")
    if probe.dead:
        phase = probe.dead[0][0] or "<phase>"
        return Check(
            "slots.panes",
            FAIL,
            "worker died without `swarm done`: "
            + "; ".join([what for _, what in probe.dead] + probe.dead_free),
            f"swarm free {phase}  # then relaunch it",
        )
    if probe.dead_free:
        return Check(
            "slots.panes",
            FAIL,
            "slot(s) with nowhere to launch a worker: " + "; ".join(probe.dead_free),
            "swarm down && swarm up  # rebuilds every slot's pane",
        )
    if not probe.busy:
        return Check("slots.panes", OK, "no busy slots")
    working = probe.busy - len(probe.landing) - len(probe.starting)
    apart = [
        f"{len(phases)} {what} ({', '.join(phases)})"
        for what, phases in (("landing", probe.landing), ("starting", probe.starting))
        if phases
    ]
    if probe.unknown:
        detail = f"{probe.busy} busy; {probe.unknown} pane(s) unreadable"
        return Check("slots.panes", OK, "; ".join([detail, *apart]))
    if apart:
        return Check(
            "slots.panes",
            OK,
            f"{probe.busy} busy slot(s): {working} running claude, {', '.join(apart)}",
        )
    return Check("slots.panes", OK, f"{probe.busy} busy slot(s), all running claude")


def _check_watchdog(cfg: Config, probe: PaneProbe) -> Check:
    """Whether anything will ever *reclaim* a slot whose worker died.

    ``[swarm].watchdog_s = 0`` restores the purely event-driven supervisor, which
    is a legitimate choice — but it means a dead pane is never noticed: the loop
    blocks in ``select`` forever whenever nothing is waiting, and the worker that
    would have sent the waking ``swarm done`` is the one that died. With the
    watchdog on, the same dead pane is reaped within one sweep. So this is not a
    fault on its own; it is a fault *in combination*, which is exactly the pair
    nothing else in the tool looks at.
    """
    watchdog = int(getattr(cfg, "watchdog_s", 0) or 0)
    dead = probe.dead  # busy slots only: the watchdog reaps workers, not free panes
    if watchdog and dead:
        return Check(
            "run.watchdog",
            OK,
            f"watchdog on ({watchdog}s) — the {len(dead)} dead pane(s) above will be reaped",
        )
    if watchdog:
        return Check("run.watchdog", OK, f"liveness watchdog on, sweeping every {watchdog}s")
    if dead:
        return Check(
            "run.watchdog",
            FAIL,
            f"watchdog disabled AND {len(dead)} busy slot(s) have a dead pane — nothing "
            "will ever reclaim them; the supervisor blocks in select() until an event "
            "that can no longer arrive",
            'set `watchdog_s = 300` under [swarm], or free the slot(s) by hand now',
        )
    return Check(
        "run.watchdog",
        OK,
        "watchdog disabled (purely event-driven) and no dead panes",
    )


def _check_activity(cfg: Config, st: State) -> Check:
    """A busy slot whose worktree holds no work — the lost-``/prime`` signature.

    ``launch.send_submit`` used to short-circuit to True when the prompt box had
    not painted yet, so a slow-starting session swallowed the command and the
    supervisor logged a clean ``LAUNCH`` anyway: correct env, correct worktree,
    correct pane title, claude running, empty prompt, and time gone. Nothing
    else in the tool can see this.

    A row whose ``dir:`` is an external repo writes nowhere in its mirror, so
    its lane worktree and branch count too (``_lane_activity``).

    A phase whose worker has reported (:func:`_reported`) is left out: once its
    branch is merged it has nothing ahead of main and nothing dirty, and it
    keeps its slot for a moment longer.

    A slot's time counts from its claim while no ``LAUNCH`` follows it
    (:func:`_unlaunched`): a phase that is starting again has a fresh, empty
    mirror, and the only ``LAUNCH`` on record is an earlier attempt's.
    """
    busy = [
        s
        for s in st.busy_slots()
        if s.phase and s.worktree and not _reported(cfg, st, s.phase)
    ]
    if not busy:
        return Check("slots.activity", OK, "no busy slots with worktrees")
    now = time.time()
    row_dirs = _row_dirs(cfg)
    idle: list[tuple[str, str]] = []
    for slot in busy:
        started = _unlaunched(cfg, slot.phase or "") or _log_ts(cfg, f"LAUNCH {slot.phase} ")
        if started is None:
            try:
                started = Path(slot.worktree or "").stat().st_mtime
            except OSError:
                continue
        age = now - started
        if age < _IDLE_GRACE_S:
            continue
        work = _worktree_activity(cfg, slot.phase or "", slot.worktree or "")
        if work == 0:
            work = _lane_activity(cfg, slot.phase or "", row_dirs, started)
        if work == 0:
            idle.append((slot.phase, f"{_human_age(age)}, no commits, no dirty files"))
    if idle:
        first = idle[0][0]
        return Check(
            "slots.activity",
            FAIL,
            "busy slot(s) with zero worktree activity — check the pane for an "
            "empty prompt box: " + "; ".join(f"{p} ({what})" for p, what in idle),
            f"tmux send-keys -t <pane> -l -- '/prime {first}' && "
            "tmux send-keys -t <pane> Enter",
        )
    return Check("slots.activity", OK, f"{len(busy)} busy slot(s) have written work")


def _check_integration(cfg: Config, st: State) -> Check:
    """A held integration, **with its age**.

    Today the swarm telegrams once at the instant of blocking and is then silent
    forever, so a hold discovered the next morning looks the same as one from a
    minute ago. The whole merge queue is frozen for the entire duration: no phase
    integrates, so no slot ever frees.
    """
    if not st.integ_blocked:
        return Check("integration.blocked", OK, f"queue={st.integ_queue or '[]'}, not blocked")
    since = _log_ts(cfg, f"INTEGRATE-BLOCKED {st.integ_blocked} ")
    age = time.time() - since if since else None
    repo = Path(st.integ_blocked_repo).name if st.integ_blocked_repo else "?"
    detail = (
        f"integration held on {st.integ_blocked} ({st.integ_blocked_kind} in {repo})"
        f" for {_human_age(age)} — queue {st.integ_queue} frozen, slots will not free"
    )
    status = FAIL if age is None or age >= _BLOCKED_WARN_S else WARN
    return Check(
        "integration.blocked", status, detail, f"swarm resolved {st.integ_blocked}"
    )


def _check_push_owed(st: State) -> Check:
    """Repos merged locally whose push has not reached origin.

    A WARN, never a FAIL: the queue keeps moving (workers branch from local main)
    and the supervisor retries after each integration. What it costs is origin
    — and so the other machines and GitHub's scheduled runs — falling behind,
    silently, until someone looks.
    """
    if not st.push_owed:
        return Check("integration.push", OK, "no push owed")
    return Check(
        "integration.push",
        WARN,
        "push owed — " + "; ".join(pushowed.describe(st.push_owed)),
        "fix the repo's pre-push check (or the remote), or `git -C <repo> push` by"
        " hand; the swarm retries after each integration and clears it itself",
    )


def _check_backup(cfg: Config, now: float | None = None) -> Check:
    """What the last backup pass could not copy to origin.

    A worker's uncommitted edits reach origin as a snapshot commit. A snapshot
    that fails leaves one log line, and the pass still reports what it pushed for
    everyone else, so the run reads as backed up. One bad pass is a WARN (a git
    call that timed out under load heals on the next); two running is a FAIL. A
    ref that would not push is a WARN: the next pass retries it.
    """
    if cfg.git_isolation != "worktree":
        return Check("backup", OK, "off (no worktree isolation, so nothing is mirrored)")
    rec = backup_mod.last(cfg)
    if rec is None:
        return Check("backup", OK, "no pass recorded yet")
    now = time.time() if now is None else now
    try:
        age = _human_age(now - float(rec["ts"]))
        summary = f"last pass {age} ago: {int(rec['pushed'])} pushed, {int(rec['deleted'])} deleted"
        snaps = dict(rec.get("snapshots") or {})
        failed = [str(f) for f in rec.get("failed") or []]
        n_snaps = int(rec.get("snapshots_n") or len(snaps))
        n_failed = int(rec.get("failed_n") or len(failed))
        passes = int(rec.get("unsaved_passes") or 1)
        since = rec.get("unsaved_since")
        since_age = _human_age(now - float(since)) if since else "unknown"
    except (KeyError, TypeError, ValueError):
        return Check("backup", WARN, f"{cfg.state_dir / backup_mod.RECORD} does not read",
                     "the next backup pass rewrites it")
    if snaps:
        names = ", ".join(sorted(snaps)[:5]) + (" …" if n_snaps > 5 else "")
        return Check(
            "backup",
            FAIL if passes >= 2 else WARN,
            f"uncommitted work was not backed up: {n_snaps} snapshot"
            f"{'s' if n_snaps != 1 else ''} failed ({names}), {passes} pass"
            f"{'es' if passes != 1 else ''} running, first {since_age} ago; "
            f"{next(iter(snaps.values()))}; {summary}",
            "the BACKUP-SNAPSHOT-FAILED lines in supervisor.log give git's reason per"
            " phase; until it is fixed, only what a worker commits on its branch is"
            " backed up",
        )
    if failed:
        return Check(
            "backup",
            WARN,
            f"{summary}, {n_failed} failed: " + "; ".join(failed[:5])
            + (" …" if n_failed > 5 else ""),
            "the BACKUP-FAILED and BACKUP-PUSH-FAILED lines in supervisor.log say why;"
            " the next pass retries",
        )
    return Check("backup", OK, summary)


def _check_finish_race(st: State, ready: list[str]) -> Check:
    """``finished`` while phases are still ready — the FINISH-WITH-READY race."""
    if st.finished and ready:
        return Check(
            "run.finished",
            FAIL,
            f"run marked finished but {len(ready)} phase(s) are still ready: {ready}",
            "swarm up  # restart; done phases are rehydrated from sentinels",
        )
    if st.finished:
        return Check("run.finished", OK, "run finished with nothing left ready")
    return Check("run.finished", OK, "run in progress")


def _check_nudge(st: State, ready: list[str], free: list[int]) -> Check:
    """Free slot + ready phases + not paused + not blocked = a lost nudge.

    Nothing in the design will ever wake the supervisor from this state: the wake
    path is entirely input-driven, so a poke that was dropped (or a master pass
    that ended without launching) leaves the swarm idle with work available and
    no timer to notice.
    """
    if st.finished or st.on_hold or st.integ_blocked or st.bootstrapping:
        why = (
            "finished" if st.finished
            else "paused" if st.paused
            else "held by a usage cap" if st.usage_hold
            else "draining to a stop" if st.drain
            else "integration held" if st.integ_blocked
            # `swarm up` holds the first launch until the init pass is over.
            else "starting: the first launch waits for the init pass"
        )
        return Check("run.nudge", OK, f"not applicable ({why})")
    if free and ready:
        return Check(
            "run.nudge",
            FAIL,
            f"{len(free)} free slot(s) and ready {ready} but nothing launched — "
            "lost nudge; nothing will wake the supervisor on its own",
            f"swarm launch {ready[0]}",
        )
    return Check("run.nudge", OK, f"free={free} ready={ready}")


def _check_stall(cfg: Config, st: State) -> Check:
    """No supervisor event at all for a long time, with work still in flight."""
    last = float(getattr(st, "last_event_at", 0.0) or 0.0)
    if not st.pending():
        return Check("run.stall", OK, "nothing in flight")
    if not last:
        return Check("run.stall", OK, "no event timestamp recorded yet")
    age = time.time() - last
    if age >= _STALL_WARN_S:
        return Check(
            "run.stall",
            WARN,
            f"no supervisor event for {_human_age(age)} while {len(st.busy_slots())} "
            "slot(s) are busy — a long phase, or a wedged one",
            "check the worker panes",
        )
    return Check("run.stall", OK, f"last supervisor event {_human_age(age)} ago")


def _waiting_question(cfg: Config, phase: str) -> str:
    """The worker's actual question, recovered from the notification ledger.

    ``swarm waiting`` telegrams the note and does not store it anywhere else, so
    without the ledger the owner is told they are blocking a phase but not what
    it wants. Best-effort: no ledger, no question.
    """
    for row in reversed(_notifications(cfg)):
        if row.get("kind") == "waiting" and row.get("phase") == phase:
            text = str(row.get("text", ""))
            _, sep, tail = text.partition(" — ")
            # The ping's last line says which window to open, not what is asked.
            lines = (tail if sep else text).strip().splitlines()
            return lines[0] if lines else ""
    return ""


#: Public name for the Overseer's digest, which lists the same questions.
waiting_question = _waiting_question


def _check_owner(cfg: Config, st: State) -> Check:
    """``waiting``/``parked`` phases — the cases where the OWNER is the blocker.

    Not a fault: a worker that self-reports rather than guessing is doing the
    right thing. But it holds the run open (a parked worker keeps it ``pending``
    forever), so it is surfaced with how long it has been waiting and what it
    asked.
    """
    if not st.waiting and not st.parked:
        return Check("owner.blocking", OK, "no worker is waiting on you")
    now = time.time()
    bits: list[str] = []
    oldest = 0.0
    for phase, deadline in sorted(st.waiting.items()):
        # `waiting` stores the PARK deadline, so the ask began park_after earlier.
        asked = float(deadline) - cfg.park_after
        age = now - asked
        oldest = max(oldest, age)
        question = _waiting_question(cfg, phase)
        bits.append(f"{phase} waiting {_human_age(age)}" + (f': "{question}"' if question else ""))
    for phase in sorted(st.parked):
        since = _log_ts(cfg, f"PARK {phase} ")
        age = now - since if since else None
        if age:
            oldest = max(oldest, age)
        question = _waiting_question(cfg, phase)
        bits.append(
            f"{phase} parked {_human_age(age)}" + (f': "{question}"' if question else "")
        )
    status = WARN if oldest >= _WAIT_WARN_S else OK
    target = sorted(st.waiting)[0] if st.waiting else sorted(st.parked)[0]
    return Check(
        "owner.blocking",
        status,
        "you are the blocker: " + "; ".join(bits),
        f"answer in its pane, then `swarm resumed {target} '<the answer>'` (or `swarm done {target} ...`)",
    )


def _check_ledger(cfg: Config, st: State | None = None) -> Check:
    """Structural ledger faults — a cycle or an unknown dep silently strands
    every phase behind it, and reads exactly like a clean finish."""
    try:
        graph = ledger_mod.load(cfg.project_dir / cfg.ledger)
    except OSError as exc:
        return Check("ledger", WARN, f"ledger unreadable: {exc}")
    if not graph:
        return Check("ledger", OK, f"no machine-readable phases in {cfg.ledger}")
    done = ledger_mod.with_ticked(
        st.done if st is not None else {}, ledger_mod.load_ticked(cfg.project_dir / cfg.ledger)
    )
    landed = {p for p, status in done.items() if status in ledger_mod.SATISFIES_DEPS}
    issues = ledger_mod.validate(graph, landed)
    if issues:
        return Check(
            "ledger",
            FAIL,
            f"{len(issues)} structural problem(s): " + "; ".join(issues[:4]),
            f"fix {cfg.ledger} — phases behind these will never become ready",
        )
    return Check("ledger", OK, f"{len(graph)} phase(s), no cycles or unknown deps")


def _check_telegram(cfg: Config) -> list[Check]:
    """Config validity AND whether a send has ever actually been logged.

    These are different questions. A perfectly configured ``notify.sh`` that
    fails on every call is invisible: the exit code was the only signal, no
    caller kept it, and the log recorded nothing. A run can have zero
    sends logged across hundreds of log lines while messages were delivered — from
    the tool's own records the two are indistinguishable.
    """
    ok, detail = telegram.check(cfg.telegram_notify)
    config = Check(
        "telegram.config",
        OK if ok else FAIL,
        detail,
        None if ok else "scripts/resolve-chat-id.sh, or fix the .env it names",
    )
    everything = _notifications(cfg)
    # A message the swarm chose not to send (`suppressed`) is neither a send nor
    # a drop: it only ever went to the ledger.
    rows = [r for r in everything if not r.get("suppressed")]
    held = len(everything) - len(rows)
    held_note = f" (+{held} held back, not sent)" if held else ""
    if not rows:
        sends = Check(
            "telegram.sends",
            WARN,
            "no send has ever been logged for this run — a silently failing "
            "notify.sh is indistinguishable from a healthy one",
            "swarm status; send one test ping and re-run doctor",
        )
    else:
        failed = [r for r in rows if not r.get("delivered")]
        acked = telegram.acked_at(cfg.state_dir)
        open_ = telegram.open_drops(rows, acked)
        last = rows[-1]
        age = _human_age(time.time() - float(last.get("ts") or 0)) if last.get("ts") else "unknown"
        if open_:
            recent = str(open_[-1].get("error") or "?")[:120]
            count = (f"{len(open_)} send(s) DROPPED since you acknowledged the earlier ones"
                     if acked else f"{len(open_)}/{len(rows)} sends were DROPPED")
            # Still failing (the newest send was dropped) or failed lately: FAIL.
            # A problem fixed since then only warns, and the ack clears it.
            fixed = bool(last.get("delivered")) and not _recent_drop(open_)
            sends = Check(
                "telegram.sends",
                WARN if fixed else FAIL,
                f"{count}; last error: {recent}"
                + (f"; sends have worked since, last {age} ago" if fixed else ""),
                "run scripts/notify.sh 'test' by hand and read its stderr; once you have"
                " seen them, `swarm notify --ack` (or x on the alerts tab) clears them",
            )
        elif failed:
            sends = Check(
                "telegram.sends",
                OK,
                f"{len(rows)} send(s) logged, none dropped since you acknowledged"
                f" {len(failed)} earlier drop(s), last {age} ago{held_note}",
            )
        else:
            sends = Check(
                "telegram.sends",
                OK,
                f"{len(rows)} send(s) logged, all delivered, last {age} ago{held_note}",
            )
    return [config, sends]


#: A dropped ping younger than this is a live problem, whatever came after it.
RECENT_DROP_S = 6 * 3600


def _recent_drop(drops: list[dict], now: float | None = None) -> bool:
    now = time.time() if now is None else now
    for row in drops:
        ts = row.get("ts")
        if isinstance(ts, (int, float)) and now - float(ts) < RECENT_DROP_S:
            return True
    return False


def _check_disk(cfg: Config) -> list[Check]:
    """State-dir size and growth rate, plus cargo incremental dead weight.

    The growth *rate* is why the sample file exists: a single size reading cannot
    distinguish a cache that has sat at one size for a week from one that will be
    twice that by morning. The sample is this module's only write.
    """
    size = _dir_size(cfg.state_dir)
    now = time.time()
    sample_path = cfg.state_dir / _SAMPLE_NAME
    rate: float | None = None
    try:
        prior = json.loads(sample_path.read_text(encoding="utf-8"))
        elapsed = now - float(prior["ts"])
        if size is not None and elapsed > 60:
            rate = (size - int(prior["bytes"])) / (elapsed / 3600.0)
    except (OSError, ValueError, KeyError, TypeError):
        rate = None
    if size is not None:
        try:
            sample_path.write_text(
                json.dumps({"ts": now, "bytes": size}), encoding="utf-8"
            )
        except OSError:
            pass

    growth = f", growing {_human_bytes(int(rate))}/h" if rate and rate > 0 else ""
    if size is None:
        disk = Check("disk.state", OK, f"{cfg.state_dir} size unknown (du timed out)")
    elif size >= _DISK_WARN_BYTES or (rate or 0) >= _GROWTH_WARN_BYTES_PER_H:
        auto_ok, auto_note = _auto_gc(cfg)
        disk = Check(
            "disk.state",
            WARN,
            f"state dir is {_human_bytes(size)}{growth}; {auto_note}",
            f"swarm gc  # dry run: what {cfg.build_cache_dir} and dead mirrors would give back"
            if auto_ok
            else f"swarm gc --yes  # sweeps {cfg.build_cache_dir}, drops dead mirrors and tmp",
        )
    else:
        disk = Check("disk.state", OK, f"state dir is {_human_bytes(size)}{growth}")

    return [disk, _check_incremental(cfg), _check_tmp()]


def _auto_gc(cfg: Config, now: float | None = None) -> tuple[bool, str]:
    """``(healthy, one-line status)`` of the supervisor's automatic gc.

    Healthy means a manual ``swarm gc --yes`` would only do early what is about
    to happen anyway: auto is on and its last run did not fail and is not
    overdue (twice ``[gc].every_s``). Only when it is not do the disk checks
    tell the owner to run it by hand.
    """
    if not cfg.gc_auto:
        return False, "automatic gc is off ([gc].auto = false)"
    now = time.time() if now is None else now
    rec = gc_mod.read_record(cfg)
    if rec is None:
        return True, "automatic gc has not run yet (it runs every [gc].every_s and when the swarm idles)"
    ts = float(rec.get("ts") or 0.0)
    when = _human_age(now - ts)
    if rec.get("outcome") == gc_mod.AUTO_FAILED:
        return False, f"automatic gc FAILED {when} ago: {rec.get('detail') or '?'}"
    if cfg.gc_every_s and now - ts > 2 * cfg.gc_every_s:
        return False, f"automatic gc has not completed for {when}"
    return True, f"automatic gc last ran {when} ago, freed {_human_bytes(int(rec.get('freed') or 0))}"


def _check_incremental(cfg: Config) -> Check:
    """``CARGO_INCREMENTAL`` and the dead incremental state already on disk.

    Every phase builds a *different* source tree against one shared ``target/``,
    so no phase's incremental cache is ever reusable by another — it is pure
    write amplification. It can reach tens of GiB on a long-running swarm.
    """
    env = os.environ.get("CARGO_INCREMENTAL")
    incr_dirs = []
    try:
        incr_dirs = [p for p in cfg.build_cache_dir.glob("*/*/incremental") if p.is_dir()]
    except OSError:
        pass
    sizes = [s for s in (_dir_size(p) for p in incr_dirs) if s]
    on_disk = sum(sizes)
    auto_ok, auto_note = _auto_gc(cfg)
    hint = (
        "swarm gc --yes  # removes every incremental/ (and export CARGO_INCREMENTAL=0)"
        if on_disk
        else "export CARGO_INCREMENTAL=0 before `swarm up`"
    )
    if env not in (None, "", "0"):
        return Check(
            "disk.incremental",
            WARN,
            f"CARGO_INCREMENTAL={env!r} is inherited by every worker; "
            f"{_human_bytes(on_disk)} of incremental state on disk that no phase can reuse",
            hint,
        )
    if on_disk and auto_ok:
        # Dead weight with a scheduled removal is not something to act on.
        return Check(
            "disk.incremental",
            OK,
            f"{_human_bytes(on_disk)} of stale incremental state; the next"
            f" automatic gc removes it ({auto_note})",
        )
    if on_disk:
        return Check(
            "disk.incremental",
            WARN,
            f"{_human_bytes(on_disk)} of stale incremental state in the build cache "
            f"(workers now set CARGO_INCREMENTAL=0, so nothing will reuse it); {auto_note}",
            hint,
        )
    return Check("disk.incremental", OK, "no cargo incremental dead weight")


def _tmpfs(path: Path) -> bool:
    """Whether ``path`` is its own tmpfs mount (i.e. RAM), per ``/proc/mounts``."""
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == str(path):
            return parts[2] == "tmpfs"
    return False


def _check_tmp(root: Path = _TMP_ROOT) -> Check:
    """A RAM-backed ``/tmp`` filling up, and the swarm-looking things filling it.

    Where ``/tmp`` is a small tmpfs, a worker's scratch cargo target
    there and Claude Code's own diff cache can fill it and push swap to the limit.
    Sessions the swarm starts now get an on-disk ``TMPDIR``; this names whatever
    still lands in RAM. Report-only — ``/tmp`` is shared with the owner's own
    sessions, so the hint is a path to look at, never a command that deletes.
    """
    if not _tmpfs(root):
        return Check("disk.tmp", OK, f"{root} is not a tmpfs")
    try:
        st = os.statvfs(root)
    except OSError:
        return Check("disk.tmp", OK, f"{root} usage unknown")
    total = st.f_blocks * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    pct = 100.0 * used / total if total else 0.0
    line = f"{root} (tmpfs, RAM) is {pct:.0f}% full ({_human_bytes(used)} of {_human_bytes(total)})"
    if pct <= _TMP_WARN_PCT:
        return Check("disk.tmp", OK, line)
    top = gc_mod.tmp_offenders(root)[:3]
    named = "; ".join(f"{p} {_human_bytes(n)} ({why})" for p, n, why in top)
    return Check(
        "disk.tmp",
        WARN,
        f"{line} — largest swarm-looking: {named}" if named else line,
        f"du -sh {root}/* | sort -h | tail  # then remove what is yours",
    )


def _check_sentinels(cfg: Config, st: State) -> Check:
    """``done/<phase>.<status>`` versus the ``done`` map.

    A campaign's sentinels can all read ``needs-owner`` while
    state reads ``ok`` for every one of them. The sentinel is the durable record
    the worker itself wrote; the map is what the dashboard and every report show.
    When they disagree, phases the owner was asked to review are presented as
    clean successes.

    A phase that is landing has its sentinel and no entry in the map yet: the
    map is written when it lands, so it is counted apart and is not missing.
    """
    sentinels = _sentinels(cfg)
    if not sentinels:
        return Check("sentinels", OK, "no sentinels written yet")
    mismatched = [
        f"{p}: sentinel={s} state={st.done[p]}"
        for p, s in sorted(sentinels.items())
        if p in st.done and st.done[p] != s
    ]
    landing = sorted(p for p in _landing(st) if p in sentinels and p not in st.done)
    missing = sorted(p for p in sentinels if p not in st.done and p not in landing)
    if mismatched:
        return Check(
            "sentinels",
            FAIL,
            f"{len(mismatched)} phase(s) disagree with the done map: "
            + "; ".join(mismatched[:5]),
            "the sentinel is the worker's own record — trust it and review those phases",
        )
    if missing:
        return Check(
            "sentinels",
            WARN,
            f"{len(missing)} sentinel(s) not reflected in state: {missing[:5]}",
            "swarm up  # rehydrates the done map from sentinels",
        )
    if landing:
        return Check(
            "sentinels",
            OK,
            f"{len(sentinels) - len(landing)} sentinel(s) agree with state; "
            f"{len(landing)} landing: {landing[:5]}",
        )
    return Check("sentinels", OK, f"{len(sentinels)} sentinel(s) agree with state")


def _check_recaps(cfg: Config) -> Check:
    """Whether any phase's recap was overwritten by a later, thinner one.

    ``swarm done`` appends every attempt to ``done/<phase>.jsonl`` with a verdict:
    ``refused`` means a downgrade was blocked and the good recap survived,
    ``forced`` means ``--force`` deliberately replaced a fuller one. Only the
    latter loses text from the sentinel — and it is recoverable, because the
    history kept it. Reporting ``refused`` as a problem would be noise: that is
    the guard working.
    """
    forced: list[str] = []
    refused = 0
    try:
        histories = sorted(cfg.done_dir.glob("*.jsonl"))
    except OSError:
        return Check("recaps.history", OK, "no done history yet")
    for path in histories:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get("verdict") == "forced":
                forced.append(path.stem)
            elif row.get("verdict") == "refused":
                refused += 1
    if forced:
        names = sorted(set(forced))
        return Check(
            "recaps.history",
            WARN,
            f"a fuller recap was replaced with --force for {names} — the sentinel no "
            "longer holds the original text",
            f"the earlier recap is still in {cfg.done_dir}/<phase>.jsonl",
        )
    if refused:
        return Check(
            "recaps.history",
            OK,
            f"{refused} thinner re-report(s) refused; every recap intact",
        )
    return Check("recaps.history", OK, f"{len(histories)} phase(s) with done history")


def _check_failed(cfg: Config, st: State) -> Check:
    """Phases recorded ``fail``. Their work was rolled back and nothing
    downstream of them can ever become ready again on its own. A ``later``
    finish that waits for its date is not one (:func:`ledgerw.not_failed`).

    Nor, once the supervisor has swept, is a failed phase whose row was closed
    since (:func:`ledgerw.release_closed`): until then it is listed as on its
    way out. A ticked row that is not is one the swarm does not read as
    closed: it still says failed, or its tick is not committed on the target
    branch."""
    done = ledgerw.not_failed(st.done, ledgerw.dated(cfg))
    failed = sorted(p for p, s in done.items() if s == "fail")
    if not failed:
        return Check("phases.failed", OK, "no failed phases")
    closing = ledgerw.closable(cfg, st)
    ticked = sorted(set(failed) & ledger_mod.load_ticked(cfg.project_dir / cfg.ledger) - closing)
    if closing and not ticked:
        return Check(
            "phases.failed",
            WARN,
            f"{len(failed)} phase(s) recorded fail: {failed} — dependents stay blocked;"
            f" closed in the ledger since, the record goes at the supervisor's next"
            f" sweep: {sorted(closing)}",
            "nothing to do while a supervisor runs the installed code (it sweeps on every"
            " watchdog tick); if this stays: swarm restart",
        )
    if ticked:
        return Check(
            "phases.failed",
            WARN,
            f"{len(failed)} phase(s) recorded fail: {failed} — dependents stay blocked;"
            f" ticked in the ledger all the same: {ticked}",
            f"swarm record {ticked[0]} done \"<why>\"  # if it really is closed; or, to build"
            f" it again: swarm retry {ticked[0]}",
        )
    return Check(
        "phases.failed",
        WARN,
        f"{len(failed)} phase(s) recorded fail: {failed} — dependents stay blocked",
        f"swarm retry {failed[0]}  # after fixing whatever failed",
    )


def _check_open(cfg: Config, st: State) -> Check:
    """Phases the swarm landed whose row the ledger still shows open.

    The reverse of a closed failure (:func:`ledgerw.release_closed`), and here
    the record wins: the work is merged, so the phase counts as done, releases
    its dependents and is never built again on its own, whatever its box says.
    It is named so that someone settles it. A row whose tick is still on its
    way (a report about it queued, or the phase landing) is not one, and
    neither is a ``skip``, which never ticks a row. A ledger with no checklist
    rows has no boxes to compare.
    """
    path = cfg.project_dir / cfg.ledger
    if not ledgerw.is_checklist(cfg):
        return Check("phases.open", OK, "the ledger has no checkboxes to compare")
    graph = ledger_mod.load(path)
    ticked = ledger_mod.load_ticked(path)
    on_its_way = {*ledgerw.reported(cfg), *_landing(st), *st.claimed_phases()}
    still_open = sorted(
        p for p, s in st.done.items()
        if s in statuses.INTEGRATES and p in graph and p not in ticked and p not in on_its_way
    )
    if not still_open:
        return Check("phases.open", OK, "every landed phase is ticked in the ledger")
    first = still_open[0]
    return Check(
        "phases.open",
        WARN,
        f"{len(still_open)} phase(s) landed here but still open in the ledger: {still_open}"
        " — counted done, and never built again on their own",
        f"swarm record {first} done \"<why>\"  # the ledger agrees; or, to build it again:"
        f" swarm retry {first}",
    )


def _check_operator(cfg: Config) -> Check:
    """Operator jobs that need the owner: waiting on a decision, or given up on.

    A waiting job holds the single operator window until it is parked in one of
    its own, so later jobs queue behind it meanwhile; an abandoned one will
    never run again unless someone acts on it. Neither is a fault of the swarm —
    both are the owner's to clear, which is why they are warnings with the way
    out spelled out.
    """
    items = opqueue.load_all(cfg)
    now = time.time()
    slow = [
        i for i in items
        if i.state == opqueue.WAITING
        and now - (i.asked_at or i.queued_at) >= _OPERATOR_WAIT_WARN_S
    ]
    dropped = [i for i in items if i.state == opqueue.ABANDONED]
    if not slow and not dropped:
        live = sum(1 for i in items if not i.terminal)
        return Check("operator", OK, f"{live} operator job(s) open, none stuck on you")
    bits = [
        f'{i.phase} waiting on you {_human_age(now - (i.asked_at or i.queued_at))}:'
        f' "{i.question}"'
        for i in slow
    ]
    bits += [
        f"{i.phase} abandoned" + (" (asked you, older swarm)" if i.asked else "")
        for i in dropped
    ]
    if slow:
        key = state_mod.waiter_key(state_mod.OPERATOR, slow[0].phase)
        window = (state_mod.wait_window(key) if key in state_mod.read(cfg).parked
                  else "operator")
        fix = (
            f"answer in tmux window {window}; the session then runs"
            f" `swarm resumed {slow[0].phase}`"
        )
    else:
        fix = (
            f"carry it out by hand, then delete {opqueue.item_path(cfg, dropped[0].phase)};"
            f' or re-queue it: swarm operator-add --phase {dropped[0].phase} "<brief>"'
        )
    return Check("operator", WARN, "; ".join(bits), fix)


def _check_prompts() -> Check:
    """The master/resolver/operator prompt files must be resolvable.

    They are looked up in the packaged ``<package>/prompts/`` first and then by
    walking three directories up from ``__file__`` — the latter holds only for an
    editable install and breaks for a wheel in ``site-packages``, where the master
    silently never receives a prompt, ``spawn`` returns False, and the run stalls
    on a single log line with no telegram.
    """
    packaged = Path(__file__).resolve().parent / "prompts"
    source = Path(__file__).resolve().parent.parent.parent / "prompts"
    wanted = ("init_master.md", "resolver.md", "operator.md", "overseer.md")
    missing = [
        name
        for name in wanted
        if not (packaged / name).is_file() and not (source / name).is_file()
    ]
    if missing:
        return Check(
            "prompts",
            FAIL,
            f"prompt file(s) resolvable at neither {packaged} nor {source}: {missing}",
            "uv pip install -e .  # or ship prompts/ inside the package",
        )
    found = packaged if (packaged / wanted[0]).is_file() else source
    return Check("prompts", OK, f"{len(wanted)} prompt file(s) present in {found}")


# -- entry points ---------------------------------------------------------
def _check_web(cfg: Config, st: State) -> Check:
    """Is the web board answering, and at what address?

    The address is the point: it is what the owner types into the phone. Only
    a live run that should have a board and does not is worth a warning — a
    stopped run has no board by design.
    """
    if not cfg.web_enabled:
        return Check("web.board", OK, "off ([web] enabled = false)")
    where = " ".join(web_lifecycle.urls(cfg))
    state, detail = web_lifecycle.probe(cfg)
    if state == web_lifecycle.OURS:
        return Check("web.board", OK, f"listening: {where}")
    if state == web_lifecycle.TAKEN:
        who = f" ({detail})" if detail else ""
        return Check(
            "web.board",
            WARN,
            f"port :{cfg.web_port} is held by another program{who}, not the board",
            "set [web].port in .swarm.toml to a free port and restart",
        )
    if st.supervisor_pid and _pid_alive(st.supervisor_pid):
        return Check(
            "web.board",
            WARN,
            f"the run is up but nothing answers on :{cfg.web_port}",
            "the dashboard serves it (its status bar says why not), or run `swarm web`",
        )
    return Check("web.board", OK, f"not running — `swarm up` starts it on :{cfg.web_port}")


def _check_usage(cfg: Config, st: State) -> Check:
    """The usage caps in plain English: a hold is a WARN, since it stops new work."""
    lines = caps.summary_for(cfg, st.usage_hold)
    if st.usage_hold:
        return Check("usage.caps", WARN, " ".join(lines),
                     "swarm resume --override-cap   # to run anyway until the reset")
    return Check("usage.caps", OK, " ".join(lines))


def _check_tgbot(cfg: Config, st: State) -> Check:
    """Is the bot's command listener (``/usage``) running, and is it being answered?

    A 409 means some other program polls the same bot token; the listener backs
    off and says so in its status file, which is where this reads it from.
    """
    name = "telegram.bot"
    if not cfg.telegram_commands:
        return Check(name, OK, "off ([telegram] commands = false)")
    pid = tgbot.running(cfg)
    if pid is not None:
        info = tgbot.read_status(cfg, pid)
        state, detail = info.get("state") or "starting", info.get("detail") or ""
        if state in (tgbot.CONFLICT, tgbot.REJECTED, tgbot.WAITING_LOCK, tgbot.NETWORK):
            return Check(name, WARN, f"running (pid {pid}) but {state}: {detail}",
                         f"see {cfg.log_dir / tgbot.LOG}")
        return Check(name, OK, f"running (pid {pid}): /usage and /help answered")
    if st.supervisor_pid and _pid_alive(st.supervisor_pid):
        why = ("" if tgbot.credentials(cfg) is not None
               else f" (no bot token/chat id in {tgbot.env_file(cfg)})")
        return Check(name, WARN, f"the run is up but the command listener is not running{why}",
                     f"swarm telegram-bot   # or check {cfg.log_dir / tgbot.LOG}")
    return Check(name, OK, "not running — `swarm up` starts it")


def run_checks(cfg: Config) -> list[Check]:
    """Every diagnosis, in reading order. Never raises.

    An unreadable ``state.json`` short-circuits to a single failure: with no
    state there is nothing else meaningful to say, and guessing would produce a
    page of confident nonsense.
    """
    st = _read_state(cfg)
    if st is None:
        return [
            Check(
                "state",
                FAIL,
                f"{cfg.state_path} unreadable after 3 tries",
                "swarm up  # or delete the state dir for a clean slate",
            )
        ]

    try:
        ctx = build_context(cfg, st)
    except (OSError, ValueError):
        ctx = {}
    ready = list(ctx.get("ready", []))
    free = list(ctx.get("free_slots", []))

    probe = _dead_panes(cfg, st)
    checks: list[Check] = []
    checks.extend(_check_supervisor(cfg, st))
    checks.append(_check_panes(cfg, st, probe))
    checks.append(_check_watchdog(cfg, probe))
    checks.append(_check_activity(cfg, st))
    checks.append(_check_integration(cfg, st))
    checks.append(_check_push_owed(st))
    checks.append(_check_backup(cfg))
    checks.append(_check_finish_race(st, ready))
    checks.append(_check_nudge(st, ready, free))
    checks.append(_check_stall(cfg, st))
    checks.append(_check_owner(cfg, st))
    checks.append(_check_ledger(cfg, st))
    checks.extend(_check_telegram(cfg))
    checks.extend(_check_disk(cfg))
    checks.append(_check_sentinels(cfg, st))
    checks.append(_check_recaps(cfg))
    checks.append(_check_failed(cfg, st))
    checks.append(_check_open(cfg, st))
    checks.append(_check_operator(cfg))
    checks.append(_check_prompts())
    checks.append(_check_web(cfg, st))
    checks.append(_check_tgbot(cfg, st))
    checks.append(_check_usage(cfg, st))
    checks.append(_check_kept(cfg))
    checks.append(_check_restart(cfg, st))
    checks.append(_check_build_gate(cfg))
    checks.extend(_check_resources(cfg, st))
    return checks


def _check_build_gate(cfg: Config) -> Check:
    """Who holds the build gate and how many wait (information only)."""
    from . import buildstatus

    try:
        line = buildstatus.summary_line(cfg)
    except OSError as exc:
        return Check("build.gate", OK, f"unreadable: {exc}")
    if line is None:
        off = cfg.build_max_concurrent < 1
        return Check("build.gate", OK, "off" if off else "no build has used it yet")
    return Check("build.gate", OK, line.removeprefix("build gate: "))


def _check_resources(cfg: Config, st: State) -> list[Check]:
    """The resource sampler: fresh samples, bounded files, idle build holders."""
    alive = bool(st.supervisor_pid) and _pid_alive(st.supervisor_pid)
    return [Check(name, status, detail, fix)
            for name, status, detail, fix in resources_view.doctor_checks(cfg, alive)]


def _check_restart(cfg: Config, st: State, now: float | None = None) -> Check:
    """A restart that is planned, under way or failed — and a supervisor that
    runs older code than is installed, which is what a restart is for.

    The CLI is an editable install, so every command runs the code on disk; the
    supervisor runs what was there when it started."""
    now = time.time() if now is None else now
    plan = restart_mod.load(cfg)
    stage = plan.get("stage")
    text = restart_mod.line(plan, *restart_mod.counts(st), now)
    pid = st.supervisor_pid if _pid_alive(st.supervisor_pid) else None
    if stage == restart_mod.FAILED and text:
        return Check("restart", WARN, text, "swarm restart")
    if stage == restart_mod.PLANNED:
        if plan.get("timer") == "supervisor":
            fires = restart_mod.capable(cfg, pid, "restart-at")
        else:
            fires = _pid_alive(plan.get("runner_pid"))
        if not fires:
            return Check(
                "restart", WARN,
                text + " — but what was to start it is gone, so it will not happen",
                "swarm restart --at <HH:MM>  # plan it again",
            )
        return Check("restart", OK, text)
    if restart_mod.active(plan):
        return Check("restart", OK, text)
    behind = restart_mod.stale_code(cfg, pid)
    if behind is not None and not st.finished:
        return Check(
            "restart", WARN,
            f"the supervisor (pid {pid}) started {_human_age(behind)} before the installed"
            " code last changed: it and the dashboard run the older code",
            "swarm restart  # replaces only the supervisor; workers and questions are untouched",
        )
    return Check("restart", OK, "none planned; the supervisor runs the installed code"
                 if pid else "none planned")


def _check_kept(cfg: Config, now: float | None = None) -> Check:
    """What ``swarm keep`` left running: listed, and a WARN once one is old.

    Never a FAIL — keeping it was a decision — but nothing may linger forever
    unnoticed: past :data:`keep.STALE_S` the owner is asked to look."""
    recs = keep_mod.load_all(cfg)
    if not recs:
        return Check("keep", OK, "nothing kept running")
    now = time.time() if now is None else now
    alive = [r for r in recs if r.alive]
    stale = [r for r in alive if r.age_s(now) >= keep_mod.STALE_S]
    detail = "; ".join(keep_mod.line(r, now) for r in recs)
    if stale:
        return Check(
            "keep", WARN,
            f"{len(stale)} kept process(es) older than {keep_mod.STALE_S // 86400} days: {detail}",
            "; ".join(r.stop_cmd for r in stale) + "  # if no longer needed",
        )
    return Check("keep", OK, f"{len(alive)} alive, {len(recs) - len(alive)} dead: {detail}")


def worst(checks: list[Check]) -> str:
    """The most severe status present (``ok`` for an empty list)."""
    return max((c.status for c in checks), key=lambda s: _RANK.get(s, 0), default=OK)


def exit_code(checks: list[Check]) -> int:
    """``1`` if anything failed, else ``0`` — warnings alone are not an error."""
    return 1 if any(c.status == FAIL for c in checks) else 0


def render(checks: list[Check]) -> str:
    """Terminal output: one line per check, worst-first summary at the end.

    Problems keep their fix hint on an indented continuation line so a wide
    detail never pushes the actionable part off the screen.
    """
    marks = {OK: "ok  ", WARN: "WARN", FAIL: "FAIL"}
    width = max((len(c.name) for c in checks), default=0)
    lines: list[str] = []
    for c in checks:
        lines.append(f"{marks.get(c.status, '????')} {c.name:<{width}}  {c.detail}")
        if c.fix_hint and c.status != OK:
            lines.append(f"{'':<{width + 6}}  -> {c.fix_hint}")
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    if fails or warns:
        lines.append(f"\n{fails} failing, {warns} warning, {len(checks)} checks total")
    else:
        lines.append(f"\nall {len(checks)} checks pass")
    return "\n".join(lines)
