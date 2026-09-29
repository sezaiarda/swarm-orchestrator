"""The one forecast every view reads, made off the render path.

The dashboard repaints every two seconds and the web board serves phones; a
simulation takes seconds. So the forecast is computed on a thread of its own
(:class:`Engine`) and every view reads the last one it made: the cheap
:func:`forecast.floor` first, the replays when they land.

It is recomputed at once when the plan really moved — a row landed or
launched, the ledger was edited — and when anything else it depends on moved
(a question, a merge, a hold, the config, a cap's reading) no sooner than
:data:`MIN_RECOMPUTE_S` after the last one (:class:`Gate`); plus every
:data:`RECOMPUTE_S` while rows run, since a running row's remaining time changes
as it ages, and every :data:`IDLE_RECOMPUTE_S` when none does. Keyed on every
state move, it re-simulated back to back through a merge and cost the dashboard
a third of a core. The result is kept in
``<state>/cache/eta.json`` under the key of what it was made from, so the TUI,
the web board and ``swarm status`` share one answer instead of each simulating
its own: whoever computes first writes it under a lock, and the others, waiting
on that lock, read it.

The model, and the upper band's calibration (:mod:`.calibrate`), are refitted
only when a phase finishes (:func:`fit` is keyed on the record of finishes),
never on a repaint.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import threading
import time
import weakref
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .. import ledger as ledger_mod
from .. import logutil
from .. import pace as pace_mod
from .. import usage as usage_mod
from ..tui import data as data_mod
from ..tui.campaign import campaign_of
from . import calibrate as calibrate_mod
from . import forecast as forecast_mod
from . import hazards as hazards_mod
from . import holds as holds_mod
from . import model as model_mod
from . import plan as plan_mod
from . import record as record_mod
from . import sim as sim_mod

#: Replays per forecast. P85 from 500 is good to about 1.6 percentile points.
RUNS = 500
#: Replays per forecast the dashboard makes (P85 good to about 3 points): it
#: remakes one every few minutes all day, and ``swarm status`` reads it from the
#: cache rather than simulating its own.
LIVE_RUNS = 150
#: Anything but a landed or launched row, or a ledger edit, recomputes no
#: sooner than this after the last forecast.
MIN_RECOMPUTE_S = 120.0
#: A cap's reading moves the forecast in steps this coarse (percentage points).
USAGE_STEP = 5.0
#: How much the forecast thread yields to the dashboard (``nice``, Linux only).
NICE = 10
#: A forecast is remade at least this often while it has rows running…
RECOMPUTE_S = 300.0
#: …and this often when none is: its clock still moves.
IDLE_RECOMPUTE_S = 1800.0
#: The usage burn per busy worker-hour is measured over this long a stretch.
BURN_WINDOW_S = 7 * 86400.0
CACHE_NAME = "eta.json"
#: Bumped when a forecast's meaning changes, so an older cache is not read.
VERSION = 2


@dataclass(frozen=True)
class Inputs:
    """Everything a forecast is made from, as whoever asks already holds it."""

    now: float
    #: The ledger's text, and :func:`ledger.parse` of it.
    text: str
    graph: dict[str, set[str]]
    #: The launcher's done view (:func:`ledger.with_ticked`).
    landed: dict[str, str]
    #: The swarm's state (:class:`state.State`).
    state: object
    #: phase -> when its current worker started, from the supervisor log.
    started: dict[str, float]
    #: :func:`tui.data.build_history` of the log, newest first, and its events.
    history: list = field(default_factory=list)
    events: list = field(default_factory=list)
    ledger_history: pace_mod.History = field(default_factory=pace_mod.History)
    #: ``limits.jsonl`` as :class:`usage.Sample` rows.
    usage: list = field(default_factory=list)
    exclude: frozenset[str] = frozenset()
    workers: int = 1
    build_slots: int = 0
    park_after: float = 900.0
    #: The ``[usage].rules`` in force (none when caps are switched off).
    rules: tuple = ()
    #: This machine's logged forecasts (:func:`calibrate.load`), oldest first.
    forecasts: tuple = ()


def gather(cfg, state, *, events, history, ledger_history, usage, text: str | None = None,
           now: float | None = None) -> Inputs:
    """:class:`Inputs` from a config, the state and what the caller has read."""
    now = time.time() if now is None else now
    if text is None:
        try:
            text = (Path(cfg.project_dir) / cfg.ledger).read_text(encoding="utf-8")
        except OSError:
            text = ""
    graph = ledger_mod.parse(text)
    flying = ({s.phase for s in state.busy_slots() if s.phase} | set(state.parked)
              | set(state.waiting))
    started: dict[str, float] = {}
    for ev in events:
        if ev.kind in ("launch", "claim") and ev.phase and ev.ts is not None:
            started[ev.phase] = ev.ts
    return Inputs(
        now=now, text=text, graph=graph,
        landed=ledger_mod.with_ticked(state.done, ledger_mod.ticked(text), flying),
        state=state, started=started, history=list(history or ()), events=list(events or ()),
        ledger_history=ledger_history, usage=list(usage or ()),
        exclude=frozenset(getattr(cfg, "exclude", None) or ()),
        workers=int(getattr(cfg, "max_workers", 1) or 1),
        build_slots=int(getattr(cfg, "build_max_concurrent", 0) or 0),
        park_after=float(getattr(cfg, "park_after", 900) or 0),
        rules=tuple(getattr(cfg, "usage_rules", ()) or ()) if getattr(cfg, "usage_enabled",
                                                                      False) else (),
        forecasts=tuple(calibrate_mod.load(calibrate_mod.log_path(cfg))),
    )


def from_files(cfg, st, now: float | None = None) -> Inputs:
    """:class:`Inputs` read from disk, for a caller with no dashboard: the log,
    the done sentinels, the ledger's git history and the usage readings."""
    events = data_mod.parse_events(logutil.read_all(cfg.supervisor_log))
    history = data_mod.build_history(
        events, data_mod.load_sentinels(cfg.done_dir), state=asdict(st),
        ticked=data_mod.load_ticked(cfg))
    usage = usage_mod.load_samples(Path(cfg.state_dir) / usage_mod.METERS_DIR
                                   / usage_mod.LIMITS_LOG)
    return gather(cfg, st, events=events, history=history,
                  ledger_history=pace_mod.load(cfg), usage=usage, now=now)


# -- what a forecast is made from ---------------------------------------------------
def plan_of(inputs: Inputs) -> plan_mod.Plan:
    st = inputs.state
    busy = {s.phase: inputs.started.get(s.phase, inputs.now) for s in st.busy_slots()
            if s.phase}
    return plan_mod.build(
        inputs.graph, inputs.text, inputs.landed, now=inputs.now, busy=busy,
        asking=set(st.parked) | set(st.waiting), merging=st.integrating(),
        excluded=set(inputs.exclude), workers=inputs.workers,
        build_slots=inputs.build_slots, park_after=inputs.park_after)


@dataclass(frozen=True)
class Fitted:
    """The model as of the last finish."""

    durations: model_mod.Durations
    hazards: hazards_mod.Hazards
    availability: holds_mod.Availability
    #: window -> percentage points per busy worker-hour.
    burn: dict[str, float]
    #: How far the upper band is stretched (:mod:`.calibrate`).
    calibration: calibrate_mod.Calibration = calibrate_mod.Calibration()


def fit_key(inputs: Inputs) -> str:
    """What the fit depends on: the attempts that ended, and the ledger's ticks.

    Not the calibration's log: a forecast it has just logged has no outcome yet,
    and its outcomes arrive as ticks, which refit it here."""
    ended = [a.end for a in record_mod.attempts(inputs.history, inputs.events)
             if a.end is not None]
    ticks = inputs.ledger_history.ticks
    last = max((t for t, _ in ticks.values()), default=0.0)
    return f"{len(ended)}:{max(ended, default=0.0)}:{len(ticks)}:{last}"


def fit(inputs: Inputs) -> Fitted:
    """Refit everything the forecast learns from history."""
    dirs = ledger_mod.dirs(inputs.text)
    rec = record_mod.from_sources(inputs.history, inputs.events, inputs.ledger_history)
    durations = model_mod.fit(record_mod.samples(rec, inputs.now),
                              lambda p: plan_mod.meta_for(p, dirs))
    ticks = [t for t, n in rec.ticks.values() if n <= pace_mod.BULK]
    workers = list(rec.workers) or [(0.0, inputs.workers)]
    return Fitted(
        durations=durations,
        hazards=hazards_mod.fit(rec, inputs.now, campaign_of),
        availability=holds_mod.availability(ticks, workers, inputs.now, durations.mean_s),
        burn=burn(inputs),
        calibration=calibrate_mod.fit(
            calibrate_mod.pairs_from_log(list(inputs.forecasts), inputs.ledger_history.ticks,
                                         inputs.now),
            default=calibrate_mod.DEFAULT_K),
    )


def burn(inputs: Inputs) -> dict[str, float]:
    """Each usage window's percentage points per busy worker-hour, lately."""
    return burn_of(inputs.events, inputs.workers, inputs.usage, inputs.now)


def burn_of(events, workers: int, usage, now: float) -> dict[str, float]:
    """:func:`burn` from its parts: the log's events, the worker count and the
    ``limits.jsonl`` samples. The dashboard's usage box projects the caps with it,
    so it and the forecast burn at the same rate.

    Measured from the last account switch (:func:`usage.switched_at`) when
    there was one: another account's plan burns at its own rate."""
    start = max(now - BURN_WINDOW_S, usage_mod.switched_at(list(usage or ())) or 0.0)
    # The whole log, so a worker launched before the stretch counts inside it.
    events = [e for e in events if e.ts is not None and e.ts <= now]
    busy = data_mod.occupancy_series(events, workers).points
    seat_h = sum(v * max(0.0, min(b, now) - max(a, start))
                 for (a, v), (b, _) in zip(busy, busy[1:] + [(now, 0.0)])) / 3600.0
    out = {}
    for window, pace in (("week", usage_mod.week_pace(usage, start, now)),
                         ("five_hour", usage_mod.five_pace(usage, start, now))):
        if pace.per_h is not None and seat_h >= 1.0:
            out[window] = pace.used / seat_h
    return out


def holds_of(inputs: Inputs, fitted: Fitted) -> holds_mod.Holds:
    return holds_mod.from_state(inputs.state, list(inputs.rules), inputs.usage, fitted.burn,
                                inputs.now, fitted.availability)


def key(inputs: Inputs, fit_id: str) -> str:
    """What a forecast depends on, bar the clock: the ledger, the state, the
    config, the caps and the fit."""
    st = inputs.state
    parts = {
        "v": VERSION,
        "ledger": hashlib.sha1(inputs.text.encode("utf-8")).hexdigest(),
        "landed": sorted(inputs.landed.items()),
        "busy": sorted((s.phase, round(inputs.started.get(s.phase, 0.0)))
                       for s in st.busy_slots() if s.phase),
        "asking": sorted(set(st.parked) | set(st.waiting)),
        "merging": sorted(st.integrating()),
        "holds": [st.paused, bool(st.drain), st.pause_at, sorted(st.usage_hold),
                  sorted(st.usage_override)],
        "config": [sorted(inputs.exclude), inputs.workers, inputs.build_slots,
                   inputs.park_after, json.dumps(inputs.rules, sort_keys=True)],
        "usage": [usage_key(usage_mod.latest(inputs.usage, w, inputs.now))
                  for w in ("week", "five")],
        "fit": fit_id,
    }
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def usage_key(got: tuple[float, float | None] | None) -> tuple | None:
    """A cap's reading as the forecast needs it: :data:`USAGE_STEP` steps, and
    the reset to the hour. Every one-point tick used to make a new forecast."""
    if got is None:
        return None
    pct, resets = got
    return (math.floor(pct / USAGE_STEP), None if resets is None else round(resets / 3600.0))


def compute(inputs: Inputs, fitted: Fitted | None = None, runs: int = RUNS,
            log: Path | None = None) -> forecast_mod.Forecast:
    """The simulated forecast, its upper band calibrated. With ``log``, a running
    swarm's forecast is also added to the calibration's record there."""
    fitted = fitted or fit(inputs)
    plan = plan_of(inputs)
    holds = holds_of(inputs, fitted)
    opts = sim_mod.Options(runs=runs)
    futures = sim_mod.simulate(plan, fitted.durations, fitted.hazards, holds, opts)
    if log is not None and not holds.stopped:
        calibrate_mod.record(log, inputs.now, calibrate_mod.known(plan, futures))
    fc = forecast_mod.summarise(plan, holds, futures, inputs.now, opts)
    return calibrate_mod.apply(fc, fitted.calibration)


def floor(inputs: Inputs, fitted: Fitted | None = None) -> forecast_mod.Forecast:
    """The cheap bound shown until the first simulation lands."""
    fitted = fitted or fit(inputs)
    return forecast_mod.floor(plan_of(inputs), fitted.durations, holds_of(inputs, fitted),
                              inputs.now)


# -- the shared cache ----------------------------------------------------------------
def cache_path(cfg) -> Path:
    return Path(cfg.state_dir) / "cache" / CACHE_NAME


def read_cache(cfg, want: str, not_before: float) -> forecast_mod.Forecast | None:
    """The cached forecast for key ``want``, if it was made at ``not_before`` or later."""
    try:
        raw = json.loads(cache_path(cfg).read_text(encoding="utf-8"))
        if raw.get("key") != want or float(raw.get("made_at", 0.0)) < not_before:
            return None
        return forecast_mod.Forecast.from_json(raw["forecast"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_cache(cfg, want: str, fc: forecast_mod.Forecast) -> None:
    """tmp + ``os.replace``; a lost write only costs the next reader a simulation."""
    path = cache_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"key": want, "made_at": fc.made_at,
                                   "forecast": fc.to_json()}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def recompute_s(inputs: Inputs) -> float:
    """How long a forecast stands: :data:`RECOMPUTE_S` while rows run (they
    age), :data:`IDLE_RECOMPUTE_S` when none does."""
    running = any(s.phase for s in inputs.state.busy_slots())
    return RECOMPUTE_S if running else IDLE_RECOMPUTE_S


def shared(cfg, inputs: Inputs, fitted: Fitted, fit_id: str, runs: int,
           max_age: float | None = None) -> forecast_mod.Forecast:
    """The cached forecast for ``inputs`` if it still stands (made under
    ``max_age`` seconds ago, by default :func:`recompute_s`), else one simulated
    and cached now. The lock makes a second asker wait for the first one's
    answer instead of simulating the same thing beside it."""
    want = key(inputs, fit_id)
    not_before = inputs.now - (recompute_s(inputs) if max_age is None else max_age)
    path = cache_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = open(path.with_suffix(".lock"), "a")  # noqa: SIM115 - held to the end
    except OSError:
        lock = None
    try:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        got = read_cache(cfg, want, not_before)
        if got is None:
            got = compute(inputs, fitted, runs, calibrate_mod.log_path(cfg))
            write_cache(cfg, want, got)
        return got
    finally:
        if lock is not None:
            lock.close()


def current(cfg, inputs: Inputs, runs: int = RUNS) -> forecast_mod.Forecast:
    """The forecast now, from the cache when it still stands, else simulated here."""
    return shared(cfg, inputs, fit(inputs), fit_key(inputs), runs)


# -- when to remake it -----------------------------------------------------------------
class Gate:
    """When a forecast is due, from what the asker has seen.

    ``real`` is what moves the plan itself (the rows landed and running, the
    ledger's text): a change there is due at once. ``soft`` is the rest a
    forecast reads (questions, merges, holds, the config, a cap's reading in
    :data:`USAGE_STEP` steps): a change there waits until :data:`MIN_RECOMPUTE_S`
    have passed since the last forecast. With neither moved, one is due every
    ``every`` seconds, since the clock moves a forecast too.
    """

    def __init__(self, min_s: float = MIN_RECOMPUTE_S) -> None:
        self.min_s = min_s
        self.real: object = None
        self.soft: object = None
        self.at: float | None = None

    def due(self, real, soft, now: float, every: float) -> bool:
        """Whether to remake the forecast now; if so, it counts as made now."""
        if self.at is not None and real == self.real:
            since = now - self.at
            if since < (self.min_s if soft != self.soft else every):
                return False
        self.real, self.soft, self.at = real, soft, now
        return True


# -- the background worker -------------------------------------------------------------
_SHARED: dict[str, "Engine"] = {}
_SHARED_LOCK = threading.Lock()


def shared_engine(cfg, runs: int = LIVE_RUNS) -> "Engine":
    """The one :class:`Engine` of this process for ``cfg``'s state dir.

    The dashboard and the web board it serves each hold a :class:`~tui.dash.Dash`;
    with an engine each, both remade the same forecast, and while one had read a
    state the other had not yet, each found the other's cached forecast stale."""
    key = str(cfg.state_dir)
    with _SHARED_LOCK:
        eng = _SHARED.get(key)
        if eng is None:
            eng = _SHARED[key] = Engine(cfg, runs)
        return eng


class Engine:
    """Makes forecasts on a thread of its own; views read :attr:`result`.

    :meth:`request` is what a repaint may call: it compares a stamp the caller
    already has (what moved, and the :func:`recompute_s` bucket) and only when
    that changed hands the worker thread a way to read its
    :class:`Inputs`. Gathering them runs on that thread too. The newest request
    wins; one in flight is finished first.
    """

    def __init__(self, cfg, runs: int = RUNS) -> None:
        self.cfg = cfg
        self.runs = runs
        #: When the next forecast is due (:meth:`due`).
        self.gate = Gate()
        #: The latest forecast, or ``None`` before the first one.
        self.result: forecast_mod.Forecast | None = None
        #: Why the last attempt made none ("" when it did).
        self.error = ""
        #: Bumped each time :attr:`result` is replaced.
        self.version = 0
        self._lock = threading.Lock()
        self._make = None
        self._stamp = None
        self._fit: tuple[str, Fitted] | None = None
        self._thread: threading.Thread | None = None
        self._driver: weakref.ref | None = None

    def drives(self, asker) -> bool:
        """Whether ``asker`` is the one that asks for forecasts: the first to ask
        while it lives. The others read :attr:`result`; a second asker a poll
        behind the first would otherwise undo each of its requests."""
        with self._lock:
            got = self._driver() if self._driver is not None else None
            if got is None:
                self._driver = weakref.ref(asker)
                return True
            return got is asker

    def request(self, stamp, make) -> None:
        """Ask for a forecast of ``make()`` unless ``stamp`` is the last one asked."""
        if stamp == self._stamp:
            return
        self._stamp = stamp
        with self._lock:
            self._make = make
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name="eta", daemon=True)
                self._thread.start()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the forecast in flight has landed; ``False`` on a timeout."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _work(self) -> None:
        _yield_cpu()
        while True:
            with self._lock:
                make, self._make = self._make, None
            if make is None:
                return
            try:
                self._forecast(make())
                self.error = ""
            except Exception as exc:  # noqa: BLE001 - a bad forecast must never take a view down
                self.error = f"{type(exc).__name__}: {exc}"

    def _publish(self, fc: forecast_mod.Forecast) -> None:
        self.result = fc
        self.version += 1

    def _forecast(self, inputs: Inputs) -> None:
        fid = fit_key(inputs)
        if self._fit is None or self._fit[0] != fid:
            self._fit = (fid, fit(inputs))
        fitted = self._fit[1]
        if self.result is None:
            self._publish(floor(inputs, fitted))
        # Its gate already said a forecast is due, so the cache stands only when
        # another process made it just now, not when it is this one's last.
        self._publish(shared(self.cfg, inputs, fitted, fid, self.runs, MIN_RECOMPUTE_S))


def _yield_cpu() -> None:
    """Lower this thread's priority: on Linux ``nice`` is per thread, so the
    dashboard's own thread keeps its place while a forecast simulates."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), NICE)
    except (AttributeError, OSError):
        pass
