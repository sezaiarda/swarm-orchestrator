"""``swarm web``: the read-only board of every swarm on the machine, reached
over Tailscale.

Stdlib only (``http.server``, ``json``, threads): the package has no web
dependency and a board is not a reason to grow one. One server shows every
swarm (:mod:`.hub`). The endpoints, all GET:

``/``                 the overview: what waits on the owner on the whole
                      machine, the totals, and one card per swarm
``/api/machine``      the overview's data, and what each swarm page draws its
                      switcher from (gzip + ETag)
``/healthz``          identifies this server as the board of *this* state root
                      (:func:`lifecycle.health_body`), so a caller that only got
                      a connection accepted on the port (another program, or
                      another machine's board, can be squatting on it) can tell
``/s/<slug>/``        one swarm's board; ``<slug>`` is its state dir's name, as
                      ``swarm ls --json`` lists it. Under it:

    ``api/board``        the whole board (gzip + ETag; a phone refetches only on change)
    ``api/phase/<id>``   one card's detail sheet
    ``api/search?q=``    ids of rows whose full ledger text matches (the phone holds titles only)
    ``api/graph``        the phase graph, laid out: ``?mode=open|all&book=<name>`` (gzip + ETag)
    ``api/usage``        both usage windows over time, their caps, projection and runs (gzip + ETag)
    ``api/resources``    the host, the builds running and the build queue, now (gzip + ETag)
    ``api/resources/history?window=1h|6h|24h|7d|30d``
                         one window of host history in a few hundred min/avg/max
                         buckets, with the builds that ran in it (gzip + ETag)
    ``api/resources/builds?window=&sort=&dir=asc|desc&phase=&limit=``
                         the finished builds, filtered and sorted here (gzip + ETag)
    ``api/resources/capacity``
                         what ``swarm resources`` works out: would more builds or
                         workers fit (gzip + ETag)
    ``events``           Server-Sent Events: the board's version whenever it moves,
                         and a comment every :data:`HEARTBEAT_S` so a phone's
                         connection (and any proxy on the way) stays open

A slug no swarm here has is a 404, and a swarm's routes serve that swarm's data
only: the slug picks a feed out of the ones the registry listed, and is never
joined into a path.

**Read-only and open, by the owner's choice.** There is no mutating endpoint
(anything but GET/HEAD is refused by :mod:`http.server` itself) and no path is
ever mapped to a file: each page is one bytes object put together at start,
everything else is JSON computed from the runs, and every string in it has been
through :func:`web.redact.deep`. A request for ``/../state.json`` is just an
unknown path.
"""

from __future__ import annotations

import errno
import html
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
from .hub import Hub

STATIC = Path(__file__).resolve().parent / "static"
#: The two pages, and the parts both are put together from. A page names a part
#: where it goes, as a comment (``/*{{shared.css}}*/``), and is served with the
#: part in its place: one file to the browser, one copy of the tokens on disk.
OVERVIEW = "overview.html"
SWARM = "swarm.html"
PARTS = ("shared.css", "shared.js")
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
#: A swarm's slug on the wire: a state dir's name.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")

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
#: restart starts the new board while the old one is still closing its listener
#: (SIGTERM lets its request loop finish its poll first), and the new one died
#: with "Address already in use", status 2. SO_REUSEADDR covers a port left in
#: TIME_WAIT; this covers the rest.
BIND_WAIT_S = 5.0
_BIND_RETRY_S = 0.2


def load_pages() -> dict[str, bytes]:
    """Each page as it is served: self-contained, the shared parts in place."""
    parts = {name: (STATIC / name).read_text(encoding="utf-8") for name in PARTS}
    pages = {}
    for name in (OVERVIEW, SWARM):
        text = (STATIC / name).read_text(encoding="utf-8")
        for part, body in parts.items():
            text = text.replace("/*{{" + part + "}}*/", body)
        pages[name] = text.encode()
    return pages


class BoardServer(ThreadingHTTPServer):
    """A threading server that carries the shared :class:`Hub` and the pages."""

    daemon_threads = True
    #: SO_REUSEADDR: a restart must not wait out the old socket's TIME_WAIT.
    allow_reuse_address = True

    def __init__(self, addr, hub: Hub, pages: dict[str, bytes]) -> None:
        super().__init__(addr, Handler)
        self.hub = hub
        self.pages = pages
        self.streams = 0
        self.streams_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server: BoardServer
    server_version = "swarm-web"
    sys_version = ""

    def log_message(self, fmt, *args) -> None:  # noqa: D401 - http.server hook
        """Quiet: a phone polling all day must not fill the log with lines."""

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

    def _page(self, name: str) -> None:
        self._send(200, self.server.pages[name], "text/html; charset=utf-8")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server naming
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        hub = self.server.hub
        hub.touch()
        path = urlsplit(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._page(OVERVIEW)
            elif path == "/api/machine":
                self._cached(*hub.current())
            elif path == "/healthz":
                self._json(200, lifecycle.health_body(hub.root, os.getpid()))
            elif path.startswith("/s/"):
                slug, sep, rest = path[len("/s/"):].partition("/")
                self._swarm(slug, rest if sep else None)
            else:
                self._json(404, {"error": "not found"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _swarm(self, slug: str, rest: str | None) -> None:
        """Everything under ``/s/<slug>``: ``rest`` is what follows the slug's
        slash, or None when there is no slash."""
        feed = self.server.hub.feed(slug) if _SLUG_RE.match(slug) else None
        if feed is None:
            self._no_swarm(slug, page=not rest)
            return
        if rest is None:
            # The page asks for its data by relative address, so it must be at
            # the slash.
            self._send(308, b"", "text/plain; charset=utf-8", {"Location": f"/s/{slug}/"})
            return
        feed.touch()
        qs = parse_qs(urlsplit(self.path).query)
        if rest in ("", "index.html"):
            self._page(SWARM)
        elif rest == "api/board":
            _, body, gz, etag = feed.current()
            self._cached(body, gz, etag)
        elif rest.startswith("api/phase/"):
            self._phase(feed, unquote(rest[len("api/phase/"):]))
        elif rest == "api/search":
            query = qs.get("q", [""])[0][:200]
            self._json(200, {"q": query, "ids": feed.search(query)})
        elif rest == "api/graph":
            mode = qs.get("mode", ["open"])[0]
            book = qs.get("book", [""])[0][:80]
            if mode not in graph_mod.MODES or (book and not _BOOK_RE.match(book)):
                self._json(400, {"error": "bad view"})
            else:
                self._cached(*feed.graph(mode, book))
        elif rest == "api/usage":
            self._cached(*feed.usage())
        elif rest == "api/resources":
            self._cached(*feed.resources.now(feed.cfg))
        elif rest.startswith("api/resources/"):
            self._resources(feed, rest[len("api/resources/"):], qs)
        elif rest == "events":
            self._events(feed)
        else:
            self._json(404, {"error": "not found"})

    def _no_swarm(self, slug: str, page: bool) -> None:
        """404 for a slug no swarm here has: a line of JSON to a script, and to a
        person who followed an old bookmark a page that leads back."""
        if not page:
            self._json(404, {"error": "no such swarm"})
            return
        name = html.escape(slug[:120])
        body = ("<!doctype html><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<meta name=\"color-scheme\" content=\"dark light\"><title>No such swarm</title>"
                "<body style=\"font:16px/1.5 system-ui,sans-serif;margin:12vh auto;"
                "max-width:34em;padding:0 20px\">"
                f"<h1 style=\"font-size:20px\">No swarm called {name} on this machine</h1>"
                "<p>It may have been removed, or its project folder moved.</p>"
                "<p><a href=\"/\">See every swarm</a></p></body>")
        self._send(404, body.encode(), "text/html; charset=utf-8")

    def _cached(self, body: bytes, gz: bytes, etag: str) -> None:
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self._send(200, body, "application/json; charset=utf-8", {"ETag": etag}, gz=gz)

    def _resources(self, feed: Feed, what: str, qs: dict) -> None:
        """The Resources tab's other views; anything off their menus is a 400."""

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

    def _phase(self, feed: Feed, pid: str) -> None:
        if not _ID_RE.match(pid) or ".." in pid:
            self._json(404, {"error": "no such phase"})
            return
        body = feed.detail(pid)
        if body is None:
            self._json(404, {"error": "no such phase", "id": pid})
            return
        self._send(200, body, "application/json; charset=utf-8")

    def _events(self, feed: Feed) -> None:
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
            seen = feed.version
            self.wfile.write(b"retry: 3000\n\n" + _event(seen))
            self.wfile.flush()
            while not feed.stopping.is_set():
                now = feed.wait(seen, HEARTBEAT_S)
                # An open stream is a client waiting for news of this swarm.
                srv.hub.touch()
                feed.touch()
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


def bind(host: str, port: int, hub: Hub, pages: dict[str, bytes],
         wait_s: float = BIND_WAIT_S) -> BoardServer:
    """The server, bound, retrying for up to ``wait_s`` while the port is in use."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return BoardServer((host, port), hub, pages)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE or time.monotonic() >= deadline:
                raise
            time.sleep(_BIND_RETRY_S)


def make_server(root: Path, host: str, port: int, poll_s: float | None = None,
                bind_wait_s: float = BIND_WAIT_S) -> BoardServer:
    """A bound server over the swarms of the state root ``root``, its overview
    built once and its watcher thread running.

    Split from :func:`serve` so a test can run one on a thread.
    """
    hub = Hub(root, **({} if poll_s is None else {"poll_s": poll_s}))
    srv = bind(host, port, hub, load_pages(), bind_wait_s)
    try:
        hub.refresh()
    except Exception:
        srv.server_close()
        hub.stop()
        raise
    threading.Thread(target=hub.run, name="swarm-web-hub", daemon=True).start()
    return srv


def close(srv: BoardServer) -> None:
    srv.hub.stop()
    srv.shutdown()
    srv.server_close()


def serve(root: Path, host: str, port: int) -> int:
    """Run the board until SIGTERM/SIGINT. Returns a process exit code."""
    try:
        srv = make_server(root, host, port)
    except OSError as exc:
        print(f"swarm web: cannot listen on {host}:{port}: {exc}", file=sys.stderr)
        return 2
    at = lifecycle.Place(Path(root), host, srv.server_address[1])
    print(f"swarm web: the read-only board of every swarm under {root}")
    for shown in lifecycle.urls(at):
        print(f"  {shown}")
    print("  open to anyone who can reach it, no token (by choice); Ctrl-C stops it", flush=True)

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
        srv.hub.stop()
        srv.server_close()
    return 0
