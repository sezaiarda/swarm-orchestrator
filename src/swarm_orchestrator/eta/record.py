"""What past phases took: the measurements every forecast is fitted on.

Two sources, each read by a parser that already exists:

* **this machine's supervisor log**, as :func:`tui.data.build_history` pairs it
  into attempts (a launch to its ``done``, or to the line that lost it), plus
  the ``waiting``/``resumed`` lines that say how long each one waited on the
  owner. It knows when a phase started, so it is the one source of true work
  times;
* **the ledger's git history** (:mod:`pace`), which knows every row wherever it
  was built but only when it was ticked and filed. That says how fast the swarm
  delivered and how campaigns grew; it is *not* a work time. A start
  reconstructed from the ticks around a row (the offline backtest
  calibrated one) runs long whenever the swarm sat idle, and the backtest scored
  the fit worse with those samples than without them.

A pause does not stop a running worker (``swarm pause`` only stops launches),
so it is not taken out of a phase's work time; the time a worker sat waiting
on the owner is, because that is a separate, rarer delay (:mod:`.hazards`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import pace as pace_mod
from .. import statuses

#: A work time shorter than this is a bookkeeping finish, not a build.
MIN_WORK_S = 60.0


@dataclass(frozen=True)
class Attempt:
    """One launch of one phase, as the supervisor log recorded it."""

    phase: str
    start: float
    #: ``None`` while it is still running.
    end: float | None
    #: Finished with a report. ``False`` is a lost attempt: the worker died, the
    #: swarm restarted under it, or it was relaunched before it reported.
    done: bool
    #: Seconds inside it spent waiting on the owner.
    waited_s: float = 0.0


@dataclass(frozen=True)
class Record:
    """Everything measured, in one value the model can be fitted on."""

    attempts: tuple[Attempt, ...] = ()
    #: phase -> ``(tick time, rows that commit ticked)``, from :mod:`pace`.
    ticks: dict[str, tuple[float, int]] = field(default_factory=dict)
    #: phase -> ``(time it was filed, rows that commit filed)``.
    added: dict[str, tuple[float, int]] = field(default_factory=dict)
    #: ``(time, max_workers)`` as ``.swarm.toml`` set it, oldest first.
    workers: tuple[tuple[float, int], ...] = ()


@dataclass(frozen=True)
class Sample:
    """One phase's work time."""

    phase: str
    seconds: float
    end: float
    #: Still running: the work time is *at least* ``seconds``.
    censored: bool = False


# -- reading the sources ------------------------------------------------------
def owner_waits(events) -> dict[str, list[tuple[float, float]]]:
    """phase -> the spans its worker waited on the owner (``waiting`` to
    ``resumed``, or to its ``done`` when it never said ``resumed``)."""
    spans: dict[str, list[tuple[float, float]]] = {}
    since: dict[str, float] = {}
    for ev in sorted((e for e in events if e.ts is not None and e.phase), key=lambda e: e.ts):
        if ev.kind == "waiting":
            since.setdefault(ev.phase, ev.ts)
        elif ev.kind in ("resumed", "done") and ev.phase in since:
            spans.setdefault(ev.phase, []).append((since.pop(ev.phase), ev.ts))
    return spans


def attempts(history, events) -> tuple[Attempt, ...]:
    """Every attempt in ``history`` (:class:`tui.data.PhaseRun`, newest first)
    that has a launch time, oldest first, with its owner waits."""
    waits = owner_waits(events)
    out = []
    for run in history or ():
        if run.started_at is None:
            continue
        end = run.ended_at
        done = run.status in statuses.ALL
        if end is None and not run.running:
            continue  # over, and when is not known: nothing to measure
        inside = sum(max(0.0, min(b, end if end is not None else b) - max(a, run.started_at))
                     for a, b in waits.get(run.phase, ()))
        out.append(Attempt(run.phase, run.started_at, end, done and end is not None, inside))
    out.sort(key=lambda a: a.start)
    return tuple(out)


def from_sources(history, events, ledger_history: pace_mod.History) -> Record:
    """The record from what the dashboard already holds."""
    return Record(
        attempts=attempts(history, events),
        ticks=dict(ledger_history.ticks),
        added=dict(ledger_history.added),
        workers=tuple(ledger_history.workers),
    )


# -- work times ----------------------------------------------------------------
def samples(record: Record, now: float) -> list[Sample]:
    """Work times known at ``now``: a finished attempt's launch to done, less
    its owner waits; a running one as at least its age so far."""
    out = []
    for a in record.attempts:
        if a.start > now:
            continue
        if a.end is None or a.end > now:
            age = now - a.start - a.waited_s
            if age >= MIN_WORK_S:
                out.append(Sample(a.phase, age, now, censored=True))
        elif a.done:
            work = a.end - a.start - a.waited_s
            if work >= MIN_WORK_S:
                out.append(Sample(a.phase, work, a.end))
    return out
