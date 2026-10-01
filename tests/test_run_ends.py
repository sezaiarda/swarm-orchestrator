"""A phase run in the history ends when its claim ends — with or without a report.

The history tab paired ``CLAIM``/``LAUNCH`` with ``EVENT done`` and nothing else,
so a claim that ended any other way (``swarm up`` rebuilding the slots, the
watchdog reaping a dead pane, ``swarm free``, a worker that never started) stayed
"running" forever: a history could show workers as running long after they died.
Two halves are tested here: every path that ends a claim writes its
``RUN-ENDED`` line, and the reader never says "running" without a busy slot in the
current state, whatever the log failed to record.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from swarm_orchestrator import cli, gitq, launch, logutil
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor
from swarm_orchestrator.tui import data
from swarm_orchestrator.tui.tables import history_status

LEDGER = "- [ ] `P0` · needs:—\n- [ ] `P1` · needs:—\n- [ ] `P2` · needs:—\n"
BASE = datetime(2026, 9, 26, 7, 0, 0).timestamp()


def log_text(*entries: tuple[float, str]) -> str:
    out = []
    for off, msg in entries:
        when = datetime.fromtimestamp(BASE + off)
        out.append(f"{when.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {1000.0 + off:.3f} {msg}\n")
    return "".join(out)


def history(text: str, **kw) -> list[data.PhaseRun]:
    return data.build_history(data.parse_events(text), **kw)


def by_phase(runs, phase):
    return [r for r in runs if r.phase == phase]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))  # never the network
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_SLUG", "runends")
    monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    c = load(project_dir=str(tmp_path))
    c.ensure_dirs()
    return c


def logged(cfg) -> str:
    return cfg.supervisor_log.read_text(encoding="utf-8") if cfg.supervisor_log.is_file() else ""


def state_dict(cfg) -> dict:
    return state_mod.read(cfg).to_dict()


# -- the writers: every way a claim ends leaves its line ------------------
def test_swarm_up_closes_every_claim_it_wipes(cfg):
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
        st.waiting["P1"] = 0.0
        st.parked.append("operator:P0-op2")  # not a phase: no run to close
    log = Log(cfg.supervisor_log)
    try:
        log.line("LAUNCH P0 slot=0")
        log.line("LAUNCH P1 slot=1")
        state_mod.init_state(cfg, log=log)
    finally:
        log.close()

    text = logged(cfg)
    assert "RUN-ENDED P0 reason=restart" in text
    assert "RUN-ENDED P1 reason=restart" in text
    assert "P0-op2" not in text.split("LAUNCH P1 slot=1", 1)[1]
    runs = history(text, state=state_dict(cfg))
    assert {r.phase: (r.status, r.why, r.running) for r in runs} == {
        "P0": (data.LOST, "restart", False),
        "P1": (data.LOST, "restart", False),
    }
    assert all(r.ended_at is not None for r in runs)


def test_init_state_without_a_log_writes_nothing(cfg):
    """Tests and tools rebuild state without a log; only `swarm up` passes one."""
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
    state_mod.init_state(cfg)
    assert "RUN-ENDED" not in logged(cfg)


def test_swarm_up_passes_its_log_to_init_state():
    import inspect

    assert "init_state(cfg, log=log" in inspect.getsource(cli.cmd_up)


class _R:
    def __init__(self, rc: int, out: str) -> None:
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_the_watchdog_reap_closes_the_run(cfg, monkeypatch):
    cfg.driver = "tmux"
    cfg.watchdog_s = 1
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
        st.slots[0].pane_id = "%42"
    monkeypatch.setattr(gitq, "set_aside", lambda c, p, l: None)
    # tmux lists no panes at all: the slot's %42 is gone.
    monkeypatch.setattr("swarm_orchestrator.tmux.run", lambda args, check=False: _R(0, ""))
    sup = Supervisor(cfg)
    try:
        sup.log.line("LAUNCH P0 slot=0")
        sup._watchdog_tick()
        sup._last_sweep = 0.0
        sup._watchdog_tick()  # confirmed dead -> reaped
    finally:
        sup.log.close()

    text = logged(cfg)
    assert "WATCHDOG-REAP P0" in text and "RUN-ENDED P0 reason=reaped" in text
    (run,) = history(text, state=state_dict(cfg))
    assert run.status == data.LOST and run.why == "reaped" and not run.running


@pytest.mark.parametrize("target", ["P0", "0"])
def test_swarm_free_closes_the_run(cfg, monkeypatch, target):
    monkeypatch.setattr(cli, "_poke", lambda c, verb: False)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
    log = Log(cfg.supervisor_log)
    log.line("LAUNCH P0 slot=0")
    log.close()

    assert cli.cmd_free(cfg, target) == 0
    text = logged(cfg)
    assert "RUN-ENDED P0 reason=freed" in text
    (run,) = history(text, state=state_dict(cfg))
    assert run.status == data.LOST and run.why == "freed"


def test_freeing_a_phase_that_held_nothing_writes_no_line(cfg, monkeypatch):
    monkeypatch.setattr(cli, "_poke", lambda c, verb: False)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.mark_done("P0", "ok")
    assert cli.cmd_free(cfg, "P0") == 0
    assert "RUN-ENDED" not in logged(cfg)


def test_swarm_skip_of_a_busy_phase_closes_the_run_as_skipped(cfg, monkeypatch):
    monkeypatch.setattr(cli, "_poke", lambda c, verb: False)
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
    log = Log(cfg.supervisor_log)
    log.line("LAUNCH P0 slot=0")
    log.close()

    cli.cmd_skip(cfg, "P0")
    text = logged(cfg)
    assert "RUN-ENDED P0 reason=skipped" in text
    sentinels = data.load_sentinels(cfg.done_dir)
    (run,) = history(text, sentinels=sentinels, state=state_dict(cfg))
    assert run.status == "skip" and not run.running  # the sentinel says how it ended


def test_a_worker_that_never_started_closes_its_run(cfg, monkeypatch):
    monkeypatch.setattr(launch, "_launch_bare", lambda *a, **k: False)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        assert launch.launch_outcome(cfg, "P0", log, quiet=True) == launch.FAILED
    finally:
        log.close()
    text = logged(cfg)
    assert "CLAIM P0 slot=0" in text and "RUN-ENDED P0 reason=launch-failed" in text
    (run,) = history(text, state=state_dict(cfg))
    assert run.status == data.LOST and run.why == "launch-failed"
    assert not state_mod.read(cfg).busy_slots()


def test_the_retired_slot_path_never_ends_a_live_claim(cfg):
    """A shrink only retires a busy slot; it is dropped after its `swarm done`."""
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.resize(2)
        st.reap_retired()
        st.claim_slot("P0")
        st.claim_slot("P1")
        st.resize(1)
        assert st.reap_retired() == []
        assert sorted(st.claimed_phases()) == ["P0", "P1"]


# -- the reader: running means a busy slot, nothing else -----------------
def busy_state(*phases: str, **kw) -> dict:
    slots = [{"id": i, "busy": True, "phase": p} for i, p in enumerate(phases)]
    return {"slots": slots, "parked": [], "waiting": {}, "integ_queue": [], **kw}


STALE = log_text(
    (0, "SUPERVISOR-START pid=1 driver=tmux"),
    (10, "CLAIM onyx-W2 slot=0"),
    (12, "LAUNCH onyx-W2 slot=0"),
    (20, "CLAIM mint-W1 slot=1"),
    (22, "LAUNCH mint-W1 slot=1"),
    (30, "CLAIM navy-W7 slot=2"),
    (31, "LAUNCH navy-W7 slot=2"),
    (40, "CLAIM list-W8 slot=3"),
    (41, "LAUNCH list-W8 slot=3"),
    (500, "SUPERVISOR-STOP"),
    (9000, "SUPERVISOR-START pid=2 driver=tmux"),
    (9100, "CLAIM perf-F34 slot=0"),
    (9105, "LAUNCH perf-F34 slot=0"),
)


def test_the_display_never_says_running_without_a_live_slot():
    runs = history(STALE, state=busy_state("perf-F34"))
    assert [r.phase for r in runs if r.running] == ["perf-F34"]
    assert all(history_status(r) != "running" for r in runs if r.phase != "perf-F34")


def test_in_flight_equals_the_busy_slots():
    state = busy_state("perf-F34", "list-W8")  # a slot still holding a pre-restart run
    runs = history(STALE, state=state)
    busy = {s["phase"] for s in state["slots"]}
    assert sum(1 for r in runs if r.running) == len(busy) - 1  # list-W8's run was wiped
    runs = history(STALE + log_text((9200, "CLAIM list-W8 slot=1"),
                                    (9201, "LAUNCH list-W8 slot=1")), state=state)
    assert {r.phase for r in runs if r.running} == busy


def test_a_restart_closes_what_it_wiped_at_the_restart():
    runs = history(STALE, state=busy_state("perf-F34"))
    (geo,) = by_phase(runs, "onyx-W2")
    assert geo.status == data.LOST and geo.why == "restart"
    assert geo.ended_at == pytest.approx(BASE + 9000, abs=0.01)
    assert history_status(geo) == "worker gone"


def test_a_ledger_ticked_stale_run_reads_as_done_elsewhere():
    runs = history(STALE, state=busy_state("perf-F34"), ticked={"onyx-W2", "mint-W1"})
    for phase in ("onyx-W2", "mint-W1"):
        (run,) = by_phase(runs, phase)
        assert run.status == "ledger" and history_status(run) == "done elsewhere"
        assert run.why == "restart"  # what ended the claim here is still known
    (other,) = by_phase(runs, "list-W8")
    assert history_status(other) == "worker gone"


def test_a_stale_run_that_left_a_sentinel_reads_as_that_sentinel():
    sentinels = {"navy-W7": data.Sentinel("navy-W7", "ok", "done", mtime=BASE + 400)}
    runs = history(STALE, sentinels=sentinels, state=busy_state("perf-F34"))
    (run,) = by_phase(runs, "navy-W7")
    assert run.status == "ok" and run.ended_at == pytest.approx(BASE + 400)
    assert not run.running


def test_a_sentinel_from_an_earlier_attempt_does_not_close_a_later_run():
    text = log_text((0, "LAUNCH P0 slot=0"))
    old = {"P0": data.Sentinel("P0", "fail", "", mtime=BASE - 100)}
    (run,) = history(text, sentinels=old, state={"slots": []})
    assert run.status == data.LOST
    (live,) = history(text, sentinels=old, state=busy_state("P0"))
    assert live.running and history_status(live) == "running"


def test_a_run_open_in_the_log_but_in_no_slot_has_no_growing_duration():
    text = log_text((0, "LAUNCH P0 slot=0"))
    (run,) = history(text, state={"slots": []})
    assert run.status == data.LOST and run.why == "stale"
    assert run.ended_at is None and run.duration_s is None


def test_parked_and_integrating_phases_are_held_not_running():
    text = log_text((0, "LAUNCH P0 slot=0"), (1, "LAUNCH P1 slot=1"))
    state = {"slots": [], "parked": ["P0"], "waiting": {}, "integ_queue": ["P1"]}
    runs = {r.phase: r for r in history(text, state=state)}
    assert history_status(runs["P0"]) == "parked" and not runs["P0"].running
    assert history_status(runs["P1"]) == "integrating" and not runs["P1"].running
    assert runs["P0"].duration_s is not None  # still going


def test_run_ended_closes_it_at_that_line():
    text = log_text((0, "LAUNCH P0 slot=0"), (60, "RUN-ENDED P0 reason=freed"),
                    (70, "LAUNCH P0 slot=0"), (90, "EVENT done P0 ok freed_slot=0 parked=False"))
    runs = history(text, state={"slots": []})
    assert [(r.status, r.why) for r in runs] == [("ok", ""), (data.LOST, "freed")]
    assert runs[1].duration_s == pytest.approx(60, abs=0.01)


def test_a_relaunch_without_an_end_line_keeps_both_attempts():
    """The old reader overwrote the first attempt, so it vanished from history."""
    text = log_text((0, "CLAIM P0 slot=0"), (1, "LAUNCH P0 slot=0"),
                    (50, "CLAIM P0 slot=1"), (51, "LAUNCH P0 slot=1"))
    runs = history(text, state=busy_state("P0"))
    assert [(r.running, r.status) for r in runs] == [(True, None), (False, data.LOST)]
    assert runs[0].started_at == pytest.approx(BASE + 51, abs=0.01)


def test_legacy_lines_that_freed_a_slot_close_the_run():
    text = log_text((0, "CLAIM P0 slot=0"), (5, "LAUNCH-FAIL P0 slot=0"),
                    (10, "LAUNCH P1 slot=1"), (20, "WATCHDOG-REAP P1 pane-dead"))
    runs = {r.phase: r for r in history(text, state={"slots": []})}
    assert (runs["P0"].why, runs["P1"].why) == ("launch-failed", "reaped")
    assert runs["P1"].ended_at == pytest.approx(BASE + 20, abs=0.01)


def test_without_state_an_open_run_still_reads_as_running():
    """Callers with no state (tools, old tests) keep the log-only reading."""
    (run,) = history(log_text((0, "LAUNCH P0 slot=0")))
    assert run.running


def test_the_history_detail_says_why_it_ended():
    from swarm_orchestrator.tui.tables import _ended_line

    runs = {r.phase: r for r in history(STALE, state=busy_state("perf-F34"),
                                        ticked={"onyx-W2"})}
    assert "ticked it since" in _ended_line(runs["onyx-W2"])
    assert "restarted" in _ended_line(runs["mint-W1"])
    assert _ended_line(runs["perf-F34"]) == ""


def test_the_board_never_lists_a_lost_run_as_finished():
    from types import SimpleNamespace

    from swarm_orchestrator.web.board import _activity

    runs = history(STALE, state=busy_state("perf-F34"), ticked={"onyx-W2"})
    out = _activity(SimpleNamespace(history=runs), [])
    assert [(f["id"], f["st"]) for f in out["finished"]] == [("onyx-W2", "done elsewhere")]


def test_run_ended_line_shape():
    class Sink:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def line(self, msg):
            self.lines.append(msg)

    sink = Sink()
    logutil.run_ended(sink, "P0", "freed")
    ev = data.parse_event(log_text((0, sink.lines[0])).strip())
    assert (ev.kind, ev.phase, ev.fields["reason"]) == ("run-ended", "P0", "freed")
