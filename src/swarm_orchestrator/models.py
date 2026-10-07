"""A row's worker model.

A ledger row may name the model its worker runs on (``model:`sonnet```). A row
that names none, or names the model ``[worker].worker_cmd`` already carries, runs
on the swarm's own: the command exactly as configured. For any other model the
launcher swaps ``--model`` in that command, and the worker is told, in its
system prompt, that it may hand the phase back (``swarm escalate``).

A hand-back is recorded here before anything else happens (``handup/<phase>``
in the state dir): it is what makes the next launch use the swarm's own model at
once, whether or not the ledger edit that says so has landed yet, and what makes
a second hand-back of the same row impossible.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from pathlib import Path

from . import ledger as ledger_mod
from .config import Config

#: The worker's env var naming the model its row asked for ("" = the swarm's own).
ENV = "SWARM_MODEL"
#: A model as claude takes it: an alias (``sonnet``) or a full id.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}$")
#: The word that clears a row's model on the command line.
DEFAULT_WORD = "default"
#: A reason shorter than this says nothing a reader can act on.
MIN_WHY_CHARS = 30


def valid(name: str) -> bool:
    return bool(_NAME_RE.match(name)) and name != DEFAULT_WORD


#: What the family aliases name on the CLI the swarm runs (Claude Code 2.1.293:
#: ``claude -p --model opus`` answers as ``claude-opus-5-5``). A row that says
#: ``opus`` under a ``worker_cmd`` naming ``claude-opus-5-5`` runs on the swarm's
#: own model, not on a model of its own.
ALIASES = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
           "haiku": "claude-haiku-5-5"}


def same(a: str, b: str) -> bool:
    """``a`` and ``b`` name one model (an alias and the id it stands for)."""
    return ALIASES.get(a, a) == ALIASES.get(b, b)


def _tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def default(cfg: Config) -> str:
    """The model ``worker_cmd`` names, or "" when it leaves that to claude."""
    tokens = _tokens(cfg.worker_cmd)
    for k, tok in enumerate(tokens):
        if tok == "--model" and k + 1 < len(tokens):
            return tokens[k + 1]
        if tok.startswith("--model="):
            return tok[len("--model="):]
    return ""


def swap(cmd: str, model: str) -> str:
    """``cmd`` with its ``--model`` set to ``model`` (added when it has none)."""
    tokens = _tokens(cmd)
    out: list[str] = []
    done = False
    k = 0
    while k < len(tokens):
        tok = tokens[k]
        if tok == "--model" and k + 1 < len(tokens):
            out += ["--model", model]
            k += 2
            done = True
            continue
        if tok.startswith("--model="):
            out.append(f"--model={model}")
            done = True
        else:
            out.append(tok)
        k += 1
    if not done:
        out += ["--model", model]
    return " ".join(shlex.quote(t) for t in out)


def _ledger_text(cfg: Config) -> str:
    try:
        return (cfg.project_dir / cfg.ledger).read_text(encoding="utf-8")
    except OSError:
        return ""


def overrides(cfg: Config, text: str | None = None) -> dict[str, str]:
    """``{row: model}`` for every row that runs on a model other than the
    swarm's own: its ``model:`` field, unless it names the default or the row
    was handed back."""
    own = default(cfg)
    rows = ledger_mod.models(_ledger_text(cfg) if text is None else text)
    gone = handed_up(cfg)
    return {p: m for p, m in rows.items()
            if not same(m, own) and p not in gone and valid(m)}


def override(cfg: Config, phase: str, text: str | None = None) -> str:
    """The model ``phase`` launches on when it is not the swarm's own, else ""."""
    own = default(cfg)
    model = ledger_mod.models(_ledger_text(cfg) if text is None else text).get(phase, "")
    if not model or same(model, own) or not valid(model) or handup(cfg, phase) is not None:
        return ""
    return model


# -- hand-backs -------------------------------------------------------------
def _dir(cfg: Config) -> Path:
    return Path(cfg.state_dir) / "handup"


def handup(cfg: Config, phase: str) -> dict | None:
    """The record of ``phase``'s hand-back, or None when it never made one."""
    try:
        got = json.loads((_dir(cfg) / f"{phase}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def handed_up(cfg: Config) -> set[str]:
    try:
        return {p.stem for p in _dir(cfg).glob("*.json")}
    except OSError:
        return set()


def record_handup(cfg: Config, phase: str, frm: str, to: str, why: str) -> None:
    """Write ``phase``'s hand-back; from here on it launches on the swarm's model."""
    path = _dir(cfg) / f"{phase}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"phase": phase, "from": frm, "to": to, "why": why,
                               "ts": time.time(), "settled": False}), encoding="utf-8")
    os.replace(tmp, path)


def settle(cfg: Config, phase: str) -> None:
    """The supervisor ended the session and freed the slot: nothing is owed."""
    rec = handup(cfg, phase)
    if rec is None:
        return
    rec["settled"] = True
    path = _dir(cfg) / f"{phase}.json"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, path)


def unsettled(cfg: Config) -> list[str]:
    """Hand-backs whose poke nobody acted on yet."""
    return sorted(p for p in handed_up(cfg) if not (handup(cfg, p) or {}).get("settled"))


# -- what the worker is told ---------------------------------------------------
def brief(model: str, own: str, phase: str) -> str:
    """The lines added to the system prompt of a worker on a row's own model."""
    other = own or "the swarm's default model"
    return (
        f"You are building phase {phase} as {model}. The swarm normally builds with {other};"
        " this phase was judged to be one you can build correctly on your own, and you are"
        " expected to build it yourself, start to finish. Being hard, long, tedious or"
        " unfamiliar is not a reason to give it away."
        f" One exit exists. If the phase turns out to be a different kind of work than its row"
        f" says, you may hand it to {other} with"
        f" `swarm escalate {phase} \"<what you found, and why it needs {other}>\"`."
        " That is right only when one of these is true and you can say which:"
        " (1) the cause of the problem is still unknown after you have really investigated it;"
        " (2) the row leaves open a design or architecture decision that later work depends on;"
        " (3) the change turned out to reach across repos, a shared contract or wire format,"
        " or far more of a subsystem than the row names;"
        " (4) it is correctness-critical code (concurrency, ordering, data loss, security,"
        " money, live data) where you cannot convince yourself your change is right;"
        " (5) you have tried twice and the gates still fail for a reason you do not understand."
        " Decide early: handing up after hours of work wastes all of it. When you hand up,"
        " your session ends at once, nothing you wrote is merged, and a fresh"
        f" {other} worker starts the phase from a clean tree with your reason in the row's"
        " history; so make the reason specific (what you found, what you ruled out, where"
        " the difficulty is). A phase can be handed up once. Do not hand up and also run"
        " `swarm done`."
    )
