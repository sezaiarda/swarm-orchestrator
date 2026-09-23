"""Tests for the home screen's feed and needs-you data (:mod:`tui.timeline`).

The feed exists because what each phase did and the calls its worker made are the
most useful part of the dashboard. What matters here:

* **every source lands, newest first** — finishes with their recap, worker notes,
  the owner's own answers, operator outcomes and Overseer passes, interleaved by
  time rather than grouped by where they came from;
* **nothing is guessed into place** — an entry without a timestamp is dropped,
  not sorted to "now" where it would sit on top forever;
* **needs-you counts what matters** — how long each thing has waited and how
  many not-yet-landed phases sit behind it.
"""

from __future__ import annotations

from types import SimpleNamespace

from swarm_orchestrator import opqueue
from swarm_orchestrator.tui import data
from swarm_orchestrator.tui import timeline as tl

NOW = 1_700_000_000.0


def run(phase, ended, status="ok", summary="", note="", started=None):
    return data.PhaseRun(phase=phase, status=status, started_at=started, ended_at=ended,
                         summary=summary, note=note)


def note(phase, kind, text, ts):
    return data.Note(phase=phase, kind=kind, text=text, ts=ts)


def pass_(id_, started, ended=0.0, status="done", summary="", left="", question="", answer="",
          asked_at=0.0):
    return SimpleNamespace(id=id_, started_at=started, ended_at=ended, status=status,
                           summary=summary, did="", left=left, question=question,
                           answer=answer, asked_at=asked_at)


# -- the feed -------------------------------------------------------------
def test_feed_interleaves_every_source_newest_first():
    history = [run("P2", NOW - 10, summary="built the thing"),
               run("P1", NOW - 300, status="fail", note="tests red")]
    notes = {"P2": [note("P2", "decision", "kept v1", NOW - 50),
                    note("P2", "owner_decision", "ship it", NOW - 20)]}
    jobs = [opqueue.Item(phase="P1-op1", state=opqueue.DONE, outcome="rolled", done_at=NOW - 100),
            opqueue.Item(phase="P3", state=opqueue.QUEUED, queued_at=NOW - 5)]
    passes = [pass_("20260923T100000Z", NOW - 400, NOW - 200, summary="filed two", left="W12?")]

    feed = tl.build_feed(history, notes, jobs, passes)

    assert [(f.kind, f.phase or f.ref) for f in feed] == [
        (tl.FINISH, "P2"), (tl.OWNER, "P2"), ("decision", "P2"), (tl.OPERATOR, "P1"),
        (tl.OVERSEER, "20260923T100000Z"), (tl.FINISH, "P1"),
    ]
    assert feed[0].text == "built the thing"
    assert feed[-1].text == "tests red"  # no recap: the worker's own note stands in
    assert feed[3].text == "rolled" and feed[3].ref == "P1-op1"  # a job opens its phase
    assert feed[4].left == "W12?"


def test_feed_skips_what_is_still_running_and_what_has_no_clock():
    history = [run("P1", None, started=NOW - 60)]
    notes = {"P1": [note("P1", "risk", "no ts", None)]}
    jobs = [opqueue.Item(phase="P2", state=opqueue.DONE)]  # never stamped
    assert tl.build_feed(history, notes, jobs, []) == []


def test_feed_keeps_only_the_newest_up_to_the_limit():
    history = [run(f"P{i}", NOW - i) for i in range(100)]
    feed = tl.build_feed(history, limit=5)
    assert [f.phase for f in feed] == ["P0", "P1", "P2", "P3", "P4"]


def test_feed_survives_nothing_at_all():
    assert tl.build_feed() == []
    assert tl.build_feed(None, None, None, None) == []


def test_an_unknown_note_kind_reads_as_a_decision():
    feed = tl.build_feed([], {"P1": [note("P1", "whim", "x", NOW)]})
    assert feed[0].kind == "decision"


def test_an_abandoned_job_reports_its_error():
    job = opqueue.Item(phase="op-1", state=opqueue.ABANDONED, last_error="boom", queued_at=NOW)
    item = tl.build_feed([], {}, [job])[0]
    assert (item.status, item.text, item.phase) == (opqueue.ABANDONED, "boom", "op-1")


# -- needs you ------------------------------------------------------------
def dash_with(blockers=(), passes=(), graph=None, done=None, slots=(), notifications=()):
    snap = data.Snapshot(ok=True, blockers=list(blockers), done=dict(done or {}),
                         slots=list(slots))
    return SimpleNamespace(snapshot=snap, passes=list(passes), graph=graph or {},
                           notifications=list(notifications), sentinels={})


def test_downstream_counts_only_what_has_not_landed():
    graph = {"A": set(), "B": {"A"}, "C": {"B"}, "D": {"A"}, "E": set()}
    rdeps = tl.dependents(graph)
    assert tl.downstream(rdeps, {}, "A") == 3
    assert tl.downstream(rdeps, {"D": "ok"}, "A") == 2
    assert tl.downstream(rdeps, {}, "E") == 0


def test_needs_you_lists_blockers_longest_waiting_first_with_what_they_hold_up():
    graph = {"A": set(), "B": {"A"}, "C": {"B"}}
    blockers = [
        data.Blocker(phase="B", kind="waiting", question="which?", since=NOW - 60),
        data.Blocker(phase="A", kind="parked", question="", since=NOW - 600),
    ]
    slot = data.SlotView(id=2, busy=True, phase="B", pane_id=None, branch=None, worktree=None)
    notes = [data.Notification(ts=NOW - 700, kind="question", phase="A", source="",
                               text="asked by ping", delivered=True, error="")]
    got = tl.needs_you(dash_with(blockers, graph=graph, slots=[slot], notifications=notes))
    assert [n.phase for n in got] == ["A", "B"]
    assert got[0].blocks == 2 and got[1].blocks == 1
    assert got[0].question == "asked by ping"  # the ping fills a blank question
    assert got[1].slot == 2 and got[0].slot is None
    assert got[0].kind == tl.NEED_LABEL["parked"]


def test_needs_you_adds_an_overseer_question_nobody_answered():
    passes = [pass_("p1", NOW - 90, status="running", question="retire W3?", asked_at=NOW - 30),
              pass_("p0", NOW - 900, status="running", question="old", answer="yes")]
    got = tl.needs_you(dash_with(passes=passes))
    assert [(n.kind, n.ref, n.phase) for n in got] == [(tl.NEED_LABEL[tl.OVERSEER], "p1", None)]
    assert got[0].since == NOW - 30


def test_an_operator_job_blocks_behind_its_owning_phase():
    graph = {"A": set(), "B": {"A"}}
    got = tl.needs_you(dash_with([data.Blocker(phase="A-op2", kind="operator-ask", question="?")],
                                 graph=graph))
    assert (got[0].phase, got[0].ref, got[0].blocks) == ("A", "A-op2", 1)


def test_needs_you_is_empty_before_a_run():
    assert tl.needs_you(SimpleNamespace(snapshot=data.Snapshot())) == []
    assert tl.needs_you(dash_with()) == []


# -- what's next ----------------------------------------------------------
def test_upcoming_lists_ready_before_blocked_in_ledger_order():
    graph = {"A": set(), "B": {"A"}, "C": set(), "D": {"B", "X"}, "E": set()}
    got = tl.upcoming(graph, {"A": "ok"}, busy={"E"})
    assert [(u.phase, u.ready) for u in got] == [("B", True), ("C", True), ("D", False)]
    assert got[-1].needs == ("B", "X")


def test_upcoming_floats_the_active_campaign_and_skips_the_excluded():
    graph = {"x-W1": set(), "coral-W1": set(), "coral-W2": set()}
    got = tl.upcoming(graph, {}, excluded={"coral-W2"}, prefer="coral")
    assert [u.phase for u in got] == ["coral-W1", "x-W1"]
