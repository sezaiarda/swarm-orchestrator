"""Delegation + decision history: what a question costs, and what the owner said.

Owner answers to worker questions never reached the history, and
every ping read the same whether it held up
nothing or a chain of phases. These tests pin the four pieces that fix that: the
cost line on a waiting ping (and its one-screen question), the owner's answer
recorded as an ``owner_decision`` note, ``notes.add`` refusing duplicates, and
``swarm report --decisions`` reading all of it back per phase.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from swarm_orchestrator import launch, ledger, opqueue, ovdigest, owner
from swarm_orchestrator import notes as notes_mod
from swarm_orchestrator import report as report_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load
from swarm_orchestrator.tui import tables
from swarm_orchestrator.tui.data import PhaseRun

LEDGER = (
    "- [ ] `A` · needs:—\n"
    "- [ ] `B` · needs:`A`\n"
    "- [ ] `C` · needs:`B`\n"
    "- [ ] `D` · needs:`A`\n"
    "- [ ] `E` · needs:`C`,`D`\n"
    "- [ ] `F` · needs:—\n"
)


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    c = load(project_dir=str(project))
    c.ensure_dirs()
    return c


def _sent(tmp_path: Path) -> list[str]:
    path = tmp_path / "tg.log"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _cli(cfg, *args: str) -> int:
    return cli_main(["--project-dir", str(cfg.project_dir), *args])


# -- how many phases a question holds up --------------------------------------
GRAPH = {"A": set(), "B": {"A"}, "C": {"B"}, "D": {"A"}, "E": {"C", "D"}, "F": set()}


def test_blocked_behind_counts_every_transitive_dependent_once():
    assert ledger.blocked_behind(GRAPH, "A", {}) == 4  # B C D E — E only once
    assert ledger.blocked_behind(GRAPH, "C", {}) == 1
    assert ledger.blocked_behind(GRAPH, "F", {}) == 0
    assert ledger.blocked_behind(GRAPH, "not-in-ledger", {}) == 0


def test_a_dependent_in_done_is_not_waiting_and_is_not_walked_through():
    # D landed (skip) and E is only reached through C; B failed — attempted, so
    # not "waiting on A" either, and C behind it is B's problem now.
    assert ledger.blocked_behind(GRAPH, "A", {"D": "skip", "B": "fail"}) == 0
    assert ledger.blocked_behind(GRAPH, "A", {"D": "ok"}) == 3  # B C E


def test_excluded_dependents_are_not_counted():
    assert ledger.blocked_behind(GRAPH, "A", {}, {"D"}) == 3  # B C E


def test_a_long_serial_chain_does_not_recurse():
    chain = {f"P{i}": ({f"P{i - 1}"} if i else set()) for i in range(5000)}
    assert ledger.blocked_behind(chain, "P0", {}) == 4999


# -- the ping itself ----------------------------------------------------------
def test_cost_line_says_what_waiting_costs():
    at = time.mktime((2026, 9, 23, 14, 5, 0, 0, 0, -1))
    assert launch.cost_line(3, "slot held", at) == "holding up 3 phases · slot held · asked 14:05"
    assert launch.cost_line(1, "slot held", at).startswith("holding up 1 phase ·")
    # Unknown count: say what is known rather than guess a number.
    assert launch.cost_line(None, "operator session held", at) == "operator session held · asked 14:05"


def test_a_long_question_is_cut_to_one_screen_and_says_where_the_rest_is():
    short = launch.ping_question("  which\n  schema? ")
    assert short == "which schema?"
    long = launch.ping_question("word " * 400)
    assert long.endswith("… (full question in its window)")
    assert len(long) <= launch.PING_QUESTION_CHARS + len(" … (full question in its window)")


def test_the_waiting_ping_leads_with_the_cost_from_the_live_ledger(cfg, tmp_path):
    with state_mod.transaction(cfg) as st:
        st.done = {"D": "ok"}
    owner.waiting(cfg, "A", "roll now or later? " + "x" * 900)
    cost, body = _sent(tmp_path)[:2]
    assert cost.startswith("holding up 3 phases · a worker place is tied up · asked ")
    assert body.startswith("swarm: the worker on A is waiting on you — roll now or later?")
    assert body.endswith("(full question in its window)")
    # The digest and doctor still recover the question from the ledger line.
    from swarm_orchestrator import doctor
    assert doctor.waiting_question(cfg, "A").startswith("roll now or later?")


def test_operator_and_overseer_pings_are_trimmed_the_same_way():
    # They share ping_question/cost_line with the worker's ping; the CLI tests
    # (test_opsession, test_overseer_pass) cover the wiring.
    assert launch.ping_question("q" * 5000).endswith("(full question in its window)")


# -- the owner's answer goes into history -------------------------------------
def test_resumed_with_an_answer_records_an_owner_decision(cfg):
    owner.waiting(cfg, "B", "close it and file the leftovers as new rows?")
    assert _cli(cfg, "resumed", "B", "yes,", "file", "them") == 0
    [note] = notes_mod.load(cfg, "B")
    assert note.kind == notes_mod.OWNER_DECISION
    assert note.text == "yes, file them (asked: close it and file the leftovers as new rows?)"


def test_resumed_without_an_answer_still_works_and_records_nothing(cfg):
    assert _cli(cfg, "resumed", "B") == 0
    assert notes_mod.load(cfg, "B") == []


def test_owning_phase_maps_extra_jobs_back_to_their_phase():
    assert opqueue.owning_phase("api-F26") == "api-F26"
    assert opqueue.owning_phase("api-F26-op2") == "api-F26"
    assert opqueue.owning_phase("op-1758600000") == "op-1758600000"
    assert opqueue.owning_phase("op-1758600000-2") == "op-1758600000-2"


# -- notes -----------------------------------------------------------------
def test_notes_add_does_not_record_the_same_thing_twice(cfg):
    first = notes_mod.add(cfg, "A", "used a 30d window", "decision")
    again = notes_mod.add(cfg, "A", "  used a 30d window ", "decision")
    notes_mod.add(cfg, "A", "used a 30d window", "risk")  # another kind: kept
    assert again.ts == first.ts
    assert [(n.kind, n.text) for n in notes_mod.load(cfg, "A")] == [
        ("decision", "used a 30d window"), ("risk", "used a 30d window")]


def test_an_old_notes_file_still_loads(cfg):
    d = cfg.state_dir / "notes"
    d.mkdir(parents=True, exist_ok=True)
    (d / "A.jsonl").write_text(
        json.dumps({"ts": 1.0, "phase": "A", "kind": "decision", "text": "old"}) + "\n"
        + json.dumps({"text": "no kind at all"}) + "\n",
        encoding="utf-8",
    )
    assert [(n.kind, n.text) for n in notes_mod.load(cfg, "A")] == [
        ("decision", "old"), ("decision", "no kind at all")]


def test_note_takes_a_leading_kind_word_as_the_kind(cfg):
    assert _cli(cfg, "note", "A", "risk", "the cache may be cold after a roll") == 0
    assert _cli(cfg, "note", "A", "decision on retention: 30d") == 0  # one quoted word
    notes = notes_mod.load(cfg, "A")
    assert [(n.kind, n.text) for n in notes] == [
        ("risk", "the cache may be cold after a roll"),
        ("decision", "decision on retention: 30d"),
    ]


def test_a_worker_cannot_file_an_owner_decision_with_swarm_note(cfg):
    with pytest.raises(SystemExit):
        _cli(cfg, "note", "A", "--kind", "owner_decision", "I decided for them")


# -- swarm report --decisions -------------------------------------------------
def test_report_decisions_shows_owner_worker_and_operator_per_phase(cfg):
    cfg.operator_enabled = True
    notes_mod.add(cfg, "B", "anchored the timeout at first byte", "decision")
    notes_mod.add(cfg, "B", "the box may still run the old image", "risk")
    notes_mod.owner_answer(cfg, "B", "roll tonight", "roll now or later?")
    opqueue.add(cfg, "B", status="operator", note="roll payments and check healthz")
    opqueue.complete(cfg, "B", "rolled 2026-09-23.w3e; healthz green")
    opqueue.add_adhoc(cfg, "prune the old images", "B")
    notes_mod.owner_answer(cfg, notes_mod.OVERSEER, "keep the look campaign", "drop it?")

    text = report_mod.render(report_mod.build_report(cfg), decisions=True)
    block = text.split("B  [")[1].split("\n\n")[0]
    lines = [ln.strip() for ln in block.splitlines()]
    assert lines[1] == "[owner] roll tonight (asked: roll now or later?)"
    assert "[decision] anchored the timeout at first byte" in lines
    assert "[risk] the box may still run the old image" in lines
    assert "[operator done] B: rolled 2026-09-23.w3e; healthz green" in lines
    assert "[operator queued] B-op2: prune the old images" in lines
    assert "overseer  [Overseer]" in text
    assert "2 owner decision(s)" in text and "2 operator job(s)" in text


# -- every place notes are shown survives the new kind ------------------------
def test_tui_details_separate_the_owner_from_the_worker(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "P1.jsonl").write_text(
        json.dumps({"ts": 1.0, "phase": "P1", "kind": "owner_decision", "text": "roll tonight"}) + "\n"
        + json.dumps({"ts": 2.0, "phase": "P1", "kind": "decision", "text": "kept v2"}) + "\n",
        encoding="utf-8",
    )

    class Dash:
        def __init__(self):
            self.notes_dir = notes_dir
            self.cfg = type("C", (), {"done_dir": tmp_path / "done"})()

    out = tables.history_detail(PhaseRun(phase="P1", status="ok"), Dash())
    assert "owner decisions (1)" in out and "roll tonight" in out
    assert "decisions it made on its own (1)" in out and "kept v2" in out


def test_the_overseer_digest_renders_an_owner_decision(cfg):
    with state_mod.transaction(cfg) as st:
        st.done = {"A": "ok"}
    (cfg.done_dir / "A.ok").write_text("done", encoding="utf-8")
    notes_mod.owner_answer(cfg, "A", "yes", "ship it?")
    dig = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0)
    [row] = dig["finished"]
    assert row["notes"] == [{"kind": "owner_decision", "text": "yes (asked: ship it?)"}]
    assert "owner_decision: yes (asked: ship it?)" in ovdigest.render(dig)
