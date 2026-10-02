"""The dashboard lists every parked worker the way it lists a slot's.

A parked worker is in a tmux window of its own (``wait:<phase>``) and in no
slot, whether it still asks the owner or works on the answer. The Workers tab
and home's working now box list it after the slots, read from the pane in that
window as a slot's worker is read from its pane: what ``claude agents`` says of
it (``waiting`` while it asks), its age from its launch, its context, its
branch and the git counts of its worktree, and, in the detail, its pane, its
worktree, its branch work and the last lines in its pane. What is at work for
the campaign and the forecast (``Dash.working_parked``) is unchanged.

Every test reads one real state file through a real ``Dash`` and one real
``Dash.probe``, in the shapes of ``tests/test_parked_working_readers.py``, with
the run under ``isolation = worktree`` and the probes answering for P1's pane.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from textual.app import App

from swarm_orchestrator import state as state_mod
from swarm_orchestrator.state import State
from swarm_orchestrator.tui import home, probes, tables
from swarm_orchestrator.tui.campaign import at_work

from test_parked_gone_dashboard import session_of, workers_tab
from test_parked_working_dashboard import aged, col, dash_of
from test_parked_working_readers import (  # noqa: F401 - ``cfg`` is the fixture
    ANSWERED,
    ASKED,
    LAUNCHED,
    PARK_AFTER,
    STILL_ASKING,
    WORKING,
    cfg,
    run,
)
from test_tui_home import plain

WINDOW = "wait:P1"
PANE = "%9"
#: The Workers rows of the fixture's run: P0 in slot 0, slot 1 free, then P1.
PARKED_ROW = 2


def probed(cfg, monkeypatch, status: str = "busy", waiting_for: str = ""):
    """A real dash over ``cfg`` under ``isolation = worktree``, polled and
    probed once, P1's session alive: tmux has P1's window with pane ``%9`` in
    it, claude says ``status`` of the session in P1's worktree, and P1's branch
    is three commits ahead with two dirty files."""
    monkeypatch.setattr(cfg, "git_isolation", "worktree")
    with state_mod.transaction(cfg) as st:
        st.windows[WINDOW] = "@7"
    dash, _ = dash_of(cfg, monkeypatch)
    agent = probes.AgentInfo(cwd=str(cfg.wt_dir / "P1"), status=status, waiting_for=waiting_for)
    monkeypatch.setattr(probes, "agents", lambda: [agent])
    monkeypatch.setattr(probes, "panes", lambda: {PANE: probes.PaneInfo(
        PANE, window_id="@7", window_name=WINDOW)})
    monkeypatch.setattr(probes, "capture", lambda pane: (
        "reading the parser\nrunning the tests\n" if pane == PANE else ""))
    monkeypatch.setattr(probes, "repo_stat", lambda worktree, main: (
        probes.RepoStat(3, 2) if worktree == str(cfg.wt_dir / "P1") else probes.RepoStat(0, 0)))
    with session_of(cfg):
        dash.probe()
    return dash


# -- the Workers tab ------------------------------------------------------------------
def test_a_parked_worker_at_work_is_a_row_like_a_slots_worker(cfg, monkeypatch):
    run(cfg, WORKING)
    tab = workers_tab(probed(cfg, monkeypatch))
    row = tab["cells"][PARKED_ROW]
    assert col(row, "slot").strip() == "—" and col(row, "phase").strip() == "P1"
    assert col(row, "live").strip() == "busy"
    assert col(row, "+").strip() == "3" and col(row, "~").strip() == "2"
    assert col(row, "branch").strip() == "swarm/P1"
    assert "1 in own window" in tab["head"]


def test_the_detail_of_a_parked_worker_has_what_a_slots_has(cfg, monkeypatch):
    run(cfg, WORKING)
    detail = workers_tab(probed(cfg, monkeypatch))["detail"]
    for line in (f"pane            {PANE}", f"window          {WINDOW}",
                 "branch          swarm/P1", f"worktree        {cfg.wt_dir / 'P1'}",
                 "branch work     3 commit(s), 2 dirty file(s)", "last lines in its pane",
                 "running the tests"):
        assert line in detail
    assert f"works on your answer in tmux window {WINDOW}" in detail and "no slot" in detail


@STILL_ASKING
def test_a_parked_worker_that_asks_reads_waiting_with_its_question(cfg, monkeypatch, mark):
    """It asks by the state, whatever claude says it does meanwhile."""
    run(cfg, mark)
    dash = probed(cfg, monkeypatch, status="busy", waiting_for="which codec?")
    tab = workers_tab(dash)
    assert col(tab["cells"][PARKED_ROW], "live").strip() == "waiting"
    assert "1 waiting on you" in tab["head"]
    assert "waiting for     which codec?" in tab["detail"]
    text = plain(home.worker_rows(dash, 70)[-1][0])
    assert WINDOW in text and "which codec?" in text and "waiting" in text


def test_a_parked_worker_is_aged_from_its_launch_when_a_restart_closed_its_run(
        cfg, monkeypatch):
    """A supervisor that came up since closes the run in History; the launch
    in the log still says when the worker started, as it does for a slot's."""
    run(cfg, WORKING)
    stamp = datetime.fromtimestamp(LAUNCHED + 60).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with cfg.supervisor_log.open("a", encoding="utf-8") as log:
        log.write(f"{stamp} 2.000 SUPERVISOR-START pid=1\n")
    dash, _ = dash_of(cfg, monkeypatch)
    assert not any(r.running for r in dash.history if r.phase == "P1")
    (worker,) = dash.parked_workers
    assert worker.started_at == LAUNCHED
    row, ages = aged(lambda: tables.worker_row(dash.parked_rows()[0]))
    assert col(row, "elapsed").strip() in ages


# -- what is at work is unchanged -------------------------------------------------------
@STILL_ASKING
def test_a_parked_worker_that_asks_is_not_at_work(cfg, monkeypatch, mark):
    run(cfg, mark)
    dash = probed(cfg, monkeypatch)
    assert [w.phase for w in dash.parked_workers] == ["P1"]
    assert dash.working_parked == [] and at_work(dash) == {"P0"}


def test_a_parked_worker_the_owner_answered_is_at_work(cfg, monkeypatch):
    run(cfg, WORKING)
    dash = probed(cfg, monkeypatch)
    assert [w.phase for w in dash.working_parked] == ["P1"] and at_work(dash) == {"P0", "P1"}


# -- home: working now ------------------------------------------------------------------
def test_working_now_shows_every_parked_worker_under_four_slots():
    """Four slots fill the box; each parked worker is a row more, and the books
    give up a row for it. Past four slots the slots scroll, as before."""
    assert home.grid_layout(160, 50, 4)["work"] == home.WORK_VIS
    cut = home.grid_layout(160, 50, 4, 2)
    assert cut["work"] == 6 and cut["books"] == home.TALL_BOOKS - 2
    assert home.grid_layout(160, 50, 6, 2)["work"] == 6
    assert home.grid_layout(160, 50, 2, 1)["work"] == home.WORK_VIS


def four_slots_and_a_parked_worker(cfg) -> None:
    """P0, A, B and C in four slots; P1 parked, and at work on the answer."""
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(State.fresh(4).__dict__)
        for phase in ("P0", "A", "B", "P1"):
            st.claim_slot(phase)
        st.waiting["P1"] = ASKED + PARK_AFTER
        st.park("P1", PARK_AFTER)
        st.answer("P1", ANSWERED)
        st.claim_slot("C")  # in the slot P1 left
    st = state_mod.read(cfg)
    assert all(slot.busy for slot in st.slots) and st.working_parked() == ["P1"]


def test_working_now_is_not_cut_off_by_a_parked_worker_after_four_slots(cfg, monkeypatch):
    four_slots_and_a_parked_worker(cfg)
    dash, _ = dash_of(cfg, monkeypatch)
    seen = {}

    class Host(App):
        def compose(self):
            yield home.Home(id="tab-home")

    app = Host()

    async def steps():
        async with app.run_test(size=(160, 50)) as pilot:
            screen = app.query_one(home.Home)
            screen.update(dash)
            await pilot.pause()
            seen["work"] = [plain(r._swarm_text) for r in app.query("#work-rows Row")
                            if r.display]
            seen["hint"] = str(app.query_one("#p-work").border_subtitle)

    asyncio.run(asyncio.wait_for(steps(), timeout=30))
    assert len(seen["work"]) == 5 and WINDOW in seen["work"][-1]
    assert "more" not in seen["hint"]
