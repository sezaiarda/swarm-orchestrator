"""Several swarms on one machine: a command acts on the swarm it names, only.

Every session a swarm launches carries its ``SWARM_STATE_DIR``, and that variable
decides where a command reads and writes. Nothing used to check it against the
project the command named, so ``swarm --project-dir B down`` typed in a session
of swarm A (its console is taught that very form) sent the shutdown into A's
FIFO and ended A's processes under B's name. The state dir records the project
it belongs to; a command naming another one is refused before it touches
anything. Real supervisors, the bare driver, fake master and workers.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from swarm_orchestrator import console, restart
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import tmux
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import WrongSwarm, load


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
