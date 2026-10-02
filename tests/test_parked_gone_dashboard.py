"""The dashboard says so when a parked worker at work is gone.

A parked worker the owner has answered works in a tmux window of its own and in
no slot, so no slot probe looks at it. One whose session died, or whose window
was closed by hand, stayed on the Workers tab as a healthy row reading
``in window``. These tests hold the tab, its detail, its border and the History
detail to what the dashboard's probe finds of that session, by the rule
``swarm doctor`` and the supervisor's sweep share (``doctor.parked_probe``).

Every test reads one real state file through a real ``Dash`` and one real
``Dash.probe``, in the shapes of ``tests/test_parked_working_readers.py``. A
worker whose session is gone is the case. One whose session still runs, and one
tmux gave no answer about, are the control: each reads as a worker at work in
its window, as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess

import pytest
from textual.app import App

from swarm_orchestrator import doctor, procs
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import tmux as tmux_mod
from swarm_orchestrator.tui import home, probes, tables
from swarm_orchestrator.tui.theme import BAD, COLOR

from test_parked_working_dashboard import col, dash_of
from test_parked_working_readers import (  # noqa: F401 - ``cfg`` is the fixture
    STILL_ASKING,
    WORKING,
    cfg,
    run,
)
from test_tui_home import plain

WINDOW = "wait:P1"
NO_PROCESS = "no process of its session is left"
#: The Workers rows of the fixture's run: P0 in slot 0, slot 1 free, then P1.
SLOT_ROW, PARKED_ROW = 0, 2


def probe(dash, monkeypatch) -> None:
    """One real ``Dash.probe``. The slot probes (claude, the panes, git) answer
    empty, so only the look at the parked sessions touches the machine."""
    monkeypatch.setattr(probes, "agents", lambda: [])
    monkeypatch.setattr(probes, "panes", lambda: {})
    monkeypatch.setattr(probes, "capture", lambda pane: "")
    monkeypatch.setattr(probes, "repo_stat", lambda worktree, main: probes.RepoStat(0, 0))
    dash.probe()


def probed(cfg, monkeypatch):
    """A real dash over ``cfg``, polled and probed once."""
    dash, _ = dash_of(cfg, monkeypatch)
    probe(dash, monkeypatch)
    return dash


@contextlib.contextmanager
def session_of(cfg, phase: str = "P1"):
    """A live process of ``phase``'s worker session, ended on the way out
    whatever the test did."""
    child = subprocess.Popen(
        ["sleep", "60"],
        env={"PATH": os.environ["PATH"], "SWARM_STATE_DIR": str(cfg.state_dir),
             "SWARM_SESSION_ID": f"worker:{phase}"})
    try:
        yield child
    finally:
        child.kill()
        child.wait()


def in_tmux(cfg, monkeypatch, panes: str, returncode: int = 0) -> None:
    """The run is under tmux, P1's window is ``@7``, and tmux answers
    ``list-panes`` with ``panes`` (``<window_id> <pane_dead>`` a line)."""
    monkeypatch.setattr(cfg, "driver", "tmux")
    with state_mod.transaction(cfg) as st:
        st.windows[WINDOW] = "@7"

    def answer(args, check=False, input_text=None):
        if args and args[0] == "list-panes":
            return subprocess.CompletedProcess(args, returncode, panes, "")
        return subprocess.CompletedProcess(args, 1, "", "no such command here")

    monkeypatch.setattr(tmux_mod, "run", answer)


def workers_tab(dash, visit=(PARKED_ROW,)) -> dict:
    """The Workers tab alone, painted from ``dash``, as the screen shows it.

    The cursor goes to each row of ``visit`` in turn with no update in between,
    and the detail border's title is read at each: ``titles``. The rest is read
    with the cursor on the last of them.
    """
    seen = {"titles": [], "opened": []}

    class Host(App):
        def compose(self):
            yield tables.Workers()

        def on_open_detail(self, event: tables.OpenDetail) -> None:
            seen["opened"].append(event.title)

    app = Host()

    async def steps():
        async with app.run_test(size=(160, 30)) as pilot:
            tab = app.query_one(tables.Workers)
            tab.update(dash)
            await pilot.pause()
            for row in visit:
                tab.table.move_cursor(row=row)
                await pilot.pause()
                seen["titles"].append(plain(str(tab.detail_panel.border_title)))
            tab.action_open_detail()
            await pilot.pause()
            table = tab.table
            seen.update(
                cells=[tuple(str(c) for c in table.get_row_at(i)) for i in range(table.row_count)],
                keys=list(tab._keys),
                head=plain(tab.query_one(".tab-head")._swarm_text),
                detail=plain(tab.detail_text(dash)),
                bad=tab.detail_panel.has_class("-bad"),
            )

    asyncio.run(asyncio.wait_for(steps(), timeout=30))
    assert all(len(row) == len(tables.WORKER_COLUMNS) for row in seen["cells"])
    return seen


def history_detail(dash) -> str:
    """The History detail of P1's run, as the screen shows it."""
    p1 = next(r for r in dash.history if r.phase == "P1")
    return plain(tables.history_detail(p1, dash))


def reads_in_window(tab) -> None:
    """P1's row, the head and the detail read as they did before any probe:
    no claude here, so ``unknown``, as a slot's worker would."""
    row = tab["cells"][PARKED_ROW]
    assert col(row, "live").strip() == "unknown"
    assert col(row, "phase").strip() == "P1" and COLOR[BAD] not in row[2]
    assert "1/2 slots busy" in tab["head"] and "1 in own window" in tab["head"]
    assert "GONE" not in tab["head"] and not tab["bad"]
    assert f"works on your answer in tmux window {WINDOW}" in tab["detail"]
    assert "no slot" in tab["detail"] and "GONE" not in tab["detail"]


def reads_gone(tab, missing: str) -> None:
    """P1's row says gone with a gone slot's three tells, the head counts it
    apart from the workers in a window, and the detail says what is missing."""
    marker, _, phase, live = tab["cells"][PARKED_ROW][:4]
    assert plain(marker).strip() == "✖" and plain(live).strip() == "GONE"
    assert plain(phase).strip() == "P1" and COLOR[BAD] in phase
    assert f"window          {WINDOW}" in tab["detail"]  # where it was
    assert "1/2 slots busy" in tab["head"] and "1 PARKED WORKER GONE" in tab["head"]
    assert "in own window" not in tab["head"]
    assert missing in tab["detail"] and "GONE" in tab["detail"]
    assert "works on your answer" not in tab["detail"]
    assert tab["bad"]


# -- the session's processes --------------------------------------------------------
def test_a_parked_worker_whose_session_died_reads_gone(cfg, monkeypatch):
    run(cfg, WORKING)
    dash = probed(cfg, monkeypatch)
    reads_gone(workers_tab(dash), NO_PROCESS)


def test_a_parked_worker_whose_session_runs_reads_in_window_as_before(cfg, monkeypatch):
    run(cfg, WORKING)
    with session_of(cfg):
        tab = workers_tab(probed(cfg, monkeypatch))
    reads_in_window(tab)


def test_the_detail_of_a_gone_parked_worker_repeats_why_nothing_settles_it(cfg, monkeypatch):
    """No supervisor runs here, so nothing settles it, and the detail says the
    reason the shared rule gave and promises no sweep."""
    run(cfg, WORKING)
    detail = workers_tab(probed(cfg, monkeypatch))["detail"]
    (gone,), _ = doctor.parked_probe(cfg, state_mod.read(cfg))
    kept = gone.kept
    assert kept and kept in detail
    assert "second sighting" not in detail


def test_the_detail_of_a_gone_parked_worker_says_the_sweep_settles_it(cfg, monkeypatch):
    """What the probe finds under a supervisor whose sweep knows the rule."""
    run(cfg, WORKING)
    dash, _ = dash_of(cfg, monkeypatch)
    dash.parked_gone = {"P1": doctor.Gone("P1", f"the worker on P1 ({NO_PROCESS})", None)}
    detail = workers_tab(dash)["detail"]
    assert NO_PROCESS in detail and "works on your answer" not in detail
    assert "second sighting" in detail and "its work is kept" in detail
    assert "started again" in detail and "Nothing settles it" not in detail


@STILL_ASKING
def test_a_parked_worker_that_asks_and_is_gone_reads_gone_not_waiting(cfg, monkeypatch, mark):
    run(cfg, mark)
    tab = workers_tab(probed(cfg, monkeypatch))
    assert tab["keys"] == ["slot-0", "slot-1", "parked-P1"]
    assert col(tab["cells"][PARKED_ROW], "live").strip() == "GONE"
    assert "1 PARKED WORKER GONE" in tab["head"] and "waiting on you" not in tab["head"]
    assert "counts P1 as asking you" in tab["detail"] and tab["bad"]


@STILL_ASKING
def test_a_parked_worker_that_asks_and_is_there_reads_waiting(cfg, monkeypatch, mark):
    run(cfg, mark)
    with session_of(cfg):
        tab = workers_tab(probed(cfg, monkeypatch))
    assert col(tab["cells"][PARKED_ROW], "live").strip() == "waiting"
    assert "1 waiting on you" in tab["head"] and "GONE" not in tab["head"]
    assert f"waits on your answer in tmux window {WINDOW}" in tab["detail"]
    assert tab["titles"] == ["parked worker"] and not tab["bad"]


# -- under tmux: the window says ----------------------------------------------------
@pytest.mark.parametrize("panes, missing", [
    ("@1 0\n@7 1\n", f"nothing runs in its window {WINDOW}"),
    ("@1 0\n", f"its window {WINDOW} is gone"),
])
def test_a_parked_worker_whose_window_is_dead_or_closed_reads_gone(
        cfg, monkeypatch, panes, missing):
    """The window decides: a process of the session that is still there does not
    make a closed window a worker at work."""
    run(cfg, WORKING)
    in_tmux(cfg, monkeypatch, panes)
    with session_of(cfg):
        dash = probed(cfg, monkeypatch)
        tab = workers_tab(dash)
    reads_gone(tab, missing)


def test_a_parked_worker_in_a_live_window_reads_in_window_as_before(cfg, monkeypatch):
    run(cfg, WORKING)
    in_tmux(cfg, monkeypatch, "@1 0\n@7 0\n")
    with session_of(cfg):
        tab = workers_tab(probed(cfg, monkeypatch))
    reads_in_window(tab)


def test_a_live_window_with_no_process_of_the_session_reads_gone(cfg, monkeypatch):
    run(cfg, WORKING)
    in_tmux(cfg, monkeypatch, "@7 0\n")
    reads_gone(workers_tab(probed(cfg, monkeypatch)), NO_PROCESS)


def test_no_answer_from_tmux_is_not_called_gone(cfg, monkeypatch):
    """Nothing is known, so nothing is said: not even of a session with no
    process left."""
    run(cfg, WORKING)
    in_tmux(cfg, monkeypatch, "", returncode=1)
    reads_in_window(workers_tab(probed(cfg, monkeypatch)))


# -- the border of the detail ---------------------------------------------------------
def test_the_border_does_not_call_a_parked_worker_a_slot(cfg, monkeypatch):
    run(cfg, WORKING)
    with session_of(cfg):
        tab = workers_tab(probed(cfg, monkeypatch))
    assert tab["titles"] == ["parked worker"]
    assert tab["opened"] == ["parked worker"]  # the same detail, opened in full


def test_the_border_of_a_slot_row_is_unchanged(cfg, monkeypatch):
    run(cfg, WORKING)
    with session_of(cfg):
        tab = workers_tab(probed(cfg, monkeypatch), visit=(SLOT_ROW,))
    assert tab["titles"] == ["slot"] and tab["opened"] == ["slot"]
    assert not tab["bad"]


def test_the_border_follows_the_cursor(cfg, monkeypatch):
    run(cfg, WORKING)
    with session_of(cfg):
        tab = workers_tab(probed(cfg, monkeypatch), visit=(SLOT_ROW, PARKED_ROW, SLOT_ROW))
    assert tab["titles"] == ["slot", "parked worker", "slot"]


def test_the_border_counts_a_gone_parked_worker_by_name(cfg, monkeypatch):
    """A slot's ``N gone`` counts panes. A gone parked worker is counted bare on
    its own row and by name on a slot's, and turns the panel bad either way."""
    run(cfg, WORKING)
    tab = workers_tab(probed(cfg, monkeypatch), visit=(SLOT_ROW, PARKED_ROW))
    assert tab["titles"] == ["slot — 1 parked gone", "parked worker — 1 gone"]
    assert tab["bad"]


# -- History ------------------------------------------------------------------------
def test_history_does_not_say_a_gone_parked_worker_works_on_the_answer(cfg, monkeypatch):
    run(cfg, WORKING)
    dash = probed(cfg, monkeypatch)
    detail = history_detail(dash)
    assert NO_PROCESS in detail and "GONE" in detail
    assert "your answer" not in detail


def test_history_says_where_a_live_parked_worker_works_as_before(cfg, monkeypatch):
    run(cfg, WORKING)
    with session_of(cfg):
        dash = probed(cfg, monkeypatch)
    detail = history_detail(dash)
    assert f"tmux window {WINDOW}" in detail and "your answer" in detail
    assert "GONE" not in detail


# -- the two cadences: the rows from the state, the finding from the probe -------------
def test_a_worker_that_left_parked_is_no_longer_listed_as_gone(cfg, monkeypatch):
    run(cfg, WORKING)
    dash = probed(cfg, monkeypatch)
    reads_gone(workers_tab(dash), NO_PROCESS)
    with state_mod.transaction(cfg) as st:
        st.clear_pending("P1")
    dash.poll()  # the state moved; the next probe has not run yet
    assert dash.parked_gone == {}
    tab = workers_tab(dash, visit=(SLOT_ROW,))
    assert tab["keys"] == ["slot-0", "slot-1"]
    assert tab["head"] == "1/2 slots busy" and not tab["bad"]
    assert tab["titles"] == ["slot"]


def test_a_worker_parked_again_is_not_gone_by_the_last_finding(cfg, monkeypatch):
    """The finding was of the session that left; the one parked since has not
    been looked at yet."""
    run(cfg, WORKING)
    dash = probed(cfg, monkeypatch)
    reads_gone(workers_tab(dash), NO_PROCESS)
    with state_mod.transaction(cfg) as st:
        st.clear_pending("P1")
    dash.poll()
    run(cfg, WORKING)
    dash.poll()
    reads_in_window(workers_tab(dash))


def test_the_probe_looks_at_no_process_while_nothing_is_parked(cfg, monkeypatch):
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
    looked = []
    table = procs.table
    monkeypatch.setattr(procs, "table", lambda: looked.append(1) or table())
    tab = workers_tab(probed(cfg, monkeypatch), visit=(SLOT_ROW,))
    assert looked == []
    assert tab["keys"] == ["slot-0", "slot-1"] and tab["head"] == "1/2 slots busy"


def test_a_probe_of_the_parked_sessions_that_fails_keeps_the_screen_and_the_finding(
        cfg, monkeypatch):
    run(cfg, WORKING)
    dash = probed(cfg, monkeypatch)
    reads_gone(workers_tab(dash), NO_PROCESS)
    found = dict(dash.parked_gone)

    def broken(*args, **kwargs):
        raise OSError("no /proc here")

    monkeypatch.setattr(doctor, "parked_probe", broken)
    probe(dash, monkeypatch)
    assert found and dash.parked_gone == found
    reads_gone(workers_tab(dash), NO_PROCESS)


# -- home: working now ----------------------------------------------------------------
def test_home_does_not_say_a_gone_parked_worker_works_on_the_answer(cfg, monkeypatch):
    run(cfg, WORKING)
    text = plain(home.worker_rows(probed(cfg, monkeypatch), 70)[-1][0])
    assert "P1" in text and WINDOW in text  # still named, with where it was
    assert "gone" in text and "your answer" not in text


def test_home_says_a_live_parked_worker_works_on_the_answer_as_before(cfg, monkeypatch):
    run(cfg, WORKING)
    with session_of(cfg):
        text = plain(home.worker_rows(probed(cfg, monkeypatch), 70)[-1][0])
    assert WINDOW in text and "works on your answer" in text and "gone" not in text
