"""``[tasks].ledger_in_merge``: a landed phase is one commit on the target branch
(real git, no tmux, no claude).

A phase that landed used to leave two commits side by side in the project's
history: the merge of its branch, and the ledger writer's ``ledger:`` commit
with its tick. These tests pin the replacement: the reports due with the phase
are written into its own merge commit, between the merge and the push, so the
target branch reads one line per landed phase and is pushed once. Everything
that cannot ride keeps the separate commit, and a report is never lost or
written twice on the way.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from swarm_orchestrator import cli, gitq, landing, ledgerw, pace
from swarm_orchestrator import ledger as ledger_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.logutil import Log

from test_ledgerw import _git, _note, _project
from test_multirepo import _cfg as _workspace_cfg
from test_multirepo import _make_workspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

LEDGER = "docs/PHASE-LEDGER.md"
OFF = "ledger_in_merge = false\n"


def _built(cfg, log: Log, phase: str, files: dict[str, str], outcome: str = "ok") -> Path:
    """A worker's phase, finished: ``files`` committed on its branch (none: it
    made no commit in the project), its slot held and its outcome queued."""
    wt = gitq.worktree_add(cfg, phase, log)
    for rel, text in files.items():
        (wt / rel).write_text(text)
    if files:
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", f"{phase} work")
    with state_mod.transaction(cfg) as st:
        st.claim_slot(phase)
    ledgerw.queue(cfg, phase, {"kind": "outcome", "outcome": outcome, "note": "built it"})
    return wt


def _supervisor(cfg):
    from swarm_orchestrator.supervisor import Supervisor

    state_mod.init_state(cfg)
    return Supervisor(cfg)


def _count_pushes(project: Path, runs: Path) -> None:
    hook = project / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\necho run >> '{runs}'\n")
    hook.chmod(0o755)


def _subjects(project: Path, since: str) -> list[str]:
    """The target branch's own history since ``since``, newest first."""
    return _git(project, "log", "--first-parent", "--format=%s", f"{since}..HEAD").splitlines()


def _head(project: Path) -> str:
    return _git(project, "rev-parse", "HEAD").strip()


def _ticked(project: Path, rev: str = "HEAD") -> set[str]:
    return ledger_mod.ticked(_git(project, "show", f"{rev}:{LEDGER}"))


def test_a_landed_phase_is_one_commit_holding_its_code_and_its_tick(tmp_path, monkeypatch):
    cfg, project, origin = _project(tmp_path, monkeypatch)
    runs = tmp_path / "pushes"
    _count_pushes(project, runs)
    base = _head(project)
    sup = _supervisor(cfg)
    try:
        _built(cfg, sup.log, "a-W2", {"code.txt": "built"})
        monkeypatch.setenv("SWARM_SESSION_ID", "worker:a-W2")
        ledgerw.file_follow_up(cfg, "a-W2", "a-W9", "more", ["a-W3"], [], [], "scope")
        monkeypatch.delenv("SWARM_SESSION_ID")
        ledgerw.queue(cfg, "a-W2", {"kind": "lesson", "phase": "a-W2", "text": "Measure first.",
                                    "title": ""})
        sup._on_done("a-W2", "ok")
    finally:
        sup.log.close()
    assert _subjects(project, base) == [
        "Merge branch 'swarm/a-W2': a-W2 done; follow-up a-W9; lesson from a-W2"]
    # Still the merge of the worker's branch onto what main was, and nothing
    # in it but the worker's file and the three things the swarm writes.
    assert _git(project, "rev-parse", "HEAD^1").strip() == base
    assert _git(project, "log", "-1", "--format=%s", "HEAD^2").strip() == "a-W2 work"
    assert _git(project, "diff", "--name-only", base, "HEAD").split() == [
        "code.txt", LEDGER, "docs/phases/a.md", "tasks/lessons.md"]
    assert "a-W2" in _ticked(project) and "a-W9" in ledger_mod.parse(
        _git(project, "show", f"HEAD:{LEDGER}"))
    assert _git(project, "status", "--porcelain") == "" and ledgerw.pending(cfg) == {}
    assert state_mod.read(cfg).done == {"a-W2": "ok"}
    # The branch went as merged work goes: nothing had to be kept aside for it.
    assert not gitq.branch_exists(project, "swarm/a-W2") and gitq.attic_refs(project) == []
    # Pushed once, and the commit origin has is the amended one.
    assert _git(origin, "rev-parse", "master") == _git(project, "rev-parse", "HEAD")
    assert runs.read_text().splitlines() == ["run"]
    logged = cfg.supervisor_log.read_text()
    assert "LEDGER a-W2 done" in logged and "TARGET-AMEND" in logged
    assert "TARGET-COMMIT" not in logged
    # Whoever reads the history still finds the phase and its tick there.
    assert landing.landed_since(project, "master", base) == ["a-W2"]
    assert pace.load(cfg).ticks["a-W2"][1] == 1


def test_the_target_branch_reads_one_line_per_landed_phase(tmp_path, monkeypatch):
    cfg, project, origin = _project(tmp_path, monkeypatch)
    sup = _supervisor(cfg)
    try:
        for phase, name in (("a-W2", "two.txt"), ("b-W1", "beta.txt"), ("a-W3", "three.txt")):
            _built(cfg, sup.log, phase, {name: phase})
            sup._on_done(phase, "ok")
    finally:
        sup.log.close()
    assert _git(project, "log", "--first-parent", "--format=%s").splitlines() == [
        "Merge branch 'swarm/a-W3': a-W3 done",
        "Merge branch 'swarm/b-W1': b-W1 done",
        "Merge branch 'swarm/a-W2': a-W2 done",
        "init",
    ]
    assert {"a-W2", "a-W3", "b-W1"} <= _ticked(project)
    assert _git(origin, "rev-parse", "master") == _git(project, "rev-parse", "HEAD")


def test_a_phase_with_no_commit_in_the_project_adds_one_ledger_commit(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    base = _head(project)
    sup = _supervisor(cfg)
    try:
        _built(cfg, sup.log, "a-W2", {})
        sup._on_done("a-W2", "ok")
    finally:
        sup.log.close()
    assert _subjects(project, base) == ["ledger: a-W2 done"]
    assert "a-W2" in _ticked(project) and ledgerw.pending(cfg) == {}


def test_a_held_note_rides_in_the_merge_commit(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    base = _head(project)
    sup = _supervisor(cfg)
    try:
        _note(cfg, "a-W3", "first finding")
        assert sup._flush_ledger({}) is False
        assert sup._ledger_held_until and _head(project) == base  # held for a shared commit
        _built(cfg, sup.log, "b-W1", {"beta.txt": "built"})
        sup._on_done("b-W1", "ok")
        assert sup._ledger_held_until == 0.0
    finally:
        sup.log.close()
    assert _subjects(project, base) == ["Merge branch 'swarm/b-W1': b-W1 done; a-W3 note"]
    assert "first finding" in _git(project, "show", "HEAD:docs/phases/a.md")
    assert ledgerw.pending(cfg) == {}


def test_an_amend_that_fails_leaves_the_merge_and_the_next_flush_writes_the_report(
        tmp_path, monkeypatch):
    cfg, project, origin = _project(tmp_path, monkeypatch)
    base = _head(project)
    real = gitq._git

    def no_amend(repo, *args, **kw):
        if "--amend" in args:
            raise gitq.GitError("the amend was refused")
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitq, "_git", no_amend)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        _built(cfg, log, "a-W2", {"code.txt": "built"})
        ledgerw.queue(cfg, "a-W2", {"kind": "lesson", "phase": "a-W2", "text": "Measure first.",
                                    "title": ""})
        ride = ledgerw.Ride(cfg, log, "a-W2", "ok")
        assert gitq.integrate(cfg, "a-W2", log, {}, ride) == gitq.MERGED
        ride.settle()
        # The merge stands and is pushed as it is; the ledger, the history and
        # the lessons the report began to write are back as they were (the last
        # two did not exist), and the report waits.
        assert _subjects(project, base) == ["Merge branch 'swarm/a-W2'"]
        assert (project / "code.txt").read_text() == "built"
        assert _git(project, "status", "--porcelain") == ""
        assert "a-W2" not in _ticked(project) and "a-W2" in ledgerw.pending(cfg)
        assert _git(origin, "rev-parse", "master") == _git(project, "rev-parse", "HEAD")
        logged = cfg.supervisor_log.read_text()
        assert "LEDGER-RIDE-ERROR a-W2" in logged and "LEDGER a-W2 done" not in logged
        ledgerw.flush(cfg, log, {"a-W2": "ok"})
    finally:
        log.close()
    assert _subjects(project, base) == [
        "ledger: a-W2 done; lesson from a-W2", "Merge branch 'swarm/a-W2'"]
    assert "a-W2" in _ticked(project) and ledgerw.pending(cfg) == {}
    assert ledgerw.history_text(project, cfg.history_dir, "a-W2").count("· done") == 1
    assert (project / "tasks" / "lessons.md").read_text().count("## (") == 1


def test_a_commit_made_by_hand_meanwhile_is_never_amended(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    refused = []

    def write() -> None:
        (project / LEDGER).write_text("the swarm's edit")
        (project / "mine.txt").write_text("the owner's")
        _git(project, "add", "mine.txt")
        _git(project, "commit", "-qm", "by hand", "--", "mine.txt")

    def ride() -> None:
        with pytest.raises(gitq.GitError, match="HEAD moved") as exc:
            gitq.amend_merge(cfg, "a-W2", [LEDGER], write, "a-W2 done", log)
        refused.append(exc)

    try:
        _built(cfg, log, "a-W2", {"code.txt": "built"})
        assert gitq.integrate(cfg, "a-W2", log, {}, ride) == gitq.MERGED
    finally:
        log.close()
    assert refused and _git(project, "log", "--format=%s", "-2").splitlines() == [
        "by hand", "Merge branch 'swarm/a-W2'"]
    assert _git(project, "status", "--porcelain") == ""


def test_an_outcome_that_does_not_tick_rides_without_a_tick(tmp_path, monkeypatch):
    """What the phase last reported decides, as in a flush: work that merged
    under a report that says it failed lands with that status, unticked."""
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    base = _head(project)
    sup = _supervisor(cfg)
    try:
        _built(cfg, sup.log, "a-W2", {"code.txt": "built"}, outcome="fail")
        sup._on_done("a-W2", "ok")
    finally:
        sup.log.close()
    assert _subjects(project, base) == ["Merge branch 'swarm/a-W2': a-W2 failed"]
    text = _git(project, "show", f"HEAD:{LEDGER}")
    assert "a-W2" not in ledger_mod.ticked(text) and "status: failed" in text


def test_a_report_filed_while_the_merge_is_amended_stays_queued(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    base = _head(project)
    real, late = ledgerw.apply, {"kind": "lesson", "phase": "a-W2", "text": "Late.", "title": ""}

    def apply(c, root, key, *rest):
        if key == "a-W2":
            ledgerw.queue(cfg, "a-W2", late)
        return real(c, root, key, *rest)

    monkeypatch.setattr(ledgerw, "apply", apply)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        _built(cfg, log, "a-W2", {"code.txt": "built"})
        ride = ledgerw.Ride(cfg, log, "a-W2", "ok")
        assert gitq.integrate(cfg, "a-W2", log, {}, ride) == gitq.MERGED
        left = ledgerw.pending(cfg)["a-W2"]
        assert left["outcome"] is None and [op["text"] for op in left["ops"]] == ["Late."]
        ledgerw.flush(cfg, log, {"a-W2": "ok"})
    finally:
        log.close()
    assert _subjects(project, base) == [
        "ledger: lesson from a-W2", "Merge branch 'swarm/a-W2': a-W2 done"]
    assert ledgerw.history_text(project, cfg.history_dir, "a-W2").count("· done") == 1


def test_a_stray_file_where_the_swarm_writes_holds_the_ride_not_the_merge(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    base = _head(project)
    (project / "docs" / "phases").mkdir()
    (project / "docs" / "phases" / "mine.md").write_text("someone's own notes")
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        _built(cfg, log, "a-W2", {"code.txt": "built"})
        ride = ledgerw.Ride(cfg, log, "a-W2", "ok")
        assert gitq.integrate(cfg, "a-W2", log, {}, ride) == gitq.MERGED
        # Not the merge of this phase: nothing is written at all.
        held = gitq.amend_merge(cfg, "b-W1", [LEDGER], lambda: pytest.fail("wrote"), "x", log)
    finally:
        log.close()
    assert held.status == gitq.HELD and "not the merge of swarm/b-W1" in held.reason
    assert _subjects(project, base) == ["Merge branch 'swarm/a-W2'"]
    assert "a-W2" in ledgerw.pending(cfg) and ride.applied is None
    assert "LEDGER-RIDE-HELD a-W2 done uncommitted changes" in cfg.supervisor_log.read_text()
    assert (project / "docs" / "phases" / "mine.md").read_text() == "someone's own notes"


def test_with_the_key_off_a_landing_is_two_commits_and_two_pushes(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch, extra=OFF)
    assert cfg.ledger_in_merge is False
    runs = tmp_path / "pushes"
    _count_pushes(project, runs)
    base = _head(project)
    sup = _supervisor(cfg)
    try:
        _built(cfg, sup.log, "a-W2", {"code.txt": "built"})
        sup._on_done("a-W2", "ok")
    finally:
        sup.log.close()
    assert _subjects(project, base) == ["ledger: a-W2 done", "Merge branch 'swarm/a-W2'"]
    assert "a-W2" in _ticked(project) and "a-W2" not in _ticked(project, "HEAD~1")
    assert runs.read_text().splitlines() == ["run", "run"]


def test_an_integrate_by_hand_leaves_the_report_to_the_flush(tmp_path, monkeypatch, capsys):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    base = _head(project)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        _built(cfg, log, "a-W2", {"code.txt": "built"})
        assert cli.cmd_integrate(cfg, "a-W2") == 0
        assert _subjects(project, base) == ["Merge branch 'swarm/a-W2'"]
        assert "a-W2" not in _ticked(project) and "a-W2" in ledgerw.pending(cfg)
        ledgerw.flush(cfg, log, {"a-W2": "ok"})
    finally:
        log.close()
    assert _subjects(project, base) == ["ledger: a-W2 done", "Merge branch 'swarm/a-W2'"]


def test_a_merge_that_conflicted_and_was_resolved_later_keeps_the_separate_commit(
        tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        _built(cfg, log, "a-W2", {"code.txt": "the worker's"})
        (project / "code.txt").write_text("main's")
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", "main moved")
        base = _head(project)
        ride = ledgerw.Ride(cfg, log, "a-W2", "ok")
        assert gitq.integrate(cfg, "a-W2", log, {}, ride) == gitq.CONFLICT
        # No merge yet, so no tick: the row is ticked only once its work is in.
        assert _head(project) == base and "a-W2" in ledgerw.pending(cfg)
        assert "a-W2" not in ledger_mod.ticked((project / LEDGER).read_text())
        (project / "code.txt").write_text("both")
        _git(project, "add", "-A")
        _git(project, "commit", "-q", "--no-edit")
        resolved = _head(project)
        ride = ledgerw.Ride(cfg, log, "a-W2", "ok")
        assert gitq.integrate(cfg, "a-W2", log, {}, ride) == gitq.MERGED
        assert _head(project) == resolved and "a-W2" in ledgerw.pending(cfg)
        ledgerw.flush(cfg, log, {"a-W2": "ok"})
    finally:
        log.close()
    subjects = _subjects(project, base)
    assert subjects[0] == "ledger: a-W2 done" and len(subjects) == 2
    assert subjects[1].startswith("Merge branch 'swarm/a-W2'") and "done" not in subjects[1]


def test_the_post_merge_command_and_the_push_see_the_amended_commit(tmp_path, monkeypatch):
    seen = tmp_path / "seen"
    script = tmp_path / "post-merge.sh"
    script.write_text(f"git log -1 --format=%s > '{seen}'\ngit status --porcelain >> '{seen}'\n")
    extra = f'[git]\npost_merge = {{ "." = "sh {script}" }}\n[build]\nlight = ["sh *"]\n'
    cfg, project, origin = _project(tmp_path, monkeypatch, extra=extra)
    runs = tmp_path / "pushes"
    _count_pushes(project, runs)
    sup = _supervisor(cfg)
    try:
        _built(cfg, sup.log, "a-W2", {"code.txt": "built"})
        sup._on_done("a-W2", "ok")
    finally:
        sup.log.close()
    # The command ran on the commit that was then pushed, with nothing left to
    # commit beside it: the tick was already in.
    assert seen.read_text() == "Merge branch 'swarm/a-W2': a-W2 done\n"
    assert _git(origin, "rev-parse", "master") == _git(project, "rev-parse", "HEAD")
    assert runs.read_text().splitlines() == ["run"]


def test_a_push_that_loses_a_race_keeps_the_amended_commit_and_writes_nothing_twice(
        tmp_path, monkeypatch):
    cfg, project, origin = _project(tmp_path, monkeypatch)
    base = _head(project)
    real, moved = gitq._push_result, []

    def raced(repo, main, log, **kw):
        if not moved:  # someone else pushes between the amend and our push
            tree = _git(project, "rev-parse", "origin/master^{tree}").strip()
            theirs = _git(project, "commit-tree", tree, "-p", "origin/master",
                          "-m", "pushed by someone else").strip()
            _git(project, "push", "-q", "origin", f"{theirs}:master")
            moved.append(theirs)
        return real(repo, main, log, **kw)

    monkeypatch.setattr(gitq, "_push_result", raced)
    sup = _supervisor(cfg)
    try:
        _built(cfg, sup.log, "a-W2", {"code.txt": "built"})
        sup._on_done("a-W2", "ok")
    finally:
        sup.log.close()
    assert _subjects(project, base) == [
        "Merge remote-tracking branch 'origin/master'", "Merge branch 'swarm/a-W2': a-W2 done"]
    assert _git(origin, "rev-parse", "master") == _git(project, "rev-parse", "HEAD")
    assert _git(project, "merge-base", "--is-ancestor", moved[0], "HEAD") == ""
    assert "a-W2" in _ticked(project) and ledgerw.pending(cfg) == {}
    assert ledgerw.history_text(project, cfg.history_dir, "a-W2").count("· done") == 1
    assert "PUSH-RETRY 1" in cfg.supervisor_log.read_text()


def test_a_push_that_conflicts_after_the_amend_is_resolved_and_writes_nothing_twice(
        tmp_path, monkeypatch):
    cfg, project, origin = _project(tmp_path, monkeypatch)
    base = _head(project)
    real, moved = gitq._push_result, []

    def raced(repo, main, log, **kw):
        if not moved:  # someone else pushes their own code.txt first
            ext = tmp_path / "ext"
            _git(project, "worktree", "add", "-q", "--detach", str(ext), "origin/master")
            (ext / "code.txt").write_text("theirs")
            _git(ext, "add", "-A")
            _git(ext, "commit", "-qm", "pushed by someone else")
            _git(ext, "push", "-q", "origin", "HEAD:master")
            moved.append(ext)
        return real(repo, main, log, **kw)

    monkeypatch.setattr(gitq, "_push_result", raced)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        _built(cfg, log, "a-W2", {"code.txt": "built"})
        ride = ledgerw.Ride(cfg, log, "a-W2", "ok")
        assert gitq.integrate(cfg, "a-W2", log, {}, ride) == gitq.CONFLICT
        ride.settle()
        # Held mid-merge with origin: the phase's own merge is made, and holds its tick.
        assert _git(project, "log", "-1", "--format=%s").strip() == (
            "Merge branch 'swarm/a-W2': a-W2 done")
        assert "a-W2" in _ticked(project) and ledgerw.pending(cfg) == {}
        (project / "code.txt").write_text("both")
        _git(project, "add", "-A")
        _git(project, "commit", "-q", "--no-edit")
        ride = ledgerw.Ride(cfg, log, "a-W2", "ok")
        assert gitq.integrate(cfg, "a-W2", log, {}, ride) == gitq.MERGED
        assert ledgerw.flush(cfg, log, {"a-W2": "ok"}).touched == []
    finally:
        log.close()
    subjects = _subjects(project, base)
    assert len(subjects) == 2 and subjects[1] == "Merge branch 'swarm/a-W2': a-W2 done"
    assert subjects[0].startswith("Merge remote-tracking branch 'origin/master'")
    assert _git(origin, "rev-parse", "master") == _git(project, "rev-parse", "HEAD")
    assert ledgerw.history_text(project, cfg.history_dir, "a-W2").count("· done") == 1


def test_a_merge_the_swarm_resolved_by_itself_takes_its_reports_too(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    journal = "# Changelog\n\n## 0.1.0\n- first\n"
    (project / "CHANGELOG.md").write_text(journal)
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "a journal")
    cfg.git_auto_resolve = {"CHANGELOG.md": "union"}
    sup = _supervisor(cfg)
    try:
        _built(cfg, sup.log, "a-W2", {
            "CHANGELOG.md": journal.replace("\n\n## 0.1.0", "\n\n## a-W2\n- its entry\n\n## 0.1.0")})
        (project / "CHANGELOG.md").write_text(
            journal.replace("\n\n## 0.1.0", "\n\n## main\n- another entry\n\n## 0.1.0"))
        _git(project, "commit", "-qam", "main's entry")
        base = _head(project)
        sup._on_done("a-W2", "ok")
    finally:
        sup.log.close()
    subjects = _subjects(project, base)
    assert len(subjects) == 1 and subjects[0].startswith("Merge branch 'swarm/a-W2': a-W2 done")
    text = _git(project, "show", "HEAD:CHANGELOG.md")
    assert "## a-W2" in text and "## main" in text and "a-W2" in _ticked(project)
    assert "INTEGRATE-AUTORESOLVED a-W2" in cfg.supervisor_log.read_text()


ROWS = "# Ledger\n\n- [ ] `pricing-P1` · dir:`pricing` · needs:— · **the price list**\n"


def _workspace(tmp_path: Path, monkeypatch) -> tuple:
    """A project with component repos inside it, and a checklist ledger."""
    project, repos = _make_workspace(tmp_path)
    (project / LEDGER).write_text(ROWS)
    _git(project, "commit", "-qam", "a checklist ledger")
    _git(project, "push", "-q", "origin", "master")
    return _workspace_cfg(monkeypatch, tmp_path, project), project, repos


def test_a_phase_whose_work_is_all_in_a_component_repo_adds_one_ledger_commit(
        tmp_path, monkeypatch):
    cfg, project, repos = _workspace(tmp_path, monkeypatch)
    base = _head(project)
    sup = _supervisor(cfg)
    try:
        wt = _built(cfg, sup.log, "pricing-P1", {})
        (wt / "pricing" / "code.txt").write_text("priced\n")
        _git(wt / "pricing", "commit", "-qam", "pricing-P1 work")
        sup._on_done("pricing-P1", "ok")
    finally:
        sup.log.close()
    assert (repos["pricing"] / "code.txt").read_text() == "priced\n"
    assert _subjects(project, base) == ["ledger: pricing-P1 done"]
    assert "pricing-P1" in _ticked(project) and ledgerw.pending(cfg) == {}


def test_a_component_that_conflicts_leaves_the_row_unticked(tmp_path, monkeypatch):
    cfg, project, repos = _workspace(tmp_path, monkeypatch)
    base = _head(project)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        wt = _built(cfg, log, "pricing-P1", {"notes.txt": "the project's half"})
        (wt / "pricing" / "code.txt").write_text("priced\n")
        _git(wt / "pricing", "commit", "-qam", "pricing-P1 work")
        (repos["pricing"] / "code.txt").write_text("someone else's\n")
        _git(repos["pricing"], "commit", "-qam", "main moved")
        ride = ledgerw.Ride(cfg, log, "pricing-P1", "ok")
        assert gitq.integrate(cfg, "pricing-P1", log, {}, ride) == gitq.CONFLICT
    finally:
        log.close()
    # Components land first: the project's half is not merged, so nothing rode.
    assert _head(project) == base and "pricing-P1" in ledgerw.pending(cfg)
    assert "pricing-P1" not in ledger_mod.ticked((project / LEDGER).read_text())


def test_with_lanes_on_a_landing_that_is_not_ready_writes_nothing(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch, extra="[lanes]\nenabled = true\n")
    base = _head(project)
    monkeypatch.setattr(landing, "_prepare", lambda *a, **kw: gitq.LANE_RED)  # its check is red
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        _built(cfg, log, "a-W2", {"code.txt": "built"})
        ride = ledgerw.Ride(cfg, log, "a-W2", "ok")
        assert gitq.integrate(cfg, "a-W2", log, {}, ride) == gitq.LANE_RED
    finally:
        log.close()
        landing._unflock(project)
    assert _head(project) == base and "a-W2" in ledgerw.pending(cfg)
    assert "a-W2" not in ledger_mod.ticked((project / LEDGER).read_text())


def test_with_lanes_on_a_landing_is_one_commit_too(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch, extra="[lanes]\nenabled = true\n")
    assert cfg.lanes_enabled
    base = _head(project)
    sup = _supervisor(cfg)
    try:
        _built(cfg, sup.log, "a-W2", {"code.txt": "built"})
        sup._on_done("a-W2", "ok")
    finally:
        sup.log.close()
    assert _subjects(project, base) == ["Merge branch 'swarm/a-W2': a-W2 done"]
    assert "a-W2" in _ticked(project) and ledgerw.pending(cfg) == {}
    assert "CARRY-SKIPPED a-W2 lanes" in cfg.supervisor_log.read_text()
