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

Nothing here reads the filesystem: it takes the graph and the done map (or a
dashboard that already holds them, :func:`standings`) and returns dataclasses,
so it is trivially testable and cannot fail a render. The one exception is
:func:`counts`, for a caller that holds a config and a state and no dashboard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import ledger as ledger_mod
from .. import ledgerw, statuses

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
    #: Rows whose worker waits on the owner: it asked a question, on its park
    #: timer in its slot or parked in a window of its own, and no answer came
    #: yet. Its session is alive, so the launcher never starts the row: it is
    #: not ready, and it does not build until the owner answers.
    asking: list[str] = field(default_factory=list)
    ready: list[str] = field(default_factory=list)
    blocked: int = 0
    #: Rows whose dependencies have landed but whose ``after:`` date is ahead.
    dated: int = 0
    excluded: int = 0
    #: Of ``built``, the rows the swarm holds done that the ledger has not
    #: ticked: an operator hand-off still to prove, a standing target it ran.
    held: int = 0

    @property
    def active(self) -> bool:
        """Is this campaign the one being worked right now?"""
        return bool(self.running or self.asking or self.ready)

    @property
    def manned(self) -> bool:
        """Does a worker hold one of its rows, building it or asking about it?"""
        return bool(self.running or self.asking)

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
    ticked: set[str] | None = None,
    deferred: dict[str, str] | set[str] | None = None,
    asking: set[str] | None = None,
) -> list[Campaign]:
    """Campaign standings, active first, then by size.

    ``graph`` is the ledger (phase -> deps), ``done`` the launcher's view of the
    state map (:func:`ledger.with_ticked`). This is THE count: the TUI header,
    the status bar and ``swarm status`` all read it, and the web board places
    its cards by the same rules. Done is a status that releases dependents,
    whoever produced it — built here, ticked in the ledger, skipped. An
    excluded row counts once it is done (an owner-run row the owner ticked is
    progress) and is out of the count until then. ``ticked``, when given, lets
    ``held`` say how many done rows the ledger itself still shows open. A phase is
    *ready* when every dependency is satisfied and it has not been attempted — a
    ``fail`` does NOT satisfy a dependency, matching ledger.SATISFIES_DEPS, so a
    dependent of a failed phase reads as blocked rather than ready. Nor is a
    row that waits for a date still ahead (``deferred``, as
    :func:`ledgerw.dated` reads it): the launcher leaves it alone, so it is
    ``dated``, never ready, and never failed either, even while the record of
    its ``later`` finish is still there (:func:`ledgerw.not_failed`). Nor is a
    row whose worker waits on the owner (``asking``, as
    :meth:`state.State.on_owner` reads it): its session is alive, so the
    launcher leaves it alone too. It is ``asking`` wherever that worker sits, in
    its slot on the park timer or parked, as on the web board; a parked row the
    owner has answered is at work again and belongs in ``busy``.
    """
    busy = busy or set()
    asking = asking or set()
    excluded = excluded or set()
    deferred = deferred or set()
    done = ledgerw.not_failed(done, deferred)
    satisfied = {p for p, s in done.items() if s in SATISFIED}

    buckets: dict[str, dict] = {}
    for phase, deps in graph.items():
        name = campaign_of(phase)
        b = buckets.setdefault(
            name,
            {"total": 0, "built": 0, "skipped": 0, "failed": 0, "running": [], "asking": [],
             "ready": [], "blocked": 0, "dated": 0, "excluded": 0, "held": 0},
        )
        b["total"] += 1
        status = done.get(phase)
        if phase in asking:
            b["asking"].append(phase)
        elif phase in busy:
            b["running"].append(phase)
        elif status in SATISFIED:
            b["built"] += 1  # a finished owner-run row too, as on the web board
            b["skipped"] += status == statuses.SKIP
            b["held"] += ticked is not None and phase not in ticked
        elif phase in excluded:
            b["excluded"] += 1
        elif status == "fail":
            b["failed"] += 1
        elif not deps <= satisfied:
            b["blocked"] += 1
        elif phase in deferred:
            b["dated"] += 1
        else:
            b["ready"].append(phase)

    out = [
        Campaign(
            name=name,
            total=b["total"],
            built=b["built"],
            skipped=b["skipped"],
            failed=b["failed"],
            running=sorted(b["running"]),
            asking=sorted(b["asking"]),
            ready=sorted(b["ready"]),
            blocked=b["blocked"],
            dated=b["dated"],
            excluded=b["excluded"],
            held=b["held"],
        )
        for name, b in buckets.items()
    ]
    # A finished campaign is never the headline while one with work left idles,
    # and one merely ready never while another has a worker on it: the headline
    # names what is being built, not the biggest campaign that could be.
    out.sort(key=lambda c: (not c.active, not c.manned, c.complete, -c.live_total, c.name))
    return out


#: The kinds of a dashboard's blockers (``data.Blocker.kind``) that are a worker
#: asking the owner: on its park timer, or parked with no answer yet.
ASKING_KINDS = frozenset({"waiting", "parked"})


def at_work(dash) -> set[str]:
    """The phases a worker is at work on: in a busy slot, or parked and at work on
    the owner's answer in a window of its own (``Dash.working_parked``)."""
    return ({s.phase for s in dash.snapshot.slots if s.busy and s.phase}
            | {w.phase for w in getattr(dash, "working_parked", None) or ()})


def asking(dash) -> set[str]:
    """The phases whose worker waits on the owner, as the dashboard's own list
    of what waits on the owner has them (``Snapshot.blockers``)."""
    return {b.phase for b in getattr(dash.snapshot, "blockers", None) or ()
            if b.kind in ASKING_KINDS and b.phase}


def standings(dash) -> list[Campaign]:
    """:func:`summarise` over what a dashboard holds. Every panel that counts
    the ledger calls this, so the headline and the status bar cannot differ."""
    return summarise(
        getattr(dash, "graph", None) or {},
        dict(dash.snapshot.landed),
        at_work(dash),
        set(getattr(getattr(dash, "cfg", None), "exclude", None) or []),
        getattr(dash, "ticked", None),
        getattr(dash, "deferred", None),
        asking(dash),
    )


def active(campaigns: list[Campaign]) -> Campaign | None:
    """The campaign to put in the headline, or None when nothing is in flight."""
    for c in campaigns:
        if c.active:
            return c
    return None


def overall(campaigns: list[Campaign]) -> Campaign:
    """Every campaign added up: the whole ledger, counted the same way."""
    return Campaign(
        name="all",
        total=sum(c.total for c in campaigns),
        built=sum(c.built for c in campaigns),
        skipped=sum(c.skipped for c in campaigns),
        failed=sum(c.failed for c in campaigns),
        running=sorted(p for c in campaigns for p in c.running),
        asking=sorted(p for c in campaigns for p in c.asking),
        ready=sorted(p for c in campaigns for p in c.ready),
        blocked=sum(c.blocked for c in campaigns),
        dated=sum(c.dated for c in campaigns),
        excluded=sum(c.excluded for c in campaigns),
        held=sum(c.held for c in campaigns),
    )


def counts(cfg, st) -> dict:
    """The whole ledger of one swarm, counted exactly as the dashboard counts it.

    :func:`summarise` over the launcher's done view, read from the project's
    ledger and the run's state, so `swarm status`, `swarm ls`, the TUI header
    and the web board can never disagree about what is done. ``done``,
    ``running``, ``asking``, ``ready``, ``blocked``, ``dated`` and ``failed``
    are disjoint and add up to ``total``.
    """
    path = cfg.project_dir / cfg.ledger
    graph = ledger_mod.load(path)
    ticked = ledger_mod.load_ticked(path)
    dated = ledgerw.dated(cfg)
    # At work: in a slot, or parked and working on the owner's answer.
    busy = {s.phase for s in st.busy_slots() if s.phase} | set(st.working_parked())
    # Its worker waits on the owner, in its slot or parked: the launcher will
    # not start it, so it is never ready, and it builds nothing until answered.
    asking = set(st.on_owner())
    landed = ledger_mod.with_ticked(st.done, ticked, busy | set(st.parked) | set(st.waiting))
    t = overall(summarise(graph, landed, busy, set(cfg.exclude or []), ticked, dated, asking))
    return {"done": t.built, "total": t.live_total, "held": t.held, "running": len(t.running),
            "asking": len(t.asking), "ready": len(t.ready), "blocked": t.blocked,
            "dated": t.dated, "failed": t.failed, "excluded": t.excluded,
            "dates": {p: d for p, d in sorted(dated.items(), key=lambda kv: kv[::-1])
                      if p in graph and p not in busy and p not in asking}}


def history_line(campaigns: list[Campaign]) -> str:
    """One line for everything that is not the active campaign."""
    rest = [c for c in campaigns if not c.active and c.built]
    if not rest:
        return ""
    built = sum(c.built for c in rest)
    return f"{built} phase(s) built earlier across {len(rest)} campaign(s)"
