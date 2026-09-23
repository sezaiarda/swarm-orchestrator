"""Tests for ``swarm doctor`` and the cockpit's health tab.

Every check in :mod:`doctor` exists because its failure mode produced **no log
line at all**: a recorded supervisor pid that outlived its process, a FIFO nobody
reads, a busy slot whose ``/prime`` never arrived, a held merge queue that
telegrammed once and went quiet, a ``notify.sh`` that failed on every send. So
each check is tested from both sides — it fires on the fault, and it stays ``ok``
on the healthy look-alike — because a doctor that cries wolf is the one the owner
learned to ignore, which is how a real stall goes unnoticed.

The checks are driven one at a time against a real :class:`Config` over a temp
project and state dir: real FIFOs, real ``/proc`` readership, a real ``git init``
for the activity probe. Only ``tmux`` and ``du`` are stubbed. The health tab is
exercised through a Textual pilot with ``swarm doctor`` replaced by a canned
reply, plus one contract test that feeds it the real CLI's JSON.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm_orchestrator import doctor, launch
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import telegram
from swarm_orchestrator.config import load
from swarm_orchestrator.doctor import FAIL, OK, WARN, Check

LEDGER = "P0\nP1 needs:P0\nP2 needs:P0\n"


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_SLUG", "doctortest")
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.delenv("CARGO_INCREMENTAL", raising=False)
    for leak in ("SWARM_MASTER_CMD", "SWARM_WORKER_CMD", "SWARM_SESSION", "SWARM_LAYOUT"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    return c


def set_state(cfg, slots: int = 2, **kw) -> state_mod.State:
    """Reset the run to a clean ``slots``-wide state with the given overrides."""
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(state_mod.State.fresh(slots).__dict__)
        for key, val in kw.items():
            setattr(st, key, val)
    return state_mod.read(cfg)


def busy(st: state_mod.State, slot: int, phase: str, **kw) -> None:
    s = st.slots[slot]
    s.busy, s.phase = True, phase
    for key, val in kw.items():
        setattr(s, key, val)


def save(cfg, st: state_mod.State) -> state_mod.State:
    with state_mod.transaction(cfg) as live:
        live.__dict__.update(st.__dict__)
    return state_mod.read(cfg)


def log(cfg, *entries: tuple[float, str]) -> None:
    """Append ``(seconds_ago, message)`` lines in the supervisor's own format."""
    now = time.time()
    with cfg.supervisor_log.open("a", encoding="utf-8") as fh:
        for ago, msg in entries:
            stamp = datetime.fromtimestamp(now - ago).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            fh.write(f"{stamp} 1.000 {msg}\n")


def by_name(checks: list[Check], name: str) -> Check:
    return next(c for c in checks if c.name == name)


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class Reader:
    """Hold the control FIFO open for reading, as a live supervisor does."""

    def __init__(self, fifo: Path) -> None:
        if not fifo.exists():
            os.mkfifo(fifo)
        self.fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        os.close(self.fd)


# -- supervisor liveness ----------------------------------------------------
def test_a_finished_run_with_its_supervisor_gone_is_healthy(cfg):
    """`finished` is the supervisor's own exit flag: a dead pid is the design."""
    st = set_state(cfg, finished=True, supervisor_pid=dead_pid())
    checks = doctor._check_supervisor(cfg, st)
    assert all(c.status == OK for c in checks), checks
    assert "exited by design" in by_name(checks, "supervisor.pid").detail


def test_no_pid_on_an_unfinished_run_fails(cfg):
    pid = by_name(doctor._check_supervisor(cfg, set_state(cfg)), "supervisor.pid")
    assert pid.status == FAIL and pid.fix_hint == "swarm up"


def test_a_dead_supervisor_mid_run_fails(cfg):
    st = set_state(cfg, supervisor_pid=dead_pid())
    busy(st, 0, "P0")
    checks = doctor._check_supervisor(cfg, save(cfg, st))
    assert by_name(checks, "supervisor.pid").status == FAIL
    assert "gone with work still in flight" in by_name(checks, "supervisor.pid").detail


def test_a_live_supervisor_that_is_not_reading_its_fifo_fails(cfg):
    os.mkfifo(cfg.fifo_path)
    checks = doctor._check_supervisor(cfg, set_state(cfg, supervisor_pid=os.getpid()))
    assert by_name(checks, "supervisor.pid").status == OK
    fifo = by_name(checks, "supervisor.fifo")
    assert fifo.status == FAIL
    assert "NO reader" in fifo.detail and "alive but not reading it" in fifo.detail


def test_a_missing_fifo_fails_only_while_the_run_is_live(cfg):
    live = by_name(doctor._check_supervisor(cfg, set_state(cfg, supervisor_pid=os.getpid())),
                   "supervisor.fifo")
    over = by_name(doctor._check_supervisor(cfg, set_state(cfg, finished=True)), "supervisor.fifo")
    assert live.status == FAIL and over.status == OK


def test_the_recorded_supervisor_holding_its_fifo_is_all_green(cfg):
    with Reader(cfg.fifo_path):
        checks = doctor._check_supervisor(cfg, set_state(cfg, supervisor_pid=os.getpid()))
    assert [c.status for c in checks] == [OK, OK, OK], checks
    assert "reader attached" in by_name(checks, "supervisor.fifo").detail


def test_a_second_supervisor_holding_the_fifo_is_a_stray(cfg):
    """Two readers race every event and each poke reaches only one of them."""
    other = subprocess.Popen(["sleep", "30"])
    try:
        with Reader(cfg.fifo_path):
            checks = doctor._check_supervisor(cfg, set_state(cfg, supervisor_pid=other.pid))
    finally:
        other.kill()
        other.wait()
    stray = by_name(checks, "supervisor.stray")
    assert stray.status == FAIL and f"NOT pid {other.pid}" in stray.detail


def test_a_reader_behind_a_dead_recorded_pid_is_a_stray(cfg):
    with Reader(cfg.fifo_path):
        st = set_state(cfg, supervisor_pid=dead_pid())
        busy(st, 0, "P0")
        checks = doctor._check_supervisor(cfg, save(cfg, st))
    stray = by_name(checks, "supervisor.stray")
    assert stray.status == FAIL and "co-opted" in stray.detail


# -- panes and the watchdog -------------------------------------------------
def panes(cfg, monkeypatch, cmds: dict[str, str], watchdog: int = 0):
    cfg.driver = "tmux"
    cfg.watchdog_s = watchdog
    monkeypatch.setattr(doctor, "_pane_cmd", lambda pane: cmds[pane])
    st = set_state(cfg)
    for i, pane in enumerate(cmds):
        busy(st, i, f"P{i + 1}", pane_id=pane)
    st = save(cfg, st)
    probe = doctor._dead_panes(cfg, st)
    return doctor._check_panes(cfg, st, probe), doctor._check_watchdog(cfg, probe)


def test_a_pane_no_longer_running_claude_is_a_dead_worker(cfg, monkeypatch):
    pane, _ = panes(cfg, monkeypatch, {"%1": "claude", "%2": "bash"}, watchdog=300)
    assert pane.status == FAIL
    assert "slot 1 (P2) pane %2 runs 'bash'" in pane.detail
    assert pane.fix_hint.startswith("swarm free ")


def test_a_dead_pane_with_no_watchdog_will_never_be_reclaimed(cfg, monkeypatch):
    _, watchdog = panes(cfg, monkeypatch, {"%1": "gone"}, watchdog=0)
    assert watchdog.status == FAIL and "watchdog_s = 300" in watchdog.fix_hint


def test_the_watchdog_makes_a_dead_pane_survivable(cfg, monkeypatch):
    _, watchdog = panes(cfg, monkeypatch, {"%1": "gone"}, watchdog=120)
    assert watchdog.status == OK and "will be reaped" in watchdog.detail


def test_an_unreadable_pane_is_not_reported_as_dead(cfg, monkeypatch):
    """A failed tmux probe is a non-answer, never a wrong answer."""
    pane, watchdog = panes(cfg, monkeypatch, {"%1": "?", "%2": "claude"})
    assert pane.status == OK and "1 pane(s) unreadable" in pane.detail
    assert watchdog.status == OK


def test_panes_are_not_probed_off_tmux(cfg, monkeypatch):
    monkeypatch.setattr(doctor, "_pane_cmd", lambda pane: pytest.fail("probed tmux"))
    st = set_state(cfg)
    busy(st, 0, "P1", pane_id="%1")
    st = save(cfg, st)
    probe = doctor._dead_panes(cfg, st)
    assert doctor._check_panes(cfg, st, probe).status == OK


# -- the lost-/prime signature ----------------------------------------------
def worktree(tmp_path: Path, *, dirty: bool) -> Path:
    wt = tmp_path / "wt" / "P1"
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    if dirty:
        (wt / "parser.rs").write_text("fn main() {}\n")
    return wt


def activity(cfg, wt: Path, launched_ago: float) -> Check:
    cfg.git_main_branch = "main"
    log(cfg, (launched_ago, "LAUNCH P1 slot=0"))
    st = set_state(cfg)
    busy(st, 0, "P1", worktree=str(wt))
    return doctor._check_activity(cfg, save(cfg, st))


def test_a_long_busy_slot_that_wrote_nothing_is_a_lost_prime(cfg, tmp_path):
    check = activity(cfg, worktree(tmp_path, dirty=False), launched_ago=30 * 60)
    assert check.status == FAIL
    assert "P1 (30m, no commits, no dirty files)" in check.detail
    assert "/prime P1" in check.fix_hint


def test_a_busy_slot_with_written_work_is_fine(cfg, tmp_path):
    assert activity(cfg, worktree(tmp_path, dirty=True), launched_ago=30 * 60).status == OK


def test_a_fresh_launch_gets_its_grace(cfg, tmp_path):
    assert activity(cfg, worktree(tmp_path, dirty=False), launched_ago=60).status == OK


def test_a_worktree_git_cannot_read_is_not_reported(cfg, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert activity(cfg, plain, launched_ago=30 * 60).status == OK


# -- integration hold -------------------------------------------------------
def held(cfg, *entries) -> Check:
    log(cfg, *entries)
    st = set_state(cfg, integ_blocked="P1", integ_blocked_kind="conflict",
                   integ_blocked_repo="/work/frontend", integ_queue=["P1", "P2"])
    return doctor._check_integration(cfg, st)


def test_a_fresh_hold_warns_with_its_age_and_repo(cfg):
    check = held(cfg, (60, "INTEGRATE-BLOCKED P1 conflict"))
    assert check.status == WARN
    assert "conflict in frontend" in check.detail and "for 60s" in check.detail
    assert check.fix_hint == "swarm resolved P1"


def test_an_old_hold_fails(cfg):
    assert held(cfg, (15 * 60, "INTEGRATE-BLOCKED P1 conflict")).status == FAIL


def test_a_hold_of_unknown_age_fails_rather_than_guessing_young(cfg):
    check = held(cfg)
    assert check.status == FAIL and "for unknown" in check.detail


def test_no_hold_is_ok(cfg):
    assert doctor._check_integration(cfg, set_state(cfg)).status == OK


# -- run-level races ---------------------------------------------------------
def test_finished_with_ready_phases_is_the_finish_race():
    st = state_mod.State.fresh(2)
    st.finished = True
    check = doctor._check_finish_race(st, ["P3"])
    assert check.status == FAIL and "P3" in check.detail


def test_free_slots_and_ready_work_with_nothing_launched_is_a_lost_nudge():
    st = state_mod.State.fresh(2)
    check = doctor._check_nudge(st, ["P1", "P2"], [0, 1])
    assert check.status == FAIL and check.fix_hint == "swarm launch P1"


@pytest.mark.parametrize("flag", ["paused", "finished", "integ_blocked"])
def test_a_quiet_swarm_with_a_reason_is_not_a_lost_nudge(flag):
    st = state_mod.State.fresh(2)
    setattr(st, flag, "P1" if flag == "integ_blocked" else True)
    assert doctor._check_nudge(st, ["P1"], [0]).status == OK


def test_a_long_silence_with_work_in_flight_warns(cfg):
    st = set_state(cfg, last_event_at=time.time() - 2 * 3600)
    busy(st, 0, "P1")
    assert doctor._check_stall(cfg, save(cfg, st)).status == WARN


def test_silence_with_nothing_in_flight_is_fine(cfg):
    st = set_state(cfg, last_event_at=time.time() - 9 * 3600)
    assert doctor._check_stall(cfg, st).detail == "nothing in flight"


# -- the owner as the blocker -----------------------------------------------
def test_a_waiting_worker_shows_its_question_from_the_ledger(cfg):
    """`swarm waiting` stores the question nowhere but the telegram it sends."""
    launch.waiting(cfg, "P1", "which  schema\nshould I use?")
    asked = time.time() - 20 * 60
    st = set_state(cfg, waiting={"P1": asked + cfg.park_after})
    check = doctor._check_owner(cfg, st)
    assert check.status == WARN
    assert 'P1 waiting 20m: "which schema should I use?"' in check.detail
    assert "swarm resumed P1" in check.fix_hint


def test_a_recent_question_is_reported_but_not_a_warning(cfg):
    st = set_state(cfg, waiting={"P1": time.time() - 60 + cfg.park_after})
    check = doctor._check_owner(cfg, st)
    assert check.status == OK and "you are the blocker" in check.detail


def test_a_parked_worker_ages_from_its_park_line(cfg):
    log(cfg, (40 * 60, "PARK P2 window=@3"))
    check = doctor._check_owner(cfg, set_state(cfg, parked=["P2"]))
    assert check.status == WARN and "P2 parked 40m" in check.detail


# -- ledger, telegram, disk --------------------------------------------------
@pytest.mark.parametrize(
    ("text", "status"),
    [(LEDGER, OK), ("A needs:B\nB needs:A\n", FAIL), ("A needs:Z\n", FAIL), ("", OK)],
)
def test_ledger_structure(cfg, text, status):
    (cfg.project_dir / cfg.ledger).write_text(text, encoding="utf-8")
    assert doctor._check_ledger(cfg).status == status


def test_a_run_that_never_logged_a_send_warns(cfg):
    config, sends = doctor._check_telegram(cfg)
    assert config.status == OK and config.detail == "sink"
    assert sends.status == WARN


def test_delivered_sends_are_ok(cfg):
    telegram.notify(cfg.telegram_notify, "one", state_dir=cfg.state_dir)
    telegram.notify(cfg.telegram_notify, "two", state_dir=cfg.state_dir)
    sends = doctor._check_telegram(cfg)[1]
    assert sends.status == OK and "2 send(s) logged, all delivered" in sends.detail


def test_one_dropped_send_fails_with_its_error(cfg):
    telegram.notify(cfg.telegram_notify, "one", state_dir=cfg.state_dir)
    with (cfg.state_dir / telegram.LEDGER_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "delivered": False, "error": "429 Too Many"}) + "\n")
        fh.write("{torn line\n")
    sends = doctor._check_telegram(cfg)[1]
    assert sends.status == FAIL
    assert "1/2 sends were DROPPED" in sends.detail and "429 Too Many" in sends.detail


def test_a_big_state_dir_warns_and_leaves_a_sample(cfg, monkeypatch):
    monkeypatch.setattr(doctor, "_dir_size", lambda path: 50 * 1024**3)
    disk = doctor._check_disk(cfg)[0]
    assert disk.status == WARN and str(cfg.build_cache_dir) in disk.fix_hint
    sample = json.loads((cfg.state_dir / ".doctor-disk.json").read_text())
    assert sample["bytes"] == 50 * 1024**3


def test_growth_is_measured_against_the_previous_run(cfg, monkeypatch):
    """One reading cannot tell a stable 60 GiB cache from one doubling overnight."""
    (cfg.state_dir / ".doctor-disk.json").write_text(
        json.dumps({"ts": time.time() - 3600, "bytes": 0})
    )
    monkeypatch.setattr(doctor, "_dir_size", lambda path: 6 * 1024**3)
    disk = doctor._check_disk(cfg)[0]
    assert disk.status == WARN and "growing" in disk.detail


def test_a_small_steady_state_dir_is_fine(cfg, monkeypatch):
    monkeypatch.setattr(doctor, "_dir_size", lambda path: 1024)
    assert doctor._check_disk(cfg)[0].status == OK


def test_a_du_that_times_out_is_unknown_not_a_fault(cfg, monkeypatch):
    monkeypatch.setattr(doctor, "_dir_size", lambda path: None)
    disk = doctor._check_disk(cfg)[0]
    assert disk.status == OK and "unknown" in disk.detail
    assert not (cfg.state_dir / ".doctor-disk.json").exists()


def test_inherited_cargo_incremental_warns(cfg, monkeypatch):
    monkeypatch.setenv("CARGO_INCREMENTAL", "1")
    assert doctor._check_incremental(cfg).status == WARN


def test_stale_incremental_state_on_disk_warns(cfg, monkeypatch):
    (cfg.build_cache_dir / "frontend" / "debug" / "incremental").mkdir(parents=True)
    monkeypatch.setattr(doctor, "_dir_size", lambda path: 7 * 1024**2)
    check = doctor._check_incremental(cfg)
    assert check.status == WARN and "7.0 MiB" in check.detail


def test_no_incremental_dead_weight_is_ok(cfg):
    assert doctor._check_incremental(cfg).status == OK


# -- sentinels, recaps, failures ---------------------------------------------
def sentinel(cfg, name: str, body: str = "") -> None:
    cfg.done_dir.mkdir(parents=True, exist_ok=True)
    (cfg.done_dir / name).write_text(body, encoding="utf-8")


def test_sentinels_agreeing_with_state(cfg):
    sentinel(cfg, "P0.ok", "P0 ok wrote it\n")
    sentinel(cfg, ".P1.fail.tmp")  # a torn write, not a sentinel
    sentinel(cfg, "P0.jsonl", "{}\n")  # history, not a sentinel
    assert doctor._check_sentinels(cfg, set_state(cfg, done={"P0": "ok"})).status == OK


def test_a_sentinel_state_disagrees_with_fails(cfg):
    """Phases the owner was asked to review were shown as clean successes."""
    sentinel(cfg, "P0.operator")
    check = doctor._check_sentinels(cfg, set_state(cfg, done={"P0": "ok"}))
    assert check.status == FAIL and "P0: sentinel=operator state=ok" in check.detail


def test_the_retired_spelling_is_not_canonicalised_on_read(cfg):
    """Canonicalising either side on read would flag a healthy dir as mismatched."""
    sentinel(cfg, "P0.needs-owner")
    assert doctor._check_sentinels(cfg, set_state(cfg, done={"P0": "needs-owner"})).status == OK


def test_a_sentinel_missing_from_state_warns(cfg):
    sentinel(cfg, "P2.ok")
    check = doctor._check_sentinels(cfg, set_state(cfg))
    assert check.status == WARN and "swarm up" in check.fix_hint


def test_a_forced_recap_replacement_warns_and_names_the_phase(cfg):
    sentinel(cfg, "P1.jsonl", json.dumps({"verdict": "written"}) + "\nnot json\n"
             + json.dumps({"verdict": "forced"}) + "\n")
    check = doctor._check_recaps(cfg)
    assert check.status == WARN and "['P1']" in check.detail


def test_a_refused_thin_recap_is_the_guard_working(cfg):
    sentinel(cfg, "P1.jsonl", json.dumps({"verdict": "refused"}) + "\n")
    check = doctor._check_recaps(cfg)
    assert check.status == OK and "1 thinner re-report(s) refused" in check.detail


def test_a_failed_phase_warns_with_a_relaunch_hint():
    st = state_mod.State.fresh(1)
    st.done = {"P0": "ok", "P2": "fail"}
    check = doctor._check_failed(st)
    assert check.status == WARN and check.fix_hint.startswith("swarm launch P2")


def test_the_prompts_ship_with_this_checkout():
    assert doctor._check_prompts().status == OK


# -- the whole run -----------------------------------------------------------
def test_an_unreadable_state_is_one_failure_not_a_page_of_guesses(cfg, monkeypatch):
    monkeypatch.setattr(doctor.time, "sleep", lambda s: None)
    cfg.state_path.write_text("{not json", encoding="utf-8")
    checks = doctor.run_checks(cfg)
    assert [(c.name, c.status) for c in checks] == [("state", FAIL)]


def test_run_checks_reads_in_a_fixed_order_and_never_raises(cfg, monkeypatch):
    monkeypatch.setattr(doctor, "_dir_size", lambda path: 0)
    (cfg.project_dir / cfg.ledger).unlink()  # no ledger at all
    names = [c.name for c in doctor.run_checks(cfg)]
    assert names == [
        "supervisor.pid", "supervisor.fifo", "supervisor.stray", "slots.panes",
        "run.watchdog", "slots.activity", "integration.blocked", "integration.push",
        "run.finished",
        "run.nudge", "run.stall", "owner.blocking", "ledger", "telegram.config",
        "telegram.sends", "disk.state", "disk.incremental", "disk.tmp", "sentinels",
        "recaps.history", "phases.failed", "operator", "prompts",
    ]


def test_warnings_alone_exit_zero():
    """A busy swarm always has something to glance at; that is not an error."""
    warn = [Check("a", OK, ""), Check("b", WARN, "look")]
    assert doctor.exit_code(warn) == 0 and doctor.worst(warn) == WARN
    assert doctor.exit_code(warn + [Check("c", FAIL, "")]) == 1
    assert doctor.worst([]) == OK


def test_render_keeps_the_fix_on_its_own_line_and_only_for_problems():
    out = doctor.render([
        Check("supervisor.pid", FAIL, "gone", "swarm up"),
        Check("ledger", OK, "fine", "never shown"),
        Check("run.stall", WARN, "quiet"),
    ])
    lines = out.splitlines()
    assert lines[0].startswith("FAIL supervisor.pid") and lines[1].strip() == "-> swarm up"
    assert "never shown" not in out
    assert out.endswith("1 failing, 1 warning, 3 checks total")
    assert doctor.render([Check("x", OK, "")]).endswith("all 1 checks pass")


# -- the CLI ----------------------------------------------------------------
def test_swarm_doctor_on_a_run_that_never_started(swarm):
    text = swarm.cli("doctor", check=False)
    assert text.returncode == 1
    assert "FAIL supervisor.pid" in text.stdout and "-> swarm up" in text.stdout

    raw = swarm.cli("doctor", "--json", check=False)
    rows = json.loads(raw.stdout)
    assert {"name", "status", "detail", "fix_hint"} <= set(rows[0])
    assert by_name([Check(**r) for r in rows], "supervisor.pid").status == FAIL


def test_swarm_doctor_on_a_finished_run_is_clean(swarm):
    # Outlast the fake master's 1s idle wait, as test_lifecycle does: at equal
    # timings its `master-idle` races P0's `done` into the accepted
    # FINISH-WITH-READY path, which doctor rightly reports as a failure.
    swarm.env["FAKE_WORKER_SLEEP"] = "2"
    swarm.up()
    assert swarm.wait(lambda: "ACTION finish" in swarm.log_text(), timeout=40), swarm.log_text()
    assert set(swarm.state()["done"]) >= {"P0", "P1", "P2", "P3", "P4"}
    out = swarm.cli("doctor", check=False)
    assert out.returncode == 0, out.stdout
    assert "FAIL" not in out.stdout


def test_swarm_doctor_json_keeps_the_exit_convention(swarm):
    assert swarm.cli("doctor", "--json", check=False).returncode == 1


# -- the health tab -----------------------------------------------------------
CANNED = [
    {"name": "supervisor.pid", "status": "fail", "detail": "gone", "fix_hint": "swarm up"},
    {"name": "run.stall", "status": "warn", "detail": "quiet", "fix_hint": "look"},
    {"name": "ledger", "status": "ok", "detail": "fine", "fix_hint": "hidden hint"},
]
ALL_OK = [{"name": "ledger", "status": "ok", "detail": "fine", "fix_hint": None}]


def drive(cfg, steps, replies, size=(120, 30)):
    """Boot a bare app holding one health tab; ``replies`` answers each doctor call.

    Each reply is a ``CompletedProcess`` to return or an exception to raise, so
    the tab's worker thread never runs a real ``swarm``.
    """
    from textual.app import App

    from swarm_orchestrator.tui import doctor as tab_mod

    calls: list[list[str]] = []
    queue = list(replies)

    def fake_run(args, **kw):
        calls.append(list(args))
        reply = queue.pop(0)
        if callable(reply):
            reply = reply()
        if isinstance(reply, BaseException):
            raise reply
        return reply

    class Host(App):
        def __init__(self):
            super().__init__()
            self.cfg = cfg

        def compose(self):
            yield tab_mod.Doctor(id="tab-doctor")

    app = Host()
    original = tab_mod.subprocess.run
    tab_mod.subprocess.run = fake_run
    try:
        async def run():
            async with app.run_test(size=size) as pilot:
                await steps(app, app.query_one(tab_mod.Doctor), pilot)

        asyncio.run(asyncio.wait_for(run(), timeout=30))
    finally:
        tab_mod.subprocess.run = original
    return calls


def reply(payload, rc: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    out = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.CompletedProcess(["swarm"], rc, out, stderr)


async def run_once(app, tab, pilot) -> None:
    tab.run_checks()
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def seen(tab) -> dict:
    from swarm_orchestrator.tui.theme import Body, Panel

    body = tab.query_one("#doctor-body", Body)
    panel = tab.query_one("#doctor-panel", Panel)
    return {
        "body": str(body.content),
        "subtitle": str(panel.border_subtitle or ""),
        "classes": set(panel.classes),
    }


def test_the_tab_never_runs_doctor_on_its_own(cfg):
    """It is open all day; a tmux+git+du sweep every 2s tick would be the cost."""
    got = {}

    async def steps(app, tab, pilot):
        for _ in range(3):
            tab.update(None)
        await pilot.pause()
        got.update(seen(tab))

    assert drive(cfg, steps, []) == []
    assert got["subtitle"] == "not run" and "press" in got["body"]


def test_the_tab_renders_every_check_and_hints_only_problems(cfg):
    got = {}

    async def steps(app, tab, pilot):
        await run_once(app, tab, pilot)
        got.update(seen(tab))

    calls = drive(cfg, steps, [reply(CANNED, rc=1)])
    assert calls == [["swarm", "doctor", "--json"]]
    assert got["subtitle"].startswith("1 failing · 1 warning · ran")
    for name in ("supervisor.pid", "run.stall", "ledger"):
        assert name in got["body"]
    assert "→ swarm up" in got["body"] and "→ look" in got["body"]
    assert "hidden hint" not in got["body"]
    assert "-bad" in got["classes"]


def test_the_tab_accepts_a_checks_envelope(cfg):
    got = {}

    async def steps(app, tab, pilot):
        await run_once(app, tab, pilot)
        got.update(seen(tab))

    drive(cfg, steps, [reply({"checks": ALL_OK})])
    assert got["subtitle"].startswith("0 failing · 0 warning")
    assert "-ok" in got["classes"]


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (reply("", rc=2, stderr="Traceback: boom"), "Traceback: boom"),
        (reply(""), "doctor produced no output"),
        (FileNotFoundError("swarm"), "`swarm` is not on PATH"),
        (subprocess.TimeoutExpired(["swarm"], 120), "doctor timed out after 120s"),
        (reply("{not json"), "JSONDecodeError"),
    ],
)
def test_a_doctor_that_could_not_answer_says_why(cfg, answer, message):
    got = {}

    async def steps(app, tab, pilot):
        await run_once(app, tab, pilot)
        got.update(seen(tab))

    drive(cfg, steps, [answer])
    assert got["subtitle"] == "failed" and message in got["body"]


def test_a_second_press_while_running_is_ignored(cfg):
    gate = threading.Event()

    def slow():
        gate.wait(10)
        return reply(ALL_OK)

    async def steps(app, tab, pilot):
        tab.run_checks()
        await pilot.pause()
        assert seen(tab)["subtitle"] == "working"
        tab.run_checks()
        gate.set()
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(drive(cfg, steps, [slow, reply(ALL_OK)])) == 1


def test_the_tab_reads_the_real_cli_output(swarm):
    """The tab parses whatever `swarm doctor --json` prints; pin that contract."""
    raw = swarm.cli("doctor", "--json", check=False)
    got = {}

    async def steps(app, tab, pilot):
        await run_once(app, tab, pilot)
        got.update(seen(tab))

    drive(SimpleNamespace(project_dir=swarm.project), steps, [reply(raw.stdout, rc=raw.returncode)])
    assert got["subtitle"] != "failed"
    assert "supervisor.pid" in got["body"] and "prompts" in got["body"]


def test_the_border_recovers_when_the_swarm_does(cfg):
    got = {}

    async def steps(app, tab, pilot):
        await run_once(app, tab, pilot)
        await run_once(app, tab, pilot)
        got.update(seen(tab))

    drive(cfg, steps, [reply(CANNED, rc=1), reply(ALL_OK)])
    assert got["subtitle"].startswith("0 failing")
    assert "-bad" not in got["classes"]
