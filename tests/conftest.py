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
import threading
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

    def claim(self, phase: str) -> None:
        """Record ``phase`` in a slot, as a live run's state has it, so a
        ``swarm done`` from its worker is accepted without a supervisor."""
        code = (
            "import sys\n"
            "from swarm_orchestrator import state as s\n"
            "from swarm_orchestrator.config import load\n"
            "c = load(project_dir=sys.argv[1])\n"
            "s.init_state(c)\n"
            "with s.transaction(c) as st:\n"
            "    st.claim_slot(sys.argv[2])\n"
        )
        subprocess.run([sys.executable, "-c", code, str(self.project), phase],
                       env=self.env, check=True, capture_output=True)

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
def _own_state_root(tmp_path_factory, monkeypatch):
    """No test reads or writes the machine's own state root.

    A config loaded with no ``SWARM_STATE_DIR`` keeps its run under
    ``$XDG_STATE_HOME/swarm-orchestrator``, which is where the swarms really
    running on this machine keep theirs. Set in the environment, so every
    ``swarm`` a test starts looks in the same throwaway root.

    Nor the run of a session the suite itself is started in: with that session's
    ``SWARM_STATE_DIR`` still set, a test's config would land in a live swarm's
    state dir, and is refused there (it names another project). A test that
    needs the variable sets it.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state")))
    monkeypatch.delenv("SWARM_STATE_DIR", raising=False)


@pytest.fixture(autouse=True)
def _no_session_project(monkeypatch):
    """No test inherits a project from the session that runs the suite.

    A ``swarm`` command with no ``--project-dir`` reads the project named by
    ``SWARM_PROJECT``. Run from inside a swarm session, the suite would
    otherwise point every such command, ledger writes included, at that
    session's live project. A test of the rule sets it itself.
    """
    monkeypatch.delenv("SWARM_PROJECT", raising=False)


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
def _console_off(monkeypatch):
    """The owner console is on by default in a real project; in the suite it is
    off, for the Overseer's reason: its window runs a real ``claude`` unless
    ``SWARM_CONSOLE_CMD`` names a fake. ``tests/test_console.py`` turns it on."""
    monkeypatch.setenv("SWARM_CONSOLE", "0")
    monkeypatch.delenv("SWARM_CONSOLE_CMD", raising=False)


@pytest.fixture(autouse=True)
def _big_picture_off(monkeypatch):
    """The big-picture pass is on by default in a real project; in the suite it is
    off, for the Overseer's reason: a pass is a real ``claude`` session unless
    ``SWARM_BIG_PICTURE_CMD`` names a fake. ``tests/test_bigpic.py`` turns it on."""
    monkeypatch.setenv("SWARM_BIG_PICTURE_EVERY", "0")
    monkeypatch.delenv("SWARM_BIG_PICTURE_MAX_AGE_H", raising=False)
    monkeypatch.delenv("SWARM_BIG_PICTURE_CMD", raising=False)


@pytest.fixture(autouse=True)
def _gc_auto_off(monkeypatch):
    """Automatic gc is on by default in a real project; in the suite it is off,
    so no supervisor under test starts a gc thread walking /proc and the temp
    tree. ``tests/test_gc.py`` turns it back on where it is the subject."""
    monkeypatch.setenv("SWARM_GC_AUTO", "0")


@pytest.fixture(autouse=True)
def _usage_caps_off(monkeypatch):
    """Usage caps are on by default in a real project; in the suite they are off,
    so no supervisor under test reads the owner's real limits or calls the usage
    endpoint with their login. ``tests/test_caps.py`` turns them back on with the
    endpoint mocked."""
    from swarm_orchestrator import caps

    monkeypatch.setenv("SWARM_USAGE", "0")
    # Belt and braces: an in-process check that does run finds no login to send.
    monkeypatch.setattr(caps, "CREDENTIALS", Path("/nonexistent/.credentials.json"))
    # Nor any account: samples are tagged, and read, as if logged out.
    monkeypatch.setenv("SWARM_CLAUDE_JSON", "/nonexistent/.claude.json")


@pytest.fixture(autouse=True)
def _backup_off(monkeypatch):
    """Backup pushes are on by default in a real project; in the suite they are
    off, so no supervisor or ``swarm down`` under test pushes anywhere.
    ``tests/test_backup.py`` calls them directly against local bare remotes."""
    monkeypatch.setenv("SWARM_BACKUP_EVERY", "0")
    monkeypatch.setenv("SWARM_BACKUP_ON_DOWN", "0")


@pytest.fixture(autouse=True)
def _resources_off(monkeypatch):
    """The resource sampler is on by default in a real project; in the suite it
    is off, so no supervisor under test samples /proc every second or runs
    ``du`` over the temp tree. ``tests/test_resources_*.py`` turn it back on."""
    monkeypatch.setenv("SWARM_RESOURCES", "0")


@pytest.fixture(autouse=True)
def _web_off(monkeypatch):
    """The web board is on by default in a real project; in the suite it is off.

    Every end-to-end ``swarm up`` would otherwise start a server on the fixed
    default port, and two tests (or the owner's live board) would fight over it.
    ``tests/test_web_lifecycle.py`` turns it back on, on a free port.
    """
    monkeypatch.setenv("SWARM_WEB", "0")


@pytest.fixture(autouse=True)
def _tg_bot_off(monkeypatch):
    """The bot's command listener is on by default in a real project; in the
    suite it is off, so no ``swarm up`` long-polls Telegram. ``tests/test_tgbot.py``
    turns it back on against a fake API on loopback."""
    monkeypatch.setenv("SWARM_TG_COMMANDS", "0")


@pytest.fixture(autouse=True)
def _no_scopes(monkeypatch):
    """Nothing the suite starts gets a systemd scope of its own.

    ``swarm up`` starts the supervisor, the bot, a headless board and each lane
    check through :func:`freezer.scoped`, which asks systemd for a scope where
    it can. The suite must not: it would leave units on the user's manager for
    every test. The switch is the documented one, and the runtime dir the probe
    looks for goes too. Set in the environment, so the ``swarm`` fixture's copy
    of it carries both. ``tests/test_freeze.py`` tests the seam with the probe
    stubbed, and asks for one real scope, around a command that ends at once."""
    monkeypatch.setenv("SWARM_SCOPE", "0")
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)


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


#: What a ``swarm`` under test must not inherit from the shell that runs the suite.
_LEAKS = (
    "SWARM_DRIVER",
    "SWARM_MASTER_CMD",
    "SWARM_WORKER_CMD",
    "SWARM_READY_MARKER",
    "SWARM_SESSION",
    "SWARM_SESSION_ID",
    "SWARM_SLUG",
    "SWARM_LAYOUT",
)


def _fake_env(tg: Path, **extra: str) -> dict[str, str]:
    """This environment, for a swarm of fakes that pings into ``tg``."""
    env = {k: v for k, v in os.environ.items() if k not in _LEAKS}
    env.update({"SWARM_TG_SINK": str(tg), "SWARM_BIN": SWARM_BIN,
                "FAKE_WORKER_SLEEP": "1", "FAKE_MASTER_WAIT": "1", **extra})
    return env


def _end(inst: Swarm) -> None:
    """Stop ``inst`` whatever state the test left it in."""
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
    _kill_orphan_fakes(inst.state_dir)  # insurance net, scoped to THIS run


@pytest.fixture
def swarm(tmp_path: Path):
    project = tmp_path / "project"
    shutil.copytree(DEMO, project)
    state_dir = tmp_path / "state"
    env = _fake_env(tmp_path / "tg.log", SWARM_STATE_DIR=str(state_dir), SWARM_SLUG="test")
    inst = Swarm(project, state_dir, env)
    try:
        yield inst
    finally:
        _end(inst)


@pytest.fixture
def two_swarms(tmp_path: Path):
    """Two projects side by side on one machine, as two real swarms are: one
    state root (``XDG_STATE_HOME``), and each run in the state dir its own path
    gives it — no ``SWARM_STATE_DIR``, no ``SWARM_SLUG``. Neither is up."""
    from swarm_orchestrator.config import _default_slug

    root = Path(os.environ["XDG_STATE_HOME"])
    pair = []
    for name in ("alpha", "beta"):
        project = tmp_path / name
        shutil.copytree(DEMO, project)
        env = _fake_env(tmp_path / f"tg-{name}.log")
        env.pop("SWARM_STATE_DIR", None)
        pair.append(Swarm(project, root / "swarm-orchestrator" / _default_slug(project), env))
    try:
        yield tuple(pair)
    finally:
        for inst in pair:
            _end(inst)


class FakeCgroups:
    """A cgroup tree and a ``/proc`` of the test's own, and a thread that plays
    the kernel in them, for :class:`swarm_orchestrator.freezer.Cgroups`.

    Every process that carries the test's ``SWARM_STATE_DIR`` is given a group:
    one per ``SWARM_SESSION_ID`` (``/run/<kind>-<id>``), ``/supervisor``,
    ``/bot`` and ``/board`` for the run's own processes, and ``/login`` for the
    rest, the caller of a ``swarm`` command among them (``<proc>/self``).
    :meth:`place` puts any other process where a test wants it. A write to a
    group's ``cgroup.freeze`` shows in its ``cgroup.events`` a moment later,
    unless the group is in :attr:`stuck`; :attr:`writes` lists every change the
    kernel saw, in order. Nothing is really frozen."""

    TICK_S = 0.02

    def __init__(self, base: Path, state_dir: Path) -> None:
        self.root = base / "cgroup"
        self.proc = base / "proc"
        self.stuck: set[str] = set()
        self.writes: list[tuple[float, str, str]] = []
        self._want = f"SWARM_STATE_DIR={state_dir}".encode()
        self._placed: dict[int, str] = {}
        self._where: dict[int, str] = {}
        self._seen: dict[str, str] = {}
        self._scans = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-kernel", daemon=True)
        (self.proc / "self").mkdir(parents=True)
        (self.proc / "self" / "cgroup").write_text("0::/login\n")
        self.group("/login")

    @property
    def env(self) -> dict[str, str]:
        return {"SWARM_CGROUP_ROOT": str(self.root), "SWARM_CGROUP_PROC": str(self.proc)}

    def group(self, path: str) -> Path:
        """Make the group ``path`` (awake) if it is not there; return its directory."""
        d = self.root / path.lstrip("/")
        if not (d / "cgroup.freeze").is_file():
            d.mkdir(parents=True, exist_ok=True)
            (d / "cgroup.events").write_text("populated 1\nfrozen 0\n")
            (d / "cgroup.freeze").write_text("0\n")
        return d

    def place(self, pid: int, path: str) -> None:
        """Put ``pid`` in ``path``, whatever it carries."""
        self._placed[pid] = path
        self._put(pid, path)

    def lock(self, path: str) -> None:
        """Make ``path`` a group the test's user may not write, the way a scope
        root started is to everybody else."""
        (self.group(path) / "cgroup.freeze").chmod(0o444)

    def told(self, path: str) -> str:
        """What ``path``'s ``cgroup.freeze`` reads: ``"1"`` or ``"0"``."""
        return (self.root / path.lstrip("/") / "cgroup.freeze").read_text().strip()

    def groups(self) -> list[str]:
        return sorted("/" + str(f.parent.relative_to(self.root))
                      for f in self.root.rglob("cgroup.freeze"))

    def settle(self, timeout: float = 10.0) -> None:
        """Wait until the kernel has looked at every process twice more, so
        whatever started before the call is in its group."""
        target = self._scans + 2
        deadline = time.monotonic() + timeout
        while self._scans < target and time.monotonic() < deadline:
            time.sleep(self.TICK_S)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)

    # -- the kernel ---------------------------------------------------------
    def _put(self, pid: int, path: str) -> None:
        if self._where.get(pid) == path:
            return
        self.group(path)
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "cgroup.tmp"
        tmp.write_text(f"0::{path}\n")
        os.replace(tmp, d / "cgroup")
        self._where[pid] = path

    def _group_of(self, pid: int) -> str | None:
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            if self._want not in env:
                return None
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except OSError:
            return None
        for entry in env:
            if entry.startswith(b"SWARM_SESSION_ID="):
                return "/run/" + entry.split(b"=", 1)[1].decode().replace(":", "-")
        if b" _supervise" in cmd:
            return "/supervisor"
        if b" telegram-bot" in cmd:
            return "/bot"
        if b" web" in cmd:
            return "/board"
        return "/login"

    def _scan(self) -> None:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            path = self._placed.get(pid) or self._group_of(pid)
            if path is not None:
                self._put(pid, path)

    def _mirror(self) -> None:
        for path in self.groups():
            try:
                told = self.told(path)
            except OSError:
                continue
            if told not in ("0", "1") or told == self._seen.get(path, "0"):
                continue
            self._seen[path] = told
            self.writes.append((time.monotonic(), path, told))
            if path not in self.stuck:
                d = self.root / path.lstrip("/")
                (d / "cgroup.events").write_text(f"populated 1\nfrozen {told}\n")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._scan()
                self._mirror()
            except OSError:
                pass  # a process or a temp dir went mid-look
            self._scans += 1
            self._stop.wait(self.TICK_S)


@pytest.fixture
def fake_cgroups(request, tmp_path: Path, monkeypatch):
    """Point the freezer's cgroup seam at a tree of the test's own
    (:class:`FakeCgroups`). Ask for it after ``swarm`` in an end-to-end test:
    the harness's environment is given the seam's two variables too, and
    anything still frozen is thawed before the harness tears the run down."""
    fake = FakeCgroups(tmp_path / "kernel", tmp_path / "state")
    for key, value in fake.env.items():
        monkeypatch.setenv(key, value)
    swarm = request.getfixturevalue("swarm") if "swarm" in request.fixturenames else None
    if swarm is not None:
        swarm.env.update(fake.env)
    fake.start()
    try:
        yield fake
    finally:
        if swarm is not None:
            swarm.cli("thaw", "--gap", "0", check=False)
        fake.stop()
