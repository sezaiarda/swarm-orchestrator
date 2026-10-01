"""``swarm web`` — the read-only Kanban board, reached over Tailscale.

Stdlib only (``http.server``, ``json``, threads): the package has no web
dependency and a board is not a reason to grow one. The endpoints, all GET:

``/``                 the page — one self-contained HTML file, no external hosts
``/api/board``        the whole board (gzip + ETag; a phone refetches only on change)
``/api/phase/<id>``   one card's detail sheet
``/api/search?q=``    ids of rows whose full ledger text matches (the phone holds titles only)
``/api/graph``        the phase graph, laid out: ``?mode=open|all&book=<name>`` (gzip + ETag)
``/api/usage``        both usage windows over time, their caps, projection and runs (gzip + ETag)
``/api/resources``    the host, the builds running and the build queue, now (gzip + ETag)
``/api/resources/history?window=1h|6h|24h|7d|30d``
                      one window of host history in a few hundred min/avg/max
                      buckets, with the builds that ran in it (gzip + ETag)
``/api/resources/builds?window=&sort=&dir=asc|desc&phase=&limit=``
                      the finished builds, filtered and sorted here (gzip + ETag)
``/api/resources/capacity``
                      what ``swarm resources`` works out: would more builds or
                      workers fit (gzip + ETag)
``/events``           Server-Sent Events: the board's version whenever it moves,
                      and a comment every :data:`HEARTBEAT_S` so a phone's
                      connection (and any proxy on the way) stays open
``/healthz``          identifies this server as *our* board for this project —
                      see :func:`_health_body` — so a caller that only got a
                      connection accepted on the port (another program can be
                      squatting on it) can tell the two apart

**Read-only and open, by the owner's choice.** There is no mutating endpoint —
anything but GET/HEAD is refused by :mod:`http.server` itself — and no path is
ever mapped to a file: the page is one bytes object loaded at start, everything
else is JSON computed from the run, and every string in it has been through
:func:`web.redact.deep`. A request for ``/../state.json`` is just an unknown
path.
"""

from __future__ import annotations

import errno
import json
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import graph as graph_mod
from . import lifecycle
from . import reshist, resview
from .feed import Feed

PAGE = Path(__file__).resolve().parent / "static" / "index.html"
#: Seconds between SSE heartbeat comments. Under the ~30-60 s idle cut-offs of
#: mobile browsers and home routers.
HEARTBEAT_S = 15.0
#: Open SSE streams at most. Each holds a thread; past this a client gets 503
#: and falls back to its own retry, rather than exhausting the box.
MAX_STREAMS = 64
#: What a phase id may look like on the wire (the ledger's own shape, plus the
#: ``overseer:<pass>`` ids of non-phase cards).
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._/:-]{0,120}$")
#: A phase book (campaign) name: an id's prefix.
_BOOK_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")

_SECURITY = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"
    ),
}


#: How long a start waits for the port to come free before it gives up. A
#: restart (``respawn-pane -k``, or a dashboard restarted) starts the new board
#: while the old one is still closing its listener — SIGHUP lets its request loop
#: finish its poll first — and the new one died with "Address already in use",
#: status 2. SO_REUSEADDR covers a port left in TIME_WAIT; this covers the rest.
BIND_WAIT_S = 5.0
_BIND_RETRY_S = 0.2


class BoardServer(ThreadingHTTPServer):
    """A threading server that carries the shared :class:`Feed` and the page."""

    daemon_threads = True
    #: SO_REUSEADDR: a restart must not wait out the old socket's TIME_WAIT.
    allow_reuse_address = True

    def __init__(self, addr, feed: Feed, page: bytes) -> None:
        super().__init__(addr, Handler)
        self.feed = feed
        self.page = page
        self.streams = 0
        self.streams_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server: BoardServer
    server_version = "swarm-web"
    sys_version = ""

    def log_message(self, fmt, *args) -> None:  # noqa: D401 - http.server hook
        """Quiet: a phone polling all day must not fill the window with lines."""

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None,
              gz: bytes | None = None) -> None:
        use_gz = gz is not None and "gzip" in (self.headers.get("Accept-Encoding") or "")
        payload = gz if use_gz else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        if gz is not None:
            self.send_header("Vary", "Accept-Encoding")
        for k, v in {**_SECURITY, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server naming
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self.server.feed.touch()
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._send(200, self.server.page, "text/html; charset=utf-8")
            elif path == "/api/board":
                self._board()
            elif path.startswith("/api/phase/"):
                self._phase(unquote(path[len("/api/phase/"):]))
            elif path == "/api/search":
                query = parse_qs(urlsplit(self.path).query).get("q", [""])[0][:200]
                self._json(200, {"q": query, "ids": self.server.feed.search(query)})
            elif path == "/api/graph":
                qs = parse_qs(urlsplit(self.path).query)
                mode = qs.get("mode", ["open"])[0]
                book = qs.get("book", [""])[0][:80]
                if mode not in graph_mod.MODES or (book and not _BOOK_RE.match(book)):
                    self._json(400, {"error": "bad view"})
                else:
                    self._cached(*self.server.feed.graph(mode, book))
            elif path == "/api/usage":
                self._cached(*self.server.feed.usage())
            elif path == "/api/resources":
                self._cached(*self.server.feed.resources.now(self.server.feed.cfg))
            elif path.startswith("/api/resources/"):
                self._resources(path[len("/api/resources/"):],
                                parse_qs(urlsplit(self.path).query))
            elif path == "/events":
                self._events()
            elif path == "/healthz":
                self._json(200, _health_body(self.server.feed.cfg))
            else:
                self._json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _board(self) -> None:
        _, body, gz, etag = self.server.feed.current()
        self._cached(body, gz, etag)

    def _cached(self, body: bytes, gz: bytes, etag: str) -> None:
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self._send(200, body, "application/json; charset=utf-8", {"ETag": etag}, gz=gz)

    def _resources(self, what: str, qs: dict) -> None:
        """The Resources tab's other views; anything off their menus is a 400."""
        feed = self.server.feed

        def arg(name: str, default: str) -> str:
            return qs.get(name, [default])[0][:80]

        if what == "capacity":
            self._cached(*feed.resources.capacity(feed.cfg))
        elif what == "history":
            window = arg("window", reshist.DEFAULT_WINDOW)
            if window not in reshist.WINDOWS:
                self._json(400, {"error": "bad window"})
                return
            self._cached(*feed.resources.window(feed.cfg, window))
        elif what == "builds":
            window, sort = arg("window", resview.ALL), arg("sort", "ended")
            order, phase, limit = arg("dir", "desc"), arg("phase", ""), arg("limit", "")
            if (window not in resview.TABLE_WINDOWS or sort not in resview.SORTS
                    or order not in ("asc", "desc") or not resview.PHASE_RE.match(phase)
                    or (limit and not limit.isdigit())):
                self._json(400, {"error": "bad view"})
                return
            count = min(max(int(limit), 1), resview.MAX_LIMIT) if limit else resview.LIMIT
            self._cached(*feed.resources.table(feed.cfg, window=window, sort=sort,
                                               desc=order == "desc", phase=phase, limit=count))
        else:
            self._json(404, {"error": "not found"})

    def _phase(self, pid: str) -> None:
        if not _ID_RE.match(pid) or ".." in pid:
            self._json(404, {"error": "no such phase"})
            return
        body = self.server.feed.detail(pid)
        if body is None:
            self._json(404, {"error": "no such phase", "id": pid})
            return
        self._send(200, body, "application/json; charset=utf-8")

    def _events(self) -> None:
        srv = self.server
        with srv.streams_lock:
            if srv.streams >= MAX_STREAMS:
                self._json(503, {"error": "too many open streams"})
                return
            srv.streams += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            for k, v in _SECURITY.items():
                self.send_header(k, v)
            self.end_headers()
            seen = srv.feed.version
            self.wfile.write(b"retry: 3000\n\n" + _event(seen))
            self.wfile.flush()
            while not srv.feed.stopping.is_set():
                now = srv.feed.wait(seen, HEARTBEAT_S)
                srv.feed.touch()  # an open stream is a client waiting for news
                if now != seen:
                    seen = now
                    self.wfile.write(_event(seen))
                else:
                    self.wfile.write(b": hb\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with srv.streams_lock:
                srv.streams -= 1


def _event(version: int) -> bytes:
    return f"event: board\ndata: {json.dumps({'version': version})}\n\n".encode()


def _health_body(cfg) -> dict:
    """What ``/healthz`` answers: enough for a caller (:func:`lifecycle.probe`) to
    tell *our* board from whatever else might already be squatting the port."""
    return {"app": lifecycle.APP_ID, "project": cfg.project_dir.name}


def bind(host: str, port: int, feed: Feed, page: bytes,
         wait_s: float = BIND_WAIT_S) -> BoardServer:
    """The server, bound — retrying for up to ``wait_s`` while the port is in use."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return BoardServer((host, port), feed, page)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                raise
            time.sleep(_BIND_RETRY_S)


def make_server(cfg, host: str, port: int, explicit_config: str | None = None,
                poll_s: float | None = None, bind_wait_s: float = BIND_WAIT_S,
                dash=None) -> BoardServer:
    """A bound server with its feed built and its watcher thread running.

    Split from :func:`serve` so tests — and the dashboard, which runs the board
    in-process (:mod:`swarm_orchestrator.tui.webboard`) — can run one on a thread.
    """
    feed = Feed(cfg, explicit_config, dash=dash)
    feed.refresh(force=True)
    srv = bind(host, port, feed, PAGE.read_bytes(), bind_wait_s)
    kwargs = {} if poll_s is None else {"poll_s": poll_s}
    threading.Thread(target=feed.run, kwargs=kwargs, name="swarm-web-feed",
                     daemon=True).start()
    return srv


def close(srv: BoardServer) -> None:
    srv.feed.stop()
    srv.shutdown()
    srv.server_close()


def serve(cfg, host: str, port: int, pidfile: str | None = None,
          explicit_config: str | None = None) -> int:
    """Run the board until SIGTERM/SIGINT. Returns a process exit code."""
    try:
        srv = make_server(cfg, host, port, explicit_config)
    except OSError as exc:
        print(f"swarm web: cannot listen on {host}:{port}: {exc}", file=sys.stderr)
        return 2
    real = srv.server_address[1]
    cfg.web_port = real
    cfg.web_host = host
    shown = lifecycle.urls(cfg)
    print(f"swarm web: read-only board for {cfg.project_dir.name} (state {cfg.state_dir})")
    for url in shown:
        print(f"  {url}")
    print("  open to anyone who can reach it, no token (by choice); Ctrl-C stops it", flush=True)
    pid_path = Path(pidfile) if pidfile else None
    if pid_path is not None:
        try:
            pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError:
            pid_path = None

    def _stop(signum, frame) -> None:
        # shutdown() blocks until serve_forever returns, so never call it on
        # the thread that is running serve_forever.
        threading.Thread(target=close, args=(srv,), daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _stop)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.feed.stop()
        srv.server_close()
        if pid_path is not None:
            try:
                if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    pid_path.unlink()
            except OSError:
                pass
    return 0
