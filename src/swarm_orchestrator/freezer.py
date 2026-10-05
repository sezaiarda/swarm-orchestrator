"""``swarm freeze`` / ``swarm thaw``: stop every session in place, then wake them.

The freeze is the kernel's (``cgroup.freeze``), never a stop signal: tmux
continues a stopped pane, and a frozen group takes no CPU and can be pushed out
to swap whole. This only freezes; what is done with the memory is the caller's
business. The supervisor stays awake and aware: while the record below exists it
launches nothing, reaps nothing, pings nothing and times nothing out
(:meth:`supervisor.Supervisor._freeze_tick`).

**What is frozen** is decided by membership, never by a scope's name
(:func:`plan`): every process that carries this run's ``SWARM_STATE_DIR``, plus
the trees under the tmux session's panes, each mapped to its group through
``/proc/<pid>/cgroup``. Never frozen: the groups of the supervisor, the Telegram
listener, a headless web board, the caller and the tmux server. One of those
that also holds sessions is reported ``shared``: they stay awake with it. So
that this does not happen by accident, the swarm starts its own long-lived
processes in a scope of their own where it can (:func:`scoped`).

**The record** is ``State.frozen``, written when the freeze is asked for and
dropped by the thaw. A group is named by its path as the kernel prints it
(``/user.slice/…``), with the kind and id of the session in it, which is what
gives the thaw its order.

**The freeze** (:func:`freeze`) is made with the state lock and the build queue
lock in hand, and they are kept until every group says it is frozen: a process
frozen while it holds either would stop everything still awake. A group that
was told to freeze and does not settle takes the whole freeze back. A group
this user may not write (a scope root started) is no error: it is *left*, named
in the record and in ``--json``, for a caller that can to freeze and wake.

**Frozen time is not elapsed time.** Each freeze is a span in
``<state>/history/frozen.jsonl`` (:func:`spans`); a timeout, a grace or a count
of working hours takes them out (:func:`awake_elapsed`). At the thaw every
deadline the swarm keeps is moved along by how long the freeze lasted
(:func:`rebase`): the files here, the supervisor's memory in
:meth:`supervisor.Supervisor._shift_clocks`.

**The thaw** (:func:`thaw`) writes with no lock a session can hold in its
hand, for the same reason turned round: whoever froze the sessions may have
frozen more than this did. Sessions wake a few seconds apart
(:func:`thaw_order`), because a mass wake-up races one login token.

**One verb at a time.** ``swarm freeze`` and ``swarm thaw`` each hold
``<state>/freeze.lock`` from their first step to their last (:func:`turn`), so
neither ever acts on a record the other is half way through. Nothing else
takes that lock, so no frozen session can be holding it. And a group is named
in the record before it is told to freeze, so that whatever cuts a freeze
short, a thaw can undo all of it.

Every access to the cgroup tree goes through :class:`Cgroups`, whose two roots
come from ``SWARM_CGROUP_ROOT`` and ``SWARM_CGROUP_PROC``, so the tests point
it at a tree of their own.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import procs
from . import state as state_mod
from .config import Config

#: Where the cgroup tree and ``/proc`` are read from (the test seam).
ROOT_ENV = "SWARM_CGROUP_ROOT"
PROC_ENV = "SWARM_CGROUP_PROC"
#: ``SWARM_SCOPE=0``: never start anything in a scope of its own.
SCOPE_ENV = "SWARM_SCOPE"

#: ``State.frozen["stage"]``: asked for and waiting for the supervisor to go
#: quiet; every group frozen; being woken.
FREEZING = "freezing"
FROZEN = "frozen"
THAWING = "thawing"

#: How long a group gets to say it is frozen before the freeze is taken back.
SETTLE_S = 10.0
#: How long the freeze tries for each of its two locks.
LOCK_S = 10.0
#: The lock ``swarm freeze`` and ``swarm thaw`` share, under the state dir, and
#: how long one of them waits for the other to finish before it gives up.
TURN_LOCK = "freeze.lock"
TURN_S = 600.0
#: How long a thaw tries for the state lock, to say in the record that it has
#: begun, before it wakes the sessions without having said so: a frozen session
#: may be the one holding it.
MARK_S = 5.0
#: Closed freezes, one ``{"since", "until"}`` a line, under ``<state>/history``.
SPANS = "frozen.jsonl"

#: The kinds of group besides a session's own (``worker``, ``operator``,
#: ``overseer``, ``resolver``, …, from its ``SWARM_SESSION_ID``).
CONSOLE = "console"
DASHBOARD = "dashboard"
OTHER = "other"
#: A group holding several things is named for the first of these it holds.
_KINDS = (CONSOLE, DASHBOARD, state_mod.OPERATOR, state_mod.OVERSEER, "resolver",
          state_mod.WORKER)
#: The run's own groups that are never frozen, in the order they are named.
_ROLES = ("supervisor", "bot", "board", "tmux", "caller")

_SCOPE = ("systemd-run", "--user", "--scope", "--quiet", "--collect")


class Busy(RuntimeError):
    """A lock the freeze has to hold stayed taken."""


@dataclass(frozen=True)
class Cgroups:
    """The cgroup v2 tree and the ``/proc`` that says who is in which group."""

    root: Path = Path("/sys/fs/cgroup")
    proc: Path = Path("/proc")

    @classmethod
    def from_env(cls) -> "Cgroups":
        return cls(Path(os.environ.get(ROOT_ENV) or "/sys/fs/cgroup"),
                   Path(os.environ.get(PROC_ENV) or "/proc"))

    def of(self, pid: int | str) -> str | None:
        """The group ``pid`` is in (``"self"``: this process), as the kernel
        prints it; None when the process is gone."""
        try:
            text = (self.proc / str(pid) / "cgroup").read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            if line.startswith("0::"):
                return line[3:].strip() or "/"
        return None

    def _file(self, path: str, name: str) -> Path:
        return self.root / path.lstrip("/") / name

    def _read(self, path: str, name: str) -> str | None:
        try:
            return self._file(path, name).read_text(encoding="utf-8")
        except OSError:
            return None

    def exists(self, path: str) -> bool:
        return self._file(path, "cgroup.freeze").is_file()

    def set(self, path: str, frozen: bool) -> bool:
        """Ask ``path`` to freeze or to wake. False when it would not take it
        (the group is gone, or is not this user's to write)."""
        try:
            fd = os.open(self._file(path, "cgroup.freeze"), os.O_WRONLY)
        except OSError:
            return False
        try:
            os.write(fd, b"1" if frozen else b"0")
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def asked(self, path: str) -> bool:
        """Is ``path`` told to be frozen?"""
        return (self._read(path, "cgroup.freeze") or "").strip() == "1"

    def settled(self, path: str) -> bool:
        """Has every process in ``path`` stopped?"""
        events = self._read(path, "cgroup.events") or ""
        return any(line.split() == ["frozen", "1"] for line in events.splitlines())


# -- scopes -----------------------------------------------------------------
def can_scope(env: dict[str, str] | None = None) -> bool:
    """Can a process be started in a systemd user scope of its own, in ``env``
    (this process's own when none is given)? Asked of the environment the
    process will be started in: another one's answer says nothing about it."""
    env = dict(os.environ) if env is None else env
    if env.get(SCOPE_ENV) == "0":
        return False
    if shutil.which("systemd-run", path=env.get("PATH")) is None \
            or not env.get("XDG_RUNTIME_DIR"):
        return False
    try:
        probe = subprocess.run([*_SCOPE, "true"], capture_output=True, timeout=10, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def scoped(argv: list[str], env: dict[str, str] | None = None) -> list[str]:
    """``argv``, to be started in a scope of its own where one can be made.
    ``env`` is the environment it will be started in, when that is not this
    process's own.

    ``systemd-run --scope`` moves itself into the new scope and then becomes
    the command, so the pid the caller gets is the command's own. Where no
    scope can be made ``argv`` comes back as it is, and the process shares the
    group of whatever starts it."""
    return [*_SCOPE, "--", *argv] if can_scope(env) else list(argv)


def in_scope(argv: list[str]) -> bool:
    """Did :func:`scoped` give ``argv`` a scope?"""
    return tuple(argv[:len(_SCOPE)]) == _SCOPE


# -- the record -------------------------------------------------------------
def peek_state(cfg: Config) -> dict:
    """``state.json`` as it is on disk, read without the lock: the file is
    swapped in whole, and a frozen process may be holding the lock."""
    try:
        data = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def look(cfg: Config) -> dict | None:
    """The frozen record, read without the lock: ``{}`` when there is none, and
    None when the state could not be read, which says nothing either way. For
    whoever would do harm by taking "could not tell" for "not frozen"."""
    try:
        data = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}  # no state at all: no run, and nothing frozen
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    record = data.get("frozen")
    return dict(record) if isinstance(record, dict) else {}


def peek(cfg: Config) -> dict:
    """The frozen record, read without the lock; ``{}`` when there is none, or
    the state could not be read (:func:`look` tells the two apart)."""
    return look(cfg) or {}


@contextmanager
def turn(cfg: Config, wait_s: float | None = None) -> Iterator[bool]:
    """Hold the lock ``swarm freeze`` and ``swarm thaw`` share for the block:
    the whole of one of them. Yields whether it was taken; after ``wait_s``
    (:data:`TURN_S`) of the other still running it was not, and the caller
    changes nothing.

    Only those two take it, each in the group of whoever ran it, which no
    freeze of this run ever stops: it can never be in a frozen hand."""
    cfg.ensure_dirs()
    deadline = time.monotonic() + (TURN_S if wait_s is None else wait_s)
    with (cfg.state_dir / TURN_LOCK).open("w") as lockf:
        got = True
        while True:
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    got = False
                    break
                time.sleep(0.05)
        try:
            yield got
        finally:
            if got:
                fcntl.flock(lockf, fcntl.LOCK_UN)


def still_frozen(record: dict, cg: Cgroups | None = None) -> list[str]:
    """The groups of ``record`` that are there and still told to be frozen.

    None left is a record nothing stands behind any more: the machine was
    restarted, or everything in it was woken or ended some other way."""
    cg = cg or Cgroups.from_env()
    return [g["path"] for g in record.get("cgroups") or [] if cg.asked(g["path"])]


def line(record: dict, now: float | None = None) -> str:
    """The freeze in plain English, for ``swarm status``."""
    if not record:
        return ""
    now = time.time() if now is None else now
    since = float(record.get("since") or now)
    groups = len(record.get("cgroups") or [])
    what = f"{groups} group{'' if groups == 1 else 's'}"
    left = len(record.get("left") or [])
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
    stage = record.get("stage")
    if stage == FREEZING:
        waits = list(record.get("waiting") or [])
        tail = f", waiting for {', '.join(waits)}" if waits else ""
        return f"Freezing since {when}: {what} to freeze{tail}"
    if stage == THAWING:
        return f"Thawing: frozen since {when}, {what} being woken (`swarm thaw` finishes it)"
    aside = f" ({left} left alone: not this user's to freeze)" if left else ""
    return f"Frozen since {when}: {what} frozen{aside}, nothing runs until `swarm thaw`"


def close_span(cfg: Config, since: float, until: float) -> None:
    """Append one closed freeze to ``<state>/history/frozen.jsonl``. A thaw run
    a second time finds its span already there and adds nothing."""
    from . import runs as runs_mod

    path = runs_mod.history_dir(cfg.state_dir) / SPANS
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        last = path.read_text(encoding="utf-8").splitlines()[-1]
        if json.loads(last).get("since") == since:
            return
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"since": since, "until": until}) + "\n")


# -- frozen time is not elapsed time ----------------------------------------
def merged(stretches) -> list[tuple[float, float]]:
    """``(since, until)`` stretches, oldest first, those that touch made one."""
    out: list[tuple[float, float]] = []
    for since, until in sorted(s for s in stretches if s[1] > s[0]):
        if out and since <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], until))
        else:
            out.append((since, until))
    return out


def spans(cfg: Config, now: float | None = None,
          record: dict | None = None) -> list[tuple[float, float]]:
    """Every stretch this run stood frozen, oldest first, as ``(since, until)``:
    the closed ones from the history, then the open one, which runs to the end
    its thaw stamped or to ``now``. ``record`` is ``State.frozen`` when the
    caller holds it already."""
    from . import runs as runs_mod

    now = time.time() if now is None else now
    found: list[tuple[float, float]] = []
    try:
        lines = (runs_mod.history_dir(cfg.state_dir) / SPANS).read_text(
            encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for text in lines:
        try:
            row = json.loads(text)
            found.append((float(row["since"]), float(row["until"])))
        except (ValueError, TypeError, KeyError):
            continue
    record = peek(cfg) if record is None else record
    if record.get("since"):
        found.append((float(record["since"]), float(record.get("until") or now)))
    return merged(found)


def frozen_in(frozen: list[tuple[float, float]], start: float, end: float) -> float:
    """How much of ``start``..``end`` lies inside the ``frozen`` stretches."""
    return sum(max(0.0, min(end, until) - max(start, since)) for since, until in frozen)


def awake_elapsed(cfg: Config, start: float, now: float | None = None) -> float:
    """The time since ``start`` that the run was awake for: the wall clock's,
    less every frozen stretch in it. What a timeout, a grace or an idle limit
    counts, so that none of them runs out on the first look after a thaw."""
    now = time.time() if now is None else now
    return max(0.0, now - start - frozen_in(spans(cfg, now), start, now))


def state_now(record: dict, now: float | None = None) -> float:
    """The moment the state's own clocks are read against: now, or when the
    freeze that is still on began. A park deadline, the last event and the
    moment a session asked stand where they stood then, and the thaw moves
    them along (:func:`rebase`); until it has, an age taken from the wall clock
    would count the frozen hours. ``record`` is ``State.frozen``."""
    now = time.time() if now is None else now
    since = float(record.get("since") or 0.0) if record else 0.0
    if not since or "state" in (record.get("shifted") or []):
        return now
    return min(now, since)


#: What a thaw moves along besides the state, in order: each a module's own
#: ``shift(cfg, delta, now)`` over the files it keeps.
_CLOCKS = ("operator", "overseer", "bigpic", "blocked", "builds")


def _shifters() -> dict:
    from . import bigpic, blockedping, buildidle, opqueue, overseer

    return {"operator": opqueue.shift, "overseer": overseer.shift, "bigpic": bigpic.shift,
            "blocked": blockedping.shift, "builds": buildidle.shift}


def rebase(cfg: Config, frozen_s: float) -> list[str]:
    """The thaw's one hook for time: called once the sessions are awake and
    before the record is dropped, with how long the freeze lasted. Whatever on
    disk kept counting while everything stood still is moved along from here:
    the state's own clocks (:meth:`state.State.shift`), then the operator
    queue, the Overseer's policy, the big-picture memory, the gathered blocked
    pings and the build gate's idle samples.

    Each is named in the record's ``shifted`` once it is moved, the state's in
    the same write that moves it, so a thaw cut short and run again moves
    nothing twice. Returns the ones that could not be moved (the thaw goes on
    without them: a run that never carries on is worse than a timer that fires
    early)."""
    record = peek(cfg)
    if frozen_s <= 0 or not record:
        return []
    now = float(record.get("until") or time.time())
    done = set(record.get("shifted") or [])
    shifters = _shifters()
    failed: list[str] = []
    for name in ("state", *_CLOCKS):
        if name in done:
            continue
        try:
            if name != "state":
                shifters[name](cfg, frozen_s, now)
            with state_mod.transaction(cfg) as st:
                if not st.frozen:
                    return failed  # dropped under us: whoever did carries on
                if name == "state":
                    st.shift(frozen_s, now)
                st.frozen.setdefault("shifted", []).append(name)
        except Exception:  # noqa: BLE001 - the thaw must reach its end
            failed.append(name)
    return failed


# -- what to freeze ----------------------------------------------------------
@dataclass(frozen=True)
class Plan:
    """What a freeze would stop (``[{path, kind, id}]``) and which of the
    run's own groups it leaves alone (``[{path, kind, shared}]``)."""

    frozen: list[dict]
    awake: list[dict]


def _label(env: list[bytes]) -> tuple[str, str]:
    """``(kind, id)`` of the session a process with this environment is in."""
    from . import console as console_mod

    mark = f"{procs.SESSION_ENV}=".encode()
    for entry in env:
        if entry.startswith(mark):
            kind, _, ident = entry[len(mark):].decode("utf-8", "replace").partition(":")
            if kind:
                return kind, ident
    if any(e.startswith(f"{console_mod.CONSOLE_ENV}=".encode()) for e in env):
        return CONSOLE, ""
    return OTHER, ""


def _name(labels: set[tuple[str, str]]) -> tuple[str, str]:
    """The one ``(kind, id)`` a group holding ``labels`` goes by."""
    for kind in _KINDS:
        found = sorted(label for label in labels if label[0] == kind)
        if found:
            return found[0]
    rest = sorted(label for label in labels if label[0] != OTHER)
    return rest[0] if rest else (OTHER, "")


def _roles(cfg: Config, st: state_mod.State, server: int | None) -> dict[str, int | str | None]:
    """The processes whose groups are never frozen, by what they are."""
    from . import restart as restart_mod
    from . import tgbot
    from .web import lifecycle as web_lifecycle

    return {
        "supervisor": restart_mod.live_supervisor(cfg, st),
        "bot": tgbot.running(cfg),
        "board": web_lifecycle.running(cfg),
        "tmux": server,
        "caller": "self",
    }


def plan(cfg: Config, st: state_mod.State, cg: Cgroups | None = None) -> Plan:
    """Which groups a freeze of this run stops, and which it leaves awake.

    A session is a process that carries a ``SWARM_SESSION_ID``, or anything
    under a pane of the run's tmux session. A group left awake that holds one
    is ``shared``: the session stays awake with it."""
    from . import console as console_mod
    from . import session as session_mod
    from . import tmux

    cg = cg or Cgroups.from_env()
    table = procs.table()
    members: dict[int, tuple[str, str]] = {
        pid: _label(procs.environ(pid)) for pid in session_mod._marked(cfg, table, ())
    }
    sessions = {pid for pid, label in members.items() if label[0] != OTHER}
    server = None
    if cfg.driver == "tmux" and session_mod.owns_session(cfg, st.windows):
        server = tmux.server_pid()
        named = {console_mod.WINDOW: CONSOLE, "dash": DASHBOARD}
        for root, window in tmux.session_pane_windows(cfg.session):
            if root not in table:
                continue
            for pid in session_mod._descendants(table, {root}):
                if window in named:
                    members[pid] = (named[window], "")
                else:
                    members.setdefault(pid, (OTHER, ""))
                sessions.add(pid)

    groups: dict[str, set[tuple[str, str]]] = {}
    shared: set[str] = set()
    for pid, label in members.items():
        path = cg.of(pid)
        if path is None:
            continue  # gone since the table was read
        groups.setdefault(path, set()).add(label)
        if pid in sessions:
            shared.add(path)

    awake: dict[str, list[str]] = {}
    for role, pid in _roles(cfg, st, server).items():
        path = cg.of(pid) if pid else None
        if path is not None:
            awake.setdefault(path, []).append(role)
    if "/" in groups:
        awake.setdefault("/", []).append("root")  # the root group cannot be frozen

    frozen = [
        {"path": path, "kind": kind, "id": ident}
        for path, (kind, ident) in sorted((p, _name(ls)) for p, ls in groups.items())
        if path not in awake
    ]
    return Plan(frozen, [
        {"path": path, "kind": "+".join(sorted(roles, key=_role_rank)),
         "shared": path in shared}
        for path, roles in sorted(awake.items())
    ])


def _role_rank(role: str) -> int:
    return _ROLES.index(role) if role in _ROLES else len(_ROLES)


# -- freeze -----------------------------------------------------------------
def freeze(cfg: Config, groups: list[dict], cg: Cgroups | None = None,
           settle_s: float | None = None, lock_s: float | None = None,
           ) -> tuple[list[dict], list[str], list[dict]]:
    """Freeze ``groups``; return ``(the ones now frozen, the paths that would
    not freeze, the ones left alone)``. Any of the second and everything is
    awake again.

    Made holding the state lock and the build queue lock, and they are kept
    until every group says it is frozen, so nothing is stopped with either in
    its hand. A group that is gone by now ended on its own and is left out. One
    that is there and would not take the order is not this user's to write: it
    is left alone, for whoever may write it to freeze and wake.
    Raises :class:`Busy` when a lock stays taken; nothing was frozen then."""
    from . import buildsem

    cg = cg or Cgroups.from_env()
    settle_s = SETTLE_S if settle_s is None else settle_s
    lock_s = LOCK_S if lock_s is None else lock_s
    with state_mod.held(cfg, lock_s) as state_lock, \
            buildsem._qlock(cfg, wait_s=lock_s) as queue_lock:
        if not state_lock or not queue_lock:
            raise Busy("the state lock" if not state_lock else "the build queue lock")
        live: list[dict] = []
        left: list[dict] = []
        for group in groups:
            if cg.set(group["path"], True):
                live.append(group)
            elif cg.exists(group["path"]):
                left.append(group)
        stuck = [g["path"] for g in live]
        deadline = time.monotonic() + settle_s
        while stuck:
            stuck = [p for p in stuck if cg.exists(p) and not cg.settled(p)]
            if not stuck or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if stuck:
            for group in live:
                cg.set(group["path"], False)
    return live, stuck, left


# -- thaw -------------------------------------------------------------------
def thaw_order(record: dict, st: state_mod.State) -> list[dict]:
    """The record's groups in the order they are woken: the owner's console,
    the dashboard, sessions asking the owner, the operator, the Overseer, a
    resolver, the workers by slot, then the rest."""
    asking = list(st.on_owner())
    slot_of = {s.phase: s.id for s in st.slots if s.busy and s.phase}

    def rank(group: dict) -> tuple[int, int]:
        kind, ident = group.get("kind"), group.get("id") or ""
        if kind == CONSOLE:
            return 0, 0
        if kind == DASHBOARD:
            return 1, 0
        if kind in (state_mod.WORKER, state_mod.OPERATOR, state_mod.OVERSEER):
            key = state_mod.waiter_key(kind, ident)
            if key in asking:
                return 2, asking.index(key)
        if kind == state_mod.OPERATOR:
            return 3, 0
        if kind == state_mod.OVERSEER:
            return 4, 0
        if kind == "resolver":
            return 5, 0
        if kind == state_mod.WORKER:
            return 6, slot_of.get(ident, len(st.slots))
        return 7, 0

    return sorted(record.get("cgroups") or [], key=rank)


def thaw(groups: list[dict], cg: Cgroups | None = None, gap_s: float = 0.0,
         sleep=time.sleep) -> list[str]:
    """Wake ``groups`` in the order given, ``gap_s`` apart after each one that
    holds a Claude session. Takes no lock. Returns the paths woken; a group
    that is gone is passed over."""
    cg = cg or Cgroups.from_env()
    woken: list[str] = []
    for i, group in enumerate(groups):
        if not cg.set(group["path"], False):
            continue
        woken.append(group["path"])
        last = i == len(groups) - 1
        if gap_s > 0 and not last and group.get("kind") not in (DASHBOARD, OTHER):
            sleep(gap_s)
    return woken
