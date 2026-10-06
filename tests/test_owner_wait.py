"""Sessions that wait on the owner, and rows only the owner can do.

``swarm waiting`` is the one way a worker, an operator job or the Overseer says
it needs the owner; past ``park_after`` it is moved, alive, to a window of its
own and frees what it held. An operator job whose moment has not come goes back
in the queue (``--not-before``) instead of asking. Owner-run rows that hold other
rows up reach the owner's "Needs you" list and one ping.

All on the bare driver: parking is pure state there, and no session ever starts.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarm_orchestrator import ledger as ledger_mod
from swarm_orchestrator import operator as operator_mod
from swarm_orchestrator import opqueue, owner, telegram
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor
from swarm_orchestrator.tui import data as tui_data
from swarm_orchestrator.web import board

REPO = Path(__file__).resolve().parent.parent
JOB, NEXT = "op-S1", "op-S2"
KEY = f"operator:{JOB}"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "ledger.txt").write_text(
        "P0\nOWN needs:P0\nA needs:OWN\nB needs:A\nC needs:P0\n", encoding="utf-8")
    (project / ".swarm.toml").write_text(
        '[swarm]\ndriver = "bare"\nmax_workers = 1\n[tasks]\nledger = "ledger.txt"\n'
        'exclude = ["OWN"]\n[operator]\nenabled = true\n[worker]\npark_after = 60\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_BIN", "true")
    monkeypatch.setenv("SWARM_WATCHDOG", "0")
    for leak in ("SWARM_TRIAGE_CMD", "SWARM_DRIVER", "SWARM_OPERATOR", "SWARM_PARK_AFTER",
                 "SWARM_OPERATOR_JOB", "SWARM_OVERSEER_PASS", "SWARM_GIT_ISOLATION"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


@pytest.fixture
def log(cfg):
    lg = Log(cfg.supervisor_log)
    yield lg
    lg.close()


@pytest.fixture
def sup(cfg):
    s = Supervisor(cfg)
    yield s
    s.log.close()


def tg(cfg) -> str:
    path = Path(str(cfg.state_dir)).parent / "tg.log"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def running(cfg, log, job: str = JOB) -> None:
    opqueue.add_adhoc(cfg, f"brief of {job}", job)
    assert operator_mod.dispatch(cfg, job, log) is True


def park_now(sup, key: str) -> None:
    sup._on_waiting(key)
    with state_mod.transaction(sup.cfg) as st:
        st.waiting[key] = time.time() - 1
    sup._check_park_deadlines()


# -- where to answer ---------------------------------------------------------
def test_the_window_to_answer_in_is_known_before_and_after_the_park(cfg, monkeypatch):
    """The ask on the phone is the session's own two sentences; the window to
    answer in is what status, the drawer and the board show."""
    cfg.driver = "tmux"
    monkeypatch.setattr(owner.tmux, "window_name_of", lambda pane: "workers")
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0").pane_id = "%7"
    st = state_mod.read(cfg)
    assert owner.where(cfg, "P0", st) == "workers"
    assert owner.where(cfg, KEY, st) == "operator"
    with state_mod.transaction(cfg) as s:
        s.parked.append(KEY)
    assert owner.where(cfg, KEY, state_mod.read(cfg)) == "wait:op-S1"


def test_who_resolves_from_the_sessions_own_environment(cfg, monkeypatch):
    assert owner.resolve(cfg, "P0") == "P0"
    assert owner.resolve(cfg, KEY) == KEY
    monkeypatch.setenv(operator_mod.JOB_ENV, JOB)
    assert owner.resolve(cfg, JOB) == KEY
    with pytest.raises(owner.WaitError):
        owner.resolve(cfg, "overseer")
    monkeypatch.setenv("SWARM_OVERSEER_PASS", "20260927T100000Z")
    assert owner.resolve(cfg, "overseer") == "overseer:20260927T100000Z"


# -- an operator job parks like a worker ------------------------------------
def test_a_waiting_job_parks_and_frees_the_operator_window_for_the_next(cfg, log, sup):
    running(cfg, log)
    opqueue.add_adhoc(cfg, "brief of the next job", NEXT)
    owner.waiting(cfg, KEY, "which host?")
    assert state_mod.read(cfg).operator_phase == JOB

    park_now(sup, KEY)

    st = state_mod.read(cfg)
    assert st.parked == [KEY] and st.waiting == {}
    assert st.operator_phase == NEXT  # the queue moved on
    assert opqueue.load(cfg, JOB).state == opqueue.WAITING  # still alive, still owed
    assert JOB in operator_mod.blocking(cfg) and st.pending()
    assert f"PARK {KEY} window=wait:op-S1" in cfg.supervisor_log.read_text()


def test_a_parked_job_resumes_and_ends_without_touching_the_next_job(cfg, log, sup):
    running(cfg, log)
    opqueue.add_adhoc(cfg, "brief of the next job", NEXT)
    owner.waiting(cfg, KEY, "which host?")
    park_now(sup, KEY)

    assert owner.resumed(cfg, KEY, "staging") is True
    item = opqueue.load(cfg, JOB)
    # It runs on outside the operator window's lease: no hour-long reclaim.
    assert item.state == opqueue.RUNNING and item.lease_until > time.time() + opqueue.LEASE_S
    assert state_mod.read(cfg).operator_phase == NEXT

    opqueue.complete(cfg, JOB, "rolled on staging")
    sup._on_operator_done(JOB)

    st = state_mod.read(cfg)
    assert st.parked == [] and st.operator_phase == NEXT  # the next job's lease survives
    assert f"UNPARK {KEY}" in cfg.supervisor_log.read_text()


def test_a_job_answered_before_the_park_keeps_its_window(cfg, log, sup):
    running(cfg, log)
    owner.waiting(cfg, KEY, "which host?")
    sup._on_waiting(KEY)
    owner.resumed(cfg, KEY, "staging")
    sup._on_resumed(KEY)
    sup._check_park_deadlines()
    st = state_mod.read(cfg)
    assert st.waiting == {} and st.parked == [] and st.operator_phase == JOB


def test_a_parked_job_that_outlives_its_wait_lease_stops_holding_the_run(cfg, log, sup):
    running(cfg, log)
    owner.waiting(cfg, KEY, "which host?")
    park_now(sup, KEY)

    operator_mod.sweep(cfg, log, now=time.time() + opqueue.WAIT_LEASE_S + 60)

    assert opqueue.load(cfg, JOB).state == opqueue.QUEUED
    assert state_mod.read(cfg).parked == []


# -- not before -------------------------------------------------------------
def cli(cfg, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "swarm_orchestrator", "--project-dir", str(cfg.project_dir), *args],
        cwd=str(cfg.project_dir), capture_output=True, text=True, timeout=60,
    )


def test_operator_add_not_before_holds_the_job_until_then(cfg, log):
    result = cli(cfg, "operator-add", "--phase", "P0", "--not-before", "2d", "delete the kit")
    assert result.returncode == 0, result.stderr
    item = opqueue.load(cfg, "P0")
    assert item.run_after > time.time() + 86400
    assert operator_mod.sweep(cfg, log) is False
    assert opqueue.next_deadline(cfg) == item.run_after
    assert [i.phase for i in opqueue.ready(cfg, now=item.run_after + 1)] == ["P0"]
    assert cli(cfg, "operator-add", "--not-before", "someday", "x").returncode == 2


def test_a_job_whose_moment_has_not_come_goes_back_in_the_queue(cfg, log, sup):
    """Not a question to the owner, and not an attempt spent: the job reopens then."""
    running(cfg, log)
    result = cli(cfg, "operator-done", JOB, "kit kept until the 30th", "--not-before", "3d")
    assert result.returncode == 0, result.stderr
    item = opqueue.load(cfg, JOB)
    assert item.state == opqueue.QUEUED and item.attempts == 0
    assert item.run_after > time.time() + 2 * 86400
    assert item.last_error == "kit kept until the 30th"
    assert "kit kept until the 30th" in operator_mod.brief(cfg, item)
    assert tg(cfg) == ""  # nobody was asked anything
    sup._on_operator_done(JOB)
    assert state_mod.read(cfg).operator_phase is None
    assert operator_mod.sweep(cfg, log) is False  # not before its date


# -- rows only the owner can do ----------------------------------------------
def test_owner_rows_are_the_ready_ones_that_hold_rows_up():
    graph = {"P0": set(), "OWN": {"P0"}, "A": {"OWN"}, "B": {"A"}, "LEAF": {"P0"},
             "LATER": {"A"}}
    excluded = {"OWN", "LEAF", "LATER"}
    assert ledger_mod.owner_rows(graph, {}, excluded) == []  # P0 not landed yet
    done = {"P0": "ok"}
    # LEAF holds nothing up; LATER is not the owner's to do yet.
    assert ledger_mod.owner_rows(graph, done, excluded) == [("OWN", 2)]
    assert ledger_mod.owner_rows(graph, {**done, "OWN": "skip"}, excluded) == []


def test_an_owner_row_pings_once_when_it_starts_holding_rows_up(cfg, sup):
    sup._fill_slots("test")
    assert "OWN" not in tg(cfg)
    with state_mod.transaction(cfg) as st:
        st.done["P0"] = "ok"
    sup._fill_slots("P0 landed")
    sup._fill_slots("again")
    asks = [ln for ln in tg(cfg).splitlines() if "OWN" in ln]
    assert len(asks) == 1 and len(asks[0]) <= telegram.PHONE_MAX
    assert asks[0].startswith(f"[{cfg.name}] Asks you: Do OWN")
    assert asks[0].endswith(", then tick it in the ledger or run `swarm skip OWN`: only you can"
                            " do it, and 2 rows wait on it.")
    rows = [json.loads(ln) for ln in (cfg.state_dir / telegram.LEDGER_NAME).read_text().splitlines()]
    assert [(r["kind"], r["class"]) for r in rows if "OWN" in r["text"]] == [("owner-row", "ask")]
    # Across a restart too.
    Supervisor(cfg)._fill_slots("up")
    assert tg(cfg).count("Do OWN") == 1


def test_an_owner_row_is_in_needs_you_with_what_it_holds_up(cfg):
    state = json.loads((cfg.state_dir / "state.json").read_text())
    state["done"] = {"P0": "ok"}
    graph = ledger_mod.load(cfg.project_dir / cfg.ledger)
    snap = tui_data.build_snapshot(cfg, state, graph=graph)
    [b] = [b for b in snap.blockers if b.kind == "owner-row"]
    assert b.phase == "OWN" and "2 rows wait on it" in b.question
    col, extra = board._place("OWN", graph, snap.done, {"P0"}, {"OWN"}, {}, [], {}, [], snap,
                              {}, {}, {b.phase: b for b in snap.blockers}, {})
    assert col == board.NEEDS_YOU and "2 rows wait on it" in extra["q"]
