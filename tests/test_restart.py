"""``swarm restart``: the supervisor is replaced and nothing else is; a full
restart never drops a session that waits on the owner unless told to.

End to end with the real detached supervisor and the fake worker/master, as the
rest of the suite. The tmux cases run on a server of their own (its own
``TMUX_TMPDIR``, killed after), never the default one.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from swarm_orchestrator import drain as drain_mod
from swarm_orchestrator import restart as restart_mod
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import tmux
from swarm_orchestrator.config import load
from swarm_orchestrator.state import State
from swarm_orchestrator.supervisor import Supervisor

LEDGER = "- [ ] `P0` · needs:—\n- [ ] `P1` · needs:—\n- [ ] `P2` · needs:—\n"


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2:][:1] not in ("Z", "X")


def _plan(swarm) -> dict:
    path = swarm.state_dir / restart_mod.PLAN_FILE
    return json.loads(path.read_text()) if path.is_file() else {}


def _worker_pids(swarm, phase: str) -> set[int]:
    """Live processes that carry ``phase``'s worker marker in this run."""
    want = {f"SWARM_STATE_DIR={swarm.state_dir}".encode(), f"SWARM_PHASE={phase}".encode()}
    out = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            env = set((entry / "environ").read_bytes().split(b"\0"))
        except OSError:
            continue
        if want <= env and _alive(int(entry.name)):
            out.add(int(entry.name))
    return out


def _flat_ledger(swarm, n: int) -> list[str]:
    """``n`` phases with no dependencies, so every slot fills at once."""
    names = [f"W{i}" for i in range(n)]
    (swarm.project / "ledger.txt").write_text("\n".join(names) + "\n", encoding="utf-8")
    return names


def _edit_state(swarm, body: str) -> None:
    """Run ``body`` on the run's state (``st``) under its lock, in its environment."""
    code = ("import sys, time\n"
            "from swarm_orchestrator import state as s\n"
            "from swarm_orchestrator.config import load\n"
            "c = load(project_dir=sys.argv[1])\n"
            "with s.transaction(c) as st:\n"
            f"    {body}\n")
    subprocess.run([sys.executable, "-c", code, str(swarm.project)], env=swarm.env,
                   check=True, capture_output=True)


def _launches(swarm, phase: str) -> int:
    return sum(1 for ln in swarm.log_text().splitlines() if f" LAUNCH {phase} " in ln)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    cfg = load(project_dir=str(tmp_path))
    state_mod.init_state(cfg)
    return cfg


# -- in place, end to end ----------------------------------------------------
def test_restart_replaces_the_supervisor_and_nothing_else(swarm):
    """Running workers, a parked session and a waiting question all come through;
    the new supervisor adopts the slots, launches nothing twice, and the run
    finishes."""
    _flat_ledger(swarm, 6)
    swarm.env["FAKE_WORKER_SLEEP"] = "300"   # the workers stay; the test says done
    swarm.up()
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 4, timeout=30), swarm.log_text()
    old = swarm.state()["supervisor_pid"]
    parked, asker = swarm.busy_phases()[:2]
    # One asks and its park deadline passes; the other asks and is still waiting.
    swarm.cli("waiting", parked, "which way: left or right?")
    assert swarm.wait(lambda: parked in swarm.state()["waiting"], timeout=10)
    _edit_state(swarm, f"st.waiting[{parked!r}] = 1.0")
    swarm.cli("waiting", asker, "may I delete the old table?")
    assert swarm.wait(lambda: swarm.state()["parked"] == [parked], timeout=15), swarm.log_text()
    assert swarm.wait(lambda: asker in swarm.state()["waiting"], timeout=10)
    # Parking freed a slot, and the next ready phase took it.
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 4, timeout=30), swarm.log_text()
    workers = {p: _worker_pids(swarm, p) for p in [parked, *swarm.busy_phases()]}
    assert all(workers.values()), workers
    deadline = swarm.state()["waiting"][asker]
    slots = {s["id"]: (s["phase"], s["pane_id"]) for s in swarm.state()["slots"]}

    r = swarm.cli("restart", timeout=120)

    assert "restarted: supervisor pid" in r.stdout, r.stdout + r.stderr
    st = swarm.state()
    new = st["supervisor_pid"]
    assert new != old and _alive(new) and not _alive(old)
    assert st["parked"] == [parked]
    assert st["waiting"] == {asker: deadline}     # the park timer is the same moment
    assert {s["id"]: (s["phase"], s["pane_id"]) for s in st["slots"]} == slots
    assert {p: _worker_pids(swarm, p) for p in workers} == workers  # the same processes
    log = swarm.log_text()
    assert "SUPERVISOR-STOP handover" in log and "adopting" in log and "ADOPT busy=" in log
    assert all(_launches(swarm, p) == 1 for p in workers)
    assert _plan(swarm)["stage"] == restart_mod.DONE
    assert not swarm.tg_lines() or not any("did not restart" in ln for ln in swarm.tg_lines())

    # The adopted run carries on: every done is handled, the next phases launch.
    for phase in workers:
        swarm.cli("done", phase, "ok")
    swarm.env["FAKE_WORKER_SLEEP"] = "0"
    assert swarm.wait(lambda: set(workers) <= set(swarm.state()["done"]), timeout=30), swarm.log_text()
    assert swarm.state()["parked"] == [] and swarm.state()["waiting"] == {}
    assert all(_launches(swarm, p) == 1 for p in workers)


def _poke(swarm, line: str) -> None:
    fd = os.open(swarm.state_dir / "control.fifo", os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)


def test_a_restart_asked_from_a_session_says_who_asked(swarm):
    _flat_ledger(swarm, 2)
    swarm.env["FAKE_WORKER_SLEEP"] = "300"
    swarm.up()
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 2, timeout=30), swarm.log_text()
    worker = {**swarm.env, "SWARM_SESSION_ID": "worker:W1", "SWARM_PHASE": "W1"}
    r = subprocess.run(
        [sys.executable, "-m", "swarm_orchestrator", "--project-dir", str(swarm.project),
         "restart", "--at", "03:00"],
        cwd=str(swarm.project), env=worker, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "restart planned at" in r.stdout and "asked by the worker on W1" in r.stdout
    assert "2 workers and 0 questions carry on untouched" in r.stdout
    plan = _plan(swarm)
    assert plan["by"] == "the worker on W1" and plan["stage"] == restart_mod.PLANNED
    assert plan["timer"] == "supervisor"  # this supervisor fires it itself
    status = swarm.cli("status").stdout
    assert "restart planned at" in status and "asked by the worker on W1" in status
    assert json.loads(swarm.cli("status", "--json").stdout)["restart"]["by"] == "the worker on W1"
    assert "by='the worker on W1'" in swarm.log_text()
    doctor = swarm.cli("doctor", check=False).stdout
    assert "restart planned at" in doctor and "asked by the worker on W1" in doctor

    # Cancelled from the owner's terminal: nothing happens at 03:00.
    r = swarm.cli("restart", "--cancel")
    assert "cancelled" in r.stdout
    assert _plan(swarm)["stage"] == restart_mod.CANCELLED
    assert "restart planned" not in swarm.cli("status").stdout
    assert "RESTART-CANCELLED" in swarm.log_text() and "by='owner terminal'" in swarm.log_text()
    assert swarm.cli("restart", "--cancel").stdout.strip() == "no restart to cancel"


def test_a_scheduled_restart_fires_at_its_time(swarm):
    _flat_ledger(swarm, 2)
    swarm.env["FAKE_WORKER_SLEEP"] = "300"
    swarm.up()
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 2, timeout=30), swarm.log_text()
    old = swarm.state()["supervisor_pid"]
    workers = {p: _worker_pids(swarm, p) for p in swarm.busy_phases()}
    swarm.cli("restart", "--in", "1h")
    time.sleep(1.0)
    assert swarm.state()["supervisor_pid"] == old and "RESTART-DUE" not in swarm.log_text()

    # The moment comes (the plan is moved up rather than an hour waited out).
    plan = _plan(swarm)
    plan["at"] = time.time() + 1.5
    (swarm.state_dir / restart_mod.PLAN_FILE).write_text(json.dumps(plan))
    _poke(swarm, "restart-scheduled")
    time.sleep(0.5)
    assert swarm.state()["supervisor_pid"] == old  # not before its time

    assert swarm.wait(lambda: _plan(swarm).get("stage") == restart_mod.DONE, timeout=30), (
        swarm.log_text() + (swarm.state_dir / "logs" / restart_mod.RUN_LOG).read_text())
    new = swarm.state()["supervisor_pid"]
    assert new != old and _alive(new) and not _alive(old)
    assert "RESTART-DUE" in swarm.log_text() and "RESTART-DONE" in swarm.log_text()
    assert {p: _worker_pids(swarm, p) for p in workers} == workers
    assert all(_launches(swarm, p) == 1 for p in workers)


def test_a_supervisor_that_predates_the_command_is_restarted_in_place(swarm):
    """The very first use: the running supervisor knows no ``handover``. It is
    stopped with ``shutdown`` once that would end nothing, and its successor
    adopts the run."""
    _flat_ledger(swarm, 3)
    swarm.env["FAKE_WORKER_SLEEP"] = "300"
    swarm.up()
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 3, timeout=30), swarm.log_text()
    old = swarm.state()["supervisor_pid"]
    swarm.cli("waiting", "W0", "keep the old name?")
    assert swarm.wait(lambda: "W0" in swarm.state()["waiting"], timeout=10)
    workers = {p: _worker_pids(swarm, p) for p in swarm.busy_phases()}
    (swarm.state_dir / restart_mod.MARK_FILE).unlink()  # an older supervisor writes none

    r = swarm.cli("restart", timeout=120)

    assert "restarted: supervisor pid" in r.stdout, r.stdout + r.stderr
    st = swarm.state()
    assert st["supervisor_pid"] != old and _alive(st["supervisor_pid"]) and not _alive(old)
    assert list(st["waiting"]) == ["W0"] and not st["paused"]
    assert {p: _worker_pids(swarm, p) for p in workers} == workers
    log = swarm.log_text()
    assert "RESTART-LEGACY" in log and "SUPERVISOR-STOP\n" in log and "adopting" in log
    assert "handover" not in log.split("RESTART-LEGACY")[1].split("adopting")[0]
    assert all(_launches(swarm, p) == 1 for p in workers)


def test_restart_picks_up_a_swarm_whose_supervisor_died_and_reads_the_done_it_missed(swarm):
    _flat_ledger(swarm, 6)
    swarm.env["FAKE_WORKER_SLEEP"] = "300"
    swarm.up()
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 4, timeout=30), swarm.log_text()
    old = swarm.state()["supervisor_pid"]
    os.kill(old, signal.SIGKILL)
    assert swarm.wait(lambda: not _alive(old), timeout=10)
    # A worker finishes while nothing reads the FIFO: only its sentinel is left.
    r = swarm.cli("done", "W2", "ok")
    assert (swarm.state_dir / "done" / "W2.ok").is_file()
    assert "W2" not in swarm.state()["done"]

    r = swarm.cli("restart", timeout=120)

    assert "restarted: supervisor pid none ->" in r.stdout, r.stdout + r.stderr
    assert swarm.wait(lambda: swarm.state()["done"].get("W2") == "ok", timeout=20), swarm.log_text()
    assert "ADOPT-DONE W2 ok" in swarm.log_text()
    # Its slot went to the next ready phase, and nothing running was started again.
    assert swarm.wait(lambda: "W4" in swarm.busy_phases(), timeout=20), swarm.log_text()
    assert all(_launches(swarm, p) == 1 for p in ("W0", "W1", "W2", "W3", "W4"))


def test_a_restart_applies_a_settings_edit_the_way_reload_does(swarm):
    """The run is the same run, so the new supervisor moves from the old
    settings to the file's as ``swarm reload`` would: one more worker is one
    more slot, filled at once."""
    _flat_ledger(swarm, 6)
    swarm.env["FAKE_WORKER_SLEEP"] = "300"
    swarm.up()
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 4, timeout=30), swarm.log_text()
    toml = swarm.project / ".swarm.toml"
    toml.write_text(toml.read_text().replace("max_workers = 4", "max_workers = 5"))

    swarm.cli("restart", timeout=120)

    assert swarm.wait(lambda: len(swarm.busy_phases()) == 5, timeout=30), swarm.log_text()
    assert len(swarm.state()["slots"]) == 5
    log = swarm.log_text()
    assert "RELOAD-RESIZE 4->5" in log and "RELOAD max_workers" in log
    assert all(_launches(swarm, f"W{i}") == 1 for i in range(5))
    # The snapshot is what runs now, so a later `swarm reload` has nothing to do.
    assert "max_workers" not in swarm.cli("reload", "--dry-run").stdout


def _cfg_of(swarm, monkeypatch):
    """The run's config, loaded in this process (its environment made the run's)."""
    for key in ("SWARM_STATE_DIR", "SWARM_TG_SINK", "SWARM_SLUG", "SWARM_BIN"):
        monkeypatch.setenv(key, swarm.env[key])
    return load(project_dir=str(swarm.project))


def test_a_restart_that_does_not_come_back_tells_the_owner(swarm, monkeypatch):
    _flat_ledger(swarm, 2)
    swarm.env["FAKE_WORKER_SLEEP"] = "300"
    swarm.up()
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 2, timeout=30), swarm.log_text()
    old = swarm.state()["supervisor_pid"]
    workers = {p: _worker_pids(swarm, p) for p in swarm.busy_phases()}
    cfg = _cfg_of(swarm, monkeypatch)
    monkeypatch.setattr(restart_mod, "start_supervisor", lambda *a, **k: None)
    plan = restart_mod.save(cfg, restart_mod.new_plan(
        cfg, restart_mod.SUPERVISOR, time.time(), "the worker on W1"))

    assert restart_mod.run(cfg, plan["id"]) == 1

    assert not _alive(old)
    plan = _plan(swarm)
    assert plan["stage"] == restart_mod.FAILED and "did not start" in plan["detail"]
    told = [ln for ln in swarm.tg_lines() if "the restart" in ln]
    assert len(told) == 1 and len(told[0]) <= 280
    assert told[0].startswith(
        f"[{swarm.project.name}] Asks you: Run `swarm restart` to bring the supervisor back:"
        " the restart the worker on W1 asked for failed (")
    assert told[0].endswith("so nothing is started or merged. Workers and questions are still"
                            " in their windows.")
    assert "RESTART-FAILED" in swarm.log_text() and "left=unsupervised" in swarm.log_text()
    assert "the last restart FAILED" in swarm.cli("status").stdout
    # Nothing else was lost, and what the message says to do brings it back.
    monkeypatch.undo()
    assert {p: _worker_pids(swarm, p) for p in workers} == workers
    r = swarm.cli("restart", timeout=120)
    assert "restarted: supervisor pid none ->" in r.stdout
    assert _alive(swarm.state()["supervisor_pid"]) and swarm.busy_phases() == sorted(workers)
    assert "FAILED" not in swarm.cli("status").stdout


# -- full restart: the questions ------------------------------------------------
def test_a_full_restart_refuses_while_a_question_waits_and_keeps_it_when_told_to(swarm):
    _flat_ledger(swarm, 5)
    swarm.env["FAKE_WORKER_SLEEP"] = "300"
    swarm.up()
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 4, timeout=30), swarm.log_text()
    old = swarm.state()["supervisor_pid"]
    swarm.cli("waiting", "W1", "drop the legacy column, yes or no?")
    assert swarm.wait(lambda: "W1" in swarm.state()["waiting"], timeout=10)
    asker = _worker_pids(swarm, "W1")
    assert asker

    r = swarm.cli("restart", "--full", check=False)
    assert r.returncode == 1
    assert "refused" in r.stderr and "the worker on W1" in r.stderr
    assert "drop the legacy column, yes or no?" in r.stderr
    for flag in ("--wait-questions", "--keep-questions", "--force"):
        assert flag in r.stderr
    st = swarm.state()
    assert st["supervisor_pid"] == old and _alive(old) and not st["drain"]
    assert "W1" in st["waiting"] and _worker_pids(swarm, "W1") == asker
    assert not _plan(swarm)

    r = swarm.cli("restart", "--full", "--keep-questions")
    assert "draining now" in r.stdout, r.stdout + r.stderr
    assert swarm.wait(lambda: swarm.state()["drain"].get("waiting") == ["3 workers"], timeout=10)
    assert "then restart" in swarm.cli("status").stdout
    for phase in ("W0", "W2", "W3"):  # the work that was running finishes
        swarm.cli("done", phase, "ok")
    assert swarm.wait(lambda: _plan(swarm).get("stage") == restart_mod.DONE, timeout=90), (
        swarm.log_text() + (swarm.state_dir / "logs" / "drain-down.log").read_text())

    st = swarm.state()
    assert st["supervisor_pid"] != old and _alive(st["supervisor_pid"]) and not _alive(old)
    assert st["parked"] == ["W1"] and not st["drain"]
    assert _worker_pids(swarm, "W1") == asker  # the same session, never ended
    log = swarm.log_text()
    assert "RESTART-KEPT W1" in log and "RESTART-KEPT-RESTORED W1" in log
    assert "RUN-ENDED W1" not in log
    assert not (swarm.state_dir / restart_mod.KEPT_FILE).exists()
    assert not any("did not restart" in ln for ln in swarm.tg_lines())
    # The new run carries on around it, and it finishes like any parked worker.
    assert swarm.wait(lambda: "W4" in swarm.busy_phases(), timeout=30), swarm.log_text()
    assert _launches(swarm, "W1") == 1
    swarm.cli("done", "W1", "ok")
    assert swarm.wait(lambda: swarm.state()["done"].get("W1") == "ok", timeout=20), swarm.log_text()
    assert swarm.state()["parked"] == []


def test_a_drain_told_to_wait_for_questions_counts_them(cfg):
    st = State.fresh(3)
    st.claim_slot("P0")
    st.claim_slot("P1")
    st.waiting["P1"] = time.time() + 60
    st.parked = ["P2"]
    st.drain = {"since": 1.0, "restart": "x", "questions": restart_mod.WAIT}
    assert drain_mod.waiting_for(cfg, st) == ["1 worker", "2 questions"]
    st.drain["questions"] = restart_mod.KEEP
    assert drain_mod.waiting_for(cfg, st) == ["1 worker"]
    assert drain_mod.line({"waiting": ["1 worker"], "restart": "x"}) == (
        "Draining: waiting for 1 worker, then restart")


def test_plain_drain_warns_that_it_closes_the_waiting_sessions(swarm):
    _flat_ledger(swarm, 2)
    swarm.env["FAKE_WORKER_SLEEP"] = "300"
    swarm.up()
    assert swarm.wait(lambda: len(swarm.busy_phases()) == 2, timeout=30), swarm.log_text()
    swarm.cli("waiting", "W0", "which port?")
    assert swarm.wait(lambda: "W0" in swarm.state()["waiting"], timeout=10)
    r = swarm.cli("down", "--drain")  # W1 is still working, so the stop waits
    assert "draining" in r.stdout
    assert "closes them" in r.stderr and "the worker on W0: which port?" in r.stderr
    assert "swarm restart" in r.stderr
    swarm.cli("down", "--cancel")
    assert not swarm.state()["drain"]


# -- the pieces -------------------------------------------------------------------
def test_requester_names_the_session_or_the_owner(cfg):
    who = restart_mod.requester
    assert who(cfg, {}) == "owner terminal"
    assert who(cfg, {"SWARM_SESSION_ID": "worker:P7"}) == "the worker on P7"
    assert who(cfg, {"SWARM_PHASE": "P7"}) == "the worker on P7"
    assert who(cfg, {"SWARM_SESSION_ID": "operator:P7-op1"}) == "operator job P7-op1"
    assert who(cfg, {"SWARM_SESSION_ID": "overseer:20260101T000000Z"}) == (
        "the Overseer (pass 20260101T000000Z)")
    assert who(cfg, {"SWARM_OWNER_CONSOLE": "1"}) == "the owner console"


def test_the_line_says_what_a_planned_restart_waits_for(cfg):
    now = 1_800_000_000.0
    plan = restart_mod.new_plan(cfg, restart_mod.FULL, now + 3600, "owner terminal",
                                questions=restart_mod.WAIT, now=now)
    text = restart_mod.line(plan, 3, 2, now)
    assert text.startswith("full restart planned at ") and "(in 1h 0m)" in text
    assert "waits for 3 workers and 2 questions" in text and "asked by owner terminal" in text
    plan["questions"] = restart_mod.KEEP
    assert "waits for 3 workers; 2 questions carried across" in restart_mod.line(plan, 3, 2, now)
    plan["questions"] = restart_mod.REFUSE
    assert "refuses while 2 questions wait" in restart_mod.line(plan, 3, 2, now)
    plan["mode"] = restart_mod.SUPERVISOR
    assert "supervisor only, 1 worker and 0 questions carry on untouched" in (
        restart_mod.line(plan, 1, 0, now))
    assert "(in " not in restart_mod.line(plan, 1, 0, now, relative=False)
    plan.update(stage=restart_mod.FAILED, detail="it broke", ended_at=now)
    assert "FAILED" in restart_mod.line(plan, 1, 0, now + 60)
    assert restart_mod.line(plan, 1, 0, now + 2 * 86400) == ""  # old news
    plan["stage"] = restart_mod.DONE
    assert restart_mod.line(plan, 1, 0, now) == ""


def _sup(cfg, **kw) -> Supervisor:
    sup = Supervisor(cfg, **kw)
    sup._bootstrapped = True
    return sup


def test_a_hand_over_waits_for_a_launch_in_flight_and_starts_nothing_meanwhile(cfg):
    plan = restart_mod.save(cfg, restart_mod.new_plan(
        cfg, restart_mod.SUPERVISOR, time.time(), "owner terminal"))
    sup = _sup(cfg)
    sup._launching.add("P0")  # its thread has not settled yet
    sup._launch_fails["P1"] = (3, 123.0)

    sup._on_handover(plan["id"])

    assert not sup._stop and not sup._handed_over
    assert restart_mod.load(cfg)["waiting"] == ["1 worker starting"]
    assert sup._fill_slots("test") == []  # P1/P2 are ready and nothing launches
    assert "LAUNCH-HELD handover" in cfg.supervisor_log.read_text()

    sup._launching.discard("P0")
    sup._handover_tick()

    assert sup._stop and sup._handed_over
    note = json.loads((cfg.state_dir / restart_mod.HANDOVER_FILE).read_text())
    assert note["id"] == plan["id"] and note["launch_fails"] == {"P1": [3, 123.0]}


def test_a_cancelled_hand_over_resumes_launching(cfg):
    sup = _sup(cfg)
    sup._launching.add("P0")
    sup._on_handover("plan-1")
    sup._on_handover_cancel("another-plan")
    assert sup._handover == "plan-1"
    sup._launching.discard("P0")
    sup._on_handover_cancel("plan-1")
    assert sup._handover is None and not sup._stop
    assert sup.stub_launches == ["P0", "P1", "P2"]


def test_a_hand_over_leaves_the_master_and_the_operator_alone(cfg, monkeypatch):
    """What a plain stop ends on its way out, a hand-over leaves for its successor."""
    from swarm_orchestrator import operator as operator_mod

    ended: list[str] = []
    monkeypatch.setattr(operator_mod, "release", lambda *a, **k: ended.append("operator"))
    sup = _sup(cfg)
    monkeypatch.setattr(sup.bigpic, "shutdown", lambda: ended.append("bigpic"))
    sup._open_fifo()
    sup._handed_over = True
    sup._stop = True
    sup.run()
    assert ended == []
    assert "SUPERVISOR-STOP handover" in cfg.supervisor_log.read_text()

    sup = _sup(cfg)
    monkeypatch.setattr(sup.bigpic, "shutdown", lambda: ended.append("bigpic"))
    sup._stop = True
    sup.run()
    assert ended == ["operator", "bigpic"]


def _claim(cfg, phase: str) -> None:
    """``phase`` in a slot with its ``CLAIM`` line, as a launch leaves it."""
    from swarm_orchestrator.logutil import Log

    with state_mod.transaction(cfg) as st:
        slot = st.claim_slot(phase)
        st.bootstrapping = False
    log = Log(cfg.supervisor_log)
    log.line(f"CLAIM {phase} slot={slot.id}")
    log.close()


def test_adoption_leaves_a_sentinel_from_an_earlier_attempt_alone(cfg):
    """A ``fail`` left by an earlier attempt says nothing about the worker that
    runs now; acted on, it would roll that worker's branch back."""
    from swarm_orchestrator import launch as launch_mod

    launch_mod._write_sentinel(cfg, "P0", "fail", "the first attempt broke")
    old = time.time() - 3600
    os.utime(cfg.done_dir / "P0.fail", (old, old))
    _claim(cfg, "P0")  # launched again since, by hand
    sup = Supervisor(cfg, adopt=True)
    sup._adopt()
    st = state_mod.read(cfg)
    assert "P0" not in st.done and [s.phase for s in st.busy_slots()] == ["P0"]
    assert "ADOPT-OLD-SENTINEL P0 fail" in cfg.supervisor_log.read_text()


def test_adoption_keeps_the_back_offs_and_acts_on_a_done_nobody_read(cfg):
    from swarm_orchestrator import launch as launch_mod

    _claim(cfg, "P0")
    launch_mod._write_sentinel(cfg, "P0", "ok", "finished while nobody was reading")
    restart_mod.write_handover(cfg, {"id": "x", "launch_fails": {"P1": [3, time.time()]},
                                     "crashes": {"P2": [1.0]}, "pinged": {"k": 5.0},
                                     "overseer_bad": 2, "backup_last": 7.0})
    sup = Supervisor(cfg, adopt=True)

    sup._adopt()

    st = state_mod.read(cfg)
    assert st.done == {"P0": "ok"} and not st.busy_slots()
    assert sup._bootstrapped and sup._given_up("P1") and sup._overseer_bad == 2
    assert sup._crashes == {"P2": [1.0]} and sup._pinged["k"] == 5.0 and sup._backup_last == 7.0
    # Not P0 (done) and not P1 (given up). The launch stub claims nothing, so
    # each look at the free slots picks P2 again; a real launch is guarded.
    assert set(sup.stub_launches) == {"P2"}
    assert not (cfg.state_dir / restart_mod.HANDOVER_FILE).exists()  # read once
    log = cfg.supervisor_log.read_text()
    assert "ADOPT busy=['P0']" in log and "ADOPT-DONE P0 ok" in log


def test_adoption_lets_a_done_inside_its_grace_run_the_grace_out(cfg, monkeypatch):
    from swarm_orchestrator import launch as launch_mod

    monkeypatch.setattr(cfg, "done_grace_s", 30)
    _claim(cfg, "P0")
    launch_mod._write_sentinel(cfg, "P0", "ok", "just now")
    sup = Supervisor(cfg, adopt=True)
    sup._adopt()
    assert "P0" not in state_mod.read(cfg).done and "P0" in sup._adopt_recheck
    assert 0 < sup._next_timeout() <= 33
    sup._adopt_recheck["P0"] = time.time() - 1
    sup._adopt_recheck_tick()
    assert state_mod.read(cfg).done == {"P0": "ok"} and not sup._adopt_recheck


def test_a_scheduled_restart_is_started_once_when_due_and_not_when_cancelled(cfg, monkeypatch):
    from swarm_orchestrator.cli import cmd_restart_cancel

    started: list[str] = []

    class Helper:
        returncode = None

        def poll(self):
            return self.returncode

    helper = Helper()
    monkeypatch.setattr(restart_mod, "spawn_runner",
                        lambda c, plan_id, at=None: started.append(plan_id) or helper)
    plan = restart_mod.new_plan(cfg, restart_mod.SUPERVISOR, time.time() + 3600, "owner terminal")
    plan["timer"] = "supervisor"
    restart_mod.save(cfg, plan)
    sup = _sup(cfg)

    sup._restart_tick()
    assert started == [] and 3500 < sup._next_timeout() <= 3600

    restart_mod.update(cfg, plan["id"], at=time.time() - 1)
    sup._restart_tick()
    sup._restart_tick()
    assert started == [plan["id"]]
    assert "RESTART-DUE" in cfg.supervisor_log.read_text()

    # The helper dies without settling the plan (the new code does not import):
    # this supervisor is still here, and it is the one that says so.
    helper.returncode = 2
    sup._restart_tick()
    assert restart_mod.load(cfg)["stage"] == restart_mod.FAILED
    # Nothing was changed, so nobody is asked: it is held for the Overseer's summary.
    assert not (cfg.state_dir.parent / "tg.log").exists()
    told = (cfg.state_dir / "notifications.jsonl").read_text()
    assert "did not restart" in told and "Nothing was changed" in told and '"folded"' in told

    later = restart_mod.new_plan(cfg, restart_mod.SUPERVISOR, time.time() + 3600, "owner terminal")
    later["timer"] = "supervisor"
    restart_mod.save(cfg, later)
    assert cmd_restart_cancel(cfg) == 0
    restart_mod.update(cfg, later["id"], at=time.time() - 1)
    sup._restart_tick()
    assert started == [plan["id"]]


def test_the_bridge_keeps_a_poke_sent_while_no_supervisor_reads(cfg):
    from swarm_orchestrator import launch as launch_mod

    cfg.ensure_dirs()
    os.mkfifo(cfg.fifo_path)
    assert launch_mod._poke_fifo(cfg, "done P0 ok\n") is False  # nobody there: lost
    with restart_mod.Bridge(cfg) as bridge:
        assert launch_mod._poke_fifo(cfg, "done P0 ok\n") is True
        assert launch_mod._poke_fifo(cfg, "waiting P1\n") is True
        assert os.read(bridge.fd, 4096) == b"done P0 ok\nwaiting P1\n"


def test_events_read_and_not_handled_go_back_into_the_pipe_on_a_hand_over(cfg):
    import threading

    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
    with restart_mod.Bridge(cfg) as bridge:
        sup = _sup(cfg)
        thread = threading.Thread(target=sup.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not restart_mod.supervisor_mark(cfg):
            time.sleep(0.02)
        os.write(bridge.fd, b"handover plan-1\ndone P0 ok\nwaiting P1\n")  # one read
        thread.join(15)
        assert not thread.is_alive() and sup._handed_over
        assert os.read(bridge.fd, 4096) == b"done P0 ok\nwaiting P1\n"
        assert "P0" not in state_mod.read(cfg).done

        # ...and the supervisor that takes over reads them.
        nxt = Supervisor(cfg, adopt=True)
        os.write(bridge.fd, b"done P0 ok\nwaiting P1\n")
        thread = threading.Thread(target=nxt.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "P0" not in state_mod.read(cfg).done:
            time.sleep(0.02)
        os.write(bridge.fd, b"shutdown\n")
        thread.join(15)
    assert state_mod.read(cfg).done == {"P0": "ok"}


def test_adoption_drops_orders_addressed_to_the_last_supervisor(cfg):
    """A ``shutdown`` or ``handover`` the last supervisor died before reading is
    still in the pipe the restart held open. Obeyed, it would stop the very
    process the restart was for."""
    import threading

    with state_mod.transaction(cfg) as st:
        st.claim_slot("P0")
    with restart_mod.Bridge(cfg) as bridge:
        os.write(bridge.fd, b"shutdown\nhandover old-plan\ndone P0 ok\n")
        sup = Supervisor(cfg, adopt=True)
        thread = threading.Thread(target=sup.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and "P0" not in state_mod.read(cfg).done:
            time.sleep(0.02)
        time.sleep(0.3)
        assert thread.is_alive() and not sup._handed_over  # it did not obey them
        os.write(bridge.fd, b"shutdown\n")  # one sent to it is obeyed
        thread.join(15)
        assert not thread.is_alive()
    assert state_mod.read(cfg).done == {"P0": "ok"}
    log = cfg.supervisor_log.read_text()
    assert "STALE-ORDER-DROPPED shutdown" in log and "STALE-ORDER-DROPPED handover old-plan" in log


def test_swarm_down_cancels_a_restart_that_has_not_begun(cfg):
    from swarm_orchestrator.cli import cmd_down

    plan = restart_mod.save(cfg, restart_mod.new_plan(
        cfg, restart_mod.SUPERVISOR, time.time() + 3600, "owner terminal"))
    assert cmd_down(cfg) == 0
    assert restart_mod.load(cfg)["stage"] == restart_mod.CANCELLED
    # A full restart's own down is part of it, and leaves its plan alone.
    plan = restart_mod.new_plan(cfg, restart_mod.FULL, time.time(), "owner terminal")
    plan["stage"] = restart_mod.RESTARTING
    restart_mod.save(cfg, plan)
    assert cmd_down(cfg) == 0
    assert restart_mod.load(cfg)["stage"] == restart_mod.RESTARTING


def test_what_stops_an_older_supervisor_from_being_stopped_safely(cfg):
    st = State.fresh(2)
    assert restart_mod.legacy_blockers(cfg, st) == []
    st.master_alive = True
    assert restart_mod.legacy_blockers(cfg, st) == ["the start-up pass"]
    st.overseer_pass = "20260101T000000Z"
    st.operator_phase = "P7"
    assert restart_mod.legacy_blockers(cfg, st) == ["an Overseer pass", "operator job P7"]
    st = State.fresh(2)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    cfg.supervisor_log.write_text(
        "t m SUPERVISOR-START pid=1\n"
        "t m LAUNCH-READY P0 P1 (init master idle)\n"
        "t m EVENT launched P0 launched fails=0\n")
    assert restart_mod.launching_from_log(cfg) == {"P1"}
    assert restart_mod.legacy_blockers(cfg, st) == ["1 worker starting"]
    with cfg.supervisor_log.open("a") as fh:
        fh.write("t m EVENT launched P1 failed fails=1\n")
    assert restart_mod.legacy_blockers(cfg, st) == []


def test_a_supervisor_only_knows_handover_if_it_said_so(cfg):
    assert not restart_mod.capable(cfg, os.getpid())
    restart_mod.mark_supervisor(cfg, adopted=False)
    assert restart_mod.capable(cfg, os.getpid()) and restart_mod.capable(cfg, os.getpid(), "restart-at")
    assert not restart_mod.capable(cfg, os.getppid())  # some other process


def test_doctor_says_when_the_supervisor_runs_older_code(cfg, monkeypatch):
    from swarm_orchestrator import doctor

    st = state_mod.read(cfg)
    assert doctor._check_restart(cfg, st).status == doctor.OK
    st.supervisor_pid = os.getpid()
    monkeypatch.setattr(restart_mod, "code_stamp", lambda: time.time() + 3600)
    check = doctor._check_restart(cfg, st)
    assert check.status == doctor.WARN and "older code" in check.detail
    assert check.fix_hint.startswith("swarm restart")
    monkeypatch.setattr(restart_mod, "code_stamp", lambda: 0.0)
    assert doctor._check_restart(cfg, st).status == doctor.OK
    # A plan whose timer is gone will never fire, and doctor says so.
    plan = restart_mod.new_plan(cfg, restart_mod.SUPERVISOR, time.time() + 60, "owner terminal")
    plan.update(timer="runner", runner_pid=2 ** 22 + 12345)
    restart_mod.save(cfg, plan)
    check = doctor._check_restart(cfg, st)
    assert check.status == doctor.WARN and "will not happen" in check.detail


def test_restart_is_refused_with_flags_that_only_a_full_one_takes(cfg, capsys):
    from swarm_orchestrator.cli import cmd_restart

    assert cmd_restart(cfg, policy=restart_mod.KEEP) == 2
    assert "are for --full" in capsys.readouterr().err
    assert cmd_restart(cfg, delay="soon") == 2
    assert cmd_restart(cfg, delay="1h") == 1  # nothing is running to restart later
    assert "no supervisor is running" in capsys.readouterr().err
    assert not restart_mod.load(cfg)


def test_the_dashboard_and_the_board_show_a_planned_restart(cfg):
    from types import SimpleNamespace

    from swarm_orchestrator.tui import alerts
    from swarm_orchestrator.web import board

    plan = restart_mod.new_plan(cfg, restart_mod.SUPERVISOR, time.time() + 7200, "the worker on P1")
    state = {"slots": [{"busy": True, "phase": "P0"}, {"busy": True, "phase": "P1"}],
             "waiting": {"P1": 1.0}, "parked": ["P2"]}
    snap = SimpleNamespace(ok=True, frozen={}, drain={}, paused=False, usage_hold="",
                           supervisor_alive=True, finished=False, integ_blocked=None, slots=[],
                           last_event_at=None, pause_at=0.0)
    dash = SimpleNamespace(snapshot=snap, notifications=[], restart=plan, _state=state)
    lines = [text for text, _ in alerts.problems(dash)]
    assert any("restart planned at" in t and "1 worker and 2 questions carry on" in t
               and "asked by the worker on P1" in t for t in lines)
    header = board._restart(dash, time.time())
    assert header["restart_stage"] == "planned" and "(in " not in header["restart"]
    assert board._restart(SimpleNamespace(restart={}, _state=state), 0.0) == {
        "restart": "", "restart_stage": ""}


# -- tmux: a server of its own ------------------------------------------------------
FAKE_DASH = """#!/bin/sh
echo $$ >> "$FAKE_DASH_LOG"
exec sleep 600
"""


@pytest.fixture
def tswarm(tmp_path, monkeypatch):
    """A :class:`conftest.Swarm` on the tmux driver, on an isolated tmux server."""
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")
    from conftest import DEMO, SWARM_BIN, Swarm, _kill_orphan_fakes

    sockdir = tempfile.mkdtemp(prefix="restartprobe-")
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)
    project = tmp_path / "project"
    shutil.copytree(DEMO, project)
    dash = tmp_path / "fake-dash.sh"
    dash.write_text(FAKE_DASH)
    dash.chmod(0o755)
    env = dict(os.environ)
    for leak in ("SWARM_MASTER_CMD", "SWARM_WORKER_CMD", "SWARM_READY_MARKER", "SWARM_SESSION_ID",
                 "SWARM_LAYOUT", "SWARM_PROJECT", "SWARM_PHASE", "TMUX_PANE"):
        env.pop(leak, None)
    env.update({
        "SWARM_STATE_DIR": str(tmp_path / "state"), "SWARM_TG_SINK": str(tmp_path / "tg.log"),
        "SWARM_BIN": SWARM_BIN, "SWARM_SLUG": "ttest", "SWARM_DRIVER": "tmux",
        "SWARM_SESSION": "restartprobe", "SWARM_TUI_AUTOSTART": "1",
        "SWARM_TUI_CMD": str(dash), "FAKE_DASH_LOG": str(tmp_path / "dash.log"),
        "FAKE_WORKER_SLEEP": "300", "FAKE_MASTER_WAIT": "1",
    })
    for key in ("SWARM_STATE_DIR", "SWARM_SLUG", "SWARM_DRIVER", "SWARM_SESSION",
                "SWARM_TUI_AUTOSTART", "SWARM_TUI_CMD", "SWARM_TG_SINK"):
        monkeypatch.setenv(key, env[key])
    inst = Swarm(project, tmp_path / "state", env)
    inst.dash_log = tmp_path / "dash.log"
    try:
        yield inst
    finally:
        try:
            inst.down()
        except Exception:  # noqa: BLE001
            pass
        st = inst.state()
        if st and st.get("supervisor_pid"):
            try:
                os.kill(st["supervisor_pid"], signal.SIGKILL)
            except OSError:
                pass
        tmux.run(["kill-server"])
        _kill_orphan_fakes(tmp_path / "state")
        shutil.rmtree(sockdir, ignore_errors=True)


def _settled(swarm, n: int) -> bool:
    """``n`` launches have finished: under tmux a claim comes seconds before it."""
    return sum(1 for ln in swarm.log_text().splitlines() if " EVENT launched " in ln) >= n


def _pane_pid(pane: str) -> int:
    return int(tmux.run(["display-message", "-p", "-t", pane, "#{pane_pid}"]).stdout.strip())


def _windows(session: str) -> list[str]:
    return tmux.run(["list-windows", "-t", f"={session}", "-F", "#{window_name}"]).stdout.split()


def test_restart_under_tmux_leaves_every_pane_and_restarts_the_dashboard(tswarm):
    swarm = tswarm
    _flat_ledger(swarm, 6)
    swarm.up()
    assert swarm.wait(lambda: _settled(swarm, 4), timeout=90), swarm.log_text()
    assert swarm.wait(lambda: swarm.dash_log.is_file(), timeout=10)
    st = swarm.state()
    old = st["supervisor_pid"]
    # One worker asks and is parked in its own window; another has just asked.
    swarm.cli("waiting", "W0", "left or right?")
    assert swarm.wait(lambda: "W0" in swarm.state()["waiting"], timeout=10)
    _edit_state(swarm, "st.waiting['W0'] = 1.0")
    swarm.cli("waiting", "W1", "delete the old table?")
    assert swarm.wait(lambda: swarm.state()["parked"] == ["W0"], timeout=20), swarm.log_text()
    assert swarm.wait(lambda: _settled(swarm, 5), timeout=90), swarm.log_text()  # W4 took the slot
    st = swarm.state()
    wait_win = st["windows"]["wait:W0"]
    panes = {s["phase"]: s["pane_id"] for s in st["slots"]}
    pids = {phase: _pane_pid(pane) for phase, pane in panes.items()}
    parked_pid = _pane_pid(tmux.list_panes(wait_win)[0])
    console = st["master_pane"], st["operator_pane"]
    dash_before = swarm.dash_log.read_text().split()

    r = swarm.cli("restart", timeout=120)

    assert "restarted: supervisor pid" in r.stdout and "dashboard" in r.stdout, r.stdout + r.stderr
    st = swarm.state()
    assert st["supervisor_pid"] != old and _alive(st["supervisor_pid"]) and not _alive(old)
    assert {s["phase"]: s["pane_id"] for s in st["slots"]} == panes   # slots adopted as they were
    assert {phase: _pane_pid(pane) for phase, pane in panes.items()} == pids  # same processes
    assert st["parked"] == ["W0"] and list(st["waiting"]) == ["W1"]
    assert st["windows"]["wait:W0"] == wait_win and "wait:W0" in _windows("restartprobe")
    assert _pane_pid(tmux.list_panes(wait_win)[0]) == parked_pid
    assert (st["master_pane"], st["operator_pane"]) == console
    assert swarm.wait(lambda: len(swarm.dash_log.read_text().split()) == len(dash_before) + 1,
                      timeout=10)  # the dashboard was started again, once
    assert all(_launches(swarm, p) == 1 for p in panes)
    # The parked worker's question is still there to be answered, and its done lands.
    swarm.cli("done", "W0", "ok")
    assert swarm.wait(lambda: swarm.state()["done"].get("W0") == "ok", timeout=20), swarm.log_text()
    assert swarm.wait(lambda: "wait:W0" not in _windows("restartprobe"), timeout=10)


def test_adoption_frees_a_claim_whose_launch_was_cut_off(tswarm):
    swarm = tswarm
    _flat_ledger(swarm, 3)
    swarm.up()
    assert swarm.wait(lambda: _settled(swarm, 3), timeout=90), swarm.log_text()
    old = swarm.state()["supervisor_pid"]
    os.kill(old, signal.SIGKILL)
    assert swarm.wait(lambda: not _alive(old), timeout=10)
    # The slot is claimed and nothing runs in it: the pane holds its idle command.
    pane = next(s["pane_id"] for s in swarm.state()["slots"] if s["phase"] == "W2")
    tmux.respawn_pane(pane, "exec sleep infinity")
    assert swarm.wait(lambda: not _worker_pids(swarm, "W2"), timeout=10)

    swarm.cli("restart", timeout=120)

    assert swarm.wait(lambda: "ADOPT-DEAD-CLAIM W2" in swarm.log_text(), timeout=20), swarm.log_text()
    # Freed, and started again; the two that were running were not touched.
    assert swarm.wait(lambda: _launches(swarm, "W2") == 2, timeout=60), swarm.log_text()
    assert _launches(swarm, "W0") == 1 and _launches(swarm, "W1") == 1


def test_questions_are_carried_out_of_the_session_and_back_in(tswarm, monkeypatch):
    """The move a full ``--keep-questions`` restart makes around down and up: the
    parked window leaves the session before it is torn down, and comes back."""
    from swarm_orchestrator.cli import cmd_down, cmd_up
    from swarm_orchestrator.logutil import Log

    swarm = tswarm
    cfg = load(project_dir=str(swarm.project))
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    session_mod.setup(cfg)
    st = state_mod.read(cfg)
    slot = st.slots[0]
    marker = {"SWARM_STATE_DIR": str(cfg.state_dir), "SWARM_SESSION_ID": "worker:W0",
              "SWARM_PHASE": "W0"}
    tmux.respawn_pane(slot.pane_id, "exec sleep 987", env=marker)
    asker = _pane_pid(slot.pane_id)
    with state_mod.transaction(cfg) as s2:
        s2.claim_slot("W0")
        s2.waiting["W0"] = time.time() + 600  # asked, not parked yet
    log = Log(cfg.supervisor_log)
    try:
        assert restart_mod.carry_out(cfg, "plan-1", log) == ["W0"]
        hold = restart_mod.hold_session(cfg)
        assert "wait:W0" in _windows(hold) and "wait:W0" not in _windows(cfg.session)

        assert cmd_down(cfg) == 0  # the session goes; the question does not
        assert not tmux.session_exists(cfg.session) and tmux.session_exists(hold)
        assert _alive(asker)

        monkeypatch.setenv("SWARM_TUI_AUTOSTART", "0")
        cfg2 = load(project_dir=str(swarm.project))
        state_mod.init_state(cfg2, carried=restart_mod.kept_phases(cfg2))
        session_mod.setup(cfg2)
        assert restart_mod.carry_in(cfg2, log) == ["W0"]
    finally:
        log.close()
    st = state_mod.read(cfg2)
    assert st.parked == ["W0"] and not st.busy_slots()
    assert "wait:W0" in _windows(cfg2.session) and not tmux.session_exists(hold)
    assert _pane_pid(tmux.list_panes(st.windows["wait:W0"])[0]) == asker and _alive(asker)
    assert not restart_mod.load_kept(cfg2)
    assert cmd_up is not None


def test_a_full_restart_under_tmux_brings_the_question_back_in_its_window(tswarm):
    swarm = tswarm
    _flat_ledger(swarm, 3)
    swarm.up()
    assert swarm.wait(lambda: _settled(swarm, 3), timeout=90), swarm.log_text()
    old = swarm.state()["supervisor_pid"]
    swarm.cli("waiting", "W1", "keep the old endpoint?")
    assert swarm.wait(lambda: "W1" in swarm.state()["waiting"], timeout=10)
    pane = next(s["pane_id"] for s in swarm.state()["slots"] if s["phase"] == "W1")
    asker = _pane_pid(pane)

    r = swarm.cli("restart", "--full", "--keep-questions")
    assert "draining now" in r.stdout, r.stdout + r.stderr
    swarm.cli("done", "W0", "ok")
    swarm.cli("done", "W2", "ok")
    assert swarm.wait(lambda: _plan(swarm).get("stage") == restart_mod.DONE, timeout=180), (
        swarm.log_text() + (swarm.state_dir / "logs" / "drain-down.log").read_text())

    st = swarm.state()
    assert st["supervisor_pid"] != old and _alive(st["supervisor_pid"])
    assert st["parked"] == ["W1"]
    assert "wait:W1" in _windows("restartprobe")          # in the new session
    assert not tmux.session_exists("restartprobe-kept")   # the holding one is gone
    assert tmux.list_panes(st["windows"]["wait:W1"]) == [pane]
    assert _pane_pid(pane) == asker and _alive(asker)     # the same session, never ended
    assert _launches(swarm, "W1") == 1
    swarm.cli("done", "W1", "ok")
    assert swarm.wait(lambda: swarm.state()["done"].get("W1") == "ok", timeout=30), swarm.log_text()
    assert swarm.wait(lambda: "wait:W1" not in _windows("restartprobe"), timeout=10)
