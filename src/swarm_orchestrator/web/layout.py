"""A layered, left-to-right layout of the phase graph: pure, deterministic, stdlib.

The Graph tab draws every phase as a box and every ``needs:`` as a line from the
row it needs to the row that needs it, so what blocks what reads left to right.
The layout is the classic Sugiyama pipeline, kept small:

1. **Layers by longest path.** A row sits one column right of the furthest row
   it needs; a row that needs nothing is pulled right to sit just before the
   first row that needs it, so a lone dependency does not strand its line
   across the whole width. Rows in a ``needs:`` cycle (the ledger validator
   reports those) are layered ignoring the edge that closes the cycle.
2. **Dummy points** on every edge that spans more than one column, so a long
   edge is ordered — and routed — like a chain of short ones.
3. **Barycenter ordering** in a few down/up sweeps, each keeping the best
   ordering seen by crossing count; ties break by ledger order, so the same
   ledger always yields the same picture.
4. **Coordinates**: each box is placed at the mean height of its neighbours in
   the column before (then after), with the order kept and boxes kept apart by
   an isotonic (pool-adjacent-violators) fit — the least movement that removes
   every overlap.
5. **Packing**: each connected component is laid out on its own and the
   components are shelved, biggest first, rows with no line at all last.

Coordinates are in abstract pixels; the client scales them with an SVG viewBox.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A phase box, and the room around it.
NODE_W = 200
NODE_H = 46
GAP_X = 70
GAP_Y = 14
#: A dummy point: an edge passing through a column.
DUMMY_H = 4
DUMMY_GAP = 5
#: Space between packed components, and the width a shelf fills before wrapping.
PACK_GAP = 48
MIN_SHELF_W = 6 * (NODE_W + GAP_X)
#: Ordering sweeps (each a down and an up pass) and coordinate passes.
SWEEPS = 4
PASSES = 4


@dataclass(frozen=True)
class Layout:
    """Where everything goes.

    ``pos`` is each box's top-left corner; ``edges`` holds every drawn edge as
    ``(need, needer, points)`` where ``points`` are the bends between the two
    boxes, flat ``[x0, y0, x1, y1, …]`` (empty for a one-column edge).
    """

    pos: dict[str, tuple[float, float]]
    edges: list[tuple[str, str, list[float]]]
    width: float
    height: float
    layers: int
    crossings: int


def layout(order: list[str], edges: list[tuple[str, str]]) -> Layout:
    """Lay out ``order`` (node ids, ledger order) with ``(need, needer)`` edges.

    Edges naming a node not in ``order``, self-loops and duplicates are dropped.
    """
    index = {n: i for i, n in enumerate(dict.fromkeys(order))}
    nodes = list(index)
    clean = _clean(edges, index)
    comps = _components(nodes, clean, index)
    boxes = []
    for members in comps:
        mine = set(members)
        boxes.append(_one(members, [e for e in clean if e[0] in mine], index))
    return _pack(boxes)


def _clean(edges, index: dict[str, int]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for a, b in edges:
        if a != b and a in index and b in index and (a, b) not in seen:
            seen.add((a, b))
            out.append((a, b))
    return out


def reduce(order: list[str], edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The transitive reduction: ``edges`` minus every edge another path implies.

    If ``c`` needs ``b`` and ``b`` needs ``a``, a line from ``a`` to ``c`` says
    nothing the two short lines do not, and on a ledger that repeats its
    transitive needs it doubles the ink. What blocks what (reachability) is
    unchanged. An edge that closes a cycle is always kept.
    """
    index = {n: i for i, n in enumerate(dict.fromkeys(order))}
    clean = _clean(edges, index)
    layer, back = _layers(list(index), clean, index)
    preds: dict[str, list[str]] = {n: [] for n in index}
    for a, b in clean:
        if (a, b) not in back:
            preds[b].append(a)
    below: dict[str, int] = {}  # every node reachable backwards, as a bitset
    for n in sorted(index, key=lambda m: (layer[m], index[m])):
        bits = 0
        for p in preds[n]:
            bits |= below.get(p, 0) | (1 << index[p])
        below[n] = bits
    keep = []
    for a, b in clean:
        if (a, b) in back:
            keep.append((a, b))
            continue
        implied = any(o != a and below.get(o, 0) >> index[a] & 1 for o in preds[b])
        if not implied:
            keep.append((a, b))
    return keep


# -- components ------------------------------------------------------------------
def _components(nodes: list[str], edges: list[tuple[str, str]],
                index: dict[str, int]) -> list[list[str]]:
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            if index[ra] > index[rb]:
                ra, rb = rb, ra
            parent[rb] = ra
    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(find(n), []).append(n)
    return sorted(groups.values(), key=lambda g: (-len(g), index[g[0]]))


# -- one component -------------------------------------------------------------------
@dataclass
class _Box:
    pos: dict[str, tuple[float, float]]
    edges: list[tuple[str, str, list[float]]]
    width: float
    height: float
    layers: int
    crossings: int
    first: int


def _layers(members: list[str], edges: list[tuple[str, str]],
            index: dict[str, int]) -> tuple[dict[str, int], set[tuple[str, str]]]:
    """``(layer of each node, edges ignored because they close a cycle)``."""
    preds: dict[str, list[str]] = {n: [] for n in members}
    succs: dict[str, list[str]] = {n: [] for n in members}
    for a, b in edges:
        preds[b].append(a)
        succs[a].append(b)
    indeg = {n: len(preds[n]) for n in members}
    ready = sorted((n for n in members if not indeg[n]), key=index.__getitem__)
    topo: list[str] = []
    placed: set[str] = set()
    back: set[tuple[str, str]] = set()
    remaining = sorted(members, key=index.__getitem__)
    while len(topo) < len(members):
        if not ready:
            # A cycle: release the earliest unplaced row, ignoring what closes it.
            n = next(m for m in remaining if m not in placed)
            for p in preds[n]:
                if p not in placed:
                    back.add((p, n))
            indeg[n] = 0
            ready = [n]
        n = ready.pop(0)
        if n in placed:
            continue
        placed.add(n)
        topo.append(n)
        fresh = []
        for s in succs[n]:
            if s in placed or (n, s) in back:
                continue
            indeg[s] -= 1
            if indeg[s] == 0:
                fresh.append(s)
        ready = sorted(ready + fresh, key=index.__getitem__)
    layer: dict[str, int] = {}
    for n in topo:
        layer[n] = max((layer[p] + 1 for p in preds[n] if (p, n) not in back and p in layer),
                       default=0)
    # Pull a row that needs nothing right, next to the first row that needs it.
    for n in reversed(topo):
        if not any((p, n) not in back for p in preds[n]):
            after = [layer[s] for s in succs[n] if (n, s) not in back]
            if after:
                layer[n] = max(layer[n], min(after) - 1)
    return layer, back


def _one(members: list[str], edges: list[tuple[str, str]], index: dict[str, int]) -> _Box:
    layer, back = _layers(members, edges, index)
    depth = max(layer.values(), default=0) + 1
    # Every edge becomes a chain of unit steps through dummy points.
    ranks: list[list[str]] = [[] for _ in range(depth)]
    for n in sorted(members, key=index.__getitem__):
        ranks[layer[n]].append(n)
    up: dict[str, list[str]] = {n: [] for n in members}
    down: dict[str, list[str]] = {n: [] for n in members}
    chains: list[tuple[str, str, list[str]]] = []
    height_of: dict[str, float] = {n: NODE_H for n in members}
    for a, b in edges:
        if (a, b) in back or layer[b] <= layer[a]:
            chains.append((a, b, []))
            continue
        prev, mid = a, []
        for k in range(layer[a] + 1, layer[b]):
            d = f"\x00{a}\x00{b}\x00{k}"
            height_of[d] = DUMMY_H
            up[d], down[d] = [prev], []
            down[prev].append(d)
            ranks[k].append(d)
            mid.append(d)
            prev = d
        down[prev].append(b)
        up[b].append(prev)
        chains.append((a, b, mid))
    ranks, crossings = _order(ranks, up, down)
    ys = _coords(ranks, up, down, height_of)
    step = NODE_W + GAP_X
    pos = {n: (layer[n] * step, ys[n] - NODE_H / 2) for n in members}
    out_edges = []
    for a, b, mid in chains:
        pts: list[float] = []
        for d in mid:
            x = int(d.rsplit("\x00", 1)[1]) * step
            pts += [x - GAP_X * 0.15, ys[d], x + NODE_W + GAP_X * 0.15, ys[d]]
        out_edges.append((a, b, [round(v, 1) for v in pts]))
    top = min((y for _, y in pos.values()), default=0.0)
    top = min([top] + [ys[d] - DUMMY_H for mid in (c[2] for c in chains) for d in mid])
    pos = {n: (x, round(y - top, 1)) for n, (x, y) in pos.items()}
    out_edges = [(a, b, [v - top if i % 2 else v for i, v in enumerate(p)])
                 for a, b, p in out_edges]
    bottom = max([y + NODE_H for _, y in pos.values()]
                 + [p[i] for _, _, p in out_edges for i in range(1, len(p), 2)])
    return _Box(pos, out_edges, depth * step - GAP_X, bottom, depth, crossings,
                min(index[n] for n in members))


# -- ordering --------------------------------------------------------------------------
def _crossings(upper: list[str], lower: list[str], up: dict[str, list[str]]) -> int:
    """Edge crossings between two adjacent columns (merge-count of inversions)."""
    at = {n: i for i, n in enumerate(upper)}
    seq = [at[p] for n in lower for p in sorted(up[n], key=lambda q: at.get(q, 0)) if p in at]
    return _inversions(seq)


def _inversions(seq: list[int]) -> int:
    if len(seq) < 2:
        return 0
    size = max(seq) + 2
    tree = [0] * (size + 1)
    count = 0
    for i, v in enumerate(seq):
        # How many earlier values are greater than v.
        j, le = v + 1, 0
        while j > 0:
            le += tree[j]
            j -= j & -j
        count += i - le
        j = v + 1
        while j <= size:
            tree[j] += 1
            j += j & -j
    return count


def _total(ranks: list[list[str]], up: dict[str, list[str]]) -> int:
    return sum(_crossings(ranks[k - 1], ranks[k], up) for k in range(1, len(ranks)))


def _sweep(ranks: list[list[str]], nbrs: dict[str, list[str]], rng) -> None:
    for k in rng:
        ref = {n: i for i, n in enumerate(ranks[k - 1 if rng.step > 0 else k + 1])}
        cur = ranks[k]
        keyed = []
        for i, n in enumerate(cur):
            got = [ref[m] for m in nbrs[n] if m in ref]
            # A row with no neighbour on that side keeps its place.
            keyed.append((sum(got) / len(got) * len(cur) / max(1, len(ref)) if got else i, i, n))
        keyed.sort(key=lambda t: (t[0], t[1]))
        ranks[k] = [n for _, _, n in keyed]


def _order(ranks: list[list[str]], up: dict[str, list[str]],
           down: dict[str, list[str]]) -> tuple[list[list[str]], int]:
    best = [list(r) for r in ranks]
    best_x = _total(best, up)
    cur = [list(r) for r in ranks]
    for _ in range(SWEEPS):
        if best_x == 0:
            break
        _sweep(cur, up, range(1, len(cur)))
        _sweep(cur, down, range(len(cur) - 2, -1, -1))
        x = _total(cur, up)
        if x < best_x:
            best, best_x = [list(r) for r in cur], x
    return best, best_x


# -- coordinates -------------------------------------------------------------------------
def _gap(a: str, b: str, h: dict[str, float]) -> float:
    real = h[a] == NODE_H and h[b] == NODE_H
    return (h[a] + h[b]) / 2 + (GAP_Y if real else DUMMY_GAP)


def _fit(want: list[float], seps: list[float]) -> list[float]:
    """The ys nearest ``want`` (least squares) with ``y[i+1] - y[i] >= seps[i]``."""
    off = [0.0]
    for s in seps:
        off.append(off[-1] + s)
    blocks: list[list[float]] = []  # [sum, count, value]
    for w, o in zip(want, off):
        blocks.append([w - o, 1.0, w - o])
        while len(blocks) > 1 and blocks[-2][2] > blocks[-1][2]:
            s, c, _ = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
            blocks[-1][2] = blocks[-1][0] / blocks[-1][1]
    out: list[float] = []
    for s, c, v in blocks:
        out += [v] * int(c)
    return [v + o for v, o in zip(out, off)]


def _coords(ranks: list[list[str]], up: dict[str, list[str]], down: dict[str, list[str]],
            h: dict[str, float]) -> dict[str, float]:
    ys: dict[str, float] = {}
    for rank in ranks:
        y = 0.0
        for i, n in enumerate(rank):
            if i:
                y += _gap(rank[i - 1], n, h)
            ys[n] = y
    for p in range(PASSES):
        order = range(1, len(ranks)) if p % 2 == 0 else range(len(ranks) - 2, -1, -1)
        nbrs = up if p % 2 == 0 else down
        for k in order:
            rank = ranks[k]
            want = []
            for n in rank:
                got = [ys[m] for m in nbrs[n]]
                want.append(sum(got) / len(got) if got else ys[n])
            seps = [_gap(rank[i], rank[i + 1], h) for i in range(len(rank) - 1)]
            for n, y in zip(rank, _fit(want, seps)):
                ys[n] = y
    return ys


# -- packing ------------------------------------------------------------------------------
def _pack(boxes: list[_Box]) -> Layout:
    """Shelve the components: biggest first, each shelf as wide as the widest box."""
    shelf_w = max([MIN_SHELF_W] + [b.width for b in boxes])
    pos: dict[str, tuple[float, float]] = {}
    edges: list[tuple[str, str, list[float]]] = []
    x = y = shelf_h = 0.0
    width = 0.0
    for b in boxes:
        if x and x + b.width > shelf_w:
            y += shelf_h + PACK_GAP
            x = shelf_h = 0.0
        for n, (nx, ny) in b.pos.items():
            pos[n] = (round(nx + x), round(ny + y))
        for a, c, pts in b.edges:
            edges.append((a, c, [round(v + (y if i % 2 else x)) for i, v in enumerate(pts)]))
        width = max(width, x + b.width)
        shelf_h = max(shelf_h, b.height)
        # Single boxes pack tight; a component keeps a margin around it.
        x += b.width + (GAP_X if len(b.pos) == 1 else PACK_GAP + GAP_X)
    height = y + shelf_h if boxes else 0.0
    return Layout(pos=pos, edges=edges, width=round(width), height=round(height),
                  layers=max((b.layers for b in boxes), default=0),
                  crossings=sum(b.crossings for b in boxes))
