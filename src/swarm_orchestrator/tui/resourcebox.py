"""The home screen's resources box: the host and the builds, at a glance.

A few lines from the sampler's snapshot (``<state>/resources-now.json``, see
:mod:`swarm_orchestrator.resources`): CPU, memory with page cache apart, swap,
pressure, disk throughput and real free space, then one line per build running
on the machine's gate. Another swarm's build carries that swarm's name, and
only this swarm's own is marked as an idle holder: a neighbour's idle build is
said to be idle and left to its own swarm. History and the capacity estimate
are ``swarm resources``; this box only says what is happening now.

Text in, text out: :func:`box_lines` does no I/O.
"""

from __future__ import annotations

from rich.markup import escape

from ..resources.view import STALE_S, who
from .theme import BAD, MUTED, WARN, paint


def _g(mb) -> str:
    return "?" if mb is None else f"{mb / 1024:.1f}G"


def _p(v) -> str:
    return "?" if v is None else f"{v:.0f}"


def box_lines(snap: dict | None, now: float) -> list[str]:
    if not snap:
        return [paint("no samples yet (the sampler runs in the supervisor)", MUTED)]
    age = now - (snap.get("ts") or 0)
    if age > STALE_S:
        return [paint(f"sampler not running: last sample {age / 60:.0f} min ago", MUTED)]
    h = snap.get("host") or {}
    st = snap.get("static") or {}
    psi = h.get("psi") or {}
    d = snap.get("disk") or {}
    lines = [
        f"cpu {_p(h.get('cpu'))}% of {st.get('ncpu', '?')} · load {h.get('load', '?')}"
        f" · swap {_g(h.get('swap_mb'))}",
        f"avail {_g(h.get('avail_mb'))} · anon {_g(h.get('anon_mb'))}"
        f" · cache {_g(h.get('cache_mb'))}",
        f"psi mem {_p(psi.get('mem'))}/{_p(psi.get('memf'))}% io {_p(psi.get('io'))}/"
        f"{_p(psi.get('iof'))}% · write {h.get('wr_mbs', '?')} MB/s"
        + (f" · free {d['headroom_gb']:.0f}G" if d.get("headroom_gb") is not None else ""),
    ]
    builds = snap.get("builds") or []
    if not builds:
        lines.append(paint(f"no build running · {snap.get('queued', 0)} queued", MUTED))
    for b in builds:
        text = (f"slot {b.get('slot')} {escape(who(b))} {b['age_s'] / 60:.0f}m"
                f" · {b['cores']:.1f} cores · {_g(b['anon_mb'])} anon")
        theirs = b.get("mine") is False
        if b.get("yielded"):  # the gate set it aside: nothing queues behind it
            lines.append(paint(text + " · idle, slot released", MUTED if theirs else WARN))
        elif b.get("idle"):
            lines.append(paint(text + " · idle (its swarm's to look at)", MUTED) if theirs
                         else paint(text + " · IDLE holder", BAD))
        else:
            lines.append(text)
    if builds and snap.get("queued"):
        ours = snap.get("queued_mine")
        lines.append(paint(f"{snap['queued']} build(s) queued behind"
                           + (f" ({ours} this swarm's)" if ours is not None else ""),
                           MUTED if ours == 0 else WARN))
    return lines


def subtitle(snap: dict | None) -> str:
    n = len((snap or {}).get("builds") or [])
    return f"{n} build(s) · swarm resources" if snap else "swarm resources"
