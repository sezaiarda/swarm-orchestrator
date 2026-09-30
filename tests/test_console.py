"""The owner console: its config, its primer, its conversation id, and its window.

The window's lifecycle runs on a fully isolated tmux server (its own
``TMUX_TMPDIR``, killed after, so no session can outlive the test) with a fake
``claude`` (``[console] cmd``) that logs how it was started and exits when told
to, the way ``/exit`` ends the real one.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from swarm_orchestrator import cli, console, promptlint, resolver, tmux
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.resources import ptree


def _wait(pred, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def project(tmp_path, monkeypatch):
    pdir = tmp_path / "project"
    pdir.mkdir()
    (pdir / ".swarm.toml").write_text('[swarm]\ndriver = "bare"\n', encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    for leak in ("SWARM_DRIVER", "SWARM_SESSION", "SWARM_PHASE", "SWARM_SESSION_ID"):
        monkeypatch.delenv(leak, raising=False)
    return pdir


@pytest.fixture
def cfg(project):
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


# -- config ------------------------------------------------------------------------
def test_console_config_defaults_file_and_env(project, monkeypatch):
    monkeypatch.delenv("SWARM_CONSOLE", raising=False)
    c = load(project_dir=str(project))
    assert (c.console_enabled, c.console_prompt_file, c.console_model, c.console_cmd) == (
        True, "", "", "")
    (project / ".swarm.toml").write_text(
        '[console]\nenabled = false\nprompt_file = "docs/console.md"\nmodel = "opus"\n'
        'cmd = "my-claude --x"\n', encoding="utf-8")
    c = load(project_dir=str(project))
    assert (c.console_enabled, c.console_prompt_file, c.console_model, c.console_cmd) == (
        False, "docs/console.md", "opus", "my-claude --x")
    monkeypatch.setenv("SWARM_CONSOLE", "1")
    monkeypatch.setenv("SWARM_CONSOLE_CMD", "fake")
    c = load(project_dir=str(project))
    assert c.console_enabled and c.console_cmd == "fake"


# -- the primer ----------------------------------------------------------------------
def test_the_primer_names_every_cli_command(cfg):
    text = console.primer(cfg)
    missing = [name for name in sorted(cli._known_commands()) if f"`{name}`" not in text]
    assert missing == [], f"commands missing from the console primer: {missing}"
    # One line each, with the CLI's own help, for the ones that are the console's.
    assert "- `follow-up` — file a new ledger row" in text
    assert "`done`" in text.split("## Commands")[1].split("\n\n")[-1]  # listed as not its


def test_the_primer_says_where_the_swarm_keeps_things(cfg):
    text = console.primer(cfg)
    for fact in (cfg.ledger, cfg.lessons, cfg.history_dir, str(cfg.state_dir), cfg.session,
                 f"swarm --project-dir {cfg.project_dir}"):
        assert fact in text
    assert "{{" not in text


def test_the_primer_appends_the_projects_prompt_file(cfg, capsys):
    cfg.console_prompt_file = "docs/console-extra.md"
    assert "## This project" not in console.primer(cfg)
    assert "cannot be read" in capsys.readouterr().err
    (cfg.project_dir / "docs").mkdir()
    (cfg.project_dir / "docs" / "console-extra.md").write_text("Rows here are called waves.\n")
    text = console.primer(cfg)
    assert text.rstrip().endswith("## This project\n\nRows here are called waves.")


def test_the_console_prompt_lints_clean():
    path = resolver.prompt_path(console.PROMPT)
    assert promptlint.lint(path.read_text(encoding="utf-8"),
                           known_commands=cli._known_commands()) == []


# -- the conversation id ---------------------------------------------------------------
def test_resume_by_id_once_a_transcript_exists_else_that_id_is_started(cfg, tmp_path):
    assert console.session_id(cfg) is None
    sid = console.new_session_id(cfg)
    assert console.session_id(cfg) == sid and not console.has_transcript(sid)
    argv = console.claude_argv(cfg, sid, resume=False)
    assert argv[:5] == ["claude", "-n", "swarm · console", "--session-id", sid]
    assert "--continue" not in argv and "--resume" not in argv
    # Claude Code wrote the conversation: the next launch resumes exactly it.
    tdir = tmp_path / "claude" / "projects" / "-some-project"
    tdir.mkdir(parents=True)
    (tdir / f"{sid}.jsonl").write_text("{}\n")
    assert console.has_transcript(sid)
    argv = console.claude_argv(cfg, sid, resume=True)
    assert argv[:5] == ["claude", "-n", "swarm · console", "--resume", sid]
    assert argv[-2] == "--append-system-prompt" and "owner's console" in argv[-1]
    # --new: a fresh id, never the old one.
    assert console.new_session_id(cfg) not in (sid, None)


def test_model_and_command_come_from_the_config(cfg):
    cfg.console_model = "opus"
    cfg.console_cmd = "/opt/claude --verbose"
    argv = console.claude_argv(cfg, "abc", resume=True)
    assert argv[:7] == ["/opt/claude", "--verbose", "--model", "opus", "-n", "swarm · console",
                        "--resume"]


# -- not a worker ------------------------------------------------------------------------
def test_the_console_carries_no_session_or_phase_marker(cfg):
    cfg.env_marker = "MY_PHASE"
    base = {"PATH": "/bin", "SWARM_PHASE": "P1", "MY_PHASE": "P1", "SWARM_SESSION_ID":
            "worker:P1", "SWARM_WORKTREE": "/wt/P1", "SWARM_OVERSEER_PASS": "p1",
            "SWARM_SLUG": "s"}
    env = console.claude_env(cfg, base)
    assert env == {"PATH": "/bin", "SWARM_SLUG": "s", "SWARM_STATE_DIR": str(cfg.state_dir),
                   console.CONSOLE_ENV: "1"}
    assert console.CONSOLE_ENV == ptree.CONSOLE_ENV


def test_no_session_reaper_matches_the_console_but_down_ends_it(cfg):
    """Every mid-run reaper ends one session by its markers; the console has none.
    `swarm down` ends everything carrying the run's state dir, the console too."""
    env = console.claude_env(cfg, {**os.environ, "SWARM_PHASE": "P1",
                                   "SWARM_SESSION_ID": "worker:P1"})
    proc = subprocess.Popen(["sleep", "30"], env=env)
    try:
        assert _wait(lambda: f"SWARM_STATE_DIR={cfg.state_dir}".encode()
                     in Path(f"/proc/{proc.pid}/environ").read_bytes().split(b"\0"), 5)
        for kind, ident in (("worker", "P1"), ("operator", "job"), ("overseer", "p1"),
                            ("resolver", "P1"), ("guide", "x"), ("big-picture", "x")):
            markers = session_mod.session_markers(cfg, kind, ident)
            assert proc.pid not in session_mod.session_processes(cfg, markers=markers), kind
        assert proc.pid in session_mod.session_processes(cfg)  # what `swarm down` ends
        # the resource sampler files it as the owner's: not a worker, not overhead
        table = ptree.scan()
        assert ptree.Attributor(cfg.state_dir).label(table).get(proc.pid) == ptree.CONSOLE
    finally:
        proc.kill()
        proc.wait()


def test_console_on_the_bare_driver_says_so(cfg, capsys):
    assert cli.main(["--project-dir", str(cfg.project_dir), "console"]) == 2
    assert "needs the tmux driver" in capsys.readouterr().err


# -- the window, on an isolated tmux server ---------------------------------------------------
FAKE_CLAUDE = r"""#!/bin/sh
# Stands in for claude: logs how it was started, writes the transcript the real one
# would, and runs until the test touches the quit file (the owner's /exit).
prev=""
for a in "$@"; do
  case "$prev" in
    --resume) echo "resume $a" >> "$SWARM_FAKE_LOG" ;;
    --session-id) echo "new $a" >> "$SWARM_FAKE_LOG"
      mkdir -p "$CLAUDE_CONFIG_DIR/projects/p" && : > "$CLAUDE_CONFIG_DIR/projects/p/$a.jsonl" ;;
  esac
  prev="$a"
done
echo "env phase=${SWARM_PHASE-unset} session=${SWARM_SESSION_ID-unset}" >> "$SWARM_FAKE_LOG"
echo "FAKE CLAUDE UP"
while [ ! -e "$SWARM_FAKE_QUIT" ]; do sleep 0.05; done
rm -f "$SWARM_FAKE_QUIT"
"""


@pytest.fixture
def live(tmp_path, monkeypatch):
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")
    sockdir = tempfile.mkdtemp(prefix="consoleprobe-")
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".swarm.toml").write_text(
        '[swarm]\nmax_workers = 2\ndriver = "tmux"\n[tmux]\nsession = "consoleprobe"\n'
        "[web]\nenabled = false\n[tui]\nautostart = false\n", encoding="utf-8")
    fake = tmp_path / "fake-claude.sh"
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    log, quit_ = tmp_path / "fake.log", tmp_path / "quit"
    for key, val in {"SWARM_STATE_DIR": str(tmp_path / "state"), "SWARM_CONSOLE": "1",
                     "SWARM_CONSOLE_CMD": str(fake), "SWARM_FAKE_LOG": str(log),
                     "SWARM_FAKE_QUIT": str(quit_), "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
                     # A worker's markers in the environment must never reach the console.
                     "SWARM_PHASE": "leak", "SWARM_SESSION_ID": "worker:leak",
                     "SWARM_TG_SINK": str(tmp_path / "tg.log")}.items():
        monkeypatch.setenv(key, val)
    for leak in ("SWARM_DRIVER", "SWARM_SESSION", "SWARM_LAYOUT", "SWARM_PANES_PER_WINDOW",
                 "SWARM_GIT_ISOLATION", "SWARM_MASTER_CMD"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    c.log, c.quit = log, quit_  # for the tests; not config
    try:
        yield c
    finally:
        tmux.run(["kill-server"])
        shutil.rmtree(sockdir, ignore_errors=True)


def _log(cfg) -> list[str]:
    return cfg.log.read_text().splitlines() if cfg.log.is_file() else []


def _starts(cfg) -> list[str]:
    return [ln for ln in _log(cfg) if ln.startswith(("new ", "resume "))]


def _pane(cfg) -> str:
    return tmux.list_panes(tmux.find_window(cfg.session, console.WINDOW))[0]


def _exit_claude(cfg) -> None:
    """The owner types /exit: the fake ends, the keeper goes idle."""
    cfg.quit.touch()
    pane = _pane(cfg)
    assert _wait(lambda: console.IDLE_LINE[:40] in tmux.capture_joined(pane))
    assert _wait(lambda: console.pane_state(pane) == console.IDLE)


def test_up_puts_the_console_between_the_dashboard_and_the_overseer(live):
    cfg = live
    windows = session_mod.setup(cfg)
    names = tmux.run(["list-windows", "-t", f"={cfg.session}", "-F", "#{window_name}"])
    assert names.stdout.split() == ["dash", "console", "overseer", "operator", "workers"]
    assert windows["console"] == tmux.find_window(cfg.session, console.WINDOW)
    # It starts at once, as a new conversation whose id the swarm chose and kept.
    assert _wait(lambda: len(_starts(cfg)) == 1)
    assert _starts(cfg) == [f"new {console.session_id(cfg)}"]
    assert "env phase=unset session=unset" in _log(cfg)
    # Not a slot: untagged, in no slot record, and every slot still free.
    pane = _pane(cfg)
    assert tmux.list_panes_with_slot(windows["console"]) == [(pane, "")]
    st = state_mod.read(cfg)
    assert pane not in {s.pane_id for s in st.slots}
    assert len(st.slots) == cfg.max_workers == len(st.free_slots())
    assert console.pane_state(pane) == console.RUNNING


def test_exit_leaves_the_pane_and_the_key_reopens_the_same_conversation(live):
    cfg = live
    session_mod.setup(cfg)
    assert _wait(lambda: len(_starts(cfg)) == 1)
    sid = console.session_id(cfg)
    _exit_claude(cfg)
    pane = _pane(cfg)
    assert tmux.window_alive(tmux.find_window(cfg.session, console.WINDOW))
    time.sleep(0.5)
    assert len(_starts(cfg)) == 1  # closed on purpose: never relaunched on its own
    # `o` in the dashboard (and `swarm console`) is open_console: it resumes by id.
    what, _ = console.open_console(cfg)
    assert what == console.REOPENED
    assert _wait(lambda: len(_starts(cfg)) == 2)
    assert _starts(cfg)[-1] == f"resume {sid}" and _pane(cfg) == pane
    # Pressed again while it runs: only focused, never a second claude.
    assert _wait(lambda: console.pane_state(pane) == console.RUNNING)
    what, win = console.open_console(cfg)
    assert what == console.FOCUSED
    current = tmux.run(["display-message", "-p", "-t", f"={cfg.session}:", "#{window_id}"])
    assert current.stdout.strip() == win  # the client is moved there
    time.sleep(0.5)
    assert len(_starts(cfg)) == 2


def test_new_starts_a_fresh_conversation_and_is_refused_while_one_runs(live):
    cfg = live
    session_mod.setup(cfg)
    assert _wait(lambda: len(_starts(cfg)) == 1)
    sid = console.session_id(cfg)
    assert _wait(lambda: console.pane_state(_pane(cfg)) == console.RUNNING)
    with pytest.raises(console.ConsoleError, match="/exit it there first"):
        console.open_console(cfg, new=True)
    assert console.session_id(cfg) == sid
    _exit_claude(cfg)
    what, _ = console.open_console(cfg, new=True)
    assert what == console.REOPENED
    assert _wait(lambda: len(_starts(cfg)) == 2)
    fresh = console.session_id(cfg)
    assert fresh != sid and _starts(cfg)[-1] == f"new {fresh}"


def test_a_missing_window_or_dead_keeper_is_started_again_beside_the_dashboard(live):
    cfg = live
    session_mod.setup(cfg)
    assert _wait(lambda: len(_starts(cfg)) == 1)
    sid = console.session_id(cfg)
    keeper = int(tmux.run(["display-message", "-p", "-t", _pane(cfg), "#{pane_pid}"])
                 .stdout.strip())
    os.killpg(keeper, signal.SIGKILL)  # the keeper dies, and its claude with it
    assert _wait(lambda: console.pane_state(_pane(cfg)) == console.DEAD)
    assert console.open_console(cfg)[0] == console.OPENED
    assert _wait(lambda: len(_starts(cfg)) == 2) and _starts(cfg)[-1] == f"resume {sid}"
    _exit_claude(cfg)
    tmux.kill_window(tmux.find_window(cfg.session, console.WINDOW))
    assert console.open_console(cfg)[0] == console.OPENED
    names = tmux.run(["list-windows", "-t", f"={cfg.session}", "-F", "#{window_name}"])
    assert names.stdout.split() == ["dash", "console", "overseer", "operator", "workers"]
    assert _wait(lambda: len(_starts(cfg)) == 3) and _starts(cfg)[-1] == f"resume {sid}"


def test_console_off_opens_no_window_and_says_so(live, monkeypatch):
    monkeypatch.setenv("SWARM_CONSOLE", "0")
    cfg = load(project_dir=str(live.project_dir))
    windows = session_mod.setup(cfg)
    assert "console" not in windows and tmux.find_window(cfg.session, "console") is None
    with pytest.raises(console.ConsoleError, match="off"):
        console.open_console(cfg)
