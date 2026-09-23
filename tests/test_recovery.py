"""Tier D: the swarm survives things going wrong.

Everything here covers a failure the lifecycle used to absorb *silently* — a
dependency that never landed, a handler that raised, a worker killed out of band,
a status rewritten in the merge queue. All hermetic: no tmux, no claude, no git
(the git-shaped cases stub :mod:`gitq` at the seam the supervisor calls).

The pure-state cases assert the accounting directly; the loop cases drive a real
:class:`Supervisor` over a real FIFO with the bare driver.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from swarm_orchestrator import gitq, ledger
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.state import State
from swarm_orchestrator.supervisor import Supervisor

LEDGER = (
    "- [ ] `P0` · needs:—\n"
    "- [ ] `P1` · needs:`P0`\n"
    "- [ ] `R1` · needs:—\n"
)


def _cfg(tmp_path, monkeypatch, *, driver="bare", isolation=None, watchdog=300):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_DRIVER", driver)
    monkeypatch.setenv("SWARM_SLUG", "recovery")
    if isolation is None:
        monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)
    else:
        monkeypatch.setenv("SWARM_GIT_ISOLATION", isolation)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    cfg = load(project_dir=str(tmp_path))
    cfg.watchdog_s = watchdog  # config.py may not carry the field yet
    return cfg


def _tg(tmp_path) -> list[str]:
    path = tmp_path / "tg.log"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


# -- C1: a failed dependency must not release its dependents --------------
def test_failed_dependency_does_not_release_its_dependent():
    """A `fail` is *attempted*, not *landed*.

    Reading both meanings off bare membership in `done` reported P2 ready while
    P1's branch had already been discarded — so P2 would build against a main
    that provably lacks the dependency it declares.
    """
    graph = ledger.parse("P0\nP1 needs:P0\nP2 needs:P1\n")

    assert ledger.ready(graph, {"P0": "ok", "P1": "fail"}, set(), set()) == []
    # ...and the failed phase itself is NOT re-offered: no auto-retry backstop,
    # `swarm retry` is the explicit reset.
    assert "P1" not in ledger.ready(graph, {"P0": "ok", "P1": "fail"}, set(), set())

    # Every status that actually LANDED releases the dependent.
    for landed in ("ok", "needs-owner", "skip"):
        assert ledger.ready(graph, {"P0": "ok", "P1": landed}, set(), set()) == ["P2"]


def test_ready_still_gates_on_busy_and_excluded():
    """The split of `attempted` from `satisfied` leaves the other filters alone."""
    graph = ledger.parse("P0\nP1 needs:P0\nP2 needs:P0\n")
    assert ledger.ready(graph, {"P0": "ok"}, {"P1"}, {"P2"}) == []


# -- C3: a raising handler must not end the run ---------------------------
def test_dispatch_absorbs_a_raising_handler(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    sup = Supervisor(cfg)
    try:
        def boom():
            raise RuntimeError("tmux pane %999 vanished")

        assert sup._dispatch("done P0 ok", boom) is False
        assert sup._dispatch("done P0 ok", lambda: None) is True
    finally:
        sup.log.close()

    log = cfg.supervisor_log.read_text(encoding="utf-8")
    assert "HANDLER-ERROR" in log and "pane %999 vanished" in log
    assert any("supervisor error on" in ln for ln in _tg(tmp_path))


def test_a_handler_exception_does_not_kill_the_loop(tmp_path, monkeypatch):
    """A tmux failure mid-event used to unwind into the `finally`, which logged
    SUPERVISOR-STOP — indistinguishable from a clean shutdown. The loop must
    step over it and keep reading the FIFO."""
    cfg = _cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)

    def explode(_phase: str) -> None:
        raise RuntimeError("send-keys failed")

    monkeypatch.setattr(sup, "_on_resolved", explode)
    thread = threading.Thread(target=sup.run, daemon=True)
    thread.start()
    try:
        fd = -1
        for _ in range(200):  # wait until the loop is actually READING the FIFO
            try:
                fd = os.open(cfg.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError:
                time.sleep(0.02)  # ENOENT/ENXIO: not opened for reading yet
        assert fd >= 0, "supervisor never attached to the FIFO"
        try:
            os.write(fd, b"resolved P1\n")  # raises inside the handler
            time.sleep(0.3)
            os.write(fd, b"waiting P0\n")  # ...the loop is still reading
            time.sleep(0.3)
            os.write(fd, b"shutdown\n")
        finally:
            os.close(fd)
        thread.join(timeout=10)
    finally:
        sup._stop = True
        thread.join(timeout=5)

    log = cfg.supervisor_log.read_text(encoding="utf-8")
    assert "HANDLER-ERROR" in log and "send-keys failed" in log
    assert "EVENT waiting P0" in log, log  # the NEXT event was still handled
    assert "SUPERVISOR-STOP" in log  # and it stopped cleanly, on request


# -- C2: the opt-in liveness watchdog -------------------------------------
def _pane_probe(dead: set[str]):
    """Stand-in for `tmux run`: report `#{pane_dead}` for known panes only."""

    class R:
        def __init__(self, rc: int, out: str) -> None:
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake(args, check=False):
        pane = args[args.index("-t") + 1]
        if pane in dead:
            return R(1, "")  # tmux: no such pane
        return R(0, "0\n")

    return fake


def test_watchdog_frees_a_slot_whose_pane_died(tmp_path, monkeypatch):
    """A worker killed out of band (host OOM) never sends `done`, so its slot
    stayed busy forever, `pending()` stayed true forever and `_finish` was
    unreachable — a silent hang with no telegram, ever."""
    cfg = _cfg(tmp_path, monkeypatch, driver="tmux", isolation="worktree", watchdog=1)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
        st.slots[0].pane_id = "%42"
    discarded: list[str] = []
    monkeypatch.setattr(gitq, "discard", lambda c, p, l: discarded.append(p))
    monkeypatch.setattr("swarm_orchestrator.tmux.run", _pane_probe({"%42"}))

    sup = Supervisor(cfg)
    try:
        sup._watchdog_tick()
        # First sighting only SUSPECTS it: `launch` claims a slot before it
        # respawns the pane, so one sighting could reap a starting worker.
        assert state_mod.read(cfg).busy_slots(), "reaped on a single sighting"
        assert discarded == []

        sup._last_sweep = 0.0
        sup._watchdog_tick()  # confirmed dead on a second sweep -> reap
        st = state_mod.read(cfg)
        assert not st.busy_slots()  # slot freed
        assert "P0" not in st.done  # NOT recorded done: still relaunchable
        assert discarded == ["P0"]  # its branches rolled back
    finally:
        sup.log.close()

    assert any("died without 'swarm done'" in ln for ln in _tg(tmp_path))
    assert "WATCHDOG-REAP P0" in cfg.supervisor_log.read_text(encoding="utf-8")


def test_watchdog_leaves_a_live_pane_alone(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, driver="tmux", isolation="worktree", watchdog=1)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
        st.slots[0].pane_id = "%42"
    monkeypatch.setattr("swarm_orchestrator.tmux.run", _pane_probe(set()))

    sup = Supervisor(cfg)
    try:
        for _ in range(3):
            sup._last_sweep = 0.0
            sup._watchdog_tick()
        assert [s.phase for s in state_mod.read(cfg).busy_slots()] == ["P0"]
    finally:
        sup.log.close()


def test_watchdog_renudges_an_idle_swarm_with_ready_phases(tmp_path, monkeypatch):
    """The accepted lost-nudge race is now recoverable instead of terminal."""
    cfg = _cfg(tmp_path, monkeypatch, watchdog=1)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.last_event_at = time.time() - 600  # nothing has happened for 10 min

    sup = Supervisor(cfg)
    injected: list[str] = []
    monkeypatch.setattr(sup.master, "is_alive", lambda: True)
    monkeypatch.setattr(sup.master, "inject", lambda msg: injected.append(msg))
    try:
        sup._watchdog_tick()
        assert injected and "watchdog" in injected[0]
        # ...and the nudge counts as movement, so it does not re-fire at once.
        injected.clear()
        sup._last_sweep = 0.0
        sup._watchdog_tick()
        assert injected == []
    finally:
        sup.log.close()


def test_watchdog_finishes_a_settled_run(tmp_path, monkeypatch):
    """The finish check only ever ran on `master-idle`, and that event is
    single-shot: refused once while a worker held a slot, it never comes again —
    so a run whose last worker was reaped stayed unfinished forever."""
    cfg = _cfg(tmp_path, monkeypatch, watchdog=1)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.done = {"P0": "ok", "P1": "ok", "R1": "ok"}  # whole ledger accounted for
        st.last_event_at = time.time() - 600
    sup = Supervisor(cfg)
    try:
        sup._watchdog_tick()
        assert state_mod.read(cfg).finished
    finally:
        sup.log.close()
    assert "WATCHDOG-FINISH settled" in cfg.supervisor_log.read_text(encoding="utf-8")
    assert any("swarm finished: 3 phase(s) done" in ln for ln in _tg(tmp_path))


def test_watchdog_does_not_finish_a_run_still_holding_a_worker(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, watchdog=1)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.done = {"P0": "ok", "P1": "ok"}
        st.parked.append("R1")  # owes the owner an answer -> still pending
        st.last_event_at = time.time() - 600
    sup = Supervisor(cfg)
    try:
        sup._watchdog_tick()
        assert not state_mod.read(cfg).finished
    finally:
        sup.log.close()


def test_watchdog_holds_off_while_paused_or_blocked(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, watchdog=1)
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)
    injected: list[str] = []
    monkeypatch.setattr(sup.master, "is_alive", lambda: True)
    monkeypatch.setattr(sup.master, "inject", lambda msg: injected.append(msg))
    try:
        for field, value in (("paused", True), ("integ_blocked", "P9")):
            with state_mod.transaction(cfg) as st:
                st.paused, st.integ_blocked = False, None
                setattr(st, field, value)
                st.last_event_at = time.time() - 600
            sup._last_sweep = 0.0
            sup._watchdog_tick()
            assert injected == [], field
    finally:
        sup.log.close()


def test_watchdog_zero_restores_the_pure_event_loop(tmp_path, monkeypatch):
    """`watchdog_s = 0` must be byte-for-byte the old behaviour: no timer, no
    sweep — the README's design philosophy forbids polling, so it stays opt-in."""
    cfg = _cfg(tmp_path, monkeypatch, driver="tmux", watchdog=0)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
        st.slots[0].pane_id = "%42"
    monkeypatch.setattr("swarm_orchestrator.tmux.run", _pane_probe({"%42"}))

    sup = Supervisor(cfg)
    try:
        assert sup._select_timeout() is None  # select blocks indefinitely
        sup._watchdog_tick()
        sup._last_sweep = 0.0
        sup._watchdog_tick()
        assert [s.phase for s in state_mod.read(cfg).busy_slots()] == ["P0"]
    finally:
        sup.log.close()


def test_select_timeout_caps_but_never_delays_a_park(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, watchdog=300)
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)
    try:
        assert sup._next_timeout() is None  # nothing scheduled...
        assert 0 <= sup._select_timeout() <= 300  # ...but the sweep is capped
        with state_mod.transaction(cfg) as st:
            st.waiting["P0"] = time.time() + 5
        assert sup._select_timeout() <= 5  # an earlier park deadline still wins
    finally:
        sup.log.close()


# -- status must survive the merge queue ----------------------------------
def test_needs_owner_survives_integration(tmp_path, monkeypatch):
    """`_advance_done(phase, "ok")` was hardcoded in the merge queue, so every
    `needs-owner` was recorded as a clean success and `swarm status` could never
    tell the owner a phase was waiting on them."""
    cfg = _cfg(tmp_path, monkeypatch, isolation="worktree")
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
    monkeypatch.setattr(gitq, "integrate", lambda c, p, l, *_: gitq.MERGED)

    sup = Supervisor(cfg)
    monkeypatch.setattr(sup.master, "is_alive", lambda: True)
    monkeypatch.setattr(sup.master, "inject", lambda msg: None)
    try:
        sup._on_done("P0", "needs-owner")
        st = state_mod.read(cfg)
        assert st.done["P0"] == "needs-owner"
        assert st.integ_queue == [] and st.integ_status == {}
    finally:
        sup.log.close()


def test_merge_queue_keeps_each_phase_status(tmp_path, monkeypatch):
    """Two phases queued behind a block keep their own statuses."""
    cfg = _cfg(tmp_path, monkeypatch, isolation="worktree")
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
        st.claim_slot("P1")
        st.integ_blocked = "P9"  # queue held: nothing drains
    sup = Supervisor(cfg)
    try:
        sup._on_done("P0", "needs-owner")
        sup._on_done("P1", "ok")
        st = state_mod.read(cfg)
        assert st.integ_queue == ["P0", "P1"]  # display shape unchanged
        assert st.integ_head() == ("P0", "needs-owner")
        assert st.integ_status == {"P0": "needs-owner", "P1": "ok"}
    finally:
        sup.log.close()


# -- the finish report must not count what was never built ----------------
def test_finish_count_excludes_skipped_and_failed(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)
    try:
        sup._finish(2, None, skipped=3, failed=["P8", "P9"])
    finally:
        sup.log.close()
    line = _tg(tmp_path)[0]
    assert "2 phase(s) done" in line  # ok + needs-owner only
    assert "3 skipped" in line and "2 failed: P8, P9" in line


def test_master_idle_finish_counts_only_built_phases(tmp_path, monkeypatch):
    """`len(done)` counted the whole map, so a run that skipped many phases and
    built few reported the inflated total as "phase(s) done"."""
    cfg = _cfg(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.done = {"P0": "ok", "R1": "needs-owner", "P1": "skip", "P8": "fail"}
    sup = Supervisor(cfg)
    try:
        sup._on_master_idle()
    finally:
        sup.log.close()
    line = _tg(tmp_path)[0]
    assert "2 phase(s) done" in line and "1 skipped" in line and "1 failed: P8" in line


# -- `swarm skip` / `swarm free` must be able to clear a pending phase ----
def test_clear_phase_settles_a_parked_phase():
    """`clear_pending` was only ever reached from the supervisor's `done` path, so
    skipping a parked phase left `pending()` true forever with no CLI to clear it
    — the run could never finish."""
    st = State.fresh(2)
    st.claim_slot("P0")
    st.parked.append("P0")
    st.waiting["P0"] = 1.0
    assert st.pending()

    assert st.clear_phase("P0", "skip") is True  # was parked
    assert not st.pending()
    assert st.done["P0"] == "skip"
    assert not st.any_busy() and not st.parked and not st.waiting


def test_clear_phase_without_a_status_just_frees():
    st = State.fresh(1)
    st.claim_slot("P0")
    st.waiting["P0"] = 1.0
    assert st.clear_phase("P0") is False  # was waiting, not parked
    assert not st.pending() and st.done == {}


# -- a live worker's phase can never be claimed twice ---------------------
@pytest.mark.parametrize("where", ["parked", "waiting"])
def test_claim_slot_refuses_a_parked_or_waiting_phase(where):
    """Both hold a LIVE worker on `swarm/<phase>`; a second claim would hand
    `worktree_add` a branch it force-deletes out from under it."""
    st = State.fresh(2)
    if where == "parked":
        st.parked.append("P0")
    else:
        st.waiting["P0"] = 1.0
    assert st.claim_slot("P0") is None
    assert st.claim_slot("R1") is not None  # another phase is unaffected


# -- live resize ----------------------------------------------------------
def test_resize_grows_and_retires_without_evicting_a_worker():
    st = State.fresh(2)
    st.claim_slot("P0")

    assert st.resize(4) == (2, 0)  # grow: two fresh slots, nothing retiring
    assert [s.id for s in st.slots] == [0, 1, 2, 3]

    added, retiring = st.resize(1)  # shrink below the running worker count
    assert (added, retiring) == (0, 3)
    assert len(st.free_slots()) == 0  # retiring slots are unclaimable
    assert st.claim_slot("R1") is None
    assert [s.id for s in st.busy_slots()] == [0]  # P0 was NOT evicted

    assert sorted(st.reap_retired()) == [1, 2, 3]  # idle ones drop immediately
    assert [s.id for s in st.slots] == [0]


def test_reap_retired_waits_for_a_busy_slot_to_drain():
    st = State.fresh(2)
    st.claim_slot("P0")
    st.claim_slot("P1")
    st.resize(1)
    assert st.reap_retired() == []  # both busy: nothing to reap yet
    st.free_slot_for("P1")
    assert st.reap_retired() == [1]
    assert len(st.slots) == 1


# -- old state files must still load --------------------------------------
def test_from_dict_loads_a_pre_watchdog_state_file():
    """`swarm up` must not choke on a state.json written before these fields."""
    old = {
        "slots": [{"id": 0, "pane_id": "%1", "busy": True, "phase": "P0"}],
        "done": {"P0": "ok"},
        "integ_queue": ["P1"],  # bare ids, no status map
        "waiting": {},
        "parked": [],
    }
    st = State.from_dict(old)
    assert st.slots[0].retiring is False
    assert st.last_event_at == 0.0
    assert st.integ_head() == ("P1", "ok")  # missing status reads as ok
    assert State.from_dict(st.to_dict()).integ_head() == ("P1", "ok")


# -- C4: a held restart integration must be reported, not swallowed -------
def test_reconcile_reports_held_phases(tmp_path, monkeypatch):
    """`swarm up` seeds `done` from the sentinels even when integration was HELD,
    leaving a phantom-complete phase with no resolver and no ping. The NEXT `up`
    then sees it in `done` and `discard`s the completed work outright."""
    from swarm_orchestrator.logutil import Log

    cfg = _cfg(tmp_path, monkeypatch, isolation="worktree")
    repo = tmp_path / "sub"
    monkeypatch.setattr(gitq, "_all_swarm_phases", lambda c: {"P0", "P1"})
    monkeypatch.setattr(gitq, "sentinel_done", lambda c: {"P0": "ok", "P1": "needs-owner"})
    monkeypatch.setattr(
        gitq, "integrate", lambda c, p, l, *_: gitq.MERGED if p == "P0" else gitq.CONFLICT
    )
    monkeypatch.setattr(gitq, "blocked_repo", lambda c, p: repo)

    log = Log(cfg.supervisor_log)
    try:
        result = gitq.reconcile(cfg, {}, log)
        assert result.integrated == ["P0"]
        assert result.held == [gitq.Held(phase="P1", kind=gitq.CONFLICT, repo=repo)]
        # the old shape still answers exactly as before, for callers not yet updated
        assert gitq.reconcile_orphans(cfg, {}, log) == ["P0"]
    finally:
        log.close()


def test_branch_exists_is_public(tmp_path, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(gitq, "_branch_exists", lambda r, b: calls.append((r, b)) or True)
    assert gitq.branch_exists(Path("/repo"), "swarm/P0") is True
    assert calls == [(Path("/repo"), "swarm/P0")]
