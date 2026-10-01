"""A parked session the owner has answered is working, not waiting on the owner.

Once a session is parked it stays in its own window until it finishes, so the
``parked`` list alone cannot say whether it still waits for an answer. State
carries that: ``answered`` when the owner answered it, ``asked`` when its
question has no answer yet. These tests drive the real supervisor (bare driver)
through ``waiting`` / park / ``resumed`` on a real state file with the clock
under the test's hand, and read the result the way the owner is told about it:
the Overseer's owner trigger, ``swarm doctor``'s ``owner.blocking`` and the
Overseer digest.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

from swarm_orchestrator import doctor, ovdigest, owner
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.doctor import OK, WARN
from swarm_orchestrator.state import State
from swarm_orchestrator.supervisor import Supervisor

PARK_AFTER = 120
FIRST = "which schema should I use?"
SECOND = "keep the old column or drop it?"


class Clock:
    """``time.time`` under the test's hand, starting at the real moment."""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(time, "time", c)
    return c


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    # One phase with a worker, one behind it: nothing else is ready to launch.
    (project / "ledger.txt").write_text("P0\nP1 needs:P0\n")
    (project / ".swarm.toml").write_text(
        '[swarm]\nmax_workers = 2\ndriver = "bare"\n[tasks]\nledger = "ledger.txt"\n'
        f"[worker]\npark_after = {PARK_AFTER}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_OVERSEER", "1")
    monkeypatch.setenv("SWARM_WATCHDOG", "0")
    for leak in ("SWARM_DRIVER", "SWARM_GIT_ISOLATION", "SWARM_MASTER_CMD", "SWARM_OVERSEER_PASS",
                 "SWARM_PARK_AFTER", "SWARM_OVERSEER_OWNER_WAIT"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


@pytest.fixture
def sup(cfg, clock):
    s = Supervisor(cfg)
    s._bootstrapped = True
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")  # a launched worker holding a slot
    yield s
    s.log.close()


def _look(sup) -> list[str]:
    """One look of the Overseer's trigger policy, as the supervisor takes after
    every event and on every wake. Returns the owner reasons it now holds."""
    sup.overseer.observe(state_mod.read(sup.cfg), time.time())
    return [r.text for r in sup.overseer.pending if r.key.startswith("owner:")]


def _asks(sup, key: str, question: str) -> None:
    """The session's ``swarm waiting``: the ping, then the supervisor's event."""
    owner.waiting(sup.cfg, key, question)
    sup._on_waiting(key)
    _look(sup)


def _answers(sup, key: str, answer: str) -> None:
    """The session's ``swarm resumed``: the record, then the supervisor's event."""
    owner.resumed(sup.cfg, key, answer)
    sup._on_resumed(key)


def _park(sup, clock, key: str = "P0", question: str = FIRST) -> float:
    """``key`` asks and is left unanswered until it is parked. Returns when it asked."""
    asked = time.time()
    _asks(sup, key, question)
    clock.advance(PARK_AFTER)
    sup._check_park_deadlines()
    assert state_mod.read(sup.cfg).parked == [key]
    _look(sup)
    return asked


def _digest(cfg) -> tuple[dict, str]:
    data = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0, now=time.time())
    return data, ovdigest.render(data)


def _owner_section(md: str) -> str:
    return md.split("## Waiting on the owner", 1)[1].split("\n## ", 1)[0]


# -- parked, then answered: it is working ------------------------------------
def test_an_answered_parked_session_is_not_waiting_on_the_owner(sup, cfg, clock):
    _park(sup, clock)
    clock.advance(60)
    _answers(sup, "P0", "the second one")
    _look(sup)
    clock.advance(cfg.overseer_owner_wait_s + 600)  # far past the owner-wait bound

    assert _look(sup) == []  # no Overseer pass is asked for
    st = state_mod.read(cfg)
    assert st.parked == ["P0"]  # still in its own window; nothing moved it
    check = doctor._check_owner(cfg, st)
    assert check.status == OK and check.detail.startswith("no worker is waiting on you")
    assert "working on your answer: P0 in wait:P0" in check.detail
    assert FIRST not in check.detail
    data, md = _digest(cfg)
    assert data["owner"] == []
    assert [a["who"] for a in data["owner_answered"]] == ["P0"]
    assert data["context"]["parked"] == [] and data["context"]["parked_working"] == ["P0"]
    assert "- nobody" in _owner_section(md) and FIRST not in md
    assert "P0 (wait:P0, answered" in _owner_section(md)
    # The starvation map: P1 waits behind a row that is being built, not one
    # parked on the owner.
    assert ovdigest.in_flight(st) == {"P0": "building"}
    assert [(b["phase"], b["kind"]) for b in data["starvation"]["blockers"]] == [("P0", "building")]


def test_a_parked_session_that_asks_again_is_timed_from_its_new_question(sup, cfg, clock):
    first = _park(sup, clock)
    clock.advance(600)
    _answers(sup, "P0", "the second one")
    clock.advance(20)  # no look falls between the answer and the next question
    second = time.time()
    owner.waiting(cfg, "P0", SECOND)
    sup._on_waiting("P0")
    assert second - first > 700

    clock.advance(cfg.overseer_owner_wait_s - 1)  # the first question is long past the bound
    assert _look(sup) == []
    clock.advance(1)
    assert _look(sup) == ["P0 has waited on the owner for 60m"]
    clock.advance(900)
    assert len(_look(sup)) == 1  # once for this question

    check = doctor._check_owner(cfg, state_mod.read(cfg))
    assert check.status == WARN
    assert f'P0 parked, asked 75m ago: "{SECOND}"' in check.detail
    assert FIRST not in check.detail and "swarm resumed P0" in check.fix_hint
    data, md = _digest(cfg)
    [q] = data["owner"]
    assert (q["who"], q["state"], q["question"]) == ("P0", "parked", SECOND)
    assert q["age_s"] == pytest.approx(time.time() - second)
    assert data["owner_answered"] == []
    st = state_mod.read(cfg)
    assert ovdigest.in_flight(st) == {"P0": "parked"}
    assert st.asking("P0") and st.asked_at("P0", cfg.park_after) == second


def test_the_owner_trigger_fires_once_per_unanswered_question(sup, cfg, clock):
    _park(sup, clock)
    clock.advance(cfg.overseer_owner_wait_s)
    assert len(_look(sup)) == 1  # the first question went unanswered too long
    sup.overseer.begin(time.time())  # a pass takes the reason
    _answers(sup, "P0", "the second one")
    owner.waiting(cfg, "P0", SECOND)
    sup._on_waiting("P0")
    assert _look(sup) == []  # a new question starts a new clock
    clock.advance(cfg.overseer_owner_wait_s - 1)
    assert _look(sup) == []
    clock.advance(1)
    assert _look(sup) == ["P0 has waited on the owner for 60m"]


# -- the controls: a parked session nobody answered ----------------------------
def test_a_parked_session_nobody_answered_still_waits_on_the_owner(sup, cfg, clock):
    asked = _park(sup, clock)
    clock.advance(cfg.overseer_owner_wait_s - PARK_AFTER - 1)
    assert _look(sup) == []
    clock.advance(1)
    assert time.time() - asked == pytest.approx(cfg.overseer_owner_wait_s)
    assert _look(sup) == ["P0 has waited on the owner for 60m"]

    st = state_mod.read(cfg)
    check = doctor._check_owner(cfg, st)
    assert check.status == WARN and "you are the blocker: P0 parked" in check.detail
    assert f'"{FIRST}"' in check.detail and "swarm resumed P0" in check.fix_hint
    data, md = _digest(cfg)
    [q] = data["owner"]
    assert (q["who"], q["state"], q["question"]) == ("P0", "parked", FIRST)
    assert "- P0 (parked, " in _owner_section(md) and FIRST in _owner_section(md)
    assert ovdigest.in_flight(st) == {"P0": "parked"}


def _old_state_file(cfg) -> None:
    """The file a release from before the marks wrote: a parked key, nothing else."""
    old = State.fresh(2).to_dict()
    old["parked"] = ["P0"]
    assert "asked" not in old and "answered" not in old
    cfg.state_path.write_text(json.dumps(old), encoding="utf-8")


def test_a_state_file_from_before_the_marks_reads_a_parked_session_as_asking(cfg, clock):
    _old_state_file(cfg)
    stamp = datetime.fromtimestamp(time.time() - 40 * 60).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    with cfg.supervisor_log.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} 1.000 PARK P0 window=wait:P0\n")
    owner.waiting(cfg, "P0", FIRST)

    st = state_mod.read(cfg)
    check = doctor._check_owner(cfg, st)
    assert check.status == WARN and f'P0 parked 40m: "{FIRST}"' in check.detail
    assert ovdigest.owner_questions(cfg, st, time.time()) == [
        {"who": "P0", "state": "parked", "age_s": None, "question": FIRST}]
    assert ovdigest.in_flight(st) == {"P0": "parked"}
    # The Overseer times it from its first sighting, as before.
    s = Supervisor(cfg)
    try:
        assert _look(s) == []
        clock.advance(cfg.overseer_owner_wait_s)
        assert _look(s) == ["P0 has waited on the owner for 60m"]
    finally:
        s.log.close()


# -- what state records ---------------------------------------------------------
def test_an_unmarked_parked_key_is_asking_since_an_unknown_moment(cfg):
    _old_state_file(cfg)
    st = state_mod.read(cfg)
    assert st.asking("P0") and st.on_owner() == ["P0"] and st.working_parked() == []
    assert st.asked_at("P0", cfg.park_after) is None


def test_parking_records_when_the_question_was_asked(sup, cfg, clock):
    asked = _park(sup, clock)
    st = state_mod.read(cfg)
    assert st.asked == {"P0": pytest.approx(asked)} and st.answered == {}
    assert st.asked_at("P0", cfg.park_after) == pytest.approx(asked)


def test_resumed_marks_a_parked_session_answered_and_waiting_marks_it_asking(sup, cfg, clock):
    _park(sup, clock)
    clock.advance(30)
    answered = time.time()
    _answers(sup, "P0", "yes")
    st = state_mod.read(cfg)
    assert st.answered == {"P0": answered} and st.asked == {}
    assert not st.asking("P0") and st.on_owner() == [] and st.working_parked() == ["P0"]
    assert st.pending() and st.in_flight("P0")  # still in flight, in its own window

    clock.advance(30)
    sup._on_waiting("P0")
    st = state_mod.read(cfg)
    assert st.asked == {"P0": time.time()} and st.answered == {}
    assert st.waiting == {}  # a parked session has no park timer to arm
    log = cfg.supervisor_log.read_text(encoding="utf-8")
    assert "EVENT resumed P0 cancelled=False parked=True" in log
    assert "EVENT waiting P0 parked=True asking-again=True" in log


def test_an_answer_before_the_park_leaves_no_mark(sup, cfg, clock):
    _asks(sup, "P0", FIRST)
    _answers(sup, "P0", "yes")
    st = state_mod.read(cfg)
    assert st.waiting == {} and st.parked == [] and st.asked == {} and st.answered == {}
    assert "asked" not in st.to_dict() and "answered" not in st.to_dict()


def test_a_finished_parked_session_takes_its_marks_with_it(sup, cfg, clock):
    _park(sup, clock)
    _answers(sup, "P0", "yes")
    sup._on_done("P0", "ok")
    st = state_mod.read(cfg)
    assert st.parked == [] and st.asked == {} and st.answered == {}


def test_the_marks_survive_the_state_file(cfg):
    with state_mod.transaction(cfg) as st:
        st.parked = ["P0", "operator:op-1"]
        st.asked = {"operator:op-1": 100.0}
        st.answered = {"P0": 200.0}
    raw = json.loads(cfg.state_path.read_text(encoding="utf-8"))
    assert raw["asked"] == {"operator:op-1": 100.0} and raw["answered"] == {"P0": 200.0}
    st = state_mod.read(cfg)
    assert st.on_owner() == ["operator:op-1"] and st.working_parked() == ["P0"]
    assert st.asked_at("operator:op-1", 120) == 100.0 and st.asked_at("P0", 120) is None


def test_an_operator_job_reads_the_same_mark(cfg, clock):
    key = state_mod.waiter_key(state_mod.OPERATOR, "op-1")
    with state_mod.transaction(cfg) as st:
        st.waiting[key] = time.time() + PARK_AFTER
        st.park(key, PARK_AFTER)
        assert st.asking(key)
        assert st.answer(key, time.time()) is True
    s = Supervisor(cfg)
    try:
        clock.advance(cfg.overseer_owner_wait_s + 1)
        assert _look(s) == []
    finally:
        s.log.close()
