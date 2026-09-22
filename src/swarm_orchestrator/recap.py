"""One-glance, 1-2 sentence recaps of what each worker actually did.

The owner's dashboard needs a line per phase — *what did this worker deliver, and
does it need me?* — plus a searchable history of every past worker. This module
produces exactly that, and it produces it **only** at two moments:

* **on completion** — the supervisor/`swarm done` path calls
  :func:`summarize` once, when a phase's sentinel lands, and
* **on demand** — the owner explicitly asks (``on_demand=True``).

There is deliberately **no timer**. A periodic recap would burn a model call per
worker per tick for a line nobody asked for, and would report on a phase that is
still mid-flight — the summary would be wrong more often than right.

Where the text comes from
-------------------------
A worker's ``Stop`` hook (``scripts/stop-hook.py``) appends the *final text of
every assistant turn* to ``<state>/turns/<phase>.jsonl``. That payload field
(``last_assistant_message``) is free, arrives at the turn boundary, and is the
exact text the worker last said — no API call, no transcript scraping, and it
keeps working while the worker is busy. The ``swarm done`` sentinel adds the
worker's own one-line completion note on top.

Notably we do **not** drive claude's ``/recap`` slash command. It is not an
`immediate` command, so a ``send-keys`` into a live worker queues behind a turn
that can run for hours — it returns nothing for the entire window you care about.

The model call
--------------
Haiku is asked for the 1-2 sentences. Three back-ends, in preference order:
``SWARM_RECAP_CMD`` (the hermetic-test seam, same shape as ``SWARM_MASTER_CMD``),
a direct Messages API call when ``ANTHROPIC_API_KEY`` exists, then ``claude -p``
— which needs no key at all because it reuses the CLI's own OAuth login. There is
no API key is assumed in the environment, so ``claude -p`` is the live path; it
carries a few thousand tokens of harness overhead per call, which is irrelevant
at ~25 calls per campaign and buys zero setup.

And when the captured text is *already* short enough to serve as the recap (the
common case — a worker's completion note is usually one tight sentence), no model
is called at all.

Nothing here raises. A missing turns file, no sentinel, no ``claude`` binary, a
timeout, a garbage response — every one yields a :class:`Recap` with
``summary=None`` and a ``reason``, because a dashboard that crashes on a missing
recap is worse than a dashboard with a blank cell.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import statuses
from .config import Config

# A CLI family alias, never a version: `haiku` always resolves to the newest Haiku.
MODEL = "haiku"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Sentinel statuses, in the same precedence `gitq.sentinel_done` uses: a
# completed build outranks a fail/skip left over from an earlier attempt.
_STATUSES = statuses.PRECEDENCE

# How many trailing turns feed the model. The last few turns are where a worker
# says what it landed; earlier ones are mid-flight noise, and every extra turn is
# tokens spent on text the recap will not mention.
_TURN_WINDOW = 8
_MAX_MATERIAL_CHARS = 12_000
# At or under this, the captured text IS the recap — skip the model entirely.
_SHORT_ENOUGH_WORDS = 40
# Hard ceiling on what we will hand the dashboard, model or not. A model that
# ignores the word limit must not be able to blow up a one-line UI.
_MAX_SUMMARY_WORDS = 60

_CLI_TIMEOUT_S = 180.0
_API_TIMEOUT_S = 60.0
_MAX_TOKENS = 300

_PROMPT = """\
You are writing a one-glance status line for the owner of an automated build \
swarm. They are scanning a dashboard, not reading a report.

Phase: {phase}
Outcome status: {status}

Below is what the worker for this phase actually said. Summarise it in ONE or \
TWO plain sentences, under 40 words total.

Rules:
- Lead with what the phase DELIVERED — what now exists or works that did not before.
- Then, only if there is one, the single thing that needs the owner: a decision \
to make, a deliberate deviation, a risk they are carrying.
- Plain prose. No markdown, no bullets, no headings, no backticks, no emoji.
- No preamble. Do not start with "This phase", "The worker" or "Summary:".
- Invent nothing. If the text below does not say it, it does not go in.
- Answer directly from the text below; do not use any tools.

--- worker report ---
{material}
--- end of worker report ---
"""


@dataclass
class Recap:
    """One phase's recap. JSON-serialisable both ways (the TUI reads the file).

    ``summary`` is ``None`` whenever we could not produce one; ``reason`` then
    says why, so a dashboard can show "no turns captured" instead of a blank it
    cannot explain.
    """

    phase: str
    status: str | None = None
    summary: str | None = None
    raw: str = ""
    ts: float = 0.0
    source: str = "completion"  # completion | on-demand
    reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Recap":
        """Build from a possibly-partial/foreign dict — unknown keys are dropped
        and missing ones default, so a recap written by an older (or newer)
        version never breaks the reader."""
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs.setdefault("phase", "")
        return cls(**kwargs)


# -- paths ----------------------------------------------------------------
def turns_dir(cfg: Config) -> Path:
    """Where the ``Stop`` hook appends per-phase turn text."""
    return cfg.state_dir / "turns"


def turns_path(cfg: Config, phase: str) -> Path:
    return turns_dir(cfg) / f"{phase}.jsonl"


def recaps_dir(cfg: Config) -> Path:
    return cfg.state_dir / "recaps"


def recap_path(cfg: Config, phase: str) -> Path:
    return recaps_dir(cfg) / f"{phase}.json"


# -- inputs ---------------------------------------------------------------
def read_turns(cfg: Config, phase: str, limit: int = _TURN_WINDOW) -> list[dict]:
    """The last ``limit`` captured turns for ``phase``, oldest first.

    Tolerant by construction: the hook appends under no lock, so a torn or
    half-written final line is possible and is simply skipped rather than
    poisoning the whole recap.
    """
    path = turns_path(cfg, phase)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    turns: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("text"):
            turns.append(rec)
    return turns[-limit:] if limit > 0 else turns


def sentinel(cfg: Config, phase: str) -> tuple[str | None, str]:
    """``(status, note)`` from ``done/<phase>.<status>``, or ``(None, "")``.

    The sentinel body is ``"<phase> <status> <note>"`` (see
    ``launch._write_sentinel``); the note is the worker's own one-line recap and
    is by far the highest-signal sentence we have about the phase.
    """
    for status in _STATUSES:
        path = cfg.done_dir / f"{phase}.{status}"
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts = body.strip().split(None, 2)
        return status, parts[2] if len(parts) > 2 else ""
    return None, ""


# -- text shaping ---------------------------------------------------------
_MD_LEAD = re.compile(r"^\s*(?:[-*+]\s+|#{1,6}\s+|>\s+|\d+[.)]\s+)")
_MD_INLINE = re.compile(r"[`*_~]+")


def _plain(text: str) -> str:
    """Markdown-ish text collapsed to one line of plain prose.

    Workers write markdown; a dashboard cell wants a sentence. Strip list/heading
    markers per line, drop inline emphasis and backticks, then collapse all
    whitespace — so a bulleted final message still reads as a sentence.
    """
    lines = [_MD_LEAD.sub("", ln) for ln in (text or "").splitlines()]
    joined = " ".join(ln.strip() for ln in lines if ln.strip())
    return " ".join(_MD_INLINE.sub("", joined).split())


def _words(text: str) -> int:
    return len(text.split())


def _clamp(text: str) -> str:
    """Cut a summary to :data:`_MAX_SUMMARY_WORDS`, ellipsis if it was cut."""
    words = text.split()
    if len(words) <= _MAX_SUMMARY_WORDS:
        return text
    return " ".join(words[:_MAX_SUMMARY_WORDS]) + "..."


def _direct_summary(note: str, turns: list[dict]) -> str | None:
    """The recap when the captured text is already short enough to *be* it.

    The worker's completion note wins — it is the worker's own deliberate
    one-liner about its own phase, so paraphrasing it through a model can only
    lose information. Failing that, a short final turn serves. Anything longer
    goes to the model. This is why a typical campaign spends far fewer calls than
    it has phases.
    """
    note_plain = _plain(note)
    if note_plain and _words(note_plain) <= _SHORT_ENOUGH_WORDS:
        return note_plain
    if not note_plain and turns:
        last = _plain(str(turns[-1].get("text", "")))
        if last and _words(last) <= _SHORT_ENOUGH_WORDS:
            return last
    return None


def _material(note: str, turns: list[dict]) -> str:
    """The model's input: the completion note plus recent turns, newest kept.

    Budgeted from the *back*: when the turns overflow
    :data:`_MAX_MATERIAL_CHARS` the oldest are dropped first, because the last
    thing a worker said is the thing the recap is about. The note is never
    dropped — it is the smallest and most informative piece.
    """
    head = f"[completion note] {note.strip()}" if note.strip() else ""
    blocks: list[str] = []
    for rec in turns:
        text = str(rec.get("text", "")).strip()
        if not text:
            continue
        blocks.append(f"[turn] {text}")
    budget = _MAX_MATERIAL_CHARS - len(head)
    kept: list[str] = []
    for block in reversed(blocks):
        if budget - len(block) < 0:
            break
        kept.append(block)
        budget -= len(block)
    kept.reverse()
    return "\n\n".join([p for p in (head, *kept) if p])


# -- the model call -------------------------------------------------------
def _child_env(cfg: Config) -> dict[str, str]:
    """Environment for a recap subprocess, with this run's worker markers removed.

    If the ``Stop`` hook is ever registered in *user* settings rather than only in
    ``worker_settings``, a recap's own ``claude -p`` session would fire it and
    append its output to the very phase file it is summarising. Dropping the
    markers makes the hook a no-op there, so a recap can never feed itself.
    """
    env = dict(os.environ)
    for key in (cfg.env_marker, "SWARM_PHASE", "SWARM_STATE_DIR"):
        env.pop(key, None)
    return env


def _seam_summary(cfg: Config, prompt: str, seam: str) -> tuple[str | None, str | None]:
    """The named env seam: prompt on stdin, answer on stdout (test seam)."""
    cmd = os.environ.get(seam)
    if not cmd:
        return None, "no-seam"
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", cmd],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_S,
            env=_child_env(cfg),
        )
    except subprocess.TimeoutExpired:
        return None, "seam-timeout"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"seam-failed: {exc}"
    if proc.returncode != 0:
        return None, f"seam-exit-{proc.returncode}"
    return proc.stdout.strip() or None, None


def _api_summary(
    cfg: Config, prompt: str, model: str
) -> tuple[str | None, str | None]:
    """One Messages API call over stdlib urllib (the package has no deps).

    Only reachable when ``ANTHROPIC_API_KEY`` is set. It is preferred when it is,
    because it costs a fraction of the ``claude -p`` path — a bare prompt instead
    of a whole CLI harness.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None, "no-api-key"
    base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    url = f"{base}/v1/messages" if base else API_URL
    body = json.dumps(
        {
            "model": model,
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "content-type": "application/json",
            "anthropic-version": API_VERSION,
            "x-api-key": key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_API_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, f"api-http-{exc.code}"
    except (OSError, ValueError) as exc:
        return None, f"api-failed: {exc}"
    chunks = [
        c.get("text", "")
        for c in data.get("content", [])
        if isinstance(c, dict) and c.get("type") == "text"
    ]
    return " ".join(chunks).strip() or None, None


def _cli_summary(
    cfg: Config, prompt: str, model: str
) -> tuple[str | None, str | None]:
    """``claude -p`` — the live path. No API key, reuses the CLI's OAuth login.

    Run from the project directory on purpose: it is a directory claude already
    trusts, so the call can never stall on a first-run trust prompt the way a
    fresh state dir would.
    """
    if shutil.which("claude") is None:
        return None, "no-claude-binary"
    cwd = cfg.project_dir if cfg.project_dir.is_dir() else cfg.state_dir
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--model", model, "--output-format", "json"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_S,
            env=_child_env(cfg),
        )
    except subprocess.TimeoutExpired:
        return None, "model-timeout"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"model-spawn-failed: {exc}"
    if proc.returncode != 0:
        return None, f"model-exit-{proc.returncode}"
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        # No --output-format json support (older CLI): the raw stdout is the answer.
        return proc.stdout.strip() or None, None
    if isinstance(data, dict):
        if data.get("is_error"):
            return None, "model-error"
        result = data.get("result")
        if isinstance(result, str):
            return result.strip() or None, None
    return None, "model-unparsable"


def ask(
    cfg: Config, prompt: str, *, seam: str, model: str = MODEL
) -> tuple[str | None, str | None]:
    """One cheap headless model call. Returns ``(answer, reason)``; never raises.

    Back-ends in preference order: the ``seam`` env var (the hermetic-test stand-in
    for the model), the direct API when a key exists, then ``claude -p``. Each is
    skipped rather than tried when it is not configured, so adding an
    ``ANTHROPIC_API_KEY`` later switches paths with no code change.

    Taken out of :func:`_model_summary` so the operator triage shares this chain
    instead of copying it — including the rule a copy would get wrong, that a key
    which is present but broken falls through to the CLI (a different credential)
    rather than costing the caller its answer.
    """
    if os.environ.get(seam):
        return _seam_summary(cfg, prompt, seam)
    # A CLI alias (`haiku`) is not a Messages API model id, so only a full
    # `claude-*` id can take the API path; an alias goes straight to the CLI.
    if os.environ.get("ANTHROPIC_API_KEY") and model.startswith("claude-"):
        answer, reason = _api_summary(cfg, prompt, model)
        if answer:
            return answer, None
        cli, cli_reason = _cli_summary(cfg, prompt, model)
        return cli, None if cli else (cli_reason or reason)
    return _cli_summary(cfg, prompt, model)


def _model_summary(
    cfg: Config, phase: str, status: str | None, material: str
) -> tuple[str | None, str | None]:
    """Ask a model for the 1-2 sentences. Returns ``(summary, reason)``."""
    prompt = _PROMPT.format(phase=phase, status=status or "unknown", material=material)
    return ask(cfg, prompt, seam="SWARM_RECAP_CMD")


# -- persistence ----------------------------------------------------------
def _write(cfg: Config, rec: Recap) -> None:
    """Atomically persist one recap. Best-effort — a full disk loses the recap,
    not the phase."""
    try:
        recaps_dir(cfg).mkdir(parents=True, exist_ok=True)
        dest = recap_path(cfg, rec.phase)
        tmp = dest.with_name(f".{dest.name}.tmp")
        tmp.write_text(json.dumps(rec.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp, dest)
    except OSError:
        pass


def load(cfg: Config, phase: str) -> Recap | None:
    """The stored recap for ``phase``, or ``None``. Never raises."""
    try:
        data = json.loads(recap_path(cfg, phase).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    rec = Recap.from_dict(data)
    return rec if rec.phase else None


def load_all(cfg: Config) -> list[Recap]:
    """Every stored recap, newest first — the searchable worker history.

    Newest-first because that is the order a history is read in. Keyed access for
    a per-phase dashboard cell is ``{r.phase: r for r in load_all(cfg)}``.
    """
    out: list[Recap] = []
    try:
        entries = sorted(recaps_dir(cfg).glob("*.json"))
    except OSError:
        return out
    for entry in entries:
        if entry.name.startswith("."):
            continue
        rec = load(cfg, entry.stem)
        if rec is not None:
            out.append(rec)
    out.sort(key=lambda r: r.ts, reverse=True)
    return out


# -- the entry point ------------------------------------------------------
def summarize(
    cfg: Config, phase: str, *, on_demand: bool = False, force: bool = False
) -> Recap:
    """Produce (and store) the recap for ``phase``. Never raises.

    Called at exactly two moments — when a phase completes, and when the owner
    asks. ``on_demand=True`` always regenerates, because regenerating is the
    entire point of asking; the completion path instead reuses an existing
    *successful* recap unless ``force`` is set, so a re-run of ``swarm done``
    (or a supervisor restart replaying a sentinel) cannot bill a second model
    call for a line that already exists. A previously *failed* recap is always
    retried — that is a blank cell waiting to be filled, not a result.
    """
    status, note = sentinel(cfg, phase)
    source = "on-demand" if on_demand else "completion"

    if not on_demand and not force:
        existing = load(cfg, phase)
        if existing is not None and existing.summary:
            return existing

    turns = read_turns(cfg, phase)
    material = _material(note, turns)
    rec = Recap(phase=phase, status=status, raw=material, ts=time.time(), source=source)
    if not material:
        rec.reason = "no-turns-and-no-sentinel"
        _write(cfg, rec)
        return rec

    direct = _direct_summary(note, turns)
    if direct:
        rec.summary = _clamp(direct)
    else:
        summary, reason = _model_summary(cfg, phase, status, material)
        if summary:
            rec.summary = _clamp(_plain(summary))
        else:
            rec.reason = reason or "model-empty"
    _write(cfg, rec)
    return rec
