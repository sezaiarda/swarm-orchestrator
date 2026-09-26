"""The swarm as the ledger's only writer: reports queue, the supervisor applies them.

Pure text edits first (tick, status, date gate, carried needs, new rows), then
the history files, then the whole path on real git: a phase's report is applied
on the target branch only once the phase lands, committed and pushed by the
swarm, and held (not lost) when the checkout cannot take it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import cli, gitq, ledgerw
from swarm_orchestrator import ledger as ledger_mod
from swarm_orchestrator import master as master_mod
from swarm_orchestrator import notes as notes_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log

LEDGER = """# Ledger

## Campaign A
- [x] `a-W1` · dir:`alpha` · needs:— · **the first wave**
- [ ] `a-W2` · dir:`alpha` · needs:`a-W1` · **the second wave** · **TAG:`v1`**
- [ ] `a-W3` · dir:`alpha` · needs:`a-W2` · **the third wave**

## Campaign B
- [ ] `b-W1` · dir:`beta` · needs:`a-W1` · **beta starts**
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


# -- pure text ---------------------------------------------------------------
def test_tick_status_and_date_gate_keep_the_row_parseable():
    text = ledgerw.set_state(LEDGER, "a-W2", tick=True, status="done (2026-09-27)")
    assert "a-W2" in ledger_mod.ticked(text)
    assert ledger_mod.parse(text) == ledger_mod.parse(LEDGER)
    line = next(ln for ln in text.splitlines() if "`a-W2`" in ln)
    assert line.endswith("**TAG:`v1`** · status: done (2026-09-27)")

    later = ledgerw.set_state(LEDGER, "a-W3", status="later, after 2999-01-01", after="2999-01-01")
    assert ledger_mod.deferred(later, "2026-09-27") == {"a-W3": "2999-01-01"}
    assert ledger_mod.deferred(later, "2999-01-02") == {}
    # A second write replaces our fields rather than stacking them.
    again = ledgerw.set_state(later, "a-W3", status="failed (2026-09-28)")
    line = next(ln for ln in again.splitlines() if "`a-W3`" in ln)
    assert line.count("status:") == 1 and "after:`2999-01-01`" in line


def test_ticking_a_row_carries_its_open_needs_to_its_dependents():
    # a-W3 needs a-W2 needs (open) b-W1: ticking a-W2 must not let a-W3 race b-W1.
    text = LEDGER.replace("`a-W2` · dir:`alpha` · needs:`a-W1`", "`a-W2` · dir:`alpha` · needs:`b-W1`")
    ticked = ledgerw.set_state(text, "a-W2", tick=True)
    out, changed = ledgerw.carry_needs(ticked, "a-W2")
    assert changed == ["a-W3"]
    assert ledger_mod.parse(out)["a-W3"] == {"a-W2", "b-W1"}
    line = next(ln for ln in out.splitlines() if "`a-W3`" in ln)
    assert "needs:`b-W1` `a-W2`" in line  # first, where a whitespace-only reader looks


def test_a_follow_up_row_is_validated_and_lands_in_its_section():
    with pytest.raises(ledgerw.ReportError, match="already has a ledger row"):
        ledgerw.check_row(LEDGER, "a-W2", [])
    with pytest.raises(ledgerw.ReportError, match="names no ledger row"):
        ledgerw.check_row(LEDGER, "a-W9", ["zz-W1"])
    with pytest.raises(ledgerw.ReportError, match="not a phase id"):
        ledgerw.check_row(LEDGER, "9 bad", [])
    ledgerw.check_row(LEDGER, "a-W4", ["a-W3"])

    row = ledgerw.make_row("a-W4", "the **fourth** wave", ["a-W3"], ["alpha", "gamma"], [])
    assert row == "- [ ] `a-W4` · dir:`alpha`+`gamma` · needs:`a-W3` · **the fourth wave**"
    out = ledgerw.insert_row(LEDGER, "a-W1", row)
    lines = out.splitlines()
    assert lines.index(row) == lines.index(next(ln for ln in lines if "`a-W3`" in ln)) + 1
    assert ledger_mod.parse(out)["a-W4"] == {"a-W3"}


def test_a_cycle_is_refused():
    with pytest.raises(ledgerw.ReportError, match="cycle"):
        ledgerw.check_row(LEDGER.replace("needs:`a-W1` · **the second", "needs:`a-W5` · **the second"),
                          "a-W5", ["a-W3"])


# -- history files -------------------------------------------------------------
def test_history_appends_per_family_and_splits_past_the_limit(tmp_path):
    ledgerw.append_history(tmp_path, "h", 0, "read-W1", ledgerw.entry("d1 · done", "alpha"), "t1")
    ledgerw.append_history(tmp_path, "h", 0, "read-W2", ledgerw.entry("d1 · done", "bravo"))
    ledgerw.append_history(tmp_path, "h", 0, "read-W1", ledgerw.entry("d2 · note", "# not a heading"))
    fam = (tmp_path / "h" / "read.md").read_text()
    assert fam.index("## `read-W1` — t1") < fam.index("### d2 · note") < fam.index("## `read-W2`")
    assert "\\# not a heading" in fam
    assert "bravo" in ledgerw.history_text(tmp_path, "h", "read-W2")
    assert "alpha" not in ledgerw.history_text(tmp_path, "h", "read-W2")

    ledgerw.append_history(tmp_path, "h", 1, "read-W3", ledgerw.entry("d3", "x" * 2000))
    assert not (tmp_path / "h" / "read.md").exists()
    per = tmp_path / "h" / "read" / "read-W1.md"
    assert per.read_text().startswith("# `read-W1` — t1")
    assert "### d2 · note" in per.read_text()
    ledgerw.append_history(tmp_path, "h", 1, "read-W1", ledgerw.entry("d4", "four"))
    assert "four" in ledgerw.history_text(tmp_path, "h", "read-W1")
    assert ledgerw.family("contract-P0-ops") == "contract" and ledgerw.family("U0") == "U0"


# -- end to end on git -------------------------------------------------------------
def _project(tmp_path: Path, monkeypatch, gate: str = "") -> tuple:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True)
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", "-b", "master", str(project)], check=True)
    for k, v in (("user.email", "swarm@test"), ("user.name", "swarm"), ("commit.gpgsign", "false")):
        _git(project, "config", k, v)
    (project / "docs").mkdir()
    (project / "docs" / "PHASE-LEDGER.md").write_text(LEDGER)
    (project / ".swarm.toml").write_text(
        f'[tasks]\nledger = "docs/PHASE-LEDGER.md"\nledger_gate = "{gate}"\n')
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-q", "-u", "origin", "master")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_SLUG", "ledgerw")
    monkeypatch.delenv("SWARM_SESSION_ID", raising=False)
    cfg = load(project_dir=str(project))
    return cfg, project, origin


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def test_a_landed_phase_is_ticked_noted_committed_and_pushed(tmp_path, monkeypatch):
    cfg, project, origin = _project(tmp_path, monkeypatch)
    log = Log(cfg.supervisor_log)
    try:
        notes_mod.add(cfg, "a-W2", "kept the old wire shape", "decision")
        ledgerw.queue(cfg, "a-W2", {"kind": "outcome", "outcome": "ok", "note": "built it"})
        ledgerw.queue(cfg, "a-W2", {"kind": "lesson", "phase": "a-W2", "text": "Measure first. Then fix.",
                                    "title": ""})
        # Not landed yet: nothing is written.
        assert ledgerw.flush(cfg, log, {}).touched == []
        got = ledgerw.flush(cfg, log, {"a-W2": "ok"})
        assert got.released and "a-W2 done" in got.touched
        text = (project / "docs" / "PHASE-LEDGER.md").read_text()
        assert "a-W2" in ledger_mod.ticked(text)
        hist = ledgerw.history_text(project, cfg.history_dir, "a-W2")
        assert "· done" in hist and "built it" in hist and "kept the old wire shape" in hist
        assert "## (" in (project / "tasks" / "lessons.md").read_text()
        assert ledgerw.pending(cfg) == {}
        assert _git(project, "status", "--porcelain") == ""
        assert "ledger: a-W2" in _git(origin, "log", "-1", "--format=%s", "master")
    finally:
        log.close()


def test_a_failed_phase_is_not_ticked_even_if_it_says_ok(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    log = Log(cfg.supervisor_log)
    try:
        ledgerw.queue(cfg, "a-W2", {"kind": "outcome", "outcome": "ok", "note": "claims ok"})
        ledgerw.flush(cfg, log, {"a-W2": "fail"})
        text = (project / "docs" / "PHASE-LEDGER.md").read_text()
        assert "a-W2" not in ledger_mod.ticked(text)
        assert "status: failed" in text
    finally:
        log.close()


def test_a_later_with_no_date_is_recorded_as_blocked(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    log = Log(cfg.supervisor_log)
    try:
        ledgerw.queue(cfg, "a-W2", {"kind": "outcome", "outcome": "later", "note": "", "after": ""})
        ledgerw.flush(cfg, log, {"a-W2": "fail"})
        text = (project / "docs" / "PHASE-LEDGER.md").read_text()
        assert "status: blocked (" in text and "after:" not in text
    finally:
        log.close()


def test_a_checkout_that_cannot_take_it_holds_the_report(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    log = Log(cfg.supervisor_log)
    try:
        led = project / "docs" / "PHASE-LEDGER.md"
        led.write_text(led.read_text() + "\nsomeone's edit\n")
        ledgerw.queue(cfg, "a-W2", {"kind": "outcome", "outcome": "ok", "note": ""})
        assert ledgerw.flush(cfg, log, {"a-W2": "ok"}).touched == []
        assert "a-W2" in ledgerw.pending(cfg)
        assert "LEDGER-HELD" in cfg.supervisor_log.read_text()
        _git(project, "checkout", "--", "docs/PHASE-LEDGER.md")
        assert ledgerw.flush(cfg, log, {"a-W2": "ok"}).touched
        assert ledgerw.pending(cfg) == {}
    finally:
        log.close()


def test_a_follow_up_the_gate_rejects_is_refused_and_recorded(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch, gate="false")
    log = Log(cfg.supervisor_log)
    try:
        monkeypatch.setenv("SWARM_SESSION_ID", "worker:a-W2")
        assert ledgerw.file_follow_up(cfg, "a-W2", "a-W9", "more", ["a-W3"], [], [], "scope") == "a-W2"
        with pytest.raises(ledgerw.ReportError):  # its id is taken by the queued one
            ledgerw.file_follow_up(cfg, "a-W2", "a-W9", "again", [], [], [], "")
        got = ledgerw.flush(cfg, log, {"a-W2": "ok"})
        assert got.refused and "a-W9" not in ledger_mod.parse((project / "docs/PHASE-LEDGER.md").read_text())
        assert "follow-up `a-W9` refused" in ledgerw.history_text(project, cfg.history_dir, "a-W2")
    finally:
        log.close()


def test_record_and_follow_up_through_the_cli(tmp_path, monkeypatch, capsys):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    args = ["--project-dir", str(project)]
    assert cli.main(args + ["record", "b-W1", "done", "the owner picked B"]) == 0
    assert cli.main(args + ["record", "zz-W1", "done", "x"]) == 2
    assert cli.main(args + ["record", "a-W3", "later", "needs a week"]) == 2  # no --after
    assert cli.main(args + ["follow-up", "b-W1", "b-W2", "--title", "beta grows",
                            "--needs", "b-W1", "--dir", "beta", "it must grow"]) == 0
    assert cli.main(args + ["follow-up", "b-W1", "b-W3", "--title", "x", "--needs", "nope"]) == 2
    out = capsys.readouterr().out
    assert "queued" in out and "do not edit the ledger" in out
    log = Log(cfg.supervisor_log)
    try:
        ledgerw.flush(cfg, log, {})
    finally:
        log.close()
    text = (project / "docs" / "PHASE-LEDGER.md").read_text()
    assert "b-W1" in ledger_mod.ticked(text)
    assert ledger_mod.parse(text)["b-W2"] == {"b-W1"}
    assert "it must grow" in ledgerw.history_text(project, cfg.history_dir, "b-W2")
    assert "the owner picked B" in ledgerw.history_text(project, cfg.history_dir, "b-W1")


def test_the_supervisor_ticks_only_after_the_merge(tmp_path, monkeypatch):
    from swarm_orchestrator.supervisor import Supervisor

    cfg, project, _origin = _project(tmp_path, monkeypatch)
    log = Log(cfg.supervisor_log)
    try:
        wt = gitq.worktree_add(cfg, "a-W2", log)
        (wt / "code.txt").write_text("built")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", "a-W2 work")
        state_mod.init_state(cfg)
        with state_mod.transaction(cfg) as st:
            st.claim_slot("a-W2")
        ledgerw.queue(cfg, "a-W2", {"kind": "outcome", "outcome": "ok", "note": "done"})
        sup = Supervisor(cfg)
        sup._on_done("a-W2", "ok")
        assert (project / "code.txt").read_text() == "built"
        text = (project / "docs" / "PHASE-LEDGER.md").read_text()
        assert "a-W2" in ledger_mod.ticked(text)
        subjects = _git(project, "log", "--format=%s", "-3").splitlines()
        assert subjects[0].startswith("ledger: a-W2") and "Merge" in subjects[1]
    finally:
        log.close()


def test_a_later_phase_waits_for_its_date_then_comes_back(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    log = Log(cfg.supervisor_log)
    try:
        state_mod.init_state(cfg)
        ledgerw.queue(cfg, "a-W2", {"kind": "outcome", "outcome": "later", "note": "a week of data",
                                    "after": "2999-01-01"})
        ledgerw.flush(cfg, log, {"a-W2": "fail"})
        with state_mod.transaction(cfg) as st:
            st.done["a-W2"] = "fail"
        text = (project / "docs" / "PHASE-LEDGER.md").read_text()
        assert "status: later, after 2999-01-01" in text
        assert master_mod.build_context(cfg, state_mod.read(cfg))["deferred"] == {"a-W2": "2999-01-01"}
        assert ledgerw.release_due(cfg, log) == []

        (project / "docs" / "PHASE-LEDGER.md").write_text(text.replace("2999-01-01", "2000-01-01"))
        cfg.done_dir.mkdir(parents=True, exist_ok=True)
        (cfg.done_dir / "a-W2.fail").write_text("a-W2 fail x\n")
        assert ledgerw.release_due(cfg, log) == ["a-W2"]
        assert "a-W2" not in state_mod.read(cfg).done
        assert not (cfg.done_dir / "a-W2.fail").exists()
        assert "a-W2" in master_mod.build_context(cfg, state_mod.read(cfg))["ready"]
    finally:
        log.close()


def test_swarm_done_queues_the_outcome_and_later_pages_nobody(tmp_path, monkeypatch):
    from swarm_orchestrator import launch as launch_mod

    cfg, _project_dir, _origin = _project(tmp_path, monkeypatch)
    res = launch_mod.done(cfg, "a-W3", "later", "needs a week of data", after="2999-01-01")
    assert res.status == "fail" and res.ping == "skipped"
    assert "(as `later`)" in res.render() and "do not edit the ledger" in res.render()
    queued = ledgerw.pending(cfg)["a-W3"]["outcome"]
    assert queued["outcome"] == "later" and queued["after"] == "2999-01-01"
    assert not (tmp_path / "tg.log").exists() or "a-W3" not in (tmp_path / "tg.log").read_text()


def test_the_web_detail_sheet_shows_the_history_under_the_row(tmp_path):
    from types import SimpleNamespace

    from swarm_orchestrator.web import detail

    ledgerw.append_history(tmp_path, "h", 0, "a-W1", ledgerw.entry("d · done", "what it did"))
    cfg = SimpleNamespace(project_dir=tmp_path, history_dir="h")
    text = detail._with_history(cfg, "a-W1", "- [x] `a-W1` · **t**")
    assert text.startswith("- [x] `a-W1`") and "what it did" in text
    assert detail._with_history(cfg, "zz-W1", "row") == "row"
