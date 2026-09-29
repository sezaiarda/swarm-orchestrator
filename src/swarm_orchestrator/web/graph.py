"""The Graph tab's data: which phases to draw, their status, and where they go.

A view is chosen by ``mode`` and ``book``:

* ``open`` (the default) — every row still owed work, plus the **done
  frontier**: the landed rows an open row needs, so every open row's lines
  have somewhere to start.
* ``all`` — every row in the ledger.
* ``book`` — narrows either to one phase book's rows, plus their direct
  neighbours in other books as context (what they need, what they unblock).

Lines are the transitive reduction of ``needs:`` (:func:`layout.reduce`): what
blocks what is unchanged, the ink halves. Each node still carries its full
list of direct needs for the side panel.

The layout is the expensive part (tens of milliseconds), and it depends only on
which nodes and which lines are drawn — not on their colours. So layouts are
cached by that shape in :class:`Layouts`, and a status change (a row starting,
landing) re-colours the cached picture instead of re-laying it out.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict

from . import layout as layout_mod
from .rows import clip

OPEN, ALL = "open", "all"
MODES = (OPEN, ALL)

#: What the graph colours by, in legend order.
RUNNING, READY, BLOCKED, WAITING, OWNER, DONE, FAILED = (
    "running", "ready", "blocked", "waiting", "owner", "done", "failed")
STATUSES = (RUNNING, READY, BLOCKED, WAITING, OWNER, FAILED, DONE)

#: A node's title on the wire: enough to recognise it in the side panel.
TITLE_CHARS = 110


def status(card: dict | None) -> str:
    """A board card's column as one of :data:`STATUSES`."""
    if card is None:
        return BLOCKED
    col = card.get("col")
    if col in ("building", "merging"):
        return RUNNING
    if col in ("done", "operator"):
        return DONE
    if col in ("needs_you", "excluded"):
        return OWNER
    if col == "failed":
        return FAILED
    if col == "ready":
        return READY
    if col == "blocked" and not card.get("unmet") and str(card.get("sub", "")).startswith(
            "waits until"):
        return WAITING
    return BLOCKED


def select(graph: dict[str, set[str]], order: list[str], cards: dict[str, dict],
           mode: str = OPEN, book: str = "") -> tuple[list[str], set[str]]:
    """``(nodes in ledger order, context nodes)`` for a view.

    Context nodes are drawn for their lines only: the done frontier in ``open``
    mode, and another book's neighbours when ``book`` is set.
    """
    stat = {p: status(cards.get(p)) for p in order}
    subject = [p for p in order if mode == ALL or stat[p] != DONE]
    if book:
        subject = [p for p in subject if (cards.get(p) or {}).get("c") == book]
    chosen = set(subject)
    context: set[str] = set()
    for p in subject:
        for d in graph.get(p, ()):
            if d not in chosen:
                context.add(d)
    if book:
        wanted = set(subject)
        for p in order:
            if p in chosen or (mode == OPEN and stat[p] == DONE):
                continue
            if graph.get(p, set()) & wanted:
                context.add(p)
    known = [p for p in order if p in chosen or p in context]
    # A need that names no ledger row still gets a box: the line must end somewhere.
    extra = sorted(context - set(known))
    return known + extra, context


class Layouts:
    """Layouts by shape, most recently used kept; safe to share between threads."""

    def __init__(self, keep: int = 8) -> None:
        self._keep = keep
        self._got: OrderedDict[str, tuple[layout_mod.Layout, list, float]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, nodes: list[str], edges: list[tuple[str, str]]):
        """``(layout, drawn edges, milliseconds it took)`` — cached by shape."""
        key = hashlib.sha1(("\n".join(nodes) + "\x01" + "\n".join(
            f"{a}\x00{b}" for a, b in edges)).encode()).hexdigest()
        with self._lock:
            hit = self._got.get(key)
            if hit is not None:
                self._got.move_to_end(key)
                return hit
            t0 = time.perf_counter()
            drawn = layout_mod.reduce(nodes, edges)
            lay = layout_mod.layout(nodes, drawn)
            got = (lay, drawn, round((time.perf_counter() - t0) * 1000, 1))
            self._got[key] = got
            while len(self._got) > self._keep:
                self._got.popitem(last=False)
            return got


def view(board: dict, graph: dict[str, set[str]], order: list[str], *, forecast=None,
         mode: str = OPEN, book: str = "", layouts: Layouts | None = None) -> dict:
    """The Graph tab's JSON for one view of ``board`` (already redacted)."""
    mode = mode if mode in MODES else OPEN
    cards = {c["id"]: c for col in board.get("columns", []) for c in col.get("cards", [])}
    nodes, context = select(graph, order, cards, mode, book)
    at = {n: i for i, n in enumerate(nodes)}
    edges = [(d, p) for p in nodes if p not in context or book
             for d in sorted(graph.get(p, ())) if d in at]
    lay, drawn, took = (layouts or Layouts()).get(nodes, edges)
    etas = _etas(forecast)
    out_nodes = []
    counts: dict[str, int] = {}
    for n in nodes:
        card = cards.get(n) or {}
        s = status(card) if n in cards else BLOCKED
        x, y = lay.pos[n]
        node = {"id": n, "c": card.get("c", n.split("-")[0]), "s": s,
                "t": clip(card.get("t", ""), TITLE_CHARS), "x": x, "y": y}
        if n in context:
            node["k"] = 1
        else:
            counts[s] = counts.get(s, 0) + 1
        if card.get("r"):
            node["r"] = card["r"]
        why = _why(card)
        if why:
            node["w"] = why
        if n in etas:
            node["e"] = etas[n]
        needs = sorted(graph.get(n, ()))
        if needs:
            node["n"] = needs
        out_nodes.append(node)
    live = {n for n in nodes if n not in context and status(cards.get(n)) != DONE}
    crit = critical_path(graph, live, etas, getattr(forecast, "critical", None) or ())
    books = [{"name": c["name"], "open": c.get("open", 0), "total": c.get("total", 0)}
             for c in board.get("campaigns", [])]
    return {
        "mode": mode, "book": book, "version": board.get("version"),
        "w": round(lay.width, 1), "h": round(lay.height, 1),
        "nw": layout_mod.NODE_W, "nh": layout_mod.NODE_H, "layers": lay.layers,
        "nodes": out_nodes,
        "edges": [[at[a], at[b], pts] for a, b, pts in lay.edges],
        "direct": len(edges), "critical": crit, "counts": counts, "books": books,
        "layout_ms": took,
    }


def critical_path(graph: dict[str, set[str]], live: set[str], etas: dict[str, list],
                  tail=()) -> list[str]:
    """The chain of open rows that sets "all done", first row first.

    The forecast names the chain's end (the rows that finish last in the
    median replay, :attr:`Forecast.critical`); this walks back from there
    through ``needs:``, each step to the open row that finishes latest — by
    its own forecast, else by how long a chain of open rows it sits at the
    end of — so the path reaches back to what is running or ready now.
    """
    depth = _depths(graph, live)

    def key(p: str):
        e = etas.get(p)
        return (e[0] if e else 0, depth.get(p, 1), p)

    ends = [p for p in tail if p in live]
    if ends:
        start = ends[-1]
    elif live:
        start = max(live, key=key)
    else:
        return []
    chain, seen = [start], {start}
    while True:
        needs = [d for d in graph.get(chain[-1], ()) if d in live and d not in seen]
        if not needs:
            break
        nxt = max(needs, key=key)
        chain.append(nxt)
        seen.add(nxt)
    return chain[::-1]


def _depths(graph: dict[str, set[str]], live: set[str]) -> dict[str, int]:
    """How many open rows long the chain ending at each open row is (1 = needs none).

    Iterative (Kahn): a ledger chain can run hundreds of rows deep. Rows left in
    a cycle keep the depth they had reached.
    """
    needs = {p: [d for d in graph.get(p, ()) if d in live] for p in live}
    users: dict[str, list[str]] = {p: [] for p in live}
    for p, ds in needs.items():
        for d in ds:
            users[d].append(p)
    left = {p: len(ds) for p, ds in needs.items()}
    depth = {p: 1 for p in live}
    todo = [p for p in live if not left[p]]
    while todo:
        p = todo.pop()
        for u in users[p]:
            depth[u] = max(depth[u], depth[p] + 1)
            left[u] -= 1
            if not left[u]:
                todo.append(u)
    return depth


def _why(card: dict) -> str:
    """What holds a row, in a few words (the board card's own reason)."""
    if card.get("root"):
        return f"waits on {card['root']}"
    return str(card.get("sub") or "")


def _etas(fc) -> dict[str, list[float]]:
    """Open row -> ``[p50, p85]`` from the forecast's per-book rows."""
    out: dict[str, list[float]] = {}
    for book in getattr(fc, "books", None) or ():
        for row in book.rows:
            r = row.finish
            if r is not None and r.p50 != float("inf"):
                out[row.id] = [round(r.p50), round(r.p85) if r.p85 != float("inf") else None]
    return out
