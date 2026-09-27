"""Phase 3: park a worker that is waiting on the owner.

Three tiers:

* pure state — ``park``/``pending``/``clear_pending`` accounting and the
  ``waiting``/``parked`` serialisation round-trip (no tmux, no supervisor);
* config — ``park_after`` parsing from ``[worker]`` / env with the 120 default and
  0-disables floor;
* supervisor lifecycle (bare driver, ``SWARM_DRIVER=bare``) — a ``waiting`` arms a
  deadline, ``_check_park_deadlines`` parks the phase (slot freed, phase parked, a
  replacement can claim the slot), the finish guard blocks while parked/waiting,
  ``resumed`` cancels a pending park, and a ``done`` on a parked phase clears it and
  unblocks its dependents. All driver-guarded pane ops are skipped in bare mode, so
  these are PURE STATE.
* a live-tmux smoke for the real split-first ``park_pane`` pane mechanic on a fully
  isolated server (its own ``TMUX_TMPDIR``, killed after — never a running swarm).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.master import build_context
from swarm_orchestrator.state import State
from swarm_orchestrator.supervisor import Supervisor


# -- pure state -----------------------------------------------------------
def test_state_park_frees_slot_and_moves_waiting_to_parked():
    st = State.fresh(2)
    st.claim_slot("P0")
    st.slots[0].pane_id = "%5"  # a running swarm has a pane behind the slot
    st.waiting["P0"] = 123.0
    st.park("P0")
    assert "P0" in st.parked and "P0" not in st.waiting
    assert not any(s.busy and s.phase == "P0" for s in st.slots)  # slot freed
    # the freed slot KEEPS its pane_id so a replacement worker can respawn into it
    assert st.slots[0].pane_id == "%5"
    st.park("P0")  # idempotent -> no duplicate in parked
    assert st.parked.count("P0") == 1


def test_state_pending_true_while_waiting_or_parked():
    st = State.fresh(1)
    assert not st.pending()
    st.waiting["X"] = 1.0
    assert st.pending()  # waiting counts
    st.waiting.clear()
    st.parked.append("X")
    assert st.pending()  # parked counts
    assert st.clear_pending("X") is True  # was parked
    assert not st.pending()
    assert st.clear_pending("X") is False  # already gone


def test_state_from_dict_roundtrips_waiting_and_parked():
    st = State.fresh(2)
    st.waiting = {"A": 111.5, "B": 222.0}
    st.parked = ["C", "D"]
    st2 = State.from_dict(st.to_dict())
    assert st2.waiting == {"A": 111.5, "B": 222.0}
    assert st2.parked == ["C", "D"]


# -- config ---------------------------------------------------------------
def _write_toml(tmp_path: Path, body: str) -> None:
    (tmp_path / ".swarm.toml").write_text(body, encoding="utf-8")


def test_park_after_parses_from_worker_env_and_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_PARK_AFTER", raising=False)
    monkeypatch.delenv("SWARM_SLUG", raising=False)
    _write_toml(tmp_path, "[swarm]\nmax_workers = 2\n")
    assert load(project_dir=str(tmp_path)).park_after == 120  # code default

    _write_toml(tmp_path, "[swarm]\nmax_workers = 2\n[worker]\npark_after = 30\n")
    assert load(project_dir=str(tmp_path)).park_after == 30  # from [worker]

    monkeypatch.setenv("SWARM_PARK_AFTER", "7")
    assert load(project_dir=str(tmp_path)).park_after == 7  # env overrides config

    monkeypatch.setenv("SWARM_PARK_AFTER", "0")
    assert load(project_dir=str(tmp_path)).park_after == 0  # 0 disables (still floors)

    monkeypatch.setenv("SWARM_PARK_AFTER", "-5")
    assert load(project_dir=str(tmp_path)).park_after == 0  # floored at minimum 0


# -- supervisor lifecycle (bare driver -> pure state) ---------------------
LEDGER = (
    "- [ ] `P0` · needs:—\n"
    "- [ ] `P0b` · needs:`P0`\n"
    "- [ ] `R1` · needs:—\n"
)


def _bare_cfg(tmp_path, monkeypatch, park_after="5"):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_PARK_AFTER", park_after)
    monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)  # isolation=none
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    return load(project_dir=str(tmp_path))


def _fake_alive_master(sup) -> subprocess.Popen:
    """Give the supervisor a harmless live master so `_relaunch` INJECTS (writes to
    a `sleep` proc's ignored stdin) instead of spawning a real `claude`."""
    proc = subprocess.Popen(["sleep", "60"], stdin=subprocess.PIPE, text=True)
    sup.master.proc = proc
    with state_mod.transaction(sup.cfg) as st:
        st.master_alive = True
    return proc


def _claim(cfg, phase: str) -> None:
    with state_mod.transaction(cfg) as st:
        st.claim_slot(phase)


def _force_due(cfg, phase: str) -> None:
    with state_mod.transaction(cfg) as st:
        st.waiting[phase] = time.time() - 0.01


def test_waiting_arms_deadline_then_park_frees_slot_for_replacement(tmp_path, monkeypatch):
    cfg = _bare_cfg(tmp_path, monkeypatch, park_after="5")
    state_mod.init_state(cfg)
    _claim(cfg, "P0")  # simulate a launched worker holding a slot
    sup = Supervisor(cfg)
    proc = _fake_alive_master(sup)
    try:
        t0 = time.time()
        sup._on_waiting("P0")
        st = state_mod.read(cfg)
        assert "P0" in st.waiting
        assert t0 + 4 <= st.waiting["P0"] <= t0 + 6  # deadline = now + park_after
        # not yet due -> a wake parks nothing, and the timeout is still in the future
        sup._check_park_deadlines()
        assert "P0" not in state_mod.read(cfg).parked
        assert sup._next_timeout() > 0

        _force_due(cfg, "P0")
        assert sup._next_timeout() == 0.0  # past deadline clamps to poll-now
        sup._check_park_deadlines()  # fires the park

        st = state_mod.read(cfg)
        assert "P0" in st.parked and "P0" not in st.waiting
        assert not any(s.busy and s.phase == "P0" for s in st.slots)  # slot freed
        assert sup._next_timeout() is None  # nothing waiting -> block indefinitely
        # The owner was asked when it started waiting; a park only moves windows,
        # so it is logged for the dashboard and not sent.
        sink = tmp_path / "tg.log"
        assert "now in its own tmux window" not in (sink.read_text() if sink.exists() else "")
        ledger = (cfg.state_dir / "notifications.jsonl").read_text()
        assert '"kind": "park"' in ledger and '"suppressed"' in ledger
        # a replacement phase can claim the freed slot
        with state_mod.transaction(cfg) as st2:
            claimed = st2.claim_slot("R1")
        assert claimed is not None
    finally:
        proc.kill()
        sup.log.close()


def test_finish_guard_does_not_finish_while_parked_or_waiting(tmp_path, monkeypatch):
    cfg = _bare_cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    for kind in ("parked", "waiting"):
        sup = Supervisor(cfg)
        proc = _fake_alive_master(sup)
        try:
            with state_mod.transaction(cfg) as st:
                st.parked.clear()
                st.waiting.clear()
                if kind == "parked":
                    st.parked.append("P0")
                else:
                    st.waiting["P0"] = time.time() + 100
            sup._on_master_idle()  # kills master, checks pending()
            assert not state_mod.read(cfg).finished, kind  # pending -> no finish
        finally:
            proc.kill()
            sup.log.close()
    # once cleared, the swarm is free to finish
    sup = Supervisor(cfg)
    proc = _fake_alive_master(sup)
    try:
        with state_mod.transaction(cfg) as st:
            st.parked.clear()
            st.waiting.clear()
            st.done = {"P0": "ok", "P0b": "ok", "R1": "ok"}  # nothing left ready
        sup._on_master_idle()
        assert state_mod.read(cfg).finished
    finally:
        proc.kill()
        sup.log.close()


def test_resumed_cancels_a_pending_park(tmp_path, monkeypatch):
    cfg = _bare_cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    _claim(cfg, "P0")
    sup = Supervisor(cfg)
    proc = _fake_alive_master(sup)
    try:
        sup._on_waiting("P0")
        assert "P0" in state_mod.read(cfg).waiting
        sup._on_resumed("P0")  # owner answered before the park fired
        assert "P0" not in state_mod.read(cfg).waiting
        sup._check_park_deadlines()  # nothing due -> never parks
        st = state_mod.read(cfg)
        assert "P0" not in st.parked
        assert any(s.busy and s.phase == "P0" for s in st.slots)  # still holds its slot
    finally:
        proc.kill()
        sup.log.close()


def test_done_on_a_parked_phase_clears_it_and_unblocks_dependents(tmp_path, monkeypatch):
    cfg = _bare_cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    _claim(cfg, "P0")
    sup = Supervisor(cfg)
    proc = _fake_alive_master(sup)
    try:
        sup._on_waiting("P0")
        _force_due(cfg, "P0")
        sup._check_park_deadlines()
        assert "P0" in state_mod.read(cfg).parked
        # a phase depending on the parked phase must NOT be ready yet
        assert "P0b" not in build_context(cfg, state_mod.read(cfg))["ready"]

        # owner answers -> the worker runs `swarm done` (isolation=none -> advance)
        sup._on_done("P0", "ok")
        st = state_mod.read(cfg)
        assert st.done.get("P0") == "ok"
        assert "P0" not in st.parked and "P0" not in st.waiting
        # its dependent auto-clears via the normal advance
        assert "P0b" in build_context(cfg, st)["ready"]
    finally:
        proc.kill()
        sup.log.close()


def test_park_disabled_when_park_after_zero(tmp_path, monkeypatch):
    cfg = _bare_cfg(tmp_path, monkeypatch, park_after="0")
    state_mod.init_state(cfg)
    _claim(cfg, "P0")
    sup = Supervisor(cfg)
    try:
        sup._on_waiting("P0")  # parking disabled -> no deadline armed
        assert state_mod.read(cfg).waiting == {}
        assert sup._next_timeout() is None
        assert "WAITING-IGNORED" in cfg.supervisor_log.read_text(encoding="utf-8")
    finally:
        sup.log.close()


def test_ready_excludes_parked_and_waiting_phases(tmp_path, monkeypatch):
    cfg = _bare_cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    # baseline: both P0 and R1 are deps-free roots -> both ready
    assert set(build_context(cfg, state_mod.read(cfg))["ready"]) >= {"P0", "R1"}
    with state_mod.transaction(cfg) as st:
        st.parked.append("P0")  # parked off-grid
        st.waiting["R1"] = time.time() + 100  # waiting on the owner
    ready = build_context(cfg, state_mod.read(cfg))["ready"]
    assert "P0" not in ready  # parked -> never relaunched
    assert "R1" not in ready  # waiting -> never relaunched


# -- end-to-end: the real detached supervisor + select() timeout ----------
def test_end_to_end_waiting_parks_via_select_timeout_and_replaces(swarm):
    """Drive the REAL supervisor loop: a `waiting` arms a deadline, the select()
    TIMEOUT (no FIFO event) fires the park, the freed slot is filled by the other
    independent root, finish stays blocked while parked, and `done` clears it.

    Two independent roots with ``max_workers = 1`` so the initial launch takes one
    and parking it frees the single slot for the replacement — proving the whole
    waiting -> park -> replace -> done cycle through the process boundary."""
    toml = swarm.project / ".swarm.toml"
    toml.write_text(
        toml.read_text().replace("max_workers = 4", "max_workers = 1"), encoding="utf-8"
    )
    (swarm.project / "ledger.txt").write_text("P0\nQ0\n", encoding="utf-8")
    swarm.env["FAKE_WORKER_PARK"] = "1"  # workers hold their slot; test drives park
    swarm.env["SWARM_PARK_AFTER"] = "2"  # short deadline so the select timeout fires
    swarm.up()

    assert swarm.wait(lambda: swarm.busy_count() == 1, timeout=20), swarm.log_text()
    first = swarm.busy_phases()[0]  # whichever root launched into the lone slot
    other = "Q0" if first == "P0" else "P0"

    swarm.cli("waiting", first, "need", "the", "owner")
    # No further FIFO events: only the deadline-driven select() timeout can advance
    # this. The park frees the slot and the relaunched master fills it with `other`.
    assert swarm.wait(
        lambda: swarm.busy_phases() == [other]
        and first in (swarm.state() or {}).get("parked", []),
        timeout=20,
    ), swarm.log_text()
    assert f"PARK {first}" in swarm.log_text()
    assert not swarm.finished()  # a parked worker keeps the swarm pending

    swarm.cli("done", first, "ok")  # owner answered -> worker finishes
    assert swarm.wait(
        lambda: first not in (swarm.state() or {}).get("parked", []), timeout=10
    ), swarm.log_text()
    assert swarm.state()["done"].get(first) == "ok"


# -- live-tmux smoke: the real split-first park_pane mechanic --------------
@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not available")
def test_park_pane_mechanic_live_tmux(monkeypatch):
    """The real ``tmux.park_pane`` on a fully isolated server (own TMUX_TMPDIR).

    Asserts the waiting worker survives (same pane id + pid) in a fresh
    ``wait:<phase>`` window IN THE SWARM'S OWN SESSION, while the slot's window
    keeps a fresh replacement pane tagged with the slot. The server is killed and
    its socket dir removed after, so the default server (and any live swarm on it)
    is never touched.

    A second, more-recently-used ``decoy`` session exists for the whole park: with
    only one session on the server, tmux's "most recently used" fallback is always
    the right answer by accident, so a single-session probe cannot see a
    ``break-pane`` that names no destination."""
    from swarm_orchestrator import tmux

    sockdir = tempfile.mkdtemp(prefix="wtprobe-")  # isolated tmux server socket dir
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)  # never nest onto the outer server
    session = "wtprobe"
    try:
        win = tmux.new_session(session)
        tmux.harden(session)
        tmux.rename_window(win, "workers")
        # a 2-pane workers window: pane0 = the waiting worker, pane1 = a sibling
        old_pane, sib = tmux.split_layout(win, 2)
        tmux.set_slot(old_pane, 0)
        tmux.set_slot(sib, 1)
        # give the waiter a distinctive long-lived marker process to track survival
        tmux.respawn_pane(old_pane, "exec sleep 987654")
        old_pid = tmux.run(
            ["display-message", "-p", "-t", old_pane, "#{pane_pid}"]
        ).stdout.strip()
        assert old_pid.isdigit()

        # sanity: split-first avoids the single-pane break edge (window has >= 2)
        assert tmux.window_of(old_pane) == win

        # the owner's session, touched last so tmux would drift the waiter into it
        tmux.new_session("decoy")
        tmux.run(["select-window", "-t", "=decoy:"])

        wait_win, replacement = tmux.park_pane(
            win, old_pane, 0, "wait:demo-P3", session
        )

        # the wait window belongs to the SWARM's session, never the decoy
        assert (
            tmux.run(
                ["display-message", "-p", "-t", wait_win, "#{session_name}"]
            ).stdout.strip()
            == session
        )

        # the waiting worker survived in its own wait window: same pane id + same pid
        assert tmux.list_panes(wait_win) == [old_pane]
        wait_pid = tmux.run(
            ["display-message", "-p", "-t", old_pane, "#{pane_pid}"]
        ).stdout.strip()
        assert wait_pid == old_pid
        import os

        os.kill(int(old_pid), 0)  # process still alive -> no exception
        assert (
            tmux.run(
                ["display-message", "-p", "-t", wait_win, "#{window_name}"]
            ).stdout.strip()
            == "wait:demo-P3"
        )

        # the slot's window kept a FRESH replacement pane tagged with the slot
        slots = dict(tmux.list_panes_with_slot(win))
        assert replacement in slots and slots[replacement] == "0"
        assert old_pane not in slots  # the waiter left this window
    finally:
        tmux.run(["kill-server"])  # the isolated server only
        shutil.rmtree(sockdir, ignore_errors=True)
