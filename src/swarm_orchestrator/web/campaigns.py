"""What each campaign *is*, in one line, found in the ledger itself.

A campaign is a phase-id prefix (:func:`tui.campaign.campaign_of`: ``coral-W3`` is
in ``coral``). The prefix is a handle, not an explanation — a reader wants to see
"what the campaign is". Nobody writes that down twice, so the extractor
looks where it already is:

1. **A heading that names the campaign.** A ledger titles each campaign's
   section with its own ids — ``### … ADR-N: A title
   (`coral-W0`…`coral-W9`)`` — so a heading above the campaign's rows that
   mentions one of its ids is the campaign's own title. The one holding most of
   its rows wins (a heading that merely *cites* a row, like "filed by
   `read-W3`", holds none of them).
2. **A neutral heading holding most of its rows.** ``### billing (shared
   library consumer)`` names no campaign, but every billing row sits under it. A
   heading that names *other* campaigns only is somebody else's section: the
   ``perf-F*`` rows filed under a heading that names only ``read-W*`` ids are not
   about reading.
3. **The ADR most of its rows cite**, titled from ``docs/adr/`` when present.
4. Otherwise nothing: the prefix stands alone rather than borrowing a line that
   would be confidently wrong.

Pure apart from the optional ADR-title lookup, which is best-effort.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from ..tui.campaign import campaign_of
from .rows import Heading, Row, clip, plain

_TICK_RE = re.compile(r"`([^`]+)`")
_ADR_RE = re.compile(r"\bADR[- ](\d{4})\b")
_PAREN_RE = re.compile(r"\s*\(([^()]*)\)")
#: ``ADR-N / ADR-M:`` — the record numbers and their separator; the
#: number moves to its own field, the words around it are the title.
_ADR_RUN_RE = re.compile(r"ADR[- ]\d{4}(?:\s*/\s*ADR[- ]\d{4})*\s*[:—-]?\s*")
#: A "what it is" line is read on a phone card; past this it is a paragraph.
WHAT_CHARS = 140


@dataclass(frozen=True)
class Meta:
    """One campaign's name, what it is, and where that came from."""

    name: str
    what: str = ""
    about: str = ""
    adr: str = ""
    #: ``heading`` | ``section`` | ``adr`` | ``""`` — which rule answered.
    source: str = ""
    #: 1-based ledger line of the heading used, for the curious.
    line: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _mentions(heading: Heading, known: set[str]) -> set[str]:
    """Campaigns whose ids a heading names (``coral-W0``, ``read-W*``, ``a`…`b``).

    Only real campaigns count: a heading citing a path or a git tag in backticks
    (``docs/design/smart-cache/canonical.md``) names no campaign at all.
    """
    out: set[str] = set()
    for token in _TICK_RE.findall(heading.text):
        for part in re.split(r"[…,\s]+", token):
            part = part.strip().rstrip("*")
            # `read-W*` loses its star to `read-W`, which is no phase id; a
            # digit makes it one without changing the campaign it names.
            for guess in (campaign_of(part), campaign_of(part + "0")):
                if guess in known:
                    out.add(guess)
                    break
    return out


def clean(heading: str, name: str) -> tuple[str, str]:
    """``(what, adr)`` from a heading's text.

    Drops the parenthetical that lists the campaign's ids (the reader is already
    looking at them), lifts a leading ``ADR-NNNN`` into its own field, and keeps
    any other parenthetical only when stripping it would leave next to nothing —
    ``payments (own shape-parity structs …)`` says what it is only in brackets.
    """
    text = heading.strip()
    m = _ADR_RE.search(text)
    adr = f"ADR-{m.group(1)}" if m else ""
    text = re.sub(r"^`[^`]*`\s*[—:-]\s*", "", text)  # "`read-W*` — make …"
    text = _ADR_RUN_RE.sub("", text)
    bare = plain(_PAREN_RE.sub("", text)).strip(" —:-")
    rest = bare
    if len(bare.replace(name, "").strip(" —:-")) < 12:
        inner = [p for p in _PAREN_RE.findall(text) if not _TICK_RE.search(p)]
        rest = plain(" — ".join([bare, *inner])).strip(" —:-")
    return clip(rest, WHAT_CHARS), adr


def describe(rows: dict[str, Row], adr_titles: dict[str, str] | None = None) -> dict[str, Meta]:
    """``{campaign: Meta}`` for every campaign with a row in the ledger."""
    adr_titles = adr_titles or {}
    members: dict[str, list[Row]] = {}
    for row in rows.values():
        members.setdefault(campaign_of(row.id), []).append(row)
    known = set(members)
    return {name: _one(name, mine, known, adr_titles) for name, mine in members.items()}


def _one(name: str, mine: list[Row], known: set[str], adr_titles: dict[str, str]) -> Meta:
    held: Counter = Counter()
    by_line: dict[int, Heading] = {}
    for row in mine:
        for head in row.heads:
            if head.level <= 1:
                continue  # the document title is everybody's, so nobody's
            held[head.line] += 1
            by_line[head.line] = head

    def best(pred) -> Heading | None:
        cands = [by_line[ln] for ln in held if pred(by_line[ln])]
        # The most specific heading that still holds most of the campaign: an
        # owner report's h2 holds every row of the three campaigns it spawned,
        # but the h3 holding 13 of `list`'s 19 is the one that is about `list`.
        major = [h for h in cands if held[h.line] * 2 >= len(mine)]
        if major:
            return max(major, key=lambda h: (h.level, held[h.line], -h.line))
        return max(cands, key=lambda h: (held[h.line], h.level, -h.line), default=None)

    own = best(lambda h: name in _mentions(h, known))
    if own is not None:
        return _from_heading(name, own, "heading")
    neutral = best(lambda h: not _mentions(h, known))
    if neutral is not None and held[neutral.line] * 2 >= len(mine):
        return _from_heading(name, neutral, "section")
    cited = Counter(f"ADR-{n}" for row in mine for n in set(_ADR_RE.findall(row.text)))
    if cited:
        adr, hits = cited.most_common(1)[0]
        if hits * 2 >= len(mine):
            return Meta(name, what=clip(adr_titles.get(adr, ""), WHAT_CHARS), adr=adr,
                        source="adr")
    return Meta(name)


def _from_heading(name: str, head: Heading, source: str) -> Meta:
    what, adr = clean(head.text, name)
    return Meta(name, what=what, about=head.intro, adr=adr, source=source, line=head.line)


def adr_titles(adr_dir: Path) -> dict[str, str]:
    """``{"ADR-N": "A title"}`` from ``NNNN-*.md`` files.

    Best-effort: the first ``#`` heading of each record, minus its own number.
    A project without ADRs (or a dir that will not list) yields ``{}``.
    """
    out: dict[str, str] = {}
    try:
        paths = sorted(Path(adr_dir).glob("[0-9][0-9][0-9][0-9]-*.md"))
    except OSError:
        return out
    for path in paths:
        title = ""
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for _ in range(20):
                    line = fh.readline()
                    if not line:
                        break
                    if line.startswith("# "):
                        title = line[2:].strip()
                        break
        except OSError:
            continue
        title = re.sub(r"^(?:ADR[- ]?)?\d{1,4}\s*[.:—-]?\s*", "", title)
        out.setdefault(f"ADR-{path.name[:4]}", plain(title) or path.stem[5:].replace("-", " "))
    return out
