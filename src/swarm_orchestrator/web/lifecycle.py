"""Starting, stopping and finding the web board.

There is one board for the machine, whatever the number of swarms: one process,
one port, one address to keep on the phone. It is a machine service
(:mod:`swarm_orchestrator.service`): any ``swarm up`` starts it when it is not
already answering, every later ``up`` leaves it alone, and the ``swarm down``
of the last swarm that is still up stops it. ``swarm web`` does the same by
hand.

**Why it stops with the last swarm.** The other choice was to keep serving
until ``swarm web stop``. It stops, because after the owner has taken every
swarm down nothing of the tool should still hold a port open to the network,
and because a board that outlived every restart would go on serving the code
of the day it was started. A run that finishes by itself is not a ``down``, so
its board stays up and keeps showing how it ended; and ``swarm web`` starts the
board again over swarms that are down, to read their last state.

So this module owns the board's command line, the probe that tells our board
from whatever else answers on the port (``/healthz``), and the address the
owner is given: the machine's Tailscale IP, or its LAN addresses when Tailscale
is absent. Where it listens is a machine setting (``[web]`` in
``machine.toml``), never a project's.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import config as config_mod
from .. import machine
from .. import service as service_mod

#: The board's name as a machine service: ``web.pid`` and ``web.log`` in the
#: machine directory.
NAME = "web"
#: What ``/healthz`` answers under ``app`` when the listener is a board of ours.
APP_ID = "swarm-web"
#: :func:`probe` outcomes: this machine's board, something else on the port, or
#: nothing listening at all. A plain connect-and-see cannot tell the first two
#: apart, and that gap once let a stray ``python3 -m http.server`` read as
#: "(listening)" in ``swarm status``.
OURS = "ours"
TAKEN = "taken"
CLOSED = "closed"
#: Interfaces that are never the LAN: container bridges and virtual links. A
#: phone cannot reach 172.17.0.1, and printing it as "the address" would send
#: the owner to a dead URL first.
_VIRTUAL = re.compile(r"^(lo|docker\d*|br-|veth|virbr|cni|flannel|tun|tap|zt|tailscale)")


def display_name(cfg) -> str | None:
    """What the board calls a swarm: ``[swarm].name``, by default the project
    folder's name. None for a config that carries neither."""
    name = getattr(cfg, "name", None)
    if name:
        return str(name)
    pdir = getattr(cfg, "project_dir", None)
    return Path(pdir).name if pdir else None


# -- where the board is -------------------------------------------------------
@dataclass(frozen=True)
class Place:
    """Where a machine's board is: the state root whose swarms it shows, and
    the address it binds."""

    root: Path
    host: str
    port: int

    @property
    def mdir(self) -> Path:
        """The machine directory its pid file and log are in."""
        return self.root / machine.DIR_NAME

    @property
    def local(self) -> str:
        """The address to ask on this machine itself."""
        return self.host if self.host not in ("", "0.0.0.0", "::") else "127.0.0.1"


def place(state_dir: Path | None = None) -> Place:
    """Where this machine's board is: of this machine, or (given a swarm's
    ``state_dir``) of the machine that swarm is on, which is the same place for
    every swarm in one state root. Where it listens is the machine file's to
    say, as it reads now (:func:`config.machine_settings`): raises
    :class:`machine.SettingsError` for one that does not read, unless this
    process read it while it was sound."""
    conf = config_mod.machine_settings()
    return Place(machine.directory(state_dir).parent.resolve(), conf.web_host, conf.web_port)


def the_service(at: Place) -> service_mod.Service:
    """The board as a machine service: its command line says everything it
    needs, because it is started with no swarm's environment."""
    return service_mod.Service(
        NAME,
        ("web", "serve", "--state-root", str(at.root), "--host", at.host,
         "--port", str(at.port)),
        mark=("web", "serve"),
        answers=lambda: probe(at).state == OURS,
    )


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


def urls(at: Place) -> list[str]:
    """The board's address for the owner: Tailscale when this machine has it,
    else the LAN addresses (so a box without Tailscale still says something).
    It is the overview of every swarm; :func:`link` is one swarm's page."""
    if at.host in ("", "0.0.0.0", "::"):
        ts = tailscale_ip()
        hosts = [ts] if ts else (lan_ips() or ["localhost"])
    else:
        hosts = [at.host]
    return [f"http://{h}:{at.port}/" for h in hosts]


def url(at: Place) -> str:
    """The first of :func:`urls`: the one address to hand out."""
    try:
        found = urls(at)
    except Exception:  # noqa: BLE001 - an address is a nicety
        found = []
    return found[0] if found else f"http://localhost:{at.port}/"


def link(base: str, slug: str) -> str:
    """One swarm's page under the board at ``base``."""
    return f"{base.rstrip('/')}/s/{slug}/"


def slug_of(cfg) -> str:
    """The name a swarm has on the board: its state dir's, which is what the
    machine's registry lists it by (:attr:`machine.Swarm.slug`)."""
    return Path(cfg.state_dir).name


# -- what answers on the port -------------------------------------------------
@dataclass(frozen=True)
class Found:
    """What :func:`probe` found on the board's port.

    ``holder`` says, for :data:`TAKEN`, who holds the port in words the owner
    can act on, and ``fix`` what to change. ``pid`` is the board's own, when it
    said so."""

    state: str
    holder: str = ""
    fix: str = ""
    pid: int | None = None


def health_body(root: Path, pid: int) -> dict:
    """What ``/healthz`` answers: enough for :func:`probe` to tell this
    machine's board from whatever else might hold the port."""
    return {"app": APP_ID, "machine": str(root), "pid": pid}


def is_ours(at: Place, body: object) -> bool:
    """Whether a ``/healthz`` answer is from the board of this state root. A
    board of another root (another user's, a test's) is not, and neither is a
    board from before boards were one per machine, which names one swarm."""
    return (isinstance(body, dict) and body.get("app") == APP_ID
            and body.get("machine") == str(at.root))


def _move_port() -> str:
    return (f"set [web].port in {machine.settings_path()} to a free port, then run"
            " `swarm web`")


def _taken(at: Place, body: object) -> Found:
    """Who holds the port, from what it answered and what ``ss`` says."""
    who, _ = _occupant(at.port)
    proc = f" ({who})" if who else ""
    if isinstance(body, dict) and body.get("app") == APP_ID:
        if "machine" in body:
            return Found(TAKEN, f"the board of another state root ({body['machine']}){proc}",
                         _move_port())
        name = body.get("project") or body.get("slug") or "another swarm"
        return Found(
            TAKEN, f"the board of the swarm {name!r}, started before boards were one per"
                   f" machine{proc}",
            "restart that swarm (`swarm down`, then `swarm up`, in its project) so it lets"
            f" go of the port; or {_move_port()}")
    return Found(TAKEN, who or "another program", _move_port())


def listening(at: Place, timeout: float = 0.3) -> bool:
    """Whether something accepts connections on the board's port right now.

    Cheap, but cannot tell *our* board from an unrelated process that beat it to
    the port. :func:`probe` is the answer to trust.
    """
    try:
        with socket.create_connection((at.local, at.port), timeout=timeout):
            return True
    except OSError:
        return False


def probe(at: Place, timeout: float = 0.5) -> Found:
    """What answers on the board's port right now: :data:`OURS`, :data:`TAKEN`
    (something else holds it) or :data:`CLOSED` (nothing does).

    ``GET /healthz`` and match its JSON body, rather than a connect-only check:
    a squatter that merely accepts the connection (another ``http.server``, say)
    must not read as our board.
    """
    try:
        with urllib.request.urlopen(f"http://{at.local}:{at.port}/healthz",
                                    timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (OSError, ValueError, urllib.error.URLError):
        if not listening(at, timeout):
            return Found(CLOSED)
        # The listener took the connection but did not answer /healthz in time.
        # That is not proof of a squatter: the server binds (and the kernel
        # starts accepting) before it has read every swarm and begun to answer,
        # so a board still starting looks exactly like this. The process we
        # started holding the port is ours, whatever its answer speed; and
        # where ``ss`` cannot say who holds it, a live board of ours gets the
        # benefit of the doubt for the moment its start takes.
        mine = running(at)
        if mine is not None and _occupant(at.port)[1] in (None, mine):
            return Found(OURS, pid=mine)
        return _taken(at, None)
    if is_ours(at, body):
        pid = body.get("pid")
        return Found(OURS, pid=pid if isinstance(pid, int) else None)
    return _taken(at, body)


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


def wait_probe(at: Place, timeout: float = 5.0) -> Found:
    """:func:`probe`, retried until it stops saying :data:`CLOSED` or ``timeout``
    runs out: a board just started needs a moment to bind, so a single probe
    right away cannot tell "still starting" from "never came up"."""
    deadline = time.monotonic() + timeout
    found = probe(at)
    while found.state == CLOSED and time.monotonic() < deadline:
        time.sleep(0.2)
        found = probe(at)
    return found


# -- the lifecycle ------------------------------------------------------------
def running(at: Place) -> int | None:
    """The pid of the board this machine started, if it is still alive."""
    return service_mod.running(the_service(at), at.mdir)


def ensure(at: Place, timeout: float = 10.0) -> Found:
    """Start the board unless it is there, and say what answers on its port.

    A board already answering is left alone, whichever swarm started it. A port
    something else holds is reported and nothing is started: a second process
    would only fail to bind.
    """
    found = probe(at)
    if found.state != CLOSED:
        return found
    service_mod.start(the_service(at), at.mdir)
    return wait_probe(at, timeout)


def _served_by_hand(at: Place) -> int | None:
    """The pid of a board of ours that answers without a pid file."""
    return probe(at).pid


def stop(at: Place) -> bool:
    """Stop the board, whoever started it. Returns whether one was running."""
    return service_mod.stop(the_service(at), at.mdir, also=_served_by_hand(at))


def stop_with_last(at: Place, leaving: Path | None = None) -> bool:
    """``swarm down``'s half: stop the board unless a swarm other than the one
    whose state dir is ``leaving`` is still up. Returns whether it was stopped."""
    return service_mod.stop_with_last(the_service(at), at.mdir, leaving,
                                      also=lambda: _served_by_hand(at))


def restart(at: Place) -> bool:
    """Start the board again, so it runs the code on disk; one that was not
    running is started all the same. Returns whether it answers."""
    stop(at)
    return ensure(at).state == OURS


# -- in words -----------------------------------------------------------------
def taken_line(at: Place, found: Found) -> str:
    """Why this machine's board is not on its port, in one line."""
    return f"port :{at.port} is held by {found.holder}, not this machine's board"


def machine_line(at: Place | None = None) -> str:
    """One line for ``swarm ls`` and ``swarm web status``: the one address, and
    whether the board answers on it."""
    try:
        at = place() if at is None else at
    except machine.SettingsError as exc:
        return f"web board: {exc}"
    found = probe(at)
    if found.state == OURS:
        pid = f", pid {found.pid}" if found.pid else ""
        return f"web board: {url(at)} (listening{pid})"
    if found.state == TAKEN:
        return f"web board: {taken_line(at, found)} — {found.fix}"
    return f"web board: not running — `swarm up` starts it on :{at.port}, or run `swarm web`"


def status_line(cfg) -> str:
    """One line for ``swarm status``: where the board is, whether it answers,
    and this swarm's own page on it."""
    if not cfg.web_enabled:
        return "web: off for this swarm ([web] enabled = false)"
    try:
        at = place(cfg.state_dir)
    except machine.SettingsError as exc:
        return f"web: {exc}"
    found = probe(at)
    if found.state == OURS:
        base = url(at)
        return f"web: {base} (listening) — this swarm: {link(base, slug_of(cfg))}"
    if found.state == TAKEN:
        return f"web: {taken_line(at, found)} — {found.fix}"
    return f"web: not listening on :{at.port} — `swarm up` starts it, or run `swarm web`"
