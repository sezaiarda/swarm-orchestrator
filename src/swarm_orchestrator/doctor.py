"""``swarm doctor`` — what is wrong with this swarm *right now*.

The supervisor is deliberately watchdog-free: it reacts to FIFO events and logs
what it does, and that is all. Every failure mode below shares one property —
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
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import gitq
from . import ledger as ledger_mod
from . import pushowed
from . import state as state_mod
from . import statuses
from . import telegram
from .config import Config
from .logutil import parse_ts
from .master import build_context
from .state import State

OK = "ok"
WARN = "warn"
FAIL = "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}

# A busy slot whose worktree holds zero bytes of work this long after LAUNCH is
# the signature of a `/prime` that never arrived. Left alone it sits idle for
# hours; a watcher polling every half hour needs two ticks to notice.
# 20 minutes is well past any plausible cold start and still catches it early.
_IDLE_GRACE_S = 20 * 60
# A held integration freezes the entire queue — every slot drains and none refill.
# It is worth an alert almost immediately.
_BLOCKED_WARN_S = 5 * 60
# The owner being the blocker is normal for a while, then it is the run stalling.
_WAIT_WARN_S = 15 * 60
# No supervisor event at all for this long, with work still in flight.
_STALL_WARN_S = 90 * 60

_DISK_WARN_BYTES = 40 * 1024**3
_GROWTH_WARN_BYTES_PER_H = 5 * 1024**3
_DU_TIMEOUT_S = 20.0
_GIT_TIMEOUT_S = 30.0
_TMUX_TIMEOUT_S = 10.0
_SAMPLE_NAME = ".doctor-disk.json"

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


def _log_ts(cfg: Config, prefix: str) -> float | None:
    """Wall-clock time of the last supervisor-log line starting with ``prefix``.

    The log is the only place a per-phase *timestamp* exists — state.json records
    what is happening, never when it started — so ages ("blocked for 4h") come
    from here. Reads the tail only; the log grows unbounded over a campaign.
    """
    try:
        with cfg.supervisor_log.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-4000:]
    except OSError:
        return None
    for line in reversed(lines):
        ts, message = parse_ts(line)
        if message.startswith(prefix):
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
                "swarm down && swarm up",
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


def _dead_panes(cfg: Config, st: State) -> tuple[list[str], int, int, list[str]]:
    """``(dead busy panes, unreadable count, busy count, dead free panes)`` —
    probed once.

    Shared by :func:`_check_panes` and :func:`_check_watchdog` so a swarm with
    four busy slots costs four ``tmux`` calls, not eight.

    Every slot is probed, not only the busy ones. A *free* slot with no pane (or
    a pane tmux no longer knows) is invisible until something launches into it —
    and then every launch that picks it fails ``no-pane``: that is what a
    ``swarm reload`` growing ``max_workers`` left behind before it created panes.
    A free pane only has to exist; it runs the ``sleep`` placeholder, not claude.
    """
    busy = [s for s in st.busy_slots() if s.pane_id]
    if cfg.driver != "tmux":
        return [], 0, len(busy), []
    dead: list[str] = []
    dead_free: list[str] = []
    unknown = 0
    for slot in busy:
        cmd = _pane_cmd(slot.pane_id or "")
        if cmd == "?":
            unknown += 1
        elif cmd != "claude":
            dead.append(f"slot {slot.id} ({slot.phase}) pane {slot.pane_id} runs {cmd!r}")
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
    return dead, unknown, len(busy), dead_free


def _check_panes(
    cfg: Config, st: State, probe: tuple[list[str], int, int, list[str]]
) -> Check:
    """Every busy slot's pane still exists and still runs ``claude``, and every
    free slot still has a pane to launch into.

    A worker that crashed or was killed never runs ``swarm done``, so its slot
    stays busy forever and the run can neither progress nor finish.
    """
    dead, unknown, busy, dead_free = probe
    if cfg.driver != "tmux":
        return Check("slots.panes", OK, f"driver={cfg.driver}; no panes to check")
    if dead:
        phase = next((s.phase for s in st.busy_slots() if s.phase), "<phase>")
        return Check(
            "slots.panes",
            FAIL,
            "worker died without `swarm done`: " + "; ".join(dead + dead_free),
            f"swarm free {phase}  # then relaunch it",
        )
    if dead_free:
        return Check(
            "slots.panes",
            FAIL,
            "slot(s) with nowhere to launch a worker: " + "; ".join(dead_free),
            "swarm down && swarm up  # rebuilds every slot's pane",
        )
    if not busy:
        return Check("slots.panes", OK, "no busy slots")
    if unknown:
        return Check("slots.panes", OK, f"{busy} busy; {unknown} pane(s) unreadable")
    return Check("slots.panes", OK, f"{busy} busy slot(s), all running claude")


def _check_watchdog(cfg: Config, probe: tuple[list[str], int, int, list[str]]) -> Check:
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
    dead = probe[0]  # busy slots only: the watchdog reaps workers, not free panes
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
    """
    busy = [s for s in st.busy_slots() if s.phase and s.worktree]
    if not busy:
        return Check("slots.activity", OK, "no busy slots with worktrees")
    now = time.time()
    idle: list[str] = []
    for slot in busy:
        started = _log_ts(cfg, f"LAUNCH {slot.phase} ")
        if started is None:
            try:
                started = Path(slot.worktree or "").stat().st_mtime
            except OSError:
                continue
        age = now - started
        if age < _IDLE_GRACE_S:
            continue
        if _worktree_activity(cfg, slot.phase or "", slot.worktree or "") == 0:
            idle.append(f"{slot.phase} ({_human_age(age)}, no commits, no dirty files)")
    if idle:
        first = busy[0].phase
        return Check(
            "slots.activity",
            FAIL,
            "busy slot(s) with zero worktree activity — check the pane for an "
            "empty prompt box: " + "; ".join(idle),
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
    if st.finished or st.paused or st.integ_blocked:
        why = "finished" if st.finished else ("paused" if st.paused else "integration held")
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
            return tail.strip() if sep else text.strip()
    return ""


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
        f"answer in its pane, then `swarm resumed {target}` (or `swarm done {target} ...`)",
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
    landed = {p for p, status in st.done.items() if status in ledger_mod.SATISFIES_DEPS} if st is not None else set()
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
    rows = _notifications(cfg)
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
        last = rows[-1]
        age = _human_age(time.time() - float(last.get("ts") or 0)) if last.get("ts") else "unknown"
        if failed:
            recent = str(failed[-1].get("error") or "?")[:120]
            sends = Check(
                "telegram.sends",
                FAIL,
                f"{len(failed)}/{len(rows)} sends were DROPPED; last error: {recent}",
                "run scripts/notify.sh 'test' by hand and read its stderr",
            )
        else:
            sends = Check(
                "telegram.sends", OK, f"{len(rows)} send(s) logged, all delivered, last {age} ago"
            )
    return [config, sends]


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
        disk = Check(
            "disk.state",
            WARN,
            f"state dir is {_human_bytes(size)}{growth}",
            f"rm -rf {cfg.build_cache_dir}  # shared cargo target cache, rebuilt on demand",
        )
    else:
        disk = Check("disk.state", OK, f"state dir is {_human_bytes(size)}{growth}")

    return [disk, _check_incremental(cfg)]


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
    hint = (
        f"rm -rf {cfg.build_cache_dir}/*/*/incremental  (and export CARGO_INCREMENTAL=0)"
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
    if on_disk:
        return Check(
            "disk.incremental",
            WARN,
            f"{_human_bytes(on_disk)} of stale incremental state in the build cache "
            "(workers now set CARGO_INCREMENTAL=0, so nothing will reuse it)",
            hint,
        )
    return Check("disk.incremental", OK, "no cargo incremental dead weight")


def _check_sentinels(cfg: Config, st: State) -> Check:
    """``done/<phase>.<status>`` versus the ``done`` map.

    A campaign's sentinels can all read ``needs-owner`` while
    state reads ``ok`` for every one of them. The sentinel is the durable record
    the worker itself wrote; the map is what the dashboard and every report show.
    When they disagree, phases the owner was asked to review are presented as
    clean successes.
    """
    sentinels = _sentinels(cfg)
    if not sentinels:
        return Check("sentinels", OK, "no sentinels written yet")
    mismatched = [
        f"{p}: sentinel={s} state={st.done[p]}"
        for p, s in sorted(sentinels.items())
        if p in st.done and st.done[p] != s
    ]
    missing = sorted(p for p in sentinels if p not in st.done)
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


def _check_failed(st: State) -> Check:
    """Phases recorded ``fail``. Their work was rolled back and nothing
    downstream of them can ever become ready again on its own."""
    failed = sorted(p for p, s in st.done.items() if s == "fail")
    if not failed:
        return Check("phases.failed", OK, "no failed phases")
    return Check(
        "phases.failed",
        WARN,
        f"{len(failed)} phase(s) recorded fail: {failed} — dependents stay blocked",
        f"swarm launch {failed[0]}  # after fixing whatever failed",
    )


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
    wanted = ("init_master.md", "step_master.md", "resolver.md", "operator.md")
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
    checks.append(_check_finish_race(st, ready))
    checks.append(_check_nudge(st, ready, free))
    checks.append(_check_stall(cfg, st))
    checks.append(_check_owner(cfg, st))
    checks.append(_check_ledger(cfg, st))
    checks.extend(_check_telegram(cfg))
    checks.extend(_check_disk(cfg))
    checks.append(_check_sentinels(cfg, st))
    checks.append(_check_recaps(cfg))
    checks.append(_check_failed(st))
    checks.append(_check_prompts())
    return checks


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
