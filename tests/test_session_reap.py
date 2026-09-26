"""Everything a session starts dies with the session; ``swarm keep`` is the exception.

A session that leaves a process behind (say a detached ``setsid nohup python -m http.server … &``)
is a leak, so the rule is: shells do not outlive their worker unless deliberately kept.
So a session's end (``swarm done``,
``operator-done``, an Overseer pass, a resolver) ends every process carrying its
markers — a detached one included — and ``swarm keep`` is the one sanctioned way
to leave something running, listed everywhere until ``swarm keep --stop``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarm_orchestrator import cli
from swarm_orchestrator import doctor as doctor_mod
from swarm_orchestrator import keep as keep_mod
from swarm_orchestrator import operator as operator_mod
from swarm_orchestrator import opqueue, procs
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor

#: A session stand-in: it detaches a child the way the operator did (its own
#: session, reparented once the shell that started it is gone), records the
#: child's pid, and stays alive itself.
DETACH = "setsid sleep 300 </dev/null >/dev/null 2>&1 & echo $! > {pidfile}; exec sleep 300"


def _wait(pred, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _kill(*pids: int) -> None:
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".swarm.toml").write_text(
        '[operator]\nenabled = true\n[swarm]\ndriver = "bare"\n', encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_BIN", "true")
    for leak in ("SWARM_DRIVER", "SWARM_OPERATOR", "SWARM_PHASE", procs.SESSION_ENV):
        monkeypatch.delenv(leak, raising=False)
    monkeypatch.setattr(session_mod, "REAP_GRACE_S", 0.0)
    monkeypatch.setattr(session_mod, "END_WAIT_S", 0.5)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


def _session(cfg, tmp_path, name: str, marker: dict[str, str]) -> tuple[subprocess.Popen, int]:
    """A stand-in session carrying ``marker``, and the pid of the child it detached."""
    pidfile = tmp_path / f"{name}.pid"
    env = {**os.environ, "SWARM_STATE_DIR": str(cfg.state_dir), **marker}
    proc = subprocess.Popen(["bash", "-c", DETACH.format(pidfile=pidfile)], env=env,
                            start_new_session=True)
    assert _wait(lambda: pidfile.is_file() and pidfile.read_text().strip())
    return proc, int(pidfile.read_text())


def test_a_worker_session_ends_with_everything_it_started_detached_or_not(cfg, tmp_path):
    worker, detached = _session(cfg, tmp_path, "w", {procs.SESSION_ENV: "worker:P1",
                                                     "SWARM_PHASE": "P1"})
    other, other_child = _session(cfg, tmp_path, "o", {procs.SESSION_ENV: "worker:P2",
                                                       "SWARM_PHASE": "P2"})
    try:
        assert os.getsid(detached) != os.getsid(worker.pid)  # it really left
        session_mod.reap_session(cfg, "worker", "P1", background=False)

        assert _wait(lambda: not procs.alive(detached) and not procs.alive(worker.pid))
        assert procs.alive(other.pid) and procs.alive(other_child)  # another session's
    finally:
        _kill(worker.pid, detached, other.pid, other_child)
        for p in (worker, other):
            p.wait(timeout=5)


def test_a_phase_marker_alone_is_enough_for_a_worker(cfg, tmp_path):
    """``SWARM_PHASE`` is the worker's marker too: a session from before the
    ``SWARM_SESSION_ID`` marker existed is still ended."""
    worker, detached = _session(cfg, tmp_path, "w", {"SWARM_PHASE": "P1"})
    try:
        session_mod.reap_session(cfg, "worker", "P1", background=False)
        assert _wait(lambda: not procs.alive(detached) and not procs.alive(worker.pid))
    finally:
        _kill(worker.pid, detached)
        worker.wait(timeout=5)


def test_another_runs_session_of_the_same_name_is_never_touched(cfg, tmp_path):
    env = {**os.environ, "SWARM_STATE_DIR": str(tmp_path / "another-run"),
           procs.SESSION_ENV: "worker:P1", "SWARM_PHASE": "P1"}
    stranger = subprocess.Popen(["sleep", "300"], env=env)
    try:
        session_mod.reap_session(cfg, "worker", "P1", background=False)
        time.sleep(0.3)
        assert procs.alive(stranger.pid)
    finally:
        stranger.kill()
        stranger.wait(timeout=5)


def test_an_operator_jobs_detached_child_dies_on_operator_done(cfg, tmp_path):
    item = opqueue.add(cfg, "J1", status="operator", note="serve the mockups to the owner")
    assert item is not None
    log = Log(cfg.supervisor_log)
    session, detached = _session(cfg, tmp_path, "op", {procs.SESSION_ENV: "operator:J1"})
    try:
        assert operator_mod.dispatch(cfg, "J1", log) is True
        done = subprocess.run(
            [sys.executable, "-m", "swarm_orchestrator", "--project-dir", str(cfg.project_dir),
             "operator-done", "J1", "served them"],
            cwd=str(cfg.project_dir), capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
        Supervisor(cfg)._on_operator_done("J1")  # what the poke runs

        assert _wait(lambda: not procs.alive(detached) and not procs.alive(session.pid))
        assert _wait(lambda: "REAP operator:J1" in Path(cfg.supervisor_log).read_text())
    finally:
        log.close()
        _kill(session.pid, detached)
        session.wait(timeout=5)


def test_a_kept_process_survives_its_session_and_down_until_stopped(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv(procs.SESSION_ENV, "worker:P1")
    monkeypatch.setenv("SWARM_PHASE", "P1")
    rec = keep_mod.start(cfg, "mockups", ["sleep", "300"], "serves the mockups for the owner")
    try:
        assert rec.alive and rec.by == "worker:P1"
        env = procs.environ(rec.pid)
        assert not any(e.startswith((b"SWARM_STATE_DIR=", b"SWARM_PHASE=",
                                     procs.SESSION_ENV.encode() + b"=")) for e in env)
        session_mod.reap_session(cfg, "worker", "P1", background=False)
        found = session_mod.session_processes(cfg)  # what `down` would end
        assert rec.pid not in found
        session_mod.end_processes(cfg, found, wait=0.3)
        assert procs.alive(rec.pid)

        assert cli.cmd_keep(cfg, None, None, [], stop="mockups") == 0
        assert _wait(lambda: not procs.alive(rec.pid))
        assert keep_mod.get(cfg, "mockups") is None
    finally:
        _kill(rec.pid)


def test_a_kept_pid_is_spared_even_if_it_carries_the_markers(cfg, tmp_path):
    """The belt: a record's pid + start time excludes it from every sweep."""
    env = {**os.environ, "SWARM_STATE_DIR": str(cfg.state_dir), "SWARM_PHASE": "P1"}
    proc = subprocess.Popen(["sleep", "300"], env=env, start_new_session=True)
    try:
        keep_mod.keep_dir(cfg).mkdir(parents=True, exist_ok=True)
        rec = keep_mod.Kept(name="belt", pid=proc.pid, start_ticks=procs.start_ticks(proc.pid),
                            started_at=time.time(), argv=["sleep", "300"], cwd="/", by="owner",
                            why="a test", log="/dev/null")
        keep_mod._path(cfg, "belt").write_text(json.dumps(rec.to_json()))
        assert proc.pid not in session_mod.session_processes(cfg)
        session_mod.reap_session(cfg, "worker", "P1", background=False)
        assert procs.alive(proc.pid)
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_keep_requires_a_one_line_why(cfg, capsys):
    assert cli.cmd_keep(cfg, "x", None, ["--", "sleep", "300"]) == 1
    assert "--why is required" in capsys.readouterr().err
    assert cli.cmd_keep(cfg, "x", "a" * (keep_mod.WHY_MAX + 1), ["sleep", "300"]) == 1
    assert "one line" in capsys.readouterr().err
    assert keep_mod.load_all(cfg) == []


def test_a_live_name_is_refused_and_a_dead_one_replaced(cfg, capsys):
    first = keep_mod.start(cfg, "srv", ["sleep", "300"], "first")
    try:
        assert cli.cmd_keep(cfg, "srv", "second", ["--", "sleep", "300"]) == 1
        assert "already running" in capsys.readouterr().err
        _kill(first.pid)
        assert _wait(lambda: not keep_mod.get(cfg, "srv").alive)
        assert cli.cmd_keep(cfg, "srv", "second", ["--", "sleep", "300"]) == 0
        second = keep_mod.get(cfg, "srv")
        assert second.alive and second.pid != first.pid and second.why == "second"
    finally:
        _kill(first.pid)
        rec = keep_mod.get(cfg, "srv")
        if rec:
            _kill(rec.pid)


def test_list_status_and_doctor_report_kept_processes(cfg, capsys):
    rec = keep_mod.start(cfg, "srv", ["sleep", "300"], "serves the look mockups")
    try:
        assert cli.cmd_keep(cfg, None, None, [], listing=True) == 0
        out = capsys.readouterr().out
        assert "srv: alive pid" in out and "serves the look mockups" in out
        assert "swarm keep --stop srv" in out
        assert cli.cmd_status(cfg) == 0
        assert "kept: srv: alive" in capsys.readouterr().out

        check = doctor_mod._check_kept(cfg)
        assert check.status == doctor_mod.OK and "serves the look mockups" in check.detail
        old = doctor_mod._check_kept(cfg, now=time.time() + keep_mod.STALE_S + 60)
        assert old.status == doctor_mod.WARN and "swarm keep --stop srv" in old.fix_hint
        assert "keep" in {c.name for c in doctor_mod.run_checks(cfg)}

        _kill(rec.pid)
        assert _wait(lambda: not keep_mod.get(cfg, "srv").alive)
        assert cli.cmd_keep(cfg, None, None, [], listing=True) == 0
        assert "srv: dead" in capsys.readouterr().out
        assert doctor_mod._check_kept(cfg).status == doctor_mod.OK  # dead is not a warning
    finally:
        _kill(rec.pid)


def test_a_command_that_exits_at_once_is_reported(cfg, capsys):
    assert cli.cmd_keep(cfg, "bad", "fails at once", ["--", "false"]) == 1
    assert "exited at once" in capsys.readouterr().err


def test_a_workers_detached_child_dies_with_its_done_end_to_end(swarm, tmp_path):
    """Through the real supervisor: every fake worker detaches a child and keeps a
    process; each child dies when its worker's `swarm done` lands, and the kept
    processes survive the run's end and `swarm down`."""
    prefix = tmp_path / "detached"
    swarm.env.update({"FAKE_WORKER_DETACH": str(prefix), "FAKE_WORKER_KEEP": "1"})
    kept: list[int] = []
    try:
        swarm.up()
        assert swarm.wait(swarm.finished, timeout=90), swarm.log_text()
        pids = [int(p.read_text()) for p in tmp_path.glob("detached.*")]
        assert len(pids) == 5
        assert _wait(lambda: not any(procs.alive(p) for p in pids)), swarm.log_text()
        assert _wait(lambda: "REAP worker:P4" in swarm.log_text()), swarm.log_text()

        listing = swarm.cli("keep", "--list", "--json")
        recs = json.loads(listing.stdout)
        kept = [r["pid"] for r in recs]
        assert {r["name"] for r in recs} == {f"keep-P{i}" for i in range(5)}
        assert all(r["alive"] for r in recs) and all(r["by"].startswith("worker:") for r in recs)

        swarm.down()
        assert all(procs.alive(p) for p in kept)

        stop = swarm.cli("keep", "--stop", "keep-P0")
        assert "stopped keep-P0" in stop.stdout
        assert _wait(lambda: not procs.alive(recs[0]["pid"]))
    finally:
        _kill(*kept)


def test_swarm_dones_own_detached_helpers_do_not_carry_the_workers_markers(cfg, monkeypatch):
    """The grace poke, the recap and the operator triage are started by
    `swarm done` from inside the worker, and must outlive the worker's end."""
    from swarm_orchestrator import launch as launch_mod

    monkeypatch.setenv(procs.SESSION_ENV, "worker:P1")
    monkeypatch.setenv("SWARM_PHASE", "P1")
    env = launch_mod.detached_env(cfg)
    assert procs.SESSION_ENV not in env and "SWARM_PHASE" not in env
    assert env["SWARM_STATE_DIR"] == os.environ["SWARM_STATE_DIR"]
