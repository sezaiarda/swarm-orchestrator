"""Bring up / tear down the ``swarm`` tmux topology.

Layout: a ``master`` window (1 idle pane) plus one or more worker windows tagged
``@swarm_slot 0..N-1`` across a GLOBAL slot index. Slots paginate into windows of
at most :data:`PANES_PER_WINDOW` (``workers``, ``workers-2``, …), each laid out by
:func:`swarm_orchestrator.tmux.split_layout` (1 full pane / 2 LEFT|RIGHT / 3-4
tiled). Windows are referenced by captured id everywhere downstream; slot pane ids
are recorded in state so accounting is tag-driven (workers run their own teammates
in-process, so no teammate panes ever appear).
"""

from __future__ import annotations

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
    master_win = tmux.new_session(cfg.session)
    tmux.harden(cfg.session)
    tmux.rename_window(master_win, "master")
    master_pane = tmux.list_panes(master_win)[0]

    windows = {"master": master_win}
    slot_panes: dict[int, str] = {}
    base = 0
    for name, size in plan_worker_windows(cfg.max_workers):
        win = tmux.new_window(cfg.session, name)
        for offset, pane in enumerate(tmux.split_layout(win, size)):
            gidx = base + offset
            tmux.set_slot(pane, gidx)
            slot_panes[gidx] = pane
        windows[name] = win
        base += size

    with state_mod.transaction(cfg) as st:
        st.windows = windows
        st.master_pane = master_pane
        for gidx, pane in slot_panes.items():
            if gidx < len(st.slots):
                st.slots[gidx].pane_id = pane
    return windows


def teardown(cfg: Config) -> None:
    """Kill the swarm tmux session (idempotent)."""
    if tmux.session_exists(cfg.session):
        tmux.kill_session(cfg.session)
