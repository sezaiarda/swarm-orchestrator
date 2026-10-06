"""``[git].post_merge``: a repo's own command, run on its main before a push
(real git, no tmux, no claude).

A merge that moves a dependency's pin leaves the main checkout's install where
it was: workers install in their mirrors, and nobody owns the main checkout's.
The repo's pre-push check then refuses every push until someone installs the
pin by hand. These tests pin the replacement: the swarm runs the project's
command between the merge and the push, through the build gate, only when the
project's quick test says there is something to do; a command that fails leaves
the push owed in its own words and never holds the queue.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import machine_toml

from swarm_orchestrator import buildlog, buildsem, buildstatus, gitq, pushowed, repocmd
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

STALE = "the install is 1 but the manifest pins 2: install the pin"
WHY = ["the pinned package is not built here", "nothing was installed"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _init_repo(path: Path, seed: dict[str, str]) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "master", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "swarm@test")
    _git(path, "config", "user.name", "swarm")
    _git(path, "config", "commit.gpgsign", "false")
    for rel, text in seed.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "init")
    return path


def _with_origin(repo: Path, origin: Path) -> Path:
    subprocess.run(["git", "init", "--bare", "-b", "master", str(origin)], check=True, capture_output=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "master")
    return origin


def _gate(repo: Path, hook_runs: Path) -> None:
    """A pre-push hook shaped like a repo's gate: it refuses a checkout whose
    install is not the one its manifest pins, and says which."""
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(
        "#!/bin/sh\n"
        f"echo run >> '{hook_runs}'\n"
        'cd "$(git rev-parse --show-toplevel)"\n'
        "if ! cmp -s pin.txt installed.txt; then\n"
        '  echo "the install is $(cat installed.txt) but the manifest pins $(cat pin.txt):'
        ' install the pin"\n'
        "  exit 1\n"
        "fi\n"
    )
    hook.chmod(0o755)


class Ws:
    """One project whose manifest (``pin.txt``) pins what must be installed
    beside it (``installed.txt``, not in git), with the command that installs
    the pin and the quick test that says whether there is anything to install."""

    def __init__(self, monkeypatch, tmp_path: Path, timeout_s: int = 20, extra: str = "",
                 test: bool = True) -> None:
        self.ctl = tmp_path / "ctl"
        self.ctl.mkdir()
        self.runs, self.hook_runs = self.ctl / "runs", self.ctl / "hook-runs"
        self.install = self.ctl / "install.sh"
        self.install.write_text(
            "#!/bin/sh\n"
            f"echo run >> '{self.runs}'\n"
            f"if [ -e '{self.ctl}/sleep' ]; then sleep \"$(cat '{self.ctl}/sleep')\"; fi\n"
            f"if [ -e '{self.ctl}/fail' ]; then\n"
            "  echo 'reading the manifest'\n"
            f"  echo '{WHY[0]}'\n"
            f"  echo '{WHY[1]}' >&2\n"
            "  exit 1\n"
            "fi\n"
            f"if [ -e '{self.ctl}/scribble' ]; then echo changed >> notes.txt; fi\n"
            "cp pin.txt installed.txt\n"
        )
        asked = '\npost_merge_if = { "." = "! cmp -s pin.txt installed.txt" }' if test else ""
        self.project = _init_repo(tmp_path / "project", {
            "pin.txt": "1\n", "notes.txt": "base\n",
            ".gitignore": "/installed.txt\n/.swarm.toml\n",
        })
        (self.project / "installed.txt").write_text("1\n")
        (self.project / ".swarm.toml").write_text(
            "[git]\n"
            f'post_merge = {{ "." = "sh {self.install}" }}{asked}\n'
            f"post_merge_timeout_s = {timeout_s}\n"
            "[build]\n"
            'heavy = ["sh */install.sh"]\n'  # a real one installs and builds: it queues
            f"{extra}"
        )
        self.origin = _with_origin(self.project, tmp_path / "origin.git")
        _gate(self.project, self.hook_runs)
        monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
        monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
        monkeypatch.setenv("SWARM_GIT_MAIN", "master")
        monkeypatch.setenv("SWARM_DRIVER", "bare")
        monkeypatch.setenv("SWARM_MASTER_CMD", "true")
        monkeypatch.setenv("SWARM_SLUG", "postmerge")
        for leak in ("SWARM_WORKER_CMD", "SWARM_READY_MARKER", "SWARM_GIT_REPOS",
                     "SWARM_BUILD_HEAVY", "SWARM_BUILD_LIGHT"):
            monkeypatch.delenv(leak, raising=False)
        self.cfg = load(project_dir=str(self.project))
        state_mod.init_state(self.cfg)
        self.log = Log(self.cfg.supervisor_log)

    def worker(self, phase: str, edits: dict[str, str]) -> None:
        wt = gitq.worktree_add(self.cfg, phase, self.log)
        for rel, content in edits.items():
            (wt / rel).write_text(content)
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", f"{phase} work")

    def supervisor(self, *phases: str):
        from swarm_orchestrator.supervisor import Supervisor

        with state_mod.transaction(self.cfg) as st:
            st.resize(len(phases))
            for phase in phases:
                st.claim_slot(phase)
        return Supervisor(self.cfg)

    def count(self, path: Path) -> int:
        return len(path.read_text().splitlines()) if path.exists() else 0

    def on_origin(self) -> set[str]:
        return set(_git(self.origin, "ls-tree", "-r", "--name-only", "master").split())

    def events(self) -> list[dict]:
        path = buildlog.events_path(self.cfg)
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text().splitlines()]

    def text(self) -> str:
        return self.cfg.supervisor_log.read_text()


# -- the three cases the push gate's refusals came down to -----------------
def test_a_merge_that_moves_the_pin_runs_the_command_once_and_the_push_passes(
        monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path)
    ws.worker("P1", {"pin.txt": "2\n", "p1.txt": "1"})
    sup = ws.supervisor("P1")
    try:
        sup._on_done("P1", "ok")
    finally:
        sup.log.close()
    st = state_mod.read(ws.cfg)
    assert st.done.get("P1") == "ok" and st.integ_blocked is None
    assert ws.count(ws.runs) == 1 and (ws.project / "installed.txt").read_text() == "2\n"
    assert st.push_owed == {} and "p1.txt" in ws.on_origin()
    assert ws.count(ws.hook_runs) == 1  # one push, and the gate found the pin installed
    text = ws.text()
    assert "POST-MERGE project ok" in text
    assert "PUSH-REFUSED" not in text and "PUSH-OWED" not in text
    # It went through the build gate like any build, in the main checkout.
    mine = [e for e in ws.events() if e["argv"] == f"sh {ws.install}"]
    assert [e["event"] for e in mine] == ["queued", "start", "end"]
    assert {e["cwd"] for e in mine} == {str(ws.project)} and mine[-1]["exit"] == 0
    assert {e["phase"] for e in mine} == {"P1"}


def test_a_merge_that_does_not_move_the_pin_runs_nothing(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path)
    ws.worker("P1", {"p1.txt": "1"})
    sup = ws.supervisor("P1")
    try:
        sup._on_done("P1", "ok")
    finally:
        sup.log.close()
    st = state_mod.read(ws.cfg)
    assert st.done.get("P1") == "ok" and st.push_owed == {} and "p1.txt" in ws.on_origin()
    assert ws.count(ws.runs) == 0
    assert ws.events() == []  # it never joined the build queue
    assert "POST-MERGE" not in ws.text()


def test_a_failing_command_leaves_the_push_owed_with_its_reason_and_the_queue_moves(
        monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path)
    ws.worker("P1", {"pin.txt": "2\n", "p1.txt": "1"})
    ws.worker("P2", {"p2.txt": "2"})
    (ws.ctl / "fail").write_text("")
    sup = ws.supervisor("P1", "P2")
    try:
        sup._on_done("P1", "ok")
        st = state_mod.read(ws.cfg)
        assert st.done.get("P1") == "ok" and st.integ_blocked is None and st.integ_queue == []
        assert not any(s.phase == "P1" for s in st.busy_slots())  # slot freed
        assert "p1.txt" in _git(ws.project, "ls-tree", "-r", "--name-only", "master")
        rec = st.push_owed[str(ws.project)]
        # The command's own last lines, and not a refusal: the gate never ran.
        assert rec["reason"] == f"reading the manifest {WHY[0]} {WHY[1]}"
        assert not rec["refused"] and rec["phase"] == "P1"
        assert ws.count(ws.hook_runs) == 0 and "p1.txt" not in ws.on_origin()
        assert f"POST-MERGE-FAILED project {rec['reason']}" in ws.text()
        assert "PUSH-REFUSED" not in ws.text()

        # The next phase lands straight through, and its push tries the command again.
        sup._on_done("P2", "ok")
        st = state_mod.read(ws.cfg)
        assert st.done.get("P2") == "ok" and st.integ_blocked is None
        assert st.push_owed[str(ws.project)]["phase"] == "P1" and ws.count(ws.runs) == 2

        # What it needed is there now: the owed push's own retry installs and pushes.
        (ws.ctl / "fail").unlink()
        pushowed.retry(ws.cfg, sup.log)
        assert state_mod.read(ws.cfg).push_owed == {}
        assert ws.count(ws.runs) == 3 and {"p1.txt", "p2.txt"} <= ws.on_origin()
    finally:
        sup.log.close()


# -- the build gate, the timeout, the checkout ------------------------------
def test_a_command_that_gets_no_build_slot_in_time_runs_nothing_and_the_push_is_owed(
        monkeypatch, tmp_path):
    machine_toml(build={"max_concurrent": 1})
    ws = Ws(monkeypatch, tmp_path, timeout_s=1)
    (ws.project / "pin.txt").write_text("2\n")
    _git(ws.project, "commit", "-am", "pin 2")
    try:
        with buildsem.slot(ws.cfg, "a long build", tmp_path, "P9"):
            res = gitq.retry_push(ws.cfg, ws.project, ws.log)
    finally:
        ws.log.close()
    assert res.status == gitq.PUSH_FAILED and not res.refused
    assert res.reason.startswith("no build slot within 1s (P9 `a long build` on slot 0")
    assert res.reason.endswith("nothing ran")
    assert ws.count(ws.runs) == 0 and ws.count(ws.hook_runs) == 0
    mine = [e for e in ws.events() if e["argv"] == f"sh {ws.install}"]
    assert [e["event"] for e in mine] == ["queued", "left"] and mine[-1]["cls"] == "heavy"
    assert buildsem.live_tickets(ws.cfg, prune=False) == []  # it left the queue
    recent = buildstatus.render(buildstatus.snapshot(ws.cfg))
    assert "for a slot and left; nothing ran" in recent and "gc waited" not in recent


def test_a_command_that_runs_too_long_is_stopped_and_the_push_is_owed(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path, timeout_s=1)
    (ws.ctl / "sleep").write_text("30")
    (ws.project / "pin.txt").write_text("2\n")
    _git(ws.project, "commit", "-am", "pin 2")
    try:
        res = gitq.retry_push(ws.cfg, ws.project, ws.log)
    finally:
        ws.log.close()
    assert res.status == gitq.PUSH_FAILED and res.reason == "timed out after 1s"
    assert ws.count(ws.hook_runs) == 0
    assert "# result: timed out after 1s" in gitq._post_merge_log(ws.cfg, ws.project).read_text()


def test_a_command_that_leaves_the_tree_changed_has_failed(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path)
    (ws.ctl / "scribble").write_text("")
    (ws.project / "pin.txt").write_text("2\n")
    _git(ws.project, "commit", "-am", "pin 2")
    try:
        res = gitq.retry_push(ws.cfg, ws.project, ws.log)
    finally:
        ws.log.close()
    assert res.status == gitq.PUSH_FAILED
    assert res.reason == "the command left uncommitted changes: notes.txt"
    assert ws.count(ws.hook_runs) == 0  # the next merge would be held for them


def test_an_owed_push_from_a_checkout_the_owner_is_working_in_runs_no_command(
        monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path)
    (ws.project / "pin.txt").write_text("2\n")
    _git(ws.project, "commit", "-am", "pin 2")
    (ws.project / "notes.txt").write_text("the owner, mid-fix\n")
    try:
        res = gitq.retry_push(ws.cfg, ws.project, ws.log)
    finally:
        ws.log.close()
    assert ws.count(ws.runs) == 0
    assert res.status == gitq.PUSH_FAILED and res.refused and STALE in res.reason


def test_the_ledger_writers_commit_runs_no_command(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path, test=False)  # no quick test: it would run every time
    try:
        done = gitq.commit_to_target(
            ws.cfg, ["notes.txt"], lambda: (ws.project / "notes.txt").write_text("a row\n"),
            "ledger: a row", ws.log)
    finally:
        ws.log.close()
    assert done.status == gitq.COMMITTED and done.push.status == gitq.MERGED
    assert ws.count(ws.runs) == 0 and ws.events() == []


def test_without_the_quick_test_the_command_runs_at_every_merge(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path, test=False)
    ws.worker("P1", {"p1.txt": "1"})
    try:
        pushes: dict = {}
        assert gitq.integrate(ws.cfg, "P1", ws.log, pushes) == gitq.MERGED
    finally:
        ws.log.close()
    assert ws.count(ws.runs) == 1 and pushes[ws.project].status == gitq.MERGED


def test_a_quick_test_that_cannot_run_is_a_no_and_is_logged(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path)
    monkeypatch.setattr(repocmd, "IF_TIMEOUT_S", 0.2)
    ws.cfg.git_post_merge_if = {".": "sleep 5"}
    ws.worker("P1", {"p1.txt": "1"})
    try:
        pushes: dict = {}
        assert gitq.integrate(ws.cfg, "P1", ws.log, pushes) == gitq.MERGED
    finally:
        ws.log.close()
    assert ws.count(ws.runs) == 0 and pushes[ws.project].status == gitq.MERGED
    assert "POST-MERGE-IF-FAILED project 'sleep 5'" in ws.text()


# -- a sibling the command installs from lands first ------------------------
def test_a_repo_with_a_command_lands_after_the_sibling_its_phase_also_changed(
        monkeypatch, tmp_path):
    project = _init_repo(tmp_path / "project", {
        "notes.txt": "base\n", ".gitignore": "/app/\n/lib/\n/.swarm.toml\n"})
    app = _init_repo(project / "app", {"pin.txt": "1\n", ".gitignore": "/installed.txt\n"})
    lib = _init_repo(project / "lib", {"version.txt": "1\n"})
    for repo in (app, lib, project):
        _with_origin(repo, tmp_path / f"{repo.name}.git")
    (app / "installed.txt").write_text("1\n")
    _gate(app, tmp_path / "hook-runs")
    # The command installs what the sibling's main checkout holds, as packing it
    # there would: only a sibling that has landed can satisfy the pin.
    (project / ".swarm.toml").write_text(
        '[git]\npost_merge = { "app" = "cp ../lib/version.txt installed.txt" }\n'
        'post_merge_if = { "app" = "! cmp -s pin.txt installed.txt" }\n')
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_SLUG", "postmerge")
    monkeypatch.delenv("SWARM_GIT_REPOS", raising=False)
    cfg = load(project_dir=str(project))
    assert [r.name for r, _main in gitq._repos(cfg)] == ["lib", "app", "project"]
    log = Log(cfg.supervisor_log)
    try:
        wt = gitq.worktree_add(cfg, "P1", log)
        (wt / "lib" / "version.txt").write_text("2\n")
        (wt / "app" / "pin.txt").write_text("2\n")
        for name in ("lib", "app"):
            _git(wt / name, "commit", "-am", "P1: version 2")
        pushes: dict = {}
        assert gitq.integrate(cfg, "P1", log, pushes) == gitq.MERGED
    finally:
        log.close()
    assert (app / "installed.txt").read_text() == "2\n"
    assert pushes[app].status == gitq.MERGED and pushes[lib].status == gitq.MERGED
    assert _git(tmp_path / "app.git", "show", "master:pin.txt") == "2\n"


def test_without_a_command_the_repos_keep_their_order(monkeypatch, tmp_path):
    project = _init_repo(tmp_path / "project", {"notes.txt": "base\n"})
    for name in ("app", "lib"):
        _init_repo(project / name, {"code.txt": "base\n"})
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.delenv("SWARM_GIT_REPOS", raising=False)
    cfg = load(project_dir=str(project))
    assert [r.name for r, _main in gitq._repos(cfg)] == ["app", "lib", "project"]


# -- the runner the lane check shares ---------------------------------------
def test_the_table_is_read_by_repo_name_then_the_default(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path)
    ws.log.close()
    child = ws.project / "app"
    assert repocmd.command(ws.cfg, {".": "a", "app": "b"}, ws.project) == "a"
    assert repocmd.command(ws.cfg, {".": "a", "app": "b"}, child) == "b"
    assert repocmd.command(ws.cfg, {"*": "c"}, child) == "c"
    assert repocmd.command(ws.cfg, {".": "a"}, child) == ""


def test_a_light_command_runs_at_once_beside_a_build(monkeypatch, tmp_path):
    machine_toml(build={"max_concurrent": 1})
    ws = Ws(monkeypatch, tmp_path)
    ws.log.close()
    out = tmp_path / "out.log"
    with out.open("w") as fh, buildsem.slot(ws.cfg, "a long build", tmp_path, "P9"):
        fh.write("# header\n")
        ran = repocmd.run(ws.cfg, "echo hello", ws.project, fh, timeout_s=20, wait_s=1)
    assert ran.ok and ran.reason == ""
    text = out.read_text()
    assert text.startswith("# header\n# light command (") and text.endswith("hello\n")
    mine = [e for e in ws.events() if e["argv"] == "echo hello"]
    assert [e["event"] for e in mine] == ["bypass", "end"]


def test_a_failed_run_says_why_in_the_commands_last_lines(monkeypatch, tmp_path):
    ws = Ws(monkeypatch, tmp_path)
    ws.log.close()
    out = tmp_path / "out.log"
    with out.open("w") as fh:
        fh.write("# an earlier line of the caller's\n")
        ran = repocmd.run(ws.cfg, "seq 1 9; echo; echo ' the end ' >&2; exit 3",
                          ws.project, fh, timeout_s=20)
        quiet = repocmd.run(ws.cfg, "exit 3", ws.project, fh, timeout_s=20)
        gone = repocmd.run(ws.cfg, "echo never", tmp_path / "no-such-dir", fh, timeout_s=20)
    assert not ran.ok and ran.reason == "8 9 the end"
    assert not quiet.ok and quiet.reason == "failed with no output"
    assert not gone.ok and gone.reason.startswith("could not start: ")
