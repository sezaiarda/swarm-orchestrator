"""The config schema: every setting declared once, on its :class:`Config` field.

``load()``, ``swarm reload`` and the TUI form all read :data:`config.SETTINGS`,
so these tests pin the schema itself: every entry is complete and well-formed,
every key really is read from its table (and its env var, where it has one),
and ``docs/config.md`` says what the schema says.
"""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

pytest.importorskip("textual")

from swarm_orchestrator import reload as reload_mod  # noqa: E402
from swarm_orchestrator.config import (  # noqa: E402
    BOOL, CHOICE, CLI, FROZEN, HOT, INT, LIST, NEXT, RESTART, SETTINGS, STR, Config, load,
)
from swarm_orchestrator.tui.configform import toml_literal  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "docs" / "config.md"
FILE_KEYS = [s for s in SETTINGS.values() if s.table != CLI]


@pytest.fixture
def project(tmp_path, monkeypatch):
    for setting in SETTINGS.values():
        if setting.env:
            monkeypatch.delenv(setting.env, raising=False)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    pdir = tmp_path / "project"
    pdir.mkdir()
    return pdir


def _other(setting):
    """A valid value for ``setting`` that is not its default."""
    if setting.kind == BOOL:
        return not setting.default
    if setting.kind == INT:
        return max(setting.minimum or 0, setting.default) + 400
    if setting.kind == LIST:
        return ["a", "b"]
    if setting.kind == CHOICE:
        return next(c for c in setting.choices if c and c != setting.default)
    return f"x-{setting.key}"


# -- every entry is complete ----------------------------------------------
def test_every_config_field_is_a_setting():
    assert list(SETTINGS) == [f.name for f in fields(Config) if f.init]


def test_every_setting_is_well_formed():
    envs = [s.env for s in SETTINGS.values() if s.env]
    assert len(envs) == len(set(envs))
    keys = [(s.table, s.key) for s in SETTINGS.values()]
    assert len(keys) == len(set(keys))
    for s in SETTINGS.values():
        assert s.klass in (HOT, NEXT, RESTART), s.name
        assert s.kind in (INT, STR, BOOL, LIST, CHOICE, FROZEN), s.name
        assert s.why, s.name
        assert s.gate is None or s.gate in reload_mod.GATES, s.name
        assert bool(s.choices) == (s.kind == CHOICE), s.name
        assert (s.minimum is not None) == (s.kind == INT), s.name
        assert s.env is None or s.env.startswith("SWARM_"), s.name


def test_every_row_says_what_the_setting_does_in_one_short_line():
    for s in SETTINGS.values():
        assert s.doc and not s.doc.rstrip().endswith(".")
        assert len(s.doc) <= 50, f"{s.name}: too long for the column"
        assert "\n" not in s.doc


# -- every key is really read ---------------------------------------------
def test_an_empty_file_gives_every_default(project):
    cfg = load(project_dir=str(project))
    for s in FILE_KEYS:
        if callable(s.default):
            continue
        want = list(s.default) if s.name == "usage_rules" else s.default
        assert getattr(cfg, s.name) == want, s.name


def test_every_key_is_read_from_its_table(project):
    edits = {s.name: _other(s) for s in FILE_KEYS if s.kind != FROZEN}
    text = ""
    for table in dict.fromkeys(SETTINGS[n].table for n in edits):
        text += f"[{table}]\n" + "".join(
            f"{SETTINGS[n].key} = {toml_literal(v)}\n"
            for n, v in edits.items() if SETTINGS[n].table == table)
    (project / ".swarm.toml").write_text(text)
    cfg = load(project_dir=str(project))
    for name, value in edits.items():
        assert getattr(cfg, name) == value, name


def test_every_env_override_wins_over_the_file(project, monkeypatch):
    for s in FILE_KEYS:
        if not s.env:
            continue
        value = _other(s)
        raw = {BOOL: "1" if value else "0", LIST: "a, b"}.get(s.kind, str(value))
        monkeypatch.setenv(s.env, raw)
        assert getattr(load(project_dir=str(project)), s.name) == value, s.name
        monkeypatch.delenv(s.env)


# -- the reference doc says what the schema says --------------------------
def _doc_rows() -> dict[tuple[str, str], list[str]]:
    rows, table = {}, None
    for line in DOCS.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^## `\[(\w+)\]`", line)
        if line.startswith("## "):
            table = heading.group(1) if heading else None
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if table and line.startswith("| `") and len(cells) >= 5:
            rows[(table, cells[0].strip("`"))] = cells
    return rows


def test_the_reference_doc_lists_exactly_the_file_keys():
    assert set(_doc_rows()) == {(s.table, s.key) for s in FILE_KEYS}


def test_the_reference_doc_agrees_on_env_reload_class_and_default():
    rows = _doc_rows()
    for s in FILE_KEYS:
        _, default, env, klass, _meaning = rows[(s.table, s.key)][:5]
        where = f"[{s.table}].{s.key}"
        assert (re.findall(r"`(SWARM_\w+)`", env) or [None])[0] == s.env, where
        assert klass.split(",")[0] == s.klass, where
        if not callable(s.default) and s.name not in ("telegram_notify", "usage_rules"):
            assert default == f"`{toml_literal(s.default)}`", where
