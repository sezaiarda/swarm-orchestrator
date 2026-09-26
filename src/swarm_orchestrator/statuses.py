"""The `swarm done` status vocabulary, in one place instead of ten.

The statuses a worker can finish with were written out as ~10 separate literal
tuples — in :mod:`ledger`, :mod:`launch`, :mod:`gitq`, :mod:`report`,
:mod:`doctor`, :mod:`recap`, :mod:`cli` and twice in the dashboard — byte-
identical copies held together by nothing but grep. That is survivable while the
vocabulary never changes and lethal the moment it does, because every omission
fails *silently*: a status missing from ``gitq.DONE_INTEGRATE`` makes the
supervisor roll back the branch of a phase that **succeeded**, and one missing
from the sentinel-suffix set makes the next ``swarm up`` read a finished phase as
interrupted and discard its work. No exception, no log line — just deleted work.

So the vocabulary gets exactly one home, and it is a stdlib-only leaf that
imports nothing from the package: everything above may import it, including the
Textual-free ``tui`` data modules, with no cycle to reason about.

Six sets, not one, because there are six genuinely different questions — does it
merge, does it release dependents, does it carry a brief for the owner, does it
ping, did it owe a ping, does it open an operator session. Collapsing any two
because they happen to hold the same members today is how the ten copies started.
"""

from __future__ import annotations

OK = "ok"
OPERATOR = "operator"
FAIL = "fail"
SKIP = "skip"
#: Retired in favour of ``operator``. Every *read* path still needs
#: it — live sentinels on disk say ``needs-owner`` and always will — so it is in
#: :data:`ALL` and every membership set it belongs to; it is out of
#: :data:`WRITABLE` only because that is the set ``swarm done`` advertises.
NEEDS_OWNER = "needs-owner"
#: Not a ``swarm done`` status and never written anywhere: the in-memory reading
#: of a ledger row ticked ``[x]`` that the swarm holds no record of (built before
#: the swarm, or by hand). :func:`ledger.with_ticked` adds it to a *view* of the
#: done map so the launcher treats the row as landed, exactly as the web board
#: shows it; a real record, a ``fail`` included, always wins over it.
LEDGER = "ledger"

#: Two more ways to say "nothing lands", told apart only in the ledger and the
#: phase history: ``blocked`` (something outside the phase is missing) and
#: ``later`` (it cannot be done before a date; ``--after`` gives the date, and
#: the row waits for it). The swarm records and handles both as ``fail``.
BLOCKED = "blocked"
LATER = "later"

#: The statuses ``swarm done`` advertises as an argument.
WRITABLE = (OK, OPERATOR, FAIL, BLOCKED, LATER)
#: Deprecated input spelling -> the canonical one it means.
RETIRED = {NEEDS_OWNER: OPERATOR}
#: Outcome words the swarm handles as another status.
ALIASES = {BLOCKED: FAIL, LATER: FAIL}
#: What argparse accepts: what we advertise, plus what we still honour quietly.
ACCEPTED = WRITABLE + tuple(RETIRED)
#: Every status that can exist on disk, including the retired spelling and
#: ``skip`` (which the owner writes, not a worker).
ALL = (OK, OPERATOR, NEEDS_OWNER, FAIL, SKIP)
#: :func:`recap.sentinel` probes ``done/<phase>.<status>`` in this order and the
#: first file that opens wins, so integrating statuses must come first: a
#: completed build outranks a ``fail``/``skip`` left by an earlier attempt.
PRECEDENCE = ALL

#: Merge into main rather than roll back. ``operator``/``needs-owner`` land
#: exactly like ``ok`` — the work succeeded. They differ only in what happens
#: after the merge: ``needs-owner`` told the owner, ``operator`` opens a session.
INTEGRATES = frozenset({OK, OPERATOR, NEEDS_OWNER})
#: Statuses that SATISFY a dependent's ``needs:``. ``skip`` counts — the owner
#: declared the phase unnecessary, which releases its dependents by definition.
#: So does ``ledger``: the owner's ledger says the row is built.
#: ``fail`` does NOT: its branch was discarded, so anything built on it would be
#: built on a main that lacks it.
SATISFIES_DEPS = INTEGRATES | {SKIP, LEDGER}
#: The sentinel note is a brief for the owner, not a sign-off.
CARRIES_ACTION = frozenset({OPERATOR, NEEDS_OWNER})

# PINGS and OWED_PING are deliberately NOT the same set, and must not be merged.
# PINGS is future tense — "will finishing like this telegram the owner?" — and
# ``operator`` answers no, because it opens a session instead. OWED_PING is past
# tense: "did this outcome, as recorded, owe one?", asked of sentinels already on
# disk, where older ``needs-owner`` finishes genuinely did. One set would
# either re-flag those forever as `owner-never-pinged` or erase that history.
#: Who telegrams the owner NOW.
PINGS = frozenset({FAIL})
#: Who owed the owner a telegram THEN, per the status on disk.
OWED_PING = frozenset({FAIL, NEEDS_OWNER})
#: Who dispatches an operator session.
ROUTES = frozenset({OPERATOR})


def canonical(status: str) -> str:
    """The current spelling of *status*; a retired one maps forward, else itself."""
    return RETIRED.get(status, ALIASES.get(status, status))
