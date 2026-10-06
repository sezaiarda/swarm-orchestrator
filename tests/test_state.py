"""Unit tests: slot claim/flock, dependency resolver, FIFO line format."""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from swarm_orchestrator import ledger
from swarm_orchestrator.state import State


# -- dependency resolver --------------------------------------------------
def test_parse_and_ready_gating():
    graph = ledger.parse(
        "P0\nP1 needs:P0\nP2 needs:P0\nP4 needs:P1,P2 trailing note\n# comment\n"
    )
    assert graph == {"P0": set(), "P1": {"P0"}, "P2": {"P0"}, "P4": {"P1", "P2"}}
    # nothing done -> only the root is ready
    assert ledger.ready(graph, {}, set(), set()) == ["P0"]
    # P0 done -> P1,P2 ready; P4 still gated on P2
    assert ledger.ready(graph, {"P0": "ok"}, set(), set()) == ["P1", "P2"]
    # P1,P2 done -> P4 ready
    assert ledger.ready(graph, {"P0": "ok", "P1": "ok", "P2": "ok"}, set(), set()) == [
        "P4"
    ]


def test_ready_excludes_busy_and_excluded():
    graph = ledger.parse("P0\nP1 needs:P0\nP2 needs:P0\n")
    done = {"P0": "ok"}
    # P1 busy, P2 excluded -> neither ready
    assert ledger.ready(graph, done, {"P1"}, {"P2"}) == []


def test_prose_ledger_yields_no_false_phases():
    # A real markdown ledger line should not be misread as a phase declaration.
    graph = ledger.parse("## Phase 0 — the walking skeleton (done)\n- bullet\n")
    assert graph == {}


def test_markdown_checklist_ledger_ignores_prose():
    # A real project's markdown ledger: only `- [ ]`/`- [x]` checklist items are
    # phases; the surrounding legend/heading/prose-note lines must NOT leak in as
    # phantom deps-free phases (the bug: prose continuation words parsed as phases).
    md = (
        "# Ledger\n"
        "> legend prose that must not become a phase\n"
        "## Phase 0\n"
        "- [x] `frontend-P0` · dir:`frontend` · needs:— · shell + routing\n"
        "      _(2026: repos.txt uses master; shell over lazy groups; the note)_\n"
        "- [ ] `frontend-P1` · needs:`frontend-P0` `bundle-v0.1.0` · browse list\n"
        "- [ ] `I1` · needs:`frontend-P0` `billing-P1` · smoke test\n"
    )
    graph = ledger.parse(md)
    assert set(graph) == {"frontend-P0", "frontend-P1", "I1"}  # only the checklist items
    # needs: deps are captured from the `needs:` field only, then filtered to
    # known phase ids: the `dir:`frontend`` back-tick, the git tag `bundle-v0.1.0`, and
    # the undeclared `billing-P1` all drop; real phase deps stay.
    assert graph == {"frontend-P0": set(), "frontend-P1": {"frontend-P0"}, "I1": {"frontend-P0"}}
    for phantom in ("repos.txt", "shell", "the", "note", "legend", "Phase"):
        assert phantom not in graph
    assert ledger.validate(graph) == []  # no phantom cycles/unknowns


def test_markdown_ready_is_incomplete_minus_excluded():
    graph = ledger.parse(
        "- [x] `A` · done\n- [ ] `B` · next\n- [ ] `C` · blocked externally\n"
    )
    # A done, C excluded -> only B is "ready"; ready == what's left to build
    assert ledger.ready(graph, {"A": "ok"}, set(), {"C"}) == ["B"]
    # nothing left -> ready empty, so the master's launchable-empty stop-guard fires
    assert ledger.ready(graph, {"A": "ok", "B": "ok"}, set(), {"C"}) == []


def test_needs_list_tolerates_whitespace_but_keeps_trailing_note():
    # Spaces after the colon / commas must NOT silently drop dependencies.
    assert ledger.parse("P4 needs: P1, P2\n")["P4"] == {"P1", "P2"}
    assert ledger.parse("P4 needs:P1, P2\n")["P4"] == {"P1", "P2"}
    assert ledger.parse("P4 needs:P1,P2\n")["P4"] == {"P1", "P2"}
    # A prose trailing note (not a comma-continuation) is still ignored.
    assert ledger.parse("P4 needs:P1,P2 (blocked externally)\n")["P4"] == {"P1", "P2"}
    assert ledger.parse("P4 needs: P1, P2 optional-ish note\n")["P4"] == {"P1", "P2"}


def test_validate_detects_cycles_self_and_unknown_deps():
    issues = ledger.validate(ledger.parse("A needs:B\nB needs:A\nC needs:C\nD needs:Z\n"))
    joined = " | ".join(issues)
    assert "cycle" in joined  # A <-> B
    assert "self-dependency: C" in joined
    assert "unknown dependency: D needs Z" in joined
    # A clean ledger has no issues.
    assert ledger.validate(ledger.parse("P0\nP1 needs:P0\n")) == []


def test_validate_ignores_a_cycle_that_runs_through_a_landed_phase():
    """A landed phase can stall nothing, so a cycle through it is not a stall.

    A regression shape: an ordering edge added after the
    fact makes a CLOSED row "need" a later OPEN one, which already needs it.
    Unfiltered that reads as a live cycle, and a master told to stop on
    ``ledger_issues`` halted a run that had nothing wrong with it.
    """
    graph = ledger.parse("OLD needs:NEW\nNEW needs:OLD\nC needs:C\nD needs:Z\n")
    assert any("OLD" in i and "cycle" in i for i in ledger.validate(graph))
    # OLD has landed: that cycle is gone (C's self-dependency still reads as one).
    assert not any("OLD" in i for i in ledger.validate(graph, {"OLD"}))
    # A landed phase's own faults are history too; an open phase's are not.
    landed = ledger.validate(graph, {"OLD", "C"})
    assert not any("self-dependency: C" in i for i in landed)
    assert any("unknown dependency: D needs Z" in i for i in landed)
    # A cycle made only of phases that have NOT landed still reports.
    live = ledger.parse("A needs:B\nB needs:A\nDONE\n")
    assert any("cycle" in i for i in ledger.validate(live, {"DONE"}))


# -- slot accounting (in-memory) ------------------------------------------
def test_claim_and_free_slots():
    st = State.fresh(4)
    claimed = [st.claim_slot(f"P{i}") for i in range(4)]
    assert [s.id for s in claimed] == [0, 1, 2, 3]
    assert st.claim_slot("P4") is None  # no free slot
    assert st.any_busy()
    freed = st.free_slot_for("P2")
    assert freed is not None and freed.id == 2
    assert not freed.busy and freed.phase is None
    # freed slot is reused first
    assert st.claim_slot("P5").id == 2


def test_claim_rejects_duplicate_phase():
    """A phase never occupies two slots (else `done` can't free both)."""
    st = State.fresh(4)
    first = st.claim_slot("P1")
    assert first is not None and first.id == 0
    assert st.claim_slot("P1") is None  # duplicate refused
    other = st.claim_slot("P2")  # a different phase still claims
    assert other is not None and other.id == 1
    st.free_slot_for("P1")
    assert st.claim_slot("P1").id == 0  # reclaimable once freed


# -- config: state-dir slug is path-unique; max_workers is validated ------
def test_default_slug_is_unique_per_path(tmp_path):
    from swarm_orchestrator.config import _default_slug

    a = tmp_path / "left" / "myproject"
    b = tmp_path / "right" / "myproject"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    # Same basename, different parents -> distinct slugs (no shared state dir).
    assert _default_slug(a) != _default_slug(b)
    assert _default_slug(a).startswith("myproject-")


def test_max_workers_must_be_positive(tmp_path, monkeypatch):
    import pytest

    from swarm_orchestrator.config import load

    monkeypatch.delenv("SWARM_SLUG", raising=False)
    (tmp_path / ".swarm.toml").write_text("[swarm]\nmax_workers = 0\n")
    with pytest.raises(ValueError):
        load(project_dir=str(tmp_path))


# -- pre-trusting a worktree so claude never pops the folder-trust dialog --
def test_pretrust_dir_seeds_trust_additively(tmp_path, monkeypatch):
    import json

    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator.logutil import Log

    cfg_file = tmp_path / "claude.json"
    cfg_file.write_text(
        json.dumps({"projects": {"/other": {"hasTrustDialogAccepted": True, "lastCost": 1.5}}})
    )
    monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(cfg_file))
    log = Log(tmp_path / "l.log")
    try:
        wt = tmp_path / "wt" / "P1"
        wt.mkdir(parents=True)
        launch_mod.pretrust_dir(wt, log)
        data = json.loads(cfg_file.read_text())
        assert data["projects"][str(wt.resolve())]["hasTrustDialogAccepted"] is True
        assert data["projects"]["/other"]["lastCost"] == 1.5  # existing entry untouched
        launch_mod.pretrust_dir(wt, log)  # idempotent, no error
        assert (
            json.loads(cfg_file.read_text())["projects"][str(wt.resolve())][
                "hasTrustDialogAccepted"
            ]
            is True
        )
    finally:
        log.close()


def test_pretrust_dir_tolerates_missing_or_bad_config(tmp_path, monkeypatch):
    import json

    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator.logutil import Log

    log = Log(tmp_path / "l.log")
    try:
        wt = tmp_path / "wt"
        wt.mkdir()
        missing = tmp_path / "new.json"  # absent -> created
        monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(missing))
        launch_mod.pretrust_dir(wt, log)
        assert json.loads(missing.read_text())["projects"][str(wt.resolve())][
            "hasTrustDialogAccepted"
        ] is True
        # Unreadable -> NEVER written back: it is the owner's login and settings,
        # and a torn read of a file claude is rewriting looks exactly like this.
        monkeypatch.setattr(launch_mod, "PRETRUST_RETRY_S", 0.0)
        for text in ("{ not json", "[1, 2]", '{"projects": [], "oauthAccount": {"a": 1}}'):
            bad = tmp_path / "bad.json"
            bad.write_text(text)
            monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(bad))
            launch_mod.pretrust_dir(wt, log)  # never raises
            assert bad.read_text() == text
    finally:
        log.close()
    assert "PRETRUST-SKIPPED" in (tmp_path / "l.log").read_text()


def test_pretrust_dir_recovers_from_a_torn_read(tmp_path, monkeypatch):
    import json

    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator.logutil import Log

    wt = tmp_path / "wt"
    wt.mkdir()
    conf = tmp_path / "claude.json"
    conf.write_text(json.dumps({"oauthAccount": {"emailAddress": "x"}, "projects": {}}))
    monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(conf))
    monkeypatch.setattr(launch_mod, "PRETRUST_RETRY_S", 0.0)
    real = launch_mod._read_claude_config
    torn = [None]  # the first read is torn, the next is whole
    monkeypatch.setattr(launch_mod, "_read_claude_config",
                        lambda p: torn.pop() if torn else real(p))
    log = Log(tmp_path / "l.log")
    try:
        launch_mod.pretrust_dir(wt, log)
    finally:
        log.close()
    data = json.loads(conf.read_text())
    assert data["oauthAccount"] == {"emailAddress": "x"}
    assert data["projects"][str(wt.resolve())]["hasTrustDialogAccepted"] is True


def _pretrust_env(tmp_path, monkeypatch):
    """A claude config of the test's own, as two swarms on one machine share it."""
    import json

    conf = tmp_path / "claude.json"
    conf.write_text(json.dumps({"projects": {}}))
    monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(conf))
    monkeypatch.delenv("SWARM_STATE_DIR", raising=False)
    return conf


def test_pretrust_dir_waits_its_turn_behind_another_swarms_write(tmp_path, monkeypatch):
    """Two supervisors launch at the same moment (a usage window they share
    resets). Both rewrote the file from their own read: one entry was lost, or
    the live file was replaced by one still being written."""
    import fcntl
    import json
    import threading

    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator import machine
    from swarm_orchestrator.logutil import Log

    conf = _pretrust_env(tmp_path, monkeypatch)
    lock = machine.directory() / launch_mod.CLAUDE_LOCK
    lock.parent.mkdir(parents=True)
    log = Log(tmp_path / "l.log")
    try:
        with lock.open("a") as other:  # another swarm, mid-write
            fcntl.flock(other, fcntl.LOCK_EX)
            mine = threading.Thread(target=launch_mod.pretrust_dir, args=(tmp_path / "a", log))
            mine.start()
            mine.join(0.5)
            assert mine.is_alive() and json.loads(conf.read_text()) == {"projects": {}}
            conf.write_text(json.dumps({"projects": {"/theirs": {"hasTrustDialogAccepted": True}}}))
            fcntl.flock(other, fcntl.LOCK_UN)
        mine.join(10)
        assert not mine.is_alive()
    finally:
        log.close()
    projects = json.loads(conf.read_text())["projects"]
    assert projects["/theirs"] == {"hasTrustDialogAccepted": True}  # not written over
    assert projects[str((tmp_path / "a").resolve())]["hasTrustDialogAccepted"] is True


def test_pretrust_dir_gives_up_on_a_lock_that_stays_taken_and_touches_nothing(
        tmp_path, monkeypatch):
    """Its holder may be frozen (`swarm freeze`); a launch never waits on that."""
    import fcntl

    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator import machine
    from swarm_orchestrator.logutil import Log

    conf = _pretrust_env(tmp_path, monkeypatch)
    monkeypatch.setattr(launch_mod, "CLAUDE_LOCK_WAIT_S", 0.2)
    before = conf.read_text()
    lock = machine.directory() / launch_mod.CLAUDE_LOCK
    lock.parent.mkdir(parents=True)
    log = Log(tmp_path / "l.log")
    try:
        with lock.open("a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            launch_mod.pretrust_dir(tmp_path / "a", log)
    finally:
        log.close()
    assert conf.read_text() == before and sorted(p.name for p in tmp_path.iterdir() if
                                                 p.name.startswith("claude.json")) == ["claude.json"]
    assert "PRETRUST-SKIPPED" in (tmp_path / "l.log").read_text()


def test_pretrust_dir_writes_through_a_temp_file_of_its_own(tmp_path, monkeypatch):
    """The name was fixed (``.swarm-tmp``): two writers shared one temp file."""
    import os

    from swarm_orchestrator import launch as launch_mod
    from swarm_orchestrator.logutil import Log

    conf = _pretrust_env(tmp_path, monkeypatch)
    used: list[str] = []
    real = os.replace
    monkeypatch.setattr(launch_mod.os, "replace",
                        lambda src, dst: (used.append(str(src)), real(src, dst))[1])
    log = Log(tmp_path / "l.log")
    try:
        for name in ("a", "b"):
            launch_mod.pretrust_dir(tmp_path / name, log)
    finally:
        log.close()
    assert len(set(used)) == 2 and str(conf) + ".swarm-tmp" not in used
    assert all(u.startswith(str(conf) + ".swarm-tmp.") for u in used)
    assert [p.name for p in tmp_path.iterdir() if "swarm-tmp" in p.name] == []


# -- flock check-and-set under real concurrency ---------------------------
def test_concurrent_launch_never_double_claims(swarm):
    """8 concurrent `swarm launch` against 4 slots -> exactly 4 claim.

    Uses phase ids absent from the demo ledger so the dependency backstop can't
    deny any of them — this isolates the flock check-and-set (the four losers are
    denied purely for `no-free-slot`, not for unmet deps).
    """
    swarm.env["FAKE_WORKER_PARK"] = "1"  # parked workers: hold slot, never done

    results: list[int] = []
    lock = threading.Lock()

    def launch(phase: str) -> None:
        proc = swarm.cli("launch", phase, check=False, timeout=20)
        with lock:
            results.append(proc.returncode)

    threads = [threading.Thread(target=launch, args=(f"Q{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert swarm.busy_count() == 4  # never 5+, never a crash
    assert results.count(0) == 4  # exactly four succeeded
    assert results.count(1) == 4  # four denied (no free slot)


# -- FIFO poke line format (the counterpart of the supervisor parser) -----
def test_done_writes_sentinel_and_fifo_line(swarm):
    swarm.state_dir.mkdir(parents=True, exist_ok=True)
    fifo = swarm.state_dir / "control.fifo"
    os.mkfifo(fifo)
    # Hold the FIFO open O_RDWR (as the supervisor does) so the poke lands.
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    swarm.claim("P1")
    try:
        swarm.cli("done", "P1", "ok", "some note", timeout=10)
        # sentinel written atomically
        assert (swarm.state_dir / "done" / "P1.ok").is_file()
        line = os.read(fd, 4096).decode()
        assert line == "done P1 ok\n"
    finally:
        os.close(fd)


# -- finish surfaces the accepted lost-injection race (no backstop) --------
def test_finish_is_idempotent_and_surfaces_leftover(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    from swarm_orchestrator.config import load
    from swarm_orchestrator.supervisor import Supervisor

    cfg = load(project_dir=str(tmp_path))
    sup = Supervisor(cfg)
    try:
        sup._finish(2, ["P1", "P7"])  # a lost nudge left P1,P7 ready-but-unlaunched
        sent = (tmp_path / "tg.log").read_text()
        tg = sent.splitlines()
        assert sent.count("swarm finished") == 1
        assert "2 phase(s) done" in tg[0]
        assert "unlaunched" in tg[0] and "P1" in tg[0] and "P7" in tg[0]
        assert "usage" not in sent  # usage reaches the phone only when asked
        # rule: finish fires exactly once -- a second call is a no-op.
        sup._finish(2, ["P1", "P7"])
        assert (tmp_path / "tg.log").read_text() == sent
    finally:
        sup.log.close()


# -- reboot resizes slots but keeps progress ------------------------------
def test_init_state_preserves_done_and_rebuilds_slots(monkeypatch, tmp_path):
    """`swarm up` re-derives the slot list from config (the only way to resize a
    swarm) but must NOT wipe the completed-phase record -- otherwise a down/up to
    change the worker count would silently re-run everything already done."""
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    from swarm_orchestrator import state as state_mod
    from swarm_orchestrator.config import load

    cfg4 = load(project_dir=str(tmp_path))  # default max_workers = 4
    state_mod.init_state(cfg4)
    with state_mod.transaction(cfg4) as st:
        st.done = {"P0": "ok", "P1": "skip", "P2": "fail"}
        st.finished = True

    cfg1 = load(project_dir=str(tmp_path))
    cfg1.max_workers = 1  # simulate a 4 -> 1 config edit before the next `up`
    rebuilt = state_mod.init_state(cfg1)
    assert len(rebuilt.slots) == 1  # slot count follows config
    assert rebuilt.done == {"P0": "ok", "P1": "skip", "P2": "fail"}  # progress kept
    assert not rebuilt.finished and not rebuilt.paused  # boot flags reset fresh
