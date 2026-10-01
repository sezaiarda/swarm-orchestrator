"""The board, computed once per change and shared by every client.

A phone left open on the board must cost the box nothing, and ten of them must
cost the same as one. So one thread watches the run's files and rebuilds the
board only when something moved; every HTTP request and every SSE stream reads
the cached result. Clients are told *that* it changed (a version number over
SSE) and fetch it — gzipped, with an ETag — so a quiet run sends heartbeats and
nothing else.

What is watched, and how often:

* Through the TUI's :class:`~swarm_orchestrator.tui.dash.Dash` (mtime-gated
  re-reads, the same code the dashboard runs): ``state.json``, the supervisor
  log, ``done/``, ``notes/``, ``recaps/``, the operator queue, the run record,
  ``meters/`` and ``limits.jsonl``, the ledger, the Overseer's pass records,
  ``keep/`` (what ``swarm keep`` left running).
* Here: ``history/``, ``.swarm.toml`` (the exclude
  list lives there — it is re-loaded like ``swarm reload`` would) and the last
  captured turn of each busy worker.

Meters and turns move every few seconds while a worker runs; they only refresh
context % and "last activity", so they rebuild at most every
:data:`SLOW_S`. Everything else rebuilds within one poll. Even a rebuild bumps
the version only when the result differs, so a supervisor event that changes
nothing on the board wakes no phone.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import threading
import time
from pathlib import Path

from .. import config as config_mod
from .. import recap as recap_mod
from ..tui.dash import Dash
from ..tui.data import read_state
from . import board as board_mod
from . import campaigns, detail, graph as graph_mod, resview, rows as rows_mod, usagechart
from .redact import deep

#: Seconds between polls of the run's files when the board owns its dash
#: (``swarm web``). Hosted by the TUI it polls nothing: the TUI's dash does.
POLL_S = 2.5
#: Hosted, a pass reads only what the TUI's dash banked, so it can be quick.
SHARED_POLL_S = 1.0
#: A standalone board stops polling this long after a client last asked for anything.
IDLE_S = 60.0
#: Floor between rebuilds driven only by meters/turns (context %, last activity).
SLOW_S = 15.0
#: A rebuild at least this often regardless — the supervisor dying changes no
#: file, and "is it running" must still turn red on its own.
FORCE_S = 20.0

#: Dash sources whose change always rebuilds; the rest are the slow ones.
_FAST = {"state", "log", "notifications", "done", "recaps", "notes", "operator", "run",
         "ledger", "overseer", "keep", "eta"}


class Feed:
    """The current board, its version, and a condition clients wait on."""

    def __init__(self, cfg, explicit_config: str | None = None, dash: Dash | None = None) -> None:
        self.cfg = cfg
        self._explicit = explicit_config
        #: Hosted by the TUI: ``dash`` is the TUI's own, polled by it, only read here.
        self.shared = dash is not None
        self.dash = dash if dash is not None else Dash(cfg)
        self._wake = threading.Event()
        self._touched = time.monotonic()
        self.version = 0
        self.board: dict = {}
        self.body = b"{}"
        self.gz = gzip.compress(self.body)
        self.etag = '"0"'
        self.stopping = threading.Event()
        self._cond = threading.Condition()
        self._lock = threading.Lock()
        self._digest = ""
        self._stamps: dict[str, float] = {}
        self._rows: dict = {}
        self._metas: dict = {}
        self._passes: list = []
        self._state: dict | None = None
        self._turns: dict[str, dict] = {}
        self._built_at = 0.0
        self._details: dict[str, bytes] = {}
        #: Served views other than the board, by key: ``(stamp, body, gz, etag)``.
        self._views: dict[tuple, tuple] = {}
        self._views_lock = threading.Lock()
        self.layouts = graph_mod.Layouts()
        #: The Resources tab's views; they read the sampler's files, not the dash,
        #: and only when a client asks.
        self.resources = resview.Resources(cfg.state_dir)

    # -- change detection ---------------------------------------------------
    def _moved(self, key: str, path: Path) -> bool:
        try:
            st = path.stat()
            stamp = st.st_mtime + st.st_size
        except OSError:
            stamp = -1.0
        if self._stamps.get(key) == stamp:
            return False
        self._stamps[key] = stamp
        return True

    def _reload_config(self) -> None:
        """Adopt an edited ``.swarm.toml`` (the exclude list, the ledger path).

        The state dir never moves under a running board — it is what the board
        was started against — so a load that would change it is ignored, as is
        one that fails: the last good config keeps serving.
        """
        try:
            new = config_mod.load(explicit=self._explicit, project_dir=str(self.cfg.project_dir))
        except (ValueError, OSError):
            return
        if new.state_dir != self.cfg.state_dir:
            return
        self.cfg = new
        if not self.shared:
            self.dash.cfg = new

    def _load_ledger(self) -> None:
        path = Path(self.cfg.project_dir) / self.cfg.ledger
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        self._rows, _ = rows_mod.parse(text)
        adr = campaigns.adr_titles(path.parent / "adr")
        if not adr:
            adr = campaigns.adr_titles(Path(self.cfg.project_dir) / "docs" / "adr")
        self._metas = campaigns.describe(self._rows, adr)

    def _poll_turns(self) -> bool:
        """The last captured turn of each busy worker: its own "what I'm doing"."""
        busy = [s.phase for s in self.dash.snapshot.slots if s.busy and s.phase]
        moved = False
        for phase in busy:
            if self._moved(f"turn:{phase}", recap_mod.turns_path(self.cfg, phase)):
                moved = True
                got = recap_mod.read_turns(self.cfg, phase, limit=1)
                self._turns[phase] = got[-1] if got else {}
        for gone in set(self._turns) - set(busy):
            self._turns.pop(gone, None)
            moved = True
        return moved

    def refresh(self, now: float | None = None, force: bool = False) -> bool:
        """One poll. Returns whether the board's version moved."""
        now = time.time() if now is None else now
        fast = False
        if self._moved("config", Path(self.cfg.project_dir) / ".swarm.toml") and self._built_at:
            self._reload_config()
            fast = True
        changed = self.dash.take_changes() if self.shared else self.dash.poll()
        fast |= bool(changed & _FAST)
        if "state" in changed or self._state is None:
            self._state = read_state(self.cfg)
        if self._moved("ledger", Path(self.cfg.project_dir) / self.cfg.ledger) or not self._built_at:
            self._load_ledger()
            fast = True
        if self._moved("history", Path(self.cfg.state_dir) / "history"):
            fast = True
        # The dash re-reads the Overseer's records when they (or the live pass)
        # move — the TUI's feed needs them too, so they are read once, there.
        self._passes = self.dash.passes
        slow = bool(changed - _FAST) | self._poll_turns()
        due = (fast or force or not self._built_at or now - self._built_at >= FORCE_S
               or (slow and now - self._built_at >= SLOW_S))
        if not due:
            return False
        return self._rebuild(now)

    def _rebuild(self, now: float) -> bool:
        board = board_mod.build(self.cfg, self.dash, state=self._state, rows=self._rows,
                                metas=self._metas, passes=self._passes, turns=self._turns,
                                now=now)
        board = deep(board)
        self._built_at = now
        digest = hashlib.sha1(
            json.dumps({k: v for k, v in board.items() if k != "generated_at"},
                       sort_keys=True, default=str).encode()).hexdigest()
        if digest == self._digest:
            return False
        with self._lock:
            self._digest = digest
            self.version += 1
            board["version"] = self.version
            self.board = board
            self.body = json.dumps(board, separators=(",", ":"), default=str).encode()
            self.gz = gzip.compress(self.body, compresslevel=6)
            self.etag = f'"{self.version}-{digest[:12]}"'
            self._details = {}
        with self._cond:
            self._cond.notify_all()
        return True

    # -- readers -------------------------------------------------------------
    def current(self) -> tuple[int, bytes, bytes, str]:
        with self._lock:
            return self.version, self.body, self.gz, self.etag

    def detail(self, pid: str) -> bytes | None:
        """One card's detail JSON, built on first ask and cached per version."""
        with self._lock:
            cached = self._details.get(pid)
            board, version = self.board, self.version
        if cached is not None:
            return cached
        got = detail.phase(self.cfg, self.dash, board, self._rows, self._metas, pid,
                           turns=self._turns)
        if got is None:
            return None
        got["version"] = version
        body = json.dumps(deep(got), separators=(",", ":"), default=str).encode()
        with self._lock:
            if self.version == version:
                self._details[pid] = body
        return body

    def _view(self, key: tuple, stamp: tuple, make) -> tuple[bytes, bytes, str]:
        """A cached JSON view: rebuilt only when ``stamp`` moves, then gzipped once."""
        with self._views_lock:
            hit = self._views.get(key)
            if hit is not None and hit[0] == stamp:
                return hit[1], hit[2], hit[3]
            body = json.dumps(deep(make()), separators=(",", ":"), default=str).encode()
            etag = f'"{hashlib.sha1(body).hexdigest()[:16]}"'
            got = (stamp, body, gzip.compress(body, compresslevel=6), etag)
            self._views[key] = got
            if len(self._views) > 32:
                self._views.pop(next(iter(self._views)))
            return got[1], got[2], got[3]

    def graph(self, mode: str = graph_mod.OPEN, book: str = "") -> tuple[bytes, bytes, str]:
        """``/api/graph``: one view of the phase graph, for this board version.

        The layout underneath is cached by shape (:class:`graph.Layouts`), so a
        new version that only re-colours rows costs a dict walk, not a layout.
        """
        with self._lock:
            board, version = self.board, self.version
        order = list(self._rows) + [p for p in (self.dash.graph or {}) if p not in self._rows]
        return self._view(("graph", mode, book), (version,), lambda: graph_mod.view(
            board, self.dash.graph or {}, order, forecast=self.dash.forecast, mode=mode,
            book=book, layouts=self.layouts))

    def usage(self, now: float | None = None) -> tuple[bytes, bytes, str]:
        """``/api/usage``: both windows, their projection and the runs table.

        Rebuilt when the board moves or a reading lands, and at most once a
        minute otherwise (the chart's left edge follows the clock).
        """
        now = time.time() if now is None else now
        with self._lock:
            board, version = self.board, self.version
        stamp = (version, len(self.dash.samples), int(now // 60))
        return self._view(("usage",), stamp,
                          lambda: usagechart.payload(self.cfg, self.dash, board, now))

    def search(self, query: str, limit: int = 500) -> list[str]:
        """Ids of rows whose id or full ledger text holds every word of ``query``.

        Server-side because the phone holds only each row's title; the full text
        of a 5,000-line ledger is not worth shipping to it for a search box.
        """
        words = [w for w in (query or "").lower().split() if w][:8]
        if not words:
            return []
        out = []
        for pid, row in list(self._rows.items()):
            hay = f"{pid} {row.text}".lower()
            if all(w in hay for w in words):
                out.append(pid)
                if len(out) >= limit:
                    break
        return out

    def wait(self, seen: int, timeout: float) -> int:
        """Block until the version passes ``seen`` (or ``timeout``); return it."""
        with self._cond:
            self._cond.wait_for(lambda: self.version != seen or self.stopping.is_set(),
                                timeout=timeout)
        return self.version

    def touch(self) -> None:
        """A client asked for something: keep polling (and wake a sleeping feed)."""
        self._touched = time.monotonic()
        self._wake.set()

    def idle(self) -> bool:
        """Whether a standalone feed has gone quiet: nobody asked for :data:`IDLE_S`."""
        return not self.shared and time.monotonic() - self._touched > IDLE_S

    def run(self, poll_s: float | None = None) -> None:
        """The watcher loop: poll until :attr:`stopping` is set. Never raises.

        Standalone, it sleeps once idle and wakes on the next :meth:`touch`;
        hosted, each pass only reads what the TUI's dash banked.
        """
        if poll_s is None:
            poll_s = SHARED_POLL_S if self.shared else POLL_S
        while not self.stopping.is_set():
            if self.idle():
                self._wake.wait()
                self._wake.clear()
                if self.stopping.is_set():
                    break
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - a bad read must not kill the board
                print(f"swarm web: refresh failed: {exc!r}", flush=True)
            self.stopping.wait(poll_s)

    def stop(self) -> None:
        self.stopping.set()
        self._wake.set()
        with self._cond:
            self._cond.notify_all()
