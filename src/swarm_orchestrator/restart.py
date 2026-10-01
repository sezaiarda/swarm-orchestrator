"""``swarm restart``: load new code, or rebuild the run, without costing the owner work.

The request is one record, ``restart.json`` in the state dir: who asked, when
it happens, which kind, and how far it has got. It is kept out of
``state.json`` because a supervisor on older code rewrites that file without
the keys it does not know.

*In place* (the default). Only the supervisor process is replaced, and the
dashboard, the web board and the Telegram listener with it. The tmux session,
every worker, every session waiting on the owner, the operator and the owner
console are not touched; the new supervisor adopts the slots and panes
``state.json`` records (:meth:`supervisor.Supervisor._adopt`). The CLI is an
editable install, so every ``swarm`` command already runs the code on disk;
this is what makes the long-running supervisor and dashboard run it too.

The old supervisor is asked to hand over (the ``handover`` FIFO verb) and does
so at a safe point: no launch, session start, clean-up or backup under way, and
never mid-merge, because a merge runs inside one event and the request is read
between events. While it stops and its successor starts, :class:`Bridge` holds
the control FIFO open, so a ``swarm done`` or ``swarm waiting`` sent in the gap
stays in the pipe and is read by the new supervisor. A supervisor that predates
the verb is stopped with ``shutdown`` instead, once nothing its exit would end
is running (:func:`legacy_blockers`).

*Full* (``--full``). Drain, ``swarm down``, ``swarm up``: a new tmux session
and a new run. A session waiting on the owner would die with the old session,
so the default refuses while one is; ``--wait-questions`` drains until they are
answered, and ``--keep-questions`` carries each one across alive
(:func:`carry_out` / :func:`carry_in`): parked in a holding tmux session while
the swarm goes down and comes back, then moved into the new session with its
place in the run restored. The process is never ended, so what it asked is
still on screen.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import opqueue
from . import ovrecord
from . import pauseat
from . import procs
from . import runs as runs_mod
from . import state as state_mod
from . import telegram, tmux
from .config import Config
from .logutil import Log

PLAN_FILE = "restart.json"
#: Written by a supervisor that knows the ``handover`` verb, once its FIFO is open.
MARK_FILE = "supervisor.json"
#: What the old supervisor remembered only in memory, for its successor.
HANDOVER_FILE = "handover.json"
#: The sessions a full restart is carrying across, while they are in transit.
KEPT_FILE = "kept-sessions.json"
RUN_LOG = "restart.log"
START_ERR = "supervisor-start.err"

# -- the two kinds ---------------------------------------------------------
SUPERVISOR = "supervisor"
FULL = "full"

# -- how far a plan has got -------------------------------------------------
PLANNED = "planned"
STOPPING = "stopping"  # in place: the old supervisor is reaching a safe point
DRAINING = "draining"  # full: running work finishes first
RESTARTING = "restarting"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
ACTIVE = frozenset({PLANNED, STOPPING, DRAINING, RESTARTING})

# -- what a full restart does about sessions waiting on the owner ------------
REFUSE = "refuse"
WAIT = "wait"
KEEP = "keep"
FORCE = "force"

#: What a supervisor that can hand over says of itself in :data:`MARK_FILE`.
#: ``keep-later``: it keeps the work of a phase that finishes ``later`` for its
#: date, so ``swarm done`` may tell the worker so.
CAPS = ("handover", "restart-at", "keep-later")

#: How long a restart waits for a safe moment, then for the old supervisor to
#: be gone. Past it the restart fails and says what it was still waiting for;
#: nothing is ever stopped by force.
SAFE_TIMEOUT_S = 900.0
STOP_TIMEOUT_S = 900.0
#: How long a new supervisor gets to open the FIFO and say so.
START_TIMEOUT_S = 30.0
#: How long a failed restart stays in ``swarm status`` and doctor.
FAILED_SHOWN_S = 24 * 3600.0
#: A :data:`HANDOVER_FILE` older than this belongs to some earlier restart.
HANDOVER_FRESH_S = 3600.0


# -- the plan record ---------------------------------------------------------
def plan_path(cfg: Config) -> Path:
    return Path(cfg.state_dir) / PLAN_FILE


@contextmanager
def _locked(cfg: Config) -> Iterator[None]:
    """One writer of the plan at a time: the CLI, the runner and the supervisor
    all change it, and each change is a read-modify-write."""
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    with (Path(cfg.state_dir) / "restart.lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load(cfg: Config) -> dict:
    """The plan, or ``{}`` when there is none."""
    return _read_json(plan_path(cfg))


def save(cfg: Config, plan: dict) -> dict:
    with _locked(cfg):
        _write_json(plan_path(cfg), plan)
    return plan


def update(cfg: Config, plan_id: str, **changes) -> dict | None:
    """Change the plan ``plan_id``; ``None`` when it is no longer the plan (a
    newer request replaced it), so a late writer never overwrites a newer one."""
    with _locked(cfg):
        plan = load(cfg)
        if not plan or plan.get("id") != plan_id:
            return None
        plan.update(changes)
        _write_json(plan_path(cfg), plan)
        return plan


def active(plan: dict) -> bool:
    return bool(plan) and plan.get("stage") in ACTIVE


def new_plan(cfg: Config, mode: str, at: float, by: str, *, questions: str = REFUSE,
             now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "id": f"{time.strftime('%Y%m%dT%H%M%S', time.localtime(now))}-{uuid.uuid4().hex[:6]}",
        "mode": mode,
        "at": float(at),
        "requested_at": now,
        "by": by,
        "questions": questions,
        "stage": PLANNED,
        "waiting": [],
        "detail": "",
    }


def requester(cfg: Config, env=None) -> str:
    """Who is asking, in words: the session the command runs in (its
    ``SWARM_SESSION_ID`` or phase marker), else ``owner terminal``."""
    env = os.environ if env is None else env
    kind, _, ident = (env.get(procs.SESSION_ENV) or "").partition(":")
    if kind == "worker" and ident:
        return f"the worker on {ident}"
    if kind == "operator" and ident:
        return f"operator job {ident}"
    if kind == "overseer":
        return f"the Overseer (pass {ident})" if ident else "the Overseer"
    if kind:
        return f"the {kind} session {ident}".rstrip()
    phase = env.get(cfg.env_marker) or ""
    if phase:
        return f"the worker on {phase}"
    if env.get("SWARM_OWNER_CONSOLE"):
        return "the owner console"
    return "owner terminal"


# -- who waits on the owner ---------------------------------------------------
@dataclass(frozen=True)
class Question:
    """One session waiting on the owner."""

    key: str
    who: str
    where: str  # its tmux window; "" on the bare driver
    text: str
    parked: bool


def questions(cfg: Config, st: state_mod.State) -> list[Question]:
    """Every session that waits on the owner right now: a worker that asked, a
    parked one, an operator job that asked, an Overseer pass that asked."""
    from . import doctor as doctor_mod  # lazy: doctor reads the plan too
    from . import owner as owner_mod

    keys = list(dict.fromkeys([*st.waiting, *st.parked]))
    for item in opqueue.load_all(cfg):
        key = state_mod.waiter_key(state_mod.OPERATOR, item.phase)
        if item.state == opqueue.WAITING and key not in keys:
            keys.append(key)  # parking is off, or the poke has not landed yet
    out: list[Question] = []
    for key in keys:
        kind, ident = state_mod.waiter(key)
        if kind == state_mod.OPERATOR:
            item = opqueue.load(cfg, ident)
            who, text = f"operator job {ident}", (item.question if item else "")
        elif kind == state_mod.OVERSEER:
            rec = ovrecord.load_json(cfg, ident)
            who, text = "the Overseer", (rec.question if rec else "")
        else:
            who, text = f"the worker on {ident}", doctor_mod.waiting_question(cfg, ident)
        try:
            where = owner_mod.where(cfg, key, st)
        except (OSError, KeyError, subprocess.SubprocessError):
            where = ""
        out.append(Question(key, who, where, " ".join((text or "").split()), key in st.parked))
    return out


def question_lines(qs: list[Question]) -> list[str]:
    """One line per waiting session: who, where, what it asked."""
    out = []
    for q in qs:
        line = f"  - {q.who}"
        if q.where:
            line += f" (tmux window {q.where})"
        if q.text:
            line += f": {q.text[:200]}"
        out.append(line)
    return out


def counts(st: state_mod.State) -> tuple[int, int]:
    """``(workers at work, sessions waiting on the owner)`` from the state alone."""
    workers = sum(1 for s in st.busy_slots() if s.phase not in st.waiting)
    return workers, len(set(st.waiting) | set(st.parked))


def counts_of(state: dict) -> tuple[int, int]:
    """:func:`counts` for a raw ``state.json`` dict (the dashboard's view)."""
    waiting = state.get("waiting") or {}
    parked = state.get("parked") or []
    workers = sum(1 for s in state.get("slots") or []
                  if isinstance(s, dict) and s.get("busy") and s.get("phase") not in waiting)
    return workers, len(set(waiting) | set(parked))


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def line(plan: dict, workers: int, asked: int, now: float | None = None, *,
         relative: bool = True) -> str:
    """The restart in plain English, for ``swarm status``, doctor, the dashboard
    and the board. ``""`` when there is nothing to say. ``relative=False`` leaves
    out "in 5h 2m", for a reader that must not change just because time passed."""
    if not plan:
        return ""
    now = time.time() if now is None else now
    stage = plan.get("stage")
    by = plan.get("by") or "owner terminal"
    full = plan.get("mode") == FULL
    if stage == PLANNED:
        due = float(plan.get("at") or now)
        when = pauseat.when(due, now) if relative else pauseat.stamp(due).replace("T", " ")
        if not full:
            what = (f"restart planned at {when}: supervisor only, {_n(workers, 'worker')} and"
                    f" {_n(asked, 'question')} carry on untouched")
        else:
            policy = plan.get("questions") or REFUSE
            what = f"full restart planned at {when}: waits for {_n(workers, 'worker')}"
            if policy == WAIT:
                what += f" and {_n(asked, 'question')}"
            elif policy == KEEP:
                what += f"; {_n(asked, 'question')} carried across"
            elif policy == FORCE:
                what += f"; {_n(asked, 'question')} would be dropped"
            elif asked:
                what += f"; refuses while {_n(asked, 'question')} wait"
        return f"{what} — asked by {by}; swarm restart --cancel drops it"
    if stage == STOPPING:
        waits = list(plan.get("waiting") or [])
        what = (f"waiting for {_join(waits)}" if waits else "the supervisor is handing over")
        return f"restart under way (asked by {by}): {what}; workers and questions are untouched"
    if stage == DRAINING:
        return f"full restart under way (asked by {by}): draining, then down and up"
    if stage == RESTARTING:
        return f"{'full ' if full else ''}restart under way (asked by {by})"
    if stage == FAILED:
        ended = float(plan.get("ended_at") or now)
        if now - ended > FAILED_SHOWN_S:
            return ""
        at = pauseat.stamp(ended)
        return (f"the last restart FAILED ({at}, asked by {by}): {plan.get('detail') or '?'}"
                " — `swarm restart` tries again")
    return ""


def status_line(cfg: Config, st: state_mod.State | None = None, now: float | None = None) -> str:
    plan = load(cfg)
    if not plan:
        return ""
    st = state_mod.read(cfg) if st is None else st
    return line(plan, *counts(st), now)


def state_line(state_dir: Path | str, state: dict, now: float | None = None) -> str:
    """:func:`status_line` for a reader that holds the raw state dict."""
    plan = _read_json(Path(state_dir) / PLAN_FILE)
    return line(plan, *counts_of(state), now) if plan else ""


# -- the supervisor's own mark ------------------------------------------------
def mark_supervisor(cfg: Config, adopted: bool) -> None:
    """Record that this process is the supervisor and what it can do. Written
    once the FIFO is open: a restart waits for it before it lets go of the pipe."""
    pid = os.getpid()
    _write_json(Path(cfg.state_dir) / MARK_FILE, {
        "pid": pid, "ticks": procs.start_ticks(pid), "started_at": time.time(),
        "caps": list(CAPS), "adopted": adopted,
    })


def supervisor_mark(cfg: Config) -> dict:
    return _read_json(Path(cfg.state_dir) / MARK_FILE)


def capable(cfg: Config, pid: int | None, cap: str = "handover") -> bool:
    """Is ``pid`` a supervisor that said it knows ``cap``? One that predates the
    mark, or a pid the kernel has since reused, is not."""
    mark = supervisor_mark(cfg)
    return bool(pid) and mark.get("pid") == pid and procs.same(pid, mark.get("ticks")) \
        and cap in (mark.get("caps") or [])


def is_supervisor(cfg: Config, pid: int) -> bool:
    """Whether ``pid`` is this project's supervisor and not a process that has
    since been given the same number."""
    args = procs.cmdline(pid)
    if "_supervise" not in args or not any("swarm" in a for a in args):
        return False
    return procs.cwd(pid) == cfg.project_dir.resolve()


def live_supervisor(cfg: Config, st: state_mod.State | None = None) -> int | None:
    """The pid of this project's running supervisor, or ``None``."""
    st = state_mod.read(cfg) if st is None else st
    pid = st.supervisor_pid
    if pid and procs.alive(pid) and is_supervisor(cfg, pid):
        return pid
    return None


def has_reader(cfg: Config) -> bool:
    """Is anything reading the control FIFO?"""
    if not cfg.fifo_path.exists():
        return False
    try:
        fd = os.open(cfg.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False
    os.close(fd)
    return True


def code_stamp() -> float:
    """When the installed code last changed: the newest ``.py`` of the package."""
    newest = 0.0
    for path in Path(__file__).resolve().parent.rglob("*.py"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def started_at(pid: int) -> float | None:
    """When ``pid`` started, as wall-clock seconds; ``None`` if it cannot be told."""
    ticks = procs.start_ticks(pid)
    try:
        with open("/proc/uptime", encoding="utf-8") as fh:
            boot = time.time() - float(fh.read().split()[0])
        return boot + ticks / os.sysconf("SC_CLK_TCK") if ticks is not None else None
    except (OSError, ValueError, IndexError):
        return None


def stale_code(cfg: Config, pid: int | None) -> float | None:
    """Seconds by which the code on disk is newer than the running supervisor,
    or ``None`` when it runs what is installed (or nothing can be told)."""
    began = started_at(pid) if pid else None
    if began is None:
        return None
    behind = code_stamp() - began
    return behind if behind > 0 else None


# -- the FIFO bridge ----------------------------------------------------------
class Bridge:
    """Holds the control FIFO open across the gap between two supervisors.

    A pipe keeps what was written to it for as long as anything has it open, so
    with this in place a poke sent while no supervisor is reading is not lost
    (``ENXIO``, as it would be) but waits in the pipe for the next one."""

    def __init__(self, cfg: Config) -> None:
        Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
        if not cfg.fifo_path.exists():
            os.mkfifo(cfg.fifo_path)
        self.fd = os.open(cfg.fifo_path, os.O_RDWR | os.O_NONBLOCK)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "Bridge":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def _poke(cfg: Config, verb: str) -> bool:
    from . import launch as launch_mod

    return launch_mod._poke_fifo(cfg, f"{verb}\n")


# -- the environment a supervisor runs in --------------------------------------
#: What a session's environment adds for that session alone. A supervisor that
#: inherited any of it would work in the session's temp dir, and be ended with
#: the session: its end reaps whatever carries its marker.
_SESSION_KEYS = (
    procs.SESSION_ENV, "SWARM_OWNER_CONSOLE", "SWARM_WORKTREE", "SWARM_MAIN",
    "SWARM_TOUCHES", "SWARM_BUILD_MAX", "SWARM_BUILD_JOBS", "CARGO_BUILD_JOBS",
    "SWARM_PROJECT", "SWARM_MASTER_KIND", "SWARM_OVERSEER_PASS", "SWARM_OVERSEER_DIGEST",
    "SWARM_OVERSEER_RECORD", "SWARM_OPERATOR_JOB", "TMUX_PANE",
)


def clean_env(cfg: Config, env: dict[str, str] | None = None) -> dict[str, str]:
    """``env`` without what marks it as one session's (see :data:`_SESSION_KEYS`),
    its temp dir included when that is a session's own."""
    env = dict(os.environ if env is None else env)
    for key in (*_SESSION_KEYS, cfg.env_marker):
        env.pop(key, None)
    tmp_root = str(cfg.tmp_dir)
    for key in ("TMPDIR", "TMP", "TEMP"):
        if (env.get(key) or "").startswith(tmp_root):
            env.pop(key)
    return env


def supervisor_env(pid: int) -> dict[str, str] | None:
    """The environment ``pid`` runs in: what ``swarm up`` gave the supervisor,
    with every ``SWARM_*`` override that shapes its config."""
    env: dict[str, str] = {}
    for entry in procs.environ(pid):
        key, sep, val = entry.partition(b"=")
        if sep and key:
            env[key.decode(errors="replace")] = val.decode(errors="replace")
    return env or None


# -- stopping a supervisor that predates the hand-over --------------------------
def launching_from_log(cfg: Config, tail_bytes: int = 4 * 1024 * 1024) -> set[str]:
    """Phases the running supervisor has picked to launch and not yet settled,
    read from its log: only its own memory holds them, and one that predates
    the hand-over cannot be asked."""
    try:
        with cfg.supervisor_log.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return set()
    start = text.rfind("SUPERVISOR-START")
    pending: set[str] = set()
    for raw in text[max(0, start):].splitlines():
        if " LAUNCH-READY " in raw:
            names = raw.split(" LAUNCH-READY ", 1)[1].split(" (", 1)[0]
            pending.update(names.split())
        elif " EVENT launched " in raw:
            pending.discard(raw.split(" EVENT launched ", 1)[1].split()[0])
    return pending


def legacy_blockers(cfg: Config, st: state_mod.State) -> list[str]:
    """What a ``shutdown`` of a supervisor that cannot hand over would end, in
    words (``[]`` = it is safe to stop it now).

    On its way out it ends whatever runs in the master pane and in the operator
    window, and its exit ends its own launch threads mid-way. Workers, parked
    sessions and the owner console it leaves alone. (It also ends a big-picture
    pass, which is not waited for: nothing of the owner's is in one, and the
    next supervisor runs it again.)"""
    out: list[str] = []
    if st.overseer_pass:
        out.append("an Overseer pass")
    elif st.master_alive or st.bootstrapping:
        out.append("the start-up pass")
    if st.operator_phase:
        out.append(f"operator job {st.operator_phase}")
    launching = launching_from_log(cfg)
    if launching:
        out.append(f"{_n(len(launching), 'worker')} starting")
    return out


# -- the dashboard and the helpers ----------------------------------------------
def restart_helpers(cfg: Config, st: state_mod.State, log: Log) -> list[str]:
    """Start the dashboard, the web board and the Telegram listener again, so
    they run the code on disk too. Each is stateless; returns what was restarted."""
    from . import session as session_mod
    from . import tgbot
    from .web import lifecycle as web_lifecycle

    done: list[str] = []
    hosted = cfg.driver == "tmux" and bool(cfg.tui_autostart)
    if hosted and st.dash_pane:
        try:
            session_mod.start_dashboard(cfg, st.dash_pane)
            done.append("dashboard")
        except (subprocess.CalledProcessError, OSError) as exc:
            log.line(f"RESTART-DASHBOARD-FAILED {exc}")
    if cfg.web_enabled and not hosted and web_lifecycle.stop(cfg):
        if web_lifecycle.start_detached(cfg):
            done.append("web board")
    if cfg.telegram_commands and tgbot.stop(cfg):
        if tgbot.start_detached(cfg)[0]:
            done.append("telegram listener")
    return done


# -- in place --------------------------------------------------------------------
#: What :func:`in_place` returns first: it worked; nothing was changed (the old
#: supervisor is still running); or the sessions run on without a supervisor.
OK = "ok"
UNCHANGED = "unchanged"
UNSUPERVISED = "unsupervised"
#: A full restart that went down and did not come back up.
DOWN = "down"


def start_supervisor(cfg: Config, env: dict[str, str], log: Log) -> int | None:
    """Start a supervisor that adopts the run as it stands; its pid once it has
    the FIFO open, ``None`` if two attempts both failed."""
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        with (cfg.log_dir / START_ERR).open("ab") as err:
            proc = subprocess.Popen(
                [sys.executable, "-m", "swarm_orchestrator", "_supervise", "--adopt"],
                cwd=str(cfg.project_dir), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=err, start_new_session=True,
            )
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline and proc.poll() is None:
            if supervisor_mark(cfg).get("pid") == proc.pid:
                return proc.pid
            time.sleep(0.05)
        log.line(f"RESTART-START-FAILED attempt={attempt} rc={proc.poll()}")
        if proc.poll() is None:
            proc.terminate()
    return None


def _wait_gone(pid: int, timeout: float, cancelled: Callable[[], bool]) -> str:
    """``gone``, ``cancelled`` or ``timeout``."""
    deadline = time.monotonic() + timeout
    while procs.alive(pid):
        if cancelled():
            return "cancelled"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(0.1)
    return "gone"


def _hand_over(cfg: Config, plan: dict, pid: int, log: Log) -> tuple[bool, str]:
    """Ask a supervisor that knows how to hand over, and wait until it has."""
    plan_id = plan["id"]
    if not _poke(cfg, f"handover {plan_id}"):
        return False, "the supervisor's control FIFO would not take the request"

    def cancelled() -> bool:
        now = load(cfg)
        return now.get("id") != plan_id or now.get("stage") == CANCELLED

    outcome = _wait_gone(pid, SAFE_TIMEOUT_S, cancelled)
    if outcome == "gone":
        return True, ""
    _poke(cfg, f"handover-cancel {plan_id}")
    if outcome == "cancelled":
        return False, "cancelled"
    waits = load(cfg).get("waiting") or []
    return False, ("no safe moment came within "
                   f"{int(SAFE_TIMEOUT_S // 60)} minutes: still waiting for "
                   f"{_join(waits) if waits else 'the supervisor'}")


def _stop_legacy(cfg: Config, plan: dict, pid: int, log: Log) -> tuple[bool, str, bool]:
    """Stop a supervisor that predates the hand-over, once its exit would end
    nothing. Returns ``(stopped, why not, we paused the swarm)``.

    Launches are held with a pause while it waits, so the moment comes: a busy
    swarm otherwise always has one starting. The pause is lifted by the caller
    once the new supervisor is up (or here, when the stop is given up)."""
    plan_id = plan["id"]
    with state_mod.transaction(cfg) as st:
        paused = not st.paused
        st.paused = True
    log.line(f"RESTART-LEGACY pid={pid} predates the hand-over; pausing={paused}")

    def unpause() -> None:
        if paused:
            with state_mod.transaction(cfg) as st:
                st.paused = False
            _poke(cfg, "resume")

    deadline = time.monotonic() + SAFE_TIMEOUT_S
    told: list[str] | None = None
    while True:
        if not procs.alive(pid):
            return True, "", paused  # it went on its own
        waits = legacy_blockers(cfg, state_mod.read(cfg))
        if waits != told:
            told = waits
            if update(cfg, plan_id, waiting=waits) is None:
                unpause()
                return False, "cancelled", False
            if waits:
                log.line(f"RESTART-WAITING {', '.join(waits)}")
        if load(cfg).get("stage") == CANCELLED:
            unpause()
            return False, "cancelled", False
        if not waits:
            break
        if time.monotonic() >= deadline:
            unpause()
            return False, ("no safe moment came within "
                           f"{int(SAFE_TIMEOUT_S // 60)} minutes: still waiting for "
                           f"{_join(waits)}"), False
        time.sleep(0.5)
    _poke(cfg, "shutdown")
    if _wait_gone(pid, STOP_TIMEOUT_S, lambda: False) != "gone":
        unpause()
        return False, ("the old supervisor was told to stop and has not, after "
                       f"{int(STOP_TIMEOUT_S // 60)} minutes; once it has, run"
                       " `swarm restart` again"), False
    return True, "", paused


def in_place(cfg: Config, plan: dict, log: Log) -> tuple[str, str]:
    """Replace the supervisor process and leave everything else running.

    Returns ``(OK | UNCHANGED | UNSUPERVISED, detail)``. With no supervisor running (it
    crashed, or was killed) this starts one that adopts the run as it stands,
    which is how a swarm is picked up again without losing its parked sessions."""
    from . import session as session_mod

    st = state_mod.read(cfg)
    old = live_supervisor(cfg, st)
    if cfg.driver == "tmux" and not (
            tmux.session_exists(cfg.session) and session_mod.owns_session(cfg, st.windows)):
        return UNCHANGED, (f"the swarm's tmux session {cfg.session!r} is not up, so there is"
                           " nothing to keep running: `swarm up` starts it")
    if old is None and has_reader(cfg):
        return UNCHANGED, ("something holds the control FIFO, but it is not the supervisor"
                           " this run recorded; `swarm doctor` says what")
    if old is None and (st.finished or runs_mod.current(cfg.state_dir) is None):
        # Finished, or stopped with `swarm down`: nothing is left to take over.
        return UNCHANGED, "no run is in progress: `swarm up` starts one"
    env = (supervisor_env(old) if old else None) or clean_env(cfg)
    update(cfg, plan["id"], stage=STOPPING, old_pid=old)
    paused = False
    with Bridge(cfg):
        if old is not None:
            if capable(cfg, old):
                stopped, why = _hand_over(cfg, plan, old, log)
            else:
                stopped, why, paused = _stop_legacy(cfg, plan, old, log)
            if not stopped:
                return UNCHANGED, why
        if state_mod.read(cfg).finished:
            return OK, "the run finished while the restart waited; nothing left to restart"
        latest = load(cfg)
        if latest.get("id") != plan["id"] or latest.get("stage") == CANCELLED \
                or runs_mod.current(cfg.state_dir) is None:
            # Replaced by a newer request, or a `swarm down` came in between.
            return UNCHANGED, "cancelled"
        update(cfg, plan["id"], stage=RESTARTING, waiting=[])
        new = start_supervisor(cfg, env, log)
    if paused:
        # The pause was this restart's own, to find a safe moment: lifted
        # whether or not a supervisor is there to hear it.
        with state_mod.transaction(cfg) as s2:
            s2.paused = False
    if new is None:
        return UNSUPERVISED, f"the new supervisor did not start ({cfg.log_dir / START_ERR} says why)"
    if paused:
        _poke(cfg, "resume")
    helpers = restart_helpers(cfg, state_mod.read(cfg), log)
    workers, asked = counts(state_mod.read(cfg))
    return OK, (f"supervisor pid {old or 'none'} -> {new}; {_n(workers, 'worker')} and"
                f" {_n(asked, 'question')} untouched"
                + (f"; restarted the {_join(helpers)}" if helpers else ""))


# -- what the old supervisor hands its successor ---------------------------------
def write_handover(cfg: Config, data: dict) -> None:
    _write_json(Path(cfg.state_dir) / HANDOVER_FILE, {**data, "at": time.time()})


def take_handover(cfg: Config) -> dict:
    """The predecessor's memory, read once. Empty when there is none, or it is
    from some earlier restart."""
    path = Path(cfg.state_dir) / HANDOVER_FILE
    data = _read_json(path)
    try:
        path.unlink()
    except OSError:
        pass
    if not data or time.time() - float(data.get("at") or 0.0) > HANDOVER_FRESH_S:
        return {}
    return data


# -- full restart: carrying the questions across -----------------------------------
def kept_path(cfg: Config) -> Path:
    return Path(cfg.state_dir) / KEPT_FILE


def load_kept(cfg: Config) -> dict:
    return _read_json(kept_path(cfg))


def hold_session(cfg: Config) -> str:
    """The tmux session the kept windows wait in while the swarm is down."""
    return f"{cfg.session}-kept"


def _markers(cfg: Config, kind: str, ident: str) -> list[str]:
    out = [f"{procs.SESSION_ENV}={kind}:{ident}"]
    if kind == state_mod.WORKER:
        out.append(f"{cfg.env_marker}={ident}")
    return out


def kept_markers(cfg: Config) -> tuple[str, ...]:
    """The environment markers of every session in transit (``swarm down`` must
    not end them)."""
    return tuple(m for e in load_kept(cfg).get("sessions") or [] for m in e.get("markers") or [])


def kept_roots(cfg: Config) -> set[int]:
    """The pane processes of every session in transit."""
    out: set[int] = set()
    for entry in load_kept(cfg).get("sessions") or []:
        for pid, ticks in entry.get("pids") or []:
            if procs.same(int(pid), ticks):
                out.add(int(pid))
    return out


def kept_mirrors(cfg: Config) -> set[str]:
    """The ``swarm/<name>`` mirrors live sessions in transit are working in:
    ``swarm up`` must neither set them aside nor merge them."""
    return {e["mirror"] for e in load_kept(cfg).get("sessions") or [] if e.get("mirror")}


def kept_phases(cfg: Config) -> set[str]:
    """The worker phases in transit: their claim is not over."""
    return {e["ident"] for e in load_kept(cfg).get("sessions") or []
            if e.get("kind") == state_mod.WORKER}


def _pane_pids(window: str) -> list[list]:
    out = tmux.run(["list-panes", "-t", window, "-F", "#{pane_pid}"])
    pids = [int(p) for p in out.stdout.split() if p.isdigit()]
    return [[p, procs.start_ticks(p)] for p in pids]


def _own_window(cfg: Config, key: str, pane: str | None) -> str | None:
    """Give a session still in its home pane a window of its own; its id."""
    if not pane:
        return None
    name = state_mod.wait_window(key)
    try:
        win = tmux.window_of(pane)
        if len(tmux.list_panes(win)) > 1:
            return tmux.break_pane(pane, name, cfg.session) or None
        tmux.rename_window(win, name)
        return win
    except (subprocess.CalledProcessError, OSError):
        return None


def carry_out(cfg: Config, plan_id: str, log: Log) -> list[str]:
    """Move every session waiting on the owner out of the swarm's tmux session
    before it is torn down, and record what the next ``swarm up`` must restore.

    Each is alive and stays alive: its window moves to :func:`hold_session`, and
    ``swarm down`` spares the processes that carry its markers. The supervisor
    parked the ones that asked before it stopped; one that asked since is given
    its own window here."""
    from . import operator as operator_mod

    st = state_mod.read(cfg)
    entries: list[dict] = []
    keys = list(dict.fromkeys([*st.parked, *st.waiting]))
    hold = hold_session(cfg)
    for key in keys:
        kind, ident = state_mod.waiter(key)
        name = state_mod.wait_window(key)
        win = st.windows.get(name)
        if cfg.driver == "tmux" and key not in st.parked:
            pane = (st.operator_pane if kind == state_mod.OPERATOR
                    else st.master_pane if kind == state_mod.OVERSEER
                    else next((s.pane_id for s in st.slots if s.busy and s.phase == ident), None))
            win = _own_window(cfg, key, pane)
        entry: dict = {
            "key": key, "kind": kind, "ident": ident, "window": name, "win": win,
            "markers": _markers(cfg, kind, ident),
            "pids": _pane_pids(win) if cfg.driver == "tmux" and win else [],
        }
        if kind == state_mod.WORKER:
            entry["lane"] = st.lanes.get(ident)
            entry["mirror"] = ident if cfg.git_isolation == "worktree" else ""
        elif kind == state_mod.OPERATOR:
            item = opqueue.load(cfg, ident)
            entry["item"] = item.to_dict() if item is not None else None
            entry["mirror"] = (operator_mod.job_mirror(cfg, ident)
                               if cfg.git_isolation == "worktree" else "")
        else:
            entry["mirror"] = (ovrecord.mirror_name(ident)
                               if cfg.git_isolation == "worktree" else "")
        entries.append(entry)
    if not entries:
        return []
    if cfg.driver == "tmux":
        if not tmux.session_exists(hold):
            tmux.new_session(hold)
            tmux.mark_owner(hold, str(cfg.state_dir))
        for entry in entries:
            if entry["win"]:
                tmux.run(["move-window", "-d", "-s", entry["win"], "-t", f"={hold}:"])
    _write_json(kept_path(cfg), {"plan": plan_id, "at": time.time(), "hold": hold,
                                 "sessions": entries})
    log.line(f"RESTART-KEPT {' '.join(e['key'] for e in entries)}")
    return [e["key"] for e in entries]


def _still_there(cfg: Config, entry: dict) -> bool:
    """Is the kept session still running?"""
    if cfg.driver == "tmux":
        return bool(entry.get("win")) and tmux.window_alive(entry["win"])
    want = {m.encode() for m in entry.get("markers") or []}
    run = f"SWARM_STATE_DIR={cfg.state_dir}".encode()
    for pid in procs.table():
        env = procs.environ(pid)
        if run in env and want.intersection(env):
            return True
    return False


def carry_in(cfg: Config, log: Log) -> list[str]:
    """Put the sessions :func:`carry_out` set aside back into the run: their
    windows into the new tmux session, their keys into ``parked``. Called by
    ``swarm up`` once the session exists and before the supervisor starts, so
    the launcher never sees a kept phase as ready. A session that ended while
    the swarm was down is left out; its work is where it left it."""
    kept = load_kept(cfg)
    entries = kept.get("sessions") or []
    if not entries:
        return []
    restored: list[str] = []
    gone: list[str] = []
    for entry in entries:
        key = entry.get("key") or ""
        if not key or not _still_there(cfg, entry):
            gone.append(key)
            continue
        if cfg.driver == "tmux":
            tmux.run(["move-window", "-d", "-s", entry["win"], "-t", f"={cfg.session}:"])
        with state_mod.transaction(cfg) as st:
            st.park(key)
            if cfg.driver == "tmux" and entry.get("win"):
                st.windows[entry["window"]] = entry["win"]
            if entry.get("kind") == state_mod.WORKER and entry.get("lane"):
                st.lanes[entry["ident"]] = list(entry["lane"])
        if entry.get("kind") == state_mod.OPERATOR and entry.get("item"):
            opqueue.restore(cfg, entry["item"])
        restored.append(key)
    hold = kept.get("hold")
    if cfg.driver == "tmux" and hold and tmux.session_exists(hold) \
            and tmux.session_owner(hold) == str(cfg.state_dir):
        tmux.kill_session(hold)
    try:
        kept_path(cfg).unlink()
    except OSError:
        pass
    log.line(f"RESTART-KEPT-RESTORED {' '.join(restored) or '-'}"
             + (f" gone={' '.join(gone)}" if gone else ""))
    return restored


# -- running it ---------------------------------------------------------------------
def spawn_runner(cfg: Config, plan_id: str, at: float | None = None) -> subprocess.Popen | None:
    """Start ``swarm _restart-run`` in a session of its own: it stops the
    supervisor that may have started it, and the dashboard a caller may be in.
    With ``at`` it sleeps until then first (the timer for a supervisor that
    predates scheduled restarts)."""
    argv = [sys.executable, "-m", "swarm_orchestrator", "--project-dir", str(cfg.project_dir)]
    if getattr(cfg, "config_file", None):
        argv += ["--config", str(cfg.config_file)]
    argv += ["_restart-run", plan_id]
    if at is not None:
        argv += ["--at", repr(float(at))]
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (cfg.log_dir / RUN_LOG).open("a", encoding="utf-8") as out:
            return subprocess.Popen(
                argv, cwd=str(cfg.project_dir), env=clean_env(cfg),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    except OSError:
        return None


def fail(cfg: Config, plan: dict, detail: str, log: Log, *, left: str = UNCHANGED) -> None:
    """Record a restart that did not happen and tell the owner.

    ``left`` is how the swarm was left: :data:`UNCHANGED` (still running as it
    was), :data:`UNSUPERVISED` (sessions alive, no supervisor) or :data:`DOWN`."""
    update(cfg, plan.get("id", ""), stage=FAILED, detail=detail, ended_at=time.time())
    log.line(f"RESTART-FAILED id={plan.get('id')} by={plan.get('by')!r} left={left} {detail}")
    by = plan.get("by") or "owner terminal"
    tail = {
        UNSUPERVISED: (" The swarm is NOT supervised now: workers and questions are still in"
                       " their windows, but nothing is started or merged. Run `swarm restart`"
                       " to bring the supervisor back (it keeps them); `swarm doctor` shows"
                       " the state."),
        DOWN: " The swarm is DOWN. Run `swarm up` to start it.",
    }.get(left, " Nothing was changed: the swarm runs on as it was.")
    telegram.notify(
        cfg.telegram_notify,
        f"swarm: {cfg.slug} did not restart (asked by {by}): {detail}.{tail}",
        kind="restart", source="restart.fail", state_dir=cfg.state_dir,
    )


def _sleep_until(cfg: Config, plan_id: str, at: float) -> bool:
    """Wait for ``at``; False when the plan was dropped or replaced meanwhile."""
    while True:
        plan = load(cfg)
        if plan.get("id") != plan_id or plan.get("stage") != PLANNED:
            return False
        left = at - time.time()
        if left <= 0:
            return True
        time.sleep(min(left, 5.0))


def run(cfg: Config, plan_id: str, at: float | None = None) -> int:
    """The restart itself (``swarm _restart-run``), detached from every session."""
    if at is not None and not _sleep_until(cfg, plan_id, at):
        return 0
    plan = load(cfg)
    if plan.get("id") != plan_id or plan.get("stage") != PLANNED:
        print(f"restart {plan_id}: no longer planned (stage={plan.get('stage')}); nothing to do")
        return 0
    log = Log(cfg.supervisor_log)
    try:
        with (Path(cfg.state_dir) / "restart-run.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fail(cfg, plan, "another restart is already under way", log)
                return 1
            update(cfg, plan_id, runner_pid=os.getpid(), fired_at=time.time())
            log.line(f"RESTART-FIRED id={plan_id} mode={plan.get('mode')} by={plan.get('by')!r}")
            if plan.get("mode") == FULL:
                return _run_full(cfg, plan, log)
            return _finish_in_place(cfg, plan, log)
    finally:
        log.close()


def _finish_in_place(cfg: Config, plan: dict, log: Log) -> int:
    outcome, detail = in_place(cfg, plan, log)
    if outcome == OK:
        update(cfg, plan["id"], stage=DONE, detail=detail, ended_at=time.time())
        log.line(f"RESTART-DONE id={plan['id']} by={plan.get('by')!r} {detail}")
        print(f"restart done: {detail}")
        return 0
    if detail == "cancelled":
        log.line(f"RESTART-CANCELLED id={plan['id']} while waiting for a safe moment")
        return 0
    fail(cfg, plan, detail, log, left=outcome)
    print(f"restart FAILED: {detail}")
    return 1


def refusal(plan: dict, qs: list[Question]) -> str:
    return (f"{_n(len(qs), 'session')} waiting on the owner would be closed by a full restart:"
            f" {_join([q.who for q in qs])}")


def _run_full(cfg: Config, plan: dict, log: Log) -> int:
    """Start the drain a full restart waits behind. The supervisor ends it:
    once nothing is left running it starts ``swarm _drain-down``, which sees the
    plan and runs :func:`finish_full`."""
    st = state_mod.read(cfg)
    qs = questions(cfg, st)
    policy = plan.get("questions") or REFUSE
    if qs and policy == REFUSE:
        fail(cfg, plan, refusal(plan, qs) + "; answer them, or plan it again with"
             " --wait-questions or --keep-questions", log)
        return 1
    old = live_supervisor(cfg, st)
    if old is not None and not capable(cfg, old):
        # Only a supervisor on this code waits for questions or parks them.
        outcome, detail = in_place(cfg, plan, log)
        if outcome != OK:
            fail(cfg, plan, f"could not load the new supervisor first: {detail}", log,
                 left=outcome)
            return 1
    if not has_reader(cfg):
        print("no supervisor is running, so there is nothing to wait for")
        return spawn_drain_down(cfg, plan, log)
    with state_mod.transaction(cfg) as s2:
        s2.drain = {"since": time.time(), "then": "", "waiting": [],
                    "restart": plan["id"], "questions": policy}
    update(cfg, plan["id"], stage=DRAINING)
    _poke(cfg, "drain")
    log.line(f"RESTART-DRAINING id={plan['id']} questions={policy}")
    print("draining: the swarm stops once the running work is finished, then comes back up")
    return 0


def spawn_drain_down(cfg: Config, plan: dict, log: Log) -> int:
    """With no supervisor to end the drain, run its last step from here."""
    from . import drain as drain_mod

    with state_mod.transaction(cfg) as st:
        st.drain = {"since": time.time(), "then": "", "waiting": [], "stopping_at": time.time(),
                    "restart": plan["id"], "questions": plan.get("questions") or REFUSE}
    update(cfg, plan["id"], stage=DRAINING)
    if not drain_mod.spawn_down(cfg):
        fail(cfg, plan, "could not start the stop", log)
        return 1
    return 0


def finish_full(cfg: Config, plan_id: str, down: Callable[[Config], int],
                up: Callable[[Config], int]) -> int:
    """The drain is over: carry the questions out, ``swarm down``, ``swarm up``
    (which carries them back in), and say so if it did not come back."""
    plan = load(cfg)
    if plan.get("id") != plan_id:
        plan = {"id": plan_id, "by": "?", "questions": FORCE}
    log = Log(cfg.supervisor_log)
    try:
        update(cfg, plan_id, stage=RESTARTING)
        kept = carry_out(cfg, plan_id, log) if plan.get("questions") == KEEP else []
        log.line(f"RESTART-FULL-DOWN id={plan_id} kept={len(kept)}")
        try:
            down(cfg)
        except BrokenPipeError:
            pass
        with state_mod.transaction(cfg) as st:
            st.drain = {}
        try:
            rc = up(cfg)
        except Exception as exc:  # noqa: BLE001 - the owner must hear, whatever broke
            rc, detail = 1, f"`swarm up` raised {exc!r}"
        else:
            detail = "`swarm up` failed (logs/drain-down.log says why)"
        if rc == 0 and has_reader(cfg):
            words = f"{_n(len(kept), 'question')} carried across" if kept else "nothing to carry"
            update(cfg, plan_id, stage=DONE, detail=words, ended_at=time.time())
            log.line(f"RESTART-DONE id={plan_id} by={plan.get('by')!r} full; {words}")
            return 0
        if load_kept(cfg):
            detail += (f"; the kept questions are alive in tmux session {hold_session(cfg)}"
                       " and `swarm up` brings them back")
        fail(cfg, plan, detail, log, left=DOWN)
        return 1
    finally:
        log.close()
