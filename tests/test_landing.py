"""Landing under lanes re-tests a phase against what landed beside it.

Real git, no tmux, no claude. The umbrella + component workspace of
``test_multirepo``, lanes on in ``.swarm.toml``, and a fake ``[lanes] check`` that
records every run (cwd, the tree it tested, start/end) and exits as a control
file says. The check really runs detached through ``swarm _lane-check``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarm_orchestrator import buildsem
from swarm_orchestrator import gitq
from swarm_orchestrator import landing
from swarm_orchestrator import ledgerw
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

SWARM_BIN = f"{sys.executable} -m swarm_orchestrator"


# -- workspace helpers (as test_multirepo) ----------------------------------
def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "swarm@test")
    _git(repo, "config", "user.name", "swarm")
    _git(repo, "config", "commit.gpgsign", "false")


def _init_repo(path: Path, seed: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "master", str(path)], check=True, capture_output=True)
    _identity(path)
    for rel, content in seed.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


def _with_origin(repo: Path, origin: Path) -> None:
    subprocess.run(["git", "init", "--bare", "-b", "master", str(origin)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "master")


def _make_workspace(tmp_path: Path, siblings=("pricing", "webhooks")):
    project = tmp_path / "myproject"
    _init_repo(
        project,
        {
            "docs/PHASE-LEDGER.md": "".join(f"{s}-P1\n" for s in siblings),
            ".gitignore": "".join(f"/{s}/\n" for s in siblings) + "/.swarm.toml\n",
            "notes.txt": "base\n",
        },
    )
    repos = {}
    for s in siblings:
        r = _init_repo(project / s, {"code.txt": "base\n", "other.txt": "base\n"})
        _with_origin(r, tmp_path / f"{s}.git")
        repos[s] = r
    _with_origin(project, tmp_path / "myproject.git")
    return project, repos


class Ws:
    """One lanes-on workspace: config, log, the fake check's control files."""

    def __init__(self, monkeypatch, tmp_path: Path, timeout_s: int = 60,
                 extra: str = "") -> None:
        self.project, self.repos = _make_workspace(tmp_path)
        self.ctl = tmp_path / "ctl"
        self.ctl.mkdir()
        self.record = self.ctl / "record"
        check = self.ctl / "check.sh"
        check.write_text(
            "#!/bin/sh\n"
            "start=$(date +%s.%N)\n"
            f"while [ -e {self.ctl}/hold ]; do sleep 0.05; done\n"
            f"if [ -e {self.ctl}/sleep ]; then sleep \"$(cat {self.ctl}/sleep)\"; fi\n"
            f"code=$(cat {self.ctl}/exit 2>/dev/null || echo 0)\n"
            "tree=$(git rev-parse 'HEAD^{tree}')\n"
            f"echo \"$PWD $tree $start $(date +%s.%N) $code\" >> {self.record}\n"
            "exit $code\n"
        )
        (self.project / ".swarm.toml").write_text(
            "[lanes]\n"
            "enabled = true\n"
            'commons = ["./docs/PHASE-LEDGER.md", "*/CHANGELOG.md"]\n'
            f"check_timeout_s = {timeout_s}\n"
            "[lanes.check]\n"
            f'"*" = "sh {check}"\n'
            f"{extra}"
        )
        monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
        monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
        monkeypatch.setenv("SWARM_GIT_MAIN", "master")
        monkeypatch.setenv("SWARM_DRIVER", "bare")
        monkeypatch.setenv("SWARM_MASTER_CMD", "true")
        monkeypatch.setenv("SWARM_WORKER_CMD", "true")
        monkeypatch.setenv("SWARM_SLUG", "landtest")
        monkeypatch.setenv("SWARM_BIN", SWARM_BIN)
        for var in ("SWARM_GIT_REPOS", "SWARM_READY_MARKER", "SWARM_LANES"):
            monkeypatch.delenv(var, raising=False)
        self.cfg = load(project_dir=str(self.project))
        assert self.cfg.lanes_enabled
        state_mod.init_state(self.cfg)
        self.log = Log(self.cfg.supervisor_log)

    # -- phases -------------------------------------------------------------
    def phase(self, name: str) -> Path:
        return gitq.worktree_add(self.cfg, name, self.log)

    def commit(self, phase: str, lane: str, files: dict[str, str]) -> None:
        wt = self.wt(phase, lane)
        for rel, text in files.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(text)
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", f"{phase} work")

    def wt(self, phase: str, lane: str) -> Path:
        return self.cfg.wt_dir / phase if lane == "." else self.cfg.wt_dir / phase / lane

    def repo(self, lane: str) -> Path:
        return self.project if lane == "." else self.repos[lane]

    def integrate(self, phase: str) -> str:
        return gitq.integrate(self.cfg, phase, self.log)

    def land(self, phase: str) -> None:
        assert self.integrate(phase) == gitq.MERGED

    # -- the fake check -----------------------------------------------------
    def set_exit(self, code: int) -> None:
        (self.ctl / "exit").write_text(f"{code}\n")

    def runs(self) -> list[dict]:
        if not self.record.is_file():
            return []
        out = []
        for line in self.record.read_text().splitlines():
            cwd, tree, start, end, code = line.split()
            out.append({"cwd": Path(cwd).resolve(), "tree": tree, "start": float(start),
                        "end": float(end), "code": int(code)})
        return out

    def wait_result(self, phase: str, lane: str, deadline_s: float = 30.0) -> str:
        path = landing._result_path(self.cfg, phase, self.repo(lane))
        end = time.monotonic() + deadline_s
        while time.monotonic() < end:
            if path.is_file() and path.read_text().strip():
                return path.read_text().strip()
            time.sleep(0.05)
        log = landing.check_log(self.cfg, phase, self.repo(lane))
        raise AssertionError(
            f"no check result for {phase}/{lane}; log: "
            f"{log.read_text() if log.is_file() else '(none)'}"
        )

    def main_head(self, lane: str) -> str:
        return _out(self.repo(lane), "rev-parse", "master")

    def canonical_clean(self, lane: str) -> None:
        repo = self.repo(lane)
        assert _out(repo, "rev-parse", "--abbrev-ref", "HEAD") == "master"
        assert not gitq._merge_in_progress(repo)
        assert _out(repo, "status", "--porcelain", "--untracked-files=no") == ""

    def close(self) -> None:
        self.log.close()


@pytest.fixture
def ws(monkeypatch, tmp_path):
    w = Ws(monkeypatch, tmp_path)
    yield w
    w.close()


@pytest.fixture(autouse=True)
def _drop_landing_flocks():
    """The landing flocks live in a module-level table: never let one test's
    hold (keyed by repo slug) leak into the next."""
    yield
    for slug in list(landing._FDS):
        _phase, fd = landing._FDS.pop(slug)
        try:
            os.close(fd)
        except OSError:
            pass


# -- 1. main has not moved ----------------------------------------------------
def test_main_unmoved_lands_without_a_check(ws):
    ws.phase("P")
    ws.commit("P", "pricing", {"code.txt": "P\n"})
    ws.land("P")
    assert ws.runs() == []
    assert not state_mod.read(ws.cfg).landing
    _git(ws.repos["pricing"], "checkout", "master")
    assert (ws.repos["pricing"] / "code.txt").read_text() == "P\n"
    assert not (ws.cfg.wt_dir / "P").exists()


# -- 2. main moved only by commons ----------------------------------------------
def test_main_moved_only_by_commons_lands_without_a_check(ws):
    ws.phase("P")
    ws.commit("P", "pricing", {"code.txt": "P\n"})
    ws.commit("P", ".", {"notes.txt": "P\n"})
    # The umbrella main gains a ledger tick, the component a CHANGELOG line:
    # both commons, so neither is a sibling's work to re-test against.
    (ws.project / "docs" / "PHASE-LEDGER.md").write_text("pricing-P1 DONE\nwebhooks-P1\n")
    _git(ws.project, "commit", "-am", "ledger tick")
    (ws.repos["pricing"] / "CHANGELOG.md").write_text("- a line\n")
    _git(ws.repos["pricing"], "add", "-A")
    _git(ws.repos["pricing"], "commit", "-m", "changelog")

    ws.land("P")
    assert ws.runs() == []
    assert (ws.project / "notes.txt").read_text() == "P\n"
    assert (ws.repos["pricing"] / "code.txt").read_text() == "P\n"


# -- 3. a sibling landed on another file of the same repo -----------------------
def test_sibling_landing_catches_up_checks_in_the_worktree_and_lands_the_tested_tree(ws):
    ws.phase("S")
    ws.phase("P")
    ws.commit("S", "pricing", {"other.txt": "S\n"})
    ws.commit("P", "pricing", {"code.txt": "P\n"})
    ws.land("S")
    main_after_s = ws.main_head("pricing")

    assert ws.integrate("P") == gitq.LANE_CHECKING
    entry = state_mod.read(ws.cfg).landing["P"][str(ws.repos["pricing"])]
    assert entry["stage"] == landing.CHECKING and entry["pid"]
    # Main moved not a jot while the check runs; the catch-up is in the worktree.
    assert ws.main_head("pricing") == main_after_s
    ws.canonical_clean("pricing")
    assert ws.wait_result("P", "pricing") == "ok"
    tested_tip = _out(ws.wt("P", "pricing"), "rev-parse", "HEAD")

    ws.land("P")
    runs = ws.runs()
    assert len(runs) == 1
    assert runs[0]["cwd"] == ws.wt("P", "pricing").resolve()
    assert runs[0]["cwd"] != ws.repos["pricing"].resolve()
    repo = ws.repos["pricing"]
    # The tree that landed is the tree that was tested.
    assert _out(repo, "rev-parse", "master^{tree}") == runs[0]["tree"]
    assert _out(repo, "rev-parse", "master^2") == tested_tip
    assert (repo / "code.txt").read_text() == "P\n"
    assert (repo / "other.txt").read_text() == "S\n"
    assert not state_mod.read(ws.cfg).landing
    assert "LANE-CATCH-UP P pricing" in ws.cfg.supervisor_log.read_text()


# -- 4. a red check: a resolver on the worktree, then re-checked and landed ----
def test_red_check_holds_on_the_worktree_then_rechecks_and_lands(ws):
    ws.phase("S")
    ws.phase("P")
    ws.commit("S", "pricing", {"other.txt": "S\n"})
    ws.commit("P", "pricing", {"code.txt": "P\n"})
    ws.land("S")
    main_after_s = ws.main_head("pricing")
    wt = ws.wt("P", "pricing")
    ws.set_exit(1)

    with state_mod.transaction(ws.cfg) as st:
        st.integ_queue = ["P"]
    sup = Supervisor(ws.cfg)
    try:
        sup._pump_integrations()
        st = state_mod.read(ws.cfg)
        assert st.integ_queue == ["P"] and st.integ_blocked is None  # checking
        assert ws.wait_result("P", "pricing") == "fail"
        sup._handle("lane-checked P pricing fail")

        st = state_mod.read(ws.cfg)
        assert st.integ_blocked == "P"
        assert st.integ_blocked_kind == gitq.LANE_RED
        assert st.integ_blocked_repo == str(wt)
        assert landing.blocked(ws.cfg, "P") == (ws.repos["pricing"], wt, landing.RED)
        target, line = landing.resolver_brief(ws.cfg, "P")
        assert target == wt
        assert str(wt) in line
        assert str(landing.check_log(ws.cfg, "P", ws.repos["pricing"])) in line
        assert "S" in line.split("since this branch was cut:")[1]  # names the sibling
        assert ws.main_head("pricing") == main_after_s  # canonical never touched
        ws.canonical_clean("pricing")

        # The resolver fixes the combined tree in the worktree and commits there.
        ws.set_exit(0)
        ws.commit("P", "pricing", {"code.txt": "P fixed\n"})
        sup._on_resolved("P")
        st = state_mod.read(ws.cfg)
        assert st.integ_blocked is None and st.integ_queue == ["P"]  # re-checking
        assert ws.wait_result("P", "pricing") == "ok"
        sup._handle("lane-checked P pricing ok")

        st = state_mod.read(ws.cfg)
        assert st.integ_queue == [] and st.integ_blocked is None
        assert "P" in st.done
        assert not st.landing
    finally:
        sup.log.close()
    runs = ws.runs()
    assert [r["code"] for r in runs] == [1, 0]  # checked again after `resolved`
    assert all(r["cwd"] == wt.resolve() for r in runs)
    repo = ws.repos["pricing"]
    assert _out(repo, "rev-parse", "master^{tree}") == runs[1]["tree"]
    assert (repo / "code.txt").read_text() == "P fixed\n"
    assert (repo / "other.txt").read_text() == "S\n"


def test_red_check_via_retest_rechecks_even_when_main_is_unchanged(ws):
    ws.phase("S")
    ws.phase("P")
    ws.commit("S", "pricing", {"other.txt": "S\n"})
    ws.commit("P", "pricing", {"code.txt": "P\n"})
    ws.land("S")
    ws.set_exit(1)
    assert ws.integrate("P") == gitq.LANE_CHECKING
    assert ws.wait_result("P", "pricing") == "fail"
    assert ws.integrate("P") == gitq.LANE_RED
    assert ws.integrate("P") == gitq.LANE_RED  # stays red until `resolved`
    assert len(ws.runs()) == 1

    ws.set_exit(0)
    ws.commit("P", "pricing", {"code.txt": "P fixed\n"})
    landing.retest(ws.cfg, "P")
    assert ws.integrate("P") == gitq.LANE_CHECKING
    assert ws.wait_result("P", "pricing") == "ok"
    ws.land("P")
    assert [r["code"] for r in ws.runs()] == [1, 0]


# -- 5. a timeout is red ----------------------------------------------------------
def test_check_timeout_counts_as_red(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path, timeout_s=1)
    try:
        ws.phase("S")
        ws.phase("P")
        ws.commit("S", "pricing", {"other.txt": "S\n"})
        ws.commit("P", "pricing", {"code.txt": "P\n"})
        ws.land("S")
        main_after_s = ws.main_head("pricing")
        (ws.ctl / "sleep").write_text("30\n")
        began = time.monotonic()
        assert ws.integrate("P") == gitq.LANE_CHECKING
        assert ws.wait_result("P", "pricing", deadline_s=20) == "fail"
        assert time.monotonic() - began < 15  # killed at the timeout, not after 30 s
        assert ws.integrate("P") == gitq.LANE_RED
        assert landing.blocked(ws.cfg, "P")[2] == landing.RED
        assert "timed out" in landing.check_log(ws.cfg, "P", ws.repos["pricing"]).read_text()
        assert ws.runs() == []  # the check never finished
        assert ws.main_head("pricing") == main_after_s
    finally:
        ws.close()


# -- 6. a text conflict at the catch-up --------------------------------------------
def test_catch_up_conflict_leaves_the_worktree_mid_merge_and_the_checkout_clean(ws):
    ws.phase("S")
    ws.phase("P")
    ws.commit("S", "pricing", {"code.txt": "S\n"})
    ws.commit("P", "pricing", {"code.txt": "P\n"})
    ws.land("S")
    main_after_s = ws.main_head("pricing")
    wt = ws.wt("P", "pricing")

    assert ws.integrate("P") == gitq.LANE_CONFLICT
    assert gitq._merge_in_progress(wt)
    assert "<<<<<<<" in (wt / "code.txt").read_text()
    ws.canonical_clean("pricing")
    assert ws.main_head("pricing") == main_after_s
    assert ws.runs() == []  # no check on a conflicted tree
    assert landing.blocked(ws.cfg, "P") == (ws.repos["pricing"], wt, landing.CONFLICT)
    target, line = landing.resolver_brief(ws.cfg, "P")
    assert target == wt
    assert "catch-up conflict" in line
    assert not landing.resolve_ready(wt)

    # The supervisor holds on the worktree, never the canonical checkout.
    with state_mod.transaction(ws.cfg) as st:
        st.integ_queue = ["P"]
    sup = Supervisor(ws.cfg)
    try:
        sup._pump_integrations()
        st = state_mod.read(ws.cfg)
        assert st.integ_blocked == "P"
        assert st.integ_blocked_kind == gitq.LANE_CONFLICT
        assert st.integ_blocked_repo == str(wt)
        # Resolving: an early `resolved` stays blocked; a committed merge re-checks.
        sup._on_resolved("P")
        assert state_mod.read(ws.cfg).integ_blocked == "P"
        (wt / "code.txt").write_text("S and P\n")
        _git(wt, "add", "-A")
        _git(wt, "commit", "--no-edit")
        sup._on_resolved("P")
        assert ws.wait_result("P", "pricing") == "ok"
        sup._handle("lane-checked P pricing ok")
        st = state_mod.read(ws.cfg)
        assert st.integ_queue == [] and st.integ_blocked is None
    finally:
        sup.log.close()
    assert (ws.repos["pricing"] / "code.txt").read_text() == "S and P\n"
    assert len(ws.runs()) == 1


# -- 7. the landing lock ----------------------------------------------------------
def test_landing_lock_serialises_one_repo_and_lets_others_land(ws):
    for p in ("X", "A", "B", "C"):
        ws.phase(p)
    ws.commit("X", "pricing", {"other.txt": "X\n"})
    ws.commit("A", "pricing", {"a.txt": "A\n"})
    ws.commit("B", "webhooks", {"b.txt": "B\n"})
    ws.commit("C", "pricing", {"c.txt": "C\n"})
    ws.land("X")  # pricing's main moves: A and C must re-test

    (ws.ctl / "hold").write_text("")
    assert ws.integrate("A") == gitq.LANE_CHECKING
    ws.land("B")  # webhooks is free: B lands while A's check runs
    assert (ws.repos["webhooks"] / "b.txt").read_text() == "B\n"
    pricing_head = ws.main_head("pricing")
    assert ws.integrate("C") == gitq.LANE_WAITING
    assert ws.integrate("C") == gitq.LANE_WAITING
    st = state_mod.read(ws.cfg)
    assert "C" not in st.landing  # waiting holds nothing, starts no check
    assert not landing._result_path(ws.cfg, "C", ws.repos["pricing"]).exists()
    assert ws.main_head("pricing") == pricing_head

    (ws.ctl / "hold").unlink()
    assert ws.wait_result("A", "pricing") == "ok"
    ws.land("A")
    assert ws.integrate("C") == gitq.LANE_CHECKING  # A's landing moved main again
    assert ws.wait_result("C", "pricing") == "ok"
    ws.land("C")

    runs = ws.runs()
    assert [r["cwd"] for r in runs] == [ws.wt("A", "pricing").resolve(),
                                        ws.wt("C", "pricing").resolve()]
    assert runs[0]["end"] <= runs[1]["start"]  # never two checks in one repo at once
    for f, text in (("other.txt", "X\n"), ("a.txt", "A\n"), ("c.txt", "C\n")):
        assert (ws.repos["pricing"] / f).read_text() == text
    assert not state_mod.read(ws.cfg).landing


# -- 8. outside the lane (D6) ------------------------------------------------------
def test_undeclared_files_are_noted_and_never_hold_the_merge(ws):
    ws.phase("P")
    with state_mod.transaction(ws.cfg) as st:
        st.lanes["P"] = ["pricing/code.txt"]
    ws.commit("P", "pricing", {"code.txt": "P\n", "extra/new.txt": "new\n"})
    ws.commit("P", ".", {"notes.txt": "P\n", "docs/PHASE-LEDGER.md": "tick\n"})
    since = time.time() - 1

    ws.land("P")
    outside = ["pricing/extra/new.txt", "./notes.txt"]
    ops = ledgerw.pending(ws.cfg)["P"]["ops"]
    notes = [op for op in ops if op.get("outcome") == "note"]
    assert len(notes) == 1
    assert notes[0]["phase"] == "P" and notes[0]["kind"] == "record"
    assert notes[0]["note"] == "lane: touched outside its declaration: " + " ".join(outside)
    flagged = landing.undeclared_since(ws.cfg, since)
    assert [(r["phase"], r["paths"]) for r in flagged] == [("P", outside)]
    assert (ws.repos["pricing"] / "extra" / "new.txt").read_text() == "new\n"
    assert (ws.project / "notes.txt").read_text() == "P\n"


# -- 9. the supervisor lands later phases while a check runs ---------------------
def test_supervisor_lands_later_phases_while_a_check_runs(ws):
    for p in ("X", "A", "B"):
        ws.phase(p)
    ws.commit("X", "pricing", {"other.txt": "X\n"})
    ws.commit("A", "pricing", {"a.txt": "A\n"})
    ws.commit("B", "webhooks", {"b.txt": "B\n"})
    ws.land("X")
    (ws.ctl / "hold").write_text("")

    with state_mod.transaction(ws.cfg) as st:
        st.integ_queue = ["A", "B"]
    sup = Supervisor(ws.cfg)
    try:
        sup._pump_integrations()
        st = state_mod.read(ws.cfg)
        assert st.integ_queue == ["A"]
        assert "B" in st.done and "A" not in st.done
        assert (ws.repos["webhooks"] / "b.txt").read_text() == "B\n"
        assert st.landing["A"][str(ws.repos["pricing"])]["stage"] == landing.CHECKING

        (ws.ctl / "hold").unlink()
        assert ws.wait_result("A", "pricing") == "ok"
        sup._handle("lane-checked A pricing ok")
        st = state_mod.read(ws.cfg)
        assert st.integ_queue == [] and "A" in st.done
        assert not st.landing
    finally:
        sup.log.close()
    assert (ws.repos["pricing"] / "a.txt").read_text() == "A\n"


# -- 10. main moves after the check passed ----------------------------------------
def _passed(ws, lane: str = "pricing", sibling: str = "other.txt", mine: str = "code.txt",
            also: dict[str, str] | None = None) -> None:
    """P's check in ``lane`` has passed against sibling S; P has not landed."""
    ws.phase("S")
    ws.phase("P")
    ws.commit("S", lane, {sibling: "S\n"})
    ws.commit("P", lane, {mine: "P\n", **(also or {})})
    ws.land("S")
    assert ws.integrate("P") == gitq.LANE_CHECKING
    assert ws.wait_result("P", lane) == "ok"


def _main_gains(ws, lane: str, files: dict[str, str]) -> str:
    """Commit ``files`` straight onto ``lane``'s main, as the ledger writer or a
    sibling's landing does. Returns the new commit."""
    repo = ws.repo(lane)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main moves")
    return _out(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "lane, sibling, mine, commons",
    [("pricing", "other.txt", "code.txt", "CHANGELOG.md"),
     (".", "s.txt", "notes.txt", "docs/PHASE-LEDGER.md")],
)
def test_a_passed_check_survives_a_commons_only_move_of_main(ws, lane, sibling, mine, commons):
    _passed(ws, lane, sibling, mine)
    tested = ws.runs()[0]["tree"]
    _main_gains(ws, lane, {commons: "a note\n"})
    moved = _main_gains(ws, lane, {commons: "a note\nand another\n"})

    ws.land("P")
    assert len(ws.runs()) == 1  # the green check was not thrown away
    repo = ws.repo(lane)
    # The branch caught up with main before it landed, so that merge could not conflict.
    _git(repo, "merge-base", "--is-ancestor", moved, "master^2")
    # What landed is the tested tree plus what the commons gained, nothing else.
    assert _out(repo, "diff", "--name-only", tested, "master") == commons
    assert (repo / mine).read_text() == "P\n"
    assert (repo / sibling).read_text() == "S\n"
    ws.canonical_clean(lane)
    log = ws.cfg.supervisor_log.read_text()
    assert f"LANE-CHECK-KEPT P {repo.name}" in log
    assert "LANE-MAIN-MOVED" not in log
    assert not state_mod.read(ws.cfg).landing


def test_a_commons_move_while_the_check_runs_does_not_start_a_second_check(ws):
    ws.phase("S")
    ws.phase("P")
    ws.commit("S", "pricing", {"other.txt": "S\n"})
    ws.commit("P", "pricing", {"code.txt": "P\n"})
    ws.land("S")
    (ws.ctl / "hold").write_text("")
    assert ws.integrate("P") == gitq.LANE_CHECKING
    _main_gains(ws, "pricing", {"CHANGELOG.md": "- a line\n"})
    assert ws.integrate("P") == gitq.LANE_CHECKING  # still the first check
    (ws.ctl / "hold").unlink()
    assert ws.wait_result("P", "pricing") == "ok"

    ws.land("P")
    assert len(ws.runs()) == 1
    assert (ws.repos["pricing"] / "CHANGELOG.md").read_text() == "- a line\n"


def test_a_passed_check_is_run_again_when_main_gains_a_file_outside_the_commons(ws):
    _passed(ws)
    # A commons line and a sibling's file together: one file outside is enough.
    _main_gains(ws, "pricing", {"CHANGELOG.md": "- a line\n", "third.txt": "T\n"})

    assert ws.integrate("P") == gitq.LANE_CHECKING
    assert ws.wait_result("P", "pricing") == "ok"
    ws.land("P")
    runs = ws.runs()
    assert len(runs) == 2
    repo = ws.repos["pricing"]
    assert _out(repo, "rev-parse", "master^{tree}") == runs[1]["tree"]
    assert (repo / "third.txt").read_text() == "T\n"
    log = ws.cfg.supervisor_log.read_text()
    assert "LANE-MAIN-MOVED P pricing" in log
    assert "LANE-CHECK-KEPT" not in log


def test_a_commons_conflict_after_the_pass_holds_on_the_worktree_and_is_rechecked(ws):
    _passed(ws, also={"CHANGELOG.md": "- P\n"})
    _main_gains(ws, "pricing", {"CHANGELOG.md": "- someone else\n"})
    main_moved = ws.main_head("pricing")
    wt = ws.wt("P", "pricing")

    assert ws.integrate("P") == gitq.LANE_CONFLICT
    assert gitq._merge_in_progress(wt)
    assert landing.blocked(ws.cfg, "P") == (ws.repos["pricing"], wt, landing.CONFLICT)
    ws.canonical_clean("pricing")
    assert ws.main_head("pricing") == main_moved
    assert len(ws.runs()) == 1

    # A resolver wrote the merge by hand, so the pair is tested again.
    (wt / "CHANGELOG.md").write_text("- someone else\n- P\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "--no-edit")
    landing.retest(ws.cfg, "P")
    assert ws.integrate("P") == gitq.LANE_CHECKING
    assert ws.wait_result("P", "pricing") == "ok"
    ws.land("P")
    assert len(ws.runs()) == 2
    assert (ws.repos["pricing"] / "CHANGELOG.md").read_text() == "- someone else\n- P\n"


# -- 11. the check and the build gate ----------------------------------------------
_ONE_SLOT = "[build]\nmax_concurrent = 1\n"


def _sibling_landed(ws) -> None:
    ws.phase("S")
    ws.phase("P")
    ws.commit("S", "pricing", {"other.txt": "S\n"})
    ws.commit("P", "pricing", {"code.txt": "P\n"})
    ws.land("S")


def test_a_check_waits_for_the_build_slot_like_any_build(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path, extra=_ONE_SLOT)
    try:
        _sibling_landed(ws)
        with buildsem.slot(ws.cfg, "a long build", ws.project, "other"):
            assert ws.integrate("P") == gitq.LANE_CHECKING
            time.sleep(2.0)
            assert ws.runs() == []  # queued behind the build that holds the only slot
        assert ws.wait_result("P", "pricing") == "ok"
        ws.land("P")
        assert len(ws.runs()) == 1
    finally:
        ws.close()


def test_a_check_declared_light_runs_beside_a_build(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path, extra=_ONE_SLOT + 'light = ["sh */check.sh"]\n')
    try:
        _sibling_landed(ws)
        with buildsem.slot(ws.cfg, "a long build", ws.project, "other"):
            assert ws.integrate("P") == gitq.LANE_CHECKING
            assert ws.wait_result("P", "pricing", deadline_s=20) == "ok"
        text = landing.check_log(ws.cfg, "P", ws.repos["pricing"]).read_text()
        assert "not queued" in text
        ws.land("P")
        assert len(ws.runs()) == 1
    finally:
        ws.close()


# -- 12. the log of the run before --------------------------------------------------
def test_a_red_checks_log_is_kept_when_the_check_runs_again(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path, extra=_ONE_SLOT)
    try:
        _sibling_landed(ws)
        check = ws.ctl / "check.sh"
        check.write_text(check.read_text().replace(
            "exit $code", 'echo "the check says $code"\nexit $code'))
        log = landing.check_log(ws.cfg, "P", ws.repos["pricing"])
        kept = landing.kept_log(ws.cfg, "P", ws.repos["pricing"])
        ws.set_exit(1)
        assert ws.integrate("P") == gitq.LANE_CHECKING
        assert ws.wait_result("P", "pricing") == "fail"
        assert ws.integrate("P") == gitq.LANE_RED
        red = log.read_text()
        assert "the check says 1" in red and "# result: fail" in red
        assert not kept.exists()  # the first run had nothing to keep

        ws.set_exit(0)
        landing.retest(ws.cfg, "P")
        with buildsem.slot(ws.cfg, "a long build", ws.project, "other"):
            assert ws.integrate("P") == gitq.LANE_CHECKING
            end = time.monotonic() + 20
            while time.monotonic() < end and not (
                    log.is_file() and str(kept) in log.read_text()):
                time.sleep(0.05)
            # Queued for the slot, the new log is its header: it names the kept one.
            assert str(kept) in log.read_text()
            assert "the check says" not in log.read_text()
            assert kept.read_text() == red
        assert ws.wait_result("P", "pricing") == "ok"
        assert "the check says 0" in log.read_text()
        assert kept.read_text() == red
        ws.land("P")
    finally:
        ws.close()
