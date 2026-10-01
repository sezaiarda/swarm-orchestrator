"""Starting, stopping and finding the web board.

``swarm up`` starts the board beside the run and ``swarm down`` stops it; the
owner then needs one thing from ``swarm status`` / ``doctor``: *the address to
type into the phone*. So this module owns the command line the board is started
with, the pid file it leaves, the listening probe, and the address the owner is
given: the machine's Tailscale IP, or its LAN addresses when Tailscale is absent.

Under the tmux driver the dashboard serves the board from its own process
(:mod:`swarm_orchestrator.tui.webboard`), so it lives and dies with the
dashboard pane. With no dashboard (the headless ``bare`` driver, or
``[tui] autostart`` off) ``up`` starts it as a detached process, which writes
its pid to ``<state>/web.pid`` — only when started with ``--pidfile``, which
only ``up`` passes: a ``swarm web`` run by hand against someone's live state
dir writes nothing into it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PIDFILE = "web.pid"
LOG = "web.log"
#: Must match :data:`swarm_orchestrator.web.server.APP_ID` — the string
#: ``/healthz`` answers with when the listener is our own board.
APP_ID = "swarm-web"
#: :func:`probe` outcomes: our board, some other program on the port, or
#: nothing listening at all. A plain connect-and-see (:func:`listening`) cannot
#: tell the first two apart — that gap is what let a stray
#: ``python3 -m http.server`` on :8765 read as "(listening)" in ``swarm status``
#: while the board's own pane had died with "Address already in use".
OURS = "ours"
TAKEN = "taken"
CLOSED = "closed"
#: The detail :func:`probe` gives with :data:`OURS` when the listener is this
#: run's dashboard, which serves the board from its own process.
DASHBOARD = "the dashboard is hosting it"
#: A dashboard this young that holds the port without answering is still
#: starting; an older one that stays silent is a board that hangs.
DASHBOARD_BOOT_S = 120.0
#: Interfaces that are never the LAN: container bridges and virtual links. A
#: phone cannot reach 172.17.0.1, and printing it as "the address" would send
#: the owner to a dead URL first.
_VIRTUAL = re.compile(r"^(lo|docker\d*|br-|veth|virbr|cni|flannel|tun|tap|zt|tailscale)")


def pidfile(cfg) -> Path:
    return Path(cfg.state_dir) / PIDFILE


def display_name(cfg) -> str | None:
    """What the board calls the swarm: ``[swarm].name``, by default the project
    folder's name. None for a config that carries neither."""
    name = getattr(cfg, "name", None)
    if name:
        return str(name)
    pdir = getattr(cfg, "project_dir", None)
    return Path(pdir).name if pdir else None


def is_ours(cfg, body: object) -> bool:
    """Whether a ``/healthz`` answer is from a board of this project.

    A board says which project it serves by its slug, which a change of
    ``[swarm].name`` does not move (and which two projects never share, as they
    may a name). A board started before boards said so answers only
    ``project``, always the folder's name: it is still recognised by that, so
    it is found and stopped with its run, never left serving beside a new one.
    """
    if not isinstance(body, dict) or body.get("app") != APP_ID:
        return False
    if "slug" in body:
        return body["slug"] == cfg.slug
    return body.get("project") == Path(cfg.project_dir).name


def command(cfg) -> str:
    """The shell command that serves this project's board, as ``up`` runs it.

    The interpreter running ``swarm up`` itself, not whatever ``swarm`` is first
    on PATH: the board must be the same install as the run it describes.
    """
    return " ".join([
        shlex.quote(sys.executable), "-m", "swarm_orchestrator",
        "--project-dir", shlex.quote(str(cfg.project_dir)),
        "web", "--host", shlex.quote(cfg.web_host), "--port", str(int(cfg.web_port)),
        "--pidfile", shlex.quote(str(pidfile(cfg))),
    ])


def lan_ips() -> list[str]:
    """Non-loopback IPv4 addresses a phone on the LAN could use, best first.

    ``ip -4 -o addr`` lists every interface (under WSL's mirrored networking that
    includes the Windows host's LAN address); container bridges and tunnels are
    dropped. Without ``ip``, the address the default route would leave from is
    the one answer that is almost always right.
    """
    out: list[str] = []
    try:
        text = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                              text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        name, addr = parts[1], parts[3].split("/")[0]
        if _VIRTUAL.match(name) or addr.startswith("127.") or addr.startswith("169.254."):
            continue
        if addr not in out:
            out.append(addr)
    if not out:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))  # TEST-NET: routes, sends nothing
                addr = s.getsockname()[0]
            if not addr.startswith("127."):
                out.append(addr)
        except OSError:
            pass
    # Private LAN ranges first: a 192.168/10.x address is the one a phone uses.
    return sorted(out, key=lambda a: (not a.startswith(("192.168.", "10.", "172.")), a))


def tailscale_ip() -> str | None:
    """This machine's Tailscale IPv4 address, or ``None`` without a running Tailscale.

    The owner reaches the board over Tailscale, never the LAN. ``tailscale ip -4``
    is the right one under WSL too, where a second ``100.x`` address (the Windows
    host's, on a mirrored interface) does not reach a server inside WSL.
    """
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for word in out.stdout.split():
        try:
            return str(ipaddress.IPv4Address(word))
        except ValueError:
            continue
    return None


def urls(cfg) -> list[str]:
    """The board's address for the owner: Tailscale when this machine has it,
    else the LAN addresses (so a box without Tailscale still says something)."""
    host = cfg.web_host
    if host in ("", "0.0.0.0", "::"):
        ts = tailscale_ip()
        hosts = [ts] if ts else (lan_ips() or ["localhost"])
    else:
        hosts = [host]
    return [f"http://{h}:{int(cfg.web_port)}/" for h in hosts]


def listening(cfg, timeout: float = 0.3) -> bool:
    """Whether something accepts connections on the board's port right now.

    Cheap, but cannot tell *our* board from an unrelated process that beat it to
    the port — a plain TCP connect succeeds either way. Use :func:`probe` where
    the answer needs to be trustworthy (``status``, ``doctor``, ``up``).
    """
    host = cfg.web_host if cfg.web_host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    try:
        with socket.create_connection((host, int(cfg.web_port)), timeout=timeout):
            return True
    except OSError:
        return False


def probe(cfg, timeout: float = 0.5) -> tuple[str, str | None]:
    """What answers on the board's port right now: :data:`OURS`, :data:`TAKEN`
    (something else holds it) or :data:`CLOSED` (nothing does).

    ``GET /healthz`` and match its JSON body, rather than the connect-only check
    :func:`listening` does — a squatter that merely accepts the connection
    (another ``http.server``, say) must not read as our board. The second
    element of the pair is the occupant's ``command (pid N)`` for ``TAKEN``,
    when :func:`_occupant` can say so cheaply, and :data:`DASHBOARD` for an
    ``OURS`` that is this run's dashboard not answering yet; otherwise ``None``.
    """
    host = cfg.web_host if cfg.web_host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    port = int(cfg.web_port)
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (OSError, ValueError, urllib.error.URLError):
        if not listening(cfg, timeout):
            return CLOSED, None
        who, pid = _occupant(port)
        # The listener took the connection but did not answer /healthz in time.
        # That is not proof of a squatter: ``serve`` binds (and the kernel starts
        # accepting) before its request loop runs, so a board still starting up
        # looks exactly like this, and calling it a squatter would tell the owner
        # another program holds the port while naming its own board's pid. A
        # board of this project on the port is ours, whatever its answer speed.
        if pid is not None and _board_of(cfg, pid):
            return OURS, None
        # Under tmux the dashboard serves the board from its own process, and a
        # dashboard still painting its first screen answers late. Its command is
        # ``swarm tui``, not ``swarm web``: without this the run's own dashboard
        # was reported as "another program (swarm (pid N))" holding the port.
        if pid is not None and _dashboard_of(cfg, pid):
            return OURS, DASHBOARD
        return TAKEN, who
    if is_ours(cfg, body):
        return OURS, None
    return TAKEN, _occupant(port)[0]


def _occupant(port: int) -> tuple[str | None, int | None]:
    """``(command (pid N), N)`` holding ``port``, from ``ss -ltnp`` where that is
    cheaply available. Best-effort: no ``ss``, or an unprivileged caller ``ss``
    redacts the pid for, just means the occupant goes unnamed, not unreported."""
    try:
        out = subprocess.run(
            ["ss", "-ltnp", f"sport = :{port}"], capture_output=True, text=True, timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', out)
    if not m:
        return None, None
    return f"{m.group(1)} (pid {m.group(2)})", int(m.group(2))


def _board_of(cfg, pid: int) -> bool:
    """Is ``pid`` a board serving *this* project — ``swarm web`` for the same
    project dir, as :func:`command` (or the owner, by hand) starts it?"""
    try:
        args = [a.decode(errors="replace")
                for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
        cwd = Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return False
    if "web" not in args or not any(
        "swarm_orchestrator" in a or Path(a).name == "swarm" for a in args
    ):
        return False
    where = Path(args[args.index("--project-dir") + 1]) if "--project-dir" in args[:-1] else cwd
    try:
        return (cwd / where).resolve() == Path(cfg.project_dir).resolve()
    except OSError:
        return False


def _dashboard_of(cfg, pid: int) -> bool:
    """Is ``pid`` this run's dashboard, still starting? ``swarm up`` starts it
    with the run's ``SWARM_STATE_DIR``, and it serves the board itself
    (:mod:`swarm_orchestrator.tui.webboard`)."""
    try:
        args = [a.decode(errors="replace")
                for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False
    if "tui" not in args or not any(
        "swarm_orchestrator" in a or Path(a).name == "swarm" for a in args
    ):
        return False
    if f"SWARM_STATE_DIR={cfg.state_dir}".encode() not in env:
        return False
    try:  # how long it has been running: uptime minus its start, both since boot
        stat = Path(f"/proc/{pid}/stat").read_text()
        started = int(stat[stat.rfind(")") + 2:].split()[19]) / os.sysconf("SC_CLK_TCK")
        age = float(Path("/proc/uptime").read_text().split()[0]) - started
    except (OSError, ValueError, IndexError):
        return False
    return age < DASHBOARD_BOOT_S


def status_line(cfg) -> str:
    """One line for ``swarm status``: where the board is, and whether it answers."""
    if not cfg.web_enabled:
        return "web: off ([web] enabled = false)"
    where = " ".join(urls(cfg))
    state, detail = probe(cfg)
    if state == OURS:
        return f"web: {where} ({detail or 'listening'})"
    if state == TAKEN:
        who = f" ({detail})" if detail else " (pid unknown)"
        return (f"web: port :{cfg.web_port} is held by another program{who}, not the board — "
                "set [web].port in .swarm.toml to a free port")
    return f"web: not listening on :{cfg.web_port} — `swarm up` starts it, or run `swarm web`"


def wait_probe(cfg, timeout: float = 5.0) -> tuple[str, str | None]:
    """:func:`probe`, retried until it stops saying :data:`CLOSED` or ``timeout``
    runs out — the board's pane/process needs a moment to bind after ``up``
    starts it, so a single probe right away cannot yet distinguish "still
    starting" from "never came up"."""
    deadline = time.monotonic() + timeout
    state, detail = probe(cfg)
    while state == CLOSED and time.monotonic() < deadline:
        time.sleep(0.2)
        state, detail = probe(cfg)
    return state, detail


def start_detached(cfg) -> int | None:
    """Start the board as its own process (the ``bare`` driver). Returns its pid.

    A board already answering on the port is left alone: two would fight over
    the socket, and the second would just fail to bind.
    """
    if not cfg.web_enabled or listening(cfg):
        return None
    log_dir = Path(cfg.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SWARM_STATE_DIR": str(cfg.state_dir)}
    with (log_dir / LOG).open("ab") as log:
        proc = subprocess.Popen(
            shlex.split(command(cfg)), cwd=str(cfg.project_dir), env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    return proc.pid


def _ours(pid: int) -> bool:
    """Is ``pid`` a live board process (not a recycled pid now running something else)?"""
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True  # no /proc to check against; the pid file is all we have
    return b"swarm_orchestrator" in cmd and b" web" in cmd


def stop(cfg, timeout: float = 5.0) -> bool:
    """Stop the board ``up`` started, if it is still running. Returns whether one was."""
    path = pidfile(cfg)
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    stopped = False
    if pid > 0 and _ours(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and _ours(pid):
            time.sleep(0.05)
        if _ours(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        path.unlink()
    except OSError:
        pass
    return stopped
