"""One-time move of a ledger's accumulated notes (and STATUS journal) into history.

Before the swarm became the ledger's only writer, every session appended its
notes to the row it touched, so the ledger carried the whole history of the
project: a long-running ledger can reach megabytes, with single rows of many KB. This slims
each such row to what the swarm keeps (box, id, dir, needs, a short bold title,
tags) and files the row as it stood, verbatim, in its family's history
(:mod:`ledgerw`). A row that is already short is left alone, so a second run
changes nothing.

It also moves the dated journal out of the status page: everything above the
page's first ``##`` section and every dated section ("State — <date>",
"Previous state", "Session log", …) go verbatim to
``<history>/STATUS-archive.md``; the page keeps its title, its standing
sections, and a short note saying where things are now. ``--components`` does
the same for each component repo's ``docs/STATUS.md``, into that repo's own
``<history>/STATUS-archive.md``.

Run it with the swarm stopped, then commit the result in each repo::

    python -m swarm_orchestrator.ledgermigrate --project-dir DIR [--components] [--write]

Without ``--write`` it only reports. Either way it proves the ledger still reads
the same: the same phases in the same order, the same boxes, needs, dirs, tags
and section headings, and every moved row found verbatim in its history.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import ledger as ledger_mod
from . import ledgerw
from .config import load
from .web import rows as rows_mod

#: A row longer than this, or with any continuation line, is slimmed.
SLIM_CHARS = 240
ARCHIVE = "STATUS-archive.md"
WHERE = "**Where things are now.**"
_TAGS = re.compile(r"TAG:`([^`]+)`")


@dataclass
class Plan:
    """The migration, computed in memory."""

    ledger: str
    moved: dict[str, str] = field(default_factory=dict)  # id -> the row as it stood
    titles: dict[str, str] = field(default_factory=dict)
    status: str | None = None
    archive: str | None = None


def slim_ledger(text: str) -> Plan:
    """Each long row cut to its state fields; what it said goes to ``moved``."""
    lines = text.split("\n")
    spans = ledgerw.row_spans(lines)
    out: list[str] = []
    plan = Plan(ledger="")
    starts = {s: (pid, e) for pid, (s, e) in spans.items()}
    i = 0
    while i < len(lines):
        if i not in starts:
            out.append(lines[i])
            i += 1
            continue
        pid, end = starts[i]
        body = lines[i:end]
        head = ledgerw.split_head(body[0])
        title = ledgerw._title_of(head.prose)
        slim = head.line(title)
        if len(body) == 1 and (len(body[0]) <= SLIM_CHARS or slim == body[0]):
            out.append(body[0])
            i = end
            continue
        out.append(slim)
        plan.moved[pid] = "\n".join(body)
        plan.titles[pid] = title
        i = end
    plan.ledger = "\n".join(out)
    return plan


#: A status-page section that is a dated journal entry rather than standing
#: state: "State — <date>", "Previous state", "Prior state", "Earlier", "Session
#: log", "Last updated", or any heading carrying a date.
_JOURNAL = re.compile(
    r"^##\s+\**\s*(state|previous|previously|prior|earlier|session log|last updated)\b"
    r"|^##\s.*\b20\d\d-\d\d-\d\d\b", re.I)


def umbrella_note(ledger: str, history: str) -> list[str]:
    return [
        WHERE + f" The ledger (`{ledger}`) is the state of every phase.",
        f"What was written about a phase is in `{history}/<family>.md` (the id up to its first `-`).",
        f"The dated session entries that used to fill this page are in `{history}/{ARCHIVE}`.",
        "Sessions do not add entries here, to the ledger or to the history; they report through the swarm.",
    ]


def component_note(history: str) -> list[str]:
    return [
        WHERE + " This page keeps this repo's standing state only. Phase state is the umbrella's",
        "ledger, and what each phase did is in the umbrella's phase history. The dated entries",
        f"that used to fill this page are in `{history}/{ARCHIVE}`. Sessions do not add entries",
        "here; they report through the swarm.",
    ]


def split_status(text: str, note: list[str]) -> tuple[str, str] | None:
    """``(page, archive)`` for a status page with a dated journal, else None.

    The journal is everything between the title's first paragraph and the first
    ``##`` section, plus every section :data:`_JOURNAL` recognises; the archive
    keeps them verbatim, in page order. The page keeps its title paragraph,
    ``note``, and its standing sections.
    """
    lines = text.split("\n")
    first = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), len(lines))
    top = 0
    while top < first and not lines[top].startswith("# "):
        top += 1
    j = top + 1
    while j < first and not lines[j].strip():
        j += 1
    if j < first and lines[j].startswith(WHERE):
        j = top + 1  # no title paragraph of its own: ours comes first
    else:
        while j < first and lines[j].strip():
            j += 1
    preamble, journal = lines[:j], lines[j:first]
    k = next((i for i, ln in enumerate(journal) if ln.strip()), None)
    if k is not None and journal[k].startswith(WHERE):
        journal = []  # already migrated: that paragraph is ours
    sections: list[list[str]] = []
    for ln in lines[first:]:
        if ln.startswith("## "):
            sections.append([ln])
        elif sections:
            sections[-1].append(ln)
    keep = [s for s in sections if not _JOURNAL.match(s[0])]
    logs = [s for s in sections if _JOURNAL.match(s[0])]
    if not any(ln.strip() for ln in journal) and not logs:
        return None
    page = preamble + [""] + note + [""] + [ln for s in keep for ln in s]
    archive = ["# STATUS archive", "",
               "The dated entries of `docs/STATUS.md`, as they stood when they moved here.",
               ""] + journal + [ln for s in logs for ln in s]
    return "\n".join(page).rstrip("\n") + "\n", "\n".join(archive).rstrip("\n") + "\n"


def move_status(root: Path, status: str, history: str, note: list[str], write: bool) -> str:
    """Move one repo's status journal into ``<history>/STATUS-archive.md``; its report line."""
    path = root / status
    if not path.is_file():
        return f"{root.name}: no {status}"
    before = path.stat().st_size
    text = path.read_text(encoding="utf-8")
    split = split_status(text, note)
    if split is None:
        return f"{root.name}: {status} {before:,} bytes, unchanged"
    lost = Counter(ln for ln in text.split("\n") if ln.strip())
    lost.subtract(ln for part in split for ln in part.split("\n") if ln.strip())
    if any(n > 0 for n in lost.values()):
        return f"{root.name}: FAILED, the split would drop lines of {status}"
    if write:
        path.write_text(split[0], encoding="utf-8")
        (root / history).mkdir(parents=True, exist_ok=True)
        arch = root / history / ARCHIVE
        prior = arch.read_text(encoding="utf-8") if arch.is_file() else ""
        arch.write_text(split[1] + ("\n" + prior if prior else ""), encoding="utf-8")
    return (f"{root.name}: {status} {before:,} -> {len(split[0].encode()):,} bytes"
            f" (archive {len(split[1].encode()):,})")


def component_repos(root: Path) -> list[Path]:
    """The independent git repos directly under ``root`` that keep a status page."""
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and (p / ".git").exists() and (p / "docs" / "STATUS.md").is_file())


# -- equivalence -----------------------------------------------------------
def fingerprint(text: str) -> dict:
    """Everything a reader of the ledger computes from it, per phase and in order."""
    graph = ledger_mod.parse(text)
    rows, _heads = rows_mod.parse(text)
    lines = text.split("\n")
    spans = ledgerw.row_spans(lines)
    per: dict[str, tuple] = {}
    for pid in graph:
        head = ledgerw.split_head(lines[spans[pid][0]])
        row = rows[pid]
        per[pid] = (
            sorted(graph[pid]),
            row.checked,
            row.dirs,
            [h.text for h in row.heads],
            ledgerw._needs_tokens(head),
            [part for part in head.meta if ledgerw._NEEDS.match(part)],
            sorted(_TAGS.findall(" ".join(head.tags))),
            [m for m in head.meta if not ledgerw._NEEDS.match(m)],
        )
    return {
        "order": list(graph),
        "ticked": sorted(ledger_mod.ticked(text)),
        "issues": ledger_mod.validate(graph),
        "rows": per,
        "prose": [ln for i, ln in enumerate(lines)
                  if not any(s <= i < e for s, e in spans.values())],
    }


def diff(a: dict, b: dict) -> list[str]:
    out = []
    for key in ("order", "ticked", "issues"):
        if a[key] != b[key]:
            out.append(f"{key} differs")
    for pid, fa in a["rows"].items():
        fb = b["rows"].get(pid)
        if fa != fb:
            out.append(f"{pid}: {fa} != {fb}")
    return out


def run_gate(cmd: str, root: Path) -> str:
    """The project's own ledger gate over ``root``; its output, exit code first."""
    proc = subprocess.run(cmd, shell=True, cwd=str(root), capture_output=True, text=True,
                          timeout=300)
    return f"exit {proc.returncode}\n{proc.stdout}{proc.stderr}"


# -- the run ----------------------------------------------------------------
def migrate(root: Path, ledger: str, history: str, split_kb: int, status: str | None,
            write: bool, day: str, gate: str = "") -> list[str]:
    """Migrate ``root`` (in place when ``write``); returns the report lines."""
    ledger_path = root / ledger
    before = ledger_path.read_text(encoding="utf-8")
    plan = slim_ledger(before)
    report: list[str] = []
    problems = diff(fingerprint(before), fingerprint(plan.ledger))
    status_path = root / status if status else None
    split = None
    if status_path is not None and status_path.is_file():
        split = split_status(status_path.read_text(encoding="utf-8"), umbrella_note(ledger, history))
    gate_before = run_gate(gate, root) if gate else ""

    size = lambda p: p.stat().st_size if p.is_file() else 0  # noqa: E731
    report.append(f"ledger: {len(before.encode()):,} bytes -> {len(plan.ledger.encode()):,} bytes;"
                  f" {len(plan.moved)} of {len(ledger_mod.parse(before))} rows slimmed")
    if status_path is not None:
        report.append(f"status: {size(status_path):,} bytes -> "
                      + (f"{len(split[0].encode()):,} bytes (archive {len(split[1].encode()):,})"
                         if split else "unchanged"))
    if write:
        for pid, text in plan.moved.items():
            body = re.sub(r"^\s*[-*]\s+\[[ xX]\]\s+", "", text, count=1)
            ledgerw.append_history(root, history, split_kb, pid,
                                   ledgerw.entry(f"The ledger row as it stood on {day}", body),
                                   title=plan.titles.get(pid, ""))
        ledger_path.write_text(plan.ledger, encoding="utf-8")
        if split:
            status_path.write_text(split[0], encoding="utf-8")
            (root / history).mkdir(parents=True, exist_ok=True)
            arch = root / history / ARCHIVE
            prior = arch.read_text(encoding="utf-8") if arch.is_file() else ""
            arch.write_text(split[1] + ("\n" + prior if prior else ""), encoding="utf-8")
        lost = [pid for pid, text in plan.moved.items()
                if re.sub(r"^\s*[-*]\s+\[[ xX]\]\s+", "", text, count=1)
                not in ledgerw.history_text(root, history, pid)]
        if lost:
            problems.append(f"history is missing the moved text of {', '.join(lost[:10])}")
        files = [p for p in (root / history).rglob("*.md")]
        report.append(f"history: {len(files)} files, {sum(p.stat().st_size for p in files):,} bytes"
                      f" under {history}/ (largest {max((p.stat().st_size for p in files), default=0):,})")
        if gate:
            gate_after = run_gate(gate, root)
            if gate_after != gate_before:
                problems.append(f"the ledger gate reads differently:\n{gate_before}\n---\n{gate_after}")
            report.append(f"gate: same output before and after ({gate_before.splitlines()[0]})"
                          if gate_after == gate_before else "gate: DIFFERENT output")
    report.append("equivalence: " + ("the ledger reads the same (phases, order, boxes, needs,"
                                     " dirs, tags, headings, prose lines)" if not problems
                                     else "FAILED"))
    report += [f"  {p}" for p in problems[:40]]
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m swarm_orchestrator.ledgermigrate",
                                 description=__doc__.split("\n\n")[0])
    ap.add_argument("--project-dir", default=".")
    ap.add_argument("--status", default="docs/STATUS.md",
                    help='the status page to move the journal out of ("" = none)')
    ap.add_argument("--gate", default="",
                    help="a command checking the ledger, run before and after to compare")
    ap.add_argument("--components", action="store_true",
                    help="also move the journal out of each component repo's docs/STATUS.md")
    ap.add_argument("--repos", nargs="+", default=[], metavar="DIR",
                    help="only these component repos' status pages (the umbrella is left alone)")
    ap.add_argument("--write", action="store_true", help="write the result (default: report only)")
    a = ap.parse_args(argv)
    cfg = load(project_dir=a.project_dir)
    report: list[str] = []
    if not a.repos:
        report = migrate(cfg.project_dir, cfg.ledger, cfg.history_dir, cfg.history_split_kb,
                         a.status or None, a.write, ledgerw.today(), a.gate)
    repos = [Path(r).resolve() for r in a.repos] or (
        component_repos(cfg.project_dir) if a.components else [])
    note = component_note(cfg.history_dir)
    report += [move_status(r, "docs/STATUS.md", cfg.history_dir, note, a.write) for r in repos]
    print("\n".join(report))
    return 1 if any(ln.startswith("equivalence: FAILED") for ln in report) else 0


if __name__ == "__main__":
    sys.exit(main())
