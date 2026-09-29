"""The web board, served from inside the dashboard.

The board used to be a tmux window of its own (``swarm web`` in a ``web`` pane),
one more thing to keep alive and one more window in the way. It is read-only and
reads the same state the dashboard does, so the dashboard serves it: the server
(:func:`web.server.make_server`) runs on a thread here and stops when the
dashboard exits.

It never starts a second board. Before binding, the port is probed
(:func:`web.lifecycle.probe`): a board already answering there — a ``swarm web``
someone ran by hand, or the old ``web`` window of a run started before this —
is left alone and reported, and a port some other program holds is reported
too. Both are re-checked every probe tick, so killing the old window hands the
board to the dashboard within seconds.

Everything slow (the probe, the Tailscale lookup, the bind) runs on the
dashboard's probe thread; the status bar only reads :attr:`WebBoard.line`.
"""

from __future__ import annotations

import threading

from ..web import lifecycle

OFF = "off"
SERVING = "serving"
#: Another process of ours already serves this project's board on the port.
ELSEWHERE = "elsewhere"
TAKEN = "taken"
FAILED = "failed"


class WebBoard:
    """One in-process board: :meth:`ensure` it is up, :meth:`stop` it on exit."""

    def __init__(self, cfg, make=None, probe=None, urls=None) -> None:
        self.cfg = cfg
        self.state = OFF
        self.detail = ""
        self.url = ""
        self._srv = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopped = False
        if make is None:
            from ..web import server as web_server

            make = web_server.make_server
        self._make = make
        self._probe = probe or lifecycle.probe
        self._urls = urls or lifecycle.urls

    @property
    def serving(self) -> bool:
        return self._srv is not None

    def ensure(self) -> str:
        """Serve the board unless it is already served or cannot be. Returns the state.

        Blocking (a probe, maybe a bind): call it from a worker thread.
        """
        with self._lock:
            if self._stopped or not getattr(self.cfg, "web_enabled", False):
                self.state = OFF
                return self.state
            if self._srv is not None:
                return self.state
            found, who = self._probe(self.cfg)
            if found == lifecycle.OURS:
                self.state, self.detail = ELSEWHERE, "another process serves it"
                self.url = self.url or self._first_url()
                return self.state
            if found == lifecycle.TAKEN:
                self.state = TAKEN
                self.detail = f"port :{self.cfg.web_port} is held by {who or 'another program'}"
                return self.state
            try:
                srv = self._make(self.cfg, self.cfg.web_host, int(self.cfg.web_port))
            except OSError as exc:
                self.state, self.detail = FAILED, f"cannot listen on :{self.cfg.web_port}: {exc}"
                return self.state
            self._srv = srv
            self._thread = threading.Thread(target=srv.serve_forever,
                                            kwargs={"poll_interval": 0.5},
                                            name="swarm-web", daemon=True)
            self._thread.start()
            self.state, self.detail = SERVING, ""
            self.url = self._first_url(srv.server_address[1])
            return self.state

    def _first_url(self, port: int | None = None) -> str:
        try:
            got = self._urls(self.cfg)
        except Exception:  # noqa: BLE001 - an address is a nicety
            got = []
        url = got[0] if got else f"http://localhost:{self.cfg.web_port}/"
        if port and f":{self.cfg.web_port}/" in url and int(self.cfg.web_port) != port:
            url = url.replace(f":{self.cfg.web_port}/", f":{port}/")
        return url

    def stop(self) -> None:
        """Close the listener and the feed, and never start again."""
        from ..web import server as web_server

        with self._lock:
            self._stopped = True
            srv, self._srv = self._srv, None
            if srv is not None:
                try:
                    web_server.close(srv)
                except Exception:  # noqa: BLE001 - exiting anyway
                    pass
            self.state = OFF

    def line(self) -> tuple[str, str]:
        """``(text, state token)`` for the status bar; ``("", "")`` when off."""
        from .theme import BAD, MUTED, WARN

        if self.state == SERVING:
            return f"board {self.url}", MUTED
        if self.state == ELSEWHERE:
            return f"board {self.url} (another process)", MUTED
        if self.state == TAKEN:
            return f"board: {self.detail}", WARN
        if self.state == FAILED:
            return f"board: {self.detail}", BAD
        return "", ""
