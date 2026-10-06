"""The board's lifecycle: one for the machine. Any ``swarm up`` starts it when
it is not answering, a second ``up`` starts no second one, it outlives the
``down`` of one swarm while another is up, and the last ``down`` stops it;
``status``/``doctor``/``ls`` say where it is. The suite keeps swarms off the
board by default (conftest ``_web_off``); these tests turn it on, on a free port.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import machine_toml

from swarm_orchestrator import machine
from swarm_orchestrator import service as service_mod
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import tmux
from swarm_orchestrator.config import load
from swarm_orchestrator.web import lifecycle
from swarm_orchestrator.web import server as web_server

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _wait(pred, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def _json(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read())


def _on(port: int, *swarms) -> None:
    """Put these swarms on the board, which their machine file says listens on
    ``port`` of loopback."""
    for inst in swarms:
        inst.env["SWARM_WEB"] = "1"
        machine_toml(inst.env, web={"host": "127.0.0.1", "port": port})


def _pid(inst) -> int:
    return int((inst.state_dir.parent / "machine" / "web.pid").read_text())


@pytest.fixture
def at(tmp_path, monkeypatch) -> lifecycle.Place:
    """Where the board of this test's own state root would be, on a free port."""
    machine_toml(web={"host": "127.0.0.1", "port": _free_port()})
    return lifecycle.place()


@pytest.fixture
def squatter():
    """Start another program on a port: ``squatter(port)``."""
    procs = []

    def start(port: int) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(proc)
        assert _wait(lambda: _listening(port)), "the squatter never bound"
        return proc

    yield start
    for proc in procs:
        proc.terminate()
        proc.wait(timeout=5)


# -- one swarm ------------------------------------------------------------------------
def test_up_starts_the_board_and_the_last_down_stops_it(swarm):
    port = _free_port()
    _on(port, swarm)
    out = swarm.up()
    assert f"web board: http://127.0.0.1:{port}/" in out.stdout
    assert f"this swarm: http://127.0.0.1:{port}/s/state/" in out.stdout
    try:
        assert _wait(lambda: _listening(port)), "the board never came up"
        board = _json(port, "/s/state/api/board")
        assert {c["key"] for c in board["columns"]} >= {"ready", "done", "blocked"}
        pid = _pid(swarm)
        # Started with no swarm's environment: what it shows is on its command line.
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        assert not [e for e in environ if e.startswith(b"SWARM_")]
        status = swarm.cli("status").stdout
        assert (f"web: http://127.0.0.1:{port}/ (listening) — this swarm:"
                f" http://127.0.0.1:{port}/s/state/") in status
        doctor = swarm.cli("doctor", "--json", check=False).stdout
        web = next(c for c in json.loads(doctor) if c["name"] == "web.board")
        assert web["status"] == "ok" and "listening" in web["detail"] and "/s/state/" in web["detail"]
        ls = swarm.cli("ls").stdout
        assert f"web board: http://127.0.0.1:{port}/ (listening, pid {pid})" in ls
    finally:
        swarm.down()
    assert _wait(lambda: not _listening(port)), "the last down left the board running"
    assert not (swarm.state_dir.parent / "machine" / "web.pid").exists()
    assert not Path(f"/proc/{pid}/cmdline").exists() or b"web" not in Path(
        f"/proc/{pid}/cmdline").read_bytes()


def test_the_board_is_not_started_for_a_swarm_that_is_off_it(swarm):
    port = _free_port()
    swarm.env["SWARM_WEB"] = "0"
    machine_toml(swarm.env, web={"host": "127.0.0.1", "port": port})
    swarm.up()
    at = lifecycle.Place(swarm.state_dir.parent, "127.0.0.1", port)
    try:
        time.sleep(1.0)
        # Not "nothing listens there": on a shared box another program may have
        # taken the port since. No board of this machine does.
        assert lifecycle.running(at) is None and lifecycle.probe(at).state != lifecycle.OURS
        assert not (at.mdir / "web.pid").exists()
        assert "web: off for this swarm" in swarm.cli("status").stdout
    finally:
        swarm.down()


# -- two swarms, one board ------------------------------------------------------------
def test_two_swarms_share_one_board_until_the_last_goes_down(two_swarms):
    a, b = two_swarms
    port = _free_port()
    _on(port, a, b)
    for inst in (a, b):
        inst.env["FAKE_WORKER_PARK"] = "1"  # a worker that waits: neither run ends by itself
    slug = {inst: inst.state_dir.name for inst in (a, b)}
    first = a.up()
    assert f"this swarm: http://127.0.0.1:{port}/s/{slug[a]}/" in first.stdout
    assert _wait(lambda: _listening(port)), "the board never came up"
    pid = _pid(a)
    try:
        # A second `swarm up` starts no second server: it finds the first.
        second = b.up()
        assert f"web board: http://127.0.0.1:{port}/" in second.stdout
        assert f"this swarm: http://127.0.0.1:{port}/s/{slug[b]}/" in second.stdout
        assert "FAILED" not in second.stderr
        at = lifecycle.Place(machine.state_root(), "127.0.0.1", port)
        assert _pid(b) == pid and lifecycle.probe(at).pid == pid
        boards = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()
                  and service_mod.is_process(lifecycle.the_service(at), int(p.name))
                  and str(at.root).encode() in (p / "cmdline").read_bytes()]
        assert boards == [pid]

        def shown() -> dict:
            return {s["name"]: s for s in _json(port, "/api/machine")["swarms"]}

        assert _wait(lambda: set(shown()) == {"alpha", "beta"}
                     and all(s["up"] for s in shown().values())), shown()
        for inst, name in ((a, "alpha"), (b, "beta")):
            assert _json(port, f"/s/{slug[inst]}/api/board")["project"] == name
        for inst in (a, b):
            assert f"/s/{slug[inst]}/" in inst.cli("status").stdout

        # One swarm goes down; the other is up, so the board stays, same process.
        a.down()
        assert _listening(port) and _pid(b) == pid
        assert _wait(lambda: (shown()["alpha"]["word"], shown()["beta"]["up"]) == ("down", True))
        assert _json(port, f"/s/{slug[a]}/api/board")["header"]["running"] is False
    finally:
        b.down()
    assert _wait(lambda: not _listening(port)), "the last down left the board running"
    assert not Path(f"/proc/{pid}/cmdline").exists() or b"web" not in Path(
        f"/proc/{pid}/cmdline").read_bytes()


# -- by hand: `swarm web` -------------------------------------------------------------
def _web(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "swarm_orchestrator", "web", *args],
                          capture_output=True, text=True, timeout=60)


def test_swarm_web_starts_says_and_stops_the_board_by_hand(at, tmp_path):
    """A machine command: it needs no project, and what it starts shows every
    swarm, the ones that are down too."""
    try:
        r = _web("status")
        assert r.returncode == 0 and "not running" in r.stdout
        r = _web()  # the same as `swarm web start`
        assert r.returncode == 0, r.stderr
        assert f"web board: http://127.0.0.1:{at.port}/ (listening, pid " in r.stdout
        pid = lifecycle.running(at)
        assert pid and lifecycle.probe(at).pid == pid
        assert _web("start").returncode == 0 and lifecycle.running(at) == pid  # not a second one
        assert _json(at.port, "/api/machine")["swarms"] == []
        assert f"pid {pid}" in _web("status").stdout
    finally:
        r = _web("stop")
    assert "stopped" in r.stdout and lifecycle.probe(at).state == lifecycle.CLOSED
    assert _web("stop").stdout.strip() == "web board: was not running"


def test_a_board_started_in_the_foreground_is_found_left_alone_and_stopped(at):
    """`swarm web serve` writes no pid file; the port says it is ours."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarm_orchestrator", "web", "serve"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _wait(lambda: lifecycle.probe(at).state == lifecycle.OURS)
        assert lifecycle.running(at) is None and lifecycle.probe(at).pid == proc.pid
        assert lifecycle.ensure(at).state == lifecycle.OURS  # found, not doubled
        assert lifecycle.running(at) is None
        assert lifecycle.stop(at) is True
        assert proc.wait(timeout=10) is not None
    finally:
        proc.kill()
    assert lifecycle.probe(at).state == lifecycle.CLOSED


def test_a_restart_replaces_the_board_and_starts_one_that_was_not_running(at):
    """`swarm restart` starts the helpers again so they run the code on disk; a
    swarm that is on the board has one afterwards, whatever was there before."""
    svc = lifecycle.the_service(at)
    try:
        assert lifecycle.restart(at) is True  # none was running
        first = lifecycle.running(at)
        assert first and lifecycle.probe(at).pid == first
        assert lifecycle.restart(at) is True
        second = lifecycle.running(at)
        assert second and second != first and not service_mod.is_process(svc, first)
        assert lifecycle.probe(at).pid == second
    finally:
        lifecycle.stop(at)
    assert lifecycle.probe(at).state == lifecycle.CLOSED


# -- the reusable half: a machine service ---------------------------------------------
def test_a_machine_service_starts_once_and_stops_with_the_last_swarm(at, tmp_path):
    svc = lifecycle.the_service(at)
    root = at.root
    try:
        pid, started = service_mod.start(svc, at.mdir)
        assert started and service_mod.running(svc, at.mdir) == pid
        assert service_mod.is_process(svc, pid) and not service_mod.is_process(svc, os.getpid())
        assert service_mod.start(svc, at.mdir) == (pid, False)  # there already
        assert service_mod.pidfile(svc, at.mdir) == root / "machine" / "web.pid"
        assert service_mod.logfile(svc, at.mdir).is_file()

        # Two swarms are up (each holds its control FIFO open, as a supervisor does).
        fds = {}
        for name in ("one", "two"):
            (root / name / "logs").mkdir(parents=True)
            os.mkfifo(root / name / "control.fifo")
            fds[name] = os.open(root / name / "control.fifo", os.O_RDONLY | os.O_NONBLOCK)
        assert service_mod.others_running(at.mdir) == [root / "one", root / "two"]
        assert service_mod.others_running(at.mdir, root / "one") == [root / "two"]
        # One goes down: the other still needs the service.
        assert service_mod.stop_with_last(svc, at.mdir, leaving=root / "one") is False
        assert service_mod.running(svc, at.mdir) == pid
        os.close(fds.pop("one"))
        # The last one goes down, and its own supervisor may still be closing.
        assert service_mod.stop_with_last(svc, at.mdir, leaving=root / "two") is True
        assert service_mod.running(svc, at.mdir) is None and not service_mod.is_process(svc, pid)
        assert service_mod.stop_with_last(svc, at.mdir) is False  # a swarm is still up
        os.close(fds.pop("two"))
        assert service_mod.stop(svc, at.mdir) is False  # nothing left to stop
    finally:
        service_mod.stop(svc, at.mdir)


def test_a_recycled_pid_is_not_the_service(at):
    svc = lifecycle.the_service(at)
    at.mdir.mkdir(parents=True)
    service_mod.pidfile(svc, at.mdir).write_text(f"{os.getpid()}\n")  # ours, and not a board
    assert service_mod.running(svc, at.mdir) is None
    assert service_mod.stop(svc, at.mdir) is False  # and it is not signalled
    assert not service_mod.pidfile(svc, at.mdir).exists()


def test_a_machine_service_carries_no_swarms_environment(monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", "/somewhere/state")
    monkeypatch.setenv("SWARM_SLUG", "one-of-them")
    env = service_mod.clean_env()
    assert not [k for k in env if k.startswith("SWARM_")]
    assert env["PATH"] == os.environ["PATH"] and "XDG_STATE_HOME" in env


def test_the_command_is_this_interpreter_and_says_everything(at):
    argv = lifecycle.the_service(at).argv()
    assert argv[:3] == [sys.executable, "-m", "swarm_orchestrator"]
    assert argv[3:] == ["web", "serve", "--state-root", str(at.root), "--host", "127.0.0.1",
                        "--port", str(at.port)]


# -- the address ----------------------------------------------------------------------
def test_status_line_when_nothing_listens(tmp_path, monkeypatch, at):
    monkeypatch.setenv("SWARM_WEB", "1")
    cfg = load(project_dir=str(tmp_path))
    assert f"not listening on :{at.port}" in lifecycle.status_line(cfg)
    assert lifecycle.stop(at) is False  # no pid file: nothing to stop
    assert "not running" in lifecycle.machine_line(at)


def test_a_machine_file_that_does_not_read_is_said_not_raised(tmp_path, monkeypatch, capsys, at):
    """A project's command stops at the file (`load`); the machine's own
    commands, which load no project, say what is wrong with it; and a process
    that read it while it was sound goes on with what it read."""
    from swarm_orchestrator import config as config_mod
    from swarm_orchestrator.cli import main as cli_main

    monkeypatch.setenv("SWARM_WEB", "1")
    cfg = load(project_dir=str(tmp_path))
    path = machine.settings_path()
    path.write_text("[web]\nprot = 8765\n")
    assert f"not listening on :{at.port}" in lifecycle.status_line(cfg)  # a dashboard, say
    monkeypatch.setattr(config_mod, "_machine_read", None)  # a command started now
    for line in (lifecycle.status_line(cfg), lifecycle.machine_line()):
        assert "[web].prot is not a machine setting" in line and str(path) in line
    assert cli_main(["ls"]) == 0
    assert cli_main(["web", "status"]) == 2 and "[web].prot" in capsys.readouterr().err
    with pytest.raises(machine.SettingsError):
        load(project_dir=str(tmp_path))


def test_lan_ips_are_never_loopback_or_container_bridges():
    for ip in lifecycle.lan_ips():
        assert not ip.startswith("127.") and not ip.startswith("172.17.")


def test_the_address_is_the_tailscale_ip_when_tailscale_runs(monkeypatch):
    at = lifecycle.Place(Path("/x"), "0.0.0.0", 8765)
    monkeypatch.setattr(lifecycle, "lan_ips", lambda: ["192.168.1.10"])
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 0, stdout="100.66.77.71\n", stderr=""))
    assert lifecycle.urls(at) == ["http://100.66.77.71:8765/"]
    assert lifecycle.link(lifecycle.url(at), "magnar-ce42f2c1") == (
        "http://100.66.77.71:8765/s/magnar-ce42f2c1/")


def test_the_address_falls_back_to_the_lan_without_tailscale(monkeypatch):
    at = lifecycle.Place(Path("/x"), "0.0.0.0", 8765)
    monkeypatch.setattr(lifecycle, "lan_ips", lambda: ["192.168.1.10"])

    def missing(*a, **k):
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr(lifecycle.subprocess, "run", missing)
    assert lifecycle.urls(at) == ["http://192.168.1.10:8765/"]
    # Installed but logged out: `tailscale ip -4` fails, and prints no address.
    monkeypatch.setattr(lifecycle.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], 1, stdout="", stderr="Tailscale is stopped."))
    assert lifecycle.urls(at) == ["http://192.168.1.10:8765/"]
    assert lifecycle.urls(lifecycle.Place(Path("/x"), "10.0.0.5", 9000)) == ["http://10.0.0.5:9000/"]


def test_where_the_board_is_comes_from_the_machine_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_WEB_PORT", "9000")  # no longer read: one address for all
    assert (lifecycle.place().host, lifecycle.place().port) == ("0.0.0.0", 8765)
    path = machine_toml(web={"host": "127.0.0.1", "port": 8780})
    at = lifecycle.place()
    assert (at.host, at.port, at.local) == ("127.0.0.1", 8780, "127.0.0.1")
    assert at.root == machine.state_root() and at.mdir == machine.directory()
    # A swarm's own state dir names the same machine: its root's.
    assert lifecycle.place(tmp_path / "elsewhere" / "state").root == tmp_path / "elsewhere"
    # An edit is followed without a restart; one that does not read keeps
    # what this process read while the file was sound.
    machine_toml(web={"port": 8790})
    assert lifecycle.place().port == 8790
    path.write_text("[web]\nport = 0\n")
    assert lifecycle.place().port == 8790


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not available")
def test_tmux_up_opens_no_web_window(monkeypatch, tmp_path):
    """The board is the machine's, a process of its own: no window, and setting a
    session up starts none."""
    project = tmp_path / "project"
    shutil.copytree(DEMO, project)
    port = _free_port()
    for k, v in {"SWARM_STATE_DIR": str(tmp_path / "state"), "SWARM_DRIVER": "tmux",
                 "SWARM_SESSION": f"swarm-web-{os.getpid()}", "SWARM_SLUG": "webtest",
                 "SWARM_TUI_AUTOSTART": "0", "SWARM_WEB": "1"}.items():
        monkeypatch.setenv(k, v)
    machine_toml(web={"host": "127.0.0.1", "port": port})
    cfg = load(project_dir=str(project))
    try:
        session_mod.setup(cfg)
        names = [n for n in tmux.run(["list-windows", "-t", cfg.session, "-F",
                                      "#{window_name}"]).stdout.split("\n") if n]
        # Every index the owner knows stays put, and nothing comes after them.
        assert names == ["dash", "overseer", "operator", "workers"]
        assert "web" not in state_mod.read(cfg).windows
        time.sleep(0.5)
        assert not _listening(port), "setup started a board on its own"
    finally:
        session_mod.teardown(cfg)


# -- what answers on the port ---------------------------------------------------------
def test_probe_tells_ours_taken_and_closed_apart(at, squatter):
    """A plain connect-and-see cannot tell our board from a stray
    ``python3 -m http.server`` that got the port first: both just "accept a
    connection". ``probe`` must, by checking ``/healthz``."""
    assert lifecycle.probe(at) == lifecycle.Found(lifecycle.CLOSED)

    proc = squatter(at.port)
    found = _wait_state(at, lifecycle.TAKEN)
    assert found.state == lifecycle.TAKEN
    # ``ss`` may be unavailable; with it the program is named.
    assert found.holder in ("another program", f"python (pid {proc.pid})",
                            f"python3 (pid {proc.pid})") or f"pid {proc.pid}" in found.holder
    assert "[web].port" in found.fix and str(machine.settings_path()) in found.fix
    line = lifecycle.machine_line(at)
    assert f"port :{at.port} is held by" in line and "not this machine's board" in line
    proc.terminate()
    proc.wait(timeout=5)
    assert _wait(lambda: not _listening(at.port)), "the squatter outlived its turn"

    srv = web_server.make_server(at.root, "127.0.0.1", at.port)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        assert lifecycle.probe(at) == lifecycle.Found(lifecycle.OURS, pid=os.getpid())
    finally:
        # ``close`` calls ``shutdown()``, which blocks forever unless
        # ``serve_forever`` is actually running to notice the request.
        web_server.close(srv)


def _wait_state(at, want: str, timeout: float = 5.0) -> lifecycle.Found:
    deadline = time.monotonic() + timeout
    found = lifecycle.probe(at)
    while found.state != want and time.monotonic() < deadline:
        time.sleep(0.1)
        found = lifecycle.probe(at)
    return found


class _Health(http.server.BaseHTTPRequestHandler):
    """A listener that answers ``/healthz`` with a body of the test's choosing."""

    body: dict = {}

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        data = json.dumps(self.body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass


@pytest.mark.parametrize("body, holder, fix", [
    # A board from before boards were one per machine names one swarm.
    ({"app": "swarm-web", "project": "glasheim", "slug": "glasheim-c1d7fde6"},
     "the board of the swarm 'glasheim', started before boards were one per machine",
     "restart that swarm (`swarm down`, then `swarm up`, in its project)"),
    # Another state root's board (another user's, or a run moved by hand).
    ({"app": "swarm-web", "machine": "/home/other/.local/state/swarm-orchestrator", "pid": 7},
     "the board of another state root (/home/other/.local/state/swarm-orchestrator)",
     "set [web].port in"),
    ({"app": "something-else"}, "", "set [web].port in"),
])
def test_a_taken_port_says_whose_it_is_and_what_to_change(at, body, holder, fix):
    handler = type("Handler", (_Health,), {"body": body})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", at.port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        found = lifecycle.probe(at)
        assert found.state == lifecycle.TAKEN and holder in found.holder and found.holder
        assert fix in found.fix and "machine.toml" in found.fix
        assert not lifecycle.is_ours(at, body)
        # Nothing is started against a port that is taken: it could only fail to bind.
        assert lifecycle.ensure(at).state == lifecycle.TAKEN and lifecycle.running(at) is None
    finally:
        srv.shutdown()
        srv.server_close()


def test_only_the_board_of_this_state_root_is_ours(at):
    assert lifecycle.is_ours(at, lifecycle.health_body(at.root, 1))
    assert not lifecycle.is_ours(at, lifecycle.health_body(at.root / "other", 1))
    assert not lifecycle.is_ours(at, {"app": "swarm-web", "slug": "x", "project": "x"})
    assert not lifecycle.is_ours(at, ["not", "a", "board"])


def test_up_reports_and_logs_when_the_port_is_taken(swarm, squatter):
    """A board that cannot bind used to fail silently. ``up`` itself says so, on
    stderr and in the notification log, naming the machine file to change."""
    port = _free_port()
    _on(port, swarm)
    squatter(port)
    try:
        out = swarm.up()
        assert "web board: FAILED to start" in out.stderr
        assert f"port :{port} is held by" in out.stderr and "machine.toml" in out.stderr
        assert not (swarm.state_dir.parent / "machine" / "web.pid").exists()
        assert f"port :{port} is held by" in swarm.cli("status").stdout
        # Printed by `up`, logged for the dashboard, but not a phone ping: the
        # board is not the run (`[telegram].pings = "necessary"`).
        assert not any("the web board did not start" in ln for ln in swarm.tg_lines())
        rows = [json.loads(ln) for ln in
                (swarm.state_dir / "notifications.jsonl").read_text().splitlines() if ln]
        assert any(r["kind"] == "web-board" and r.get("suppressed") and f"port :{port}" in r["text"]
                   for r in rows), rows
    finally:
        swarm.down()


_SILENT_LISTENER = (
    "import socket, sys, time\n"
    "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind(('127.0.0.1', int(sys.argv[-1]))); s.listen(5)\n"
    "time.sleep(60)\n"
)


@pytest.mark.skipif(shutil.which("ss") is None, reason="ss not available")
def test_probe_calls_its_own_board_ours_while_it_is_still_starting(at):
    """``swarm up`` must not report "port :PORT is held by python (pid 4242)"
    when 4242 is the board that same ``up`` has just started. The server binds
    before its request loop runs, so for a moment the port accepts a connection
    nobody answers. The process this machine started (its pid file says so) is
    ours; the same silent listener started by anything else is not."""

    def board_lookalike() -> subprocess.Popen:
        # The argv `the_service` builds, on a listener that never answers.
        proc = subprocess.Popen(
            [sys.executable, "-c", _SILENT_LISTENER, "swarm_orchestrator",
             "web", "serve", "--port", str(at.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert _wait(lambda: _listening(at.port)), "the listener never bound"
        return proc

    proc = board_lookalike()
    try:
        found = lifecycle.probe(at)
        assert found.state == lifecycle.TAKEN and f"pid {proc.pid}" in found.holder
        at.mdir.mkdir(parents=True, exist_ok=True)
        service_mod.pidfile(lifecycle.the_service(at), at.mdir).write_text(f"{proc.pid}\n")
        assert lifecycle.probe(at) == lifecycle.Found(lifecycle.OURS, pid=proc.pid)
    finally:
        proc.kill()
        proc.wait(timeout=5)
    assert _wait(lambda: not _listening(at.port)), "the listener outlived its turn"
