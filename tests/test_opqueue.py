"""Tests for the durable operator hand-off queue and its triage.

No model is ever called: the ``SWARM_TRIAGE_CMD`` seam stands in for one, exactly
as ``SWARM_RECAP_CMD`` does for the recap. It is deliberately *not*
``SWARM_TG_SINK`` — that one is set in every hermetic run and means "route
telegrams to a file", so reusing it would arm the queue in the whole suite.

Three properties decide whether this feature helps or hurts:

* **the item lands in the window between the sentinel and the poke** — before the
  sentinel it is not re-derivable, after the poke the work is already merged and
  recorded done with nothing queued;
* **the queue is bounded** — an item that cannot be carried out reaches a human
  anyway, because that is what an ``operator`` finish used to do by definition;
* **triage fails toward ``later``** — ``now`` is the branch that opens a session
  holding the owner's full authority, so no parse failure may ever produce it.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import opqueue
from swarm_orchestrator import recap as recap_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.tui import data

PHASE = "op-P1"
NOTE = "rebuilt the gateway image; the host still needs a compose up to pick it up"
THIN = "done"


def _cfg(tmp_path, monkeypatch, *, enabled: bool):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / ".swarm.toml").write_text(
        f"[operator]\nenabled = {'true' if enabled else 'false'}\n", encoding="utf-8"
    )
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    # Whatever `swarm done` detaches must not be able to reach a model.
    monkeypatch.setenv("SWARM_BIN", "true")
    for leak in ("SWARM_TRIAGE_CMD", "SWARM_RECAP_CMD", "SWARM_OPERATOR",
                 "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(leak, raising=False)
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    return _cfg(tmp_path, monkeypatch, enabled=True)


@pytest.fixture
def off(tmp_path, monkeypatch):
    return _cfg(tmp_path, monkeypatch, enabled=False)


def tg_lines(cfg) -> list[str]:
    import os

    path = os.environ["SWARM_TG_SINK"]
    try:
        with open(path, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines() if ln.strip()]
    except OSError:
        return []


def seam(monkeypatch, script: str) -> None:
    """Stand in for the triage model with a shell one-liner."""
    monkeypatch.setenv(opqueue.TRIAGE_CMD_ENV, script)


# -- the write window -----------------------------------------------------
def test_the_item_lands_after_the_sentinel_and_before_the_poke(cfg, monkeypatch):
    """The one ordering that makes a crash in either window survivable."""
    seen: dict[str, bool] = {}
    real_write = launch_mod._write_sentinel

    def spy_write(c, phase, status, note, force=False):
        seen["at_sentinel"] = opqueue.load(c, phase) is not None
        return real_write(c, phase, status, note, force=force)

    def spy_poke(c, line):
        seen["at_poke"] = opqueue.load(c, PHASE) is not None
        return False

    monkeypatch.setattr(launch_mod, "_write_sentinel", spy_write)
    monkeypatch.setattr(launch_mod, "_poke_fifo", spy_poke)

    result = launch_mod.done(cfg, PHASE, "operator", NOTE)

    assert result.route == "dispatch"
    assert seen["at_sentinel"] is False, "the sentinel must be the durable one"
    assert seen["at_poke"] is True, "the poke frees the slot — queue before it"


def test_a_second_swarm_done_does_not_queue_a_second_hand_off(cfg):
    """Sessions sometimes re-ran `swarm done`, some of them several times."""
    launch_mod.done(cfg, PHASE, "operator", NOTE)
    first = opqueue.load(cfg, PHASE)
    launch_mod.done(cfg, PHASE, "operator", NOTE)

    assert opqueue.load(cfg, PHASE).queued_at == first.queued_at
    assert len(opqueue.load_all(cfg)) == 1


def test_a_disabled_operator_queues_nothing_at_all(off):
    """Not "queues and ignores": a queue nothing drains is worse than none."""
    result = launch_mod.done(off, PHASE, "operator", NOTE)

    assert result.route == "owner"  # the decision is still reported
    assert opqueue.load_all(off) == []
    assert not off.operator_dir.exists()


def test_a_disabled_operator_telegrams_the_hand_off_as_an_owner_to_do(off):
    """Hand-offs used to vanish silently this way: never silent again.

    With nobody to run it the owner is the only one left who can, so the note
    goes to their phone — and `swarm done` stops promising a session.
    """
    result = launch_mod.done(off, PHASE, "operator", NOTE)

    lines = tg_lines(off)
    assert len(lines) == 1
    assert PHASE in lines[0] and NOTE in lines[0] and "to-do" in lines[0]
    rendered = result.render()
    assert "operator job queued" not in rendered
    assert "operator is off" in rendered and "to-do" in rendered


def test_a_re_run_swarm_done_does_not_send_the_to_do_twice(off):
    launch_mod.done(off, PHASE, "operator", NOTE)
    again = launch_mod.done(off, PHASE, "operator", NOTE)

    assert len(tg_lines(off)) == 1
    assert "already" in again.route_detail


def test_a_thin_recap_is_not_telegrammed_as_a_to_do_either(off):
    """The worker is told to re-run with a real recap; a probe is not a to-do."""
    result = launch_mod.done(off, PHASE, "operator", THIN)

    assert result.route == "skipped"
    assert tg_lines(off) == []


def test_an_enabled_operator_promises_a_queued_job_and_telegrams_nobody(cfg):
    result = launch_mod.done(cfg, PHASE, "operator", NOTE)

    assert "operator job queued" in result.render()
    assert tg_lines(cfg) == []


def test_a_recap_too_thin_to_brief_a_session_is_never_queued(cfg):
    result = launch_mod.done(cfg, PHASE, "operator", THIN)

    assert result.route == "skipped"
    assert opqueue.load_all(cfg) == []


def test_an_ok_finish_queues_nothing(cfg):
    launch_mod.done(cfg, PHASE, "ok", NOTE)

    assert opqueue.load_all(cfg) == []


# -- triage ---------------------------------------------------------------
def test_triage_answering_in_prose_records_later(cfg, monkeypatch):
    """Prose is not a schedule, however confidently it says "now"."""
    seam(monkeypatch, "printf 'You should absolutely do this right now.'")
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)

    item = opqueue.triage(cfg, PHASE)

    assert item.triage["when"] == opqueue.LATER
    assert "prose" in item.triage["why"]


def test_a_triage_that_times_out_records_later(cfg, monkeypatch):
    monkeypatch.setattr(recap_mod, "_CLI_TIMEOUT_S", 0.3)
    seam(monkeypatch, "sleep 5")
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)

    item = opqueue.triage(cfg, PHASE)

    assert item.triage["when"] == opqueue.LATER
    assert item.triage["source"] == "seam-timeout"


def test_an_unknown_verb_records_later(cfg, monkeypatch):
    seam(monkeypatch, """printf '{"when":"immediately","why":"go","group":""}'""")
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)

    assert opqueue.triage(cfg, PHASE).triage["when"] == opqueue.LATER


def test_a_clean_now_is_honoured(cfg, monkeypatch):
    seam(monkeypatch, """printf '{"when":"now","why":"the host is down","group":"deploy"}'""")
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)

    item = opqueue.triage(cfg, PHASE)

    assert item.triage["when"] == opqueue.NOW
    assert opqueue.group_of(item) == "deploy"


def test_an_unrecognised_group_collapses_to_a_phase_singleton(cfg, monkeypatch):
    """A hallucinated bucket must not batch unrelated phases into one session."""
    seam(monkeypatch, """printf '{"when":"later","why":"keeps","group":"infra-migration"}'""")
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)

    item = opqueue.triage(cfg, PHASE)

    assert item.triage["group"] == "infra-migration"  # kept, for forensics
    assert opqueue.group_of(item) == f"{opqueue.SINGLETON_PREFIX}{PHASE}"


def test_triage_on_a_phase_with_no_item_is_a_no_op(cfg, monkeypatch):
    seam(monkeypatch, """printf '{"when":"now","why":"x","group":""}'""")

    assert opqueue.triage(cfg, PHASE) is None


# -- the bound ------------------------------------------------------------
def test_the_attempt_is_counted_in_the_write_that_marks_it_running(cfg, monkeypatch):
    """No persisted state may ever show `running` with the attempt uncounted."""
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    writes: list[str] = []
    real = opqueue._write_text

    def spy(path, text):
        writes.append(text)
        return real(path, text)

    monkeypatch.setattr(opqueue, "_write_text", spy)
    opqueue.lease(cfg, PHASE, now=1_000.0)

    records = [json.loads(t) for t in writes if t.lstrip().startswith("{")]
    assert [(r["state"], r["attempts"]) for r in records] == [("running", 1)]
    assert opqueue.load(cfg, PHASE).run_id == opqueue.run_id(cfg)


def test_a_backed_off_item_is_not_leasable_until_run_after(cfg):
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    opqueue.lease(cfg, PHASE, now=1_000.0)
    opqueue.release(cfg, PHASE, "boom", now=1_000.0)

    assert opqueue.lease(cfg, PHASE, now=1_000.0) is None
    assert opqueue.ready(cfg, now=1_000.0) == []
    assert opqueue.next_deadline(cfg, now=1_000.0) == 1_000.0 + opqueue.RETRY_BACKOFF_S
    assert opqueue.lease(cfg, PHASE, now=9_000.0) is not None


def test_the_cap_abandons_the_item_and_telegrams_the_owner_once(cfg):
    """The guarantee this replaces: an `operator` note always reached a human."""
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    for tick in (1_000.0, 2_000.0, 3_000.0):
        assert opqueue.lease(cfg, PHASE, now=tick) is not None
        opqueue.release(cfg, PHASE, "the session died", now=tick)

    item = opqueue.load(cfg, PHASE)
    assert item.state == opqueue.ABANDONED
    assert item.attempts == opqueue.MAX_ATTEMPTS
    assert len(tg_lines(cfg)) == 1
    assert PHASE in tg_lines(cfg)[0] and "ABANDONED" in tg_lines(cfg)[0]

    # Terminal means terminal: nothing blocks on it and nobody is told twice.
    assert opqueue.abandon(cfg, PHASE, "again") is None
    assert opqueue.lease(cfg, PHASE, now=9_000.0) is None
    assert opqueue.pending(cfg) == []
    assert len(tg_lines(cfg)) == 1


def test_a_completed_hand_off_leaves_the_queue_pending(cfg):
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    opqueue.lease(cfg, PHASE, now=1_000.0)

    assert opqueue.complete(cfg, PHASE).state == opqueue.DONE
    assert opqueue.pending(cfg) == []
    assert tg_lines(cfg) == []


# -- recovery -------------------------------------------------------------
def test_reconcile_rebuilds_an_item_from_a_sentinel_that_outlived_it(cfg):
    """The write in `swarm done` is a cache; the sentinel is the durable record."""
    launch_mod._write_sentinel(cfg, PHASE, "operator", NOTE)
    assert opqueue.load(cfg, PHASE) is None

    changed = opqueue.reconcile(cfg)

    item = opqueue.load(cfg, PHASE)
    assert changed == [f"rebuilt {PHASE}"]
    assert item.note == NOTE and item.state == opqueue.QUEUED
    assert opqueue.reconcile(cfg) == []  # and it does not rebuild it twice


def test_reconcile_requeues_a_lease_taken_by_a_run_that_is_gone(cfg):
    opqueue.begin_run(cfg)
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    opqueue.lease(cfg, PHASE, now=1_000.0)
    stale = opqueue.load(cfg, PHASE).run_id

    changed = opqueue.reconcile(cfg)

    item = opqueue.load(cfg, PHASE)
    assert changed == [f"requeued {PHASE}"]
    assert item.state == opqueue.QUEUED and item.run_id == ""
    assert item.attempts == 1, "the attempt was spent — it must still count"
    assert opqueue.run_id(cfg) != stale


def test_reconcile_does_nothing_while_the_operator_is_disabled(off):
    launch_mod._write_sentinel(off, PHASE, "operator", NOTE)

    assert opqueue.reconcile(off) == []
    assert opqueue.load_all(off) == []


# -- the read side --------------------------------------------------------
def snapshot(cfg, done: dict):
    return data.build_snapshot(
        cfg, {"slots": [], "done": done}, operator=opqueue.load_all(cfg)
    )


def test_a_live_hand_off_is_not_something_that_needs_the_owner(cfg):
    """`operator` hands its action to a session — that is the whole status."""
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)

    snap = snapshot(cfg, {PHASE: "operator"})

    assert snap.blockers == []
    assert [i.phase for i in snap.operator] == [PHASE]


def test_a_legacy_needs_owner_finish_is_still_a_blocker(cfg):
    """Such notes may still be live on disk and do still need them."""
    snap = snapshot(cfg, {"old-P1": "needs-owner"})

    assert [(b.phase, b.kind) for b in snap.blockers] == [("old-P1", "needs-owner")]


def test_an_abandoned_hand_off_becomes_a_blocker(cfg):
    """The one case a human must see: the queue tried and gave up."""
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    opqueue.abandon(cfg, PHASE, "nothing drained it")

    snap = snapshot(cfg, {PHASE: "operator"})

    assert [(b.phase, b.kind) for b in snap.blockers] == [(PHASE, "operator-abandoned")]
    assert snap.blockers[0].question == NOTE


def test_an_asked_question_is_not_reported_as_a_give_up(cfg):
    """Both are terminal and both stop blocking — but they read very differently.

    Nothing scheduling cares which it was; the owner does. Being told a hand-off
    was `abandoned` when a session in fact asked them a civil question is the
    dashboard lying about what happened.
    """
    # W8 is the shape an older swarm left on disk: `operator-ask` abandoned the
    # item with `asked` set and the question in `last_error`. It must still load.
    cfg.operator_dir.mkdir(parents=True, exist_ok=True)
    opqueue.item_path(cfg, "W8").write_text(json.dumps({
        "phase": "W8", "status": "operator", "queued_at": 1.0, "attempts": 1,
        "note": "rebuild and recreate the webhooks container", "state": "abandoned",
        "asked": True, "last_error": "which host is webhooks on?",
    }), encoding="utf-8")
    opqueue.add(cfg, "W9", status="operator", note="rebuild the source-provider image")
    opqueue.abandon(cfg, "W9", "the session never came back")

    asked, gave_up = opqueue.load(cfg, "W8"), opqueue.load(cfg, "W9")
    assert (asked.asked, gave_up.asked) == (True, False)
    assert asked.terminal and gave_up.terminal
    assert opqueue.pending(cfg) == []

    kinds = {b.phase: b for b in data.build_snapshot(
        cfg, {"slots": [], "done": {}}, operator=opqueue.load_all(cfg)).blockers}
    assert kinds["W8"].kind == "operator-ask"
    assert kinds["W9"].kind == "operator-abandoned"
    # the ask leads with the question to answer, the brief behind it;
    # the give-up leads with the work not done, the reason behind it
    assert kinds["W8"].question == "which host is webhooks on?"
    assert "webhooks container" in kinds["W8"].detail
    assert kinds["W9"].question == "rebuild the source-provider image"
    assert kinds["W9"].detail == "the session never came back"


# -- waiting on the owner -------------------------------------------------
def test_asking_the_owner_keeps_the_job_alive_and_leased(cfg):
    """`operator-ask` used to abandon the job; the answer then reached nobody."""
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    opqueue.lease(cfg, PHASE, now=1_000.0)

    item, fresh = opqueue.wait_on_owner(cfg, PHASE, "  which\n host? ", now=2_000.0)

    assert fresh and item.state == opqueue.WAITING and not item.terminal
    assert item.question == "which host?" and item.asked_at == 2_000.0
    assert item.lease_until == 2_000.0 + opqueue.WAIT_LEASE_S
    assert [i.phase for i in opqueue.pending(cfg)] == [PHASE]
    assert opqueue.ready(cfg, now=3_000.0) == []  # nobody else may lease it
    assert opqueue.next_deadline(cfg, now=3_000.0) is None


def test_the_same_question_twice_is_not_fresh(cfg):
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    opqueue.lease(cfg, PHASE, now=1_000.0)
    opqueue.wait_on_owner(cfg, PHASE, "which host?", now=2_000.0)

    again, fresh = opqueue.wait_on_owner(cfg, PHASE, "which host?", now=5_000.0)
    other, fresh2 = opqueue.wait_on_owner(cfg, PHASE, "and which port?", now=6_000.0)

    assert fresh is False and again.asked_at == 2_000.0
    assert fresh2 is True and other.asked_at == 6_000.0


def test_only_a_live_job_can_ask(cfg):
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    assert opqueue.wait_on_owner(cfg, PHASE, "q?") == (None, False)  # never leased
    opqueue.lease(cfg, PHASE, now=1_000.0)
    opqueue.complete(cfg, PHASE)
    assert opqueue.wait_on_owner(cfg, PHASE, "q?") == (None, False)  # finished


def test_resuming_records_the_answer_and_restores_an_ordinary_lease(cfg):
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    opqueue.lease(cfg, PHASE, now=1_000.0)
    opqueue.wait_on_owner(cfg, PHASE, "which host?", now=2_000.0)

    item = opqueue.resume(cfg, PHASE, "the staging box", now=9_000.0)

    assert item.state == opqueue.RUNNING and item.answer == "the staging box"
    assert item.lease_until == 9_000.0 + opqueue.LEASE_S
    assert opqueue.resume(cfg, PHASE, "again") is None  # not waiting any more


def test_reconcile_requeues_a_job_whose_waiting_session_died_with_the_run(cfg):
    opqueue.begin_run(cfg)
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    opqueue.lease(cfg, PHASE, now=1_000.0)
    opqueue.wait_on_owner(cfg, PHASE, "which host?", now=2_000.0)

    assert opqueue.reconcile(cfg) == [f"requeued {PHASE}"]

    item = opqueue.load(cfg, PHASE)
    assert item.state == opqueue.QUEUED
    assert item.question == "which host?"  # handed to the next session


def test_completing_records_the_outcome(cfg):
    opqueue.add(cfg, PHASE, status="operator", note=NOTE)
    opqueue.lease(cfg, PHASE, now=1_000.0)

    item = opqueue.complete(cfg, PHASE, "  rolled webhooks;\n healthz green ")

    assert item.outcome == "rolled webhooks; healthz green" and item.done_at > 0


def test_an_item_file_from_before_this_version_still_loads(cfg):
    cfg.operator_dir.mkdir(parents=True, exist_ok=True)
    opqueue.item_path(cfg, PHASE).write_text(json.dumps({
        "phase": PHASE, "status": "operator", "note": NOTE, "queued_at": 5.0,
        "attempts": 0, "state": "queued", "run_after": 0.0, "lease_until": 0.0,
        "triage": {}, "branch": "", "run_id": "", "last_error": "", "asked": False,
    }), encoding="utf-8")

    item = opqueue.load(cfg, PHASE)

    assert item.state == opqueue.QUEUED and item.question == "" and item.mirror == ""
    assert opqueue.ready(cfg) == [item]


# -- ad-hoc jobs ----------------------------------------------------------
def test_an_ad_hoc_job_gets_an_id_no_phase_can_have(cfg):
    item = opqueue.add_adhoc(cfg, "  re-run the post-deploy smoke\n check ")

    assert item.phase.startswith(opqueue.ADHOC_PREFIX)
    assert item.note == "re-run the post-deploy smoke check"
    assert item.source == opqueue.ADDED and item.state == opqueue.QUEUED


def test_two_ad_hoc_jobs_in_one_second_do_not_collide(cfg, monkeypatch):
    monkeypatch.setattr(opqueue.time, "time", lambda: 1_700_000_000.0)

    first = opqueue.add_adhoc(cfg, "job one, a real brief")
    second = opqueue.add_adhoc(cfg, "job two, a real brief")

    assert first.phase == "op-1700000000" and second.phase == "op-1700000000-2"


def test_an_ad_hoc_job_for_a_phase_never_overwrites_its_hand_off(cfg):
    opqueue.add(cfg, "olive-W3", status="operator", note=NOTE)

    item = opqueue.add_adhoc(cfg, "verify the roll on the box", phase="olive-W3")
    fresh = opqueue.add_adhoc(cfg, "provision the model cache", phase="coral-W1")

    assert item.phase == "olive-W3-op2"
    assert opqueue.load(cfg, "olive-W3").note == NOTE
    assert fresh.phase == "coral-W1"


def test_an_ad_hoc_job_is_refused_when_off_empty_or_unsafe(cfg, off):
    assert opqueue.add_adhoc(off, "a real brief here") is None
    assert opqueue.add_adhoc(cfg, "   ") is None
    assert opqueue.add_adhoc(cfg, "a real brief here", phase="../evil") is None
    assert opqueue.add_adhoc(cfg, "a real brief here", phase="a b") is None


# -- the queue's files are changed under a lock -----------------------------
def test_a_finish_never_crosses_a_reclaim(cfg, monkeypatch):
    """A reclaim that read a running job before its operator-done landed wrote it
    back queued, and the finished job ran again. Under the lock the finish waits."""
    opqueue.add(cfg, "P1", status="operator", note="roll it")
    assert opqueue.lease(cfg, "P1") is not None
    inside, go = threading.Event(), threading.Event()
    real_load = opqueue.load

    def slow_load(c, phase):
        item = real_load(c, phase)
        if threading.current_thread().name == "reclaim":
            inside.set()
            go.wait(5)
        return item

    monkeypatch.setattr(opqueue, "load", slow_load)
    reclaim = threading.Thread(target=opqueue.release, args=(cfg, "P1", "lease ran out"),
                               name="reclaim")
    reclaim.start()
    assert inside.wait(5)
    finish = threading.Thread(target=opqueue.complete, args=(cfg, "P1", "done"))
    finish.start()
    time.sleep(0.2)
    assert finish.is_alive()  # waiting for the lock, not writing over the reclaim
    go.set()
    reclaim.join(5)
    finish.join(5)
    assert real_load(cfg, "P1").state == opqueue.DONE


# -- not before -------------------------------------------------------------
@pytest.mark.parametrize("text,delta", [("90m", 5400), ("6h", 21600), ("+3d", 259200)])
def test_not_before_reads_relative_times(text, delta):
    assert opqueue.parse_not_before(text, now=1000.0) == 1000.0 + delta


def test_not_before_reads_a_local_date_and_refuses_nonsense():
    from datetime import datetime
    assert opqueue.parse_not_before("2026-09-30") == datetime(2026, 9, 30).timestamp()
    assert opqueue.parse_not_before("2026-09-30 08:00") == datetime(2026, 9, 30, 8).timestamp()
    with pytest.raises(ValueError):
        opqueue.parse_not_before("after the weekend")


def test_later_puts_a_leased_job_back_until_then_without_spending_an_attempt(cfg):
    opqueue.add(cfg, "P1", status="operator", note="delete the rollback kit")
    assert opqueue.lease(cfg, "P1").attempts == 1

    item = opqueue.later(cfg, "P1", 5000.0, "kept until the 30th")

    assert (item.state, item.run_after, item.attempts) == (opqueue.QUEUED, 5000.0, 0)
    assert item.last_error == "kept until the 30th"
    assert opqueue.later(cfg, "P1", 6000.0) is None  # not leased any more
