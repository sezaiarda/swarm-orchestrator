"""Bring up / tear down the ``swarm`` tmux topology.

Layout: a ``master`` window (1 idle pane), a ``workers`` window split into
``max_workers`` tiled panes each tagged ``@swarm_slot 0..N-1``, and a
``teammates`` parking window. Windows are referenced by captured id everywhere
downstream; slot pane ids are recorded in state so accounting is tag-driven.
"""

from __future__ import annotations

from . import state as state_mod
from . import tmux
from .config import Config


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

    workers_win = tmux.new_window(cfg.session, "workers")
    slot_panes = tmux.split_tiled(workers_win, cfg.max_workers)
    for i, pane in enumerate(slot_panes):
        tmux.set_slot(pane, i)

    teammates_win = tmux.new_window(cfg.session, "teammates")

    windows = {
        "master": master_win,
        "workers": workers_win,
        "teammates": teammates_win,
    }
    with state_mod.transaction(cfg) as st:
        st.windows = windows
        st.master_pane = master_pane
        for i, pane in enumerate(slot_panes):
            if i < len(st.slots):
                st.slots[i].pane_id = pane
    return windows


def teardown(cfg: Config) -> None:
    """Kill the swarm tmux session (idempotent)."""
    if tmux.session_exists(cfg.session):
        tmux.kill_session(cfg.session)
