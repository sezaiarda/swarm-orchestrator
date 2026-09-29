"""The Graph tab: the layered layout, which rows a view draws, and the served JSON."""

from __future__ import annotations

import json
from itertools import combinations
from types import SimpleNamespace

from swarm_orchestrator.web import graph as graph_mod
from swarm_orchestrator.web import layout as layout_mod
from swarm_orchestrator.web.redact import MARK

STEP = layout_mod.NODE_W + layout_mod.GAP_X

# a -> b -> d, a -> c -> d, d -> e, plus a long edge a -> e and a lone row z.
ORDER = ["a", "b", "c", "d", "e", "z"]
EDGES = [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d"), ("d", "e"), ("a", "e")]


def _layer(lay, n: str) -> int:
    return round(lay.pos[n][0] / STEP)


def test_layers_follow_the_longest_path():
    lay = layout_mod.layout(ORDER, EDGES)
    comp = {n: _layer(lay, n) for n in "abcde"}
    base = comp["a"]
    assert {n: v - base for n, v in comp.items()} == {"a": 0, "b": 1, "c": 1, "d": 2, "e": 3}
    assert lay.layers == 4


def test_a_row_that_needs_nothing_sits_just_before_its_first_user():
    lay = layout_mod.layout(["x", "y1", "y2", "y3", "q"],
                           [("x", "y1"), ("y1", "y2"), ("y2", "y3"), ("q", "y3")])
    # q needs nothing, so it is pulled right next to y3 instead of column 0.
    assert _layer(lay, "q") == _layer(lay, "y3") - 1


def test_no_two_boxes_overlap():
    lay = layout_mod.layout(ORDER, EDGES)
    w, h = layout_mod.NODE_W, layout_mod.NODE_H
    for p, q in combinations(lay.pos, 2):
        (x1, y1), (x2, y2) = lay.pos[p], lay.pos[q]
        assert x1 + w <= x2 or x2 + w <= x1 or y1 + h <= y2 or y2 + h <= y1, (p, q)


def test_the_layout_is_deterministic():
    one = layout_mod.layout(ORDER, EDGES)
    two = layout_mod.layout(list(ORDER), list(EDGES))
    assert one.pos == two.pos and one.edges == two.edges


def test_a_long_edge_bends_through_the_columns_between():
    lay = layout_mod.layout(ORDER, EDGES)
    long_edge = next(pts for a, b, pts in lay.edges if (a, b) == ("a", "e"))
    assert len(long_edge) == 2 * 2 * 2  # two columns between, two points each
    short = next(pts for a, b, pts in lay.edges if (a, b) == ("a", "b"))
    assert short == []


def test_a_cycle_is_laid_out_not_looped_forever():
    lay = layout_mod.layout(["p", "q", "r"], [("p", "q"), ("q", "r"), ("r", "p")])
    assert set(lay.pos) == {"p", "q", "r"} and len(lay.edges) == 3


def test_unknown_self_and_duplicate_edges_are_dropped():
    lay = layout_mod.layout(["a", "b"], [("a", "b"), ("a", "b"), ("a", "a"), ("a", "nope")])
    assert [(a, b) for a, b, _ in lay.edges] == [("a", "b")]


def test_reduce_drops_only_what_another_path_implies():
    kept = layout_mod.reduce(ORDER, EDGES)
    assert ("a", "e") not in kept
    assert set(kept) == set(EDGES) - {("a", "e")}


def test_reduce_keeps_the_edge_that_closes_a_cycle():
    edges = [("p", "q"), ("q", "r"), ("r", "p")]
    assert sorted(layout_mod.reduce(["p", "q", "r"], edges)) == sorted(edges)


# -- which rows a view draws --------------------------------------------------------
GRAPH = {"d1": set(), "d2": {"d1"}, "o1": {"d2"}, "o2": {"o1"}, "x1": {"d1"},
         "y1": {"o1"}, "w1": set()}
ORDER2 = ["d1", "d2", "o1", "o2", "x1", "y1", "w1"]
CARDS = {
    "d1": {"id": "d1", "c": "d", "col": "done", "t": "one"},
    "d2": {"id": "d2", "c": "d", "col": "operator", "t": "two"},
    "o1": {"id": "o1", "c": "o", "col": "building", "t": "open one"},
    "o2": {"id": "o2", "c": "o", "col": "blocked", "t": "open two", "unmet": 1, "root": "o1"},
    "x1": {"id": "x1", "c": "x", "col": "ready", "t": "ready"},
    "y1": {"id": "y1", "c": "y", "col": "blocked", "t": "other book", "unmet": 1},
    "w1": {"id": "w1", "c": "w", "col": "blocked", "sub": "waits until 2099-01-01"},
}


def test_status_colours():
    got = {p: graph_mod.status(c) for p, c in CARDS.items()}
    assert got == {"d1": "done", "d2": "done", "o1": "running", "o2": "blocked",
                   "x1": "ready", "y1": "blocked", "w1": "waiting"}
    assert graph_mod.status({"col": "needs_you"}) == graph_mod.status({"col": "excluded"}) == "owner"


def test_open_view_is_open_rows_plus_their_done_frontier():
    nodes, ctx = graph_mod.select(GRAPH, ORDER2, CARDS, graph_mod.OPEN)
    assert set(nodes) == {"o1", "o2", "x1", "y1", "w1", "d1", "d2"}
    assert ctx == {"d1", "d2"}  # d2 is o1's need, d1 is x1's


def test_all_view_has_every_row():
    nodes, ctx = graph_mod.select(GRAPH, ORDER2, CARDS, graph_mod.ALL)
    assert nodes == ORDER2 and ctx == set()


def test_a_book_view_adds_its_neighbours_as_context():
    nodes, ctx = graph_mod.select(GRAPH, ORDER2, CARDS, graph_mod.OPEN, book="o")
    assert set(nodes) == {"o1", "o2", "d2", "y1"}
    assert ctx == {"d2", "y1"}  # what it needs, and the other book's row it unblocks


def test_critical_path_walks_back_from_the_forecast_end():
    live = {"o1", "o2", "y1", "x1"}
    etas = {"o1": [10, 20], "o2": [50, 60], "y1": [40, 45], "x1": [5, 6]}
    assert graph_mod.critical_path(GRAPH, live, etas, ("o2",)) == ["o1", "o2"]
    # With no forecast end it starts at the latest finish; no etas -> the deepest chain.
    assert graph_mod.critical_path(GRAPH, live, {}, ()) in (["o1", "o2"], ["o1", "y1"])


def _board(cards: dict) -> dict:
    return {"version": 7, "columns": [{"key": "x", "cards": list(cards.values())}],
            "campaigns": [{"name": "o", "open": 2, "total": 2}]}


def test_view_json_shape_and_layout_reuse():
    layouts = graph_mod.Layouts()
    fc = SimpleNamespace(critical=("o2",), books=())
    got = graph_mod.view(_board(CARDS), GRAPH, ORDER2, forecast=fc, layouts=layouts)
    ids = [n["id"] for n in got["nodes"]]
    assert set(ids) == {"o1", "o2", "x1", "y1", "w1", "d1", "d2"}
    assert {"w", "h", "nw", "nh", "edges", "critical", "counts", "books", "layout_ms"} <= set(got)
    for a, b, pts in got["edges"]:
        assert isinstance(a, int) and isinstance(b, int) and len(pts) % 2 == 0
    node = {n["id"]: n for n in got["nodes"]}
    assert node["d2"].get("k") == 1 and "k" not in node["o1"]
    assert node["o2"]["n"] == ["o1"] and node["o2"]["w"] == "waits on o1"
    assert got["critical"] == ["o1", "o2"]
    assert got["counts"] == {"running": 1, "blocked": 2, "ready": 1, "waiting": 1}
    # A re-colour (same shape) reuses the cached layout object.
    recoloured = {**CARDS, "o1": {**CARDS["o1"], "col": "merging"}}
    first = layouts.get(*_shape(got))
    again = graph_mod.view(_board(recoloured), GRAPH, ORDER2, forecast=fc, layouts=layouts)
    assert layouts.get(*_shape(again)) is first


def _shape(got: dict):
    nodes = [n["id"] for n in got["nodes"]]
    edges = [(d, n["id"]) for n in got["nodes"] if "k" not in n for d in n.get("n", [])
             if d in nodes]
    return nodes, edges


# -- served ----------------------------------------------------------------------------
def test_feed_serves_a_cached_redacted_graph(tmp_path, monkeypatch):
    from test_web_board import make_run

    from swarm_orchestrator.web.feed import Feed

    feed = Feed(make_run(tmp_path, monkeypatch))
    feed.refresh(force=True)
    body, gz, etag = feed.graph("all")
    got = json.loads(body)
    assert {n["id"] for n in got["nodes"]} >= {"al-W0", "al-W9", "be-W9"}
    assert feed.graph("all")[0] is body  # same version: not rebuilt
    open_ids = {n["id"] for n in json.loads(feed.graph("open")[0])["nodes"]}
    assert "be-W5" not in open_ids and "al-W9" in open_ids and "be-W4" in open_ids  # frontier
    # Every string leaves through the redactor.
    for col in feed.board["columns"]:
        for card in col["cards"]:
            if card["id"] == "al-W1":
                card["t"] = "leak ghp_abcdefghijklmnopqrstuvwxyz0123"
    feed.version += 1
    body = feed.graph("all")[0].decode()
    assert "ghp_abc" not in body and MARK in body
