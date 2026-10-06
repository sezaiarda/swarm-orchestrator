"""``swarm freeze`` / ``swarm thaw``: stop every session in place, then wake them.

Three tiers, all against the ``fake_cgroups`` tree (nothing is really frozen,
and no test asks systemd for anything):

* the seam and the plan — which groups a freeze stops and which stay awake;
* the commands with no supervisor — the locks, the rollback, the order and
  the gap of the wake-up, the refused verbs, a record left by a reboot;
* a real supervisor with fake workers — it stands still while frozen and
  carries on, in order, at the thaw.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from swarm_orchestrator import cli
from swarm_orchestrator import drain as drain_mod
from swarm_orchestrator import freezer
from swarm_orchestrator import landing as landing_mod
from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import models as models_mod
from swarm_orchestrator import procs
from swarm_orchestrator import restart as restart_mod
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import tgbot
from swarm_orchestrator import tmux
from swarm_orchestrator.config import load
from swarm_orchestrator.state import State
from swarm_orchestrator.supervisor import FROZEN_POLL_S, Supervisor
from swarm_orchestrator import service as service_mod
from swarm_orchestrator.web import lifecycle as web_lifecycle

LEDGER = "- [ ] `P0` · needs:—\n- [ ] `P1` · needs:—\n- [ ] `P2` · needs:—\n"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    (tmp_path / ".swarm.toml").touch()
    cfg = load(project_dir=str(tmp_path))
    state_mod.init_state(cfg)
    return cfg


@pytest.fixture
def stand_in(cfg, fake_cgroups):
    """Start a process that stands for one of the run's: it carries the state
    dir and, given one, a session id. Ended with the test."""
    procs: list[subprocess.Popen] = []

    def start(session: str | None = None, marked: bool = True) -> int:
        env = {k: v for k, v in os.environ.items()
               if k not in ("SWARM_STATE_DIR", "SWARM_SESSION_ID")}
        if marked:
            env["SWARM_STATE_DIR"] = str(cfg.state_dir)
        if session:
            env["SWARM_SESSION_ID"] = session
        procs.append(subprocess.Popen(["sleep", "300"], env=env, start_new_session=True))
        return procs[-1].pid

    try:
        yield start
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()


def _record(cfg, *groups: dict, stage: str = freezer.FROZEN, since: float | None = None) -> None:
    with state_mod.transaction(cfg) as st:
        st.frozen = {"since": time.time() if since is None else since, "stage": stage,
                     "quiet_at": 0.0, "waiting": [], "quiesced": True,
                     "cgroups": list(groups), "awake": []}


def _spans(cfg) -> list[dict]:
    path = cfg.state_dir / "history" / freezer.SPANS
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines()]


# -- the seam -----------------------------------------------------------------
def test_the_seam_reads_a_group_and_writes_its_freeze(fake_cgroups):
    cg = freezer.Cgroups.from_env()
    assert cg.of("self") == "/login"
    assert cg.of(999999999) is None
    fake_cgroups.group("/a/b")
    assert cg.exists("/a/b") and not cg.asked("/a/b") and not cg.settled("/a/b")
    assert cg.set("/a/b", True)
    assert cg.asked("/a/b")
    deadline = time.monotonic() + 5
    while not cg.settled("/a/b") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert cg.settled("/a/b")
    assert not cg.set("/gone", True) and not cg.exists("/gone")


def test_scoped_adds_the_prefix_only_when_the_probe_passes(monkeypatch):
    argv = ["python", "-m", "thing"]
    monkeypatch.setattr(freezer, "can_scope", lambda _env=None: True)
    scoped = freezer.scoped(argv)
    assert scoped == ["systemd-run", "--user", "--scope", "--quiet", "--collect", "--", *argv]
    assert freezer.in_scope(scoped)
    monkeypatch.setattr(freezer, "can_scope", lambda _env=None: False)
    assert freezer.scoped(argv) == argv and not freezer.in_scope(argv)


def test_the_probe_is_never_run_with_scopes_switched_off(monkeypatch):
    def no_run(*_a, **_k):
        raise AssertionError("systemd was asked")

    monkeypatch.setattr(freezer.subprocess, "run", no_run)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/0")
    assert os.environ["SWARM_SCOPE"] == "0"  # the harness's rule
    assert freezer.can_scope() is False


def test_the_probe_is_asked_of_the_environment_the_process_will_start_in(monkeypatch):
    asked: list[dict] = []

    def run(_argv, **kw):
        asked.append(kw["env"])
        return subprocess.CompletedProcess(_argv, 0)

    monkeypatch.setattr(freezer.subprocess, "run", run)
    monkeypatch.setattr(freezer.shutil, "which", lambda _name, path=None: "/usr/bin/systemd-run")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/0")
    theirs = {"PATH": "/usr/bin", "XDG_RUNTIME_DIR": "/run/user/1000"}

    assert freezer.can_scope(theirs) is True

    assert asked == [theirs]  # probed in the environment given, not this process's
    # One with no user runtime dir cannot make a scope, whatever this process could.
    assert freezer.can_scope({"PATH": "/usr/bin"}) is False and asked == [theirs]
    assert freezer.scoped(["true"], {"PATH": "/usr/bin"}) == ["true"]


def test_a_scope_is_the_command_itself_in_a_group_of_its_own(tmp_path, monkeypatch):
    """The one test that asks systemd for anything: a transient scope around a
    command that prints where it is and ends."""
    monkeypatch.delenv("SWARM_SCOPE", raising=False)  # the harness switches both off
    monkeypatch.setenv("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    if not freezer.can_scope():
        pytest.skip("no systemd user scope can be made here")
    script = (
        "import json, os\n"
        "group = open('/proc/self/cgroup').read().strip().rpartition('::')[2]\n"
        "members = open('/sys/fs/cgroup' + group + '/cgroup.procs').read().split()\n"
        "print(json.dumps({'pid': os.getpid(), 'cwd': os.getcwd(), 'group': group,\n"
        "                  'mark': os.environ.get('FZ_MARK'), 'members': members}))\n"
    )
    argv = freezer.scoped([sys.executable, "-c", script])
    assert freezer.in_scope(argv)

    proc = subprocess.Popen(argv, cwd=tmp_path, env={**os.environ, "FZ_MARK": "kept"},
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)
    out, err = proc.communicate(timeout=60)

    assert proc.returncode == 0, err
    got = json.loads(out)
    # systemd-run became the command: the pid the starter holds is the command's.
    assert got["pid"] == proc.pid
    assert got["cwd"] == str(tmp_path) and got["mark"] == "kept"
    assert got["group"].endswith(".scope") and got["group"] != freezer.Cgroups().of("self")
    assert got["members"] == [str(proc.pid)]  # alone in it


def test_the_run_starts_its_own_processes_through_the_scope_seam(cfg, monkeypatch):
    started: list[list[str]] = []

    class Proc:
        pid = 4242

        def poll(self):
            return 0

    def popen(argv, **_kw):
        started.append(list(argv))
        return Proc()

    asked: list[dict | None] = []
    monkeypatch.setattr(freezer, "can_scope", lambda env=None: asked.append(env) or True)
    monkeypatch.setattr(subprocess, "Popen", popen)
    at = web_lifecycle.place(cfg.state_dir)
    monkeypatch.setattr(web_lifecycle, "probe",
                        lambda *_a, **_k: web_lifecycle.Found(web_lifecycle.CLOSED))
    monkeypatch.setattr(restart_mod, "START_TIMEOUT_S", 0.0)
    monkeypatch.setattr(cfg, "telegram_commands", True)
    monkeypatch.setattr(tgbot, "credentials", lambda _c: ("token", "chat"))
    from swarm_orchestrator.logutil import Log

    log = Log(cfg.supervisor_log)
    try:
        landing_mod._spawn_check(cfg, "P0", cfg.project_dir)
        service_mod.start(web_lifecycle.the_service(at), at.mdir)  # the machine's board
        tgbot.start_detached(cfg)
        theirs = {**os.environ, "FZ_WHOSE": "the last supervisor's"}
        restart_mod.start_supervisor(cfg, theirs, log)
        drain_mod.run_after(cfg, "true")
    finally:
        log.close()
    assert len(started) == 6  # the supervisor is tried twice
    assert all(freezer.in_scope(argv) for argv in started), started
    # The supervisor's scope is asked for in the environment it is started in.
    assert theirs in asked


# -- what a freeze stops ----------------------------------------------------------
def test_the_plan_is_by_membership_and_leaves_the_runs_own_groups_awake(
        cfg, fake_cgroups, stand_in, monkeypatch):
    worker = stand_in("worker:P0")
    operator = stand_in("operator:op-1")
    helper = stand_in()  # carries the run, is no session: a detached helper
    server = stand_in(marked=False)
    dash, console, idle = (stand_in(marked=False) for _ in range(3))
    fake_cgroups.place(server, "/tmux")
    fake_cgroups.place(dash, "/pane-dash")
    fake_cgroups.place(console, "/pane-console")
    fake_cgroups.place(idle, "/tmux")  # a pane the server did not give a group
    fake_cgroups.place(helper, "/helper")
    monkeypatch.setattr(cfg, "driver", "tmux")
    monkeypatch.setattr(session_mod, "owns_session", lambda *_a: True)
    monkeypatch.setattr(tmux, "server_pid", lambda: server)
    monkeypatch.setattr(tmux, "session_pane_windows", lambda _s: [
        (dash, "dash"), (console, "console"), (idle, "operator"), (worker, "workers")])
    fake_cgroups.settle()

    plan = freezer.plan(cfg, state_mod.read(cfg))

    assert plan.frozen == [
        {"path": "/helper", "kind": "other", "id": ""},
        {"path": "/pane-console", "kind": "console", "id": ""},
        {"path": "/pane-dash", "kind": "dashboard", "id": ""},
        {"path": "/run/operator-op-1", "kind": "operator", "id": "op-1"},
        {"path": "/run/worker-P0", "kind": "worker", "id": "P0"},
    ]
    assert plan.awake == [
        {"path": "/login", "kind": "caller", "shared": False},
        {"path": "/tmux", "kind": "tmux", "shared": True},  # the idle pane stays awake
    ]
    assert operator  # in the plan through its marker alone: no pane names it


def test_a_group_the_supervisor_shares_is_awake_and_says_what_it_holds(
        cfg, fake_cgroups, stand_in, monkeypatch):
    supervisor = stand_in()
    worker = stand_in("worker:P0")
    fake_cgroups.place(supervisor, "/shell")
    fake_cgroups.place(worker, "/shell")  # started by the supervisor, never moved
    monkeypatch.setattr(restart_mod, "live_supervisor", lambda *_a: supervisor)
    fake_cgroups.settle()

    plan = freezer.plan(cfg, state_mod.read(cfg))

    assert plan.frozen == []
    assert {"path": "/shell", "kind": "supervisor", "shared": True} in plan.awake


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not available")
def test_tmux_names_its_server_and_each_panes_process_and_window(monkeypatch):
    """What the plan asks tmux, on a server of the test's own (own TMUX_TMPDIR,
    killed after): the default server, and any live swarm on it, is never touched."""
    sockdir = tempfile.mkdtemp(prefix="fzprobe-")
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)  # never nest onto the outer server
    try:
        dash = tmux.new_session("fzprobe")
        tmux.rename_window(dash, "dash")
        tmux.new_window("fzprobe", "console")

        panes = tmux.session_pane_windows("fzprobe")
        server = tmux.server_pid()

        assert [name for _, name in panes] == ["dash", "console"]
        table = procs.table()
        assert server in table and all(table[pid] == server for pid, _ in panes)
    finally:
        tmux.run(["kill-server"])  # the isolated server only
        shutil.rmtree(sockdir, ignore_errors=True)


# -- the freeze itself ------------------------------------------------------------
def _taken(path: Path) -> bool:
    """Is an exclusive lock held on ``path`` by somebody else?"""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def test_both_locks_are_held_while_the_groups_are_written_and_until_they_settle(
        cfg, fake_cgroups):
    fake_cgroups.group("/run/worker-P0")
    queue = cfg.buildsem_dir / "queue.lock"
    seen: list[tuple[str, bool, bool]] = []

    class Watching(freezer.Cgroups):
        def set(self, path, frozen):
            seen.append(("set", _taken(cfg.lock_path), _taken(queue)))
            return super().set(path, frozen)

        def settled(self, path):
            done = super().settled(path)
            if done:
                seen.append(("settled", _taken(cfg.lock_path), _taken(queue)))
            return done

    cg = Watching(fake_cgroups.root, fake_cgroups.proc)
    group = {"path": "/run/worker-P0", "kind": "worker", "id": "P0"}
    gone = {"path": "/run/worker-gone", "kind": "worker", "id": "gone"}

    frozen, stuck, left = freezer.freeze(cfg, [group, gone], cg)

    assert (frozen, stuck, left) == ([group], [], [])  # the one that ended meanwhile is left out
    assert seen[0] == ("set", True, True) and seen[-1] == ("settled", True, True)
    assert not _taken(cfg.lock_path) and not _taken(queue)
    assert fake_cgroups.told("/run/worker-P0") == "1"


def test_a_lock_that_stays_taken_freezes_nothing(cfg, fake_cgroups):
    fake_cgroups.group("/run/worker-P0")
    cfg.ensure_dirs()
    with cfg.lock_path.open("w") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(freezer.Busy, match="the state lock"):
            freezer.freeze(cfg, [{"path": "/run/worker-P0", "kind": "worker", "id": "P0"}],
                           lock_s=0.2)
    assert fake_cgroups.told("/run/worker-P0") == "0"


def test_freeze_and_thaw_with_no_supervisor(cfg, fake_cgroups, stand_in, capsys):
    stand_in("worker:P0")
    stand_in("overseer:ovs-1")
    fake_cgroups.settle()

    assert cli.cmd_freeze(cfg, as_json=True, wait_s=30) == 0  # no supervisor: no wait

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "frozen": [{"path": "/run/overseer-ovs-1", "kind": "overseer", "id": "ovs-1"},
                   {"path": "/run/worker-P0", "kind": "worker", "id": "P0"}],
        "awake": [{"path": "/login", "kind": "caller", "shared": False}],
        "left": [],
        "quiesced": True,
    }
    assert fake_cgroups.told("/run/worker-P0") == fake_cgroups.told("/run/overseer-ovs-1") == "1"
    assert fake_cgroups.told("/login") == "0"
    st = state_mod.read(cfg)
    assert st.frozen["stage"] == freezer.FROZEN and st.on_hold
    assert st.frozen["cgroups"] == report["frozen"]
    since = st.frozen["since"]

    assert cli.cmd_status(cfg) == 0
    assert "Frozen since " in capsys.readouterr().out
    assert cli.cmd_status(cfg, as_json=True) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["frozen"]["stage"] == "frozen" and "2 groups frozen" in status["frozen_line"]

    assert cli.cmd_thaw(cfg, gap_s=0) == 0
    assert "2 group(s) woken" in capsys.readouterr().out
    assert fake_cgroups.told("/run/worker-P0") == fake_cgroups.told("/run/overseer-ovs-1") == "0"
    st = state_mod.read(cfg)
    assert st.frozen == {} and not st.on_hold
    assert "frozen" not in json.loads(cfg.state_path.read_text())
    (span,) = _spans(cfg)
    assert span["since"] == since and span["until"] >= since
    assert cli.cmd_thaw(cfg) == 0  # nothing to do, and no second span
    assert len(_spans(cfg)) == 1


def test_a_group_that_never_settles_takes_the_whole_freeze_back(
        cfg, fake_cgroups, stand_in, monkeypatch, capsys):
    stand_in("worker:P0")
    stand_in("worker:P1")
    fake_cgroups.settle()
    fake_cgroups.stuck.add("/run/worker-P1")
    monkeypatch.setattr(freezer, "SETTLE_S", 0.4)

    assert cli.cmd_freeze(cfg) == 1

    assert "would not freeze: /run/worker-P1" in capsys.readouterr().err
    assert fake_cgroups.told("/run/worker-P0") == fake_cgroups.told("/run/worker-P1") == "0"
    assert state_mod.read(cfg).frozen == {}
    assert _spans(cfg) == []  # nothing stood still: no span
    assert "FREEZE-ROLLBACK" in cfg.supervisor_log.read_text()


@pytest.mark.skipif(os.geteuid() == 0, reason="root may write any group")
def test_a_group_that_is_not_ours_to_write_is_left_alone_and_the_rest_freeze(
        cfg, fake_cgroups, stand_in, capsys):
    stand_in("worker:P0")
    stand_in("worker:P1")
    fake_cgroups.settle()
    fake_cgroups.lock("/run/worker-P1")  # a scope root started
    theirs = {"path": "/run/worker-P1", "kind": "worker", "id": "P1"}

    assert cli.cmd_freeze(cfg, as_json=True) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["frozen"] == [{"path": "/run/worker-P0", "kind": "worker", "id": "P0"}]
    assert report["left"] == [theirs]
    assert fake_cgroups.told("/run/worker-P0") == "1"
    assert fake_cgroups.told("/run/worker-P1") == "0"
    record = state_mod.read(cfg).frozen
    assert record["stage"] == freezer.FROZEN and record["left"] == [theirs]
    assert "FREEZE-ROLLBACK" not in cfg.supervisor_log.read_text()
    assert "(1 left alone" in freezer.line(record)

    # Whoever may write it froze it; the thaw wakes only what this freeze stopped.
    freeze_file = fake_cgroups.root / "run/worker-P1/cgroup.freeze"
    freeze_file.chmod(0o644)
    freeze_file.write_text("1\n")
    freeze_file.chmod(0o444)

    assert cli.cmd_thaw(cfg, gap_s=0) == 0

    assert "1 group(s) woken" in capsys.readouterr().out
    assert fake_cgroups.told("/run/worker-P0") == "0"
    assert fake_cgroups.told("/run/worker-P1") == "1"
    assert state_mod.read(cfg).frozen == {}


@pytest.mark.skipif(os.geteuid() == 0, reason="root may write any group")
def test_the_plain_report_names_a_group_left_alone(cfg, fake_cgroups, stand_in, capsys):
    stand_in("worker:P0")
    stand_in("operator:op-1")
    fake_cgroups.settle()
    fake_cgroups.lock("/run/operator-op-1")

    assert cli.cmd_freeze(cfg) == 0

    out = capsys.readouterr().out
    assert "1 group(s) stopped in place" in out
    assert "left alone, not this user's to freeze: /run/operator-op-1 (operator op-1)" in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root may write any group")
def test_a_group_left_alone_does_not_save_one_that_never_settles(
        cfg, fake_cgroups, stand_in, monkeypatch, capsys):
    stand_in("worker:P0")
    stand_in("worker:P1")
    fake_cgroups.settle()
    fake_cgroups.lock("/run/worker-P0")
    fake_cgroups.stuck.add("/run/worker-P1")  # told to freeze, never confirms
    monkeypatch.setattr(freezer, "SETTLE_S", 0.4)

    assert cli.cmd_freeze(cfg) == 1

    assert "would not freeze: /run/worker-P1" in capsys.readouterr().err
    assert fake_cgroups.told("/run/worker-P1") == "0"
    assert state_mod.read(cfg).frozen == {}


def test_a_second_freeze_stops_what_has_turned_up_and_keeps_the_first_moment(
        cfg, fake_cgroups, stand_in, capsys):
    stand_in("worker:P0")
    fake_cgroups.settle()
    assert cli.cmd_freeze(cfg) == 0
    since = state_mod.read(cfg).frozen["since"]
    fake_cgroups.root.joinpath("run/worker-P0/cgroup.freeze").write_text("0\n")  # woken by hand
    stand_in("worker:P1")
    fake_cgroups.settle()
    capsys.readouterr()

    assert cli.cmd_freeze(cfg, as_json=True) == 0

    report = json.loads(capsys.readouterr().out)
    assert [g["id"] for g in report["frozen"]] == ["P0", "P1"]
    assert fake_cgroups.told("/run/worker-P0") == fake_cgroups.told("/run/worker-P1") == "1"
    assert state_mod.read(cfg).frozen["since"] == since


# -- the thaw ---------------------------------------------------------------------
def test_the_thaw_wakes_in_the_stated_order_and_waits_after_each_claude_session(fake_cgroups):
    def group(kind: str, ident: str = "") -> dict:
        path = f"/g/{kind}-{ident}"
        fake_cgroups.group(path)
        return {"path": path, "kind": kind, "id": ident}

    st = State.fresh(3)
    st.claim_slot("P0")
    st.claim_slot("P1")
    st.claim_slot("P2")
    st.waiting["P2"] = time.time() + 60  # asked the owner: woken before the others
    st.parked = ["P9"]  # parked and still asking
    record = {"cgroups": [
        group("other"), group("worker", "P1"), group("bigpic", "bp-1"), group("worker", "P0"),
        group("resolver", "P5"), group("overseer", "ovs-1"), group("worker", "P9"),
        group("operator", "op-1"), group("worker", "P2"), group("dashboard"), group("console"),
    ]}

    order = freezer.thaw_order(record, st)

    assert [(g["kind"], g["id"]) for g in order] == [
        ("console", ""), ("dashboard", ""), ("worker", "P2"), ("worker", "P9"),
        ("operator", "op-1"), ("overseer", "ovs-1"), ("resolver", "P5"),
        ("worker", "P0"), ("worker", "P1"), ("other", ""), ("bigpic", "bp-1"),
    ]
    naps: list[tuple[str, float]] = []
    woken: list[str] = []
    cg = freezer.Cgroups.from_env()

    class Recording(freezer.Cgroups):
        def set(self, path, frozen):
            woken.append(path)
            return super().set(path, frozen)

    cg = Recording(cg.root, cg.proc)
    freezer.thaw(order, cg, gap_s=5.0, sleep=lambda s: naps.append((woken[-1], s)))

    assert woken == [g["path"] for g in order]
    # A gap after every Claude session; none after the dashboard, a group that
    # is no session, or the last one.
    assert naps == [(g["path"], 5.0) for g in order[:-1] if g["kind"] not in ("dashboard", "other")]


def test_swarm_thaw_honours_the_gap(cfg, fake_cgroups, stand_in):
    stand_in("worker:P0")
    stand_in("worker:P1")
    fake_cgroups.settle()
    assert cli.cmd_freeze(cfg) == 0
    fake_cgroups.writes.clear()

    assert cli.cmd_thaw(cfg, gap_s=0.5) == 0

    time.sleep(0.2)  # the kernel sees the last write
    first, second = [w for w in fake_cgroups.writes if w[2] == "0"]
    assert second[0] - first[0] >= 0.4


def test_the_thaw_says_it_has_begun_then_wakes_with_no_lock_a_session_can_hold(
        cfg, fake_cgroups, stand_in, monkeypatch):
    stand_in("worker:P0")
    stand_in("worker:P1")
    fake_cgroups.settle()
    assert cli.cmd_freeze(cfg) == 0
    queue = cfg.buildsem_dir / "queue.lock"
    seen: list[tuple[str, bool, bool, str]] = []
    real = freezer.Cgroups.set

    def watching(self, path, frozen):
        # Looked at while the write is being made, not after the wake-up is over.
        seen.append((path, _taken(cfg.lock_path), _taken(queue), freezer.peek(cfg)["stage"]))
        return real(self, path, frozen)

    monkeypatch.setattr(freezer.Cgroups, "set", watching)

    assert cli.cmd_thaw(cfg, gap_s=0) == 0

    assert seen == [("/run/worker-P0", False, False, freezer.THAWING),
                    ("/run/worker-P1", False, False, freezer.THAWING)]
    assert fake_cgroups.told("/run/worker-P0") == fake_cgroups.told("/run/worker-P1") == "0"


# -- what is refused --------------------------------------------------------------
REFUSED = [
    ["up", "--no-attach"], ["down"], ["restart"], ["_restart-run", "plan-1"], ["reset"],
    ["launch", "P0"], ["retry", "P0"], ["free", "0"], ["skip", "P0"], ["integrate", "P0"],
    ["finish"], ["gc"], ["reload"], ["layout"], ["console"], ["operator", "P0"],
]


@pytest.mark.parametrize("argv", REFUSED, ids=lambda a: a[0])
def test_verbs_that_reshape_the_run_are_refused_while_frozen(cfg, fake_cgroups, argv, capsys):
    fake_cgroups.group("/run/worker-P0").joinpath("cgroup.freeze").write_text("1\n")
    _record(cfg, {"path": "/run/worker-P0", "kind": "worker", "id": "P0"})
    before = cfg.state_path.read_text()

    assert cli.main(["--project-dir", str(cfg.project_dir), *argv]) == 1

    err = capsys.readouterr().err
    assert f"swarm {argv[0]}: the swarm is frozen" in err and "`swarm thaw`" in err
    assert cfg.state_path.read_text() == before


def test_the_refused_list_is_the_one_in_the_design():
    assert {a[0] for a in REFUSED} == cli.FROZEN_REFUSED


def test_reading_verbs_still_answer_while_frozen(cfg, fake_cgroups, capsys):
    fake_cgroups.group("/run/worker-P0").joinpath("cgroup.freeze").write_text("1\n")
    _record(cfg, {"path": "/run/worker-P0", "kind": "worker", "id": "P0"})
    assert cli.main(["--project-dir", str(cfg.project_dir), "status"]) == 0
    assert "Frozen since " in capsys.readouterr().out


def test_a_supervisor_that_predates_the_freeze_is_refused(cfg, fake_cgroups, stand_in, capsys):
    stand_in("worker:P0")
    fake_cgroups.settle()
    os.mkfifo(cfg.fifo_path)
    reader = os.open(cfg.fifo_path, os.O_RDWR | os.O_NONBLOCK)  # a supervisor has it open
    try:
        pid = os.getpid()
        with state_mod.transaction(cfg) as st:
            st.supervisor_pid = pid
        old = [c for c in restart_mod.CAPS if c != "freeze"]
        restart_mod._write_json(cfg.state_dir / restart_mod.MARK_FILE, {
            "pid": pid, "ticks": restart_mod.procs.start_ticks(pid), "caps": old})

        assert cli.cmd_freeze(cfg) == 1

        assert "run `swarm restart` first" in capsys.readouterr().err
        assert state_mod.read(cfg).frozen == {}
        assert fake_cgroups.told("/run/worker-P0") == "0"
    finally:
        os.close(reader)


# -- a record left behind -------------------------------------------------------------
def test_up_drops_a_record_nothing_stands_behind_and_refuses_a_live_one(
        swarm, fake_cgroups):
    since = time.time() - 3600
    stale = {"since": since, "stage": "frozen", "cgroups": [
        {"path": "/run/worker-P0", "kind": "worker", "id": "P0"}], "awake": []}
    swarm.state_dir.mkdir(parents=True, exist_ok=True)
    state_file = swarm.state_dir / "state.json"
    # Still frozen: the groups are there and told to be.
    fake_cgroups.group("/run/worker-P0").joinpath("cgroup.freeze").write_text("1\n")
    state_file.write_text(json.dumps({"frozen": stale}))

    r = swarm.cli("up", check=False)

    assert r.returncode == 1 and "run `swarm thaw` first" in r.stderr
    assert json.loads(state_file.read_text())["frozen"] == stale
    assert not (swarm.state_dir / "control.fifo").exists()

    # After a reboot: the group is back awake (or gone), and nothing is frozen.
    fake_cgroups.group("/run/worker-P0").joinpath("cgroup.freeze").write_text("0\n")

    swarm.up()

    assert "FROZEN-STALE" in swarm.log_text()
    assert "frozen" not in swarm.state()
    (span,) = [json.loads(ln) for ln in
               (swarm.state_dir / "history" / freezer.SPANS).read_text().splitlines()]
    assert span["since"] == since and span["until"] > since
    assert swarm.wait(lambda: swarm.busy_phases() == ["P0"], timeout=20), swarm.log_text()


# -- the supervisor ---------------------------------------------------------------
def test_while_frozen_the_loop_waits_a_fixed_time_whatever_is_overdue(cfg, fake_cgroups):
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
        st.waiting["P0"] = time.time() - 60  # a park deadline long past
    sup = Supervisor(cfg)
    assert sup._select_timeout() == 0.0  # awake: act on it now

    class Sampler:
        stopped = False

        def stop(self):
            self.stopped = True

    sup._resources = sampler = Sampler()
    restarted: list[bool] = []
    sup._start_resources = lambda: restarted.append(True)
    _record(cfg)
    sup._freeze_tick()
    try:
        assert sup._frozen_since is not None
        assert sup._select_timeout() == FROZEN_POLL_S
        assert sampler.stopped and sup._resources is None and not restarted
        with state_mod.transaction(cfg) as st:
            st.frozen = {}
        sup._freeze_tick()
        assert sup._frozen_since is None and restarted == [True]
    finally:
        sup.log.close()


def test_the_supervisor_says_what_it_is_in_the_middle_of_then_that_it_is_quiet(
        cfg, fake_cgroups):
    sup = Supervisor(cfg)
    sup._launching.add("P0")
    _record(cfg, stage=freezer.FREEZING)
    try:
        sup._freeze_tick()
        record = state_mod.read(cfg).frozen
        assert record["waiting"] == ["1 worker starting"] and not record["quiet_at"]

        sup._launching.clear()
        sup._freeze_tick()

        record = state_mod.read(cfg).frozen
        assert record["waiting"] == [] and record["quiet_at"] > 0
        log = cfg.supervisor_log.read_text()
        assert "FREEZE-WAITING 1 worker starting" in log and "FREEZE-QUIET" in log
    finally:
        sup.log.close()


def test_a_launch_the_freeze_catches_is_no_failure_and_starts_again_at_the_thaw(
        cfg, fake_cgroups, monkeypatch, tmp_path):
    def freeze_lands(_cfg, _phase):
        _record(cfg)  # between the claim and the start of the worker
        return None

    monkeypatch.setattr(models_mod, "override", freeze_lands)
    sup = Supervisor(cfg)
    sup._bootstrapped = True
    sup._launching.add("P0")
    try:
        sup._launch_worker("P0")  # the launch thread's body

        assert sup._launch_fails == {} and sup._launching == set()
        st = state_mod.read(cfg)
        assert not st.any_busy() and st.launch_fails == {}
        log = cfg.supervisor_log.read_text()
        assert "LAUNCH-FROZEN P0" in log and "LAUNCH-FAIL" not in log
        assert not (tmp_path / "tg.log").exists()  # nobody is told of a failure

        sup._freeze_tick()
        assert sup._fill_slots("test") == []  # frozen: nothing launches
        sup._handle("launched P0 frozen")
        with state_mod.transaction(cfg) as st:
            st.frozen = {}
        sup._freeze_tick()  # the record is gone: carry on

        assert sup._frozen_since is None
        assert sup.stub_launches[:1] == ["P0"]
    finally:
        if sup._resources is not None:
            sup._resources.stop()
        sup.log.close()


def test_a_launch_is_denied_while_frozen(cfg, fake_cgroups):
    from swarm_orchestrator.logutil import Log

    _record(cfg)
    log = Log(cfg.supervisor_log)
    try:
        assert launch_mod.launch_outcome(cfg, "P0", log, quiet=True) == launch_mod.DENIED
    finally:
        log.close()
    assert "LAUNCH-DENIED P0 frozen" in cfg.supervisor_log.read_text()


def _cpu_ticks(pid: int) -> int:
    rest = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(rest[11]) + int(rest[12])  # utime + stime


def test_a_real_supervisor_stands_still_while_frozen_and_carries_on_at_the_thaw(
        swarm, fake_cgroups):
    swarm.env.update({"FAKE_WORKER_SLEEP": "300", "SWARM_WATCHDOG": "1",
                      "SWARM_PARK_AFTER": "1"})
    up = swarm.up()
    assert "the supervisor shares the cgroup of whatever started it" in up.stderr
    assert swarm.wait(lambda: swarm.busy_phases() == ["P0"], timeout=20), swarm.log_text()
    assert swarm.wait(lambda: swarm.log_text().count("WATCHDOG idle=") >= 2, timeout=10)
    fake_cgroups.settle()
    swarm.cli("waiting", "P0", "which way?")  # arms a park deadline a second away

    r = swarm.cli("freeze", "--json", "--wait", "20")

    report = json.loads(r.stdout)
    assert report["frozen"] == [{"path": "/run/worker-P0", "kind": "worker", "id": "P0"}]
    assert report["quiesced"] is True
    assert {(g["path"], g["kind"]) for g in report["awake"]} == {
        ("/supervisor", "supervisor"), ("/login", "caller")}
    assert fake_cgroups.told("/run/worker-P0") == "1"
    assert fake_cgroups.told("/supervisor") == fake_cgroups.told("/login") == "0"
    assert swarm.state()["frozen"]["stage"] == "frozen"
    assert swarm.wait(lambda: "FREEZE groups=" in swarm.log_text(), timeout=5)

    # Frozen: its worker reports done, its park deadline passes, sweeps come due.
    pid = swarm.state()["supervisor_pid"]
    mark, ticks = len(swarm.log_text()), _cpu_ticks(pid)
    swarm.cli("done", "P0", "ok")
    told = swarm.tg_lines()
    time.sleep(3.5)

    during = swarm.log_text()[mark:]
    for word in ("WATCHDOG", "PARK", "LAUNCH", "CLAIM", "EVENT", "REAP", "RUN-ENDED"):
        assert word not in during, during
    st = swarm.state()
    assert swarm.busy_phases() == ["P0"] and "P0" in st["waiting"] and not st["parked"]
    assert swarm.tg_lines() == told  # and nobody is pinged
    # About four wake-ups in that time, not a loop spinning on a past deadline.
    assert _cpu_ticks(pid) - ticks < 0.5 * 3.5 * os.sysconf("SC_CLK_TCK")
    assert swarm.cli("launch", "P1", check=False).returncode == 1

    mark = len(swarm.log_text())
    swarm.cli("thaw", "--gap", "0")

    assert fake_cgroups.told("/run/worker-P0") == "0"
    assert swarm.wait(lambda: set(swarm.busy_phases()) == {"P1", "P2", "P3"}, timeout=20), (
        swarm.log_text())
    # A slot is claimed before its worker is started: wait for the start itself.
    assert swarm.wait(lambda: "LAUNCH P1" in swarm.log_text()[mark:], timeout=20)
    after = swarm.log_text()[mark:]
    assert after.index("THAWED frozen=") < after.index("EVENT done P0 ok") < after.index("LAUNCH P1")
    assert "deferred=1" in after
    st = swarm.state()
    assert "frozen" not in st and "P0" not in st["waiting"] and not st["parked"]
    assert swarm.wait(lambda: swarm.log_text()[mark:].count("WATCHDOG idle=") >= 1, timeout=10)
