"""The swarm writes the ledger, the phase history and the lessons file; sessions report.

Sessions used to edit these files themselves: a worker ticked its row and
appended a dated note to it, and operator, ask and Overseer sessions appended
theirs. Each did it on its own branch, so the merge queue met the same row
edited twice, so ledger rows were a steady source of merge conflicts, and
single rows grew to pages of notes nobody could read.

Now a session reports through a command (``swarm done``, ``swarm record``,
``swarm follow-up``, ``swarm lesson``). The report is queued under the state
dir, and the supervisor applies it on the target branch in the project
checkout, under the umbrella repo's lock, and commits it itself
(:func:`gitq.commit_to_target`). A worker's report is applied when its phase
lands: after the merge succeeds for an outcome that integrates, right away for
one that does not. Nothing is lost if the swarm stops in between: the queue is
on disk and the next start applies it.

The ledger keeps state only: box, id, dir, needs, a short bold title, tags, a
date gate (``after:``) and a short status. What was written about a phase goes
to the history of its family, the id's prefix (``read-W12`` -> ``read``):
``<history>/read.md``, which becomes a directory with one file per phase
(``<history>/read/read-W12.md``) once it grows past ``history_split_kb``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from . import gitq
from . import ledger as ledger_mod
from . import notes as notes_mod
from . import pushowed
from . import statuses
from .config import Config
from .logutil import Log

#: The queue key for reports that wait on no phase: ``swarm record`` and a
#: follow-up or lesson filed by a session that is not the phase's own worker.
NOW = "_now"

#: What ``swarm record`` accepts, for sessions that are not a phase's worker
#: (ask, operator, Overseer). ``done`` ticks the row; ``note`` changes no state.
RECORD_OUTCOMES = ("done", "failed", "blocked", "later", "note")

#: How a phase outcome reads in the ledger's status and the history heading.
_WORDS = {
    statuses.OK: "done",
    statuses.OPERATOR: "done, operator follow-up",
    statuses.NEEDS_OWNER: "done, owner follow-up",
    statuses.FAIL: "failed",
    statuses.BLOCKED: "blocked",
    statuses.LATER: "later",
    "done": "done",
    "failed": "failed",
    "blocked": "blocked",
    "later": "later",
    "note": "note",
}
#: Outcome words that tick the row.
_TICKS = {"done", "done, operator follow-up", "done, owner follow-up"}

TITLE_CHARS = 120
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ROW = re.compile(r"^(\s*[-*]\s+\[)([ xX])(\]\s+)(.+)$")
_FIELD_SEP = ledger_mod._FIELD_SEP
_META = re.compile(r"^\s*\**\s*(dir:|needs:)|^\s*(owner-run|owner-optional)\s*$")
_NEEDS = re.compile(r"^\s*\**\s*needs:")
_TAG = re.compile(r"^\s*\**\s*TAG:")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_SECTION = re.compile(r"^## `([^`]+)`")


class ReportError(ValueError):
    """A report the swarm refuses: bad id, unknown need, a cycle, a bad date."""


# -- the ledger text ------------------------------------------------------
def row_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    """``{id: (first, end)}`` line ranges of every checklist row, end exclusive.

    A row is its checklist line plus the blank and indented lines under it, up to
    the next line at the margin, with trailing blanks left out; the web board
    reads rows the same way. The first row wins when an id is filed twice.
    """
    spans: dict[str, tuple[int, int]] = {}
    i = 0
    while i < len(lines):
        m = _ROW.match(lines[i])
        ids = ledger_mod._BACKTICK_RE.findall(m.group(4)) if m else []
        if not ids or not ledger_mod._PHASE_RE.match(ids[0]):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or lines[j][:1].isspace()):
            j += 1
        end = j
        while end > i + 1 and not lines[end - 1].strip():
            end -= 1
        spans.setdefault(ids[0], (i, end))
        i = j
    return spans


def plain(text: str) -> str:
    """Markdown emphasis, code marks and links stripped; whitespace collapsed."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text or "")
    text = re.sub(r"[`*_]{1,3}", "", text)
    return " ".join(text.split())


def clip(text: str, limit: int = TITLE_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _drop_asides(text: str) -> str:
    """``text`` without the parenthetical notes it opens with (``*(ordering only: …)*``)."""
    while True:
        m = re.match(r"^[\s*_]*\(", text)
        if not m:
            return text
        depth, i = 0, m.end() - 1
        for i in range(m.end() - 1, len(text)):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            if depth == 0:
                break
        else:
            return ""
        text = re.sub(r"^[*_]*", "", text[i + 1:]).lstrip(" .;:—-")


def _title_of(prose: list[str]) -> str:
    """A row's short title: the bold lead of its first descriptive field, else
    that field's first sentence. A parenthetical note (``*(ordering only: …)*``)
    is history, not a title."""
    for field_ in prose:
        text = _drop_asides(field_.strip())
        if not plain(text):
            continue
        m = _BOLD.match(text)
        if m:
            return clip(plain(m.group(1)))
        flat = plain(text)
        cut = re.split(r"(?<=[.;:!?])\s|\s[—(]", flat, maxsplit=1)[0]
        return clip(cut or flat)
    return ""


@dataclass
class Head:
    """A checklist line's fields, in order, with the swarm's own two set apart."""

    box: str
    id_field: str
    fields: list[str]
    after: str = ""
    status: str = ""

    @property
    def meta(self) -> list[str]:
        """``dir:``, the owner marker and ``needs:``: what the gates read."""
        return [f for f in self.fields if _META.match(f)]

    @property
    def tags(self) -> list[str]:
        """Each ``TAG:`` field, cut to the tag itself."""
        return [_tag_split(f)[0] for f in self.fields if _TAG.match(f)]

    @property
    def prose(self) -> list[str]:
        """Everything else: what the row says, which goes to the history."""
        out = []
        for f in self.fields:
            if _TAG.match(f):
                rest = _tag_split(f)[1]
                if rest:
                    out.append(rest)
            elif not _META.match(f):
                out.append(f)
        return out

    def line(self, title: str) -> str:
        """The row cut to what the swarm keeps, with ``title`` as its prose."""
        fields = [self.id_field, *self.meta]
        if title:
            fields.append(f"**{title}**")
        return self._join(fields + self.tags)

    def rebuild(self) -> str:
        """The row again, every field where it was: only the swarm's own move."""
        return self._join([self.id_field, *self.fields])

    def _join(self, fields: list[str]) -> str:
        if self.after:
            fields.append(f"after:`{self.after}`")
        if self.status:
            fields.append(f"status: {self.status}")
        return f"- [{self.box}] " + _FIELD_SEP.join(fields)


def _tag_split(field_: str) -> tuple[str, str]:
    """``(the tag, any prose written after it in the same field)``."""
    m = re.match(r"^\s*(\*\*TAG:.*?\*\*|TAG:`[^`]*`)", field_)
    if not m:
        return field_, ""
    return m.group(0), field_[m.end():].strip()


def split_head(line: str) -> Head | None:
    """The fields of one checklist line, or None when it is not a row."""
    m = _ROW.match(line)
    if not m:
        return None
    parts = m.group(4).split(_FIELD_SEP)
    head = Head(box=m.group(2), id_field=parts[0], fields=[])
    for part in parts[1:]:
        if part.startswith("after:"):
            head.after = "".join(ledger_mod._BACKTICK_RE.findall(part)) or part[6:].strip()
        elif part.startswith("status:"):
            head.status = part[7:].strip()
        else:
            head.fields.append(part)
    return head


def _edit_row(text: str, phase: str, edit) -> str:
    """``text`` with ``edit(Head) -> str`` applied to ``phase``'s checklist line."""
    lines = text.split("\n")
    span = row_spans(lines).get(phase)
    if span is None:
        raise ReportError(f"no ledger row {phase}")
    head = split_head(lines[span[0]])
    lines[span[0]] = edit(head)
    return "\n".join(lines)


def set_state(text: str, phase: str, *, tick: bool = False, status: str | None = None,
              after: str | None = None) -> str:
    """Tick ``phase``'s row and/or set its short status and date gate."""
    def edit(head: Head) -> str:
        if tick:
            head.box = "x"
        if status is not None:
            head.status = status
        if after is not None:
            head.after = after
        return head.rebuild()
    return _edit_row(text, phase, edit)


def _needs_tokens(head: Head) -> list[str]:
    for part in head.fields:
        if _NEEDS.match(part):
            return ledger_mod._BACKTICK_RE.findall(part)
    return []


def carry_needs(text: str, phase: str) -> tuple[str, list[str]]:
    """Keep the order a row imposed once it is ticked.

    A ``[x]`` row satisfies its dependents, so ticking ``phase`` releases an open
    row B that needed it even when ``phase``'s own open needs are not built:
    the chain B -> phase -> A breaks and B may run beside A. Where the ledger's
    ``needs:`` is a build order (two rows of one repo never eligible together),
    that is a real break. So each open dependent gains ``phase``'s open needs,
    first in its list, where a reader that stops at a comma still sees them.
    Returns the new text and the ids that changed.
    """
    lines = text.split("\n")
    spans = row_spans(lines)
    heads = {p: split_head(lines[s]) for p, (s, _e) in spans.items()}
    open_ids = {p for p, h in heads.items() if h.box == " "}
    carried = [n for n in _needs_tokens(heads[phase]) if n in open_ids and n != phase] \
        if phase in heads else []
    changed: list[str] = []
    if not carried:
        return text, changed
    for pid, head in heads.items():
        if pid == phase or head.box != " ":
            continue
        have = _needs_tokens(head)
        if phase not in have:
            continue
        add = [n for n in carried if n not in have and n != pid]
        if not add:
            continue
        for k, part in enumerate(head.fields):
            if _NEEDS.match(part):
                at = part.index("needs:") + len("needs:")
                head.fields[k] = part[:at] + " ".join(f"`{n}`" for n in add) + " " + part[at:]
                break
        lines[spans[pid][0]] = head.rebuild()
        changed.append(pid)
    return "\n".join(lines), changed


def make_row(phase: str, title: str, needs: list[str], dirs: list[str], tags: list[str]) -> str:
    """A new open row in the ledger's own shape (fields in the order its gates read)."""
    fields = [f"`{phase}`"]
    if dirs:
        fields.append("dir:" + "+".join(f"`{d}`" for d in dirs))
    fields.append("needs:" + (" ".join(f"`{n}`" for n in needs) if needs else "—"))
    fields.append(f"**{clip(plain(title))}**")
    fields += [f"**TAG:`{t}`**" for t in tags]
    return "- [ ] " + _FIELD_SEP.join(fields)


def insert_row(text: str, anchor: str, row: str) -> str:
    """``row`` placed after the run of rows that holds ``anchor`` (its section),
    or at the end when ``anchor`` has no row."""
    lines = text.split("\n")
    spans = row_spans(lines)
    if anchor not in spans:
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines + [row, ""])
    end = spans[anchor][1]
    starts = {s: e for s, e in spans.values()}
    while True:
        nxt = end
        while nxt < len(lines) and not lines[nxt].strip():
            nxt += 1
        if nxt in starts and nxt == end:
            end = starts[nxt]
            continue
        break
    lines.insert(end, row)
    return "\n".join(lines)


# -- history files -------------------------------------------------------
def family(phase: str) -> str:
    """A phase's family: its id up to the first ``-`` (``read-W12`` -> ``read``)."""
    return phase.split("-", 1)[0] or phase


def history_path(root: Path, history: str, phase: str) -> Path:
    """Where ``phase``'s history lives: its own file once the family is split."""
    base = root / history
    fam = family(phase)
    if (base / fam).is_dir():
        return base / fam / f"{phase}.md"
    return base / f"{fam}.md"


def _family_header(fam: str) -> str:
    return (f"# `{fam}` phases — history\n\nWhat was written about the `{fam}-*` phases, "
            f"oldest first. The ledger keeps their state; this file keeps the record.\n")


def _safe(body: str) -> str:
    """A note's lines, with any that would read as a heading escaped."""
    return "\n".join("\\" + ln if ln.startswith("#") else ln for ln in body.strip().split("\n"))


def entry(heading: str, body: str) -> str:
    return f"### {heading}\n\n{_safe(body)}\n" if body.strip() else f"### {heading}\n"


def _sections(text: str) -> list[tuple[str | None, list[str]]]:
    """``[(id or None for the preamble, lines)]`` of a family file."""
    out: list[tuple[str | None, list[str]]] = [(None, [])]
    for ln in text.split("\n"):
        m = _SECTION.match(ln)
        if m:
            out.append((m.group(1), [ln]))
        else:
            out[-1][1].append(ln)
    return out


def append_history(root: Path, history: str, split_kb: int, phase: str,
                   block: str, title: str = "") -> None:
    """Append ``block`` (one or more entries) to ``phase``'s history."""
    path = history_path(root, history, phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    heading = f"`{phase}`" + (f" — {title}" if title else "")
    block = block.rstrip("\n") + "\n"
    if path.name == f"{phase}.md":
        text = path.read_text(encoding="utf-8") if path.is_file() else f"# {heading}\n"
        path.write_text(text.rstrip("\n") + "\n\n" + block, encoding="utf-8")
        return
    text = path.read_text(encoding="utf-8") if path.is_file() else _family_header(family(phase))
    parts = _sections(text)
    for i, (pid, body) in enumerate(parts):
        if pid == phase:
            while body and not body[-1].strip():
                body.pop()
            parts[i] = (pid, body + [""] + block.rstrip("\n").split("\n") + [""])
            break
    else:
        last = parts[-1][1]
        while last and not last[-1].strip():
            last.pop()
        last.append("")
        parts.append((phase, [f"## {heading}", ""] + block.rstrip("\n").split("\n") + [""]))
    out = "\n".join(ln for _pid, body in parts for ln in body).rstrip("\n") + "\n"
    path.write_text(out, encoding="utf-8")
    if split_kb > 0 and len(out.encode("utf-8")) > split_kb * 1024:
        split_family(path)


def split_family(path: Path) -> list[Path]:
    """Turn ``<history>/<fam>.md`` into ``<history>/<fam>/<id>.md``, one per phase."""
    fam_dir = path.with_suffix("")
    fam_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for pid, body in _sections(path.read_text(encoding="utf-8")):
        if pid is None:
            continue
        body = list(body)
        body[0] = "# " + body[0][3:]
        dest = fam_dir / f"{pid}.md"
        dest.write_text("\n".join(body).rstrip("\n") + "\n", encoding="utf-8")
        written.append(dest)
    path.unlink()
    return written


def history_text(root: Path, history: str, phase: str) -> str:
    """``phase``'s history as written, or "" when it has none."""
    path = history_path(root, history, phase)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if path.name == f"{phase}.md":
        return text
    for pid, body in _sections(text):
        if pid == phase:
            return "\n".join(body).strip() + "\n"
    return ""


def append_lesson(root: Path, lessons: str, phase: str, text: str, title: str, day: str) -> None:
    """A lesson, as the lessons file's own dated section: ``## (date, `id`) title``."""
    path = root / lessons
    path.parent.mkdir(parents=True, exist_ok=True)
    head = title.strip() or clip(re.split(r"(?<=[.!?])\s", " ".join(text.split()), 1)[0], 100)
    prior = path.read_text(encoding="utf-8") if path.is_file() else "# Lessons\n"
    path.write_text(prior.rstrip("\n") + f"\n\n## ({day}, `{phase}`) {head}\n\n{_safe(text)}\n",
                    encoding="utf-8")


# -- the queue ------------------------------------------------------------
def _qdir(cfg: Config) -> Path:
    return cfg.state_dir / "ledger"


@contextmanager
def _locked(cfg: Config) -> Iterator[None]:
    d = _qdir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    with (d / ".lock").open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"outcome": None, "ops": []}
    return {"outcome": data.get("outcome"), "ops": list(data.get("ops") or [])}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def queue(cfg: Config, key: str, op: dict) -> None:
    """Add one report to ``key``'s queue. An ``outcome`` replaces the one before."""
    with _locked(cfg):
        path = _qdir(cfg) / f"{key}.json"
        data = _read(path)
        op = {**op, "ts": time.time()}
        if op.get("kind") == "outcome":
            data["outcome"] = op
        else:
            data["ops"].append(op)
        _write(path, data)


def pending(cfg: Config) -> dict[str, dict]:
    """Every queued report, by key."""
    d = _qdir(cfg)
    if not d.is_dir():
        return {}
    return {p.stem: _read(p) for p in sorted(d.glob("*.json")) if not p.name.startswith(".")}


def key_for(phase: str) -> str:
    """Queue a report under its phase when the caller is that phase's worker
    (it lands with the phase), else under :data:`NOW` (it lands at once)."""
    return phase if os.environ.get("SWARM_SESSION_ID") == f"worker:{phase}" else NOW


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def check_date(day: str) -> str:
    if not _DATE.match(day or ""):
        raise ReportError(f"not a date: {day!r} (want YYYY-MM-DD)")
    return day


def _ledger_text(cfg: Config) -> str:
    try:
        return (cfg.project_dir / cfg.ledger).read_text(encoding="utf-8")
    except OSError:
        return ""


def check_row(text: str, phase: str, needs: list[str], taken: set[str] = frozenset()) -> None:
    """Refuse a new row whose id is taken, whose needs name no row, or that
    would make a dependency problem the ledger does not already have."""
    if not ledger_mod._PHASE_RE.match(phase or ""):
        raise ReportError(f"not a phase id: {phase!r}")
    graph = ledger_mod.parse(text)
    if phase in graph or phase in taken:
        raise ReportError(f"{phase} already has a ledger row")
    unknown = [n for n in needs if n not in graph and n not in taken]
    if unknown:
        raise ReportError(f"needs names no ledger row: {', '.join(unknown)}")
    before = set(ledger_mod.validate(graph))
    # Parsed with the row in place: an existing row that already names the new
    # id in its needs gains that edge only now.
    after = ledger_mod.parse(text.rstrip("\n") + "\n" + make_row(phase, "-", needs, [], []) + "\n")
    new = [i for i in ledger_mod.validate(after) if i not in before]
    if new:
        raise ReportError("; ".join(new))


def file_follow_up(cfg: Config, by: str, phase: str, title: str, needs: list[str],
                   dirs: list[str], tags: list[str], scope: str) -> str:
    """Validate and queue a follow-up row filed from ``by``; return its queue key."""
    if not title.strip():
        raise ReportError("--title is required: the row's one-line title")
    taken = {op["id"] for data in pending(cfg).values() for op in data["ops"]
             if op.get("kind") == "row"}
    text = _ledger_text(cfg)
    check_row(text, phase, needs, taken)
    key = key_for(by)
    queue(cfg, key, {"kind": "row", "by": by, "id": phase, "title": title.strip(),
                     "needs": needs, "dirs": dirs, "tags": tags, "scope": scope.strip()})
    return key


# -- applying -------------------------------------------------------------
@dataclass
class Applied:
    """What one flush changed, for the log and the caller's refill."""

    touched: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    released: bool = False


def _run_gate(cfg: Config, root: Path) -> str:
    """The project's ledger gate over the ledger as written now; "" when it passes."""
    cmd = getattr(cfg, "ledger_gate", "") or ""
    if not cmd.strip():
        return ""
    try:
        proc = subprocess.run(shlex.split(cmd), cwd=str(root), capture_output=True, text=True,
                              timeout=120, env={**os.environ, "SWARM_LEDGER": str(root / cfg.ledger)})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"the ledger gate did not run: {exc}"
    if proc.returncode == 0:
        return ""
    out = (proc.stdout + proc.stderr).strip().splitlines()
    return " ".join(out[:3])[:300] or f"the ledger gate exited {proc.returncode}"


def _decisions(cfg: Config, phase: str, since: float) -> str:
    rows = [n for n in notes_mod.load(cfg, phase) if n.ts > since]
    return "\n".join(f"- {n.kind.replace('_', ' ')}: {' '.join(n.text.split())}" for n in rows)


def apply(cfg: Config, root: Path, key: str, data: dict, status: str | None,
          flushed: dict[str, float]) -> Applied:
    """Apply one key's queued reports to the files under ``root``.

    ``status`` is what the swarm recorded for the phase (only an outcome that
    integrates ticks its row; a ``fail`` never does, whatever the report says).
    """
    res = Applied()
    ledger_path = root / cfg.ledger
    text = ledger_path.read_text(encoding="utf-8")
    hist, split_kb = cfg.history_dir, cfg.history_split_kb
    day = today()
    stamp = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())

    def state(phase: str, word: str, after: str = "") -> None:
        nonlocal text
        tick = word in _TICKS
        status_text = f"later, after {after}" if word == "later" else f"{word} ({day})"
        text = set_state(text, phase, tick=tick, status=status_text,
                         after=after if word == "later" else ("" if tick else None))
        if tick:
            text, _ = carry_needs(text, phase)
            res.released = True

    out = data.get("outcome")
    if out and key != NOW:
        word = _WORDS.get(out.get("outcome", ""), "failed")
        if status is not None and status not in statuses.INTEGRATES and word in _TICKS:
            word = "failed"
        after = out.get("after", "") if word == "later" else ""
        if word == "later" and not _DATE.match(after):
            word = "blocked"  # a `later` with no date waits for nobody
        if key in ledger_mod.parse(text):
            state(key, word, after)
        body = out.get("note", "")
        dec = _decisions(cfg, key, flushed.get(key, 0.0))
        if dec:
            body = (body + "\n\nDecided on the way:\n" + dec).strip()
        head = f"{stamp} · {word}" + (f" until {after}" if after else "")
        append_history(root, hist, split_kb, key, entry(head, body))
        res.touched.append(f"{key} {word}")
    for op in data.get("ops", []):
        kind = op.get("kind")
        if kind == "record":
            phase, word = op["phase"], _WORDS.get(op.get("outcome", ""), "note")
            after = op.get("after", "") if word == "later" else ""
            if word != "note" and phase in ledger_mod.parse(text):
                state(phase, word, after)
            who = op.get("by") or "a session"
            append_history(root, hist, split_kb, phase,
                           entry(f"{stamp} · {word} · by {who}", op.get("note", "")))
            res.touched.append(f"{phase} {word}")
        elif kind == "row":
            before = text
            try:
                check_row(text, op["id"], op.get("needs", []))
                row = make_row(op["id"], op["title"], op.get("needs", []),
                               op.get("dirs", []), op.get("tags", []))
                text = insert_row(text, op.get("by", ""), row)
                ledger_path.write_text(text, encoding="utf-8")
                gate = _run_gate(cfg, root)
                if gate:
                    raise ReportError(gate)
            except ReportError as exc:
                text = before
                ledger_path.write_text(text, encoding="utf-8")
                res.refused.append(f"{op['id']}: {exc}")
                append_history(root, hist, split_kb, op.get("by") or op["id"],
                               entry(f"{stamp} · follow-up `{op['id']}` refused", str(exc)))
                continue
            append_history(root, hist, split_kb, op["id"],
                           entry(f"{stamp} · filed by `{op.get('by', '?')}`", op.get("scope", "")),
                           title=clip(plain(op["title"])))
            res.touched.append(f"follow-up {op['id']}")
            res.released = True
        elif kind == "lesson":
            append_lesson(root, cfg.lessons, op.get("phase", "?"), op.get("text", ""),
                          op.get("title", ""), day)
            res.touched.append("lesson")
    ledger_path.write_text(text, encoding="utf-8")
    return res


def _markdown(cfg: Config) -> bool:
    return any(_ROW.match(ln) for ln in _ledger_text(cfg).splitlines())


def _summary(due: list[str], queued: dict[str, dict]) -> str:
    """The commit subject's tail: whose reports these are."""
    parts = []
    for k in due:
        data = queued[k]
        if data.get("outcome") and k != NOW:
            parts.append(f"{k} {_WORDS.get(data['outcome'].get('outcome', ''), 'failed')}")
        for op in data.get("ops", []):
            kind = op.get("kind")
            if kind == "record":
                parts.append(f"{op.get('phase')} {op.get('outcome')}")
            elif kind == "row":
                parts.append(f"follow-up {op.get('id')}")
            elif kind == "lesson":
                parts.append(f"lesson from {op.get('phase')}")
    return "; ".join(parts)[:200] or "reports"


def flush(cfg: Config, log: Log, finished: dict[str, str]) -> Applied:
    """Apply every queued report that is due, and commit it on the target branch.

    Due: :data:`NOW`, and each phase in ``finished`` (``{phase: status}`` as the
    swarm recorded it). A report the checkout cannot take right now (it is off
    its main branch, mid-merge, or someone left the ledger edited) stays queued
    and the next flush tries again. A bare-format ledger has no rows to write:
    its reports are dropped.
    """
    total = Applied()
    queued = pending(cfg)
    due = [k for k in queued if k == NOW or k in finished]
    if not due:
        return total
    if not _markdown(cfg):
        for k in due:
            (_qdir(cfg) / f"{k}.json").unlink(missing_ok=True)
        log.line(f"LEDGER-SKIP {' '.join(due)} (not a checklist ledger)")
        return total
    flushed_path = _qdir(cfg) / ".flushed.json"
    try:
        flushed = json.loads(flushed_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        flushed = {}
    root = cfg.project_dir
    paths = [cfg.ledger, cfg.history_dir, cfg.lessons]

    def write() -> None:
        for k in due:
            got = apply(cfg, root, k, queued[k], finished.get(k), flushed)
            total.touched += got.touched
            total.refused += got.refused
            total.released |= got.released

    what = _summary(due, queued)
    try:
        result = gitq.commit_to_target(cfg, paths, write, f"ledger: {what}", log)
    except (gitq.GitError, OSError, ReportError) as exc:
        log.line(f"LEDGER-ERROR {what} {exc}")
        return Applied()
    if result.status == gitq.HELD:
        log.line(f"LEDGER-HELD {what} {result.reason}")
        return Applied()
    if result.push is not None:
        pushowed.settle(cfg, f"ledger: {what}", {cfg.project_dir: result.push}, log)
    with _locked(cfg):
        now = time.time()
        for k in due:
            path = _qdir(cfg) / f"{k}.json"
            if _read(path) == queued[k]:
                path.unlink(missing_ok=True)
            else:  # a report arrived while we wrote: drop only what was applied
                data = _read(path)
                if data["outcome"] == queued[k]["outcome"]:
                    data["outcome"] = None
                data["ops"] = [o for o in data["ops"] if o not in queued[k]["ops"]]
                _write(path, data)
            if k != NOW:
                flushed[k] = now
        _write(flushed_path, flushed)
    for line in total.touched:
        log.line(f"LEDGER {line}")
    for line in total.refused:
        log.line(f"LEDGER-REFUSED {line}")
    return total


def release_due(cfg: Config, log: Log) -> list[str]:
    """Put back in play each ``later`` phase whose date has come.

    A ``later`` finishes like a ``fail`` (nothing lands) and its row carries the
    date; until then the launcher leaves it alone (:func:`deferred`). Once the
    date is here, its failure record is cleared the way ``swarm retry`` clears
    one, so the next free slot picks it up.
    """
    from . import state as state_mod

    text = _ledger_text(cfg)
    lines = text.split("\n")
    day = today()
    due = []
    for pid, (s, _e) in row_spans(lines).items():
        head = split_head(lines[s])
        if head and head.box == " " and head.status.startswith("later") \
                and _DATE.match(head.after or "") and head.after <= day:
            due.append(pid)
    released = []
    if not due:
        return released
    with state_mod.transaction(cfg) as st:
        for pid in due:
            if st.done.get(pid) == statuses.FAIL:
                st.done.pop(pid)
                released.append(pid)
    for pid in released:
        for status in statuses.ALL:
            (cfg.done_dir / f"{pid}.{status}").unlink(missing_ok=True)
        log.line(f"LATER-DUE {pid} back in play")
    return released
