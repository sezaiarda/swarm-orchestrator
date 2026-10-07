"""Lean sessions: a spawned ``claude`` reads the project's setup, not the owner's.

Every session the swarm opens used to boot with the owner's whole personal
setup: ``~/.claude/CLAUDE.md``, the auto-memory index, every enabled plugin's
skills, agents and MCP servers, the user-level skills, agents and commands, and
the claude.ai connectors. None of it is about the project, all of it is in the
first prompt of every worker, operator and recap, and measured on Claude Code
2.1.292 it was about 40% of a bare session's first-turn input (31.3k tokens
against 18.7k lean).

The one switch that drops all of the user-level setup at once is
``--setting-sources project,local``: the user source is what ``~/.claude/CLAUDE.md``,
``~/.claude/{skills,agents,commands}``, the enabled plugins and the user-scope
MCP servers all hang off. The project's own CLAUDE.md, ``.claude/settings*.json``,
``.claude/commands`` (``/prime``) and ``.mcp.json`` stay.

But the same switch drops the owner's ``~/.claude/settings.json``, which is how
a session runs, not what it knows: ``bypassPermissions``, the model, the
commit attribution, ``env``. So that file is carried over in ``--settings``,
less its personal-context keys (:data:`PERSONAL`). ``--settings`` outranks the
project's files, so theirs are merged over the carried copy here, keeping the
project's word final the way Claude Code itself would. Then :data:`LEAN` (no
auto-memory, no claude.ai connectors) and last the swarm's own settings (the
worker settings and the meters tap). Claude Code takes one ``--settings``, the
last given, so everything goes in that one.

Rejected: ``--bare`` (never reads the OAuth login), ``--safe-mode`` (drops the
project's CLAUDE.md and commands too), ``--disable-slash-commands`` (drops
``/prime``), ``--strict-mcp-config`` (drops the project's ``.mcp.json``; the user
scope already goes with the user source), a dedicated ``CLAUDE_CONFIG_DIR``
(moves the login), and per-key overrides on the user source
(``enabledPlugins``/``claudeMdExcludes``) which leave the user skills, agents
and commands listed.

``[swarm] lean_sessions = false`` turns it off. A command that picks its own
``--setting-sources``, or ``--settings`` that are not a readable JSON object,
is left as configured.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Iterable

#: The owner's own settings, carried into a lean session (less :data:`PERSONAL`).
OWNER_SETTINGS = Path.home() / ".claude" / "settings.json"

#: What a lean session loads from disk: the project's settings files, not the user's.
SOURCES = "project,local"

#: Owner settings that are personal context rather than how a session runs.
PERSONAL = ("enabledPlugins", "extraKnownMarketplaces", "claudeMdExcludes")

#: What every lean session gets on top: no auto-memory, no claude.ai connectors.
LEAN: dict = {"autoMemoryEnabled": False, "env": {"ENABLE_CLAUDEAI_MCP_SERVERS": "false"}}


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _merge(base: dict, over: dict) -> dict:
    """``over`` on ``base``: objects merge, lists join, anything else ``over`` wins."""
    out = dict(base)
    for key, val in over.items():
        old = out.get(key)
        if isinstance(old, dict) and isinstance(val, dict):
            out[key] = _merge(old, val)
        elif isinstance(old, list) and isinstance(val, list):
            out[key] = old + [v for v in val if v not in old]
        else:
            out[key] = val
    return out


def _own(settings: str) -> dict | None:
    """The swarm's ``--settings`` (inline JSON or a file) as an object, else None."""
    if not settings:
        return {}
    try:
        data = json.loads(settings)
    except ValueError:
        data = _read(Path(settings).expanduser()) or None
    return data if isinstance(data, dict) else None


def settings(cwd: Path, own: dict | None = None, owner: Path | None = None) -> dict:
    """The one settings object a lean session in ``cwd`` runs with."""
    out = {k: v for k, v in _read(owner or OWNER_SETTINGS).items() if k not in PERSONAL}
    for name in ("settings.json", "settings.local.json"):
        out = _merge(out, _read(cwd / ".claude" / name))
    return _merge(_merge(out, LEAN), own or {})


def args(cfg, cwd: Path, settings_arg: str = "", cmd: Iterable[str] = ()) -> list[str]:
    """The flags a session in ``cwd`` gets: lean ones, or plain ``--settings``.

    ``settings_arg`` is the swarm's own ``--settings`` value ("" = none), ``cmd``
    the command they are appended to (a configured one may pick its sources)."""
    plain = ["--settings", settings_arg] if settings_arg else []
    own = _own(settings_arg)
    picks = any(t == "--setting-sources" or t.startswith("--setting-sources=") for t in cmd)
    if not cfg.lean_sessions or own is None or picks:
        return plain
    merged = json.dumps(settings(cwd, own), separators=(",", ":"))
    return ["--setting-sources", SOURCES, "--settings", merged]


def shell(cfg, cwd: Path, settings_arg: str = "", cmd: str = "") -> str:
    """:func:`args` as a shell fragment (leading space) for a command string."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    return "".join(" " + shlex.quote(a) for a in args(cfg, cwd, settings_arg, tokens))
