"""Rows that run on a model of their own (:mod:`..models`), as the forecast sees them.

Such a row takes another time than one on the swarm's own model, burns the
usage windows at another rate, and may hand itself back (``swarm escalate``):
that costs the time it had worked, then a whole run on the swarm's model. Each
of the three starts from a prior and is replaced by what this machine measures.

The priors, for a cheaper and faster model than the swarm's own:

* time: about half of a worker's hours are the model writing and half are
  builds and the wait for a build slot, which no model shortens. A model that
  writes 1.5 times as fast shortens the first half only: 0.45 / 1.5 + 0.55 = 0.85.
* usage: the same tokens at half the price, spent in 0.85 of the time, burn a
  window at 0.5 / 0.85 = 0.6 of the rate per worker-hour.
* hand-backs: one row in ten, after half of its own run.
"""

from __future__ import annotations

import math
from statistics import median

#: ``ln`` of the work-time factor of a row on a model of its own…
TIME_PRIOR = math.log(0.85)
#: …worth this many finished rows against what is measured.
TIME_PRIOR_N = 8.0
#: Usage burned per worker-hour, as a share of a worker on the swarm's model.
BURN_PRIOR = 0.6
#: Share of such rows that hand themselves back…
HANDUP_P = 0.1
#: …after this share of the time their own run would have taken.
HANDUP_SHARE = 0.5
#: Cost per hour is measured once each side has this many metered rows.
MIN_ROWS = 8
#: A measured weight outside this range is a metering fault, not a model.
BURN_RANGE = (0.1, 3.0)


def burn_weights(meters: dict[str, tuple[float, float]], models: dict[str, str]
                 ) -> dict[str, float]:
    """``{model: usage weight}`` for every model in ``models`` (``row -> model``).

    ``meters`` is ``row -> (cost, seconds)`` as each session's meter last read.
    A model's weight is its rows' median cost per hour over that of the rows on
    the swarm's own model, once both have :data:`MIN_ROWS`; :data:`BURN_PRIOR`
    until then.
    """
    rates: dict[str, list[float]] = {}
    for row, (cost, seconds) in meters.items():
        if cost > 0 and seconds >= 60.0:
            rates.setdefault(models.get(row, ""), []).append(cost / seconds)
    own = rates.get("", [])
    out = {}
    for name in set(models.values()):
        got = rates.get(name, [])
        if len(own) >= MIN_ROWS and len(got) >= MIN_ROWS and median(own) > 0:
            out[name] = min(BURN_RANGE[1], max(BURN_RANGE[0], median(got) / median(own)))
        else:
            out[name] = BURN_PRIOR
    return out
