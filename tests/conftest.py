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
        # Insurance net for any orphaned fake (e.g. a master left in
        # `exec sleep infinity`). Workers `exec bash ./fake-*.sh` after a `cd`,
        # so their argv is the relative script name -- match on that. The suite
        # runs serially, so a global match cannot cross-kill another live test.
        subprocess.run(["pkill", "-9", "-f", "fake-master.sh"], check=False)
        subprocess.run(["pkill", "-9", "-f", "fake-worker.sh"], check=False)
