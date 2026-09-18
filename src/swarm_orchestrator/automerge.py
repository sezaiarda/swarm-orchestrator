"""Mechanical conflict resolution, tried before a resolver session is spawned.

Most merge conflicts between phases are mechanically resolvable: two phases tick
their own adjacent ledger line, or both append to a journal. Replaying such conflicts with
``git merge-tree`` shows they can be settled with no model in the loop, yet a
resolver session costs many billable tokens, mostly cache reads of the session
boot context, to splice a few ledger lines, while the merge queue sits frozen
until it finishes.

The obvious fix is the wrong one. ``merge=union`` is **incorrect** for the
umbrella ledger: both sides tick their own adjacent one-line entry, and union
keeps both whole regions, leaving the stale un-ticked line beside the ticked one.
On real conflicts it duplicated phase lines,
leaving phase ids appearing twice. The project's ``.gitattributes`` already
excludes that file from union for exactly this reason, and it is right to.

So two strategies, chosen per path:

``union``
    A proper three-way merge that keeps *both* sides at a conflicting region.
    Correct for append/prepend journals (findings, follow-ups, lessons), where
    two inserts are never a semantic conflict. Verified against real journal
    conflicts: nothing dropped, nothing duplicated.

``keyed:<regex>``
    Segment the file into records introduced by a line matching ``regex`` (group
    1 is the record's key); continuation lines attach to the record above them.
    Merge per key: whichever side differs from base wins, and only a key that
    *both* sides changed differently is a real conflict. Verified against all
    seven ledger conflicts — byte-identical to what the human resolver produced,
    zero residual conflicts.

Anything without a configured strategy, or any genuine both-sides-changed-the-
same-key case, resolves nothing and leaves the tree exactly as the failed merge
left it, so the existing resolver path can take over unchanged. Fallback, not
replacement: the one thing a session caught that no merge driver can was a file
that merged *cleanly* and was thereafter semantically false ("Only one phase has
been built", after master had already ticked another one). That is a review problem,
which is why :func:`auto_resolve` reports every file the merge touched rather
than only the ones it fixed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

CONFLICT = object()  # sentinel: this region needs a human/model


# -- three-way line merge --------------------------------------------------
def _index_map(base: list[str], side: list[str]) -> dict[int, int]:
    """base line index -> side line index, for every line the two share."""
    out: dict[int, int] = {}
    for a, b, size in SequenceMatcher(None, base, side, autojunk=False).get_matching_blocks():
        for k in range(size):
            out[a + k] = b + k
    return out


def merge3(
    base: list[str], ours: list[str], theirs: list[str], *, union: bool = False
) -> list[str] | None:
    """Three-way merge of line lists (diff3-shaped).

    A base line both sides kept is *stable*; the spans between stable lines are
    where the sides may disagree. A span only one side changed takes that change;
    a span both changed identically takes it once. A span both changed
    *differently* is a real conflict: with ``union`` it keeps ours-then-theirs
    (two inserts into a journal are not a disagreement), otherwise the merge
    fails and returns ``None`` so the caller falls back rather than guesses.

    Stability is computed from the lines the sides *share with base*, not from
    edit opcodes: a pure insertion occupies a zero-width base range, so opcode
    walking silently drops it — which is precisely the append/prepend case union
    exists to serve.
    """
    om = _index_map(base, ours)
    tm = _index_map(base, theirs)
    stable = [i for i in range(len(base)) if i in om and i in tm]

    merged: list[str] = []
    bi = oi = ti = 0
    for i in stable + [len(base)]:
        last = i == len(base)
        bo, to = (len(ours), len(theirs)) if last else (om[i], tm[i])
        if i > bi or bo > oi or to > ti:
            b_chunk, o_chunk, t_chunk = base[bi:i], ours[oi:bo], theirs[ti:to]
            if o_chunk == t_chunk:
                merged.extend(o_chunk)
            elif o_chunk == b_chunk:
                merged.extend(t_chunk)
            elif t_chunk == b_chunk:
                merged.extend(o_chunk)
            elif union:
                merged.extend(o_chunk)
                merged.extend(t_chunk)
            else:
                return None
        if not last:
            merged.append(base[i])
            bi, oi, ti = i + 1, bo + 1, to + 1
    return merged


# -- keyed record merge ----------------------------------------------------
@dataclass
class _Record:
    key: str | None
    lines: list[str] = field(default_factory=list)


def _segment(text: str, key_re: re.Pattern[str]) -> list[_Record]:
    """Split into records. Lines before the first key form a keyless preamble."""
    records: list[_Record] = [_Record(key=None)]
    for line in text.splitlines(keepends=True):
        m = key_re.search(line)
        if m:
            records.append(_Record(key=m.group(1), lines=[line]))
        else:
            records[-1].lines.append(line)
    return records


def keyed_merge(base: str, ours: str, theirs: str, pattern: str) -> str | None:
    """Merge two edits to a keyed record file, or ``None`` if genuinely conflicting.

    The ledger's failure mode is two phases ticking their own adjacent entry —
    disjoint keys landing inside git's three lines of diff context. Keyed on the
    phase id those edits do not overlap at all, so each side's change applies
    cleanly and the result is exactly what a careful human writes.

    Conservative about ordering: if the two sides disagree about the *sequence*
    of keys and neither matches base, this returns ``None`` rather than inventing
    an interleaving.
    """
    try:
        key_re = re.compile(pattern)
    except re.error:
        return None

    b_recs, o_recs, t_recs = (_segment(x, key_re) for x in (base, ours, theirs))
    b_map = {r.key: r.lines for r in b_recs if r.key is not None}
    o_map = {r.key: r.lines for r in o_recs if r.key is not None}
    t_map = {r.key: r.lines for r in t_recs if r.key is not None}
    b_keys = [r.key for r in b_recs if r.key is not None]
    o_keys = [r.key for r in o_recs if r.key is not None]
    t_keys = [r.key for r in t_recs if r.key is not None]

    if len(set(o_keys)) != len(o_keys) or len(set(t_keys)) != len(t_keys):
        return None  # duplicate keys: the key is not identifying, don't guess

    if o_keys == t_keys:
        order = o_keys
    elif o_keys == b_keys:
        order = t_keys  # only theirs reordered/added
    elif t_keys == b_keys:
        order = o_keys  # only ours did
    else:
        return None  # both changed the sequence — a real conflict

    out: list[str] = []
    # Preamble is itself three-way merged; it carries headers and prose.
    pre = merge3(
        b_recs[0].lines, o_recs[0].lines, t_recs[0].lines, union=False
    )
    if pre is None:
        return None
    out.extend(pre)

    for key in order:
        o = o_map.get(key)
        t = t_map.get(key)
        b = b_map.get(key)
        if o is not None and t is not None:
            if o == t:
                out.extend(o)
            elif b is not None and o == b:
                out.extend(t)
            elif b is not None and t == b:
                out.extend(o)
            else:
                inner = merge3(b or [], o, t, union=False)
                if inner is None:
                    return None  # both sides edited the same record differently
                out.extend(inner)
        elif o is not None:
            out.extend(o)
        elif t is not None:
            out.extend(t)
    return "".join(out)


# -- strategy dispatch -----------------------------------------------------
def strategy_for(path: str, strategies: dict[str, str]) -> str | None:
    """The configured strategy for ``path``, matched by glob. None = no strategy."""
    from fnmatch import fnmatch

    for glob, how in strategies.items():
        if fnmatch(path, glob) or path.endswith(glob):
            return how
    return None


def resolve_text(
    base: str, ours: str, theirs: str, how: str
) -> str | None:
    """Apply ``how`` (``union`` or ``keyed:<regex>``). None = could not resolve."""
    if how == "union":
        merged = merge3(
            base.splitlines(keepends=True),
            ours.splitlines(keepends=True),
            theirs.splitlines(keepends=True),
            union=True,
        )
        return None if merged is None else "".join(merged)
    if how.startswith("keyed:"):
        return keyed_merge(base, ours, theirs, how[len("keyed:") :])
    return None
