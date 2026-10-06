"""Bring up / tear down the ``swarm`` tmux topology.

Layout: window 0 ``dash`` (the always-on TUI dashboard), then ``console`` (the
owner's own Claude session, :mod:`console`; not with ``[console] enabled`` off),
``overseer`` (the master pane: the init pass, then Overseer passes — the
supervisor process itself is detached and has no tty), ``operator`` (idle until a
hand-off opens a session in it), then one or more worker windows tagged
``@swarm_slot 0..N-1`` across a GLOBAL slot index. Slots paginate into windows of
at most ``[tmux].panes_per_window`` (default :data:`PANES_PER_WINDOW`; ``workers``,
``workers-2``, …), each laid out by
:func:`swarm_orchestrator.tmux.split_layout` per ``[tmux].layout`` (``auto`` = 1
full pane / 2 LEFT|RIGHT / 3-4 tiled; or a pinned preset such as
``even-vertical`` for a TOP/BOTTOM stack). Windows are referenced by captured id everywhere downstream; slot pane ids
are recorded in state so accounting is tag-driven (workers run their own teammates
in-process, so no teammate panes ever appear). There is no ``web`` window: the
dashboard serves the board itself (:mod:`swarm_orchestrator.tui.webboard`), and
without a dashboard ``swarm up`` starts it detached.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time

from . import keep as keep_mod
from . import procs
from . import restart as restart_mod
from . import state as state_mod
from . import tmux
from .config import Config

#: The default of ``[tmux].panes_per_window``.
PANES_PER_WINDOW = 4


def plan_worker_windows(
    total: int, per_window: int = PANES_PER_WINDOW
) -> list[tuple[str, int]]:
    """Split ``total`` worker slots into windows of at most ``per_window`` panes.

    Returns ``[(window_name, pane_count), …]``: the first window is ``workers`` and
    the rest ``workers-2``, ``workers-3``, … each holding
    ``min(per_window, remaining)`` panes (5 at 2 per window: 2, 2, 1). ``total <= 0``
    yields no windows. Pure — no tmux side effects.
    """
    per_window = max(1, per_window)
    plan: list[tuple[str, int]] = []
    remaining = total
    k = 0
    while remaining > 0:
        size = min(per_window, remaining)
        name = "workers" if k == 0 else f"workers-{k + 1}"
        plan.append((name, size))
        remaining -= size
        k += 1
    return plan


def setup(cfg: Config) -> dict[str, str]:
    """Create the hardened session + windows + tagged slots. Records state."""
    if tmux.session_exists(cfg.session):
        raise RuntimeError(
            f"tmux session {cfg.session!r} already exists; run `swarm down` first"
        )
    dash_win = tmux.new_session(cfg.session)
    tmux.mark_owner(cfg.session, str(cfg.state_dir))
    tmux.harden(cfg.session)
    tmux.rename_window(dash_win, "dash")
    dash_pane = tmux.list_panes(dash_win)[0]

    # The owner console sits right beside the dashboard, before the swarm's own
    # sessions. Its keeper is started below, once state knows the run.
    console_win = tmux.new_window(cfg.session, "console") if cfg.console_enabled else None

    master_win = tmux.new_window(cfg.session, "overseer")
    master_pane = tmux.list_panes(master_win)[0]

    # The operator's window, created here rather than on demand so it sits beside
    # the master instead of appearing mid-run. It holds `sleep infinity` until the
    # supervisor opens a session in it. Safe from `swarm layout` (which only
    # matches `workers*`) and from slot accounting (never `@swarm_slot`-tagged).
    operator_win = tmux.new_window(cfg.session, "operator")
    operator_pane = tmux.list_panes(operator_win)[0]

    # "master" is the durable key every downstream consumer already uses for the
    # LLM master's window; the *display* name is "overseer" because that is what
    # runs in it for all but the first minute (the init pass, then Overseer
    # passes). Renaming the key would invalidate st.windows for any run mid-flight.
    windows = {"dash": dash_win, "master": master_win, "operator": operator_win}
    if console_win is not None:
        windows["console"] = console_win
    slot_panes: dict[int, str] = {}
    base = 0
    for name, size in plan_worker_windows(cfg.max_workers, cfg.tmux_panes_per_window):
        win = tmux.new_window(cfg.session, name)
        for offset, pane in enumerate(tmux.split_layout(win, size, cfg.tmux_layout)):
            gidx = base + offset
            tmux.set_slot(pane, gidx)
            slot_panes[gidx] = pane
        windows[name] = win
        base += size

    if cfg.tui_autostart:
        start_dashboard(cfg, dash_pane)

    with state_mod.transaction(cfg) as st:
        st.windows = windows
        st.master_pane = master_pane
        st.dash_pane = dash_pane
        st.operator_pane = operator_pane
        for gidx, pane in slot_panes.items():
            if gidx < len(st.slots):
                st.slots[gidx].pane_id = pane
    if console_win is not None:
        from . import console as console_mod

        console_mod.start_keeper(cfg, tmux.list_panes(console_win)[0])
    return windows


def start_dashboard(cfg: Config, dash_pane: str) -> None:
    """Run the dashboard in ``dash_pane``, replacing whatever runs there:
    ``swarm up`` starts it, ``swarm restart`` starts it again on the new code.

    cd first: tmux.new_session takes no -c, so window 0 inherits whatever cwd
    `swarm up` ran from. And told its project like every other session
    (``SWARM_PROJECT``): SWARM_STATE_DIR pins the state dir, and a command the
    dashboard runs must read the settings and the ledger of the same project."""
    tmux.respawn_pane(
        dash_pane,
        f"cd {shlex.quote(str(cfg.project_dir))} && exec {cfg.tui_cmd}",
        env={"SWARM_STATE_DIR": str(cfg.state_dir), "SWARM_PROJECT": str(cfg.project_dir)},
    )


def _worker_window_index(name: str) -> int:
    """``workers`` -> 1, ``workers-3`` -> 3; anything else sorts last."""
    if name == "workers":
        return 1
    tail = name.removeprefix("workers-")
    return int(tail) if tail.isdigit() else 1 << 30


def add_slot_panes(
    cfg: Config, windows: dict[str, str], slot_ids: list[int], layout: str
) -> tuple[dict[int, str], dict[str, str], list[int]]:
    """Give each new slot a tagged holding pane; return ``(panes, windows, failed)``.

    ``swarm up`` builds every slot's pane in :func:`setup`; a live ``max_workers``
    grow used to add the slot record only, so its ``pane_id`` stayed ``None`` and
    every launch that picked it failed ``no-pane``. New panes follow the same
    pagination ``setup`` uses: fill the first ``workers*`` window holding fewer
    than ``[tmux].panes_per_window`` panes (re-tidied to ``layout``), else open the
    next ``workers-N`` window. ``windows`` is returned updated with any window
    opened here; ``failed`` lists slots tmux would not give a pane (the caller
    must keep anything from launching into them).
    """
    windows = dict(windows)
    worker_wins = sorted(
        ((n, w) for n, w in windows.items() if n == "workers" or n.startswith("workers-")),
        key=lambda nw: _worker_window_index(nw[0]),
    )
    per_window = max(1, cfg.tmux_panes_per_window)
    panes: dict[int, str] = {}
    failed: list[int] = []
    for sid in slot_ids:
        try:
            target = next(
                (w for _, w in worker_wins if 0 < len(tmux.list_panes(w)) < per_window),
                None,
            )
            if target is None:
                k = max((_worker_window_index(n) for n, _ in worker_wins), default=0) + 1
                name = "workers" if k == 1 else f"workers-{k}"
                target = tmux.new_window(cfg.session, name)
                windows[name] = target
                worker_wins.append((name, target))
                pane = tmux.list_panes(target)[0]
            else:
                pane = tmux.split_one(target)
                tmux.apply_layout(target, len(tmux.list_panes(target)), layout)
            tmux.set_slot(pane, sid)
        except (subprocess.CalledProcessError, IndexError, OSError):
            failed.append(sid)
            continue
        panes[sid] = pane
    return panes, windows, failed


def owns_session(cfg: Config, windows: dict[str, str]) -> bool:
    """Whether the tmux session named ``cfg.session`` is this run's.

    The name alone proves nothing: by default it is the project directory's
    name, which the owner may use for a session of their own. A session carries
    its swarm's state dir (:data:`tmux.OWNER_OPT`); one made before that marker
    existed is ours when a window or pane this run recorded (``windows``, from
    state) is in it."""
    owner = tmux.session_owner(cfg.session)
    if owner:
        return owner == str(cfg.state_dir)
    if owner is None:
        return False
    return any(tmux.window_session(w) == cfg.session for w in windows.values())


def teardown(cfg: Config) -> None:
    """Kill the swarm tmux session (idempotent). The caller has checked
    :func:`owns_session`."""
    if tmux.session_exists(cfg.session):
        tmux.kill_session(cfg.session)


#: Seconds :func:`reap_session` waits before it looks: the session's own last
#: command (``swarm done``, ``swarm operator-done``) gets to print its result first.
REAP_GRACE_S = 2.0

#: Seconds a signal gets to work before :func:`end_processes` sends the next,
#: harder one (SIGHUP, then SIGTERM, then SIGKILL).
END_WAIT_S = 5.0


def _descendants(table: dict[int, int], found: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, ppid in table.items():
        children.setdefault(ppid, []).append(pid)
    stack = list(found)
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _marked(cfg: Config, table: dict[int, int], markers: tuple[str, ...]) -> set[int]:
    """Pids whose environment carries this run's ``SWARM_STATE_DIR`` and, when
    ``markers`` are given, at least one of them (``KEY=VALUE``)."""
    run = f"SWARM_STATE_DIR={cfg.state_dir}".encode()
    want = {m.encode() for m in markers}
    found: set[int] = set()
    for pid in table:
        env = procs.environ(pid)
        if run not in env or (want and not want.intersection(env)):
            continue
        # A tmux server started by an `up` whose shell exported the variable
        # carries it too — and hosts the owner's other sessions.
        if not procs.comm(pid).startswith("tmux"):
            found.add(pid)
    return found


def session_processes(
    cfg: Config, roots: list[int] | tuple[int, ...] = (), markers: tuple[str, ...] = ()
) -> set[int]:
    """Every live process this run (or, with ``markers``, one session) spawned,
    whole trees included.

    Two ways in, because a pane is not a reliable owner: ``respawn-pane -k``
    (the operator's release, the master's kill) only hangs up on a session, and a
    ``claude`` that outlives its SIGHUP is left running with no pane at all —
    several of them survived ``swarm down``. So: every process whose
    environment carries this run's ``SWARM_STATE_DIR`` (each session gets it at
    spawn and hands it to everything it starts) — narrowed to one session by
    ``markers`` such as ``SWARM_SESSION_ID=worker:<phase>`` — plus the trees under
    ``roots`` (pane pids). A process that detached itself (``setsid``, ``nohup``,
    reparented to init) still carries the markers, so it is found too.

    Never: this process or its ancestors (a ``down`` typed from inside the session
    must not signal its own shell first), nor anything ``swarm keep`` holds, nor
    what that started — the one sanctioned way to outlive a session — nor a
    session a restart is carrying across (:func:`restart.carry_out`), which
    waits on the owner and must come out the other side alive.
    """
    table = procs.table()
    found = {p for p in roots if p in table} | _marked(cfg, table, markers)
    found = _descendants(table, found)
    kept = _descendants(table, keep_mod.live_pids(cfg) & set(table))
    found -= kept
    carried = restart_mod.kept_markers(cfg)
    if carried:
        found -= _descendants(
            table, _marked(cfg, table, carried) | (restart_mod.kept_roots(cfg) & set(table)))
    pid = os.getpid()
    while pid > 1:
        found.discard(pid)
        pid = table.get(pid, 0)
    return found


def session_alive(cfg: Config, markers: tuple[str, ...]) -> set[int]:
    """Every live process of the one session ``markers`` name: is it still there?

    The question :func:`session_processes` cannot answer. That one lists what
    may be ended, so it leaves out the caller and its ancestors and every
    session a restart carries; asked from inside the session, or of a carried
    one, it finds nothing of a session that is plainly alive. Here only what
    ``swarm keep`` holds is left out: a kept process is meant to outlive its
    session, so it does not show that the session is still there.
    """
    table = procs.table()
    found = _descendants(table, _marked(cfg, table, markers))
    return found - _descendants(table, keep_mod.live_pids(cfg) & set(table))


def end_processes(
    cfg: Config, pids: set[int], wait: float | None = None, markers: tuple[str, ...] = ()
) -> tuple[int, list[int]]:
    """End ``pids`` and anything the run (or the ``markers`` session) spawns
    meanwhile; confirm they are gone.

    SIGHUP, then SIGTERM, then SIGKILL, each only to what outlived the one
    before — to the process and, where it leads one, its whole process group.
    Returns ``(how many were ended, the pids still alive after SIGKILL)``.
    """
    wait = END_WAIT_S if wait is None else wait
    own_group = os.getpgrp()
    ended: set[int] = set()
    alive = set(pids)
    for sig, grace in ((signal.SIGHUP, wait), (signal.SIGTERM, wait), (signal.SIGKILL, 2.0)):
        spare = keep_mod.live_pids(cfg)
        alive = {p for p in alive | session_processes(cfg, markers=markers)
                 if procs.alive(p) and p not in spare}
        ended |= alive
        if not alive:
            break
        for pid in alive:
            try:
                if os.getpgid(pid) == pid and pid != own_group:
                    os.killpg(pid, sig)
                os.kill(pid, sig)
            except OSError:
                continue  # gone already
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and any(procs.alive(p) for p in alive):
            time.sleep(0.1)
    return len(ended), sorted(p for p in alive if procs.alive(p))


def session_markers(cfg: Config, kind: str, ident: str) -> tuple[str, ...]:
    """What marks one session's processes: ``SWARM_SESSION_ID=<kind>:<ident>``, and
    for a worker also its phase marker (``SWARM_PHASE=<phase>``)."""
    markers = (f"{procs.SESSION_ENV}={kind}:{ident}",)
    if kind == "worker":
        markers += (f"{cfg.env_marker}={ident}",)
    return markers


def reap_session(
    cfg: Config, kind: str, ident: str, log=None, *, wait: float | None = None,
    grace: float | None = None, background: bool = True,
) -> threading.Thread | None:
    """End every process one session started: the session is over.

    ``kind`` is ``worker``, ``operator``, ``overseer`` or ``resolver``. Matches the
    session's markers (so a ``setsid``/``nohup`` child that left the pane's tree
    is found too) within this run, and escalates HUP → TERM → KILL like
    ``swarm down``. ``swarm keep`` processes are spared. Off the caller's thread
    by default: a stubborn process costs up to ``2 * END_WAIT_S + 2`` seconds, and
    the supervisor's loop must not wait on it. It looks only after ``grace``
    (default :data:`REAP_GRACE_S`).
    """
    markers = session_markers(cfg, kind, ident)
    grace = REAP_GRACE_S if grace is None else grace

    def run() -> None:
        try:
            if grace > 0:
                time.sleep(grace)
            found = session_processes(cfg, markers=markers)
            if not found:
                return
            ended, left = end_processes(cfg, found, wait=wait, markers=markers)
            if log is not None:
                log.line(f"REAP {kind}:{ident} ended={ended}"
                         + (f" survived={' '.join(map(str, left))}" if left else ""))
        except Exception as exc:  # noqa: BLE001 - a reaper must report, not vanish
            if log is not None:
                log.line(f"REAP-ERROR {kind}:{ident} {exc!r}")

    if not background:
        run()
        return None
    thread = threading.Thread(target=run, name=f"reap:{kind}:{ident}", daemon=True)
    with _REAPS_LOCK:
        _REAPS[:] = [t for t in _REAPS if t.is_alive()]
        _REAPS.append(thread)
    thread.start()
    return thread


#: Reaps still running in this process: a daemon thread dies with its process,
#: so the supervisor joins them before it exits (the last phase's ``done`` is
#: often what finishes the run).
_REAPS: list[threading.Thread] = []
_REAPS_LOCK = threading.Lock()


def join_reaps(timeout: float | None = None) -> None:
    """Wait for every reap this process started (at most ``timeout`` seconds)."""
    if timeout is None:
        timeout = REAP_GRACE_S + 2 * END_WAIT_S + 5.0
    deadline = time.monotonic() + timeout
    with _REAPS_LOCK:
        pending = list(_REAPS)
    for thread in pending:
        thread.join(max(0.0, deadline - time.monotonic()))
