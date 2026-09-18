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

    assert result.route == "dispatch"  # the decision is still reported
    assert opqueue.load_all(off) == []
    assert not off.operator_dir.exists()


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
    opqueue.add(cfg, "W8", status="operator", note="rebuild and recreate the webhooks container")
    opqueue.add(cfg, "W9", status="operator", note="rebuild the source-provider image")
    opqueue.escalate(cfg, "W8", "which host is webhooks on?")
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
