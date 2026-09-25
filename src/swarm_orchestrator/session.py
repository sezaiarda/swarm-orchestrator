"""Bring up / tear down the ``swarm`` tmux topology.

Layout: window 0 ``dash`` (the always-on TUI dashboard), window 1 ``overseer``
(the master pane: the init pass, then Overseer passes — the supervisor process
itself is detached and has no tty), window 2 ``operator`` (idle until a hand-off opens a session in it), then
one or more worker windows tagged
``@swarm_slot 0..N-1`` across a GLOBAL slot index. Slots paginate into windows of
at most :data:`PANES_PER_WINDOW` (``workers``, ``workers-2``, …), each laid out by
:func:`swarm_orchestrator.tmux.split_layout` per ``[tmux].layout`` (``auto`` = 1
full pane / 2 LEFT|RIGHT / 3-4 tiled; or a pinned preset such as
``even-vertical`` for a TOP/BOTTOM stack). Windows are referenced by captured id everywhere downstream; slot pane ids
are recorded in state so accounting is tag-driven (workers run their own teammates
in-process, so no teammate panes ever appear). Last, when ``[web] enabled``, a
``web`` window serving the LAN board (:mod:`swarm_orchestrator.web`).
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import time
from pathlib import Path

from . import state as state_mod
from . import tmux
from .config import Config
from .web import lifecycle as web_lifecycle

PANES_PER_WINDOW = 4


def plan_worker_windows(total: int) -> list[tuple[str, int]]:
    """Split ``total`` worker slots into windows of at most ``PANES_PER_WINDOW``.

    Returns ``[(window_name, pane_count), …]``: the first window is ``workers`` and
    the rest ``workers-2``, ``workers-3``, … each holding ``min(4, remaining)``
    panes. ``total <= 0`` yields no windows. Pure — no tmux side effects.
    """
    plan: list[tuple[str, int]] = []
    remaining = total
    k = 0
    while remaining > 0:
        size = min(PANES_PER_WINDOW, remaining)
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
    tmux.harden(cfg.session)
    tmux.rename_window(dash_win, "dash")
    dash_pane = tmux.list_panes(dash_win)[0]

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
    slot_panes: dict[int, str] = {}
    base = 0
    for name, size in plan_worker_windows(cfg.max_workers):
        win = tmux.new_window(cfg.session, name)
        for offset, pane in enumerate(tmux.split_layout(win, size, cfg.tmux_layout)):
            gidx = base + offset
            tmux.set_slot(pane, gidx)
            slot_panes[gidx] = pane
        windows[name] = win
        base += size

    if cfg.web_enabled:
        # The LAN board, in the LAST window so every index the owner's fingers
        # know (0 dash … 3 workers) stays where it was; inside the session so it
        # lives and dies with it — `swarm down` kills it with every other window.
        web_win = tmux.new_window(cfg.session, "web")
        windows["web"] = web_win
        tmux.respawn_pane(
            tmux.list_panes(web_win)[0],
            f"cd {shlex.quote(str(cfg.project_dir))} && exec {web_lifecycle.command(cfg)}",
            env={"SWARM_STATE_DIR": str(cfg.state_dir)},
        )

    if cfg.tui_autostart:
        # cd first: tmux.new_session takes no -c, so window 0 inherits whatever
        # cwd `swarm up` ran from. The dashboard resolves its project from cwd,
        # so under `swarm up --project-dir /elsewhere` it would read the wrong
        # .swarm.toml and the wrong ledger -- SWARM_STATE_DIR pins the state dir
        # but says nothing about which project it belongs to.
        tmux.respawn_pane(
            dash_pane,
            f"cd {shlex.quote(str(cfg.project_dir))} && exec {cfg.tui_cmd}",
            env={"SWARM_STATE_DIR": str(cfg.state_dir)},
        )

    with state_mod.transaction(cfg) as st:
        st.windows = windows
        st.master_pane = master_pane
        st.dash_pane = dash_pane
        st.operator_pane = operator_pane
        for gidx, pane in slot_panes.items():
            if gidx < len(st.slots):
                st.slots[gidx].pane_id = pane
    return windows


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
    than :data:`PANES_PER_WINDOW` panes (re-tidied to ``layout``), else open the
    next ``workers-N`` window. ``windows`` is returned updated with any window
    opened here; ``failed`` lists slots tmux would not give a pane (the caller
    must keep anything from launching into them).
    """
    windows = dict(windows)
    worker_wins = sorted(
        ((n, w) for n, w in windows.items() if n == "workers" or n.startswith("workers-")),
        key=lambda nw: _worker_window_index(nw[0]),
    )
    panes: dict[int, str] = {}
    failed: list[int] = []
    for sid in slot_ids:
        try:
            target = next(
                (w for _, w in worker_wins if 0 < len(tmux.list_panes(w)) < PANES_PER_WINDOW),
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


def teardown(cfg: Config) -> None:
    """Kill the swarm tmux session (idempotent)."""
    if tmux.session_exists(cfg.session):
        tmux.kill_session(cfg.session)


#: Seconds a signal gets to work before :func:`end_processes` sends the next,
#: harder one (SIGHUP, then SIGTERM, then SIGKILL).
END_WAIT_S = 5.0


def _proc_table() -> dict[int, int]:
    """``{pid: ppid}`` for every live process (zombies are already gone)."""
    table: dict[int, int] = {}
    try:
        entries = [e for e in Path("/proc").iterdir() if e.name.isdigit()]
    except OSError:
        return table
    for entry in entries:
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        rest = stat[stat.rfind(")") + 2 :].split()  # the name may hold spaces
        if len(rest) > 1 and rest[0] not in ("Z", "X"):
            table[int(entry.name)] = int(rest[1])
    return table


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2 :][:1] not in ("Z", "X")


def session_processes(cfg: Config, roots: list[int] | tuple[int, ...] = ()) -> set[int]:
    """Every live process this run spawned, whole trees included.

    Two ways in, because a pane is not a reliable owner: ``respawn-pane -k``
    (the operator's release, the master's kill) only hangs up on a session, and a
    ``claude`` that outlives its SIGHUP is left running with no pane at all —
    several of them survived ``swarm down``. So: every process whose
    environment carries this run's ``SWARM_STATE_DIR`` (each session gets it at
    spawn and hands it to everything it starts), plus the trees under ``roots``
    (the session's pane pids). Never this process or its ancestors: a ``down``
    typed from inside the session must not signal its own shell first.
    """
    table = _proc_table()
    marker = f"SWARM_STATE_DIR={cfg.state_dir}".encode()
    found = {p for p in roots if p in table}
    for pid in table:
        try:
            if marker not in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                continue
            # A tmux server started by an `up` whose shell exported the variable
            # carries it too — and hosts the owner's other sessions.
            if not Path(f"/proc/{pid}/comm").read_text().startswith("tmux"):
                found.add(pid)
        except OSError:
            continue  # exited mid-scan, or not ours to read
    children: dict[int, list[int]] = {}
    for pid, ppid in table.items():
        children.setdefault(ppid, []).append(pid)
    stack = list(found)
    while stack:
        for child in children.get(stack.pop(), ()):
            if child not in found:
                found.add(child)
                stack.append(child)
    pid = os.getpid()
    while pid > 1:
        found.discard(pid)
        pid = table.get(pid, 0)
    return found


def end_processes(
    cfg: Config, pids: set[int], wait: float | None = None
) -> tuple[int, list[int]]:
    """End ``pids`` and anything the run spawns meanwhile; confirm they are gone.

    SIGHUP, then SIGTERM, then SIGKILL, each only to what outlived the one
    before — to the process and, where it leads one, its whole process group.
    Returns ``(how many were ended, the pids still alive after SIGKILL)``.
    """
    wait = END_WAIT_S if wait is None else wait
    own_group = os.getpgrp()
    ended: set[int] = set()
    alive = set(pids)
    for sig, grace in ((signal.SIGHUP, wait), (signal.SIGTERM, wait), (signal.SIGKILL, 2.0)):
        alive = {p for p in alive | session_processes(cfg) if _alive(p)}
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
        while time.monotonic() < deadline and any(_alive(p) for p in alive):
            time.sleep(0.1)
    return len(ended), sorted(p for p in alive if _alive(p))
