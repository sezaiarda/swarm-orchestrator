"""The ledger as the board reads it: every row's full text, and where it sits.

:mod:`swarm_orchestrator.ledger` answers one question — which phase needs which —
and throws the rest of each line away, because the resolver must not care what a
row *says*. The board is the opposite reader: a card is useless without the
row's own words (its bold lead sentence is the title a person recognises), its
``[x]`` box, the repos it touches, and the section heading it was filed under,
which is where a campaign's name and intent are written down.

Rows are recognised with the resolver's own patterns, imported rather than
copied, so a line the swarm schedules is always a line the board shows and a
prose line that yields no phase there yields no card here.

Pure: text in, dataclasses out. Never raises on odd input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..ledger import _BACKTICK_RE, _CHECKBOX_RE, _FIELD_SEP, _PHASE_RE

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BOX_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
#: A row can run to pages (a long one can be dozens of lines of spec). The detail
#: sheet shows it whole up to here; past that it is a document, not a card.
MAX_ROW_CHARS = 20_000
#: The card title: one glanceable sentence, not the row.
TITLE_CHARS = 160


@dataclass(frozen=True)
class Heading:
    """A markdown heading, and the prose written directly under it."""

    level: int
    text: str
    line: int
    intro: str = ""


@dataclass
class Row:
    """One checklist row of the ledger."""

    id: str
    checked: bool
    line: int
    text: str
    title: str
    dirs: list[str] = field(default_factory=list)
    #: The headings above this row, outermost first (``#`` … ``######``).
    heads: tuple[Heading, ...] = ()


def plain(text: str) -> str:
    """Markdown emphasis and code marks stripped; whitespace collapsed."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text or "")
    text = re.sub(r"[`*_]{1,3}", "", text)
    return " ".join(text.split())


def clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _title(content: str) -> str:
    """The row's lead sentence: its first bold run, else what follows the fields.

    A ledger may write every row as ``id · dir · needs · **the problem** detail``;
    the bold run is the sentence the owner filed it under. A ledger without bold
    falls back to the last ``·`` field, which is the prose after the metadata.
    """
    m = _BOLD_RE.search(content)
    if m:
        return clip(plain(m.group(1)), TITLE_CHARS)
    parts = [p for p in content.split(_FIELD_SEP) if p.strip()]
    tail = [p for p in parts[1:] if not re.match(r"\s*\*?\s*(needs|dir|TAG):", p)]
    return clip(plain(tail[-1] if tail else ""), TITLE_CHARS)


def _dirs(content: str) -> list[str]:
    for part in content.split(_FIELD_SEP):
        if part.strip().startswith("dir:"):
            return [d for d in _BACKTICK_RE.findall(part) if d]
    return []


def parse(text: str) -> tuple[dict[str, Row], list[Heading]]:
    """``(rows by id, every heading in order)`` from ledger markdown.

    A row's text is its checklist line plus the indented continuation lines under
    it (sub-bullets, wrapped paragraphs), up to the next line that starts at the
    margin. The first row wins when an id is filed twice: that is the one a
    reader meets first, and the resolver keeps the same phase either way.
    """
    lines = (text or "").splitlines()
    rows: dict[str, Row] = {}
    headings: list[Heading] = []
    stack: list[Heading] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        hm = _HEADING_RE.match(raw)
        if hm:
            level = len(hm.group(1))
            intro = _intro(lines, i + 1)
            head = Heading(level, hm.group(2).strip(), i + 1, intro)
            headings.append(head)
            stack = [h for h in stack if h.level < level] + [head]
            i += 1
            continue
        cm = _CHECKBOX_RE.match(raw)
        ids = _BACKTICK_RE.findall(cm.group(1)) if cm else []
        if not ids or not _PHASE_RE.match(ids[0]):
            i += 1
            continue
        body = [raw]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip() and not nxt[:1].isspace():
                break
            body.append(nxt)
            j += 1
        while body and not body[-1].strip():
            body.pop()
        box = _BOX_RE.match(raw)
        phase = ids[0]
        if phase not in rows:
            rows[phase] = Row(
                id=phase,
                checked=bool(box and box.group(1) in "xX"),
                line=i + 1,
                text="\n".join(body)[:MAX_ROW_CHARS],
                title=_title(cm.group(1)),
                dirs=_dirs(cm.group(1)),
                heads=tuple(stack),
            )
        i = j
    return rows, headings


def _intro(lines: list[str], start: int, limit: int = 600) -> str:
    """The first prose paragraph under a heading, before any row or sub-heading.

    Blockquote markers are dropped (a ledger may write its section intros as
    ``>`` quotes); a paragraph ends at a blank line once it has begun.
    """
    out: list[str] = []
    for raw in lines[start : start + 40]:
        if _HEADING_RE.match(raw) or _CHECKBOX_RE.match(raw):
            break
        line = raw.strip().lstrip(">").strip()
        if not line:
            if out:
                break
            continue
        out.append(line)
    return clip(plain(" ".join(out)), limit)
