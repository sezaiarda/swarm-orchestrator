"""Bring up / tear down the ``swarm`` tmux topology.

Layout: window 0 ``dash`` (the always-on TUI dashboard), window 1 ``supervisor``
(the LLM master's pane — the supervisor process itself is detached and has no
tty), window 2 ``operator`` (idle until a hand-off opens a session in it), then
one or more worker windows tagged
``@swarm_slot 0..N-1`` across a GLOBAL slot index. Slots paginate into windows of
at most :data:`PANES_PER_WINDOW` (``workers``, ``workers-2``, …), each laid out by
:func:`swarm_orchestrator.tmux.split_layout` per ``[tmux].layout`` (``auto`` = 1
full pane / 2 LEFT|RIGHT / 3-4 tiled; or a pinned preset such as
``even-vertical`` for a TOP/BOTTOM stack). Windows are referenced by captured id everywhere downstream; slot pane ids
are recorded in state so accounting is tag-driven (workers run their own teammates
in-process, so no teammate panes ever appear).
"""

from __future__ import annotations

import shlex

from . import state as state_mod
from . import tmux
from .config import Config

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

    master_win = tmux.new_window(cfg.session, "supervisor")
    master_pane = tmux.list_panes(master_win)[0]

    # The operator's window, created here rather than on demand so it sits beside
    # the master instead of appearing mid-run. It holds `sleep infinity` until the
    # supervisor opens a session in it. Safe from `swarm layout` (which only
    # matches `workers*`) and from slot accounting (never `@swarm_slot`-tagged).
    operator_win = tmux.new_window(cfg.session, "operator")
    operator_pane = tmux.list_panes(operator_win)[0]

    # "master" is the durable key every downstream consumer already uses for the
    # LLM master's window; the *display* name is "supervisor" because that is
    # what the owner reads in the status bar. Renaming the key would invalidate
    # st.windows for any run mid-flight.
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


def teardown(cfg: Config) -> None:
    """Kill the swarm tmux session (idempotent)."""
    if tmux.session_exists(cfg.session):
        tmux.kill_session(cfg.session)
