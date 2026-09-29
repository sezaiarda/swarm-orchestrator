"""How much wider the upper band has to be than the replays make it.

The replays' median is right about half the time, but their P85 holds fewer
campaigns than it should.
What they miss is the swarm stopping for days: the replays spread idle time
evenly, so a long stop is rarer in them than in a live run. The median is left
alone; the upper tail is stretched.

**The stretch.** In log time, measured from the median: a replay that finishes
``x`` hours out, beyond the median's ``m``, is moved to ``m·(x/m)^k``. One
number, ``k`` (1 = as simulated), moves P85 and P95 together, never touches
P50, and widens a long forecast by more hours than a short one: a campaign
whose median is 5 h and one whose median is 50 h are stretched by the same
factor in log time, not by the same hours. Why this form over the simpler
ones: a ratio to the median alone ignores that the replays already spread some
campaigns more than others, and a fixed number of hours added to P85 widens a
two-hour campaign as much as a four-day one.

What it costs: P85 holds more campaigns with the median unchanged, but the
band is wider, so the scores that reward a sharp band (pinball loss, CRPS) get
worse. The misses are two-humped (a campaign met a multi-day stop or it did
not), so once the fit has seen a stop every calm campaign pays for the wider
band; and the stretch is largest where the replays' own spread is already wide.

**Fitting k** is one-sided split-conformal calibration (Romano, Patterson &
Candès, "Conformalized quantile regression", 2019) on a normalised score:
each past forecast whose truth is known scores ``s = ln(y/m) / ln(p85/m)``, the
truth's distance above the median in units of the forecast's own P85 distance
(``s ≤ 1`` means P85 held it). ``k`` is the 85% quantile of ``s`` at the
finite-sample level ``⌈(n+1)·0.85⌉/n``. A forecast whose campaign has not
finished yet is right-censored (its truth is at least what has passed), so the
quantile is read off a Kaplan-Meier estimate rather than the plain sort; that
keeps the campaigns that ran longest, the ones that matter here, in the fit.
``k`` is never below 1: the calibration widens, it never narrows the replays.

**Too little history** (fewer than :data:`MIN_EVENTS` finished campaigns, or so
many still open that the estimate never reaches 85%): the caller's default. The
backtest passes 1, no calibration, so it never learns from what it scores; at
runtime it is :data:`DEFAULT_K`.

**At runtime** the history is this machine's own forecasts: every
:data:`LOG_EVERY_S` a running swarm's forecast appends its per-campaign P50,
P85 and open rows to :data:`LOG_NAME`, and the ledger's ticks say when each
campaign really finished. A paused swarm's forecast is not logged: it is time
once resumed, and the log does not know when that will be.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path

from .forecast import Book, Forecast, Range, RowView
from .plan import Plan
from .sim import HORIZON_S, Future

#: The quantile the calibration holds the truth under.
LEVEL = 0.85
#: Finished campaigns needed before history replaces the default.
MIN_EVENTS = 5
#: A forecast whose P85 sits this close to its median has no spread to scale;
#: its score is measured against this much instead (P85 = 1.2 × the median).
SPREAD_FLOOR = math.log(1.2)
#: The runtime's default until its own log has :data:`MIN_EVENTS` finished: no
#: stretch. A stretch fitted offline is dominated by a few multi-day stops, and
#: applied live it pushes P85s days past their medians; the live log's own fit
#: replaces this once enough campaigns have finished.
DEFAULT_K = 1.0
#: The runtime's record of its own forecasts, in the state dir.
LOG_NAME = "eta-forecasts.jsonl"
#: One logged forecast per this long: four a day, so a week gives a fit.
LOG_EVERY_S = 6 * 3600.0
#: Logged forecasts older than a replay's horizon are dropped.
KEEP_S = HORIZON_S


@dataclass(frozen=True)
class Calibration:
    """The upper band's stretch and what it rests on."""

    k: float = 1.0
    #: Finished campaigns behind it (0 = the default).
    n: int = 0


# -- the score and the stretch ---------------------------------------------------------
def score(p50: float, p85: float, truth: float) -> float:
    """How far above the median the truth fell, in units of P85's distance.
    All three are time remaining from the forecast, in any one unit."""
    spread = max(SPREAD_FLOOR, math.log(p85 / p50)) if p85 > p50 else SPREAD_FLOOR
    return math.log(max(truth, p50 * 1e-3) / p50) / spread


def stretch_one(x: float, m: float, k: float) -> float:
    """Remaining time ``x`` with the upper tail above the median ``m`` stretched."""
    if k == 1.0 or not (m > 0 and x > m and math.isfinite(x)):
        return x
    return m * (x / m) ** k


def stretch(r: Range | None, now: float, k: float) -> Range | None:
    """A finish range (epoch seconds) with its P85 and P95 stretched."""
    if r is None or k == 1.0 or not math.isfinite(r.p50):
        return r
    m = r.p50 - now
    return Range(r.p50, now + stretch_one(r.p85 - now, m, k),
                 now + stretch_one(r.p95 - now, m, k))


def apply(fc: Forecast, cal: Calibration) -> Forecast:
    """Every range the forecast shows, stretched; a floor is left as it is."""
    if fc.floor or cal.k == 1.0:
        return replace(fc, stretch=cal.k, calibrated_on=cal.n)
    now = fc.made_at

    def row(v: RowView) -> RowView:
        return replace(v, finish=stretch(v.finish, now, cal.k))

    def book(b: Book) -> Book:
        return replace(b, finish=stretch(b.finish, now, cal.k),
                       rows=tuple(row(v) for v in b.rows))

    return replace(fc, overall=stretch(fc.overall, now, cal.k),
                   books=tuple(book(b) for b in fc.books), stretch=cal.k,
                   calibrated_on=cal.n)


# -- fitting ------------------------------------------------------------------------------
def fit(pairs: list[tuple[float, bool]], default: float = 1.0) -> Calibration:
    """``k`` from ``(score, finished)`` pairs; an unfinished one's score is a
    lower bound. ``default`` when there is too little to say."""
    events = sum(1 for _, done in pairs if done)
    if events < MIN_EVENTS:
        return Calibration(default, 0)
    n = len(pairs)
    got = km_quantile(pairs, min(1.0, math.ceil((n + 1) * LEVEL) / n))
    if got is None:
        return Calibration(default, 0)
    return Calibration(max(1.0, got), events)


def km_quantile(pairs: list[tuple[float, bool]], level: float) -> float | None:
    """The ``level`` quantile of right-censored values (Kaplan-Meier), or ``None``
    when the estimate never reaches it (too much is still open)."""
    ordered = sorted(pairs, key=lambda p: (p[0], not p[1]))  # an event before a tie's bound
    at_risk, surv = len(ordered), 1.0
    for value, done in ordered:
        if done:
            surv *= 1.0 - 1.0 / at_risk
            if 1.0 - surv >= level - 1e-12:
                return value
        at_risk -= 1
    return None


# -- the runtime's log -------------------------------------------------------------------
def log_path(cfg) -> Path:
    return Path(cfg.state_dir) / LOG_NAME


def load(path: Path) -> list[dict]:
    """The logged forecasts, oldest first; a torn or foreign line is skipped."""
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            got = json.loads(line)
            float(got["at"])
            if isinstance(got["books"], dict):
                out.append(got)
        except (ValueError, KeyError, TypeError):
            continue
    return out


def known(plan: Plan, futures: list[Future]) -> dict[str, tuple[Range, list[str]]]:
    """campaign -> the finish range of the rows open now (no follow-ups: the
    target the calibration scores) and those rows."""
    by_camp: dict[str, list[str]] = {}
    for row in plan.rows.values():
        by_camp.setdefault(row.meta.campaign, []).append(row.id)
    return {c: (Range.of([max(f.finish[r] for r in rows) for f in futures]), rows)
            for c, rows in by_camp.items() if futures}


def record(path: Path, now: float, ranges: dict[str, tuple[Range, list[str]]]) -> bool:
    """Append this forecast unless one was logged in the last :data:`LOG_EVERY_S`."""
    entries = load(path)
    if entries and now - float(entries[-1]["at"]) < LOG_EVERY_S:
        return False
    books = {c: [r.p50, r.p85, rows] for c, (r, rows) in ranges.items()
             if math.isfinite(r.p85) and r.p50 > now}
    if not books:
        return False
    keep = [e for e in entries if now - float(e["at"]) <= KEEP_S]
    keep.append({"at": now, "books": books})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text("".join(json.dumps(e) + "\n" for e in keep), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def pairs_from_log(entries: list[dict], ticks: dict[str, tuple[float, int]], now: float
                   ) -> list[tuple[float, bool]]:
    """Each logged campaign forecast against when its rows were all ticked; one
    still open is censored at now."""
    out = []
    for e in entries:
        at = float(e["at"])
        for p50, p85, rows in e["books"].values():
            m, hi = float(p50) - at, float(p85) - at
            if m <= 0 or not rows:
                continue
            done = [ticks[r][0] for r in rows if r in ticks]
            if len(done) == len(rows):
                out.append((score(m, hi, max(done) - at), True))
            elif now > at:
                out.append((score(m, hi, now - at), False))
    return out
