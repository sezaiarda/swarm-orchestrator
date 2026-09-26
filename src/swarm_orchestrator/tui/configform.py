"""The Config tab as a form: one row per setting, not a wall of TOML.

The tab this replaces was a ``TextArea`` holding the literal ``.swarm.toml``.
That is a file viewer wearing a dashboard's clothes: it shows the owner the same
bytes they already have in ``$EDITOR``, asks them to remember from memory what
``done_grace_s`` does, and answers none of the only question that matters at 2am
— *if I change this, when does it actually take effect?*

So this is a form. Every ``init`` field of :class:`~swarm_orchestrator.config.Config`
gets a row: a typed widget, the live value, one line of what it does, and its
reload class taken straight from :data:`~swarm_orchestrator.reload.POLICY` — HOT
(now), NEXT (next worker/master launch), RESTART (needs ``swarm down``/``up``).
That classification is never re-derived here. POLICY is the single source of
truth for it, and :func:`reload.coverage_gap` already refuses to let a new Config
field exist without one, so this screen cannot silently fall behind the config.

Three things are load-bearing.

**Comments survive, byte for byte.** A ``.swarm.toml`` is often over a hundred lines
and most of them are the owner explaining *why* a value is what it is — which
reboot, which OOM, which dated decision, which correction to an earlier comment
that turned out to be wrong. A form that round-tripped that file through
``tomllib`` and a serializer would delete every word of it, which is far worse
than the textarea it replaces. There is no ``tomlkit`` here and there is not
going to be one, so writing is a *surgical line rewrite*: find the line that
assigns the key inside its table, replace only the value text between the ``=``
and the comment, and leave ordering, alignment and every comment untouched. A key
that does not exist yet is appended at the bottom of its table, and only ever
after a run of blank lines — never into the middle of somebody's comment block.

**Env overrides are shown, not fought.** :func:`config.load` re-layers ``SWARM_*``
over the file on every call, so for a pinned field the file is decorative and an
edit here would be a lie. Those rows are disabled and name the variable that owns
them. The detection is not re-implemented either: diffing a config against
*itself* through :func:`reload.diff` yields exactly the pinned set, including the
subtlety that a malformed numeric override does *not* shadow (``_int_env`` walks
past one to the file value).

**Nothing here may raise.** This is a tab in an always-on cockpit. A missing or
unparseable ``.swarm.toml`` is a normal state of the world — the owner is halfway
through editing it in another window — and must render as a message with apply
disabled, never as a traceback that takes the whole dashboard down with it.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Input, RadioButton, RadioSet, Select, Static, Switch
from textual import work

from .. import reload as reload_mod
from ..config import EFFORTS, OPERATOR_NOTIFY, PINGS, load as load_config
from ..reload import HOT, NEXT, POLICY, RESTART
from ..tmux import LAYOUTS
from . import probes
from .dash import Dash
from .data import read_state
from .theme import ACCENT, BAD, COLOR, INFO, MUTED, OK, WARN, Body, Panel, paint

# ==========================================================================
# The TOML value writer.
#
# Pure, Textual-free and separately testable, because it is the part that can
# destroy something the owner cannot get back.
# ==========================================================================

# `[table]` / `[[array]]`, optionally with a trailing comment. Deliberately not
# a general TOML parser: it only has to find where one table's lines start and
# stop, and anything it fails to recognise degrades to "key not found" -> append.
_HEADER = re.compile(r"^\s*\[\[?\s*([^\[\]]+?)\s*\]\]?\s*(?:#.*)?$")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _split(text: str) -> tuple[list[str], bool]:
    """Lines without their terminators, plus whether the file ended with one.

    Splitting this way (rather than ``splitlines(keepends=True)``) keeps every
    column index in a line equal to its offset in that line, which is what the
    value scanner works in.
    """
    trailing = text.endswith("\n")
    body = text[:-1] if trailing else text
    return (body.split("\n") if body else []), trailing


def _join(lines: list[str], trailing: bool) -> str:
    if not lines:
        return "\n" if trailing else ""
    return "\n".join(lines) + ("\n" if trailing else "")


def _bounds(lines: list[str], table: str) -> tuple[int | None, int]:
    """``(first_line_of_table, one_past_last)``; ``(None, len)`` if absent."""
    start: int | None = None
    for i, line in enumerate(lines):
        m = _HEADER.match(line)
        if not m:
            continue
        if start is None:
            if m.group(1).strip() == table:
                start = i + 1
        else:
            return start, i
    return (start, len(lines)) if start is not None else (None, len(lines))


def _find_key(lines: list[str], lo: int, hi: int, key: str) -> tuple[int, int] | None:
    """``(row, column_just_after_the_equals_and_its_padding)`` for ``key``."""
    q = re.escape(key)
    pattern = re.compile(rf"^\s*(?:{q}|\"{q}\"|'{q}')\s*=\s*")
    for i in range(lo, hi):
        if lines[i].lstrip().startswith("#"):
            continue
        m = pattern.match(lines[i])
        if m:
            return i, m.end()
    return None


def _skip_string(lines: list[str], row: int, col: int) -> tuple[int, int]:
    """Position just past the string that opens at ``lines[row][col]``."""
    line = lines[row]
    quote = line[col]
    if line[col : col + 3] == quote * 3:  # multi-line string
        j = col + 3
        while row < len(lines):
            k = lines[row].find(quote * 3, j)
            if k != -1:
                return row, k + 3
            row, j = row + 1, 0
        return len(lines) - 1, len(lines[-1])
    basic = quote == '"'
    j = col + 1
    while j < len(line):
        if basic and line[j] == "\\":
            j += 2
            continue
        if line[j] == quote:
            return row, j + 1
        j += 1
    return row, len(line)  # unterminated: stop at end of line rather than loop


def _scan_value(lines: list[str], row: int, col: int) -> tuple[int, int]:
    """Where the value starting at ``(row, col)`` ends, exclusive.

    Stops at the first unquoted ``#`` or end of line at bracket depth 0, so an
    inline comment is never swallowed into the value and a ``#`` *inside* a
    string never ends it early. Arrays and inline tables are followed across
    lines, and a comment inside an array runs to that line's end.
    """
    depth = 0
    r, c = row, col
    while r < len(lines):
        line = lines[r]
        if c >= len(line):
            if depth == 0:
                return r, len(line)
            r, c = r + 1, 0
            continue
        ch = line[c]
        if ch in "\"'":
            r, c = _skip_string(lines, r, c)
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth < 0:
                return r, c
        elif ch == "#":
            if depth == 0:
                return r, c
            r, c = r + 1, 0
            continue
        c += 1
    return len(lines) - 1, len(lines[-1])


def _string(value: str, prefer_literal: bool = False) -> str:
    """A TOML string, keeping the file's own quoting habit where it can.

    ``worker_settings`` is written as a literal ``'...'`` string in every real
    config precisely because its contents are full of double quotes; re-emitting them as escaped basic strings would be valid TOML and
    completely unreadable, and the owner would have to undo it by hand.
    """
    if "\n" not in value and "'" not in value and (prefer_literal or '"' in value or "\\" in value):
        return f"'{value}'"
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def toml_literal(value: Any, prefer_literal: bool = False) -> str:
    """Serialize one value as a TOML literal. ``bool`` before ``int`` — it is one."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        inner = ", ".join(
            f"{k if _BARE_KEY.match(str(k)) else _string(str(k))} = {toml_literal(v)}"
            for k, v in value.items()
        )
        return "{ " + inner + " }"
    return _string(str(value), prefer_literal)


def toml_set(text: str, table: str, key: str, value: Any) -> str:
    """Rewrite ``table.key`` to ``value``, preserving everything else exactly.

    The whole point of this function is what it does *not* touch. Only the value
    text between the ``=`` and the inline comment is replaced; the key's own
    padding, the comment, its column, and every other line of the file come
    through unchanged. Where the new literal is shorter than the old one the gap
    is re-padded so the ``#`` stays in its column — configs in the wild align a
    whole block of continuation comments under that character, and shifting it
    would visibly wreck a comment we promised not to touch.
    """
    lines, trailing = _split(text)
    lo, hi = _bounds(lines, table)
    if lo is None:
        return _append_table(lines, trailing, table, key, value)
    hit = _find_key(lines, lo, hi, key)
    if hit is None:
        return _append_key(lines, trailing, lo, hi, key, value)

    row, col = hit
    end_row, end_col = _scan_value(lines, row, col)
    head, tail = lines[row][:col], lines[end_row][end_col:]
    if end_row == row:
        raw = lines[row][col:end_col]
        literal = toml_literal(value, prefer_literal=raw.lstrip()[:1] == "'")
        if tail.lstrip().startswith("#"):
            gap = " " * max(1, len(raw) - len(literal))
        else:
            gap = raw[len(raw.rstrip()) :]
    else:
        literal, gap = toml_literal(value), ""
    lines[row : end_row + 1] = [head + literal + gap + tail]
    if end_row == row and tail.lstrip().startswith("#"):
        _realign(lines, row + 1, col + len(raw), col + len(literal) + len(gap))
    return _join(lines, trailing)


def _realign(lines: list[str], start: int, old_col: int, new_col: int) -> None:
    """Follow a hanging comment block to its assignment's new width.

    Real configs indent a multi-line comment under the ``#`` of the line it
    belongs to — a long ``[git].repos`` note runs two lines that way, and
    ``done_grace_s`` six. A value that grows past that column takes its own
    comment right with it; without this the rest of the block stays behind and
    the paragraph the owner wrote comes apart. Only leading whitespace moves —
    the comment text itself is still byte-identical.
    """
    if new_col == old_col:
        return
    i = start
    while i < len(lines):
        line = lines[i]
        if line[:old_col].strip() or not line[old_col:].startswith("#"):
            return
        lines[i] = " " * new_col + line[old_col:]
        i += 1


def _append_key(
    lines: list[str], trailing: bool, lo: int, hi: int, key: str, value: Any
) -> str:
    """Add a missing key at the bottom of an existing table.

    The insertion point walks back over *blank* lines only. Walking back over
    comments too would look tidier and would sometimes split a comment block from
    the assignment it documents — the one thing this module exists to prevent.
    """
    idx = hi
    while idx > lo and lines[idx - 1].strip() == "":
        idx -= 1
    lines.insert(idx, f"{key} = {toml_literal(value)}")
    return _join(lines, trailing)


def _append_table(
    lines: list[str], trailing: bool, table: str, key: str, value: Any
) -> str:
    block = [] if not lines or lines[-1].strip() == "" else [""]
    block += [f"[{table}]", f"{key} = {toml_literal(value)}"]
    return _join(lines + block, trailing)


def toml_set_many(text: str, edits: dict[tuple[str, str], Any]) -> str:
    """Apply ``{(table, key): value}`` one at a time.

    Sequential rather than clever: each pass re-splits the (already rewritten)
    text, so an append that shifts every later line number cannot corrupt the
    next edit's coordinates.
    """
    for (table, key), value in edits.items():
        text = toml_set(text, table, key, value)
    return text


# ==========================================================================
# The settings model: what each field is, and one line of what it does.
#
# The reload class, the TOML table, the key and the env var all come from
# POLICY. What POLICY does not carry is the *widget* (it is a policy table, not
# a UI) and a plain-English line of what the setting is for — which is the whole
# reason the owner should not have to remember what `done_grace_s` means. Those
# two live here, and `test_tui_config.py` asserts this table covers POLICY
# exactly, so a field can never reach Config without reaching this screen.
# ==========================================================================

INT = "int"
STR = "str"
BOOL = "bool"
LIST = "list"
CHOICE = "choice"
FROZEN = "frozen"  # shown, never editable here: not a file value, or not a scalar

# The order the tables are drawn in — the order the file itself uses.
SECTION_ORDER = ("swarm", "worker", "tasks", "telegram", "tmux", "build", "gc", "git",
                 "operator", "ask", "overseer", "tui", "web", "(cli)")


@dataclass(frozen=True)
class Setting:
    """One row of the form: a Config field, a widget kind, and a description."""

    name: str
    kind: str
    doc: str
    choices: tuple[str, ...] = ()
    minimum: int | None = None

    @property
    def policy(self):
        return POLICY[self.name]

    @property
    def table(self) -> str:
        """The TOML table name, ``[swarm]`` -> ``swarm``."""
        return self.policy.section.strip("[]")

    @property
    def key(self) -> str:
        return self.policy.key

    @property
    def klass(self) -> str:
        return self.policy.klass

    @property
    def env(self) -> str | None:
        return self.policy.env

    @property
    def why(self) -> str:
        """POLICY's reason for the reload class — the tooltip, never re-worded."""
        return self.policy.why


FIELDS: tuple[Setting, ...] = (
    # -- [swarm] ----------------------------------------------------------
    Setting("max_workers", INT, "phases in flight at once (one pane each)", minimum=1),
    Setting("watchdog_s", INT, "seconds between liveness sweeps; 0 = off",
            minimum=0),
    Setting("master_model", STR, 'model for the master session; "" inherits'),
    Setting("master_cmd", STR, 'command the master pane runs; "" = the built-in'),
    Setting("resolver_cmd", STR, "command that opens a conflict-resolver session"),
    Setting("resolver_model", STR, 'model for the resolver; "" inherits'),
    Setting("driver", CHOICE, "tmux = real panes; bare = headless test driver",
            choices=("tmux", "bare")),
    Setting("slug", STR, "names this run's state dir — its identity on disk"),
    # -- [worker] ---------------------------------------------------------
    Setting("done_grace_s", INT,
            "seconds a worker holds its slot after `done`", minimum=0),
    Setting("park_after", INT,
            "seconds a worker may wait on you before parking", minimum=0),
    Setting("command_template", STR, "prime line typed into a new pane ({phase} expands)"),
    Setting("command_file", STR, "slash-command file the init master patches"),
    Setting("worker_cmd", STR, "command each worker pane is launched with"),
    Setting("worker_settings", STR, "settings JSON merged into each worker's `claude`"),
    Setting("worker_effort", CHOICE, 'claude --effort per worker; "" = inherit yours',
            choices=("", *EFFORTS)),
    Setting("env_marker", STR, "env var carrying the phase name into the worker"),
    Setting("ready_marker", STR,
            '"pane booted" banner; "" = the claude version'),
    # -- [tasks] ----------------------------------------------------------
    Setting("ledger", STR, "phase ledger the master reads (project-relative)"),
    Setting("exclude", LIST, "phases the swarm must never launch (comma-sep)"),
    # -- [telegram] -------------------------------------------------------
    Setting("telegram_notify", STR, "script that sends the swarm's own Telegram pings"),
    Setting("telegram_commands", BOOL, "answer /usage and /help sent to the swarm bot"),
    Setting("telegram_pings", CHOICE, "necessary = only what needs you; all = every ping",
            choices=PINGS),
    Setting("telegram_push_owed_grace_s", INT, "an owed push pings after this long (s)",
            minimum=0),
    # -- [tmux] -----------------------------------------------------------
    Setting("session", STR, "tmux session name — what you see in `tmux ls`"),
    Setting("tmux_layout", CHOICE,
            "pane arrangement (auto = 1 full/2 cols/3+ tiled)",
            choices=tuple(LAYOUTS)),
    Setting("tmux_panes_per_window", INT, "worker panes per window before workers-2",
            minimum=1),
    # -- [build] ----------------------------------------------------------
    Setting("build_max_concurrent", INT,
            "concurrent heavy `swarm build` runs; rest queue", minimum=0),
    Setting("build_jobs", INT, "CARGO_BUILD_JOBS handed to each build", minimum=0),
    Setting("build_cache", BOOL, "share one cargo target cache across worktrees"),
    # -- [gc] -------------------------------------------------------------
    Setting("gc_auto", BOOL, "prune build output / dead mirrors automatically"),
    Setting("gc_every_s", INT, "auto gc at most this often (s); 0 = idle-only", minimum=0),
    Setting("gc_idle_s", INT, "also once per idle stretch this long (s); 0 = off", minimum=0),
    Setting("gc_keep_days", INT, "keep build output used within N days", minimum=1),
    Setting("gc_attic_days", INT, "keep set-aside work (swarm-attic refs) N days", minimum=1),
    # -- [git] ------------------------------------------------------------
    Setting("git_isolation", CHOICE,
            "worktree = own mirror + queue; none = in place",
            choices=("worktree", "none")),
    Setting("git_main_branch", STR, "branch the integrator merges phase branches into"),
    Setting("git_repos", LIST, "globs picking the child repos a mirror includes"),
    Setting("git_auto_resolve", FROZEN,
            "glob -> union|keyed:<re>, tried before a resolver"),
    Setting("git_auto_resolve_check", FROZEN,
            "glob -> command that must pass after one"),
    # -- [operator] -------------------------------------------------------
    Setting("operator_enabled", BOOL,
            "arm autonomous sessions for `operator` finishes"),
    Setting("operator_cmd", STR, 'command an operator session runs; "" = built-in'),
    Setting("operator_model", STR, 'model for an operator session; "" inherits'),
    Setting("operator_triage_model", STR, "model that decides now vs later"),
    Setting("operator_notify", CHOICE,
            "which operator outcomes ping you",
            choices=OPERATOR_NOTIFY),
    # -- [ask] ------------------------------------------------------------
    Setting("ask_cmd", STR, 'command an ask window runs; "" = built-in'),
    Setting("ask_model", STR, 'model for an ask session; "" = master_model'),
    # -- [overseer] -------------------------------------------------------
    Setting("overseer_enabled", BOOL, "run periodic Overseer review passes"),
    Setting("overseer_cmd", STR, 'command an Overseer pass runs; "" = built-in'),
    Setting("overseer_model", STR, 'model for the Overseer; "" = master_model'),
    Setting("overseer_min_gap_s", INT, "min seconds between non-urgent passes", minimum=0),
    Setting("overseer_every_finished", INT, "a pass every N finished phases; 0 = off",
            minimum=0),
    Setting("overseer_every_s", INT, "a pass at least this often (s); 0 = off", minimum=0),
    Setting("overseer_owner_wait_s", INT, "owner-wait age that triggers a pass (s)",
            minimum=0),
    Setting("overseer_starve_s", INT, "idle-slot starvation before a pass (s)", minimum=0),
    Setting("overseer_hold_wait_s", INT, "merge hold a resolver has before a pass (s)",
            minimum=0),
    Setting("overseer_timeout_s", INT, "seconds before a hung pass is killed", minimum=1),
    # -- [tui] ------------------------------------------------------------
    Setting("tui_autostart", BOOL, "open this dashboard automatically at `swarm up`"),
    Setting("tui_cmd", STR, "command the dashboard pane is respawned with"),
    # -- [web] ------------------------------------------------------------
    Setting("web_enabled", BOOL, "start the web board at `swarm up`"),
    Setting("web_host", STR, "address the web board binds (0.0.0.0 = every interface)"),
    Setting("web_port", INT, "port the web board listens on", minimum=0),
    # -- (cli) ------------------------------------------------------------
    Setting("project_dir", FROZEN, "the project root — --project-dir or the cwd"),
)

SETTINGS: dict[str, Setting] = {s.name: s for s in FIELDS}


def by_section() -> list[tuple[str, list[Setting]]]:
    """The form's groups, in file order, each in declaration order."""
    groups: dict[str, list[Setting]] = {}
    for setting in FIELDS:
        groups.setdefault(setting.policy.section, []).append(setting)
    order = {f"[{name}]" if name != "(cli)" else name: i
             for i, name in enumerate(SECTION_ORDER)}
    return sorted(groups.items(), key=lambda kv: order.get(kv[0], len(order)))


def coverage_gap() -> set[str]:
    """POLICY fields this form does not draw. Empty is the only healthy answer."""
    return set(POLICY) ^ set(SETTINGS)


def pinned(cfg, env: dict[str, str] | None = None) -> dict[str, str]:
    """``{field: SWARM_VAR}`` for every field the environment owns.

    Delegated to :func:`reload.diff` rather than re-derived: diffing a config
    against *itself* leaves only the ENV entries, and that path already knows the
    rule a hand-rolled ``name in os.environ`` check would get wrong — a malformed
    numeric override does not shadow, because ``config._int_env`` walks past one
    to the file value.
    """
    facts = reload_mod.Facts(env=dict(os.environ if env is None else env))
    return {
        c.name: c.env
        for c in reload_mod.diff(cfg, cfg, facts)
        if c.effective == reload_mod.ENV and c.env
    }


def coerce(setting: Setting, raw: Any) -> Any:
    """A widget's raw value as the type :class:`Config` declares.

    Raises ``ValueError`` with a message meant for the status line — an empty
    integer box or a value under its floor must block apply, not be silently
    rounded into something the owner did not ask for. ``max_workers`` is the one
    that matters: ``load()`` raises on anything below 1, so writing a 0 from here
    would break every ``swarm`` command including the ones needed to undo it.
    """
    if setting.kind == BOOL:
        return bool(raw)
    if setting.kind == INT:
        text = str(raw).strip()
        if not text:
            raise ValueError(f"{setting.key} needs a number")
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"{setting.key}: {text!r} is not a number") from None
        if setting.minimum is not None and value < setting.minimum:
            raise ValueError(f"{setting.key} must be >= {setting.minimum}")
        return value
    if setting.kind == LIST:
        if isinstance(raw, (list, tuple)):
            return [str(v) for v in raw]
        return [part.strip() for part in str(raw).split(",") if part.strip()]
    if setting.kind == CHOICE:
        text = str(raw)
        if setting.choices and text not in setting.choices:
            raise ValueError(f"{setting.key} must be one of {', '.join(setting.choices)}")
        return text
    return str(raw)


def render_value(setting: Setting, value: Any) -> Any:
    """The typed value as the widget wants it: bool for a Switch, text for an Input."""
    if setting.kind == BOOL:
        return bool(value)
    if setting.kind == LIST:
        return ", ".join(str(v) for v in (value or []))
    if setting.kind == FROZEN:
        if isinstance(value, dict):
            return ", ".join(f"{k} = {v}" for k, v in value.items()) or "(none)"
        return str(value)
    return str(value)


def facts(dash: Dash) -> "reload_mod.Facts":
    """The live run as reload policy sees it, without touching the filesystem.

    Goes through :meth:`State.from_dict` rather than ``state.read`` on purpose:
    that call does ``ensure_dirs()``, and the dashboard is a reader — it must not
    create a state dir for a project that has never been run.
    """
    from ..state import State

    return reload_mod.Facts.from_state(State.from_dict(read_state(dash.cfg) or {}))


def live_config(dash: Dash):
    """The config the running supervisor actually holds, or the dash's own.

    The supervisor writes a snapshot at startup and after every reload, and it is
    the only correct "before" for a diff: ``load()`` layers this process's
    ``SWARM_*`` over the file, so re-reading the file computes the wrong old value
    for every overridden field — and once the file is edited, the old one is gone.
    """
    from .. import cli

    snapshot = getattr(cli, "_snapshot_cfg", None)
    if snapshot is not None:
        try:
            found = snapshot(dash.cfg)
        except Exception:  # noqa: BLE001 - a stale/foreign snapshot must not block a preview
            found = None
        if found is not None:
            return found
    return dash.cfg


def load_text(text: str, project_dir: Path):
    """Load candidate TOML as a :class:`Config` without touching the real file.

    The gate in front of every write. ``tomllib`` alone is not enough: a file can
    parse perfectly and still be refused by ``load()`` (``max_workers = 0``, an
    unknown ``[tmux].layout``), and writing one of those would break every
    ``swarm`` command — including the ones the owner needs to undo it.
    """
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".swarm.toml", delete=False, encoding="utf-8"
    )
    try:
        handle.write(text)
        handle.close()
        return load_config(explicit=handle.name, project_dir=str(project_dir))
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def preview(text: str, project_dir: Path, old_cfg, run_facts) -> str:
    """``swarm reload --dry-run`` for edits that are not on disk yet.

    Shelling out cannot answer this: ``swarm reload`` reads the real
    ``.swarm.toml``, so a preview through the CLI would describe the file as it
    stands, not the pending form. Loading the candidate text from a temp file and
    running the same pure :func:`reload.plan` gives the true verdict for what
    apply is about to write — which is the only preview worth having.
    """
    new = load_text(text, project_dir)
    return reload_mod.render(reload_mod.plan(old_cfg, new, run_facts))


# ==========================================================================
# The form.
# ==========================================================================

def safe(text: Any) -> str:
    """External text, made safe to hand to a markup renderer.

    Everything this form displays that it did not write itself is a markup
    injection waiting to happen, and the payloads are not hypothetical: a TOML
    error quotes the syntax it choked on, a long ``[git].auto_resolve``
    rule is the regex ``\\[[ x]\\]``, and ``swarm reload`` names every change as
    ``[swarm].max_workers``. Each of those reads as a style tag and is silently
    deleted — so the one line the owner needed is the one that disappears.

    Escaping every ``[`` rather than calling ``rich.markup.escape``: that
    function only escapes a bracket followed by ``[a-z#/@]``, while the renderer
    happily swallows ``[L]`` and ``[[ x]]`` as well. The narrower escape leaves
    exactly the values in this config unprotected.
    """
    return str(text).replace("[", r"\[")


_CHIP = {HOT: ("HOT", OK), NEXT: ("NEXT", INFO), RESTART: ("RESTART ⚠", WARN)}
_CLASS_LABEL = {HOT: "hot", NEXT: "next-launch", RESTART: "restart-only"}


class Row(Horizontal):
    """One setting: dirty mark, key, typed widget, reload class, description."""

    def __init__(self, setting: Setting, baseline: Any, pinned_by: str | None) -> None:
        super().__init__(id=f"row-{setting.name}", classes="set-row")
        self.setting = setting
        self.baseline = baseline
        self.pinned_by = pinned_by
        self.dirty = False
        self.error = ""
        self.options: tuple[str, ...] = self._options()
        # A RadioSet only learns its new value from a child's message, so it is
        # still reporting the old one for the rest of the frame in which it was
        # written. Everything else here is a reactive and updates in place. This
        # holds the intended value until the set catches up, so a write followed
        # by a read inside one frame — an apply straight after a toggle — cannot
        # silently drop the change.
        self.chosen: str | None = None

    @property
    def locked(self) -> bool:
        """Editable here at all? Env-pinned and non-scalar fields are not."""
        return self.pinned_by is not None or self.setting.kind == FROZEN

    def _options(self) -> tuple[str, ...]:
        """A CHOICE field's options, always including whatever is really in force.

        ``driver`` and ``[tmux].layout`` can hold a value this build does not list
        (an env override, a newer config). Dropping it from the options would make
        the row display something the swarm is not doing, which is the one thing a
        settings screen must never do.
        """
        if self.setting.kind != CHOICE:
            return ()
        current = str(self.baseline)
        choices = self.setting.choices
        return choices if current in choices else (*choices, current)

    def compose(self) -> ComposeResult:
        s = self.setting
        yield Static("", classes="set-mark")
        yield Static(s.key, classes="set-key")
        yield from self._value_widgets()
        label, state = _CHIP.get(s.klass, (s.klass, MUTED))
        chip = Static(paint(label, state), classes="set-klass")
        chip.tooltip = f"{s.klass.upper()}: {s.why}"
        yield chip
        doc = Static(self._doc(), classes="set-doc")
        doc.tooltip = s.doc
        yield doc

    def _value_widgets(self):
        """The value cell. A Switch gets a word beside it, on purpose.

        Textual draws a Switch as two coloured blocks and nothing else, so its
        state is carried by hue alone — unreadable at a glance from across the
        room, which is the whole brief for this dashboard, and unreadable full
        stop for anyone who cannot separate the two colours.
        """
        s, locked = self.setting, self.locked
        if s.kind == BOOL:
            return [
                Switch(value=bool(self.baseline), disabled=locked, classes="set-val"),
                Static("", classes="set-bool"),
            ]
        return [self._one_widget()]

    def _one_widget(self):
        s, locked = self.setting, self.locked
        if s.kind == FROZEN:
            return Static(
                paint(safe(render_value(s, self.baseline)), MUTED),
                classes="set-val set-flat",
            )
        if s.kind == CHOICE and len(self.options) <= 2:
            current = str(self.baseline)
            return RadioSet(
                *[RadioButton(c, value=(c == current)) for c in self.options],
                compact=True,
                disabled=locked,
                classes="set-val",
            )
        if s.kind == CHOICE:
            return Select(
                [(c, c) for c in self.options],
                value=str(self.baseline),
                allow_blank=False,
                compact=True,
                disabled=locked,
                classes="set-val",
            )
        return Input(
            value=render_value(s, self.baseline),
            type="integer" if s.kind == INT else "text",
            compact=True,
            disabled=locked,
            classes="set-val",
        )

    def _doc(self) -> str:
        if self.pinned_by:
            return paint(f"pinned by {self.pinned_by}", WARN)
        if self.setting.kind == FROZEN:
            return paint(f"{self.setting.doc} (read-only)", MUTED)
        return paint(self.setting.doc, MUTED)

    @property
    def widget(self):
        return self.query_one(".set-val")

    def read(self) -> Any:
        """The widget's value, typed. Raises ``ValueError`` on a bad entry."""
        w = self.widget
        if isinstance(w, Switch):
            return bool(w.value)
        if isinstance(w, RadioSet):
            if self.chosen is not None:
                return self.chosen
            index = w.pressed_index
            return self.options[index] if 0 <= index < len(self.options) else self.baseline
        if isinstance(w, Select):
            return self.baseline if w.value is Select.BLANK else str(w.value)
        if isinstance(w, Input):
            return coerce(self.setting, w.value)
        return self.baseline

    def write(self, value: Any) -> None:
        """Put ``value`` into the widget. Does not touch the baseline."""
        w = self.widget
        if isinstance(w, Switch):
            w.value = bool(value)
        elif isinstance(w, RadioSet):
            # Only ever switch a button ON: a RadioSet keeps its own invariant by
            # unpressing the rest. Setting the pressed one to False first — the
            # obvious way to write this — makes the set refuse the whole change,
            # so the row would silently ignore half of every toggle.
            target = str(value)
            if target in self.options:
                self.chosen = target
            for i, button in enumerate(w.query(RadioButton)):
                if i < len(self.options) and self.options[i] == target:
                    button.value = True
        elif isinstance(w, Select):
            if str(value) in self.options:
                w.value = str(value)
        elif isinstance(w, Input):
            w.value = render_value(self.setting, value)

    def seed(self, baseline: Any) -> None:
        """Adopt a new baseline, writing it into the widget unless it is dirty.

        The baseline always moves — the file may have been edited in another
        window, and a row whose pending edit now matches disk should stop
        claiming to be changed. The *widget* is only overwritten when it holds
        nothing of the owner's, because a tick that lands mid-keystroke must never
        eat what they typed.
        """
        self.baseline = baseline
        self.options = self._options()
        if not self.dirty:
            self.write(baseline)

    def restate(self) -> None:
        """Recompute dirty/invalid and repaint the marker."""
        if self.locked:
            self.dirty, self.error = False, ""
        else:
            try:
                self.dirty = self.read() != self.baseline
                self.error = ""
            except ValueError as exc:
                self.dirty, self.error = True, str(exc)
        if self.setting.kind == BOOL:
            on = bool(self.read())
            self.query_one(".set-bool", Static).update(
                paint("on" if on else "off", OK if on else MUTED)
            )
        mark = "●" if self.dirty else " "
        state = BAD if self.error else ACCENT
        self.query_one(".set-mark", Static).update(paint(mark, state))
        self.set_class(self.dirty, "-dirty")
        self.set_class(bool(self.error), "-invalid")


class Settings(Vertical):
    """The Config tab: every ``.swarm.toml`` setting as an editable row.

    Two modes, because a form of 32 live widgets that all grab keys is unusable
    from the keyboard. In **navigation** mode the panel itself holds focus and
    ``j``/``k`` move a cursor; ``enter``/``e`` hands focus to the widget under the
    cursor, and ``esc`` takes it back, restoring the value the row had when the
    edit started. Nothing is ever written until ``ctrl+s``/``f2`` — bound as a
    pair because many terminals still swallow ``ctrl+s`` as XOFF flow control.
    """

    can_focus = True

    BINDINGS = [
        Binding("j,down", "move(1)", "next field", show=False),
        Binding("k,up", "move(-1)", "prev field", show=False),
        Binding("enter,e", "edit", "edit", show=False),
        Binding("escape", "cancel", "cancel edit", show=False),
        Binding("ctrl+s,f2", "apply", "apply", show=True),
        Binding("r", "revert", "revert", show=True),
        Binding("d", "dry_run", "dry-run", show=True),
    ]

    DEFAULT_CSS = f"""
    Settings {{ height: 1fr; }}
    Settings #set-panel {{ height: 1fr; }}
    Settings #set-rows {{ height: 1fr; scrollbar-size-vertical: 1; }}
    Settings #set-head {{ height: auto; color: {COLOR[MUTED]}; }}
    Settings .set-group {{ height: 1; text-style: bold; padding: 0 0 0 1; }}
    Settings .set-row {{ height: 1; }}
    Settings .set-row.-cursor {{ background: $boost; }}
    Settings .set-row.-dirty .set-key {{ color: {COLOR[ACCENT]}; text-style: bold; }}
    Settings .set-row.-invalid .set-key {{ color: {COLOR[BAD]}; text-style: bold; }}
    Settings .set-mark {{ width: 2; }}
    Settings .set-key {{ width: 18; }}
    Settings .set-val {{ width: 30; margin: 0 1 0 0; }}
    Settings .set-klass {{ width: 11; }}
    Settings .set-doc {{ width: 1fr; }}
    Settings .set-flat {{ padding: 0 1; }}
    Settings Input.set-val {{ background: $surface; }}
    Settings Input.set-val:focus {{ background: $primary 30%; }}
    Settings Input.set-val:disabled {{ background: transparent; color: {COLOR[MUTED]}; }}
    Settings Switch.set-val {{ border: none; height: 1; width: 8; padding: 0 1;
                               background: transparent; }}
    Settings .set-bool {{ width: 22; }}
    Settings RadioSet.set-val {{ layout: horizontal; height: 1; width: 30;
                                 background: transparent; }}
    Settings RadioSet.set-val > RadioButton {{ width: auto; padding: 0 1 0 0; }}
    Settings #set-actions {{ height: 1; padding: 0 1; }}
    Settings #set-actions Button {{ min-width: 10; margin: 0 1 0 0; }}
    Settings #set-count {{ width: 1fr; content-align: right middle;
                           color: {COLOR[MUTED]}; }}
    Settings #set-out {{ display: none; height: auto; max-height: 14;
                         border: round $panel; padding: 0 1; }}
    Settings #set-out.-open {{ display: block; }}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rows: list[Row] = []
        self._cursor = 0
        self._built = False
        self._editing: Row | None = None
        self._edit_value: Any = None
        self._error = ""
        self._status = ""
        self._stamp: tuple | None = None
        self._path: Path | None = None
        self._project: Path | None = None
        self._text: str | None = None
        self._cfg = None
        self._baseline: dict[str, Any] = {}
        self._pinned: dict[str, str] = {}
        self._dash: Dash | None = None

    # -- composition ------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Panel("SETTINGS", id="set-panel"):
            yield Body(id="set-head")
            yield VerticalScroll(id="set-rows")
        with Horizontal(id="set-actions"):
            yield Button("apply", id="set-apply", compact=True, variant="primary")
            yield Button("revert", id="set-revert", compact=True)
            yield Button("dry-run", id="set-dry", compact=True)
            yield Body(id="set-count")
        with VerticalScroll(id="set-out"):
            yield Body(id="set-outbody")

    # -- the one entry point ----------------------------------------------
    def update(self, dash: Dash) -> None:
        """Repaint from disk. MUST never raise: this is a tab in a cockpit."""
        try:
            self._update(dash)
        except Exception as exc:  # noqa: BLE001 - deliberate: see the docstring
            self._error = f"config panel error: {safe(exc)}"
            try:
                self.query_one("#set-head", Body).update(paint(self._error, BAD))
            except Exception:  # noqa: BLE001 - not mounted yet; nothing to say it on
                pass

    def _update(self, dash: Dash) -> None:
        self._dash = dash
        self._path = dash.config_path
        self._project = Path(dash.cfg.project_dir)
        stamp = self._file_stamp(self._path)
        if stamp != self._stamp:
            self._stamp = stamp
            self._load(dash)
            if self._built:
                self._seed()
        if not self._built:
            self._build()
            return  # the rows mount on the next frame; they are born populated
        self._paint()

    @staticmethod
    def _file_stamp(path: Path) -> tuple:
        try:
            st = path.stat()
            return (st.st_mtime, st.st_size)
        except OSError:
            return (-1.0, -1)

    def _load(self, dash: Dash) -> None:
        """Re-read the file and recompute every baseline.

        The baseline is the *effective* config — file plus ``SWARM_*`` plus the
        documented defaults — not the raw file, because that is what the swarm is
        actually running and therefore the only honest thing to show. A field the
        file never mentions still gets its real value here, and writing it appends
        the key rather than pretending it was already there.
        """
        path, cfg = self._path, dash.cfg
        try:
            self._text = path.read_text(encoding="utf-8")
        except OSError:
            self._text = None
            self._error = (
                f"no {path.name} at {safe(path)} — these are the defaults in force;"
                " apply is disabled"
            )
        else:
            try:
                cfg = load_config(explicit=str(path), project_dir=str(self._project))
                self._error = ""
            except (ValueError, OSError) as exc:
                self._error = f"{path.name} did not parse, so nothing can be written: {safe(exc)}"
        self._cfg = cfg
        self._pinned = pinned(cfg)
        self._baseline = {s.name: getattr(cfg, s.name, "") for s in FIELDS}

    def _build(self) -> None:
        """Mount the rows once, already holding their values."""
        widgets: list = []
        for section, settings in by_section():
            # `[swarm]` is a colour tag unless it is escaped, and an unescaped
            # group header renders as a blank line.
            widgets.append(Static(paint(safe(section), ACCENT), classes="set-group"))
            for setting in settings:
                row = Row(setting, self._baseline.get(setting.name, ""),
                          self._pinned.get(setting.name))
                self._rows.append(row)
                widgets.append(row)
        self._built = True
        self.query_one("#set-rows", VerticalScroll).mount(*widgets)
        # The rows arrive on the next frame, so the first paint has to wait for
        # them; without this the panel would sit blank until the next tick, and
        # a tab that is never refreshed again would stay that way.
        self.call_after_refresh(self._first_paint)

    def _first_paint(self) -> None:
        try:
            self._paint()
        except Exception:  # noqa: BLE001 - same contract as update()
            pass

    def _seed(self) -> None:
        for row in self._rows:
            row.pinned_by = self._pinned.get(row.setting.name)
            row.seed(self._baseline.get(row.setting.name, ""))

    # -- painting ---------------------------------------------------------
    def _paint(self) -> None:
        for row in self._rows:
            row.restate()
        dirty = [r for r in self._rows if r.dirty]
        invalid = [r for r in dirty if r.error]
        counts: dict[str, int] = {}
        for row in dirty:
            counts[row.setting.klass] = counts.get(row.setting.klass, 0) + 1
        subtitle = f"{len(dirty)} changed" if dirty else "no changes"
        name = self._path.name if self._path else ".swarm.toml"
        self.query_one("#set-panel", Panel).set_title(f"SETTINGS ─ {name}", subtitle)

        head = [
            paint(safe(self._path), MUTED),
            paint("j/k move · enter edit · esc cancel · ctrl+s/f2 apply · r revert"
                  " · d dry-run", MUTED),
        ]
        if self._error:
            head.insert(0, paint(self._error, BAD))
        if invalid:
            head.insert(0, paint(safe(invalid[0].error), BAD))
        if self._status:
            head.append(self._status)
        self.query_one("#set-head", Body).update("\n".join(head))

        tally = " · ".join(
            f"{counts[k]} {_CLASS_LABEL[k]}" for k in (HOT, NEXT, RESTART) if counts.get(k)
        )
        self.query_one("#set-count", Body).update(paint(tally, WARN if tally else MUTED))
        blocked = bool(self._error or invalid or not dirty)
        self.query_one("#set-apply", Button).disabled = blocked
        self.query_one("#set-revert", Button).disabled = not dirty
        self._mark_cursor()

    def _mark_cursor(self) -> None:
        if not self._rows:
            return
        self._cursor = max(0, min(self._cursor, len(self._rows) - 1))
        for i, row in enumerate(self._rows):
            row.set_class(i == self._cursor, "-cursor")

    def _say(self, text: str) -> None:
        self._status = text
        try:
            self._paint()
        except Exception:  # noqa: BLE001 - a status line must not become a crash
            pass

    def _show(self, title: str, body: str, state: str | None = None) -> None:
        """Render a command's output. ``body`` is always treated as plain text."""
        text = safe(body)
        if state:
            text = paint(text, state)
        self.query_one("#set-outbody", Body).update(
            f"{paint(title, ACCENT)}\n{text}" if body
            else paint(f"{title}: (no output)", MUTED)
        )
        self.query_one("#set-out", VerticalScroll).add_class("-open")

    # -- edits ------------------------------------------------------------
    def edits(self) -> dict[tuple[str, str], Any]:
        """``{(table, key): value}`` for every changed row. Raises on a bad entry.

        Reads the widgets rather than trusting the cached ``dirty`` flags. Those
        are refreshed by a repaint, and a repaint is driven by a `Changed`
        message — so an apply that landed in the same frame as the last keystroke
        would look at stale flags and write nothing at all.
        """
        out: dict[tuple[str, str], Any] = {}
        for row in self._rows:
            if row.locked:
                continue
            value = row.read()
            if value != row.baseline:
                out[(row.setting.table, row.setting.key)] = value
        return out

    def pending_text(self) -> str:
        """What apply would write, without writing it."""
        return toml_set_many(self._text or "", self.edits())

    # -- actions ----------------------------------------------------------
    def check_action(self, action: str, parameters) -> bool | None:
        """Let unused keys fall through to the app.

        ``escape`` closes the app's search bar and ``r`` is its recap key; both
        must keep working here whenever this panel has nothing to do with them,
        and a binding that merely does nothing would still swallow the key.
        """
        if action == "cancel":
            return self._editing is not None
        if action == "revert":
            # `r` is the app's recap key; only claim it when there is an edit to
            # throw away, so the cockpit keeps working from this tab.
            return any(r.dirty for r in self._rows)
        return True

    def action_move(self, delta: int) -> None:
        """Move the cursor. Leaving a row commits its edit — `esc` is for staying."""
        if not self._rows:
            return
        self._editing = None
        self._cursor = max(0, min(self._cursor + delta, len(self._rows) - 1))
        self._mark_cursor()
        # Not animated: a held-down `j` must keep up, not glide behind.
        self._rows[self._cursor].scroll_visible(animate=False)

    def action_edit(self) -> None:
        if not self._rows:
            return
        row = self._rows[self._cursor]
        if row.pinned_by:
            self._say(paint(f"{row.setting.key} is pinned by {row.pinned_by}; unset it to"
                            " edit here", WARN))
            return
        if row.setting.kind == FROZEN:
            self._say(paint(f"{row.setting.key} is not editable from the form — {row.setting.doc}",
                            WARN))
            return
        widget = row.widget
        self._editing, self._edit_value = row, row.read()
        if isinstance(widget, Switch):
            widget.toggle()  # a two-state field: `enter` IS the change
        elif isinstance(widget, RadioSet):
            # Textual's RadioSet moves a highlight with the arrows and only
            # commits on a second key, so focusing it would leave `enter` doing
            # nothing visible. Cycling matches the Switch instead: one key, one
            # change, and `esc` still puts it back.
            row.write(self._next_option(row))
        else:
            widget.focus()
            if isinstance(widget, Select):
                widget.expanded = True
        self._paint()

    @staticmethod
    def _next_option(row: Row) -> str:
        options = row.options
        if not options:
            return row.baseline
        try:
            index = options.index(str(row.read()))
        except ValueError:
            index = -1
        return options[(index + 1) % len(options)]

    def action_cancel(self) -> None:
        """Undo the edit on the row the cursor is on.

        "The edit" is whatever the row held when `enter` was pressed, which is why
        a toggled Switch and a cycled RadioSet are cancellable too even though
        they never took focus. The baseline is untouched — only the widget moves.
        """
        row, self._editing = self._editing, None
        if row is None:
            return
        row.write(row.baseline if self._edit_value is None else self._edit_value)
        self.focus()
        self._paint()

    def action_revert(self) -> None:
        self._editing = None
        for row in self._rows:
            row.write(self._baseline.get(row.setting.name, row.baseline))
        self.focus()
        self._say(paint("reverted to the file", MUTED))

    def action_apply(self) -> None:
        """Validate, write, then offer the hot reload."""
        if self._error or self._path is None:
            self._say(paint("apply is disabled while the file cannot be read", BAD))
            return
        try:
            edits = self.edits()
        except ValueError as exc:
            self._say(paint(safe(exc), BAD))
            return
        if not edits:
            self._say(paint("nothing changed", MUTED))
            return
        text = toml_set_many(self._text or "", edits)
        try:
            tomllib.loads(text)
            load_text(text, self._project)
        except (ValueError, OSError) as exc:
            # Never write a file the CLI cannot read: this is the file every
            # `swarm` command loads, including the ones needed to undo the damage.
            self._say(paint(f"refused — the result would not load: {safe(exc)}", BAD))
            return
        try:
            self._path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self._say(paint(f"write failed: {safe(exc)}", BAD))
            return
        self._stamp = None  # force a re-read, so the baselines follow the file
        self._editing = None
        if self._dash is not None:
            self.update(self._dash)  # clear the dirty marks now, not on the next tick
        self._say(paint(f"wrote {len(edits)} setting(s) to {self._path.name} — asking the"
                        " supervisor to reload", OK))
        self._reload_now()

    def action_dry_run(self) -> None:
        """Preview: the pending form if it has edits, else the file against the run."""
        if self._error:
            self._say(paint("cannot preview while the file cannot be read", BAD))
            return
        try:
            edits = self.edits()
        except ValueError as exc:
            self._say(paint(safe(exc), BAD))
            return
        if not edits:
            self._say(paint("no pending edits — running `swarm reload --dry-run` against"
                            " the file on disk", MUTED))
            self._run_cli(["reload", "--dry-run"])
            return
        self._say(paint(f"preview of {len(edits)} unsaved edit(s)", INFO))
        self._preview_now()

    # -- worker threads ---------------------------------------------------
    @work(thread=True, exclusive=True, group="cfg")
    def _reload_now(self) -> None:
        result = probes.swarm(["reload"], project_dir=str(self._project))
        body = result.text or "(no output)"
        if result.unavailable:
            body = "this build has no `swarm reload`; the file takes effect at the next `swarm up`"
        self.app.call_from_thread(self._show, "$ swarm reload", body)

    @work(thread=True, exclusive=True, group="cfg")
    def _run_cli(self, args: list[str]) -> None:
        result = probes.swarm(args, project_dir=str(self._project))
        body = result.text or "(no output)"
        if result.unavailable:
            body = f"this build has no `swarm {args[0]}`"
        self.app.call_from_thread(self._show, f"$ swarm {' '.join(args)}", body)

    @work(thread=True, exclusive=True, group="cfg")
    def _preview_now(self) -> None:
        """The pending form's reload verdict, computed off the UI thread.

        ``load_config`` touches the filesystem and ``reload.plan`` walks the whole
        policy table; neither is slow, but nothing in this app is allowed to do
        I/O on the thread that draws.
        """
        try:
            dash = self._dash
            body = preview(self.pending_text(), self._project, live_config(dash), facts(dash))
        except Exception as exc:  # noqa: BLE001 - a preview failure is a message, not a crash
            self.app.call_from_thread(
                self._show, "dry-run of the unsaved form", f"could not preview: {exc}", BAD
            )
            return
        self.app.call_from_thread(self._show, "dry-run of the unsaved form", body)

    # -- events -----------------------------------------------------------
    def on_show(self) -> None:
        self.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        {"set-apply": self.action_apply, "set-revert": self.action_revert,
         "set-dry": self.action_dry_run}.get(event.button.id, lambda: None)()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._touch(event.input)

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self._touch(event.switch)

    def on_select_changed(self, event: Select.Changed) -> None:
        self._touch(event.select)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        # The set has caught up, so its own state is authoritative again.
        row = next((a for a in event.radio_set.ancestors_with_self
                    if isinstance(a, Row)), None)
        if row is not None:
            row.chosen = None
        self._touch(event.radio_set)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in a field commits the edit and returns to navigation."""
        event.stop()
        self._editing = None
        self.focus()
        self._paint()

    def _touch(self, widget) -> None:
        """Re-mark the row a widget belongs to. Cheap, and keeps the count live.

        The cursor only follows a widget that actually has focus. Mounting a
        `Select` or a `Switch` emits a `Changed` of its own, so a version that
        trusted every event dragged the cursor to whichever row happened to
        announce itself last — and the owner's first `enter` opened a field they
        never selected.
        """
        row = next((a for a in widget.ancestors_with_self if isinstance(a, Row)), None)
        if row is None or not self._built:
            return
        if widget.has_focus or widget.has_focus_within:
            self._cursor = self._rows.index(row)
        try:
            self._paint()
        except Exception:  # noqa: BLE001 - a keystroke must never raise
            pass
