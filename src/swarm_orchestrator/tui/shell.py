"""The cockpit frame: a status bar that never leaves, and the tab it wraps.

The first dashboard put everything worth knowing on one tab, so the moment you
went to look at anything else you lost sight of the run. That is backwards for a
window someone leaves open all day and glances at: the few facts that decide
whether to care — is it alive, is anything waiting on me, how far along is the
campaign — have to be true on *every* screen.

So the frame owns those facts and the tabs own detail. :class:`StatusBar` is
one line, always visible, and it is deliberately the only place in the app that
is allowed to shout: a blocker turns it amber, a dead supervisor turns it red.
Everything else reports; this decides whether you look up.
"""

from __future__ import annotations

from textual.containers import Horizontal
from textual.widgets import Static

from . import campaign as camp
from .data import fmt_ago, fmt_duration
from .theme import ACCENT, BAD, COLOR, INFO, MUTED, OK, WARN, paint

# A run is "quiet" long before it is broken. These are the thresholds at which
# silence stops being normal -- a supervisor that has died leaves its slots
# reading busy, so the time since anything happened is what gives it away.
STALE_WARN_S = 30 * 60
STALE_BAD_S = 2 * 60 * 60


class StatusBar(Static):
    """One always-visible line: the run's identity, health and headline number."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        padding: 0 1;
        background: #161b22;
        color: #c9d1d9;
    }
    StatusBar.-attention { background: #3d2e00; }
    StatusBar.-bad { background: #3d1418; }
    """

    def update_from(self, dash) -> None:
        snap = getattr(dash, "snapshot", None)
        if snap is None or not getattr(snap, "ok", False):
            self.set_classes("")
            self.update(paint("  no run yet — `swarm up` to start", MUTED))
            return

        parts: list[str] = []
        # 1. Who and whether it is alive.
        if not snap.supervisor_alive:
            parts.append(f"[{COLOR[BAD]}]● SUPERVISOR DOWN[/]")
        elif snap.finished:
            parts.append(f"[{COLOR[MUTED]}]● finished[/]")
        elif snap.paused:
            parts.append(f"[{COLOR[WARN]}]● paused[/]")
        else:
            parts.append(f"[{COLOR[OK]}]● live[/]")
        parts.append(f"[b]{self.app.cfg.session}[/b]")

        # 2. Capacity, as a fraction rather than prose.
        busy = sum(1 for s in snap.slots if s.busy)
        parts.append(f"[{COLOR[MUTED]}]slots[/] {busy}/{len(snap.slots)}")

        # 3. The headline number -- the ACTIVE campaign, never the whole ledger.
        # A count over the whole ledger is true and useless: most rows are skips seeded from
        # campaigns that ended long ago, so the number could not move.
        cs = camp.summarise(
            getattr(dash, "graph", {}) or {},
            dict(snap.done),
            {s.phase for s in snap.slots if s.busy and s.phase},
            set(getattr(self.app.cfg, "exclude", []) or []),
        )
        act = camp.active(cs)
        if act:
            parts.append(
                f"[{COLOR[ACCENT]}]{act.name}[/] {act.built}/{act.live_total}"
                f" [{COLOR[MUTED]}]({act.pct:.0f}%)[/]"
            )

        # 4. Uptime, and how long since anything actually happened.
        if snap.uptime_s:
            parts.append(f"[{COLOR[MUTED]}]up[/] {fmt_duration(snap.uptime_s)}")
        state = ""
        if snap.last_event_at:
            import time

            idle = time.time() - snap.last_event_at
            colour = MUTED
            if busy and idle > STALE_BAD_S:
                colour, state = BAD, "-bad"
            elif busy and idle > STALE_WARN_S:
                colour, state = WARN, "-attention"
            parts.append(f"[{COLOR[colour]}]idle {fmt_ago(snap.last_event_at)}[/]")

        # 5. The one thing that outranks everything else on this line.
        if snap.blockers:
            n = len(snap.blockers)
            parts.append(
                f"[{COLOR[WARN]}][b]⚠ {n} WAITING ON YOU[/b][/]"
            )
            state = state or "-attention"

        self.set_classes(state)
        self.update("  ".join(parts))


class TabBar(Horizontal):
    """Numbered tab strip. The numbers ARE the shortcuts, so the hint is the UI."""

    DEFAULT_CSS = """
    TabBar {
        height: 1;
        background: #0d1117;
    }
    TabBar > Static { padding: 0 1; color: #8b949e; }
    TabBar > Static.-active { background: #1f6feb; color: #ffffff; text-style: bold; }
    """
