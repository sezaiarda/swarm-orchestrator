"""Every swarm on the machine behind one server: their feeds, and the overview.

The board is the machine's, so it cannot be told which swarms exist: it reads
the registry (:func:`machine.swarms`, the state root itself) every few seconds
while anyone is looking, and so a swarm that comes up, goes down or is moved
shows without the board being restarted.

Per swarm it keeps one :class:`~swarm_orchestrator.web.feed.Feed`, made on the
swarm's config as its supervisor last recorded it (:func:`machine.swarm_config`)
and so bound to that swarm's state dir whatever this process's environment
says. A swarm that is down has a feed too: its state dir still holds how it
ended, and the board shows that, marked as not running.

**What is a swarm here.** A state dir with a recorded project whose folder
still exists and whose ``[web] enabled`` is on. One nobody ever ran a
supervisor in (``empty``), one whose project is gone (``stale``) and one that
opted out are not shown, and neither is one whose records cannot be read at all; the
overview's footer counts them, so nothing is hidden without a word.

The overview (:meth:`Hub.current`) is what ``/`` draws: first what waits on the
owner across every swarm, then the totals, then one entry per swarm. Like a
feed it is built on the hub's thread, has a version that moves only when the
result does, and is served gzipped with an ETag.

**What waits on the owner** is two lists, as everywhere else in the tool:
questions (a session asks and waits for an answer: the *Needs you* cards of
the swarm's own board) and to-dos (the owner has to do something:
:mod:`swarm_orchestrator.todo`, what ``swarm todo`` prints). A swarm's count is
both together, each thing once.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import socket
import threading
import time
from pathlib import Path

from .. import config as config_mod
from .. import machine, procs
from .. import state as state_mod
from .. import todo as todo_mod
from . import board as board_mod
from . import feed as feed_mod
from . import resview
from .feed import Feed
from .redact import deep
from .rows import clip

#: Seconds between looks at the registry while a client is asking.
POLL_S = 2.5
#: The hub stops looking this long after a client last asked for anything.
IDLE_S = feed_mod.IDLE_S
#: The most questions and the most to-dos the overview lists, and the most
#: phases a swarm's entry names as building. The counts beside them are whole.
MAX_ASKS = 30
MAX_BUILDING = 4
#: The machine's build gate is asked again at most this often.
GATE_EVERY_S = 5.0
#: A swarm whose board has not moved is looked at again at least this often:
#: the clock alone can make a to-do ready.
LOOK_EVERY_S = 20.0

#: Each status of :class:`machine.Swarm` in a plain word, as the pages say it.
WORDS = {
    machine.RUNNING: "running",
    machine.PAUSED: "paused",
    machine.HELD: "held on a usage limit",
    machine.FROZEN: "frozen",
    machine.FINISHED: "finished",
    machine.STOPPED: "down",
}
#: The order the swarms are listed in: the ones that are up first, then the
#: ones that ended by themselves, then the ones taken down; by name within each.
_RANK = {machine.RUNNING: 0, machine.PAUSED: 0, machine.HELD: 0, machine.FROZEN: 0,
         machine.FINISHED: 1, machine.STOPPED: 2}
#: Why a state dir is not shown, in the footer's words.
EMPTY = "empty"
STALE = "stale"
OFF = "off"
UNREADABLE = "unreadable"


def on_board(root: Path) -> tuple[list[tuple[Path, object]], dict[str, int]]:
    """``(the state dirs the board shows, each with its recorded config; how
    many it does not show, by why)``, for the state root ``root``. Cheap: it
    reads each dir's ``config.json`` and nothing else."""
    shown, hidden = [], {EMPTY: 0, STALE: 0, OFF: 0, UNREADABLE: 0}
    for state_dir in machine.state_dirs(root):
        cfg = machine.swarm_config(state_dir)
        if cfg is None:
            hidden[STALE if config_mod.recorded(state_dir).get("project_dir") else EMPTY] += 1
        elif not cfg.web_enabled:
            hidden[OFF] += 1
        else:
            shown.append((state_dir, cfg))
    return shown, hidden


def _place(swarm: machine.Swarm) -> tuple:
    """Where a swarm sorts among the others (see :data:`_RANK`)."""
    return _RANK.get(swarm.status, 3), swarm.name.lower(), swarm.slug


def registry(root: Path) -> list[machine.Swarm]:
    """The swarms the board shows, in the order it lists them."""
    return sorted((machine.look(state_dir) for state_dir, _ in on_board(root)[0]), key=_place)


class Hub:
    """The machine's swarms: a feed each, and the overview of all of them."""

    def __init__(self, root: Path, poll_s: float = POLL_S) -> None:
        self.root = Path(root)
        self.poll_s = poll_s
        self.stopping = threading.Event()
        self._wake = threading.Event()
        self._touched = time.monotonic()
        self._lock = threading.Lock()
        self._scan = threading.Lock()
        self._feeds: dict[str, Feed] = {}
        #: The machine's build gate as last asked, and when.
        self._gate_read: tuple[float, dict] = (0.0, {})
        #: What a look at each swarm found, and the stamp it was found under.
        self._looks: dict[str, tuple[tuple, machine.Swarm, list[dict]]] = {}
        self._digest = ""
        self.version = 0
        self.overview: dict = {}
        self.body = b"{}"
        self.gz = gzip.compress(self.body)
        self.etag = '"0"'

    # -- the swarms ---------------------------------------------------------
    def _feed_for(self, slug: str, cfg) -> Feed:
        """This swarm's feed, made and started on first need."""
        with self._lock:
            feed = self._feeds.get(slug)
        if feed is not None:
            return feed
        feed = Feed(cfg)
        feed.refresh(force=True)
        with self._lock:
            had = self._feeds.setdefault(slug, feed)
        if had is not feed:
            feed.stop()
            return had
        threading.Thread(target=feed.run, kwargs={"poll_s": self.poll_s},
                         name=f"swarm-web-feed-{slug}", daemon=True).start()
        return feed

    def feed(self, slug: str) -> Feed | None:
        """The feed of the swarm called ``slug``, or None when no swarm shown
        here has that name. Looked up among the swarms found, never built from
        the name: a request cannot reach a path the registry did not list."""
        with self._lock:
            return self._feeds.get(slug)

    def _gate(self, cfg, now: float) -> dict:
        """The machine's one build gate (:func:`resview.gate`, what every
        swarm's Resources tab shows), asked through ``cfg``, any swarm's, at
        most every :data:`GATE_EVERY_S`. Who asks only decides which builds it
        calls ``mine``, and the overview reads whose each is by its slug."""
        if now - self._gate_read[0] >= GATE_EVERY_S:
            self._gate_read = (now, resview.gate(cfg))
        return self._gate_read[1]

    def _look(self, state_dir: Path, feed: Feed, now: float) -> tuple[machine.Swarm, list[dict]]:
        """The registry's view of one swarm and its to-dos, read again only when
        its board moved, its supervisor came or went, or it has aged."""
        slug = state_dir.name
        stamp = (feed.version, procs.fifo_has_reader(state_dir / "control.fifo"),
                 int(now // LOOK_EVERY_S))
        hit = self._looks.get(slug)
        if hit is None or hit[0] != stamp:
            hit = self._looks[slug] = (stamp, machine.look(state_dir), _todos(feed))
        return hit[1], hit[2]

    # -- the overview -------------------------------------------------------
    def refresh(self, now: float | None = None) -> bool:
        """One look at the machine. Returns whether the overview's version moved."""
        with self._scan:
            return self._refresh(time.time() if now is None else now)

    def _refresh(self, now: float) -> bool:
        shown, hidden = on_board(self.root)
        rows = []
        builds = _builds(self._gate(shown[0][1], now) if shown else {})
        for state_dir, cfg in shown:
            try:
                feed = self._feed_for(state_dir.name, cfg)
                feed.touch()  # the overview is a reader of every swarm
                swarm, todos = self._look(state_dir, feed, now)
                entry = _entry(swarm, feed, todos, builds)
            except Exception as exc:  # noqa: BLE001 - one unreadable swarm must not hide the others
                print(f"swarm web: {state_dir.name} could not be read: {exc!r}", flush=True)
                hidden[UNREADABLE] += 1
                continue
            rows.append((swarm, entry, feed))
        rows.sort(key=lambda row: _place(row[0]))
        entries = [entry for _, entry, _ in rows]
        with self._lock:
            keep = {swarm.slug for swarm, _, _ in rows}
            gone = [slug for slug in self._feeds if slug not in keep]
            dropped = [self._feeds.pop(slug) for slug in gone]
            for slug in gone:
                self._looks.pop(slug, None)
        for feed in dropped:
            feed.stop()
        overview = deep({
            "generated_at": now,
            "host": socket.gethostname(),
            "waiting": _waiting(entries),
            "totals": _totals(entries, builds),
            "usage": _usage(entries, [feed for _, _, feed in rows]),
            "swarms": entries,
            "hidden": {k: v for k, v in hidden.items() if v},
        })
        digest = hashlib.sha1(json.dumps(
            {k: v for k, v in overview.items() if k != "generated_at"},
            sort_keys=True, default=str).encode()).hexdigest()
        if digest == self._digest:
            return False
        with self._lock:
            self._digest = digest
            self.version += 1
            overview["version"] = self.version
            self.overview = overview
            self.body = json.dumps(overview, separators=(",", ":"), default=str).encode()
            self.gz = gzip.compress(self.body, compresslevel=6)
            self.etag = f'"{self.version}-{digest[:12]}"'
        return True

    def current(self) -> tuple[bytes, bytes, str]:
        with self._lock:
            return self.body, self.gz, self.etag

    # -- the watcher --------------------------------------------------------
    def touch(self) -> None:
        """A client asked for something: keep looking (and wake a sleeping hub)."""
        self._touched = time.monotonic()
        self._wake.set()

    def idle(self) -> bool:
        return time.monotonic() - self._touched > IDLE_S

    def run(self) -> None:
        """The watcher loop: look until :attr:`stopping` is set. Never raises.
        It sleeps once nobody has asked for a while, and the feeds with it."""
        while not self.stopping.is_set():
            if self.idle():
                self._wake.wait()
                self._wake.clear()
                if self.stopping.is_set():
                    break
            try:
                self.refresh()
            except Exception as exc:  # noqa: BLE001 - a bad read must not kill the board
                print(f"swarm web: overview refresh failed: {exc!r}", flush=True)
            self.stopping.wait(self.poll_s)

    def stop(self) -> None:
        self.stopping.set()
        self._wake.set()
        with self._lock:
            feeds = list(self._feeds.values())
        for feed in feeds:
            feed.stop()


# -- one swarm's entry ---------------------------------------------------------
def _cards(board: dict, *keys: str) -> list[dict]:
    return [card for col in board.get("columns") or [] if col.get("key") in keys
            for card in col.get("cards") or []]


#: A to-do's kind in the page's words.
_TODO_WORDS = {
    todo_mod.OWNER_ROW: "yours to do",
    todo_mod.DEVICE_CHECK: "to check on your own device",
    todo_mod.WORKER_TODO: "left for you by a worker",
    todo_mod.GIVEN_UP: "the operator gave up on it",
    todo_mod.OPERATOR_ASK: "the operator left it for you",
    todo_mod.OVERSEER: "mentioned by the Overseer",
}


def _todos(feed: Feed) -> list[dict]:
    """The swarm's owner to-dos (:func:`todo.collect`), as the overview lists
    them: no path and no command of the box, only what to do and since when.
    ``row`` says whether the id is a ledger row, which has a sheet to open."""
    cfg = feed.cfg
    try:
        raw = json.loads(Path(cfg.state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    try:
        st = (state_mod.State.from_dict(raw) if raw
              else state_mod.State.fresh(cfg.max_workers))
        items = todo_mod.collect(cfg, st).items
    except Exception:  # noqa: BLE001 - one unreadable swarm must not hide the others
        return []
    return [{"id": it.id, "t": clip(it.title, 200), "kind": it.kind,
             "sub": _TODO_WORDS.get(it.kind, it.kind), "behind": it.rows_behind,
             "since": it.since or None, "row": it.id in feed.rows()} for it in items]


def _entry(swarm: machine.Swarm, feed: Feed, todos: list[dict], builds: dict) -> dict:
    """One swarm as the overview and the switcher show it. The flags and the
    phase counts are the registry's (what ``swarm ls`` prints); the questions
    and what is building are this swarm's own board, so the overview can never
    list a question its page does not."""
    board = feed.board
    header = board.get("header") or {}
    asks = _cards(board, board_mod.NEEDS_YOU)
    building = _cards(board, board_mod.BUILDING, board_mod.MERGING)
    eta = header.get("eta") or {}
    asked = {c.get("id") for c in asks}
    todos = [t for t in todos if t["id"] not in asked]
    return {
        "slug": swarm.slug,
        "name": swarm.name,
        "status": swarm.status,
        "word": WORDS.get(swarm.status, swarm.status),
        "up": swarm.running,
        "why": (header.get("frozen") or header.get("restart") or header.get("drain")
                or header.get("usage_hold") or ""),
        "phases": swarm.phases,
        "problem": swarm.problem,
        "needs_you": len(asks) + len(todos),
        "questions": [{"id": c.get("id"), "t": c.get("t", ""), "q": c.get("q", ""),
                       "sub": c.get("sub", ""), "since": _asked(c, feed.cfg)} for c in asks],
        "todos": todos,
        "building": [{"id": c.get("id"), "t": c.get("t", ""), "since": c.get("since"),
                      "merging": c.get("col") == board_mod.MERGING}
                     for c in building[:MAX_BUILDING]],
        "building_n": len(building),
        "workers": header.get("slots") or {"busy": 0, "total": 0},
        "builds": next(({"running": b["running"], "waiting": b["waiting"]}
                        for b in builds["by"] if b["slug"] == swarm.slug),
                       {"running": 0, "waiting": 0}),
        "eta": {"p50": eta.get("p50"), "p85": eta.get("p85"), "floor": eta.get("floor", True)},
        "last_event_at": header.get("last_event_at"),
    }


def _asked(card: dict, cfg) -> float | None:
    """When a *Needs you* card's question was asked. A worker still in its slot
    carries the moment it will be parked, and asked ``park_after`` before that;
    every other card says since when it has waited."""
    parks_at = card.get("parks_at")
    if isinstance(parks_at, (int, float)):
        return parks_at - float(getattr(cfg, "park_after", 0) or 0)
    return card.get("since")


def _builds(gate: dict) -> dict:
    """The builds on the machine, from its one gate: how many run and how many
    wait, and whose they are (``by``: one row per swarm that has any, the
    busiest first). A slot gc holds, or one whose holder is not known yet, is
    no build. A build whose record names no swarm is listed without one."""
    running = [s for s in gate.get("slots") or []
               if s.get("busy") and s.get("id") and not s.get("gc") and not s.get("unknown")]
    waiting = list(gate.get("queue") or [])
    by: dict[str, dict] = {}
    for kind, rows in (("running", running), ("waiting", waiting)):
        for row in rows:
            slug = str(row.get("swarm") or "")
            mine = by.setdefault(slug, {"slug": slug, "name": row.get("swarm_name") or slug,
                                        "running": 0, "waiting": 0})
            mine[kind] += 1
    return {"running": len(running), "waiting": len(waiting),
            "by": sorted(by.values(), key=lambda b: (-b["running"], -b["waiting"], b["name"]))}


# -- the machine as a whole ----------------------------------------------------
def _waiting(entries: list[dict]) -> dict:
    """What waits on the owner on the whole machine: the questions, then the
    to-dos, each newest first and each naming its swarm. One whose moment is
    not known sorts last, in the order its swarm lists it. ``n`` is all of
    them, whatever the lists were cut to."""
    def gathered(key: str) -> list[dict]:
        rows = [{**item, "slug": e["slug"], "swarm": e["name"]}
                for e in entries for item in e[key]]
        rows.sort(key=lambda r: -(r.get("since") or 0))
        return rows

    questions, todos = gathered("questions"), gathered("todos")
    return {"n": len(questions) + len(todos),
            "questions_n": len(questions), "questions": questions[:MAX_ASKS],
            "todos_n": len(todos), "todos": todos[:MAX_ASKS]}


def _totals(entries: list[dict], builds: dict) -> dict:
    phases = {"done": 0, "running": 0, "open": 0, "total": 0}
    for e in entries:
        for key in phases:
            phases[key] += (e["phases"] or {}).get(key, 0)
    up = [e for e in entries if e["up"]]
    return {
        "swarms": len(entries),
        "up": len(up),
        "phases": phases,
        # Sessions at work: the workers that hold a slot, on the swarms that are up.
        "workers": {"busy": sum(e["workers"].get("busy", 0) for e in up),
                    "total": sum(e["workers"].get("total", 0) for e in up)},
        "builds": builds,
    }


def _usage(entries: list[dict], feeds: list[Feed]) -> dict | None:
    """The account's two usage windows, from whichever swarm read them last:
    the swarms of one machine share one subscription. None before any reading."""
    best = None
    for entry, feed in zip(entries, feeds):
        limits = getattr(feed.dash, "limits", None)
        if limits is not None and (best is None or limits.observed_at > best[0].observed_at):
            best = (limits, entry["name"])
    if best is None:
        return None
    limits, name = best
    return {
        "read_at": limits.observed_at, "from": name,
        "five": {"pct": limits.five_pct, "resets_at": limits.five_resets_at},
        "week": {"pct": limits.week_pct, "resets_at": limits.week_resets_at},
    }
