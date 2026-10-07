"""Subagents: what model and effort every subagent of a swarm session runs on.

A session's subagents would otherwise run on whatever the session runs on, at
its effort: a lead on Opus fanned its reading and building out to Opus, and the
built-in ``Explore`` ignores ``CLAUDE_CODE_SUBAGENT_MODEL`` altogether. So every
session the swarm opens through :func:`launch._worker_shell` gets two things,
both checked on Claude Code 2.1.293 against the requests it sends:

* ``--agents`` (:func:`agents`): the built-in ``general-purpose``, ``Explore``
  and ``Plan`` redefined, and a ``builder``, each with ``[worker]
  subagent_model`` and ``subagent_effort``. A definition's ``model`` and
  ``effort`` are what its subagent runs with, built-ins included.
* ``CLAUDE_CODE_SUBAGENT_MODEL`` and ``CLAUDE_CODE_SUBAGENT_MODEL_FORCE`` in the
  session's settings ``env`` (:func:`env`): the model of every subagent, over a
  model the session names itself and over the definitions. There is no Opus
  builder: a lead that sent "hard" rows to one sent every row to it (owner
  2026-10-08: subagents are always Sonnet).

A subagent starts from its own prompt, never the lead's context (only a
``fork`` would inherit it), so a builder pays only for what its brief says.
"""

from __future__ import annotations

import json

from .config import Config

#: The redefined built-ins: their own words, short, and their own tools.
_BUILTINS = {
    "general-purpose": (
        "General-purpose agent for researching, searching and multi-step tasks.",
        "You are a general-purpose agent. Do the task you are given with the tools you have,"
        " then reply with the result, briefly: the answer, the files that matter, and"
        " anything left unresolved.",
        None,
    ),
    "Explore": (
        "Read-only search agent: finds files, code and facts across the tree and returns"
        " conclusions, not file dumps.",
        "You are a read-only search agent. Find what you are asked with Glob, Grep, Read and"
        " read-only Bash. Never edit or create files. Reply with the conclusion and"
        " file:line pointers, briefly.",
        ["Glob", "Grep", "Read", "Bash"],
    ),
    "Plan": (
        "Read-only planning agent: designs an implementation plan for a task.",
        "You are a planning agent. Read what you need (Glob, Grep, Read, read-only Bash);"
        " never edit. Reply with a short step-by-step plan, the files it touches and the"
        " risks.",
        ["Glob", "Grep", "Read", "Bash"],
    ),
}

#: What a builder is: one row, no commits, a short reply.
BUILDER = (
    "You build one ledger row in this worktree, as your brief says: make the change, run"
    " the targeted tests and the gate command it names, and fix what fails. Never commit,"
    " push, run `swarm done` or edit the ledger, phase history or lessons; the lead records."
    " Read narrowly (grep, line ranges), not whole files. Reply in at most 10 lines: what changed, files, tests run and their result,"
    " anything unresolved. Details go to a file under $TMPDIR, not into the reply."
)


def _one(cfg: Config, description: str, prompt: str, model: str,
         tools: list[str] | None = None) -> dict:
    out: dict = {"description": description, "prompt": prompt}
    if tools:
        out["tools"] = tools
    if model:
        out["model"] = model
    if cfg.subagent_effort:
        out["effort"] = cfg.subagent_effort
    return out


def agents(cfg: Config) -> dict:
    """The ``--agents`` definitions of a session."""
    out = {name: _one(cfg, desc, prompt, cfg.subagent_model, tools)
           for name, (desc, prompt, tools) in _BUILTINS.items()}
    out["builder"] = _one(cfg, "Builds one ledger row from the lead's brief.", BUILDER,
                          cfg.subagent_model)
    return out


def env(cfg: Config) -> dict[str, str]:
    """The settings ``env`` that pins the model of every subagent."""
    if not cfg.subagent_model:
        return {}
    return {"CLAUDE_CODE_SUBAGENT_MODEL": cfg.subagent_model,
            "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "1"}


def agents_arg(cfg: Config) -> str:
    """:func:`agents` as the JSON ``--agents`` takes."""
    return json.dumps(agents(cfg), separators=(",", ":"))
