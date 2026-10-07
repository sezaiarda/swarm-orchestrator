"""``swarm report`` — what the workers actually did.

Every finishing worker writes a one-line recap into its ``done/<phase>.<status>``
sentinel, and :func:`launch.done` telegrams it for ``needs-owner`` / ``fail``.
Nothing in this codebase has ever read one back: :func:`gitq.sentinel_done`
parses the *filename* and never opens the file. So every recap the swarm has ever
produced was either glanced at once as a phone notification or lost outright —
the swarm accumulated a durable, structured record of its own reasoning and then
never looked at it.

This module is the reader. It joins the five places a phase leaves a trace:

* ``done/<phase>.<status>`` — the sentinel: mtime (when the worker finished) and
  **body** (the recap it wrote);
* ``done/<phase>.jsonl`` — :func:`launch._append_history`'s record of every
  ``swarm done`` call and its verdict, which survives the sentinel being
  overwritten (or a later call being *refused*) by a subsequent one;
* ``recaps/<phase>.json`` — :mod:`recap`'s generated one-glance summary;
* ``notes/<phase>.jsonl`` — :mod:`notes`, the decisions a worker recorded without
  a ping, and the owner's answers (``owner_decision``) relayed by ``swarm
  resumed``. Its own docstring names ``swarm report --decisions`` as where they
  surface, so this is that;
* ``operator/<job>.json`` — :mod:`opqueue`, the follow-up jobs a phase handed
  on and what became of them;
* ``logs/supervisor.log`` — ``CLAIM`` / ``LAUNCH`` / ``EVENT done`` triples,
  timestamped via :func:`logutil.parse_ts` (which reads both the current
  ``<iso> <mono> <msg>`` format and legacy monotonic-only lines);
* ``notifications.jsonl`` — :mod:`telegram`'s send ledger, which is the only
  place that records whether the owner ping a ``needs-owner`` recap triggered
  actually *landed*;
* ``state.json`` — the authoritative ``done`` map and what is in flight now.

The two clocks matter. A sentinel's mtime is when the *worker* finished; the
``EVENT done`` line is logged only after the integrator has merged and pushed. So
``sentinel → EVENT done`` is the **integration** cost and ``CLAIM → sentinel`` is
the **in-slot** cost, and separating them is the difference between "the workers
are slow" and "the merge queue is the bottleneck".

Joining also makes two silent corruptions visible, which is why
:attr:`Report.warnings` exists at all:

* a sentinel whose status disagrees with the ``done`` map (e.g. several
  phases whose sentinels all say ``needs-owner`` while state says ``ok`` — several
  owner questions that were never asked);
* a recap overwritten by a later ``swarm done`` for the same phase, recoverable
  only from the jsonl history.
"""

from __future__ import annotations

import json
import re
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from . import ledger as ledger_mod
from . import notes as notes_mod
from . import opqueue, statuses
from . import recap as recap_mod
from . import state as state_mod
from . import telegram as telegram_mod
from .config import Config
from . import logutil
from .logutil import parse_ts
from .state import State

# The sentinel filename suffix must be one of these or the file is not a
# sentinel (`<phase>.jsonl` is skipped by this test).
STATUSES = statuses.ALL

# Recap text that carries no decision — a worker filling in the argument because
# it is required. `--decisions` filters these out.
_FILLER = {"", "-", "--", "n/a", "na", "none", "ok", "done", "finished", "success"}

_EVENT_DONE_RE = re.compile(r"^EVENT done (\S+) (\S+)")
_CLAIM_RE = re.compile(r"^CLAIM (\S+) slot=(\d+)")
_LAUNCH_RE = re.compile(r"^LAUNCH (\S+) slot=(\d+)")
_DENIED_RE = re.compile(r"^LAUNCH-DENIED (\S+) (.*)$")


@dataclass
class Sentinel:
    """One ``done/<phase>.<status>`` file: when it landed and what it said."""

    phase: str
    status: str
    mtime: float
    note: str


@dataclass
class Attempt:
    """One line of ``done/<phase>.jsonl`` — a single ``swarm done`` call.

    ``verdict`` is :func:`launch._write_sentinel`'s: ``written`` (it landed),
    ``forced`` (``--force`` overwrote an existing one) or ``refused`` (a duplicate
    that was NOT allowed to clobber the recap already on disk). A refused attempt
    is the one case where the note exists nowhere but here.
    """

    ts: float | None
    status: str
    note: str
    verdict: str = ""


@dataclass
class Run:
    """One trip through a slot, reconstructed from the supervisor log."""

    claimed: float | None = None
    launched: float | None = None
    done_at: float | None = None  # `EVENT done` — AFTER integration
    status: str | None = None


@dataclass
class PhaseReport:
    """Everything known about one phase, from every source."""

    phase: str
    status: str | None = None  # state.json `done` map (the authority)
    live: str | None = None  # busy / parked / waiting / integrating, if in flight
    sentinel_status: str | None = None
    note: str = ""  # the recap the worker wrote into the sentinel
    recap: str = ""  # recap.summary from recaps/<phase>.json
    recap_reason: str = ""  # why there is no recap, when there isn't one
    notes: list[dict] = field(default_factory=list)  # `swarm note` decisions
    #: The phase's operator jobs: ``{job, state, brief, outcome}``.
    operator_jobs: list[dict] = field(default_factory=list)
    pings: list[Ping] = field(default_factory=list)  # owner notifications sent
    started: float | None = None  # first CLAIM
    finished: float | None = None  # when the WORKER finished (sentinel / jsonl)
    integrated: float | None = None  # when `EVENT done` landed (post-merge)
    slot_s: float | None = None  # in-slot duration
    integ_s: float | None = None  # integration duration
    attempts: list[Attempt] = field(default_factory=list)
    runs: list[Run] = field(default_factory=list)
    denials: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def decision(self) -> str:
        """The best human-written summary for this phase (recap beats sentinel)."""
        return self.recap or self.note

    def substantive(self) -> bool:
        """Whether this phase recorded anything worth reading.

        An explicit ``swarm note`` always counts — it was recorded *because* the
        worker judged it needed review. Otherwise the summary has to be more than
        the filler a worker types because the argument is required.
        """
        if self.notes or self.operator_jobs:
            return True
        text = " ".join(self.decision.split())
        return bool(text) and text.lower() not in _FILLER and len(text) >= 4


@dataclass
class Ping:
    """One line of ``notifications.jsonl`` — an owner notification and its fate."""

    ts: float | None
    kind: str
    delivered: bool
    error: str = ""
    #: Why the swarm chose not to send it; "" = it was sent (or tried).
    suppressed: str = ""


@dataclass
class Discrepancy:
    """A disagreement between two records of the same phase."""

    phase: str
    kind: str
    detail: str


@dataclass
class Totals:
    phases: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    slot_hours: float = 0.0
    integ_hours: float = 0.0
    median_s: float | None = None
    mean_s: float | None = None


@dataclass
class Report:
    phases: list[PhaseReport] = field(default_factory=list)
    totals: Totals = field(default_factory=Totals)
    warnings: list[Discrepancy] = field(default_factory=list)
    since: float | None = None
    generated_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# -- building -------------------------------------------------------------
def build_report(
    cfg: Config,
    since: float | None = None,
    phase: str | None = None,
    st: State | None = None,
) -> Report:
    """Join every record of every phase into one :class:`Report`.

    ``since`` (epoch seconds, see :func:`parse_since`) keeps only phases with
    activity at or after that instant; a phase with no timestamps anywhere is kept
    only when there is no ``since`` filter, since "unknown when" cannot honestly be
    claimed to fall inside a window. ``phase`` restricts to a single phase.
    """
    if st is None:
        st = state_mod.read(cfg)
    sentinels = _read_sentinels(cfg)
    jsonl = _read_jsonl(cfg)
    recaps = _read_recaps(cfg)
    notes = notes_mod.load_all(cfg)
    jobs = _read_jobs(cfg)
    pings, ping_ledger = _read_pings(cfg)
    runs, denials = _read_log(cfg)
    live = _live_map(st)

    names = (
        set(st.done)
        | set(sentinels)
        | set(jsonl)
        | set(recaps)
        | set(notes)
        | set(jobs)
        | set(pings)
        | set(runs)
        | set(live)
        | set(ledger_mod.load(cfg.project_dir / cfg.ledger))
    )
    if phase is not None:
        names = {p for p in names if p == phase}

    reports = [
        _one(name, st, sentinels, jsonl, recaps, notes, pings, ping_ledger,
             runs, denials, live)
        for name in sorted(names)
    ]
    for r in reports:
        r.operator_jobs = jobs.get(r.phase, [])
    reports = [r for r in reports if _in_window(r, since)]
    reports.sort(key=lambda r: (r.finished or r.started or 0.0, r.phase))

    rep = Report(
        phases=reports,
        totals=_totals(reports),
        warnings=[_discrepancy(r.phase, w) for r in reports for w in r.warnings],
        since=since,
        generated_at=time.time(),
    )
    return rep


def _one(
    phase: str,
    st: State,
    sentinels: dict[str, list[Sentinel]],
    jsonl: dict[str, list[Attempt]],
    recaps: dict[str, recap_mod.Recap],
    notes: dict[str, list[notes_mod.Note]],
    pings: dict[str, list[Ping]],
    ping_ledger: bool,
    runs: dict[str, list[Run]],
    denials: dict[str, list[str]],
    live: dict[str, str],
) -> PhaseReport:
    """Fold one phase's records together, newest-wins, and flag the conflicts."""
    sents = sentinels.get(phase, [])
    latest = sents[-1] if sents else None
    attempts = jsonl.get(phase, [])
    my_runs = runs.get(phase, [])

    rec = recaps.get(phase)
    rep = PhaseReport(
        phase=phase,
        status=st.done.get(phase),
        live=live.get(phase),
        sentinel_status=latest.status if latest else None,
        note=" ".join((latest.note if latest else "").split()),
        recap=" ".join((rec.summary or "").split()) if rec else "",
        recap_reason=(rec.reason or "") if rec and not rec.summary else "",
        notes=[n.as_dict() for n in notes.get(phase, [])],
        pings=pings.get(phase, []),
        attempts=attempts,
        runs=my_runs,
        denials=denials.get(phase, []),
    )

    rep.started = next((r.claimed for r in my_runs if r.claimed is not None), None)
    # The worker's own finish: the jsonl entry is the precise stamp, the sentinel
    # mtime the durable fallback. `EVENT done` is NOT it — that fires after the
    # integrator has merged and pushed.
    rep.finished = next(
        (a.ts for a in reversed(attempts) if a.ts is not None),
        latest.mtime if latest else None,
    )
    rep.integrated = next(
        (r.done_at for r in reversed(my_runs) if r.done_at is not None), None
    )
    if rep.started is not None and rep.finished is not None:
        rep.slot_s = max(0.0, rep.finished - rep.started)
    if rep.finished is not None and rep.integrated is not None:
        # Includes [worker].done_grace_s, which delays the poke that starts it.
        rep.integ_s = max(0.0, rep.integrated - rep.finished)

    rep.warnings = _warnings(rep, sents, attempts, ping_ledger)
    return rep


def _warnings(
    rep: PhaseReport,
    sents: list[Sentinel],
    attempts: list[Attempt],
    ping_ledger: bool,
) -> list[str]:
    """The conflicts worth waking the owner for. Each is ``"<kind>: <detail>"``."""
    out: list[str] = []
    if rep.sentinel_status and rep.status and rep.sentinel_status != rep.status:
        out.append(
            f"sentinel-disagrees: the sentinel says `{rep.sentinel_status}` but"
            f" state.json recorded `{rep.status}`"
            + (
                " — the owner was never asked"
                if rep.sentinel_status == "needs-owner"
                else ""
            )
        )
    if rep.status and rep.status != "skip" and not sents:
        # `swarm skip` is exempt: cmd_skip marks the done map directly and never
        # writes a sentinel, because there was no worker and so no recap to lose.
        # Warning on every one of a run's skips buries the ones that matter.
        out.append(
            f"no-sentinel: recorded `{rep.status}` in state.json with no"
            " done/ sentinel — the durable record of this phase is gone"
        )
    if sents and not rep.status and not rep.live:
        out.append(
            f"orphan-sentinel: a `{rep.sentinel_status}` sentinel exists but the"
            " phase is not in the done map (the supervisor never saw the poke)"
        )
    if len(sents) > 1:
        others = ", ".join(f"`{s.status}`" for s in sents[:-1])
        out.append(
            f"multiple-sentinels: {len(sents)} sentinels ({others} then"
            f" `{sents[-1].status}`) — earlier recaps are only in the jsonl"
        )
    written = [" ".join(a.note.split()) for a in attempts if a.note.strip()]
    if len(set(written)) > 1:
        out.append(
            f"recap-overwritten: {len(written)} different recaps were written; only"
            " the last survives in the sentinel — the rest are in the jsonl"
        )
    refused = [a for a in attempts if a.verdict == "refused" and a.note.strip()]
    if refused:
        out.append(
            f"recap-refused: {len(refused)} `swarm done` call(s) were refused as"
            " duplicates, so their recap exists ONLY in the jsonl history"
        )
    out.extend(_ping_warnings(rep, ping_ledger))
    return out


def _ping_warnings(rep: PhaseReport, ping_ledger: bool) -> list[str]:
    """Whether the owner ping this phase's outcome owed actually reached them.

    A ``fail`` asks the owner or is held for the Overseer's summary, a
    ``needs-owner`` finish telegrammed them when it was live, and ``ok`` says
    nothing, so those two statuses owe a row in the log.
    ``notifications.jsonl`` is the only record of whether one landed — a recap
    that says "confirm before GA" and a send that failed look identical from
    every other file.

    Gated on the ledger existing at all: a run from before :mod:`telegram` kept
    one has no records for any phase, and reporting every single outcome as
    un-pinged would be noise indistinguishable from the real thing.
    """
    if not ping_ledger or rep.sentinel_status not in ("needs-owner", "fail"):
        return []
    sent = [p for p in rep.pings if not p.suppressed]
    if not sent and rep.pings:
        return []  # held back on purpose (the Overseer's to retry), and logged
    if not rep.pings:
        return [
            f"owner-never-pinged: the sentinel says `{rep.sentinel_status}`, which"
            " owes the owner a telegram, but nothing was ever sent for this phase"
        ]
    failed = [p for p in sent if not p.delivered]
    if len(failed) == len(sent):
        detail = failed[-1].error or "no reason recorded"
        return [
            f"ping-undelivered: every owner ping for this phase failed to send"
            f" ({detail}) — they have not seen this recap"
        ]
    return []


def _discrepancy(phase: str, warning: str) -> Discrepancy:
    kind, _, detail = warning.partition(": ")
    return Discrepancy(phase, kind, detail or kind)


def _in_window(rep: PhaseReport, since: float | None) -> bool:
    if since is None:
        return True
    stamps = [t for t in (rep.finished, rep.integrated, rep.started) if t is not None]
    return bool(stamps) and max(stamps) >= since


def _totals(reports: list[PhaseReport]) -> Totals:
    by_status: dict[str, int] = {}
    for rep in reports:
        key = rep.status or rep.live or "pending"
        by_status[key] = by_status.get(key, 0) + 1
    durations = [r.slot_s for r in reports if r.slot_s is not None]
    return Totals(
        phases=len(reports),
        by_status=dict(sorted(by_status.items())),
        slot_hours=sum(durations) / 3600.0,
        integ_hours=sum(r.integ_s for r in reports if r.integ_s is not None) / 3600.0,
        median_s=statistics.median(durations) if durations else None,
        mean_s=statistics.fmean(durations) if durations else None,
    )


# -- readers --------------------------------------------------------------
def _read_sentinels(cfg: Config) -> dict[str, list[Sentinel]]:
    """Every ``done/<phase>.<status>`` file, WITH its body, oldest first.

    Unlike :func:`gitq.sentinel_done` (which keeps one status per phase and never
    opens the file) all sentinels are kept: a phase that finished ``fail`` and was
    re-run ``ok`` has two, and the one that got overwritten is exactly the record a
    report exists to surface.
    """
    out: dict[str, list[Sentinel]] = {}
    if not cfg.done_dir.is_dir():
        return out
    for entry in sorted(cfg.done_dir.iterdir()):
        if entry.name.startswith(".") or "." not in entry.name or not entry.is_file():
            continue
        phase, _, status = entry.name.rpartition(".")
        if not phase or status not in STATUSES:
            continue
        try:
            body = entry.read_text(encoding="utf-8", errors="replace")
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        out.setdefault(phase, []).append(
            Sentinel(phase, status, mtime, _sentinel_note(body, phase, status))
        )
    for sents in out.values():
        sents.sort(key=lambda s: s.mtime)
    return out


def _sentinel_note(body: str, phase: str, status: str) -> str:
    """The recap out of a ``<phase> <status> <note>`` sentinel body.

    Written by :func:`launch._write_sentinel`. The phase/status prefix is stripped
    only when it is actually there, so a hand-written or future-format sentinel
    still yields its whole body rather than losing its first two words.
    """
    line = body.strip()
    prefix = f"{phase} {status}"
    return line[len(prefix) :].strip() if line.startswith(prefix) else line


def _read_jsonl(cfg: Config) -> dict[str, list[Attempt]]:
    """Attempt history from ``done/<phase>.jsonl``, one JSON object per line.

    Deliberately tolerant: a malformed line is skipped rather than failing the
    whole report, and the timestamp / note keys are accepted under their common
    spellings so this reader does not break the first time the writer renames a
    field. The canonical shape is ``{"ts": <epoch>, "status": …, "note": …}``.
    """
    out: dict[str, list[Attempt]] = {}
    if not cfg.done_dir.is_dir():
        return out
    for entry in sorted(cfg.done_dir.glob("*.jsonl")):
        phase = entry.name[: -len(".jsonl")]
        attempts: list[Attempt] = []
        try:
            lines = entry.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            attempts.append(
                Attempt(
                    ts=_num(_first(rec, "ts", "time", "timestamp", "at")),
                    status=str(_first(rec, "status", "result") or ""),
                    note=str(_first(rec, "note", "recap", "summary", "text") or ""),
                    verdict=str(_first(rec, "verdict") or ""),
                )
            )
        if attempts:
            out[phase] = attempts
    return out


def _read_recaps(cfg: Config) -> dict[str, recap_mod.Recap]:
    """Generated summaries, via :func:`recap.load_all` rather than re-parsing.

    :mod:`recap` owns the file format and already tolerates a partial or foreign
    dict; going through its loader means this report cannot drift out of step with
    it, and it keeps the ``reason`` a missing summary carries — "no turns
    captured" is a far better cell than a blank nobody can explain.
    """
    return {rec.phase: rec for rec in recap_mod.load_all(cfg) if rec.phase}


def _read_log(cfg: Config) -> tuple[dict[str, list[Run]], dict[str, list[str]]]:
    """Per-phase slot runs and launch denials from ``logs/supervisor.log``.

    A ``CLAIM`` opens a run and an ``EVENT done`` closes it, so a phase that was
    claimed twice (failed, re-run) yields two runs instead of one bogus interval
    spanning both. Lines whose timestamp cannot be decoded — legacy
    monotonic-only lines from an *earlier boot*, which :func:`parse_ts` refuses to
    guess at — still shape the runs; they just carry ``None`` instead of a
    confidently wrong wall clock.
    """
    runs: dict[str, list[Run]] = {}
    denials: dict[str, list[str]] = {}
    open_run: dict[str, Run] = {}
    if not cfg.supervisor_log.is_file():
        return runs, denials
    lines = logutil.read_all(cfg.supervisor_log).splitlines()  # rotated files too

    for raw in lines:
        ts, msg = parse_ts(raw)
        m = _CLAIM_RE.match(msg)
        if m:
            phase = m.group(1)
            run = Run(claimed=ts)
            runs.setdefault(phase, []).append(run)
            open_run[phase] = run
            continue
        m = _LAUNCH_RE.match(msg)
        if m:
            phase = m.group(1)
            run = open_run.get(phase)
            if run is None:
                run = Run()
                runs.setdefault(phase, []).append(run)
                open_run[phase] = run
            run.launched = ts
            continue
        m = _EVENT_DONE_RE.match(msg)
        if m:
            phase, status = m.group(1), m.group(2)
            run = open_run.pop(phase, None)
            if run is None:
                run = Run()
                runs.setdefault(phase, []).append(run)
            run.done_at = ts
            run.status = status
            continue
        m = _DENIED_RE.match(msg)
        if m:
            denials.setdefault(m.group(1), []).append(m.group(2).strip())
    return runs, denials


def _read_jobs(cfg: Config) -> dict[str, list[dict]]:
    """Operator jobs keyed by the phase they belong to, oldest first.

    ``P-op2`` files under P, so a phase's follow-ups read as one story; an
    ``op-<epoch>`` job queued with no phase is its own row.
    """
    out: dict[str, list[dict]] = {}
    for item in sorted(opqueue.load_all(cfg), key=lambda i: (i.queued_at, i.phase)):
        out.setdefault(opqueue.owning_phase(item.phase), []).append(
            {
                "job": item.phase,
                "state": item.state,
                "brief": " ".join(item.note.split()),
                "outcome": " ".join((item.outcome or item.last_error).split()),
            }
        )
    return out


def _read_pings(cfg: Config) -> tuple[dict[str, list[Ping]], bool]:
    """Owner notifications per phase, and whether the ledger exists at all.

    The second value matters more than it looks: an absent ledger means *no*
    phase has send records, which is indistinguishable from every send having
    been lost unless the caller knows which it is looking at.
    """
    out: dict[str, list[Ping]] = {}
    path = cfg.state_dir / telegram_mod.LEDGER_NAME
    if not path.is_file():
        return out, False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out, False
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # an interleaved/torn line costs that line, not the file
        if not isinstance(row, dict) or not row.get("phase"):
            continue  # run-level pings (the finish summary) belong to no phase
        out.setdefault(str(row["phase"]), []).append(
            Ping(
                ts=_num(row.get("ts")),
                kind=str(row.get("kind") or ""),
                delivered=bool(row.get("delivered")),
                error=str(row.get("error") or ""),
                suppressed=str(row.get("suppressed") or ""),
            )
        )
    return out, True


def _live_map(st: State) -> dict[str, str]:
    """Phases in flight right now, and what kind of in-flight they are."""
    live = {s.phase: "busy" for s in st.busy_slots() if s.phase}
    live.update({p: "integrating" for p in st.integ_queue})
    if st.integ_blocked:
        live[st.integ_blocked] = "integ-blocked"
    live.update({p: "waiting" for p in st.waiting})
    # A parked worker the owner has answered is at work again, in its own window.
    live.update({p: "parked" if st.asking(p) else "busy" for p in st.parked})
    # A batch's rows are at work in its seed's slot; one that has reported waits
    # to land with the batch (its sentinel is there, its record is not yet).
    for row in st.batch_rows():
        live.setdefault(row, "integrating" if row in st.batch_done else "busy")
    return live


def _first(rec: dict, *keys: str) -> object | None:
    for key in keys:
        if rec.get(key) not in (None, ""):
            return rec[key]
    return None


def _num(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# -- `--since` ------------------------------------------------------------
def parse_since(text: str, now: float | None = None) -> float:
    """Parse ``--since``: ``90m`` / ``6h`` / ``3d`` / ``2w``, or a date/datetime.

    Relative forms are the ones anyone actually types after a run ("what happened
    in the last 6 hours"); an absolute ``YYYY-MM-DD[ HH:MM]`` is accepted for
    pinning a report to a known incident. Raises ``ValueError`` on anything else
    rather than silently reporting the whole run.
    """
    raw = text.strip()
    now = time.time() if now is None else now
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if raw and raw[-1].lower() in units:
        try:
            return now - float(raw[:-1]) * units[raw[-1].lower()]
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt == "%m-%d":  # no year given: this year, or last year if in the future
            dt = dt.replace(year=datetime.fromtimestamp(now).year)
            if dt.timestamp() > now:
                dt = dt.replace(year=dt.year - 1)
        return dt.timestamp()
    raise ValueError(f"unparseable --since {text!r}: use 6h / 3d / 2026-08-27")


# -- rendering ------------------------------------------------------------
def render(rep: Report, decisions: bool = False) -> str:
    """The human report. ``decisions`` keeps only phases that recorded a note."""
    rows = [p for p in rep.phases if not decisions or p.substantive()]
    out: list[str] = []
    if rep.since is not None:
        out.append(f"since {_when(rep.since)}")
    if not rows:
        out.append("no phases recorded" + (" a decision" if decisions else ""))
        return "\n".join(out)

    if decisions:
        for p in rows:
            head = f"{p.phase}  [{p.status or p.live or _unrun_label(p.phase)}]"
            when = f"  {_when(p.finished)}" if p.finished else ""
            out.append(f"{head}{when}")
            if p.decision:
                out.append(f"    {p.decision}")
            # The owner's calls first: they are what every later line was built on.
            owner = [n for n in p.notes if n.get("kind") == notes_mod.OWNER_DECISION]
            for note in owner:
                out.append(f"    [owner] {note.get('text', '')}")
            for note in p.notes:
                if note.get("kind") != notes_mod.OWNER_DECISION:
                    out.append(f"    [{note.get('kind', 'decision')}] {note.get('text', '')}")
            for job in p.operator_jobs:
                out.append(f"    {_job_line(job)}")
        out.append("")
        owner_n = sum(
            1 for p in rows for n in p.notes if n.get("kind") == notes_mod.OWNER_DECISION
        )
        recorded = sum(len(p.notes) for p in rows) - owner_n
        jobs_n = sum(len(p.operator_jobs) for p in rows)
        extra = [
            f"{recorded} explicit `swarm note` entries" if recorded else "",
            f"{owner_n} owner decision(s)" if owner_n else "",
            f"{jobs_n} operator job(s)" if jobs_n else "",
        ]
        tail = ", ".join(e for e in extra if e)
        out.append(
            f"{len(rows)} of {len(rep.phases)} phases recorded a decision"
            + (f" ({tail})" if tail else "")
        )
        return "\n".join(out)

    header = ("phase", "status", "finished", "in-slot", "integ", "recap")
    table = [
        (
            p.phase,
            p.status or p.live or "-",
            _when(p.finished),
            _dur(p.slot_s),
            _dur(p.integ_s),
            _clip(p.decision or _no_recap(p), 52),
        )
        for p in rows
    ]
    widths = [
        max(len(str(r[i])) for r in (header, *table)) for i in range(len(header))
    ]
    out.append("  ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip())
    for row in table:
        out.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())

    t = rep.totals
    counts = " ".join(f"{k}={v}" for k, v in t.by_status.items()) or "none"
    out.append("")
    out.append(f"{t.phases} phases: {counts}")
    out.append(
        f"in-slot median {_dur(t.median_s)} / mean {_dur(t.mean_s)};"
        f" {t.slot_hours:.1f} slot-hours, {t.integ_hours:.1f}h integrating"
    )
    if rep.warnings:
        out.append("")
        out.append(f"warnings ({len(rep.warnings)}):")
        for w in rep.warnings:
            out.append(f"  {w.phase}: {w.detail}")
    return "\n".join(out)


def _unrun_label(name: str) -> str:
    """A row with no status: an open phase, or one of the two non-phase keys."""
    if name == notes_mod.OVERSEER:
        return "Overseer"
    if name.startswith(opqueue.ADHOC_PREFIX):
        return "ad-hoc job"
    return "pending"


def _job_line(job: dict) -> str:
    """One operator job: its state, then what it did — or, while open, what it is for."""
    state = job.get("state", "?")
    what = job.get("outcome") if state in opqueue.TERMINAL else ""
    return f"[operator {state}] {job.get('job', '?')}: {_clip(what or job.get('brief', ''), 160)}"


def _no_recap(p: PhaseReport) -> str:
    """What to show when a phase produced no text at all.

    :mod:`recap` sets ``summary`` to ``None`` and fills ``reason`` whenever it
    could not produce one, and the two are mutually exclusive — so an empty cell
    here is explainable, and "no-turns-captured" is worth far more to the owner
    than a blank they have to go and investigate.
    """
    return f"(no recap: {p.recap_reason})" if p.recap_reason else ""


def _when(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "-"


def _dur(seconds: float | None) -> str:
    """A duration a human can compare at a glance: ``45s`` / ``12m`` / ``1h04m``."""
    if seconds is None:
        return "-"
    td = timedelta(seconds=int(seconds))
    hours, rem = divmod(int(td.total_seconds()), 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _clip(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"
