"""The instant launcher: the supervisor starts ready phases itself.

Every free slot used to wait on an LLM master that ran ``swarm context`` and
``swarm launch`` (seconds and a model call per launch, and one hang could stall
the run). The launchable set is a pure function of the ledger and
``state.json``, so the supervisor now launches it directly, in ledger order, on
background threads. What is load-bearing here:

* **ledger order, capped at the free slots** — and a slot a launch thread has
  not claimed yet counts as taken, so two fills never oversubscribe;
* **no double launch** — a phase being launched is never picked again, and a
  racing ``swarm launch`` by hand is refused by ``claim_slot``;
* **a failed launch backs off, then gives up** — never a hot loop that fails
  the same cause into every free slot and pings the owner each time;
* **the finish holds while anything could still move**, and fires with no
  master at all once nothing can;
* **the init master still runs first** — its command-file commit must precede
  the first worktree — but a hung one cannot hold launching past the watchdog.

Everything except the ``real_launch`` cases runs with the conftest stub, which
records picks instead of starting workers.
"""

from __future__ import annotations

import time

import pytest

from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import supervisor as sup_mod
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor
from test_deps import _bare_cfg

LEDGER = (
    "- [ ] `P0` · needs:—\n"
    "- [ ] `P1` · needs:—\n"
    "- [ ] `P2` · needs:—\n"
    "- [ ] `P3` · needs:—\n"
    "- [ ] `P4` · needs:—\n"
    "- [ ] `P9` · needs:`P0`\n"
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = _bare_cfg(tmp_path, monkeypatch, LEDGER)
    monkeypatch.setenv("SWARM_BIN", "true")  # nothing detached may reach a model
    state_mod.init_state(c)  # 4 slots (max_workers default)
    return c


@pytest.fixture
def sup(cfg):
    s = Supervisor(cfg)
    yield s
    s.log.close()


def _log(cfg) -> str:
    return cfg.supervisor_log.read_text(encoding="utf-8")


def _tg(tmp_path) -> list[str]:
    path = tmp_path / "tg.log"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _fail(sup, phase: str, n: int = 1, ago: float = 0.0) -> None:
    with sup._launch_lock:
        sup._launch_fails[phase] = (n, time.time() - ago)


# -- picking ----------------------------------------------------------------
def test_fill_launches_ready_phases_in_ledger_order_capped_at_free_slots(sup, cfg):
    assert sup._fill_slots("test") == ["P0", "P1", "P2", "P3"]
    assert sup.stub_launches == ["P0", "P1", "P2", "P3"]
    assert "LAUNCH-READY P0 P1 P2 P3 (test)" in _log(cfg)


def test_a_paused_or_finished_swarm_launches_nothing(sup, cfg):
    for field in ("paused", "finished"):
        with state_mod.transaction(cfg) as st:
            st.paused = st.finished = False
            setattr(st, field, True)
        assert sup._fill_slots("test") == [], field


def test_a_phase_being_launched_is_never_picked_twice(sup, cfg):
    """The thread has not claimed yet: the phase is not busy in state, so only
    the in-memory guard stops a second fill racing it to `claim_slot` — and its
    pending claim still uses up a free slot."""
    with sup._launch_lock:
        sup._launching.update({"P0", "P1"})
    picks = sup._fill_slots("test")
    assert picks == ["P2", "P3"]  # 4 free - 2 unclaimed launches = 2


def test_a_claimed_launch_is_not_counted_twice(sup, cfg):
    """Once the thread has claimed, its slot is no longer free; subtracting it
    again would leave a free slot empty."""
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
    with sup._launch_lock:
        sup._launching.add("P0")
    assert sup._fill_slots("test") == ["P1", "P2", "P3"]


def test_the_bootstrap_pass_holds_launching_until_it_idles(sup, cfg, monkeypatch):
    monkeypatch.setattr(sup, "_spawn_master", lambda kind: True)
    sup._on_bootstrap()
    monkeypatch.setattr(sup.master, "is_alive", lambda: True)  # mid-pass
    assert sup._fill_slots("a done mid-bootstrap") == []
    assert "LAUNCH-HELD bootstrap" in _log(cfg)
    assert sup._fill_slots("watchdog", force=True)  # a hung master cannot stall it
    monkeypatch.setattr(sup.master, "kill", lambda: None)
    monkeypatch.setattr(sup.master, "is_alive", lambda: False)
    sup.stub_launches.clear()
    sup._on_master_idle()
    assert sup.stub_launches  # the idle releases the first batch


def test_bootstrap_with_no_master_launches_at_once(sup, cfg, monkeypatch):
    monkeypatch.setattr(sup, "_spawn_master", lambda kind: False)
    sup._on_bootstrap()
    assert sup.stub_launches == ["P0", "P1", "P2", "P3"]
    assert "BOOTSTRAP-NO-MASTER" in _log(cfg)


def test_an_init_master_that_died_without_idling_does_not_hold_launching(sup, cfg, monkeypatch):
    sup._bootstrapping = True
    monkeypatch.setattr(sup.master, "is_alive", lambda: False)
    assert sup._fill_slots("test") == ["P0", "P1", "P2", "P3"]


def test_a_done_launches_its_successor_with_no_master(sup, cfg, monkeypatch):
    spawned: list[str] = []
    monkeypatch.setattr(sup, "_spawn_master", lambda kind: spawned.append(kind) or True)
    with state_mod.transaction(cfg) as st:
        for p in ("P1", "P2", "P3", "P4"):
            st.done[p] = "ok"
        st.claim_slot("P0")
    sup._advance_done("P0", "ok")
    assert sup.stub_launches == ["P9"]
    assert spawned == []


# -- failed launches: back off, then give up ----------------------------------
def test_a_failed_phase_backs_off_then_is_retried(sup, cfg):
    _fail(sup, "P0")
    assert sup._fill_slots("test") == ["P1", "P2", "P3", "P4"]  # P0 waits
    assert sup._fill_slots("watchdog", force=True)[0] == "P0"  # the watchdog doesn't
    _fail(sup, "P0", ago=sup_mod.LAUNCH_RETRY_S + 1)
    assert sup._fill_slots("test")[0] == "P0"  # back-off over


def test_the_backoff_expiry_wakes_the_loop_and_fires_once(sup, cfg):
    _fail(sup, "P0", ago=sup_mod.LAUNCH_RETRY_S - 5)
    assert 0 < sup._next_timeout() <= 5  # woken even with the watchdog off
    _fail(sup, "P0", ago=sup_mod.LAUNCH_RETRY_S + 1)
    assert sup._next_timeout() is None  # an expired stamp never spins select
    sup._retry_backed_off()
    assert sup.stub_launches[0] == "P0"
    sup.stub_launches.clear()
    sup._retry_backed_off()
    assert sup.stub_launches == []  # acted on once, not on every wake


def test_repeated_failures_give_up_ask_once_and_let_the_run_finish(sup, cfg, tmp_path):
    with state_mod.transaction(cfg) as st:
        st.done = {p: "ok" for p in ("P1", "P2", "P3", "P4")}
    _fail(sup, "P0", n=sup_mod.LAUNCH_GIVE_UP, ago=sup_mod.LAUNCH_RETRY_S + 1)
    assert sup._fill_slots("watchdog", force=True) == []  # even force leaves it
    sup._on_launched("P0", launch_mod.FAILED)
    st = state_mod.read(cfg)
    assert st.finished
    tg = _tg(tmp_path)
    assert sum("Asks you: Fix why P0 will not start" in ln for ln in tg) == 1
    assert sum("failed to start 3 times" in ln for ln in tg) == 1
    assert any("The run has finished" in ln and "`swarm launch` the 1 that never started" in ln
               for ln in tg)


def test_resume_hands_given_up_phases_back(sup, cfg):
    _fail(sup, "P0", n=sup_mod.LAUNCH_GIVE_UP)
    sup._on_resume()
    assert sup.stub_launches[0] == "P0"


def test_a_launch_thread_records_the_outcome_and_clears_the_guard(sup, cfg, monkeypatch):
    outcomes = iter([launch_mod.FAILED, launch_mod.LAUNCHED])
    monkeypatch.setattr(launch_mod, "launch_outcome", lambda *a, **k: next(outcomes))
    pokes: list[str] = []
    monkeypatch.setattr(launch_mod, "_poke_fifo", lambda c, line: pokes.append(line))
    with sup._launch_lock:
        sup._launching.add("P0")
    sup._launch_worker("P0")
    assert sup._launch_fails["P0"][0] == 1 and "P0" not in sup._launching
    sup._launch_worker("P0")
    assert "P0" not in sup._launch_fails  # a success clears the count
    assert pokes == ["launched P0 failed\n", "launched P0 launched\n"]


def test_the_launcher_writes_its_pause_where_doctor_reads_it(sup, cfg, monkeypatch):
    """The back-off lived in the supervisor's memory only, so ``swarm doctor``
    read a phase waiting it out as launchable."""
    outcomes = iter([launch_mod.FAILED, launch_mod.LAUNCHED])
    monkeypatch.setattr(launch_mod, "launch_outcome", lambda *a, **k: next(outcomes))
    monkeypatch.setattr(launch_mod, "_poke_fifo", lambda c, line: None)
    with sup._launch_lock:
        sup._launching.add("P0")
    sup._publish_launches()
    assert state_mod.read(cfg).launching == ["P0"]
    sup._launch_worker("P0")
    st = state_mod.read(cfg)
    assert st.launching == [] and st.launch_fail("P0")[0] == 1
    assert time.time() - st.launch_fail("P0")[1] < 5
    with sup._launch_lock:
        sup._launching.add("P0")
    sup._launch_worker("P0")
    assert state_mod.read(cfg).launch_fails == {}  # a success clears it there too


def test_a_resume_clears_the_published_pause(sup, cfg):
    _fail(sup, "P0", n=sup_mod.LAUNCH_GIVE_UP)
    sup._publish_launches()
    assert state_mod.read(cfg).launch_fail("P0")[0] == sup_mod.LAUNCH_GIVE_UP
    sup._on_resume()
    assert "P0" not in state_mod.read(cfg).launch_fails


def test_a_raising_launch_frees_its_claim(sup, cfg, monkeypatch):
    def boom(c, phase, log, **_):
        with state_mod.transaction(c) as st:
            st.claim_slot(phase)
        raise OSError("disk full")

    monkeypatch.setattr(launch_mod, "launch_outcome", boom)
    monkeypatch.setattr(launch_mod, "_poke_fifo", lambda c, line: True)
    sup._launch_worker("P0")
    assert state_mod.read(cfg).busy_slots() == []
    assert "LAUNCH-ERROR P0" in _log(cfg)


# -- the finish ----------------------------------------------------------------
def test_finish_holds_while_a_launch_is_in_flight_or_a_phase_is_ready(sup, cfg):
    with state_mod.transaction(cfg) as st:
        st.done = {p: "ok" for p in ("P0", "P1", "P2", "P3", "P4")}
    with sup._launch_lock:
        sup._launching.add("P9")
    sup._finish_if_settled()
    assert not state_mod.read(cfg).finished  # launching
    with sup._launch_lock:
        sup._launching.clear()
    sup._finish_if_settled()
    assert not state_mod.read(cfg).finished  # P9 is ready: the launcher owns it
    with state_mod.transaction(cfg) as st:
        st.done["P9"] = "ok"
        st.push_owed = {"/repo": {"phase": "P9"}}
    sup._finish_if_settled()
    assert not state_mod.read(cfg).finished  # a push origin does not have yet
    assert "FINISH-HELD push-owed" in _log(cfg)
    with state_mod.transaction(cfg) as st:
        st.push_owed = {}
    sup._finish_if_settled()
    assert state_mod.read(cfg).finished
    assert "FINISH settled" in _log(cfg)


def test_the_last_done_finishes_the_run_without_a_master(sup, cfg, tmp_path):
    with state_mod.transaction(cfg) as st:
        st.done = {p: "ok" for p in ("P0", "P1", "P2", "P3", "P4")}
        st.claim_slot("P9")
    sup._advance_done("P9", "ok")
    assert state_mod.read(cfg).finished
    assert any("The run has finished: 6 phase(s) landed" in ln for ln in _tg(tmp_path))


# -- the real thing: threads, the FIFO poke, a racing manual launch ----------------
@pytest.mark.real_launch
def test_real_launches_run_on_threads_and_a_racing_manual_launch_is_refused(sup, cfg):
    picks = sup._fill_slots("test")
    assert picks == ["P0", "P1", "P2", "P3"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with sup._launch_lock:
            if not sup._launching:
                break
        time.sleep(0.05)
    st = state_mod.read(cfg)
    assert sorted(s.phase for s in st.busy_slots()) == picks
    log = Log(cfg.supervisor_log)
    try:
        # `swarm launch P0` by hand while the supervisor's own launch holds it.
        assert launch_mod.launch_outcome(cfg, "P0", log) == launch_mod.DENIED
    finally:
        log.close()
    assert sorted(s.phase for s in state_mod.read(cfg).busy_slots()) == picks
    assert _log(cfg).count("LAUNCH P0 ") == 1


def test_a_supervisor_launch_prints_nothing_but_the_cli_form_does(cfg, capsys):
    log = Log(cfg.supervisor_log)
    try:
        assert launch_mod.launch_outcome(cfg, "P9", log, quiet=True) == launch_mod.DENIED
        assert capsys.readouterr().out == ""
        assert launch_mod.launch(cfg, "P9", log) is False
        assert "LAUNCH-DENIED P9" in capsys.readouterr().out
    finally:
        log.close()


# -- boot readiness: 45 s, and one retry before the launch fails ---------------
def test_ready_timeout_is_45s():
    assert launch_mod.READY_TIMEOUT_S == 45.0


@pytest.mark.parametrize("ready, ok, respawns", [([False, True], True, 2), ([False, False], False, 2)])
def test_a_slow_boot_is_respawned_once_before_failing(cfg, monkeypatch, ready, ok, respawns):
    calls: list[str] = []
    answers = iter(ready)
    monkeypatch.setattr(launch_mod.tmux, "respawn_pane", lambda pane, cmd, env=None: calls.append(pane))
    monkeypatch.setattr(launch_mod, "_await_ready", lambda c, pane, log: next(answers))
    monkeypatch.setattr(launch_mod.tmux, "send_submit", lambda pane, text: True)
    log = Log(cfg.supervisor_log)
    try:
        assert launch_mod._launch_tmux(cfg, "P0", "%7", None, log) is ok
    finally:
        log.close()
    assert calls == ["%7"] * respawns
    assert "READY-RETRY P0 pane=%7" in _log(cfg)


def test_a_vanished_pane_fails_the_launch_instead_of_raising(cfg, monkeypatch):
    import subprocess

    def gone(pane, cmd, env=None):
        raise subprocess.CalledProcessError(1, ["tmux"])

    monkeypatch.setattr(launch_mod.tmux, "respawn_pane", gone)
    log = Log(cfg.supervisor_log)
    try:
        assert launch_mod._launch_tmux(cfg, "P0", "%7", None, log) is False
    finally:
        log.close()


# -- a live grow creates its panes --------------------------------------------
class _FakeTmux:
    """Windows of panes, enough for split/new-window/list/tag."""

    def __init__(self, windows: dict[str, int]) -> None:
        self.wins = {w: [f"%{w[1:]}{i}" for i in range(n)] for w, n in windows.items()}
        self.tags: dict[str, int] = {}
        self.layouts: list[str] = []

    def list_panes(self, win):
        return list(self.wins.get(win, []))

    def split_one(self, win, hold="sleep infinity"):
        pane = f"%{win[1:]}{len(self.wins[win])}"
        self.wins[win].append(pane)
        return pane

    def new_window(self, session, name, hold="sleep infinity"):
        win = f"@{len(self.wins) + 1}"
        self.wins[win] = [f"%{win[1:]}0"]
        return win

    def apply_layout(self, win, count, layout="auto"):
        self.layouts.append(win)

    def set_slot(self, pane, slot):
        self.tags[pane] = slot


def test_add_slot_panes_fills_the_last_window_then_opens_the_next(cfg, monkeypatch):
    from swarm_orchestrator import session

    fake = _FakeTmux({"@1": 3})
    for name in ("list_panes", "split_one", "new_window", "apply_layout", "set_slot"):
        monkeypatch.setattr(session.tmux, name, getattr(fake, name))
    panes, windows, failed = session.add_slot_panes(
        cfg, {"dash": "@0", "workers": "@1"}, [3, 4, 5], "auto"
    )
    assert failed == []
    assert panes[3] in fake.wins["@1"]  # the 4th pane of `workers`
    assert windows["workers-2"] == "@2"  # the 5th slot opened a new window
    assert panes[4] == "%20" and panes[5] in fake.wins["@2"]
    assert fake.tags == {panes[3]: 3, panes[4]: 4, panes[5]: 5}


def test_a_reload_grow_records_the_new_panes(tmp_path, monkeypatch):
    """`max_workers 2 -> 4` used to append slots with ``pane_id=None``, and every
    launch into one failed ``no-pane``. A slot tmux would not give a pane is
    dropped rather than left in the pool to fail launches."""
    from swarm_orchestrator.config import load

    _bare_cfg(tmp_path, monkeypatch, LEDGER)
    monkeypatch.setenv("SWARM_DRIVER", "tmux")
    (tmp_path / ".swarm.toml").write_text("[swarm]\nmax_workers = 2\n", encoding="utf-8")
    cfg = load(project_dir=str(tmp_path))
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.windows = {"workers": "@1"}
        st.slots[0].pane_id, st.slots[1].pane_id = "%10", "%11"
    (tmp_path / ".swarm.toml").write_text("[swarm]\nmax_workers = 4\n", encoding="utf-8")

    asked: list[list[int]] = []

    def fake_add(c, windows, slot_ids, layout):
        asked.append(list(slot_ids))
        return {slot_ids[0]: "%12"}, dict(windows), slot_ids[1:]

    monkeypatch.setattr(sup_mod.session_mod, "add_slot_panes", fake_add)
    sup = Supervisor(cfg)
    pings: list[str] = []
    sup._fold = lambda key, msg, *a, **k: pings.append(msg)
    try:
        sup._on_reload()
    finally:
        sup.log.close()
    st = state_mod.read(cfg)
    assert asked == [[2, 3]]
    assert [(s.id, s.pane_id) for s in st.slots] == [(0, "%10"), (1, "%11"), (2, "%12")]
    assert "RELOAD-PANES" in _log(cfg)
    assert pings and "only 1 of the 2 extra worker place(s)" in pings[0]


# -- doctor: every slot's pane, not only the busy ones ---------------------------
def test_doctor_flags_a_free_slot_with_no_pane(cfg, monkeypatch):
    from swarm_orchestrator import doctor

    cfg.driver = "tmux"
    monkeypatch.setattr(doctor, "_pane_cmd", lambda pane: {"%1": "sleep", "%2": "gone"}[pane])
    st = state_mod.State.fresh(4)
    st.slots[0].pane_id, st.slots[1].pane_id = "%1", "%2"
    probe = doctor._dead_panes(cfg, st)
    check = doctor._check_panes(cfg, st, probe)
    assert check.status == doctor.FAIL
    assert "free slot 1 pane %2 is gone" in check.detail
    assert "free slot 2 has no pane" in check.detail and "free slot 3" in check.detail
    assert "free slot 0" not in check.detail  # a sleeping placeholder is healthy
    assert doctor._check_watchdog(cfg, probe).status == doctor.OK  # not a worker


def test_doctor_is_quiet_when_every_free_slot_has_its_pane(cfg, monkeypatch):
    from swarm_orchestrator import doctor

    cfg.driver = "tmux"
    monkeypatch.setattr(doctor, "_pane_cmd", lambda pane: "sleep")
    st = state_mod.State.fresh(2)
    st.slots[0].pane_id, st.slots[1].pane_id = "%1", "%2"
    assert doctor._check_panes(cfg, st, doctor._dead_panes(cfg, st)).status == doctor.OK

