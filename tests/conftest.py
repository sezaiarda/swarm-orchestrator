"""Shared hermetic test harness.

A :class:`Swarm` binds a throwaway project dir (a copy of ``examples/demo``) and
a temp state dir, and shells out to the real ``swarm`` CLI via
``python -m swarm_orchestrator`` so the tests exercise the true process
boundaries (detached supervisor, FIFO, sentinels) with fake master/worker
scripts instead of ``claude``. Everything is torn down in the fixture finaliser.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / "examples" / "demo"
SWARM_BIN = f"{sys.executable} -m swarm_orchestrator"


class Swarm:
    """Drives the swarm CLI against one temp project + state dir."""

    def __init__(self, project: Path, state_dir: Path, env: dict[str, str]) -> None:
        self.project = project
        self.state_dir = state_dir
        self.env = env
        self.tg_sink = Path(env["SWARM_TG_SINK"])

    def cli(self, *args: str, check: bool = True, timeout: float = 30):
        return subprocess.run(
            [sys.executable, "-m", "swarm_orchestrator", "--project-dir",
             str(self.project), *args],
            cwd=str(self.project),
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )

    def up(self):
        return self.cli("up")

    def down(self):
        return self.cli("down", check=False)

    def state(self) -> dict | None:
        path = self.state_dir / "state.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def log_text(self) -> str:
        path = self.state_dir / "logs" / "supervisor.log"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def tg_lines(self) -> list[str]:
        return (
            self.tg_sink.read_text(encoding="utf-8").splitlines()
            if self.tg_sink.is_file()
            else []
        )

    def busy_phases(self) -> list[str]:
        st = self.state()
        if not st:
            return []
        return sorted(s["phase"] for s in st["slots"] if s["busy"])

    def busy_count(self) -> int:
        st = self.state()
        return sum(1 for s in st["slots"] if s["busy"]) if st else 0

    def finished(self) -> bool:
        st = self.state()
        return bool(st and st.get("finished"))

    def wait(self, pred, timeout: float = 25, interval: float = 0.05) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if pred():
                    return True
            except Exception:  # noqa: BLE001 - polling predicate, keep trying
                pass
            time.sleep(interval)
        return False


def _kill_orphan_fakes(state_dir: Path) -> None:
    """SIGKILL any fake master/worker still alive that belongs to THIS test.

    Insurance net for an orphaned fake (e.g. a master left in
    `exec sleep infinity`) once `down()` and the supervisor SIGKILL have run.

    Scoped by the `SWARM_STATE_DIR` every fake carries in its environment (the
    launcher exports it onto each worker and master) -- NOT by a global
    `pkill -f fake-*.sh`. The unscoped match assumed the suite is the only one on
    the box, so whenever two runs overlapped -- several agents working the repo at
    once, or `pytest -n` -- each teardown killed the OTHER run's live fakes. The
    victim test then saw its workers CLAIMed and LAUNCHed and then simply stop,
    and failed on its `swarm.wait` timeout: the phantom Tier A flakiness.

    A process must match BOTH the state dir and a fake script name, so nothing
    outside this fixture is ever signalled. Reading `/proc` is Linux-only, as is
    the rest of the harness (mkfifo + flock); where it is missing the `down()` +
    supervisor-kill path stands on its own.
    """
    want = f"SWARM_STATE_DIR={state_dir}".encode()
    try:
        entries = [p for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        return
    for entry in entries:
        try:
            cmdline = (entry / "cmdline").read_bytes()
            if b"fake-master.sh" not in cmdline and b"fake-worker.sh" not in cmdline:
                continue
            if want not in (entry / "environ").read_bytes().split(b"\0"):
                continue
            os.kill(int(entry.name), signal.SIGKILL)
        except (OSError, ValueError):
            continue  # exited mid-scan, or not ours to read


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers",
        "real_launch: let an in-process Supervisor really start worker processes",
    )


@pytest.fixture(autouse=True)
def _overseer_off(monkeypatch):
    """The Overseer is on by default in a real project; in the suite it is off.

    A pass is a real ``claude`` session unless a fake is configured, and every
    test that finishes phases would otherwise trigger one. Tests of the
    Overseer turn it back on (``SWARM_OVERSEER=1``) with the fake master. Set in
    the environment, so the ``swarm`` fixture's copy of it carries it too.
    """
    monkeypatch.setenv("SWARM_OVERSEER", "0")


@pytest.fixture(autouse=True)
def _gc_auto_off(monkeypatch):
    """Automatic gc is on by default in a real project; in the suite it is off,
    so no supervisor under test starts a gc thread walking /proc and the temp
    tree. ``tests/test_gc.py`` turns it back on where it is the subject."""
    monkeypatch.setenv("SWARM_GC_AUTO", "0")


@pytest.fixture(autouse=True)
def _no_inprocess_launch(request, monkeypatch):
    """An in-process :class:`Supervisor` records its launches instead of making them.

    The supervisor launches workers itself now, on a thread, the moment a slot
    frees — and the default ``worker_cmd`` is a real ``claude``. Every test that
    builds ``Supervisor(cfg)`` and drives ``_advance_done`` / ``_park`` /
    ``_on_master_idle`` would otherwise start one. Picks are appended to
    ``sup.stub_launches`` and settle like a denial (no slot claimed), so the
    double-launch guard and the finish check behave as after a real refusal.
    Tests of the launcher itself opt out with ``@pytest.mark.real_launch`` and
    point ``SWARM_WORKER_CMD`` at something harmless. The end-to-end ``swarm``
    fixture is unaffected: its supervisor is a separate process.
    """
    if request.node.get_closest_marker("real_launch"):
        return
    from swarm_orchestrator.supervisor import Supervisor

    def record(self, phase: str) -> None:
        self.__dict__.setdefault("stub_launches", []).append(phase)
        with self._launch_lock:
            self._launching.discard(phase)

    monkeypatch.setattr(Supervisor, "_start_launch", record)


@pytest.fixture
def swarm(tmp_path: Path):
    project = tmp_path / "project"
    shutil.copytree(DEMO, project)
    state_dir = tmp_path / "state"
    tg = tmp_path / "tg.log"

    env = {k: v for k, v in os.environ.items()}
    for leak in (
        "SWARM_DRIVER",
        "SWARM_MASTER_CMD",
        "SWARM_WORKER_CMD",
        "SWARM_READY_MARKER",
        "SWARM_SESSION",
        "SWARM_SLUG",
        "SWARM_LAYOUT",
    ):
        env.pop(leak, None)
    env.update(
        {
            "SWARM_STATE_DIR": str(state_dir),
            "SWARM_TG_SINK": str(tg),
            "SWARM_BIN": SWARM_BIN,
            "SWARM_SLUG": "test",
            "FAKE_WORKER_SLEEP": "1",
            "FAKE_MASTER_WAIT": "1",
        }
    )
    inst = Swarm(project, state_dir, env)
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
        _kill_orphan_fakes(state_dir)  # insurance net, scoped to THIS run
