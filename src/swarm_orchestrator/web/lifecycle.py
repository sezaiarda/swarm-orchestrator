"""Starting, stopping and finding the web board.

``swarm up`` starts the board beside the run and ``swarm down`` stops it; the
owner then needs one thing from ``swarm status`` / ``doctor``: *the address to
type into the phone*. So this module owns the command line the board is started
with, the pid file it leaves, the listening probe, and the machine's LAN
addresses.

Under the tmux driver the board runs in its own ``web`` window (created by
:func:`session.setup`), so it dies with the session like every other window.
Under the headless ``bare`` driver there is no session to die with, so ``up``
starts it as a detached process. Either way the board writes its pid to
``<state>/web.pid`` — but only when started with ``--pidfile``, which only
``up`` passes: a ``swarm web`` run by hand against someone's live state dir
writes nothing into it.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PIDFILE = "web.pid"
LOG = "web.log"
#: Interfaces that are never the LAN: container bridges and virtual links. A
#: phone cannot reach 172.17.0.1, and printing it as "the address" would send
#: the owner to a dead URL first.
_VIRTUAL = re.compile(r"^(lo|docker\d*|br-|veth|virbr|cni|flannel|tun|tap|zt|tailscale)")


def pidfile(cfg) -> Path:
    return Path(cfg.state_dir) / PIDFILE


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


def urls(cfg) -> list[str]:
    host = cfg.web_host
    if host in ("", "0.0.0.0", "::"):
        hosts = lan_ips() or ["localhost"]
    else:
        hosts = [host]
    return [f"http://{h}:{int(cfg.web_port)}/" for h in hosts]


def listening(cfg, timeout: float = 0.3) -> bool:
    """Whether something accepts connections on the board's port right now."""
    host = cfg.web_host if cfg.web_host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    try:
        with socket.create_connection((host, int(cfg.web_port)), timeout=timeout):
            return True
    except OSError:
        return False


def status_line(cfg) -> str:
    """One line for ``swarm status``: where the board is, and whether it answers."""
    if not cfg.web_enabled:
        return "web: off ([web] enabled = false)"
    where = " ".join(urls(cfg))
    if listening(cfg):
        return f"web: {where} (listening)"
    return f"web: not listening on :{cfg.web_port} — `swarm up` starts it, or run `swarm web`"


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
