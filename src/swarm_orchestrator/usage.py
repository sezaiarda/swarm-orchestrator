"""Usage per run: the 5-hour and weekly limit paces, cost, and the run summaries.

The usual measurement is "run one worker for twelve hours, then read what it
used per hour, for the 5-hour window and the weekly one". Both figures come from
Claude Code's status-line payload (``rate_limits.five_hour`` / ``seven_day``),
logged by the meters tap to ``meters/limits.jsonl`` whenever either moves.

A per-hour average of a windowed counter is not end-minus-start: the 5-hour
figure falls back to zero every time its window resets, so a twelve-hour run
spans two or three windows and a naive delta reads as negative or near zero.
:func:`window_pace` sums the *increases* inside each window and counts a new
window's first reading as usage since that reset.

Both figures are account-wide. Any other Claude session on the same login in
the same hours counts too, and nothing here can tell those apart.

The owner may switch the logged-in account (``/login``) mid-run, so every sample
carries the account it was read under (:func:`login_account`): a short hash of
Claude Code's ``accountUuid``, never the uuid or the email themselves. Figures
of two accounts are never blended; rows written before the tag existed load as
account unknown.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from . import logutil, runs, statuses

METERS_DIR = "meters"
LIMITS_LOG = "limits.jsonl"
SESSIONS_LOG = "sessions.jsonl"

#: A ``resets_at`` that moves forward by more than this is a new window, not jitter.
RESET_JUMP_S = 10 * 60
#: Under this much elapsed time a per-hour figure is one sample wearing a unit.
MIN_PACE_S = 15 * 60

SKEW_NOTE = ("5-hour and weekly figures are account-wide: other Claude sessions on "
             "the same account during a run count too, and cannot be told apart.")


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# -- the logged-in account ------------------------------------------------------
#: Claude Code's global config; its ``oauthAccount`` is the login every session
#: uses, rewritten by ``/login``. ``SWARM_CLAUDE_JSON`` points elsewhere (the
#: suite points it nowhere).
CLAUDE_JSON = Path.home() / ".claude.json"

_login_seen: tuple = (None, None)


def account_key(uuid: str) -> str:
    """The short, stable name an account goes by here: never the uuid itself."""
    return hashlib.sha256(uuid.encode("utf-8")).hexdigest()[:8]


def login_account() -> str | None:
    """:func:`account_key` of the account Claude Code is logged in as now,
    ``None`` when that cannot be read. The file is parsed again only when it
    changed, so a caller on every tick costs a ``stat``."""
    global _login_seen
    path = Path(os.environ.get("SWARM_CLAUDE_JSON") or CLAUDE_JSON)
    try:
        st = path.stat()
    except OSError:
        return None
    stamp = (str(path), st.st_mtime_ns, st.st_size)
    if _login_seen[0] == stamp:
        return _login_seen[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    uuid = oauth.get("accountUuid") if isinstance(oauth, dict) else None
    key = account_key(uuid) if isinstance(uuid, str) and uuid else None
    _login_seen = (stamp, key)
    return key


# -- samples ------------------------------------------------------------------
@dataclass(frozen=True)
class Sample:
    """One ``limits.jsonl`` row. Legacy rows (``{ts, pct, resets_at}``) are weekly-only."""

    ts: float
    run_id: str | None = None
    five_pct: float | None = None
    five_resets_at: float | None = None
    week_pct: float | None = None
    week_resets_at: float | None = None
    #: :func:`account_key` of the login it was read under; ``None`` = unknown.
    account: str | None = None
    #: ``"api"`` when the usage endpoint answered it (:func:`caps.fetch_api`);
    #: ``None`` for a status line's reading.
    src: str | None = None


def parse_sample(row) -> Sample | None:
    if not isinstance(row, dict) or _num(row.get("ts")) is None:
        return None
    if "pct" in row and "week_pct" not in row:  # written before runs existed
        return Sample(ts=_num(row["ts"]), week_pct=_num(row.get("pct")),
                      week_resets_at=_num(row.get("resets_at")))
    rid, acct, src = row.get("run_id"), row.get("account"), row.get("src")
    return Sample(
        ts=_num(row["ts"]), run_id=rid if isinstance(rid, str) else None,
        five_pct=_num(row.get("five_pct")), five_resets_at=_num(row.get("five_resets_at")),
        week_pct=_num(row.get("week_pct")), week_resets_at=_num(row.get("week_resets_at")),
        account=acct if isinstance(acct, str) and acct else None,
        src=src if isinstance(src, str) and src else None,
    )


def sample_row(ts: float, run_id: str | None, five: dict | None, week: dict | None,
               account: str | None = None) -> dict:
    """The row the tap appends. ``five``/``week`` are the meter's ``{"pct", "resets_at"}``."""
    five, week = five or {}, week or {}
    return {"ts": ts, "run_id": run_id,
            "five_pct": five.get("pct"), "five_resets_at": five.get("resets_at"),
            "week_pct": week.get("pct"), "week_resets_at": week.get("resets_at"),
            "account": account}


def same_values(a: dict, b: dict) -> bool:
    keys = ("five_pct", "five_resets_at", "week_pct", "week_resets_at", "account")
    return all(a.get(k) == b.get(k) for k in keys)


class SampleTail:
    """``limits.jsonl``, read incrementally: the dashboard re-asks every two seconds."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.samples: list[Sample] = []
        self._offset = 0

    def poll(self) -> bool:
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        if size < self._offset:  # rotated or truncated: start over
            self.samples, self._offset = [], 0
        if size == self._offset:
            return False
        try:
            with self.path.open("rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read(size - self._offset)
        except OSError:
            return False
        # Only whole lines: a row mid-append is picked up by the next poll.
        whole = chunk[: chunk.rfind(b"\n") + 1]
        self._offset += len(whole)
        for line in whole.decode("utf-8", "replace").splitlines():
            try:
                s = parse_sample(json.loads(line))
            except ValueError:
                continue
            if s is not None:
                self.samples.append(s)
        return bool(whole)


def load_samples(path: Path) -> list[Sample]:
    tail = SampleTail(path)
    tail.poll()
    return tail.samples


def newest_account(samples: list[Sample]) -> str | None:
    """The account of the newest tagged sample."""
    tagged = [s for s in samples if s.account is not None]
    return max(tagged, key=lambda s: s.ts).account if tagged else None


def active_account(samples: list[Sample]) -> str | None:
    """The account the swarm runs on now: the login, else the newest tag."""
    return login_account() or newest_account(samples)


def of_account(samples: list[Sample], account: str | None) -> list[Sample]:
    """``account``'s samples; all of them when the account is not known.

    An untagged sample is left out once the account is known: it may be
    anyone's, and one wrong figure would hold or free the swarm."""
    return list(samples) if account is None else [s for s in samples if s.account == account]


def current(samples: list[Sample]) -> list[Sample]:
    """The samples of :func:`active_account`: what every "now" figure reads.
    A stale snapshot (:func:`restarts`) is not one of them."""
    mine = of_account(samples, active_account(samples))
    return [s for s, hit in zip(mine, restarts(mine)) if hit is not None]


def latest(samples: list[Sample], which: str, now: float) -> tuple[float, float | None] | None:
    """``(pct, resets_at)`` of the newest reading of ``five``/``week`` still in its window."""
    for s in reversed(samples):
        pct, resets = getattr(s, f"{which}_pct"), getattr(s, f"{which}_resets_at")
        if pct is not None:
            return None if resets is not None and resets <= now else (pct, resets)
    return None


# -- accounts -----------------------------------------------------------------
#: A reading this close to its window's end may already belong to the next one.
_EDGE_S = 60.0


def _moved(s: Sample, cur: dict[str, float | None]) -> dict[str, float] | None:
    """The windows whose reset ``s`` moves while the window it replaces is
    still open, which one account's windows never do; ``None`` when none does.

    A later reset read before the current window ends, or an earlier one read
    before *that* ends, is another account's window. An earlier reset read
    after it passed is only a lagging reading of the last window."""
    out = {}
    for which in ("five", "week"):
        r, res = getattr(s, f"{which}_resets_at"), cur[which]
        if r is None or res is None or abs(r - res) <= RESET_JUMP_S:
            continue
        if (r > res and s.ts < res - _EDGE_S) or (r < res and s.ts < r - _EDGE_S):
            out[which] = r
    return out or None


def _follow(s: Sample, cur: dict[str, float | None]) -> None:
    """Carry each window's reset forward past ``s``: jitter and new windows,
    never back to an earlier window a lagging reading still reports."""
    for which in ("five", "week"):
        r, res = getattr(s, f"{which}_resets_at"), cur[which]
        if r is not None and (res is None or r > res - RESET_JUMP_S):
            cur[which] = r


def accounts(samples: list[Sample]) -> list[int | None]:
    """Which account each reading came from, as a segment number per sample
    (in ``samples``' order), ``None`` for a reading to ignore.

    The status line reports no account, so a switch is read off the figures:
    an account change mid-window moved the weekly
    reading sharply down and its reset to another day, which
    the running-maximum logic below took for a lagging reading and a new
    window, charting the old account and counting the new one's reading as used.
    A window's reset moving while that window is still open (:func:`_moved`)
    starts a new segment once the next reading agrees with it; a lone reading
    the next one contradicts is a lagging session of the other account, and is
    ignored. The newest readings are believed until one contradicts them.
    A drop under an unchanged reset stays a lagging reading, as before: that is
    the far commoner case, and nothing tells the two apart.

    A sample tagged with its account (:attr:`Sample.account`) needs none of
    that: a new tag is a new segment at once. Once tags appear, an untagged
    reading that moves a window is ignored rather than trusted over them.
    """
    order = sorted(range(len(samples)), key=lambda i: samples[i].ts)
    tags: list[int | None] = [None] * len(samples)
    seg = 0
    cur: dict[str, float | None] = {"five": None, "week": None}
    who: str | None = None
    pending: list[int] = []
    pend: dict[str, float] = {}
    for i in order:
        s = samples[i]
        moved = _moved(s, cur)
        if s.account is not None:
            if (s.account != who) if who is not None else moved is not None:
                seg += 1
                cur = {w: getattr(s, f"{w}_resets_at") for w in ("five", "week")}
            else:
                _follow(s, cur)
            who, tags[i] = s.account, seg
            pending, pend = [], {}
            continue
        if moved is not None and who is not None:
            continue
        if moved is None:
            tags[i] = seg
            _follow(s, cur)
            pending, pend = [], {}
            continue
        agrees = bool(pending) and all(abs(r - pend[w]) <= RESET_JUMP_S
                                       for w, r in moved.items() if w in pend) \
            and bool(set(moved) & set(pend))
        if not agrees:
            pending, pend = [i], moved
            continue
        seg += 1
        for j in pending + [i]:
            tags[j] = seg
        cur = {w: getattr(s, f"{w}_resets_at") for w in ("five", "week")}
        pending, pend = [], {}
    if pending:
        seg += 1
        for j in pending:
            tags[j] = seg
    return tags


def switch_times(samples: list[Sample]) -> list[float]:
    """When the account behind the readings changed (:func:`accounts`), oldest first."""
    first: dict[int, float] = {}
    for s, t in zip(samples, accounts(samples)):
        if t:
            first[t] = min(first.get(t, s.ts), s.ts)
    return [first[t] for t in sorted(first)]


def switched_at(samples: list[Sample]) -> float | None:
    """When the account behind the readings last changed, ``None`` if it never did."""
    times = switch_times(samples)
    return times[-1] if times else None


# -- resets in place ------------------------------------------------------------
#: The usage endpoint reading this far under a window's highest figure, the
#: window's reset where it was, is that window emptied early; less is rounding.
INPLACE_DROP = 5.0


def restarts(samples: list[Sample]) -> list[tuple[str, ...] | None]:
    """Per sample (in ``samples``' order): the windows (``"five"``/``"week"``)
    its reading finds *reset in place*, or ``None`` for a reading to ignore.

    A limit can be reset before its window ends. The window keeps its reset
    time and its figure falls to zero, which under an unchanged reset is also
    what a lagging status line looks like. Every reader keeps the running
    maximum against those, so the old figure stood until the window ended: the
    weekly line sat at 100% for days, nothing after the reset counted as used,
    and a hold could not lift.

    Only the usage endpoint tells the two apart (``src == "api"``). It answers
    for the account as it is now, where a status line repeats what its session
    last heard, which may be hours old. So the endpoint reading
    :data:`INPLACE_DROP` under the window's highest figure, its reset unchanged
    or gone, starts the window again at that reading; and once one window has,
    another the same answer reports as not open (no reset time, though the last
    one known has not ended) was closed with it.

    Sessions that have not spoken to the server since still render the figures
    from before: too *high* now, which no running maximum survives. What gives
    them away is the window that did move: a status line still naming the
    window a reset closed is a stale snapshot, and all of it is ignored
    (``None``), not only that window's half. A window reset in place with no
    other closed beside it has no such tell; the next endpoint reading under
    the stale figure restarts it again.
    """
    order = sorted(range(len(samples)), key=lambda i: samples[i].ts)
    segs = accounts(samples)
    out: list[tuple[str, ...] | None] = [()] * len(samples)
    names = ("five", "week")
    account = None
    top: dict[str, float | None] = dict.fromkeys(names)
    res: dict[str, float | None] = dict.fromkeys(names)
    gone: dict[str, float | None] = dict.fromkeys(names)
    for i in order:
        s = samples[i]
        if segs[i] is None:
            out[i] = None
            continue
        if segs[i] != account:
            account = segs[i]
            top, res, gone = dict.fromkeys(names), dict.fromkeys(names), dict.fromkeys(names)
        api = s.src == "api"
        read = {w: (getattr(s, f"{w}_pct"), getattr(s, f"{w}_resets_at")) for w in names}
        stale = False
        for w, (_, rs) in read.items():
            if res[w] is not None and s.ts >= res[w]:  # the window ended by itself
                top[w] = res[w] = None
            if gone[w] is not None and (s.ts >= gone[w] or (
                    api and rs is not None and abs(rs - gone[w]) <= RESET_JUMP_S)):
                gone[w] = None  # it would have ended by now, or the endpoint names it again
            if rs is not None and not api and gone[w] is not None \
                    and abs(rs - gone[w]) <= RESET_JUMP_S:
                stale = True
        if stale:
            out[i] = None
            continue
        fell = [w for w, (pct, rs) in read.items()
                if api and pct is not None and top[w] is not None and res[w] is not None
                and pct < top[w] - INPLACE_DROP and (rs is None or abs(rs - res[w]) <= RESET_JUMP_S)]
        hit = []
        for w, (pct, rs) in read.items():
            if pct is None or (None not in (rs, res[w]) and rs < res[w] - RESET_JUMP_S):
                continue  # nothing read, or a reading from a window already over
            closed = bool(fell) and api and rs is None and res[w] is not None
            if w in fell or closed:
                hit.append(w)
                top[w] = None
                if rs is None:
                    gone[w], res[w] = res[w], None
            elif res[w] is not None and rs is not None and rs > res[w] + RESET_JUMP_S:
                top[w] = None  # a new window
            top[w] = pct if top[w] is None else max(top[w], pct)
            if rs is not None:
                res[w] = rs
        out[i] = tuple(hit)
    return out


# -- pace ---------------------------------------------------------------------
@dataclass(frozen=True)
class Pace:
    """Usage of one window over ``[start, end]``: points used, per hour, windows seen."""

    used: float = 0.0
    hours: float = 0.0
    windows: int = 0

    @property
    def per_h(self) -> float | None:
        if self.hours * 3600 < MIN_PACE_S or not self.windows:
            return None
        return self.used / self.hours


def window_pace(points, start: float, end: float) -> Pace:
    """Piecewise usage of a resetting counter from ``(ts, pct, resets_at)`` points.

    Inside one window only increases count, measured from the window's running
    maximum so a reading that wobbles down and back up is not counted twice. A
    new window — ``resets_at`` jumping forward, or (only when there is no
    ``resets_at`` to go by) the figure dropping — starts from zero at its reset,
    so its first reading is all usage since then. With ``resets_at`` known, a
    drop under the same reset is a stale reading from a session whose status
    line lags, and a reading carrying an earlier window's reset is stale too:
    both are skipped. Treating the first as a reset once counted a whole
    weekly figure as fresh usage.

    A point may carry a fourth item, its account (:func:`accounts`): a reading
    of another account starts from its own figure, never counting it as used,
    and one tagged ``None`` is skipped. A fifth, true, says the window was
    reset in place at this reading (:func:`restarts`): a new window whatever
    its ``resets_at`` says, so the figure is usage since that reset.
    """
    pts = sorted(((pt[0], pt[1], pt[2], pt[3] if len(pt) > 3 else 0, len(pt) > 4 and bool(pt[4]))
                  for pt in points if pt[1] is not None and start <= pt[0] <= end),
                 key=lambda x: x[0])
    used, windows = 0.0, 0
    top = res = None
    account = 0
    for _, pct, resets, seg, restart in pts:
        if seg is None:
            continue
        if seg != account and top is not None:
            windows += 1
            top, res, account = pct, resets, seg
            continue
        account = seg
        if restart and top is not None:
            windows += 1
            used += pct
            top, res = pct, resets
            continue
        known = resets is not None and res is not None
        if known and (res - resets > RESET_JUMP_S or (
                abs(resets - res) <= RESET_JUMP_S and pct < top - 0.5)):
            continue  # stale: an earlier window, or a lagging reading of this one
        new = top is None or (resets - res > RESET_JUMP_S if known else pct < top - 0.5)
        if top is None:
            windows = 1
        elif new:
            windows += 1
            used += pct
        else:
            used += max(0.0, pct - top)
        top = pct if new else max(top, pct)
        res = resets if resets is not None else res
    return Pace(used=used, hours=max(0.0, end - start) / 3600, windows=windows)


def _points(samples: list[Sample], which: str):
    """:func:`window_pace`'s points for one window: a stale snapshot
    (:func:`restarts`) is tagged ``None`` like any reading to skip."""
    for s, seg, hit in zip(samples, accounts(samples), restarts(samples)):
        yield (s.ts, getattr(s, f"{which}_pct"), getattr(s, f"{which}_resets_at"),
               None if hit is None else seg, bool(hit) and which in hit)


def five_pace(samples: list[Sample], start: float, end: float) -> Pace:
    return window_pace(_points(samples, "five"), start, end)


def week_pace(samples: list[Sample], start: float, end: float) -> Pace:
    return window_pace(_points(samples, "week"), start, end)


def by_account(samples: list[Sample], start: float, end: float) -> list[dict]:
    """Each account's points used inside ``[start, end]``, in the order the
    accounts were first read; empty unless more than one tagged account ran."""
    first: dict[str, float] = {}
    for s in samples:
        if s.account is not None and start <= s.ts <= end:
            first[s.account] = min(first.get(s.account, s.ts), s.ts)
    if len(first) < 2:
        return []
    out = []
    for acct in sorted(first, key=first.get):
        mine = of_account(samples, acct)
        out.append({"account": acct, "five_used": five_pace(mine, start, end).used,
                    "week_used": week_pace(mine, start, end).used})
    return out


# -- cost ---------------------------------------------------------------------
def load_sessions(meters_dir: Path) -> list[dict]:
    """Every worker session the tap saw: live meter files plus finished ones.

    A relaunched phase replaces its meter file, so the tap logs the outgoing
    session to ``sessions.jsonl`` first; without it a retried phase's first
    attempt would vanish from the cost.
    """
    rows: dict[str, dict] = {}

    def keep(row) -> None:
        if not isinstance(row, dict) or _num(row.get("cost_usd")) is None:
            return
        key = str(row.get("session_id") or f"{row.get('phase')}@{row.get('started_at')}")
        if key not in rows or (_num(row.get("ts")) or 0) > (_num(rows[key].get("ts")) or 0):
            rows[key] = row

    try:
        for line in (meters_dir / SESSIONS_LOG).read_text(encoding="utf-8").splitlines():
            try:
                keep(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    try:
        paths = list(meters_dir.glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        try:
            keep(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return list(rows.values())


def run_cost(sessions: list[dict], start: float, end: float) -> float | None:
    """API-equivalent $ spent inside ``[start, end]``, prorated for straddling sessions."""
    total, seen = 0.0, False
    for s in sessions:
        cost, t0, t1 = _num(s.get("cost_usd")), _num(s.get("started_at")), _num(s.get("ts"))
        if cost is None or t1 is None:
            continue
        t0 = t1 if t0 is None else t0
        lo, hi = max(t0, start), min(t1, end)
        if hi < lo:
            continue
        seen = True
        total += cost if t1 <= t0 else cost * (hi - lo) / (t1 - t0)
    return total if seen else None


# -- summaries ----------------------------------------------------------------
def _done_events(events, start: float, end: float) -> list:
    return [e for e in events if e.kind == "done" and e.ts is not None and start <= e.ts <= end]


def _rate(n: float | None, hours: float) -> float | None:
    return None if n is None or hours * 3600 < MIN_PACE_S else n / hours


def summarize(rec: dict, end: float, *, samples: list[Sample], events: list,
              sessions: list[dict]) -> dict:
    """What a run did and used. ``rec`` is a run record (or the legacy pseudo-run)."""
    start = float(rec["epoch_ts"])
    hours = max(0.0, end - start) / 3600
    done = _done_events(events, start, end)
    finished = sum(1 for e in done if e.status in statuses.INTEGRATES)
    failed = sum(1 for e in done if e.status == statuses.FAIL)
    five, week = five_pace(samples, start, end), week_pace(samples, start, end)
    cost = run_cost(sessions, start, end)
    out = {
        "run_id": rec.get("run_id"),
        "start": start,
        "end": end,
        "hours": round(hours, 3),
        "max_workers": rec.get("max_workers"),
        "isolation": rec.get("isolation"),
        "phases_finished": finished,
        "phases_failed": failed,
        "phases_per_h": _rate(finished, hours),
        "five_used": five.used,
        "five_pct_per_h": five.per_h,
        "five_windows": five.windows,
        "week_used": week.used,
        "week_pct_per_h": week.per_h,
        "usd": cost,
        "usd_per_h": _rate(cost, hours),
        "accounts": by_account(samples, start, end),
        "segments": [],
    }
    spans = runs.segments(rec, end) if rec.get("run_id") else []
    if len(spans) > 1:
        for t0, t1, workers, iso in spans:
            h = max(0.0, t1 - t0) / 3600
            out["segments"].append({
                "from": t0, "to": t1, "hours": round(h, 3),
                "max_workers": workers, "isolation": iso,
                "phases_finished": sum(1 for e in _done_events(events, t0, t1)
                                       if e.status in statuses.INTEGRATES),
                "five_pct_per_h": five_pace(samples, t0, t1).per_h,
                "week_pct_per_h": week_pace(samples, t0, t1).per_h,
            })
    return out


class Sources:
    """The on-disk inputs of a summary for one state dir, read once."""

    def __init__(self, cfg) -> None:
        from .tui.data import parse_events  # the log grammar lives there

        self.cfg = cfg
        meters_dir = Path(cfg.state_dir) / METERS_DIR
        self.samples = load_samples(meters_dir / LIMITS_LOG)
        self.sessions = load_sessions(meters_dir)
        # The rotated files too: a run can outlast a rotation.
        self.events = parse_events(logutil.read_all(Path(cfg.supervisor_log)))

    def summarize(self, rec: dict, end: float) -> dict:
        return summarize(rec, end, samples=self.samples, events=self.events,
                         sessions=self.sessions)

    def last_activity(self, rec: dict) -> float:
        """Where a run left open by a crash really ended: its last sample or event."""
        start = float(rec["epoch_ts"])
        stamps = [s.ts for s in self.samples if s.ts >= start]
        stamps += [e.ts for e in self.events if e.ts is not None and e.ts >= start]
        return max([start, *stamps])

    def legacy(self, until: float) -> dict | None:
        """The pre-runs period: from the last supervisor start before ``until``.

        A state dir written before runs existed has no epoch; the last
        ``SUPERVISOR-START`` is the nearest thing to one, and a period that ran
        over days of downtime would average to nothing useful.
        """
        starts = [e.ts for e in self.events
                  if e.kind == "supervisor-start" and e.ts is not None and e.ts < until]
        if not starts:
            return None
        rec = {"run_id": None, "epoch_ts": max(starts), "max_workers": None, "isolation": None}
        out = self.summarize(rec, until)
        out["legacy"] = True
        return out


# -- lifecycle, as the CLI and the dashboard drive it --------------------------
def _seed(cfg, rec: dict, now: float) -> None:
    """Give a fresh run a baseline reading at its epoch.

    Without one, usage between the epoch and the first reading that moves is
    invisible: the pace would start from whatever the figure was when it next
    changed. The freshest meter file is the last figure anyone saw; a window
    that has reset since is left out rather than trusted.
    """
    meters_dir = Path(cfg.state_dir) / METERS_DIR
    best = None
    try:
        paths = list(meters_dir.glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(m, dict) and (m.get("five_hour") or m.get("seven_day")):
            if best is None or (_num(m.get("ts")) or 0) > (_num(best.get("ts")) or 0):
                best = m
    if best is None:
        return

    def live(w):
        return w if isinstance(w, dict) and (_num(w.get("resets_at")) or now + 1) > now else None

    acct = best.get("account")
    row = sample_row(now, rec["run_id"], live(best.get("five_hour")), live(best.get("seven_day")),
                     acct if isinstance(acct, str) and acct else None)
    if row["five_pct"] is None and row["week_pct"] is None:
        return
    try:
        with (meters_dir / LIMITS_LOG).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _archive(cfg, rec: dict, src: Sources) -> None:
    """The run's slice of the samples and its sessions, beside its ``run.json``."""
    start, end = float(rec["epoch_ts"]), float(rec.get("end_ts") or time.time())
    folder = runs.runs_dir(cfg.state_dir) / rec["run_id"]
    try:
        folder.mkdir(parents=True, exist_ok=True)
        rows = [s.__dict__ for s in src.samples if start <= s.ts <= end]
        (folder / LIMITS_LOG).write_text("".join(json.dumps(r) + "\n" for r in rows),
                                         encoding="utf-8")
        mine = [s for s in src.sessions
                if (_num(s.get("ts")) or 0) >= start and (_num(s.get("started_at")) or 0) <= end]
        (folder / "sessions.json").write_text(json.dumps(mine, indent=1), encoding="utf-8")
    except OSError:
        pass


def start_run(cfg, reason: str = "up", now: float | None = None) -> tuple[dict, dict | None]:
    """Open a run for ``cfg`` (closing a stale one at its last activity)."""
    now = time.time() if now is None else now
    src = Sources(cfg)
    prev = runs.current(cfg.state_dir)
    workers, iso = cfg.max_workers, cfg.git_isolation
    if reason == "reset" and prev is not None:
        # A reset restarts nothing, so the config in force is the live run's —
        # which a reload may have moved away from the file's.
        workers, iso = runs.config_at(prev, now)
    # `up` finding a run still open means it was never downed: end it where it
    # went quiet. A reset ends it now — it was live until this moment.
    end = None if reason == "reset" else src.last_activity
    rec, closed = runs.start(cfg.state_dir, workers, iso, reason=reason, now=now,
                             summarize=src.summarize, end=end)
    if closed is not None:
        _archive(cfg, closed, src)
    _seed(cfg, rec, now)
    return rec, closed


def close_run(cfg, reason: str = "down", now: float | None = None) -> dict | None:
    src = Sources(cfg)
    closed = runs.close(cfg.state_dir, reason=reason, now=now, summarize=src.summarize)
    if closed is not None:
        _archive(cfg, closed, src)
    return closed


def live_summary(cfg, src: Sources | None = None, now: float | None = None) -> dict | None:
    """The open run summarised up to now, or the legacy period when none is open."""
    now = time.time() if now is None else now
    src = src or Sources(cfg)
    rec = runs.current(cfg.state_dir)
    if rec is None:
        return src.legacy(now)
    out = src.summarize(rec, now)
    out["live"] = True
    return out


# -- `swarm usage` ------------------------------------------------------------
def _f(value, fmt: str = "{:.1f}", none: str = "—") -> str:
    return none if value is None else fmt.format(value)


def _when(ts: float | None) -> str:
    return "—" if ts is None else time.strftime("%m-%d %H:%M", time.localtime(ts))


def _in(ts: float | None, now: float) -> str:
    if ts is None:
        return "?"
    s = max(0.0, ts - now)
    return f"{s / 3600:.1f}h" if s < 48 * 3600 else f"{s / 86400:.1f}d"


# -- the short block: the bot's `/usage` reply -----------------------------------
#: A newest sample older than this is flagged stale: nothing rendered a status
#: line since, so the percentages may have moved without anyone seeing it.
STALE_S = 30 * 60


def newest_sample(samples: list[Sample]) -> Sample | None:
    """The most recent row carrying either figure."""
    live = [s for s in samples if s.five_pct is not None or s.week_pct is not None]
    return max(live, key=lambda s: s.ts, default=None)


def _clock(ts: float, now: float) -> str:
    """``16:00`` today, ``Wed 11:00`` within the week, ``09-24 14:05`` beyond."""
    at, today = time.localtime(ts), time.localtime(now)
    if (at.tm_year, at.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("%H:%M", at)
    fmt = "%a %H:%M" if abs(ts - now) < 7 * 86400 else "%m-%d %H:%M"
    return time.strftime(fmt, at)


def _ago(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.1f} h ago"
    return f"{seconds / 86400:.1f} d ago"


def as_of(samples: list[Sample], now: float) -> str | None:
    """``as of 14:05, 12 min ago`` for the newest sample; ``None`` when there is none.

    Samples arrive only when some session renders its status line, so a quiet
    swarm's figures can be hours old; every reader says how old.
    """
    s = newest_sample(samples)
    if s is None:
        return None
    age = max(0.0, now - s.ts)
    return (f"as of {_clock(s.ts, now)}, {_ago(age)}"
            + (" — stale" if age >= STALE_S else ""))


def _window(samples: list[Sample], which: str, label: str, now: float) -> str:
    for s in reversed(samples):
        pct, resets = getattr(s, f"{which}_pct"), getattr(s, f"{which}_resets_at")
        if pct is None:
            continue
        if resets is not None and resets <= now:
            return f"{label}: not known, the window reset at {_clock(resets, now)}."
        when = "" if resets is None else f", resets {_clock(resets, now)}"
        return f"{label} {pct:.0f}%{when}."
    return f"{label}: not reported."


def brief(samples: list[Sample], now: float, cap_lines: list[str] = ()) -> str:
    """The bot's answer to ``/usage``: both limits of the account in use, how
    old they are, and the caps."""
    account = active_account(samples)
    samples = current(samples)
    s = newest_sample(samples)
    if s is None:
        lines = ["No usage reading yet. One arrives while a swarm session runs."]
    else:
        on = f", account {account}" if account else ""
        lines = [_window(samples, "week", "Weekly", now),
                 _window(samples, "five", "5-hour", now),
                 f"Read at {_clock(s.ts, now)}, {_ago(max(0.0, now - s.ts))}{on}."]
    return "\n".join([*lines, *cap_lines])


def brief_for(cfg, now: float | None = None) -> str:
    """:func:`brief` for a project, read from disk. Never raises: the bot must answer."""
    from . import caps, state as state_mod  # caps imports this module

    now = time.time() if now is None else now
    try:
        samples = load_samples(Path(cfg.state_dir) / METERS_DIR / LIMITS_LOG)
        if not cfg.usage_enabled:
            cap = ["Usage caps are off."]
        else:
            cap = (caps.describe_hold(state_mod.read(cfg).usage_hold, now)
                   or [caps.limits_line(cfg.usage_rules)])
        return brief(samples, now, cap)
    except Exception as exc:  # noqa: BLE001 - diagnostics only, never fatal
        return f"Usage is unavailable right now ({type(exc).__name__}: {exc})"[:300]


def render(cur: dict | None, past: list[dict], samples: list[Sample], now: float) -> str:
    """The text ``swarm usage`` prints: the open run in full, then a table of past ones."""
    lines = []
    if cur is None:
        lines.append("no run open and no supervisor start in the log — nothing to measure yet")
    else:
        head = ("legacy period (before runs were recorded; since the last supervisor start)"
                if cur.get("legacy") else f"current run {cur['run_id']}")
        cfg = ("" if cur.get("legacy") else
               f" · {cur.get('max_workers')} worker(s) · isolation {cur.get('isolation')}")
        lines.append(f"{head}\n  started {_when(cur['start'])} · {cur['hours']:.1f} h elapsed{cfg}")
        span = "this period" if cur.get("legacy") else "this run"
        account = active_account(samples)
        mine = current(samples)
        on = f" · account {account}" if account else ""
        lines.append(f"  sample  {as_of(mine, now) or 'none yet'}{on}")
        for which, label in (("five", "5-hour"), ("week", "weekly")):
            now_fig = latest(mine, which, now)
            state = ("not reported" if now_fig is None
                     else f"now {now_fig[0]:.0f}% (resets in {_in(now_fig[1], now)})")
            extra = f" over {cur['five_windows']} window(s)" if which == "five" else ""
            lines.append(f"  {label:<7} {state:<28} {span} {_f(cur[f'{which}_pct_per_h'], '{:.2f}')} %/h"
                         f" · {cur[f'{which}_used']:.0f} pts used{extra}")
        for a in cur.get("accounts") or []:
            mark = " (now)" if a["account"] == account else ""
            lines.append(f"    account {a['account']}{mark}: 5-hour {a['five_used']:.0f} pts"
                         f" · weekly {a['week_used']:.0f} pts used {span}")
        lines.append(f"  phases  {cur['phases_finished']} finished · {cur['phases_failed']} failed"
                     f" · {_f(cur['phases_per_h'], '{:.2f}')}/h"
                     f"   cost {_f(cur['usd'], '${:.2f}')} · {_f(cur['usd_per_h'], '${:.2f}')}/h")
        for seg in cur.get("segments") or []:
            lines.append(f"    {_when(seg['from'])}→{_when(seg['to'])} {seg['hours']:.1f} h "
                         f"{seg['max_workers']} worker(s) {seg['isolation']}: "
                         f"5h {_f(seg['five_pct_per_h'], '{:.2f}')} %/h · "
                         f"wk {_f(seg['week_pct_per_h'], '{:.2f}')} %/h · "
                         f"{seg['phases_finished']} phase(s)")
    if past:
        lines.append("")
        lines.append("past runs")
        lines.append(f"  {'run':<17} {'started':<11} {'hours':>5} {'wk':>2} {'isolation':<9} "
                     f"{'done':>4} {'fail':>4} {'5h%/h':>6} {'win':>3} {'wk%/h':>6} {'$/h':>6}")
        for s in past:
            lines.append(
                f"  {str(s.get('run_id')):<17} {_when(s['start']):<11} {s['hours']:>5.1f} "
                f"{str(s.get('max_workers') or '?'):>2} {str(s.get('isolation') or '?'):<9} "
                f"{s['phases_finished']:>4} {s['phases_failed']:>4} "
                f"{_f(s['five_pct_per_h'], '{:.2f}'):>6} {s['five_windows']:>3} "
                f"{_f(s['week_pct_per_h'], '{:.2f}'):>6} {_f(s['usd_per_h'], '{:.2f}'):>6}")
    lines.append("")
    lines.append(f"note: {SKEW_NOTE}")
    return "\n".join(lines)


def past_summaries(state_dir, limit: int) -> list[dict]:
    """Closed runs' stored summaries, newest first."""
    out = []
    for rec in runs.list_runs(state_dir):
        if rec.get("end_ts") is None or not isinstance(rec.get("summary"), dict):
            continue
        out.append(rec["summary"] | {"closed_by": rec.get("closed_by")})
        if len(out) >= limit:
            break
    return out
