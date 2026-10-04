"""When the swarm may not launch: the calendar the simulator schedules against.

Four kinds of stop, all of which leave running workers to finish (that is what
``swarm pause`` and a usage hold do):

* **the owner's, known.** A paused or draining swarm launches nothing until the
  owner says so, and a scheduled pause (``swarm pause --at``) stops launches
  from its moment on. No clock can say when the owner resumes, so rows that
  would start after a scheduled pause have no finish time in that future, and a
  paused swarm is forecast as if resumed now;
* **the owner's, as usual.** A swarm is not up all the time: it
  sat paused, down or at one worker for days at a stretch, and a forecast that
  ignored that put four truths in five past its median. So each future works
  only some hours, as many as the swarm lately delivered of what it could have
  (:class:`Availability`);
* **a usage hold now.** Launches resume at the held window's reset, which the
  swarm already knows;
* **a usage cap the run will reach.** Each window's lowest ``pause`` rule of
  ``[usage].rules`` — or the account's own limit, 100%, which stops workers
  whatever the config says — is projected forward from the latest reading of
  the window at the run's own burn, measured in percentage points per busy
  worker-hour, so a forecast that crosses a cap waits for that window's reset
  like the swarm will. A reading does not go stale for this: within one window
  usage only rises, so an old one still says at least how far it has got.
  Reading, cap and reset are the account in use now (:func:`usage.active_account`):
  another account's window says nothing about when this one fills.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

from .. import caps as caps_mod
from .. import pace as pace_mod
from .. import usage as usage_mod

#: How long each usage window runs before it resets.
PERIOD_S = {"week": 7 * 86400.0, "five_hour": 5 * 3600.0}

#: The account's own limit: at 100% of a window no worker gets anything done.
LIMIT = 100.0

PAUSED = "paused"
DRAINING = "draining to a stop"

_STD = NormalDist()

#: Availability is what the swarm delivered over this recent a stretch…
AVAIL_WINDOW_S = 3 * 86400.0
#: …which has to cover at least this much of the ledger's history to say anything.
AVAIL_MIN_S = 86400.0
#: How widely one future's availability strays from the measured one: as much
#: as a Beta of this weight around it (the backtest's choice; lower is wider).
REGIME = 16.0


@dataclass(frozen=True)
class Cap:
    """One usage window the swarm pauses at."""

    window: str
    pct: float
    at: float
    resets_at: float
    #: Percentage points per busy worker-hour.
    burn: float
    #: Held right now: nothing launches before ``resets_at``.
    held: bool = False

    @property
    def period_s(self) -> float:
        return PERIOD_S.get(self.window, 7 * 86400.0)


@dataclass(frozen=True)
class Availability:
    """How much of its capacity the swarm has lately delivered.

    Measured, not guessed: the rows the ledger's git history says were ticked
    over the last :data:`AVAIL_WINDOW_S` (on any machine, bookkeeping bulk ticks
    left out), against the rows the model says the workers of the time could
    have finished working flat out. That one number carries every reason a
    swarm is idle — paused, down, held by a cap, turned down to one worker,
    waiting between phases — without having to know which. Each simulated
    future draws its own share around it (logit-normal, as wide as a Beta of
    weight :data:`REGIME`) and then works each hour with that probability.
    """

    share: float = 1.0
    regime: float = REGIME

    def draw(self, u: float) -> float:
        """One future's share, for quantile ``u``."""
        a = self.share
        if a >= 1.0:
            return 1.0
        spread = 1.0 / math.sqrt(a * (1.0 - a) * (self.regime + 1.0))
        z = math.log(a / (1.0 - a)) + spread * _STD.inv_cdf(min(max(u, 1e-9), 1 - 1e-9))
        return 1.0 / (1.0 + math.exp(-z))


def availability(ticks, workers, now: float, mean_work_s: float) -> Availability:
    """:class:`Availability` from the tick times (non-bulk, every machine), the
    ``(time, max_workers)`` history and the model's mean work time."""
    times = [t for t in ticks if t <= now]
    if not times or now - min(times) < AVAIL_MIN_S or mean_work_s <= 0:
        return Availability()
    start = max(now - AVAIL_WINDOW_S, min(times))
    hours = int((now - start) // 3600)
    if hours <= 0:
        return Availability()
    seats = sum(pace_mod.workers_at(list(workers), start + (i + 0.5) * 3600) or 1
                for i in range(hours))
    capacity = seats * 3600.0 / mean_work_s
    delivered = sum(1 for t in times if start <= t)
    return Availability(share=max(0.05, min(1.0, delivered / capacity)) if capacity else 1.0)


@dataclass(frozen=True)
class Holds:
    """The launch calendar."""

    #: Launching stopped until the owner acts (:data:`PAUSED`, :data:`DRAINING`).
    stopped: str = ""
    #: A scheduled pause: no launch from this moment on (0 = none).
    pause_at: float = 0.0
    caps: tuple[Cap, ...] = ()
    availability: Availability = Availability()

    @property
    def held_until(self) -> float:
        """When the usage hold in force now lifts (0 = none)."""
        return max((c.resets_at for c in self.caps if c.held), default=0.0)


def from_state(st, rules: list[dict], samples: list, burn: dict[str, float],
               now: float, avail: Availability = Availability()) -> Holds:
    """The calendar from the state (``paused``, ``drain``, ``pause_at``,
    ``usage_hold``), the ``pause`` rules, the usage samples (the newest still
    in its window counts, :func:`usage.latest`), each window's burn per busy
    worker-hour and the availability. A paused swarm is forecast as if resumed
    now. A hold measured on another account than the one in use is about to
    lift (:func:`caps.evaluate`), so the reading in use stands in for it."""
    stopped = PAUSED if st.paused else DRAINING if st.drain else ""
    account = usage_mod.active_account(list(samples))
    samples = usage_mod.current(list(samples))
    out = []
    for window in caps_mod.WINDOWS:
        limits = [r["at"] for r in rules if r["window"] == window and r["action"] == "pause"]
        limits.append(LIMIT)
        hold = (st.usage_hold or {}).get(window)
        if hold is not None and None not in (account, hold.get("account")) \
                and hold["account"] != account:
            hold = None
        if hold is not None:
            resets = hold.get("resets_at") or now + PERIOD_S.get(window, 0.0)
            out.append(Cap(window, float(hold.get("pct", 100.0)), float(hold.get("at", 0.0)),
                           float(resets), burn.get(window, 0.0), held=True))
            continue
        read = usage_mod.latest(samples, caps_mod.WINDOWS[window][0], now)
        if read is None or read[1] is None:
            continue
        if caps_mod.overridden(st.usage_override or {}, window, account, read[1]):
            continue  # the owner chose to run through this window
        out.append(Cap(window, read[0], min(limits), read[1], burn.get(window, 0.0)))
    return Holds(stopped=stopped, pause_at=st.pause_at if st.pause_at > now else
                 (now if st.pause_at else 0.0), caps=tuple(out), availability=avail)
