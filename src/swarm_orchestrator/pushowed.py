"""Owed pushes: a repo whose main merged locally but has not reached origin.

A push failure used to hold the whole merge queue. It never needed to: workers
branch from local main, so a phase whose merges all landed locally is done as far
as every later phase is concerned — only origin is behind. If a repo's
pre-push gate refused one push and the hold kept every later finished phase
queued, and every slot busy for a long stretch.

So a failed push is now a debt on the repo (``State.push_owed``), not a hold on
the queue. It is retried after every integration and on the watchdog tick, and
cleared the moment origin has local main — by our retry, by a later phase's push
of the same repo (which carries the earlier commits too), or by the owner's hand.
Nothing is lost and nothing waits while a push is owed, so the owner is not
asked: the debt is recorded for the Overseer's next summary when it opens (most
clear on the next integration), and the Overseer gets a pass for one its repo's
own check refused or that outlives a short grace (:mod:`overseer`); it asks the
owner if the fix is theirs. A retry that fails again says nothing;
``swarm doctor`` and ``swarm status`` show the standing debt with its age and
reason.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import gitq
from . import state as state_mod
from . import telegram
from .config import Config
from .logutil import Log

#: The watchdog's retry spacing. A retry runs the repo's pre-push hook, which on
#: a project can mean a build lock and a typecheck, inside the supervisor's one
#: loop — so the idle-tick retry is spaced out. The retry after an integration is
#: not: that is when new commits land and a fix is most likely to be in.
TICK_RETRY_S = 900.0


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _since(rec: dict, now: float) -> float:
    since = rec.get("since")
    return now if since is None else float(since)


def owed_message(name: str, phase: str, reason: str, refused: bool) -> str:
    """What the log keeps when a repo starts owing a push."""
    what = "push refused by its pre-push check" if refused else "push failed"
    return (
        f"{name} {what} after merging {phase} — {reason}. Nothing is lost: work keeps"
        " merging on this machine and the push is retried after each merge."
    )


def settle(
    cfg: Config, phase: str | None, pushes: dict[Path, gitq.PushResult], log: Log
) -> None:
    """Fold a batch of push outcomes into ``push_owed``, and record each change.

    A success clears the repo's debt; a failure records one only if the repo was
    not already owing (the first phase and the first ``since`` are what the owner
    needs; the reason is refreshed, since a fixed check can fail differently).
    ``phase=None`` (a bare retry) never creates a debt — only clears or refreshes.
    """
    if not pushes:
        return
    now = time.time()
    opened: list[tuple[Path, dict]] = []
    closed: list[tuple[Path, dict]] = []
    with state_mod.transaction(cfg) as st:
        for repo, res in pushes.items():
            key = str(repo)
            rec = st.push_owed.get(key)
            if res.status == gitq.MERGED:
                if rec is not None:
                    closed.append((repo, st.push_owed.pop(key)))
            elif rec is not None:
                rec.update(reason=res.reason, refused=res.refused, tried=now)
            elif phase is not None:
                rec = {
                    "phase": phase,
                    "reason": res.reason,
                    "since": now,
                    "refused": res.refused,
                    "tried": now,
                }
                st.push_owed[key] = rec
                opened.append((repo, rec))
    for repo, rec in opened:
        log.line(f"PUSH-OWED {repo.name} phase={rec['phase']} {rec['reason']}")
        telegram.fold(
            cfg,
            owed_message(repo.name, rec["phase"], rec["reason"], rec["refused"]),
            kind="push-owed",
            phase=rec["phase"],
            source="pushowed.settle",
        )
    for repo, rec in closed:
        age = _age(now - _since(rec, now))
        log.line(f"PUSH-OWED-CLEARED {repo.name} phase={rec.get('phase')} after={age}")
        telegram.log(
            cfg,
            f"{repo.name} is pushed — origin has everything since"
            f" {rec.get('phase')} (owed {age}).",
            why="a push that was owed went through; nothing to do",
            kind="push-owed",
            phase=rec.get("phase"),
            source="pushowed.settle",
        )


def retry(
    cfg: Config, log: Log, *, skip: set[Path] | None = None, min_gap: float = 0.0
) -> None:
    """Retry every owed push not in ``skip``, then :func:`settle` the outcomes.

    ``skip`` is the repos an integration just pushed (successfully or not) — one
    hook run per repo per integration is enough. ``min_gap`` spaces retries of
    the same repo by its last attempt; a debt not yet due is still cleared when
    the clone's own refs say origin has everything (:func:`gitq.origin_has`: the
    owner pushed by hand), which costs no fetch and no hook run. Never raises:
    :func:`gitq.retry_push` turns every git failure into a result.
    """
    owed = state_mod.read(cfg).push_owed
    if not owed:
        return
    skipped = {str(p) for p in (skip or set())}
    now = time.time()
    results: dict[Path, gitq.PushResult] = {}
    for key, rec in owed.items():
        if key in skipped:
            continue
        if min_gap and now - float(rec.get("tried") or 0.0) < min_gap:
            if gitq.origin_has(cfg, Path(key)):
                results[Path(key)] = gitq.PushResult(gitq.MERGED)
            continue
        results[Path(key)] = gitq.retry_push(cfg, Path(key), log)
    settle(cfg, None, results, log)


def describe(owed: dict[str, dict], now: float | None = None) -> list[str]:
    """One line per owed repo: name, phase, age, reason (for status/doctor)."""
    now = time.time() if now is None else now
    out = []
    for key in sorted(owed):
        rec = owed[key]
        age = _age(now - _since(rec, now))
        out.append(
            f"{Path(key).name} (since {rec.get('phase')}, {age}): {rec.get('reason')}"
        )
    return out
