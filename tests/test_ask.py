"""``swarm ask``: a waiting window where the owner answers review questions.

A worker finishes and the owner's picks were
``owner-run`` rows the swarm never launches, so an ask covers the case where the worker
is not there and the owner needs somewhere to answer. An ask is a small session in its own
tmux window, ``ask:<name>``, that waits for the owner like a parked worker: no slot, no
timer, one ping, and the run does not finish while it is open.

The session itself is never ``claude`` here: ``SWARM_ASK_CMD`` stands in for it.
The tmux test runs on a throwaway server (its own ``TMUX_TMPDIR``), never the
live swarm's.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from swarm_orchestrator import ask as ask_mod
from swarm_orchestrator import cli
from swarm_orchestrator import doctor as doctor_mod
from swarm_orchestrator import gc as gc_mod
from swarm_orchestrator import gitq
from swarm_orchestrator import keep as keep_mod
from swarm_orchestrator import ledger as ledger_mod
from swarm_orchestrator import ovdigest, procs
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import tmux
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.master import build_context
from swarm_orchestrator.supervisor import Supervisor

NAME = "look"
ROWS = ["coral-W1", "coral-W2"]
WHY = "the owner picks the Settings and Home layouts"
BRIEF = "Open http://box:8790/ on the phone: three mockups, A B and C. Served by keep look-mockups."


def _wait(pred, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _marked(cfg, name: str = NAME) -> set[int]:
    return session_mod.session_processes(cfg, markers=session_mod.session_markers(cfg, "ask", name))


def _kill_marked(cfg, name: str = NAME) -> None:
    """End the stand-in session: its recorded pid (a process just forked still
    shows its parent's environment, so the markers alone can miss it) and
    whatever carries its markers."""
    rec = ask_mod.load(cfg, name)
    pids = _marked(cfg, name) | ({rec.pid} if rec is not None and rec.pid else set())
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def tg_lines(cfg) -> list[str]:
    sink = Path(os.environ["SWARM_TG_SINK"])
    return [ln for ln in sink.read_text(encoding="utf-8").splitlines() if ln.strip()] \
        if sink.is_file() else []


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text(
        "- [x] `coral-W0` · needs:— · the mockups\n"
        "- [ ] `coral-W1` · needs:`coral-W0` · **owner-run.** the owner picks Settings\n"
        "- [ ] `coral-W2` · needs:`coral-W0` · **owner-run.** the owner picks Home\n"
        "- [ ] `coral-W3` · needs:`coral-W0` · **owner-run.** try the draft by hand\n"
        "- [ ] `coral-W4` · needs:`coral-W1` · build the picked Settings\n",
        encoding="utf-8")
    (project / ".swarm.toml").write_text(
        '[swarm]\ndriver = "bare"\nmax_workers = 1\n'
        '[tasks]\nexclude = ["coral-W1", "coral-W2", "coral-W3"]\n', encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_BIN", "true")
    monkeypatch.setenv("SWARM_ASK_CMD", "exec sleep 300")
    for leak in ("SWARM_DRIVER", "SWARM_PHASE", procs.SESSION_ENV, "SWARM_GIT_ISOLATION",
                 "SWARM_OPERATOR", "SWARM_SESSION"):
        monkeypatch.delenv(leak, raising=False)
    monkeypatch.setattr(session_mod, "REAP_GRACE_S", 0.0)
    monkeypatch.setattr(session_mod, "END_WAIT_S", 0.5)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    yield c
    _kill_marked(c)


@pytest.fixture
def log(cfg):
    lg = Log(cfg.supervisor_log)
    yield lg
    lg.close()


def _ask(cfg, name: str = NAME, rows=ROWS) -> ask_mod.Ask:
    ask, _ = ask_mod.create(cfg, name, list(rows), WHY, BRIEF, by="worker:coral-W0")
    return ask


# -- the record and the CLI ------------------------------------------------------
def test_an_ask_needs_a_name_rows_a_one_line_why_and_a_brief(cfg):
    for name, rows, why, brief in (("bad.name", ROWS, WHY, BRIEF), (NAME, [], WHY, BRIEF),
                                   (NAME, ROWS, "", BRIEF), (NAME, ROWS, "x" * 121, BRIEF),
                                   (NAME, ROWS, WHY, " ")):
        with pytest.raises(ask_mod.AskError):
            ask_mod.create(cfg, name, rows, why, brief)
    assert ask_mod.parse_rows("coral-W1, `coral-W2` coral-W1") == ["coral-W1", "coral-W2"]


def test_swarm_ask_records_it_and_pokes_the_supervisor(cfg, capsys):
    rc = cli.cmd_ask(cfg, NAME, "coral-W1,coral-W2", WHY, BRIEF)
    out = capsys.readouterr().out
    assert rc == 0
    ask = ask_mod.load(cfg, NAME)
    assert ask.is_open and ask.rows == ROWS and ask.why == WHY and ask.brief == BRIEF
    assert ask.session == cfg.session and not ask.pinged
    assert "NOT RUNNING" in out and f"ask:{NAME}" in out  # no supervisor in this test


def test_a_live_ask_is_refused_and_a_dead_one_takes_the_new_brief(cfg, log):
    _ask(cfg)
    assert ask_mod.open_session(cfg, NAME, log)
    with pytest.raises(ask_mod.AskError, match="already open"):
        ask_mod.create(cfg, NAME, ROWS, WHY, BRIEF)
    _kill_marked(cfg)
    assert _wait(lambda: not ask_mod.session_alive(cfg, ask_mod.load(cfg, NAME)))
    ask, reopened = ask_mod.create(cfg, NAME, ROWS, WHY, "a newer brief")
    assert reopened and ask.pinged and ask.brief == "a newer brief"


# -- opening: no slot, one ping ----------------------------------------------------
def test_an_ask_takes_no_worker_slot(cfg, log):
    _ask(cfg)
    before = build_context(cfg, state_mod.read(cfg))
    assert ask_mod.open_session(cfg, NAME, log)
    assert _wait(lambda: _marked(cfg))  # its session is running …
    st = state_mod.read(cfg)
    assert not st.any_busy() and not st.pending()  # … and holds no slot
    assert build_context(cfg, st)["free_slots"] == before["free_slots"] == [0]


def test_the_owner_is_pinged_once_however_often_it_opens(cfg, log):
    _ask(cfg)
    assert ask_mod.open_session(cfg, NAME, log)
    assert ask_mod.open_session(cfg, NAME, log)  # alive: left alone
    _kill_marked(cfg)
    assert _wait(lambda: not ask_mod.session_alive(cfg, ask_mod.load(cfg, NAME)))
    assert ask_mod.open_session(cfg, NAME, log, reason="reopen")  # opens again …
    pings = [ln for ln in tg_lines(cfg) if "wait on you" in ln]
    assert len(pings) == 1  # … silently
    assert "coral-W1, coral-W2 wait on you: answer in tmux window ask:coral" in pings[0]
    assert f"tmux attach -t {cfg.session}" in pings[0]
    assert WHY in "\n".join(tg_lines(cfg))


def test_the_supervisor_writes_the_session_a_workers_mirror_could_not_know(cfg, log):
    """In a worker's mirror the default session name is the mirror's folder name."""
    right = cfg.session
    cfg.session = "coral-w0"
    _ask(cfg)
    assert ask_mod.load(cfg, NAME).session == "coral-w0"
    cfg.session = right
    assert ask_mod.open_session(cfg, NAME, log)
    ask = ask_mod.load(cfg, NAME)
    assert ask.session == right and ask.attach() == f"tmux select-window -t {right}:ask:look"
    assert f"tmux attach -t {right}" in "\n".join(tg_lines(cfg))


def test_a_workers_ask_opens_once_its_phase_has_landed(cfg):
    """A worker runs `swarm ask` before its own `swarm done`: the window waits for
    the phase to land, so its mirror (and the owner's review) has the work."""
    with state_mod.transaction(cfg) as st:
        assert st.claim_slot("coral-W0") is not None
    ask_mod.create(cfg, NAME, ROWS, WHY, BRIEF, by="worker:coral-W0")
    sup = Supervisor(cfg)
    sup._on_ask_open(NAME, "asked")
    assert "ASK-HELD coral until coral-W0 lands" in cfg.supervisor_log.read_text()
    assert not ask_mod.load(cfg, NAME).window_at

    sup._advance_done("coral-W0", "ok")
    assert _wait(lambda: "ASK-OPEN coral ok (coral-W0 landed)" in cfg.supervisor_log.read_text())
    assert ask_mod.session_alive(cfg, ask_mod.load(cfg, NAME))


def test_a_done_ask_does_not_open(cfg, log):
    _ask(cfg)
    ask_mod.complete(cfg, NAME, "picked A", False, [])
    assert ask_mod.open_session(cfg, NAME, log) is False
    assert not _marked(cfg)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not available")
def test_ask_opens_its_own_window_with_the_session_marker_and_ask_done_closes_it(
        cfg, log, tmp_path, monkeypatch):
    sockdir = tempfile.mkdtemp(prefix="asktest-")  # a throwaway tmux server
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)
    envfile = tmp_path / "env.txt"
    monkeypatch.setenv("SWARM_ASK_CMD", f"env > {envfile}; exec sleep 300")
    cfg.driver = "tmux"
    cfg.session = "asktest"
    try:
        tmux.new_session(cfg.session)
        tmux.harden(cfg.session)
        cfg = load(project_dir=str(cfg.project_dir))
        cfg.driver, cfg.session = "tmux", "asktest"
        _ask(cfg)
        assert ask_mod.open_session(cfg, NAME, log)

        win = tmux.find_window(cfg.session, "ask:look")
        assert win is not None and tmux.window_alive(win)
        assert _wait(lambda: envfile.is_file() and "SWARM_ASK=" in envfile.read_text())
        env = dict(ln.split("=", 1) for ln in envfile.read_text().splitlines() if "=" in ln)
        assert env["SWARM_SESSION_ID"] == "ask:look"
        assert env["SWARM_ASK"] == "look"
        assert env["SWARM_STATE_DIR"] == str(cfg.state_dir)
        assert env["SWARM_PROJECT"] == str(cfg.project_dir)
        assert env["TMPDIR"] == str(cfg.state_dir / "tmp" / "ask-look")
        assert ask_mod.session_alive(cfg, ask_mod.load(cfg, NAME))
        assert _wait(lambda: _marked(cfg))
        sleeper = next(iter(_marked(cfg)))

        ask_mod.complete(cfg, NAME, "picked A", False, [])
        Supervisor(cfg)._on_ask_done(NAME)

        assert tmux.find_window(cfg.session, "ask:look") is None
        assert _wait(lambda: not procs.alive(sleeper))
    finally:
        tmux.run(["kill-server"])  # the throwaway server only
        shutil.rmtree(sockdir, ignore_errors=True)


def test_the_built_in_session_reads_its_brief_from_a_file(cfg, log, monkeypatch):
    monkeypatch.delenv("SWARM_ASK_CMD", raising=False)
    cfg = load(project_dir=str(cfg.project_dir))
    ask = _ask(cfg)
    cmd = ask_mod.ask_command(cfg, NAME, cfg.project_dir)
    assert "exec claude" in cmd and "-n ask:look" in cmd
    cfg.ask_model = "opus"
    assert "--model opus" in ask_mod.ask_command(cfg, NAME, cfg.project_dir)
    cfg.ask_model, cfg.master_model = "", "sonnet"
    assert "--model sonnet" in ask_mod.ask_command(cfg, NAME, cfg.project_dir)  # "" = master's
    text = ask_mod.brief_text(cfg, ask, None)
    for part in ("prompts/ask.md", "coral-W1, coral-W2", WHY, BRIEF, "ask-done coral",
                 "--stop-keep", cfg.command_file, "project itself"):
        assert part in text
    assert len(ask_mod.pane_line(cfg, NAME)) < 300


# -- ask-done ----------------------------------------------------------------------
def test_ask_done_records_stops_the_listed_keeps_and_is_quiet(cfg, log, capsys):
    _ask(cfg)
    kept = keep_mod.start(cfg, "look-mockups", ["sleep", "300"], "serves the look mockups")
    other = keep_mod.start(cfg, "tunnel", ["sleep", "300"], "a tunnel the owner still uses")
    try:
        rc = cli.cmd_ask_done(cfg, NAME, "owner picked A for Settings, C for Home",
                              ["look-mockups"])
        out = capsys.readouterr().out
        assert rc == 0
        ask = ask_mod.load(cfg, NAME)
        assert ask.state == ask_mod.DONE and ask.stop_keeps == ["look-mockups"]
        assert ask.outcome == "owner picked A for Settings, C for Home"
        assert not procs.same(kept.pid, kept.start_ticks)
        assert keep_mod.get(cfg, "tunnel").alive  # not listed: kept
        assert "not pinged" in out  # the operator's quiet policy
        assert not any("answered" in ln for ln in tg_lines(cfg))
        assert cli.cmd_ask_done(cfg, NAME, "again") == 1  # no longer open
        assert cli.cmd_ask_done(cfg, "other", "x", attention=True) == 1
    finally:
        keep_mod.stop(cfg, "tunnel")
        keep_mod.stop(cfg, "look-mockups")


def test_ask_done_with_attention_pings(cfg):
    _ask(cfg)
    assert cli.cmd_ask_done(cfg, NAME, "the owner wants a fourth option", attention=True) == 0
    assert any("ask coral (coral-W1, coral-W2) needs you" in ln for ln in tg_lines(cfg))


def test_ask_done_ends_the_session_and_everything_it_started(cfg, log, tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("SWARM_ASK_CMD",
                       f"setsid sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}; exec sleep 300")
    cfg = load(project_dir=str(cfg.project_dir))
    _ask(cfg)
    assert ask_mod.open_session(cfg, NAME, log)
    assert _wait(lambda: pidfile.is_file() and pidfile.read_text().strip())
    child = int(pidfile.read_text())
    session = ask_mod.load(cfg, NAME).pid
    ask_mod.complete(cfg, NAME, "picked", False, [])
    Supervisor(cfg)._on_ask_done(NAME)
    session_mod.join_reaps(10)
    assert _wait(lambda: not procs.alive(child) and not procs.alive(session))
    assert f"ASK-CLOSE {NAME}" in cfg.supervisor_log.read_text()


# -- the finish waits on it ----------------------------------------------------------
def test_an_open_ask_holds_the_finish_like_a_parked_phase(cfg, monkeypatch):
    (cfg.project_dir / cfg.ledger).write_text("", encoding="utf-8")  # nothing to build
    _ask(cfg)
    sup = Supervisor(cfg)
    sup._bootstrapped = True
    sup._finish_if_settled()
    assert not state_mod.read(cfg).finished
    assert "FINISH-HELD asks=['look']" in cfg.supervisor_log.read_text()

    ask_mod.complete(cfg, NAME, "picked", False, [])
    sup._finish_if_settled()
    assert state_mod.read(cfg).finished


def test_swarm_finish_refuses_while_an_ask_waits(cfg, capsys):
    _ask(cfg)
    assert cli.cmd_finish(cfg) == 1
    assert "wait on you" in capsys.readouterr().err
    assert cli.cmd_finish(cfg, force=True) == 0


# -- visibility -------------------------------------------------------------------------
def test_list_status_and_doctor_show_open_asks(cfg, log, capsys):
    _ask(cfg)
    _ask(cfg, "home", ["coral-W3"])
    ask_mod.complete(cfg, "home", "not today", False, [])

    assert cli.cmd_ask(cfg, None, None, None, "", listing=True) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("coral [coral-W1, coral-W2] waiting on you")
    assert WHY in out[0] and f"tmux select-window -t {cfg.session}:ask:look" in out[0]
    assert out[1].startswith("home [coral-W3] done") and "not today" in out[1]

    assert cli.cmd_status(cfg) == 0
    status = capsys.readouterr().out
    assert "ask: coral [coral-W1, coral-W2] waiting on you" in status
    assert "ask: home" not in status  # answered

    check = doctor_mod._check_asks(cfg)
    assert check.name == "owner.asks" and check.status == doctor_mod.WARN
    assert "1 ask(s) wait on you" in check.detail and "WINDOW GONE" in check.detail
    assert check.fix_hint == "swarm ask --reopen look"
    assert ask_mod.open_session(cfg, NAME, log)
    check = doctor_mod._check_asks(cfg)
    assert check.status == doctor_mod.WARN and "WINDOW GONE" not in check.detail
    assert "tmux select-window" in check.fix_hint
    assert doctor_mod.exit_code([check]) == 0  # never a FAIL

    ask_mod.complete(cfg, NAME, "picked", False, [])
    assert doctor_mod._check_asks(cfg).status == doctor_mod.OK


def test_the_digest_lists_open_asks_and_unasked_owner_run_rows(cfg):
    _ask(cfg, rows=["coral-W1"])
    st = state_mod.read(cfg)
    data = ovdigest.build(cfg, st, [], since=0.0)
    # coral-W1 is asked; coral-W2 and coral-W3 are ready and nobody asks them.
    assert data["owner_run_unasked"] == ["coral-W2", "coral-W3"]
    assert any(q["state"] == "ask" and q["who"].startswith("ask look") for q in data["owner"])
    text = ovdigest.render(data)
    assert "## Owner-run rows ready, no ask open (2)" in text and "- coral-W3" in text


def test_owner_run_rows_whose_deps_have_not_landed_are_not_listed(cfg):
    graph = ledger_mod.parse("P0\nO1 needs:P0\n")
    cfg.exclude = ["O1"]
    assert ask_mod.owner_run_unasked(cfg, graph, {}) == []
    assert ask_mod.owner_run_unasked(cfg, graph, {"P0": "ok"}) == ["O1"]
    assert ask_mod.owner_run_unasked(cfg, graph, {"P0": "ok", "O1": "skip"}) == []


def test_gc_leaves_an_open_asks_mirror_and_tmp_alone(cfg):
    _ask(cfg)
    assert "ask-look" in gc_mod._live_names(cfg, state_mod.read(cfg))


# -- recovery ----------------------------------------------------------------------------
def test_swarm_up_reopens_an_unanswered_ask_and_down_ends_it(swarm):
    swarm.env["SWARM_ASK_CMD"] = "exec sleep 300"
    # Recorded while no swarm runs (the owner by hand, or a run that went down).
    made = swarm.cli("ask", "--name", NAME, "--rows", "P4", "--why", WHY, BRIEF)
    assert "NOT RUNNING" in made.stdout

    swarm.up()
    assert swarm.wait(lambda: "ASK-OPEN look ok (reopened at swarm up)" in swarm.log_text())
    rec = ask_mod._read(swarm.state_dir / "ask" / "look.json")
    assert rec.pinged and rec.pid and procs.same(rec.pid, rec.start_ticks)
    # Every phase builds, and the run still waits on the owner.
    assert swarm.wait(lambda: "FINISH-HELD asks=['look']" in swarm.log_text(), timeout=40)
    assert not swarm.finished()

    swarm.down()
    assert _wait(lambda: not procs.alive(rec.pid))
    assert ask_mod._read(swarm.state_dir / "ask" / "look.json").is_open  # the next up reopens it


def test_the_run_finishes_once_the_ask_is_answered(swarm):
    swarm.env["SWARM_ASK_CMD"] = "exec sleep 300"
    swarm.up()
    assert swarm.cli("ask", "--name", NAME, "--rows", "P4", "--why", WHY, BRIEF).returncode == 0
    assert swarm.wait(lambda: "ASK-OPEN look ok (asked)" in swarm.log_text())
    assert swarm.wait(lambda: "FINISH-HELD asks=['look']" in swarm.log_text(), timeout=40)
    pings = [ln for ln in swarm.tg_lines() if "waits on you" in ln]
    assert len(pings) == 1 and "P4 waits on you: answer in tmux window ask:look" in pings[0]

    done = swarm.cli("ask-done", NAME, "the owner picked A")
    assert "ask-done look" in done.stdout
    assert swarm.wait(swarm.finished, timeout=20)
    assert "ASK-CLOSE look" in swarm.log_text()


# -- the mirror under worktree isolation ---------------------------------------------------
@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_ask_done_lands_its_mirror_through_the_merge_queue(monkeypatch, tmp_path):
    from test_worktree import _cfg, _git, _make_project, _out

    project, origin = _make_project(tmp_path, ledger="- [ ] `coral-W1` · needs:— · owner-run\n")
    monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(tmp_path / "claude.json"))
    monkeypatch.setenv("SWARM_ASK_CMD", "exec sleep 300")
    monkeypatch.setattr(session_mod, "REAP_GRACE_S", 0.0)
    cfg = _cfg(monkeypatch, tmp_path, project, driver="bare")
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        _ask(cfg, rows=["coral-W1"])
        assert ask_mod.open_session(cfg, NAME, log)
        mirror = cfg.wt_dir / "ask-look"
        assert (mirror / "README.md").is_file() and ask_mod.load(cfg, NAME).mirror == "ask-look"
        assert ask_mod.mirror_plan(cfg) == {"ask-look": "keep"}  # `swarm up` keeps it
        ledger = mirror / "PHASE-LEDGER.md"
        ledger.write_text("- [x] `coral-W1` · needs:— · owner-run\n"
                          "  Owner's pick (2026-09-26): layout A\n")
        _git(mirror, "add", "-A")
        _git(mirror, "commit", "-m", "coral-W1: the owner's pick")

        ask_mod.complete(cfg, NAME, "picked A", False, [])
        Supervisor(cfg)._on_ask_done(NAME)

        _git(project, "checkout", "master")
        assert "Owner's pick (2026-09-26): layout A" in (project / "PHASE-LEDGER.md").read_text()
        assert "PHASE-LEDGER.md" in _out(origin, "ls-tree", "-r", "--name-only", "master")
        assert not mirror.exists()
        assert _out(project, "branch", "--list", "swarm/*").strip() == ""
        st = state_mod.read(cfg)
        assert st.integ_queue == [] and "ask-look" not in st.done and NAME not in st.done
        assert "ASK-INTEGRATED ask-look" in cfg.supervisor_log.read_text()
    finally:
        _kill_marked(cfg)
        log.close()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_swarm_up_lands_an_answered_asks_mirror_that_never_merged(monkeypatch, tmp_path):
    from test_worktree import _cfg, _git, _make_project

    project, _origin = _make_project(tmp_path)
    monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(tmp_path / "claude.json"))
    cfg = _cfg(monkeypatch, tmp_path, project, driver="bare")
    cfg.ensure_dirs()
    log = Log(cfg.supervisor_log)
    try:
        _ask(cfg)
        mirror = gitq.worktree_add(cfg, "ask-look", log)
        (mirror / "picked.txt").write_text("A\n")
        _git(mirror, "add", "-A")
        _git(mirror, "commit", "-m", "pick")
        ask_mod.complete(cfg, NAME, "picked", False, [])

        plan = ask_mod.mirror_plan(cfg)
        assert plan == {"ask-look": "integrate"}
        result = gitq.reconcile(cfg, {}, log, operator=cli._mirror_plan(cfg))
        assert result.operator_integrated == ["ask-look"]
        _git(project, "checkout", "master")
        assert (project / "picked.txt").read_text() == "A\n"
    finally:
        log.close()


# -- the dashboard and the board ---------------------------------------------------------
def test_ask_rows_carry_rows_why_state_and_the_attach_command(cfg):
    from swarm_orchestrator.tui import data, tables

    _ask(cfg)
    _ask(cfg, "home", ["coral-W3"])
    ask_mod.complete(cfg, "home", "not today", False, ["look-mockups"])
    rows = data.ask_rows(data.load_asks(cfg))
    assert [r["name"] for r in rows] == ["look", "home"]  # open first
    look, home = rows
    assert look["open"] and look["state"] == "waiting" and look["rows_text"] == "coral-W1, coral-W2"
    assert look["attach"] == f"tmux select-window -t {cfg.session}:ask:look"
    assert not home["open"] and home["outcome"] == "not today"
    cells = tables.ask_row(look)
    assert len(cells) == len(tables.ASK_COLUMNS)
    assert "coral-W1" in cells[2] and WHY[:20] in cells[3] and "ask:coral" in cells[6]
    assert "not today" in tables.ask_detail(home) and "look-mockups" in tables.ask_detail(home)
    assert BRIEF[:30] in tables.ask_detail(look)


def test_the_dash_sees_a_new_ask(cfg):
    from swarm_orchestrator.tui.dash import Dash

    dash = Dash(cfg)
    dash.poll()
    assert dash.asks == []
    _ask(cfg)
    assert "asks" in dash.poll()
    assert [a.name for a in dash.asks] == [NAME]


def test_a_shows_the_asks_tab(cfg, monkeypatch, capfd):
    from swarm_orchestrator.tui.app import SwarmApp
    from swarm_orchestrator.tui.dash import Dash

    _ask(cfg)
    monkeypatch.setattr(Dash, "probe", lambda self, now=None: None)
    app = SwarmApp(cfg)
    got: dict = {}

    async def drive():
        async with app.run_test(size=(140, 40)) as pilot:
            app.dash.poll()
            app.refresh_all()
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            tab = app.query_one("#tab-asks")
            got["active"] = app.query_one("#tabs").active
            got["cols"] = [str(c.label) for c in tab.table.columns.values()]
            got["rows"] = [r["name"] for r in tab.rows]
            got["head"] = tab.query_one(".tab-head")._swarm_text
            got["detail"] = tab.query_one(".detail-body")._swarm_text

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(drive(), timeout=30))
    assert got["active"] == "asks"
    assert {"name", "rows", "what you decide", "attach"} <= set(got["cols"])
    assert got["rows"] == [NAME]
    assert "1 waiting on you" in got["head"]
    assert "tmux select-window" in got["detail"]


def test_the_board_lists_open_asks_without_their_brief(tmp_path, monkeypatch):
    from swarm_orchestrator.web.feed import Feed
    from test_web_board import make_run

    cfg = make_run(tmp_path, monkeypatch)
    feed = Feed(cfg)
    feed.refresh(force=True)
    assert feed.board["asks"] == []
    _ask(cfg)
    assert feed.refresh()
    (row,) = feed.board["asks"]
    assert row["name"] == NAME and row["rows"] == ROWS and row["why"] == WHY
    assert row["attach"].endswith(":ask:look")
    assert "brief" not in row


# -- the prompts ---------------------------------------------------------------------------
def test_the_prompts_tell_sessions_to_open_an_ask():
    repo = Path(__file__).resolve().parent.parent
    for name in ("init_master.md", "overseer.md", "operator.md"):
        flat = " ".join((repo / "prompts" / name).read_text(encoding="utf-8").split())
        assert "swarm ask --name" in flat and "--rows" in flat and "--why" in flat, name
    flat = " ".join((repo / "prompts" / "ask.md").read_text(encoding="utf-8").split())
    for text in ("AskUserQuestion", "Owner's pick:", "swarm ask-done", "--stop-keep",
                 "owner-run", "swarm record"):
        assert text in flat
