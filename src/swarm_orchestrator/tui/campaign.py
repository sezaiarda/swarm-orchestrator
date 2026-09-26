"""Group the ledger into campaigns, so progress means something.

The first dashboard reported ``0/N (0%)`` for a run that was going perfectly.
It was counting every phase the ledger has ever held, including the many that are
``skip`` entries seeded from campaigns that finished long ago, plus a handful of
owner-run gates that are deliberately never scheduled. A number dominated by
ancient history cannot move, so it told the owner nothing except that something
was wrong with the number.

Phases are named ``<campaign>-<slot>`` — ``dash-W3``, ``ivory-W18``, ``inventory-P4``.
Grouping on that prefix recovers the unit people actually think in: *the dash
campaign is 6 of 23 done, and these four are ready next*. The **active** campaign
is the one with work in flight or ready; everything else is history and is
summarised in a line rather than averaged into the headline.

Nothing here reads the filesystem: it takes the graph and the done map and
returns dataclasses, so it is trivially testable and cannot fail a render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import statuses

# `dash-W3` / `inventory-P4` / `billing-P0-ops` -> campaign `dash`, `inventory`, `billing`.
# A phase with no dash (`U0`, `I3`, `phase-B`) forms its own single-member group
# rather than being forced into a bucket it does not belong to.
_SPLIT = re.compile(r"^([A-Za-z][A-Za-z0-9_.]*?)-(?:[A-Z]+\d|\d)")

# Done is everything done, whoever did it: built by this run, ticked in the
# ledger or skipped. The web board counts the same way, so both say `billing 16/18`.
# Counting ticked rows as ready would make a campaign read "0 / 104" with 102 of them
# built, and would time the ETA on all 104.
SATISFIED = statuses.SATISFIES_DEPS


def campaign_of(phase: str) -> str:
    """The campaign a phase belongs to, or the phase itself when it stands alone."""
    m = _SPLIT.match(phase)
    return m.group(1) if m else phase


@dataclass(frozen=True)
class Campaign:
    """One campaign's standing. Counts are disjoint and sum to ``total``."""

    name: str
    total: int = 0
    built: int = 0        # done: built, ticked in the ledger, or skipped
    skipped: int = 0      # of those, `skip` rows: seeded, never built by anyone
    failed: int = 0
    running: list[str] = field(default_factory=list)
    ready: list[str] = field(default_factory=list)
    blocked: int = 0
    excluded: int = 0

    @property
    def active(self) -> bool:
        """Is this campaign the one being worked right now?"""
        return bool(self.running or self.ready)

    @property
    def live_total(self) -> int:
        """This campaign's phases, less the owner-run rows it never schedules.

        This is the denominator the owner means by "how far along are we". A
        campaign of 23 waves with 118 unrelated skips elsewhere is 6/23, never
        6/161.
        """
        return max(0, self.total - self.excluded)

    @property
    def pct(self) -> float:
        return 0.0 if self.live_total <= 0 else 100.0 * self.built / self.live_total

    @property
    def complete(self) -> bool:
        return self.live_total > 0 and self.built >= self.live_total


def summarise(
    graph: dict[str, set[str]],
    done: dict[str, str],
    busy: set[str] | None = None,
    excluded: set[str] | None = None,
) -> list[Campaign]:
    """Campaign standings, active first, then by size.

    ``graph`` is the ledger (phase -> deps), ``done`` the state map. A phase is
    *ready* when every dependency is satisfied and it has not been attempted — a
    ``fail`` does NOT satisfy a dependency, matching ledger.SATISFIES_DEPS, so a
    dependent of a failed phase reads as blocked rather than ready.
    """
    busy = busy or set()
    excluded = excluded or set()
    satisfied = {p for p, s in done.items() if s in SATISFIED}

    buckets: dict[str, dict] = {}
    for phase, deps in graph.items():
        name = campaign_of(phase)
        b = buckets.setdefault(
            name,
            {"total": 0, "built": 0, "skipped": 0, "failed": 0,
             "running": [], "ready": [], "blocked": 0, "excluded": 0},
        )
        b["total"] += 1
        status = done.get(phase)
        if phase in busy:
            b["running"].append(phase)
        elif status in SATISFIED:
            b["built"] += 1  # a finished owner-run row too, as on the web board
            b["skipped"] += status == statuses.SKIP
        elif phase in excluded:
            b["excluded"] += 1
        elif status == "fail":
            b["failed"] += 1
        elif deps <= satisfied:
            b["ready"].append(phase)
        else:
            b["blocked"] += 1

    out = [
        Campaign(
            name=name,
            total=b["total"],
            built=b["built"],
            skipped=b["skipped"],
            failed=b["failed"],
            running=sorted(b["running"]),
            ready=sorted(b["ready"]),
            blocked=b["blocked"],
            excluded=b["excluded"],
        )
        for name, b in buckets.items()
    ]
    # A finished campaign is never the headline while one with work left idles.
    out.sort(key=lambda c: (not c.active, c.complete, -c.live_total, c.name))
    return out


def active(campaigns: list[Campaign]) -> Campaign | None:
    """The campaign to put in the headline, or None when nothing is in flight."""
    for c in campaigns:
        if c.active:
            return c
    return None


def history_line(campaigns: list[Campaign]) -> str:
    """One line for everything that is not the active campaign."""
    rest = [c for c in campaigns if not c.active and c.built]
    if not rest:
        return ""
    built = sum(c.built for c in rest)
    return f"{built} phase(s) built earlier across {len(rest)} campaign(s)"
