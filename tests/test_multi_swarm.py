"""Several swarms on one machine: a command acts on the swarm it names, only.

Every session a swarm launches carries its ``SWARM_STATE_DIR``, and that variable
decides where a command reads and writes. Nothing used to check it against the
project the command named, so ``swarm --project-dir B down`` typed in a session
of swarm A (its console is taught that very form) sent the shutdown into A's
FIFO and ended A's processes under B's name. The state dir records the project
it belongs to; a command naming another one is refused before it touches
anything.

And a command names a swarm only where there is one. A folder with no
``.swarm.toml`` used to load as a project of defaults with a state dir of its
own, so a command one folder off, in a renamed folder or with a mistyped
``--project-dir`` addressed a new, empty swarm in silence. Real supervisors, the
bare driver, fake master and workers.

The swarms also share one tmux server, with each other and with the owner's own
sessions: what a swarm sets there it sets on its own windows, and a session of
the same name that is somebody else's is said to be. Those tests run a real
tmux, on a server of their own (its own ``TMUX_TMPDIR``, killed after).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from swarm_orchestrator import cli, console, restart
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import tmux
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import NoProject, WrongSwarm, find_project, load


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat[stat.rfind(")") + 2:][:1] not in ("Z", "X")


def _environ(pid: int) -> dict[str, str]:
    raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    return dict(e.decode().split("=", 1) for e in raw if b"=" in e)


def _has_reader(inst) -> bool:
    """Is a supervisor reading ``inst``'s control FIFO?"""
    try:
        os.close(os.open(inst.state_dir / "control.fifo", os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        return False
    return True


def _up(inst) -> int:
    """Bring ``inst`` up with a worker that holds its slot; the supervisor's pid."""
    inst.env["FAKE_WORKER_PARK"] = "1"
    inst.up()
    assert inst.wait(lambda: inst.busy_phases() == ["P0"], timeout=25), inst.log_text()
    assert (inst.state_dir / "config.json").is_file()
    return inst.state()["supervisor_pid"]


def _in_session_of(inst, *, told_its_project: bool = True) -> dict[str, str]:
    """The environment of a session ``inst`` launched: a worker's, or (not told
    its project) a console pane from before it was."""
    env = {**inst.env, "SWARM_STATE_DIR": str(inst.state_dir)}
    if told_its_project:
        env["SWARM_PROJECT"] = str(inst.project)
    return env


def _swarm(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "swarm_orchestrator", *args], cwd=str(cwd),
                          env=env, capture_output=True, text=True, timeout=60)


# -- two runs, two state dirs --------------------------------------------------
def test_two_swarms_run_side_by_side_and_stopping_one_leaves_the_other(two_swarms):
    a, b = two_swarms
    pid_a, pid_b = _up(a), _up(b)
    assert a.state_dir != b.state_dir and a.state_dir.parent == b.state_dir.parent
    assert pid_a != pid_b
    assert _environ(pid_a)["SWARM_STATE_DIR"] == str(a.state_dir)
    assert _environ(pid_b)["SWARM_STATE_DIR"] == str(b.state_dir)

    assert a.cli("down").returncode == 0

    assert a.wait(lambda: not _alive(pid_a), timeout=10)
    assert not _has_reader(a)
    assert _alive(pid_b) and _has_reader(b) and b.busy_phases() == ["P0"]
    assert b.cli("status").stdout.count("BUSY P0") == 1


# -- --project-dir B under A's state -------------------------------------------
@pytest.mark.parametrize("command", [["down"], ["pause"], ["status"], ["finish", "--force"]])
@pytest.mark.parametrize("told", [True, False], ids=["worker", "old-console"])
def test_a_session_of_one_swarm_cannot_run_a_command_on_another(two_swarms, command, told):
    a, b = two_swarms
    pid_a = _up(a)
    env = _in_session_of(a, told_its_project=told)

    r = _swarm(env, a.project, "--project-dir", str(b.project), *command)

    assert r.returncode == 2, r.stdout + r.stderr
    assert str(a.project) in r.stderr and str(b.project) in r.stderr
    assert str(a.state_dir) in r.stderr
    assert r.stdout == ""
    # Nothing reached the swarm whose state dir the session carries.
    st = a.state()
    assert _alive(pid_a) and not st["paused"] and not st["finished"]
    assert a.busy_phases() == ["P0"]
    assert "EVENT shutdown" not in a.log_text() and "EVENT pause" not in a.log_text()
    # Nor was a run made for the one it named.
    assert not b.state_dir.exists()


def test_the_same_refusal_from_another_projects_folder(two_swarms):
    """A pane that was never told its project (``SWARM_PROJECT``) resolves it
    from the cwd: ``cd B && swarm down`` there named B just the same."""
    a, b = two_swarms
    pid_a = _up(a)
    r = _swarm(_in_session_of(a, told_its_project=False), b.project, "down")
    assert r.returncode == 2 and str(b.project) in r.stderr and str(a.project) in r.stderr
    assert _alive(pid_a) and a.busy_phases() == ["P0"]


def test_a_sessions_own_commands_still_reach_its_swarm_from_any_folder(two_swarms):
    a, b = two_swarms
    _up(a)
    env = _in_session_of(a)
    assert "BUSY P0" in _swarm(env, b.project, "status").stdout
    assert _swarm(env, b.project, "pause").returncode == 0
    assert a.wait(lambda: a.state()["paused"], timeout=10)
    assert _swarm(env, a.project, "--project-dir", str(a.project), "resume").returncode == 0
    assert a.wait(lambda: not a.state()["paused"], timeout=10)
    assert not b.state_dir.exists()


def test_load_refuses_a_project_the_named_state_dir_does_not_belong_to(two_swarms, monkeypatch):
    a, b = two_swarms
    _up(a)
    monkeypatch.setenv("SWARM_STATE_DIR", str(a.state_dir))
    assert load(project_dir=str(a.project)).state_dir == a.state_dir
    with pytest.raises(WrongSwarm) as exc:
        load(project_dir=str(b.project))
    assert str(a.project) in str(exc.value) and str(b.project) in str(exc.value)
    monkeypatch.chdir(b.project)
    assert cli_main(["status"]) == 2


def test_a_state_dir_no_supervisor_ran_in_belongs_to_nobody_yet(tmp_path, monkeypatch):
    """The first ``swarm up`` of a project has nothing to be checked against."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    assert load(project_dir=str(project)).state_dir == tmp_path / "state"
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "config.json").write_text("{not json")
    assert load(project_dir=str(project)).project_dir == project


def _pin_slug(inst, slug: str) -> None:
    toml = inst.project / ".swarm.toml"
    toml.write_text(toml.read_text().replace("[swarm]\n", f'[swarm]\nslug = "{slug}"\n', 1))
    inst.state_dir = inst.state_dir.parent / slug


def test_two_projects_given_one_slug_cannot_share_a_state_dir(two_swarms):
    """Nothing checked: the second `up` started its run in the first one's
    state dir, over its done records and its ledger queue."""
    a, b = two_swarms
    for inst in (a, b):
        _pin_slug(inst, "one-slug")
    pid_a = _up(a)
    for command in (["up", "--no-attach"], ["status"], ["down"]):
        r = b.cli(*command, check=False)
        assert r.returncode == 2, r.stdout + r.stderr
        assert str(a.project) in r.stderr and str(b.project) in r.stderr
        assert "one-slug" in r.stderr and "SWARM_STATE_DIR" not in r.stderr
    assert _alive(pid_a) and a.busy_phases() == ["P0"]


def test_a_project_moved_with_its_slug_pinned_takes_its_state_along(two_swarms, tmp_path):
    a, _ = two_swarms
    _pin_slug(a, "kept")
    _up(a)
    assert a.cli("down").returncode == 0
    moved = tmp_path / "moved"
    a.project.rename(moved)
    a.project = moved
    assert "P0" in a.cli("status").stdout
    _up(a)  # the supervisor too, whose state dir is named for it
    assert a.state_dir.name == "kept" and _environ(a.state()["supervisor_pid"])[
        "SWARM_STATE_DIR"] == str(a.state_dir)


# -- what the swarm's own processes are started with -----------------------------
def test_up_gives_the_supervisor_its_state_dir_and_none_of_a_sessions_marks(two_swarms):
    """`swarm up` typed in a session: the supervisor is the swarm's own. It
    carried the session's marker (and was ended with it) and its temp dir."""
    a, _ = two_swarms
    cfg_tmp = a.state_dir / "tmp" / "P9"
    cfg_tmp.mkdir(parents=True)
    a.env.update({"SWARM_SESSION_ID": "worker:P9", "SWARM_PHASE": "P9",
                  "SWARM_PROJECT": str(a.project), "SWARM_OWNER_CONSOLE": "1",
                  "SWARM_OPERATOR_JOB": "job", "TMPDIR": str(cfg_tmp),
                  "SWARM_BUILD_MAX": "3"})
    env = _environ(_up(a))
    assert env["SWARM_STATE_DIR"] == str(a.state_dir)
    for key in ("SWARM_SESSION_ID", "SWARM_PHASE", "SWARM_PROJECT", "SWARM_OWNER_CONSOLE",
                "SWARM_OPERATOR_JOB", "TMPDIR"):
        assert key not in env, key
    # The owner's own override, typed before `swarm up`, is not a session's mark.
    assert env["SWARM_BUILD_MAX"] == "3"


def test_a_supervisor_started_by_restart_is_given_its_state_dir_too(two_swarms):
    a, _ = two_swarms
    old = _up(a)
    os.kill(old, 9)
    assert a.wait(lambda: not _alive(old), timeout=10)
    assert "SWARM_STATE_DIR" not in a.env
    a.cli("restart", timeout=120)
    assert a.wait(lambda: a.state()["supervisor_pid"] not in (None, old), timeout=20)
    assert _environ(a.state()["supervisor_pid"])["SWARM_STATE_DIR"] == str(a.state_dir)


def test_the_build_caps_are_a_sessions_only_inside_a_mirror(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    cfg = load(project_dir=str(tmp_path))
    caps = {"SWARM_BUILD_MAX": "1", "SWARM_BUILD_JOBS": "4", "CARGO_BUILD_JOBS": "4"}
    assert restart.clean_env(cfg, {**caps, "PATH": "/bin"}) == {**caps, "PATH": "/bin"}
    assert restart.clean_env(cfg, {**caps, "PATH": "/bin", "SWARM_WORKTREE": "/wt/P1"}) \
        == {"PATH": "/bin"}


def test_the_console_and_the_dashboard_are_told_their_project(tmp_path, monkeypatch):
    """They carried the state dir alone, so a command typed there for another
    project (by path, or from its folder) ran on this swarm's state."""
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    cfg = load(project_dir=str(tmp_path))
    want = {"SWARM_STATE_DIR": str(cfg.state_dir), "SWARM_PROJECT": str(cfg.project_dir)}
    assert want.items() <= console.claude_env(cfg, {"PATH": "/bin"}).items()
    assert want.items() <= console.pane_env(cfg).items()
    seen: dict = {}
    monkeypatch.setattr(tmux, "respawn_pane", lambda pane, cmd, env=None: seen.update(env))
    session_mod.start_dashboard(cfg, "%0")
    assert seen == want


# -- no phantom swarms: which project a command names ----------------------------
@pytest.fixture
def tree(tmp_path, monkeypatch):
    """``work/project`` is a project; ``work/project/crate/src`` is inside it;
    ``work/plain`` is a folder beside it."""
    monkeypatch.delenv("SWARM_PROJECT", raising=False)
    project = tmp_path / "work" / "project"
    (project / "crate" / "src").mkdir(parents=True)
    (project / ".swarm.toml").write_text("")
    (tmp_path / "work" / "plain" / "deep").mkdir(parents=True)
    return project


def test_the_project_is_the_nearest_folder_with_a_swarm_toml_from_the_cwd_up(
        tree, monkeypatch):
    for cwd in (tree, tree / "crate", tree / "crate" / "src"):
        monkeypatch.chdir(cwd)
        assert find_project() == tree
    (tree / "crate" / ".swarm.toml").write_text("")
    monkeypatch.chdir(tree / "crate" / "src")
    assert find_project() == tree / "crate"


def test_a_folder_under_no_project_names_none_and_the_refusal_says_where_it_looked(
        tree, monkeypatch):
    plain = tree.parent / "plain" / "deep"
    monkeypatch.chdir(plain)
    with pytest.raises(NoProject) as exc:
        find_project()
    assert str(plain) in str(exc.value) and "any folder above" in str(exc.value)


def test_a_named_project_is_taken_as_it_is_and_must_be_one(tree, tmp_path, monkeypatch):
    monkeypatch.chdir(tree.parent / "plain")
    assert find_project(str(tree)) == tree
    for how, name in (("--project-dir", None), ("SWARM_PROJECT", "SWARM_PROJECT")):
        for path, why in ((tree / "crate", "holds no .swarm.toml"),
                          (tmp_path / "gone", "not a directory")):
            if name:
                monkeypatch.setenv(name, str(path))
            with pytest.raises(NoProject) as exc:
                find_project(None if name else str(path))
            assert how in str(exc.value) and str(path) in str(exc.value)
            assert why in str(exc.value)


def test_the_flag_beats_the_session_which_beats_the_cwd(tree, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    (other / ".swarm.toml").write_text("")
    monkeypatch.chdir(tree)
    monkeypatch.setenv("SWARM_PROJECT", str(other / "." ))
    assert find_project() == other
    assert find_project(str(tree)) == tree


def test_config_names_the_file_of_a_project_that_holds_none(tree, monkeypatch):
    plain = tree.parent / "plain"
    file = tree / ".swarm.toml"
    monkeypatch.chdir(plain)
    assert find_project(explicit=str(file)) == plain
    assert find_project(str(plain / "deep"), str(file)) == plain / "deep"
    with pytest.raises(NoProject) as exc:
        find_project(str(tree), str(plain / "none.toml"))
    assert "--config" in str(exc.value)


# -- no phantom swarms: what the commands do -------------------------------------
def _state_root(inst) -> Path:
    return Path(inst.env["XDG_STATE_HOME"])


@pytest.mark.parametrize("command", [
    ["up", "--no-attach"], ["down"], ["pause"], ["resume"], ["status"], ["doctor"],
    ["done", "P0", "ok"], ["build", "--", "true"], ["keep", "--list"], ["gc"],
    ["freeze"], ["note", "P0", "a decision"],
])
def test_no_command_makes_a_swarm_of_a_folder_that_is_not_a_project(
        two_swarms, tmp_path, command):
    a, _ = two_swarms
    folder = tmp_path / "typo" / "deeper"
    folder.mkdir(parents=True)
    r = _swarm(a.env, folder, *command)
    assert r.returncode == 2, r.stdout + r.stderr
    assert ".swarm.toml" in r.stderr and str(folder) in r.stderr
    assert not _state_root(a).exists() or not any(_state_root(a).iterdir())


@pytest.mark.parametrize("command", [["--help"], ["status", "-h"], ["up", "-h"]])
def test_help_needs_no_project(two_swarms, tmp_path, command):
    a, _ = two_swarms
    r = _swarm(a.env, tmp_path, *command)
    assert r.returncode == 0 and r.stdout.startswith("usage: swarm")
    assert not _state_root(a).exists() or not any(_state_root(a).iterdir())


@pytest.mark.parametrize("command", [["up", "--no-attach"], ["pause"], ["status"]])
def test_a_mistyped_project_dir_is_refused_not_started(two_swarms, tmp_path, command):
    a, _ = two_swarms
    for wrong in (tmp_path / "alpah", a.project / "nested"):
        (a.project / "nested").mkdir(exist_ok=True)
        r = _swarm(a.env, a.project, "--project-dir", str(wrong), *command)
        assert r.returncode == 2 and str(wrong) in r.stderr, r.stdout + r.stderr
    assert not _state_root(a).exists() or not any(_state_root(a).iterdir())


def test_a_command_typed_inside_a_project_reaches_its_swarm_not_one_of_its_own(two_swarms):
    """A build started in a component folder ran on a private gate of that
    folder's own state dir, beside the project's."""
    a, _ = two_swarms
    _up(a)
    inside = a.project / "component" / "src"
    inside.mkdir(parents=True)
    assert "BUSY P0" in _swarm(a.env, inside, "status").stdout
    assert _swarm(a.env, inside, "build", "--", "true").returncode == 0
    assert _swarm(a.env, inside, "pause").returncode == 0
    assert a.wait(lambda: a.state()["paused"], timeout=10)
    assert [d.name for d in (_state_root(a) / "swarm-orchestrator").iterdir()] \
        == [a.state_dir.name]


#: Commands that only read. None of them may leave a run behind for a project
#: that was never started.
READ_ONLY = [
    ["status"], ["status", "--json"], ["status", "--all"], ["context"], ["why", "P0"],
    ["report"], ["todo"], ["check"], ["doctor"], ["usage"], ["resources"], ["overseer"],
    ["big-picture"], ["layout"], ["reload", "--dry-run"], ["build", "--status"],
    ["keep", "--list"], ["gc"],
]


@pytest.mark.parametrize("command", READ_ONLY, ids=" ".join)
def test_a_read_only_command_creates_no_state_dir(two_swarms, command):
    a, _ = two_swarms
    r = _swarm(a.env, a.project, *command)
    assert r.returncode in (0, 1), r.stdout + r.stderr  # doctor: 1 = it found something
    assert "Traceback" not in r.stderr
    assert not a.state_dir.exists()
    assert not _state_root(a).exists() or not any(_state_root(a).iterdir())


# -- one tmux server, several sessions -------------------------------------------
@pytest.fixture
def server(monkeypatch):
    """A tmux server of the test's own, with one session that is the owner's."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")
    sockdir = tempfile.mkdtemp(prefix="swarm-multi-")
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)  # never nest onto the outer server
    try:
        tmux.new_session("mine")
        yield
    finally:
        tmux.run(["kill-server"])
        shutil.rmtree(sockdir, ignore_errors=True)


def _opt(*args: str) -> str:
    return tmux.run(["show-options", *args]).stdout.strip()


def _eventually(pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _tmux_cfg(project: Path, state: Path, monkeypatch, session: str = "shared"):
    project.mkdir(parents=True, exist_ok=True)
    (project / ".swarm.toml").write_text(
        f'[tmux]\nsession = "{session}"\n[tui]\nautostart = false\n')
    monkeypatch.setenv("SWARM_STATE_DIR", str(state))
    monkeypatch.setenv("SWARM_DRIVER", "tmux")
    monkeypatch.delenv("SWARM_SESSION", raising=False)
    return load(project_dir=str(project))


def test_a_swarm_sets_its_tmux_options_on_its_own_windows_only(server, tmp_path, monkeypatch):
    """They were set with ``-g``: on every window of every session on the
    owner's server, and still there after the swarm was gone."""
    tmux.run(["set-option", "-g", "renumber-windows", "on"])  # the owner's own choice
    cfg = _tmux_cfg(tmp_path / "project", tmp_path / "state", monkeypatch, "swarm-a")
    windows = session_mod.setup(cfg)
    pane = tmux.split_layout(windows["workers"], 2)[0]
    parked, _ = tmux.park_pane(windows["workers"], pane, 0, "wait:P1", cfg.session)

    # Nothing server-wide, and nothing on the owner's session.
    assert _opt("-g", "-w", "-v", "remain-on-exit") == "off"
    assert _opt("-g", "-w", "-v", "automatic-rename") == "on"
    assert _opt("-g", "-v", "renumber-windows") == "on"
    assert _opt("-w", "-t", "=mine:", "-v", "remain-on-exit") == ""
    assert _opt("-t", "=mine:", "-v", "renumber-windows") == ""
    # Every window of the swarm's, the one a parked pane was moved into too.
    assert _opt("-t", f"={cfg.session}:", "-v", "renumber-windows") == "off"
    for win in (*windows.values(), parked):
        got = [_opt("-w", "-t", win, "-v", name) for name, _ in tmux.WINDOW_OPTS]
        assert got == [value for _, value in tmux.WINDOW_OPTS], win

    # And they do what they are for: a command that exits leaves its pane.
    for target in (windows["master"], parked):
        dying = tmux.list_panes(target)[0]
        tmux.respawn_pane(dying, "true")
        assert _eventually(lambda: tmux.pane_states(cfg.session).get(dying) is True)
    # In the owner's session it still closes, as they left it.
    tmux.run(["respawn-pane", "-k", "-t", "=mine:", "true"])
    assert _eventually(lambda: not tmux.session_exists("mine"))


def test_two_swarms_with_one_session_name_the_second_is_told_whose_it_is(
        server, tmp_path, monkeypatch, capsys):
    """It was told "the swarm is already up … or `swarm down` first", and that
    `down` then said the session was not its own: it could never start."""
    first = _tmux_cfg(tmp_path / "one", tmp_path / "state-one", monkeypatch)
    session_mod.setup(first)
    Path(first.state_dir, "config.json").write_text(
        '{"name": "the first", "project_dir": "%s"}' % first.project_dir)
    second = _tmux_cfg(tmp_path / "two", tmp_path / "state-two", monkeypatch)
    assert second.session == first.session == "shared"

    assert cli.cmd_up(second, attach=False) == 1
    said = capsys.readouterr().err
    assert "belongs to another swarm: the swarm the first" in said
    assert str(first.project_dir) in said and "[tmux]" in said and "already up" not in said
    assert not session_mod.owns_session(second) and session_mod.owns_session(first)
    assert not (second.state_dir / "state.json").exists()  # refused before anything
    # Its `down` leaves the other swarm's session alone, and says so.
    assert cli.cmd_down(second) == 0
    assert tmux.session_exists("shared") and "not this swarm's" in capsys.readouterr().err
    with pytest.raises(RuntimeError, match="belongs to another swarm"):
        session_mod.setup(second)


def test_a_session_of_the_owners_own_with_the_swarms_name_is_said_to_be_theirs(
        server, tmp_path, monkeypatch, capsys):
    cfg = _tmux_cfg(tmp_path / "project", tmp_path / "state", monkeypatch, "mine")
    assert cli.cmd_up(cfg, attach=False) == 1
    said = capsys.readouterr().err
    assert "is not a swarm's" in said and "tmux rename-session -t =mine" in said
    assert "[tmux]" in said and "`swarm down` first" not in said
    assert tmux.session_exists("mine") and tmux.session_owner("mine") == ""


def test_its_own_session_left_up_is_still_said_to_be_up(server, tmp_path, monkeypatch, capsys):
    cfg = _tmux_cfg(tmp_path / "project", tmp_path / "state", monkeypatch, "swarm-a")
    session_mod.setup(cfg)
    assert cli.cmd_up(cfg, attach=False) == 1
    said = capsys.readouterr().err
    assert "already up" in said and "tmux attach -t =swarm-a" in said and "swarm down" in said


@pytest.mark.parametrize("inside", [True, False], ids=["switch", "attach"])
def test_attach_names_the_session_exactly(tmp_path, monkeypatch, inside):
    """A bare name is a prefix to tmux: ``mag`` landed in ``magnar``."""
    cfg = _tmux_cfg(tmp_path / "project", tmp_path / "state", monkeypatch, "mag")
    ran: list[list[str]] = []
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(tmux, "session_exists", lambda name: True)
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **_k: ran.append(argv))
    monkeypatch.setattr(cli.os, "execvp", lambda _file, argv: ran.append(argv))
    if inside:
        monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,1,0")
    else:
        monkeypatch.delenv("TMUX", raising=False)
    cli._attach(cfg)
    assert ran == [["tmux", "switch-client" if inside else "attach", "-t", "=mag"]]
