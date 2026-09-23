"""The Overseer's pass records: what each pass was for, saw, did and left over.

Every pass leaves two files under ``<state>/overseer/``, both named by the pass
id (a UTC timestamp, so they sort in time order):

``<id>.md``
    Written for people. The supervisor creates it with the trigger and the digest
    path and three empty sections; the Overseer fills ``## Saw``, ``## Did`` and
    ``## Left for the owner`` in its own words before it signs off.
``<id>.json``
    Written for programs: id, times, status, trigger reasons, digest, mirror,
    the one-line summary from ``swarm overseer-done`` and any owner question.

:func:`load_passes` joins the two into :class:`PassRecord` — the one loader the
CLI, the dashboard and the web board read, so none of them parses markdown on
its own. A record that says ``running`` for a pass that is not the live one was
cut off by a supervisor restart and reads back as ``interrupted``.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import gitq
from .config import Config
from .overseer import overseer_dir

#: The status the Overseer's mirror rides the merge queue under. Like the
#: operator's, it is not a `swarm done` status: the supervisor keys on it to land
#: the branch without recording a ledger phase done.
INTEG_STATUS = "overseer-pass"
#: Mirror (and ``swarm/<name>`` branch) prefix — never a phase id or an ``op-`` job.
MIRROR_PREFIX = "ovs-"

RUNNING = "running"
DONE = "done"
TIMEOUT = "timeout"
FAILED = "failed"  # the session would not start
INTERRUPTED = "interrupted"  # the supervisor stopped under it

#: The sections the Overseer writes, in order: heading -> PassRecord field.
SECTIONS = {"Saw": "saw", "Did": "did", "Left for the owner": "left"}

_ID_RE = re.compile(r"^\d{8}T\d{6}Z(?:-\d+)?$")


@dataclass
class PassRecord:
    """One Overseer pass, as the programs that show it want it."""

    id: str
    started_at: float = 0.0
    ended_at: float = 0.0
    status: str = RUNNING
    reasons: list[dict] = field(default_factory=list)
    digest: str = ""
    record: str = ""
    mirror: str = ""
    summary: str = ""
    question: str = ""
    asked_at: float = 0.0
    answer: str = ""
    saw: str = ""
    did: str = ""
    left: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PassRecord":
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("id", "")
        return cls(**kwargs)

    @property
    def duration_s(self) -> float | None:
        if not self.started_at or not self.ended_at:
            return None
        return max(0.0, self.ended_at - self.started_at)


# -- naming ----------------------------------------------------------------
def new_id(cfg: Config, now: float | None = None) -> str:
    """A fresh pass id: the UTC start time, suffixed on a same-second clash."""
    now = time.time() if now is None else now
    base = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    d = overseer_dir(cfg)
    pid, n = base, 1
    while (d / f"{pid}.json").exists():
        n += 1
        pid = f"{base}-{n}"
    return pid


def mirror_name(pass_id: str) -> str:
    """``ovs-<id>``, lower-cased: the id is also a git branch name."""
    return f"{MIRROR_PREFIX}{pass_id.lower()}"


def json_path(cfg: Config, pass_id: str) -> Path:
    return overseer_dir(cfg) / f"{pass_id}.json"


def md_path(cfg: Config, pass_id: str) -> Path:
    return overseer_dir(cfg) / f"{pass_id}.md"


# -- writing ---------------------------------------------------------------
def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def create(
    cfg: Config,
    pass_id: str,
    reasons: list[dict],
    digest: Path | None,
    mirror: str = "",
    now: float | None = None,
) -> PassRecord:
    """Start a pass's record: the JSON, and the markdown skeleton it fills in."""
    now = time.time() if now is None else now
    rec = PassRecord(
        id=pass_id,
        started_at=now,
        reasons=list(reasons),
        digest=str(digest or ""),
        record=str(md_path(cfg, pass_id)),
        mirror=mirror,
    )
    _write_json(json_path(cfg, pass_id), _stored(rec))
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))
    why = "\n".join(f"- {r.get('text', r.get('key', '?'))}" for r in reasons) or "- (none)"
    body = [
        f"# Overseer pass {pass_id}",
        "",
        f"Started {when}. Digest: {rec.digest or '(none)'}",
        "",
        "## Trigger",
        why,
        "",
    ]
    for heading in SECTIONS:
        body += [f"## {heading}", "", ""]
    md_path(cfg, pass_id).write_text("\n".join(body), encoding="utf-8")
    return rec


def _stored(rec: PassRecord) -> dict:
    """The JSON half: everything but the prose sections, which live in the .md."""
    data = rec.to_dict()
    for name in SECTIONS.values():
        data.pop(name, None)
    return data


def update(cfg: Config, pass_id: str, **changes) -> PassRecord | None:
    """Change fields of a stored record; None when there is no such pass.

    A terminal status is never overwritten by another one: the session's own
    ``overseer-done`` and a timeout can race, and whichever landed first is the
    truth about how the pass ended.
    """
    rec = load_json(cfg, pass_id)
    if rec is None:
        return None
    status = changes.get("status")
    if status and status != rec.status and rec.status != RUNNING:
        changes.pop("status")
        changes.pop("ended_at", None)
    for key, val in changes.items():
        if hasattr(rec, key):
            setattr(rec, key, val)
    _write_json(json_path(cfg, pass_id), _stored(rec))
    return rec


def append_summary(cfg: Config, pass_id: str, summary: str) -> None:
    """Put the one-line summary at the foot of the markdown, too."""
    path = md_path(cfg, pass_id)
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## Summary\n{summary}\n")
    except OSError:
        pass


# -- reading ---------------------------------------------------------------
def load_json(cfg: Config, pass_id: str) -> PassRecord | None:
    try:
        data = json.loads(json_path(cfg, pass_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    rec = PassRecord.from_dict(data)
    return rec if rec.id else None


def sections(text: str) -> dict[str, str]:
    """``{field: body}`` for the Saw / Did / Left sections of a record's markdown.

    Level-two headings only; a heading the Overseer renamed or dropped just
    yields nothing for that field, never an error."""
    out: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines() + ["## "]:
        if line.startswith("## "):
            if current is not None:
                out[current] = "\n".join(lines).strip()
            current = SECTIONS.get(line[3:].strip())
            lines = []
        elif current is not None:
            lines.append(line)
    return out


def load(cfg: Config, pass_id: str, live: str | None = None) -> PassRecord | None:
    """One pass, prose included. ``live`` is the pass the supervisor is running."""
    rec = load_json(cfg, pass_id)
    if rec is None:
        return None
    try:
        text = md_path(cfg, pass_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for name, body in sections(text).items():
        setattr(rec, name, body)
    if rec.status == RUNNING and pass_id != live:
        rec.status = INTERRUPTED
    return rec


def load_passes(cfg: Config, limit: int | None = 20, live: str | None = None) -> list[PassRecord]:
    """Recent passes, newest first — what ``swarm overseer``, the TUI and the
    web board list."""
    try:
        ids = sorted(
            (p.stem for p in overseer_dir(cfg).glob("*.json") if _ID_RE.match(p.stem)),
            reverse=True,
        )
    except OSError:
        return []
    if limit is not None:
        ids = ids[:limit]
    return [r for r in (load(cfg, i, live) for i in ids) if r is not None]


def mark_stale(cfg: Config, live: str | None = None) -> list[str]:
    """Settle every ``running`` record that is not the live pass as interrupted.

    Run when a supervisor starts: no pass survives a restart (``swarm up``
    rebuilds state), so a ``running`` record now is one that was cut off."""
    out: list[str] = []
    for rec in load_passes(cfg, limit=None, live=live):
        if rec.status == INTERRUPTED:
            stored = load_json(cfg, rec.id)
            if stored is not None and stored.status == RUNNING:
                update(cfg, rec.id, status=INTERRUPTED, ended_at=time.time())
                out.append(rec.id)
    return out


def mirror_plan(cfg: Config) -> dict[str, str]:
    """``{mirror: "integrate"}`` for every pass mirror whose branch still exists.

    ``gitq.reconcile`` discards a ``swarm/*`` branch with no completion sentinel
    as an interrupted phase. An Overseer mirror has no sentinel by design, and
    what it committed are deliberate ledger edits — so ``swarm up`` lands it
    instead. Anything left uncommitted in the mirror is lost with it, which the
    prompt tells the Overseer."""
    if cfg.git_isolation != "worktree":
        return {}
    plan: dict[str, str] = {}
    for rec in load_passes(cfg, limit=None):
        if rec.mirror and gitq.branch_exists(cfg.project_dir, f"swarm/{rec.mirror}"):
            plan[rec.mirror] = "integrate"
    return plan
