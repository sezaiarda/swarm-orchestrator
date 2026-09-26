"""The big-picture pass: its trigger, the landing of its doc, the runner, the CLI.

The runner tests drive a real :class:`bigpic.Runner` on the bare driver with a
shell one-liner playing the session (``[big_picture].cmd``), against a real git
repo standing in for the project; nothing ``claude`` ever starts.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from swarm_orchestrator import bigpic
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor

DRAFT = '# Big picture\n\nWhere the project stands.\n'


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "ledger.txt").write_text("P0\nP1 needs:P0\nP2 needs:P1\n")
    (project / ".swarm.toml").write_text(
        '[swarm]\nmax_workers = 2\ndriver = "bare"\n[tasks]\nledger = "ledger.txt"\n'
        '[big_picture]\nevery = 2\n',
        encoding="utf-8",
    )
    _git(project, "init", "-q", "-b", "master")
    _git(project, "config", "user.email", "t@example.invalid")
    _git(project, "config", "user.name", "t")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "base")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(tmp_path / "claude.json"))
    monkeypatch.setenv("SWARM_WATCHDOG", "0")
    monkeypatch.delenv("SWARM_BIG_PICTURE_EVERY")
    monkeypatch.setenv("SWARM_BIG_PICTURE_CMD", 'printf "# Big picture\\n" > "$SWARM_BIG_PICTURE_DRAFT"')
    for leak in ("SWARM_DRIVER", "SWARM_GIT_ISOLATION", bigpic.PASS_ENV):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


def _runner(cfg) -> bigpic.Runner:
    return bigpic.Runner(cfg, Log(cfg.state_dir / "logs" / "supervisor.log"))


def _integrate(cfg, *phases, status="ok"):
    with state_mod.transaction(cfg) as st:
        for p in phases:
            st.mark_done(p, status)


def _wait(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


# -- config --------------------------------------------------------------------
def test_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("SWARM_BIG_PICTURE_EVERY")
    (tmp_path / "p").mkdir()
    c = load(project_dir=str(tmp_path / "p"))
    assert (c.big_picture_every, c.big_picture_max_age_h, c.big_picture_doc,
            c.big_picture_model, c.big_picture_cmd) == (10, 0, "docs/BIG-PICTURE.md", "opus", "")
    assert bigpic.enabled(c)


# -- the policy ------------------------------------------------------------------
def test_the_first_look_baselines_and_only_integrated_phases_count():
    mem = bigpic.Memory()
    assert bigpic.observe(mem, {"P0": "ok", "P1": "ok"}) == 0 and mem.since == 0
    assert bigpic.observe(mem, {"P0": "ok", "P1": "ok", "P2": "fail", "P3": "skip",
                                "P4": "operator"}) == 1
    assert mem.since == 1


def test_due_on_the_counter_the_age_the_missing_doc_and_a_request(cfg):
    mem = bigpic.Memory(last_at=1.0, since=1)
    assert bigpic.due(cfg, mem, 100.0) is None
    mem.since = 2
    assert "2 phase(s)" in bigpic.due(cfg, mem, 100.0)
    assert bigpic.due(cfg, mem, 100.0, paused=True) is None
    mem.retry_at = 200.0
    assert bigpic.due(cfg, mem, 100.0) is None
    mem.requested = True
    assert bigpic.due(cfg, mem, 100.0, paused=True) == "requested"
    mem.live = "x"
    assert bigpic.due(cfg, mem, 100.0) is None

    cfg.big_picture_every, cfg.big_picture_max_age_h = 0, 2
    mem = bigpic.Memory(last_at=1000.0, since=0)
    assert bigpic.due(cfg, mem, 1000.0 + 3 * 3600) is None  # nothing landed since
    mem.since = 1
    assert "over 2h old" in bigpic.due(cfg, mem, 1000.0 + 3 * 3600)
    assert bigpic.due(cfg, bigpic.Memory(), 5.0, doc_exists=False) == "the doc does not exist yet"

    cfg.big_picture_max_age_h = 0
    assert not bigpic.enabled(cfg)
    assert bigpic.due(cfg, bigpic.Memory(since=50), 5.0, doc_exists=False) is None


# -- landing -----------------------------------------------------------------------
def test_land_commits_only_the_doc_and_is_idempotent(cfg):
    repo = cfg.project_dir
    (repo / "other.txt").write_text("staged by someone else\n")
    _git(repo, "add", "other.txt")
    outcome, sha = bigpic.land(cfg, DRAFT, "big picture: refresh")
    assert outcome == bigpic.LANDED and sha == _git(repo, "rev-parse", "--short", "HEAD")
    assert _git(repo, "show", "--name-only", "--format=", "HEAD") == "docs/BIG-PICTURE.md"
    assert "other.txt" in _git(repo, "diff", "--cached", "--name-only")
    assert bigpic.land(cfg, DRAFT, "again") == (bigpic.UNCHANGED, "")


def test_land_waits_for_a_tree_that_is_not_ready(cfg):
    repo = cfg.project_dir
    bigpic.land(cfg, DRAFT, "first")
    (repo / "docs" / "BIG-PICTURE.md").write_text("the owner's own edit\n")
    outcome, why = bigpic.land(cfg, DRAFT + "more\n", "second")
    assert outcome == bigpic.WAITING and "uncommitted edits" in why
    assert (repo / "docs" / "BIG-PICTURE.md").read_text() == "the owner's own edit\n"
    _git(repo, "checkout", "-q", "--", "docs/BIG-PICTURE.md")
    _git(repo, "checkout", "-q", "-b", "elsewhere")
    outcome, why = bigpic.land(cfg, DRAFT + "more\n", "second")
    assert outcome == bigpic.WAITING and "elsewhere" in why


def test_a_doc_path_outside_the_project_never_lands(cfg):
    cfg.big_picture_doc = "../escape.md"
    assert bigpic.land(cfg, DRAFT, "x")[0] == bigpic.WAITING
    assert not (cfg.project_dir.parent / "escape.md").exists()


def test_check_draft():
    assert bigpic.check_draft("  \n") == bigpic.NO_DRAFT
    assert bigpic.check_draft("x" * (bigpic.MAX_BYTES + 1)) == bigpic.TOO_BIG
    assert bigpic.check_draft(DRAFT) is None


# -- the runner ------------------------------------------------------------------------
def _run_pass(cfg, r: bigpic.Runner, summary="rewrote it") -> str:
    r.tick()
    pid = r.mem.live
    assert pid
    assert _wait(lambda: r._proc is not None)
    r.handle("big-picture-spawned", [pid, "ok"])
    assert _wait(lambda: bigpic.draft_path(cfg, pid).is_file())
    r.handle("big-picture-done", [pid, *summary.split()])
    return pid


def test_a_pass_starts_on_the_counter_and_its_doc_lands(cfg):
    r = _runner(cfg)
    bigpic.draft_path(cfg, "x").parent.mkdir(parents=True, exist_ok=True)
    (cfg.project_dir / "docs").mkdir()
    (cfg.project_dir / "docs" / "BIG-PICTURE.md").write_text("old\n")  # no first-pass trigger
    _git(cfg.project_dir, "add", "docs")
    _git(cfg.project_dir, "commit", "-q", "-m", "old doc")
    r.tick()  # baseline
    _integrate(cfg, "P0")
    r.tick()
    assert not r.mem.live and r.mem.since == 1
    _integrate(cfg, "P1")
    pid = _run_pass(cfg, r)
    mem = bigpic.load(cfg)
    assert (mem.live, mem.since, mem.last_status, mem.last_summary) == ("", 0, bigpic.LANDED, "rewrote it")
    assert mem.last_head and mem.last_at
    assert _git(cfg.project_dir, "log", "-1", "--format=%s") == (
        "big picture: refresh docs/BIG-PICTURE.md — rewrote it")
    assert (cfg.project_dir / "docs" / "BIG-PICTURE.md").read_text() == "# Big picture\n"
    brief = bigpic.brief_path(cfg, pid).read_text()
    assert str(bigpic.draft_path(cfg, pid)) in brief and "big-picture-done" in brief


def test_a_request_starts_a_pass_and_a_second_waits_for_it(cfg):
    r = _runner(cfg)
    r.handle("big-picture-now", [])
    pid = r.mem.live
    assert pid
    r.handle("big-picture-now", [])
    assert r.mem.live == pid and not r.mem.requested
    assert _wait(lambda: r._proc is not None)
    r.handle("big-picture-done", ["someone-else", "x"])
    assert r.mem.live == pid


def test_a_draft_that_cannot_land_yet_lands_on_a_later_wake(cfg):
    r = _runner(cfg)
    r.mem.requested = True
    _git(cfg.project_dir, "checkout", "-q", "-b", "owner-branch")
    _run_pass(cfg, r)
    assert r.mem.land_pending and r.mem.last_status == bigpic.WAITING
    assert "owner-branch" in bigpic.status_text(cfg, r.mem)
    r.tick()
    assert r.mem.land_pending and not r.mem.live  # no new pass while it waits
    _git(cfg.project_dir, "checkout", "-q", "master")
    r.tick()
    assert not r.mem.land_pending and r.mem.last_status == bigpic.LANDED


def test_an_oversized_draft_is_not_landed_and_the_counter_is_given_back(cfg, monkeypatch):
    monkeypatch.setattr(bigpic, "MAX_BYTES", 4)
    r = _runner(cfg)
    r.mem.since, r.mem.requested = 3, True
    _run_pass(cfg, r)
    assert (r.mem.last_status, r.mem.since) == (bigpic.TOO_BIG, 3)
    assert r.mem.retry_at > time.time()
    assert not (cfg.project_dir / "docs" / "BIG-PICTURE.md").exists()


def test_a_hung_pass_times_out_and_a_dead_one_is_noticed(cfg, monkeypatch):
    monkeypatch.setenv("SWARM_BIG_PICTURE_CMD", "sleep 30")
    cfg = load(project_dir=str(cfg.project_dir))
    r = _runner(cfg)
    r.mem.requested = True
    r.tick()
    pid = r.mem.live
    assert _wait(lambda: r._proc is not None)
    r.handle("big-picture-spawned", [pid, "ok"])
    r.tick(now=r.mem.live_at + bigpic.TIMEOUT_S + 1)
    assert (r.mem.live, r.mem.last_status) == ("", bigpic.TIMEOUT)

    monkeypatch.setenv("SWARM_BIG_PICTURE_CMD", "true")
    r.cfg = load(project_dir=str(cfg.project_dir))
    r.mem.requested = True
    r.tick()
    pid = r.mem.live
    assert _wait(lambda: r._proc is not None and r._proc.poll() is not None)
    r.handle("big-picture-spawned", [pid, "ok"])
    r.tick()
    assert (r.mem.live, r.mem.last_status) == ("", bigpic.DIED)


def test_a_pass_that_would_not_start_backs_off(cfg):
    r = _runner(cfg)
    r.mem.requested = True
    r.mem.since = 2
    r.tick()
    pid = r.mem.live
    assert r.mem.since == 0
    r.handle("big-picture-spawned", [pid, "failed"])
    assert (r.mem.live, r.mem.last_status, r.mem.since) == ("", bigpic.NO_START, 2)
    r.tick()
    assert not r.mem.live  # backing off despite the counter


def test_a_restart_marks_the_live_pass_interrupted(cfg):
    bigpic.save(cfg, bigpic.Memory(live="20260101T000000Z", live_at=time.time(), live_taken=4))
    r = _runner(cfg)
    r.recover()
    mem = bigpic.load(cfg)
    assert (mem.live, mem.last_status, mem.since) == ("", bigpic.INTERRUPTED, 4)


# -- the supervisor and the CLI -------------------------------------------------------------
def test_the_supervisor_routes_big_picture_events(cfg):
    sup = Supervisor(cfg)
    sup._handle("big-picture-now")
    pid = sup.bigpic.mem.live
    assert pid
    assert _wait(lambda: bigpic.draft_path(cfg, pid).is_file())
    sup._handle(f"big-picture-done {pid} the summary")
    assert sup.bigpic.mem.last_status == bigpic.LANDED
    assert bigpic.load(cfg).last_summary == "the summary"


def test_big_picture_done_refuses_a_missing_or_oversized_draft(cfg, monkeypatch, capsys):
    monkeypatch.setenv(bigpic.PASS_ENV, "20260101T000000Z")
    args = ["--project-dir", str(cfg.project_dir), "big-picture-done", "x"]
    assert cli_main(args) == 1
    assert "write the draft" in capsys.readouterr().err
    draft = bigpic.draft_path(cfg, "20260101T000000Z")
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("x" * (bigpic.MAX_BYTES + 1))
    assert cli_main(args) == 1
    assert "cut it" in capsys.readouterr().err
    draft.write_text(DRAFT)
    assert cli_main(args) == 1  # a valid draft, but no supervisor to hear it
    assert "not running" in capsys.readouterr().out


def test_status_lines(cfg, capsys):
    now = 10_000.0
    mem = bigpic.Memory(last_status=bigpic.LANDED, last_end=now - 7200, since=1)
    assert bigpic.status_text(cfg, mem, now) == (
        "big picture: refreshed 2.0h ago · next after 1 more phase(s)")
    assert bigpic.short_text(mem, now) == "big picture 2.0h ago"
    assert bigpic.web_view(cfg, mem) == {"text": "refreshed · next after 1 more phase(s)",
                                         "at": now - 7200}
    assert bigpic.short_text(bigpic.Memory(), now) == ""
    bigpic.save(cfg, mem)
    assert cli_main(["--project-dir", str(cfg.project_dir), "big-picture"]) == 0
    assert "big picture: refreshed" in capsys.readouterr().out


def test_gc_leaves_a_live_pass_tmpdir_alone(cfg):
    from swarm_orchestrator import gc as gc_mod

    (cfg.tmp_dir / bigpic.WINDOW).mkdir(parents=True)
    (cfg.tmp_dir / bigpic.WINDOW / "f").write_text("x")
    bigpic.save(cfg, bigpic.Memory(live="20260101T000000Z", live_at=time.time()))
    labels = [t.label for t in gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True)).targets]
    assert f"tmp/{bigpic.WINDOW}" not in labels
    bigpic.save(cfg, bigpic.Memory())
    labels = [t.label for t in gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True)).targets]
    assert f"tmp/{bigpic.WINDOW}" in labels
