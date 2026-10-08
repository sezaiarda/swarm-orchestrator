"""The stall check: a busy worker that has gone silent gets a model's look.

A worker can stop with nothing left to wake it: a lead waiting on a teammate
that went idle, a builder waiting on a background run that was killed, a lead
sitting at its prompt mid-batch. Nothing it does reaches the supervisor, so the
slot stays busy and nobody notices. The watchdog sweep measures each busy
worker's silence (:func:`sources`, :meth:`Sources.last_activity`): the newest
write to its lead transcript, its subagents' and teammates' transcripts, and
its background task output. Past ``[worker].stall_check_s`` it asks
``[worker].stall_model`` (:func:`judge`) whether the worker is stuck, showing
it what the worker last did and what is running for it (:func:`material`). A
stuck verdict comes with one line for the worker, which the supervisor types
into its pane prefixed :data:`PREFIX`.

The model call goes through :func:`recap.ask`, ``SWARM_STALL_CMD`` being the
hermetic-test seam. Nothing here raises: a part of the material that cannot be
read says so, and a call that fails or answers garbage is no verdict.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import buildstatus, gc as gc_mod, procs, recap, tmux
from . import session as session_mod
from .config import Config

#: The hermetic-test stand-in for the model (prompt on stdin, answer on stdout).
SEAM = "SWARM_STALL_CMD"
#: What every line the swarm types into a stuck worker starts with.
PREFIX = "Swarm stall check: "

#: The lead transcript's rendered tail (~20k tokens).
LEAD_CHARS = 80_000
#: How much of the transcript file is read to render it (its newest part).
LEAD_TAIL_BYTES = 1_500_000
#: Per subagent/teammate transcript, and all of them together.
SUB_CHARS = 2_500
SUB_TOTAL = 20_000
SUB_TAIL_BYTES = 200_000
#: Per background task output, and how many are shown (newest first).
OUTPUT_TAIL = 600
OUTPUTS_MAX = 12
PANE_LINES = 40
PROCS_MAX = 60
#: One field of a transcript entry: a tool input, a tool result, a text.
INPUT_CHARS = 400
RESULT_CHARS = 600
TEXT_CHARS = 2_000
#: The line typed into the pane, prefix included.
NUDGE_CHARS = 600


# -- where a worker's activity lands -----------------------------------------
def claude_dir() -> Path:
    """Claude Code's config dir: ``$CLAUDE_CONFIG_DIR``, else ``~/.claude``."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def projects_dir(cwd: Path) -> Path | None:
    """The ``projects/<mangled cwd>`` dir a session started in ``cwd`` writes
    its transcripts to, or ``None`` when there is none. :func:`gc.transcript_name`
    maps ``/`` and ``.``; Claude Code maps every other non-alphanumeric too, so
    both spellings are tried."""
    root = claude_dir() / "projects"
    for name in (gc_mod.transcript_name(cwd), re.sub(r"[^A-Za-z0-9]", "-", str(cwd))):
        if (root / name).is_dir():
            return root / name
    return None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _newest_first(paths) -> list[Path]:
    return sorted(paths, key=_mtime, reverse=True)


@dataclass
class Sources:
    """The files a worker's activity is written to."""

    lead: Path | None = None
    subagents: list[Path] = field(default_factory=list)  # newest first
    outputs: list[Path] = field(default_factory=list)  # newest first

    def last_activity(self) -> float | None:
        """The newest write to any of them; ``None`` without a lead transcript
        (a worker still booting, or one whose transcript cannot be found)."""
        if self.lead is None:
            return None
        return max(_mtime(p) for p in [self.lead, *self.subagents, *self.outputs])


def _turn_session(cfg: Config, phase: str) -> str | None:
    """The session id the worker's own ``Stop`` hook last recorded."""
    for rec in reversed(recap.read_turns(cfg, phase, limit=0)):
        sid = rec.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    return None


def sources(cfg: Config, phase: str, worktree: Path | None) -> Sources:
    """Where ``phase``'s worker writes. Its cwd is its worktree, and the newest
    transcript there is its lead's. A worker without one (``isolation =
    none``) shares the project dir with every other session, so only the
    session its ``Stop`` hook recorded is taken as its own."""
    cwd = worktree or cfg.project_dir
    pdir = projects_dir(cwd)
    if pdir is None:
        return Sources()
    lead: Path | None = None
    if worktree is not None:
        found = _newest_first(pdir.glob("*.jsonl"))
        lead = found[0] if found else None
    else:
        sid = _turn_session(cfg, phase)
        if sid and (pdir / f"{sid}.jsonl").is_file():
            lead = pdir / f"{sid}.jsonl"
    if lead is None:
        return Sources()
    session = lead.stem
    subs = _newest_first((pdir / session / "subagents").glob("*.jsonl"))
    outputs: list[Path] = []
    tmp = cfg.session_tmp(phase)
    if tmp is not None and tmp.is_dir():
        outputs = _newest_first(tmp.glob(f"claude-*/*/{session}/tasks/*.output"))
    return Sources(lead=lead, subagents=subs, outputs=outputs)


# -- rendering a transcript --------------------------------------------------
def _cut(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


def _stamp(entry: dict) -> str:
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return "--:--:--"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return raw[:19]


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return ""


def render_entry(entry: dict) -> list[str]:
    """One transcript entry as compact lines; none for what says nothing about
    where the worker is (thinking, attachments, queue operations, metadata)."""
    kind = entry.get("type")
    at = _stamp(entry)
    if kind == "system":
        text = entry.get("content") or entry.get("subtype") or ""
        return [f"[{at}] system: {_cut(text, RESULT_CHARS)}"] if text else []
    if kind not in ("user", "assistant"):
        return []
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [f"[{at}] {kind}: {_cut(content, TEXT_CHARS)}"] if content.strip() else []
    out: list[str] = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and str(block.get("text", "")).strip():
            out.append(f"[{at}] {kind}: {_cut(block['text'], TEXT_CHARS)}")
        elif btype == "tool_use":
            args = json.dumps(block.get("input"), ensure_ascii=False)
            out.append(f"[{at}] {kind} tool_use {block.get('name')}: {_cut(args, INPUT_CHARS)}")
        elif btype == "tool_result":
            err = " (error)" if block.get("is_error") else ""
            text = _result_text(block.get("content"))
            out.append(f"[{at}] tool_result{err}: {_cut(text, RESULT_CHARS)}")
    return out


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    """The last whole lines of ``path`` within ``max_bytes``."""
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if size > max_bytes else lines


def render_transcript(path: Path, budget: int, tail_bytes: int) -> str:
    """The newest ``budget`` characters of ``path`` rendered, whole lines only."""
    rendered: list[str] = []
    for line in _tail_lines(path, tail_bytes):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            rendered.extend(render_entry(entry))
    kept: list[str] = []
    for line in reversed(rendered):
        budget -= len(line) + 1
        if budget < 0:
            break
        kept.append(line)
    return "\n".join(reversed(kept))


# -- what is running for it --------------------------------------------------
def _dur(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def _ago(ts: float, now: float) -> str:
    return _dur(now - ts)


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "?"


def pane_pid(pane: str) -> int | None:
    out = tmux.run(["display-message", "-p", "-t", pane, "#{pane_pid}"])
    try:
        return int(out.stdout.strip()) if out.returncode == 0 else None
    except ValueError:
        return None


def process_tree(cfg: Config, phase: str, pane: str) -> str:
    """Every process under the pane and of the worker's session: pid, ppid,
    elapsed, state, command."""
    table = procs.table()
    roots = {p for p in (pane_pid(pane),) if p in table}
    found = session_mod._descendants(table, set(roots))
    found |= session_mod.session_alive(cfg, session_mod.session_markers(cfg, "worker", phase))
    if not found:
        return "(no process found)"
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError):
        uptime, hz = 0.0, 100
    rows = []
    for pid in sorted(found)[:PROCS_MAX]:
        fields_ = procs._stat_fields(pid) or ["?"]
        ticks = procs.start_ticks(pid)
        elapsed = _dur(uptime - ticks / hz) if ticks is not None and uptime else "?"
        cmd = " ".join(procs.cmdline(pid)) or procs.comm(pid)
        rows.append(f"{pid} ppid={table.get(pid, '?')} up={elapsed} state={fields_[0]}"
                    f" {_cut(cmd, 160)}")
    if len(found) > PROCS_MAX:
        rows.append(f"... and {len(found) - PROCS_MAX} more")
    return "\n".join(rows)


def pane_tail(pane: str, lines: int = PANE_LINES) -> str:
    text = tmux.capture(pane).rstrip("\n")
    return "\n".join(text.splitlines()[-lines:]) or "(empty)"


def _meta(path: Path) -> str:
    """A subagent's name and type, from the ``.meta.json`` beside it."""
    try:
        data = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path.stem
    if not isinstance(data, dict):
        return path.stem
    parts = [f"{k}={data[k]}" for k in ("name", "customAgentType", "agentType", "taskKind",
                                         "description") if data.get(k)]
    return ", ".join(parts) or path.stem


def _section(title: str, body: str) -> str:
    return f"=== {title} ===\n{body.strip() or '(nothing)'}"


def _safe(fn, *args) -> str:
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - one unreadable part must not lose the rest
        return f"(could not read: {exc})"


def material(cfg: Config, phase: str, src: Sources, pane: str, silent_s: float,
             now: float | None = None) -> str:
    """Everything the model is shown about ``phase``'s worker."""
    now = time.time() if now is None else now
    parts = [f"Phase: {phase}. Its worker has written nothing for {_dur(silent_s)}"
             f" (since {_clock(now - silent_s)}; it is now {_clock(now)})."]

    def subs() -> str:
        out, total = [], 0
        for path in src.subagents:
            body = render_transcript(path, SUB_CHARS, SUB_TAIL_BYTES)
            block = (f"--- {_meta(path)}; last write {_clock(_mtime(path))}"
                     f" ({_ago(_mtime(path), now)} ago)\n{body}")
            if total + len(block) > SUB_TOTAL:
                out.append(f"... {len(src.subagents) - len(out)} older ones left out")
                break
            out.append(block)
            total += len(block)
        return "\n".join(out)

    def outputs() -> str:
        out = []
        for path in src.outputs[:OUTPUTS_MAX]:
            try:
                size = path.stat().st_size
                with path.open("rb") as fh:
                    fh.seek(max(0, size - OUTPUT_TAIL))
                    tail = fh.read().decode("utf-8", "replace").strip()
            except OSError:
                continue
            out.append(f"--- {path.name}: {size} bytes, last write {_clock(_mtime(path))}"
                       f" ({_ago(_mtime(path), now)} ago)\n{tail or '(empty)'}")
        if len(src.outputs) > OUTPUTS_MAX:
            out.append(f"... {len(src.outputs) - OUTPUTS_MAX} older ones left out")
        return "\n".join(out)

    def gate() -> str:
        return buildstatus.render(buildstatus.snapshot(cfg, n_recent=5))

    lead = src.lead
    parts.append(_section(
        f"lead transcript, newest part (last write {_clock(_mtime(lead)) if lead else '?'})",
        _safe(render_transcript, lead, LEAD_CHARS, LEAD_TAIL_BYTES) if lead else ""))
    parts.append(_section("subagents and teammates, most recent first", _safe(subs)))
    parts.append(_section("background task output files, most recent first", _safe(outputs)))
    parts.append(_section("processes of the worker", _safe(process_tree, cfg, phase, pane)))
    parts.append(_section("machine build gate (swarm build --status)", _safe(gate)))
    parts.append(_section(f"worker pane, last {PANE_LINES} lines", _safe(pane_tail, pane)))
    return "\n\n".join(parts)


# -- the model's verdict -----------------------------------------------------
PROMPT = """\
You are the stall watchdog of an automated build swarm. A worker session (a \
Claude Code "lead" that builds one ledger row, or a batch of rows, with \
subagents and teammates) has written nothing for a long time. Decide whether \
it is STUCK.

STUCK means nothing is running for it and nothing will wake it. For example:
- it waits on a teammate or subagent that is idle or finished without reporting;
- it waits on a background job that already ended or died (an empty output \
file, an exit code, no such process running);
- it sits at its prompt in the middle of its work;
- it is going round in a dead-end loop.
NOT stuck:
- a build, test or other process is genuinely running for it (see the \
process list), or its build is queued or running in the build gate;
- it is waiting on the owner (it ran `swarm waiting` or asked a question).

If it is stuck, write one line to the worker that says concretely what is \
wrong and what to do next, naming the teammate, job, time or exit code, e.g. \
"teammate w15 is idle and its nextest run ended at 02:49 with exit 144 and no \
output; message it to re-run in the foreground, or finish the row yourself".

Reply with JSON only, no prose and no code fence:
{{"stuck": true or false, "why": "<one line>", "message": "<one line to the worker, empty if not stuck>"}}

{material}
"""


@dataclass
class Verdict:
    stuck: bool | None  # None: no verdict (see reason)
    why: str = ""
    message: str = ""
    reason: str | None = None


def parse(answer: str | None) -> Verdict:
    """The model's JSON reply; anything else is no verdict."""
    if not answer:
        return Verdict(None, reason="empty")
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end <= start:
        return Verdict(None, reason="not-json")
    try:
        data = json.loads(answer[start : end + 1])
    except ValueError:
        return Verdict(None, reason="not-json")
    if not isinstance(data, dict) or not isinstance(data.get("stuck"), bool):
        return Verdict(None, reason="no-stuck-field")
    why = _cut(data.get("why") or "", 300)
    message = _cut(data.get("message") or "", NUDGE_CHARS - len(PREFIX))
    if data["stuck"] and not message:
        return Verdict(None, why=why, reason="stuck-without-message")
    return Verdict(data["stuck"], why=why, message=message)


def judge(cfg: Config, text: str) -> Verdict:
    """Ask ``[worker].stall_model`` about the worker ``text`` describes."""
    answer, reason = recap.ask(cfg, PROMPT.format(material=text), seam=SEAM,
                               model=cfg.stall_model or recap.MODEL)
    if answer is None:
        return Verdict(None, reason=reason or "no-answer")
    return parse(answer)


def nudge_line(message: str) -> str:
    """The one line typed into the worker's pane."""
    return _cut(PREFIX + message, NUDGE_CHARS)
