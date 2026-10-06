"""``[lanes].prepare``: a worktree is made ready before its landing check
(real git, no tmux, no claude).

What a lane check reads is not all in git: the dependencies installed beside a
checkout are not. A worker installs them in the worktree it builds in; a phase
that never built in a repo leaves none there, and a catch-up merge that moves a
pin leaves the old ones. The check was then red in a second for a reason no
merge can settle, the queue was held, and a resolver was opened only to run the
install. These tests pin the replacement: the swarm runs the project's command
in the worktree before the check, through the build gate, only when the
project's quick test says there is something to do; a command that fails is
reported in its own words, holds nothing and opens no resolver.

The workspace is ``test_landing``'s. The ``pricing`` repo pins what must be
installed beside it (``pin.txt``) in a directory git ignores (``deps/``), and
its check is red without it.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from swarm_orchestrator import buildlog, gitq, landing, repocmd
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import supervisor as sup_mod
from swarm_orchestrator.config import load
from test_landing import Ws, _drop_landing_flocks, _git, _out  # noqa: F401 - the fixture

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

WHY = ["the pinned package is not built here", "nothing was installed"]
FAILED = f"unprepared reading the manifest {WHY[0]} {WHY[1]}"


class Prep(Ws):
    """``test_landing``'s workspace, with dependencies the check needs, the
    command that installs them and the quick test that says whether to."""

    def __init__(self, monkeypatch, tmp_path: Path, test: bool = True,
                 timeout_s: int = 20) -> None:
        ctl = tmp_path / "ctl"
        asked = '[lanes.prepare_if]\npricing = "! cmp -s pin.txt deps/pin.txt"\n' if test else ""
        super().__init__(monkeypatch, tmp_path, extra=(
            f'[lanes.prepare]\npricing = "sh {ctl}/prepare.sh"\n{asked}'
            '[build]\nheavy = ["sh */prepare.sh"]\n'))  # a real one installs: it queues
        toml = self.project / ".swarm.toml"
        toml.write_text(toml.read_text().replace(
            "[lanes]\n", f"[lanes]\nprepare_timeout_s = {timeout_s}\n"))
        self.cfg = load(project_dir=str(self.project))
        self.prepare = f"sh {ctl}/prepare.sh"
        self.prepared = ctl / "prepared"
        (ctl / "prepare.sh").write_text(
            "#!/bin/sh\n"
            f"echo \"$PWD\" >> {self.prepared}\n"
            f"if [ -e {ctl}/sleep ]; then sleep \"$(cat {ctl}/sleep)\"; fi\n"
            f"if [ -e {ctl}/fail ]; then\n"
            "  echo 'reading the manifest'\n"
            f"  echo '{WHY[0]}'\n"
            f"  echo '{WHY[1]}' >&2\n"
            "  exit 1\n"
            "fi\n"
            f"if [ -e {ctl}/scribble ]; then echo changed >> other.txt; fi\n"
            "mkdir -p deps && cp pin.txt deps/pin.txt\n"
        )
        # The check a repo's gate is: red at once when the install is not the pin.
        (ctl / "check.sh").write_text(
            "#!/bin/sh\n"
            "now=$(date +%s.%N)\n"
            "code=0\n"
            "if ! cmp -s pin.txt deps/pin.txt; then\n"
            "  echo 'no dependencies for this pin: install them first'\n"
            "  code=1\n"
            "fi\n"
            f"echo \"$PWD $(git rev-parse 'HEAD^{{tree}}') $now $now $code\" >> {self.record}\n"
            "exit $code\n"
        )
        repo = self.repos["pricing"]
        (repo / "pin.txt").write_text("1\n")
        (repo / ".gitignore").write_text("/deps/\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "pin 1")

    def install(self, phase: str, pin: str = "1\n") -> None:
        """What a worker that built in the repo left beside its worktree."""
        deps = self.wt(phase, "pricing") / "deps"
        deps.mkdir()
        (deps / "pin.txt").write_text(pin)

    def sibling_landed(self, files: dict[str, str]) -> None:
        self.phase("S")
        self.phase("P")
        self.commit("S", "pricing", files)
        self.commit("P", "pricing", {"code.txt": "P\n"})
        self.install("S", files.get("pin.txt", "1\n"))
        self.land("S")

    def prepares(self) -> list[Path]:
        if not self.prepared.is_file():
            return []
        return [Path(line).resolve() for line in self.prepared.read_text().splitlines()]

    def gate(self) -> list[dict]:
        """The build gate's events for the prepare command."""
        path = buildlog.events_path(self.cfg)
        if not path.exists():
            return []
        rows = [json.loads(ln) for ln in path.read_text().splitlines()]
        return [e for e in rows if e["argv"] == self.prepare]

    def check_text(self, phase: str) -> str:
        return landing.check_log(self.cfg, phase, self.repos["pricing"]).read_text()

    def told(self) -> str:
        sink = Path(os.environ["SWARM_TG_SINK"])
        return sink.read_text() if sink.exists() else ""

    def said(self) -> list[tuple[str, str]]:
        """(class, text) of everything the swarm had to say about a landing's
        preparation: recorded, and never sent."""
        path = self.cfg.state_dir / "notifications.jsonl"
        rows = [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []
        return [(r["class"], r["text"]) for r in rows if r["kind"] == "lane-unprepared"]

    def supervisor(self, monkeypatch, *queue: str):
        """A supervisor with ``queue`` to land, and every resolver it opens."""
        opened: list[str] = []
        monkeypatch.setattr(sup_mod.resolver_mod, "spawn",
                            lambda cfg, phase, *a, **k: opened.append(phase))
        with state_mod.transaction(self.cfg) as st:
            st.integ_queue = list(queue)
        return sup_mod.Supervisor(self.cfg), opened


@pytest.fixture
def ws(monkeypatch, tmp_path):
    w = Prep(monkeypatch, tmp_path)
    yield w
    w.close()


# -- the three cases the red checks came down to ----------------------------
def test_a_worktree_with_no_dependencies_is_prepared_and_lands_with_no_resolver(
        ws, monkeypatch):
    ws.sibling_landed({"other.txt": "S\n"})
    wt = ws.wt("P", "pricing")
    assert not (wt / "deps").exists()  # P's worker never installed here
    sup, opened = ws.supervisor(monkeypatch, "P")
    try:
        sup._pump_integrations()
        assert ws.wait_result("P", "pricing") == "ok"
        assert _out(wt, "status", "--porcelain") == ""  # the worktree is as git has it
        assert (wt / "deps" / "pin.txt").read_text() == "1\n"
        sup._handle("lane-checked P pricing ok")
        st = state_mod.read(ws.cfg)
    finally:
        sup.log.close()
    assert "P" in st.done and st.integ_queue == [] and st.integ_blocked is None
    assert opened == [] and not st.landing
    assert ws.prepares() == [wt.resolve()]
    assert [r["code"] for r in ws.runs()] == [0]  # one check, and it was green
    assert (ws.repos["pricing"] / "code.txt").read_text() == "P\n"
    # It went through the build gate like any build, in the phase's worktree.
    assert [e["event"] for e in ws.gate()] == ["queued", "start", "end"]
    assert {e["cwd"] for e in ws.gate()} == {str(wt)} and {e["phase"] for e in ws.gate()} == {"P"}
    text = ws.cfg.supervisor_log.read_text()
    assert "LANE-UNPREPARED" not in text and "INTEGRATE-BLOCKED" not in text
    assert ws.told() == ""


def test_a_catch_up_that_moves_the_pin_prepares_again_though_dependencies_exist(ws):
    ws.phase("S")
    ws.phase("P")
    ws.commit("S", "pricing", {"pin.txt": "2\n"})
    ws.commit("P", "pricing", {"code.txt": "P\n"})
    ws.install("S", "2\n")
    ws.install("P")  # what P's worker installed: the pin it branched from
    ws.land("S")
    wt = ws.wt("P", "pricing")

    assert ws.integrate("P") == gitq.LANE_CHECKING
    assert ws.wait_result("P", "pricing") == "ok"
    assert (wt / "pin.txt").read_text() == "2\n"  # the catch-up brought the new pin
    assert (wt / "deps" / "pin.txt").read_text() == "2\n"
    assert _out(wt, "status", "--porcelain") == ""
    ws.land("P")
    assert ws.prepares() == [wt.resolve()] and [r["code"] for r in ws.runs()] == [0]


def test_a_worktree_whose_dependencies_are_current_runs_nothing(ws):
    ws.sibling_landed({"other.txt": "S\n"})
    ws.install("P")
    assert ws.integrate("P") == gitq.LANE_CHECKING
    assert ws.wait_result("P", "pricing") == "ok"
    ws.land("P")
    assert ws.prepares() == [] and ws.gate() == []  # it never joined the build queue
    assert "prepare" not in ws.check_text("P")
    assert [r["code"] for r in ws.runs()] == [0]


def test_a_failing_prepare_is_reported_as_one_holds_nothing_and_opens_no_resolver(
        ws, monkeypatch):
    ws.sibling_landed({"other.txt": "S\n"})
    ws.phase("C")
    ws.commit("C", "pricing", {"c.txt": "C\n"})
    wt, repo = ws.wt("P", "pricing"), ws.repos["pricing"]
    (ws.ctl / "fail").write_text("")
    sup, opened = ws.supervisor(monkeypatch, "P", "C")
    try:
        sup._pump_integrations()  # P's check starts; C waits behind P's hold on the repo
        assert ws.wait_result("P", "pricing") == FAILED
        assert "LANE-WAIT C pricing holder=P" in ws.cfg.supervisor_log.read_text()
        sup._handle("lane-checked P pricing unprepared")
        st = state_mod.read(ws.cfg)
        assert st.integ_blocked is None and opened == []
        assert st.landing["P"][str(repo)]["stage"] == landing.UNPREPARED
        assert landing.blocked(ws.cfg, "P") is None
        assert ws.runs() == []  # no check ran on a tree that was not ready
        # The command's own last lines: in the log, and held for the Overseer's
        # summary. Other work keeps merging, so nobody is asked.
        reason = FAILED.removeprefix("unprepared ")
        assert f"LANE-UNPREPARED P pricing {reason}" in ws.cfg.supervisor_log.read_text()
        [(cls, text)] = ws.said()
        assert cls == "folded" and f"the landing check failed — {reason}." in text
        assert text.startswith("P cannot land in pricing yet") and ws.told() == ""
        assert "# result: unprepared" in ws.check_text("P")

        # P holds nothing meanwhile: the other phase in the same repo has landed.
        assert "C" in st.done and st.integ_queue == ["P"] and st.integ_blocked is None
        assert (repo / "c.txt").read_text() == "C\n"
        # Not tried again at once, however often the queue looks.
        sup._pump_integrations()
        assert len(ws.prepares()) == 1

        # Tried again once the wait is over. Still failing: said once, not twice.
        monkeypatch.setattr(landing, "RETRY_S", 0.0)
        landing._result_path(ws.cfg, "P", repo).unlink()
        sup._pump_integrations()
        assert ws.wait_result("P", "pricing") == FAILED
        sup._handle("lane-checked P pricing unprepared")
        assert len(ws.prepares()) == 2 and ws.told() == ""
        assert [cls for cls, _ in ws.said()] == ["folded", "logged"]  # the summary hears once

        # What it needed is there now: the next try prepares, checks and lands.
        (ws.ctl / "fail").unlink()
        landing._result_path(ws.cfg, "P", repo).unlink()
        sup._pump_integrations()
        assert ws.wait_result("P", "pricing") == "ok"
        sup._handle("lane-checked P pricing ok")
        st = state_mod.read(ws.cfg)
    finally:
        sup.log.close()
    assert "P" in st.done and st.integ_queue == [] and not st.landing
    assert opened == [] and "INTEGRATE-BLOCKED" not in ws.cfg.supervisor_log.read_text()
    [run] = ws.runs()
    # What was tested had the work that landed meanwhile, and it is what landed.
    assert run["cwd"] == wt.resolve() and run["code"] == 0
    assert _out(repo, "rev-parse", "master^{tree}") == run["tree"]
    assert {"code.txt", "c.txt", "other.txt"} <= set(
        _out(repo, "ls-tree", "--name-only", "master").split())


# -- the clean tree, the timeout, the quick test ------------------------------
def test_a_prepare_that_leaves_the_tree_changed_has_failed(ws):
    ws.sibling_landed({"other.txt": "S\n"})
    (ws.ctl / "scribble").write_text("")
    assert ws.integrate("P") == gitq.LANE_CHECKING
    assert ws.wait_result("P", "pricing") == (
        "unprepared the command left uncommitted changes: other.txt")
    assert ws.integrate("P") == gitq.LANE_UNPREPARED
    assert ws.integrate("P") == gitq.LANE_UNPREPARED  # until it is tried again
    assert ws.runs() == [] and len(ws.prepares()) == 1


def test_a_prepare_that_runs_too_long_is_stopped_and_no_check_runs(monkeypatch, tmp_path):
    ws = Prep(monkeypatch, tmp_path, timeout_s=1)
    try:
        ws.sibling_landed({"other.txt": "S\n"})
        (ws.ctl / "sleep").write_text("30")
        assert ws.integrate("P") == gitq.LANE_CHECKING
        assert ws.wait_result("P", "pricing", deadline_s=20) == "unprepared timed out after 1s"
        assert ws.integrate("P") == gitq.LANE_UNPREPARED
        assert ws.runs() == []
    finally:
        ws.close()


def test_without_the_quick_test_the_command_runs_before_every_check(monkeypatch, tmp_path):
    ws = Prep(monkeypatch, tmp_path, test=False)
    try:
        ws.sibling_landed({"other.txt": "S\n"})
        ws.install("P")
        assert ws.integrate("P") == gitq.LANE_CHECKING
        assert ws.wait_result("P", "pricing") == "ok"
        ws.land("P")
        assert len(ws.prepares()) == 1
    finally:
        ws.close()


def test_a_quick_test_that_cannot_run_is_a_no_and_the_log_says_so(ws, monkeypatch):
    ws.sibling_landed({"other.txt": "S\n"})
    monkeypatch.setattr(repocmd, "IF_TIMEOUT_S", 0.2)
    ws.cfg.lanes_prepare_if = {"pricing": "sleep 5"}
    assert landing.run_check(ws.cfg, "P", "pricing") == 1  # in this process: the check ran
    assert ws.prepares() == [] and [r["code"] for r in ws.runs()] == [1]
    assert "# prepare_if `sleep 5` could not be asked: " in ws.check_text("P")
    assert landing._result_path(ws.cfg, "P", ws.repos["pricing"]).read_text() == "fail\n"


def test_a_phase_with_one_repo_unprepared_and_one_red_goes_to_the_resolver(ws):
    ws.phase("S")
    ws.phase("P")
    ws.commit("S", "pricing", {"other.txt": "S\n"})
    ws.commit("S", "webhooks", {"other.txt": "S\n"})
    ws.commit("P", "pricing", {"code.txt": "P\n"})
    ws.commit("P", "webhooks", {"code.txt": "P\n"})
    ws.install("S")
    ws.land("S")
    (ws.ctl / "fail").write_text("")  # pricing cannot be prepared; webhooks names no command
    assert ws.integrate("P") == gitq.LANE_CHECKING
    assert ws.wait_result("P", "pricing") == FAILED
    assert ws.wait_result("P", "webhooks") == "fail"
    assert ws.integrate("P") == gitq.LANE_RED
    assert landing.blocked(ws.cfg, "P")[0] == ws.repos["webhooks"]
