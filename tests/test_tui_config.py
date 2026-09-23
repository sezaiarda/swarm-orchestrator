"""Tests for the Config tab's settings form.

The centre of gravity here is the value writer, because it is the only part of
the dashboard that can destroy something the owner cannot get back. A
``.swarm.toml`` is often mostly *commentary* — notes on why each value is what
it is, and corrections to earlier notes — and a form that quietly reformatted
that away would be far worse than the raw textarea it replaces. So the
round-trip assertion is
deliberately brutal: after rewriting several keys, every comment must still be
present, with the same text, on the same line, in the same column, and exactly
one line per edit may differ from the original.

That property is checked against a corpus embedded below, which carries every
hazard a real file can have (aligned continuation-comment blocks, literal
strings full of double quotes, a ``#`` inside a string, an inline table, a
comment-only section).

The rest covers the two ways a settings screen lies to you — showing a value the
environment has overridden, and misstating when a change takes effect — plus the
requirement that a tab in an always-on cockpit renders a message rather than a
traceback when the file it edits is missing or broken.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("textual")

from swarm_orchestrator import reload as reload_mod  # noqa: E402
from swarm_orchestrator.config import load as load_config  # noqa: E402
from swarm_orchestrator.tui import configform as cf  # noqa: E402

CORPUS = r"""[swarm]
# max_workers above 1 is safe when the needs: chain serializes same-repo waves.
max_workers = 1
master_model = ""                 # "" = inherit; else "opus"/"sonnet"/...

[worker]
command_template = "/prime {phase}"          # sent via send-keys into each slot
done_hook       = 'swarm done "$SWARM_PHASE" ok'
worker_settings = '{"teammateMode":"in-process","hooks":{"Stop":[]}}'
done_grace_s    = 45   # after a worker signals `done`, it holds its slot this many
                       # seconds (a finish buffer to fully close out the session)
                       # before the supervisor reclaims it. 0 = advance immediately.
                       #
                       # Kept short. At max_workers = 1
                       # the grace is pure dead time.
park_after      = 900  # seconds a worker may wait on the owner. 0 = disable parking.
# ready_marker auto-derives from `claude --version` (its banner). Set to override.

[tasks]
# Phases the swarm must never launch:
#   jade-W14  -- needs the full stack; run by the operator afterwards.
exclude = ["jade-W14", "billing-P4", "teal-W19"]

[telegram]
notify = "scripts/notify.sh#main"  # not a comment

[tmux]
# session name defaults to the project
# directory's own name. Set an explicit value here only to override.

[git]
# "worktree" gives each worker its own checkout; under "none" two workers would
# race on `git index.lock` in one working tree, so keep "none" to max_workers = 1.
isolation   = "none"
main_branch = "master"     # the branch the integrator merges each phase into
repos       = ["*"]        # mirror every direct-child git repo. Narrow this (an
                           # explicit list) if some repos are large / never touched.
auto_resolve = { "docs/PHASE-LEDGER.md" = "keyed:^- \\[[ x]\\] `([A-Za-z0-9_.-]+)`" }
"""


def comments(text: str) -> list[tuple[int, int, str]]:
    """Every comment as ``(line, column, text)``.

    Deliberately a second, independent scanner rather than the module's own: a
    round-trip test that asked the writer to grade its own homework would pass
    for any self-consistent bug.
    """
    found: list[tuple[int, int, str]] = []
    for row, line in enumerate(text.split("\n")):
        quote: str | None = None
        escaped = False
        for col, ch in enumerate(line):
            if quote is not None:
                if escaped:
                    escaped = False
                elif ch == "\\" and quote == '"':
                    escaped = True
                elif ch == quote:
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "#":
                found.append((row, col, line[col:]))
                break
    return found


def only_changed(before: str, after: str) -> list[int]:
    """Indices of the lines that differ. Asserts nothing was added or removed."""
    old, new = before.split("\n"), after.split("\n")
    assert len(old) == len(new), "a value rewrite must not change the line count"
    return [i for i, (a, b) in enumerate(zip(old, new)) if a != b]


# -- the value writer: comments must survive ------------------------------
EDITS = {
    ("swarm", "max_workers"): 4,
    ("worker", "done_grace_s"): 120,
    ("worker", "park_after"): 0,
    ("git", "isolation"): "worktree",
    ("git", "main_branch"): "main",
    ("tasks", "exclude"): ["jade-W14"],
}


def test_rewriting_values_leaves_every_comment_exactly_where_it_was():
    out = cf.toml_set_many(CORPUS, EDITS)
    assert comments(out) == comments(CORPUS)
    assert len(only_changed(CORPUS, out)) == len(EDITS)


def test_rewriting_values_changes_only_the_values_asked_for():
    out = cf.toml_set_many(CORPUS, EDITS)
    before, after = tomllib.loads(CORPUS), tomllib.loads(out)
    assert after["swarm"]["max_workers"] == 4
    assert after["worker"]["done_grace_s"] == 120
    assert after["worker"]["park_after"] == 0
    assert after["git"]["isolation"] == "worktree"
    assert after["git"]["main_branch"] == "main"
    assert after["tasks"]["exclude"] == ["jade-W14"]
    for table, keys in before.items():
        for key, value in keys.items():
            if (table, key) not in EDITS:
                assert after[table][key] == value, f"{table}.{key} was collateral damage"


def test_a_shorter_value_keeps_the_comment_in_its_column():
    """The continuation block under ``done_grace_s`` is aligned to that ``#``.

    Re-padding is not cosmetic: those five following lines are indented to sit
    under the first comment's column, so a comment that slid three characters
    left would visibly break a block this module promised not to touch.
    """
    out = cf.toml_set_many(CORPUS, {("worker", "done_grace_s"): 5})
    line = next(x for x in out.split("\n") if x.startswith("done_grace_s"))
    original = next(x for x in CORPUS.split("\n") if x.startswith("done_grace_s"))
    assert line.index("#") == original.index("#")
    assert line.startswith("done_grace_s    = 5")


def test_a_value_that_grows_into_its_padding_still_leaves_the_comment_put():
    out = cf.toml_set_many(CORPUS, {("swarm", "master_model"): "claude-opus-5"})
    line = next(x for x in out.split("\n") if x.startswith("master_model"))
    original = next(x for x in CORPUS.split("\n") if x.startswith("master_model"))
    assert line.index("#") == original.index("#")
    assert tomllib.loads(out)["swarm"]["master_model"] == "claude-opus-5"


def test_a_value_that_outgrows_its_column_takes_its_comment_block_with_it():
    """Nothing can hold a comment in column 27 once the value needs column 34.

    What can be held is the *paragraph*: a real config indents two more lines under
    that ``#``, and leaving them behind would tear the note in half.
    """
    out = cf.toml_set(CORPUS, "git", "repos", ["*", "packages/*", "vendor/*"])
    lines = out.split("\n")
    head = next(x for x in lines if x.startswith("repos"))
    tail = lines[lines.index(head) + 1]
    assert head.index("#") > CORPUS.split("\n")[
        [i for i, x in enumerate(CORPUS.split("\n")) if x.startswith("repos")][0]
    ].index("#")
    assert head.index("#") == tail.index("#"), "the block came apart"
    assert tail.lstrip() == "# explicit list) if some repos are large / never touched."
    assert [c[2] for c in comments(out)] == [c[2] for c in comments(CORPUS)]


def test_a_hash_inside_a_string_is_not_mistaken_for_a_comment():
    """The old value ends in ``.sh#main`` and the new one in ``#dev``.

    A scanner that stopped at the first ``#`` would treat half the old value as
    a comment, rewrite from there, and leave a mangled line that no longer
    parses — with the real comment appended to whatever was left.
    """
    out = cf.toml_set_many(CORPUS, {("telegram", "notify"): "/tmp/n.sh#dev"})
    line = next(x for x in out.split("\n") if x.startswith("notify"))
    original = next(x for x in CORPUS.split("\n") if x.startswith("notify"))
    assert line.rstrip().endswith("# not a comment")
    assert line.rindex("#") == original.rindex("#")
    assert tomllib.loads(out)["telegram"]["notify"] == "/tmp/n.sh#dev"


def test_a_literal_string_stays_a_literal_string():
    """``done_hook`` is quoted ``'...'`` because it is full of double quotes.

    Re-emitting it as an escaped basic string would parse fine and be unreadable,
    and the owner would have to undo it by hand.
    """
    out = cf.toml_set_many(CORPUS, {("worker", "done_hook"): 'swarm done "$P" fail'})
    line = next(x for x in out.split("\n") if x.startswith("done_hook"))
    assert line == """done_hook       = 'swarm done "$P" fail'"""
    assert tomllib.loads(out)["worker"]["done_hook"] == 'swarm done "$P" fail'


def test_a_multi_line_array_is_replaced_whole():
    text = '[git]\nrepos = [\n  "*",       # every child repo\n  "packages/*",\n]\nmain_branch = "master"\n'
    out = cf.toml_set(text, "git", "repos", ["*"])
    assert tomllib.loads(out)["git"] == {"repos": ["*"], "main_branch": "master"}
    assert out.endswith('main_branch = "master"\n')


def test_a_missing_key_is_appended_below_the_comments_of_its_table():
    """``[tmux]`` in the corpus holds nothing but a two-line comment.

    The insertion point walks back over blank lines only. Walking back over
    comments too would look tidier and would sometimes drop the key into the
    middle of a block explaining something else.
    """
    out = cf.toml_set(CORPUS, "tmux", "layout", "even-vertical")
    lines = out.split("\n")
    at = lines.index('layout = "even-vertical"')
    assert lines[at - 1].startswith("# directory's own name")
    assert lines[at + 1] == ""
    assert [c[2] for c in comments(out)] == [c[2] for c in comments(CORPUS)]
    assert tomllib.loads(out)["tmux"] == {"layout": "even-vertical"}


def test_a_missing_table_is_created_at_the_end():
    out = cf.toml_set(CORPUS, "build", "jobs", 4)
    assert out.endswith("\n[build]\njobs = 4\n")
    assert tomllib.loads(out)["build"] == {"jobs": 4}
    assert [c[2] for c in comments(out)] == [c[2] for c in comments(CORPUS)]


def test_writing_into_an_empty_file_produces_valid_toml():
    out = cf.toml_set("", "swarm", "max_workers", 2)
    assert tomllib.loads(out) == {"swarm": {"max_workers": 2}}


def test_a_key_that_only_appears_in_another_table_is_not_hijacked():
    text = '[swarm]\nslug = "a"\n\n[tmux]\nsession = "b"\n'
    out = cf.toml_set(text, "tmux", "session", "c")
    assert tomllib.loads(out) == {"swarm": {"slug": "a"}, "tmux": {"session": "c"}}


def test_a_commented_out_assignment_is_not_treated_as_the_key():
    text = '[swarm]\n# max_workers = 99   (was raised once, then reverted)\nmax_workers = 1\n'
    out = cf.toml_set(text, "swarm", "max_workers", 3)
    assert "# max_workers = 99   (was raised once, then reverted)" in out
    assert tomllib.loads(out)["swarm"]["max_workers"] == 3


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, "true"),
        (False, "false"),
        (4, "4"),
        (0, "0"),
        ("plain", '"plain"'),
        ("", '""'),
        (["a", "b"], '["a", "b"]'),
        ([], "[]"),
        ({}, "{}"),
        ({"docs/L.md": "union"}, '{ "docs/L.md" = "union" }'),
        ('has "quotes"', """'has "quotes"'"""),
    ],
)
def test_toml_literal_round_trips_through_tomllib(value, expected):
    assert cf.toml_literal(value) == expected
    assert tomllib.loads(f"x = {expected}")["x"] == value


# -- the field table ------------------------------------------------------
def test_every_policy_field_has_a_row():
    """A field cannot reach Config without reaching this screen.

    ``reload.coverage_gap`` already forces someone to decide what a reload does
    with a new field; this forces them to say what it is *for* as well.
    """
    assert cf.coverage_gap() == set()
    assert reload_mod.coverage_gap() == set()


def test_the_reload_class_is_read_from_policy_never_restated():
    for setting in cf.FIELDS:
        assert setting.klass is reload_mod.POLICY[setting.name].klass
        assert setting.env is reload_mod.POLICY[setting.name].env
    assert cf.SETTINGS["max_workers"].klass == reload_mod.HOT
    assert cf.SETTINGS["done_grace_s"].klass == reload_mod.NEXT
    assert cf.SETTINGS["git_isolation"].klass == reload_mod.RESTART


def test_every_row_says_what_the_setting_does_in_one_short_line():
    for setting in cf.FIELDS:
        assert setting.doc and not setting.doc.rstrip().endswith(".")
        assert len(setting.doc) <= 50, f"{setting.name}: too long for the column"
        assert "\n" not in setting.doc


def test_the_form_is_grouped_in_file_order():
    sections = [name for name, _ in cf.by_section()]
    assert sections == ["[swarm]", "[worker]", "[tasks]", "[telegram]", "[tmux]",
                        "[build]", "[gc]", "[git]", "[operator]", "[overseer]", "[tui]", "[web]", "(cli)"]


def test_a_settings_table_name_drops_the_brackets_policy_uses():
    assert cf.SETTINGS["git_isolation"].table == "git"
    assert cf.SETTINGS["git_isolation"].key == "isolation"
    assert cf.SETTINGS["tmux_layout"].table == "tmux"
    assert cf.SETTINGS["tmux_layout"].key == "layout"


def test_the_layout_choices_come_from_tmux_not_a_copy():
    from swarm_orchestrator.tmux import LAYOUTS, normalize_layout

    assert cf.SETTINGS["tmux_layout"].choices == tuple(LAYOUTS)
    for choice in cf.SETTINGS["tmux_layout"].choices:
        assert normalize_layout(choice) == choice


# -- env pinning ----------------------------------------------------------
@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A real :class:`Config` on a temp project, with the SWARM_* env cleared.

    Clearing matters: the developer's own shell may well have half of these set,
    and a pinned-field test that inherited them would pass or fail by accident.
    """
    project = tmp_path / "project"
    project.mkdir()
    for policy in reload_mod.POLICY.values():
        if policy.env:
            monkeypatch.delenv(policy.env, raising=False)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    cfg = load_config(project_dir=str(project))
    cfg.ensure_dirs()
    return cfg


def test_an_env_override_marks_its_field_pinned(cfg, monkeypatch):
    assert cf.pinned(cfg) == {}
    monkeypatch.setenv("SWARM_PARK_AFTER", "600")
    reloaded = load_config(project_dir=str(cfg.project_dir))
    assert cf.pinned(reloaded)["park_after"] == "SWARM_PARK_AFTER"
    assert cf.pinned(reloaded, env={}) == {}


def test_an_unparseable_numeric_override_does_not_pin(cfg, monkeypatch):
    """``config._int_env`` walks past a malformed override to the file value.

    Calling that "pinned" would disable the row and tell the owner an edit here
    does nothing — in exactly the case where an edit here is the only thing that
    works.
    """
    monkeypatch.setenv("SWARM_PARK_AFTER", "not-a-number")
    reloaded = load_config(project_dir=str(cfg.project_dir))
    assert "park_after" not in cf.pinned(reloaded)
    monkeypatch.setenv("SWARM_WORKER_CMD", "claude --fake")
    reloaded = load_config(project_dir=str(cfg.project_dir))
    assert cf.pinned(reloaded)["worker_cmd"] == "SWARM_WORKER_CMD"


# -- coercion -------------------------------------------------------------
def test_an_integer_field_refuses_what_it_cannot_write():
    setting = cf.SETTINGS["max_workers"]
    assert cf.coerce(setting, "4") == 4
    assert cf.coerce(setting, " 12 ") == 12
    for bad in ("", "   ", "abc"):
        with pytest.raises(ValueError):
            cf.coerce(setting, bad)


def test_max_workers_may_not_be_written_below_one(cfg):
    """``load()`` raises under 1, so writing a 0 would break every swarm command.

    Including the ones needed to undo it — the file would have to be repaired in
    an editor before the dashboard could even start again.
    """
    with pytest.raises(ValueError):
        cf.coerce(cf.SETTINGS["max_workers"], "0")
    assert cf.coerce(cf.SETTINGS["watchdog_s"], "0") == 0


def test_a_list_field_reads_and_writes_a_comma_separated_line():
    setting = cf.SETTINGS["exclude"]
    assert cf.coerce(setting, "a, b ,c") == ["a", "b", "c"]
    assert cf.coerce(setting, "") == []
    assert cf.render_value(setting, ["a", "b"]) == "a, b"
    assert cf.render_value(setting, []) == ""


def test_a_choice_field_refuses_a_value_outside_its_options():
    assert cf.coerce(cf.SETTINGS["git_isolation"], "worktree") == "worktree"
    with pytest.raises(ValueError):
        cf.coerce(cf.SETTINGS["git_isolation"], "sometimes")


def test_a_bool_field_reads_as_a_bool():
    assert cf.coerce(cf.SETTINGS["build_cache"], True) is True
    assert cf.render_value(cf.SETTINGS["build_cache"], 1) is True


# -- the widget: it must never raise --------------------------------------
def form(cfg, capfd, body, config_text: str | None = None):
    """Boot a one-widget app around :class:`Settings` and run ``body``.

    Textual's headless driver writes to the real stdout and deadlocks under
    pytest's fd capture, so capture is suspended; the run is bounded so a future
    regression that wedges the event loop fails in 30s instead of hanging CI.
    """
    from textual.app import App, ComposeResult

    from swarm_orchestrator.tui.dash import Dash

    if config_text is not None:
        (Path(cfg.project_dir) / ".swarm.toml").write_text(config_text, encoding="utf-8")
    dash = Dash(cfg)

    class Harness(App):
        def compose(self) -> ComposeResult:
            yield cf.Settings(id="tab-config")

    async def drive() -> None:
        app = Harness()
        async with app.run_test(size=(120, 44)) as pilot:
            panel = app.query_one(cf.Settings)
            panel.update(dash)      # first pass loads and mounts
            await pilot.pause()
            panel.update(dash)      # second pass paints the mounted rows
            await pilot.pause()
            await body(pilot, panel, dash)

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(drive(), timeout=30))


def row_of(panel, name):
    return next(r for r in panel._rows if r.setting.name == name)


def screen_text(app) -> str:
    """The characters actually on screen, straight off the compositor.

    Reading the widget back is not good enough for the markup tests: the whole
    failure mode is Rich deleting a bracketed run on its way to the terminal, so
    the assertion has to happen after rendering, not before it.

    It composites rather than scraping ``export_screenshot()``. That SVG splits a
    line into one ``<text>`` element per style run and drops some of them, so a
    value could be demonstrably on screen — confirmed against the compositor and
    in a real terminal — while the regex found nothing. The test failed and the
    code was right, which is the worst way for a test to be wrong.
    """
    return "\n".join(
        "".join(segment.text for segment in strip)
        for strip in app.screen._compositor.render_strips()
    ).replace("\xa0", " ")


def test_the_form_shows_every_setting_with_its_live_value(cfg, capfd):
    async def body(pilot, panel, dash):
        assert len(panel._rows) == len(cf.FIELDS)
        assert row_of(panel, "max_workers").read() == 1
        assert row_of(panel, "git_isolation").read() == "none"
        assert row_of(panel, "done_grace_s").read() == 45
        assert row_of(panel, "build_cache").read() is True
        assert not any(r.dirty for r in panel._rows)
        assert panel._error == ""

    form(cfg, capfd, body, CORPUS)


def test_a_missing_config_file_disables_apply_instead_of_crashing(cfg, capfd):
    """A missing file is a normal state of the world, not an error to raise on."""
    async def body(pilot, panel, dash):
        assert "no .swarm.toml" in panel._error
        assert row_of(panel, "max_workers").read() == 4  # the documented default
        row_of(panel, "max_workers").write("9")
        await pilot.pause()
        panel.action_apply()
        await pilot.pause()
        assert "disabled" in panel._status
        assert not dash.config_path.exists()

    form(cfg, capfd, body)


def test_an_unparseable_config_file_disables_apply_instead_of_crashing(cfg, capfd):
    broken = CORPUS + "\nthis is not toml at all [[[\n"

    async def body(pilot, panel, dash):
        assert "did not parse" in panel._error
        row_of(panel, "max_workers").write("9")
        await pilot.pause()
        panel.action_apply()
        await pilot.pause()
        assert dash.config_path.read_text(encoding="utf-8") == broken

    form(cfg, capfd, body, broken)


def test_update_never_raises_on_a_config_path_that_is_a_directory(cfg, capfd):
    (Path(cfg.project_dir) / ".swarm.toml").mkdir()

    async def body(pilot, panel, dash):
        panel.update(dash)
        await pilot.pause()
        assert panel._error

    form(cfg, capfd, body)


def test_an_env_pinned_row_is_disabled_and_says_which_variable_owns_it(
    cfg, capfd, monkeypatch
):
    monkeypatch.setenv("SWARM_PARK_AFTER", "600")

    async def body(pilot, panel, dash):
        row = row_of(panel, "park_after")
        assert row.pinned_by == "SWARM_PARK_AFTER"
        assert row.locked and row.widget.disabled
        assert "SWARM_PARK_AFTER" in row._doc()
        panel._cursor = panel._rows.index(row)
        panel.action_edit()
        await pilot.pause()
        assert "pinned" in panel._status
        assert not row.dirty

    form(cfg, capfd, body, CORPUS)


def test_editing_a_row_marks_it_changed_and_counts_it(cfg, capfd):
    async def body(pilot, panel, dash):
        panel._cursor = panel._rows.index(row_of(panel, "max_workers"))
        await pilot.press("enter")
        await pilot.press("backspace", "8")
        await pilot.pause()
        row = row_of(panel, "max_workers")
        assert row.dirty and row.read() == 8
        assert row.has_class("-dirty")
        assert panel.query_one("#set-panel").border_subtitle == "1 changed"
        assert panel.edits() == {("swarm", "max_workers"): 8}

    form(cfg, capfd, body, CORPUS)


def test_escape_cancels_one_edit_and_revert_undoes_them_all(cfg, capfd):
    async def body(pilot, panel, dash):
        panel._cursor = panel._rows.index(row_of(panel, "git_isolation"))
        await pilot.press("enter")            # a two-state row: enter IS the change
        await pilot.pause()
        assert row_of(panel, "git_isolation").read() == "worktree"
        await pilot.press("escape")
        await pilot.pause()
        assert row_of(panel, "git_isolation").read() == "none"
        assert not any(r.dirty for r in panel._rows)

        panel._cursor = panel._rows.index(row_of(panel, "build_cache"))
        await pilot.press("enter")
        panel._cursor = panel._rows.index(row_of(panel, "max_workers"))
        await pilot.press("enter", "backspace", "7")
        await pilot.pause()
        assert len([r for r in panel._rows if r.dirty]) == 2
        panel.action_revert()
        await pilot.pause()
        assert not any(r.dirty for r in panel._rows)

    form(cfg, capfd, body, CORPUS)


def test_an_invalid_entry_blocks_apply_and_says_why(cfg, capfd):
    async def body(pilot, panel, dash):
        row = row_of(panel, "max_workers")
        row.write("0")
        await pilot.pause()
        assert row.error and "must be >= 1" in row.error
        assert panel.query_one("#set-apply").disabled
        panel.action_apply()
        await pilot.pause()
        assert tomllib.loads(dash.config_path.read_text())["swarm"]["max_workers"] == 1

    form(cfg, capfd, body, CORPUS)


def test_apply_writes_the_edits_and_keeps_every_comment(cfg, capfd, monkeypatch):
    """The end-to-end proof: keystrokes in, a file with its commentary intact out."""
    calls: list[list[str]] = []

    def fake_swarm(args, project_dir=None, timeout=60.0):
        calls.append(list(args))
        return type("R", (), {"text": "applied now:\n  [swarm].max_workers: 1 -> 8",
                              "unavailable": False, "ok": True})()

    monkeypatch.setattr(cf.probes, "swarm", fake_swarm)

    async def body(pilot, panel, dash):
        panel._cursor = panel._rows.index(row_of(panel, "max_workers"))
        await pilot.press("enter", "backspace", "8", "enter")  # last enter commits
        panel._cursor = panel._rows.index(row_of(panel, "git_isolation"))
        await pilot.press("enter")
        await pilot.pause()
        panel.action_apply()
        await pilot.pause()
        await pilot.pause()

        written = dash.config_path.read_text(encoding="utf-8")
        assert comments(written) == comments(CORPUS)
        assert len(only_changed(CORPUS, written)) == 2
        parsed = tomllib.loads(written)
        assert parsed["swarm"]["max_workers"] == 8
        assert parsed["git"]["isolation"] == "worktree"
        assert calls == [["reload"]], "apply must offer the hot reload"
        # `swarm reload` names every change as `[swarm].max_workers`, which Rich
        # reads as a style tag: unescaped, the only line worth reading vanishes.
        assert "[swarm].max_workers" in screen_text(pilot.app)

    form(cfg, capfd, body, CORPUS)


def test_a_dry_run_previews_the_unsaved_form_against_the_running_swarm(cfg, capfd):
    """The preview has to describe the *pending* edits.

    Shelling out to ``swarm reload --dry-run`` cannot: it reads the file on disk,
    which by definition does not yet contain what the form is about to write.
    """
    from dataclasses import fields as dataclass_fields

    # Exactly what the supervisor writes at startup: max_workers 4 here, while
    # the file on disk says 1. The preview must quote the snapshot's 4, because
    # that is the number the running swarm is actually using.
    snapshot = {f.name: getattr(cfg, f.name) for f in dataclass_fields(cfg) if f.init}
    snapshot["project_dir"] = str(snapshot["project_dir"])
    (cfg.state_dir / "config.json").write_text(json.dumps(snapshot), encoding="utf-8")

    async def body(pilot, panel, dash):
        row = row_of(panel, "max_workers")
        row.write("8")
        await pilot.pause()
        text = cf.preview(
            panel.pending_text(), Path(cfg.project_dir),
            cf.live_config(dash), cf.facts(dash),
        )
        assert "max_workers" in text
        assert "4 -> 8" in text, "the diff must start from the supervisor's snapshot"
        assert "applied now" in text

    form(cfg, capfd, body, CORPUS)


def test_the_dry_run_of_a_restart_only_field_says_it_is_refused(cfg, capfd):
    async def body(pilot, panel, dash):
        row_of(panel, "git_isolation").write("worktree")
        await pilot.pause()
        text = cf.preview(
            panel.pending_text(), Path(cfg.project_dir), dash.cfg, cf.facts(dash)
        )
        assert "REFUSED" in text
        assert "restart" in text

    form(cfg, capfd, body, CORPUS)


def test_an_external_edit_is_picked_up_without_eating_an_in_progress_one(cfg, capfd):
    """A tick that lands mid-keystroke must not overwrite what is being typed."""
    async def body(pilot, panel, dash):
        row_of(panel, "max_workers").write("8")
        await pilot.pause()
        dash.config_path.write_text(
            cf.toml_set(CORPUS, "git", "main_branch", "trunk"), encoding="utf-8"
        )
        panel.update(dash)
        await pilot.pause()
        assert row_of(panel, "max_workers").read() == 8       # not clobbered
        assert row_of(panel, "git_isolation").read() == "none"
        assert row_of(panel, "git_main_branch").read() == "trunk"  # followed the file
        assert [r.setting.name for r in panel._rows if r.dirty] == ["max_workers"]

    form(cfg, capfd, body, CORPUS)


def test_j_and_k_move_the_cursor_without_editing_anything(cfg, capfd):
    async def body(pilot, panel, dash):
        await pilot.press("j", "j", "j")
        assert panel._cursor == 3
        await pilot.press("k")
        assert panel._cursor == 2
        for _ in range(len(panel._rows) + 5):
            await pilot.press("j")
        assert panel._cursor == len(panel._rows) - 1     # clamped, never off the end
        assert not any(r.dirty for r in panel._rows)

    form(cfg, capfd, body, CORPUS)


def test_a_read_only_field_is_shown_but_cannot_be_edited(cfg, capfd):
    async def body(pilot, panel, dash):
        for name in ("project_dir", "git_auto_resolve"):
            row = row_of(panel, name)
            assert row.locked
            panel._cursor = panel._rows.index(row)
            panel.action_edit()
            await pilot.pause()
            assert not row.dirty
        assert "PHASE-LEDGER" in cf.render_value(
            cf.SETTINGS["git_auto_resolve"], panel._cfg.git_auto_resolve
        )

    form(cfg, capfd, body, CORPUS)


def test_a_boolean_row_spells_its_state_out_beside_the_switch(cfg, capfd):
    """A Switch carries its state in colour alone, which is not enough here.

    This dashboard is meant to be read from across the room, and the two hues
    are the same shape; the word is what makes the row legible.
    """
    async def body(pilot, panel, dash):
        row = row_of(panel, "build_cache")
        label = row.query_one(".set-bool")
        assert "off" not in str(label.render()) and "on" in str(label.render())
        panel._cursor = panel._rows.index(row)
        await pilot.press("enter")
        await pilot.pause()
        assert "off" in str(label.render())
        assert row.read() is False

    form(cfg, capfd, body, CORPUS)


def test_a_bracketed_value_is_shown_and_not_eaten_as_markup(cfg, capfd):
    """``[git].auto_resolve`` maps a path glob to a strategy, and both halves can
    hold brackets — a real ledger rule is a regex full of them."""
    async def body(pilot, panel, dash):
        assert panel._cfg.git_auto_resolve == {"[L].md": "union"}
        panel._cursor = panel._rows.index(row_of(panel, "git_auto_resolve"))
        panel.action_move(0)
        await pilot.pause()
        assert "[L].md" in screen_text(pilot.app)

    form(cfg, capfd, body, '[git]\nauto_resolve = { "[L].md" = "union" }\n')


def test_a_broken_file_reports_its_error_even_when_it_contains_brackets(cfg, capfd):
    """tomllib quotes the offending syntax, so the message itself is markup bait."""
    async def body(pilot, panel, dash):
        assert "did not parse" in panel._error
        assert panel.query_one("#set-apply").disabled
        assert "Expected ']' at the end of a table" in screen_text(pilot.app)

    form(cfg, capfd, body, "[swarm\nmax_workers = 1\n")


def test_safe_neutralises_markup_without_changing_what_is_read():
    assert cf.safe("[swarm].max_workers") == "\\[swarm].max_workers"
    assert cf.safe(Path("/tmp/[a]/x")).endswith("x")


def test_a_radio_row_reads_back_what_was_just_written_in_the_same_frame(cfg, capfd):
    """A RadioSet learns its value from a child's message, one frame late.

    Anything that writes and then reads without yielding — apply straight after
    a toggle — would otherwise read the old value and drop the change on the
    floor, having already told the owner it was saved.
    """
    async def body(pilot, panel, dash):
        row = row_of(panel, "git_isolation")
        row.write("worktree")
        assert row.read() == "worktree"                       # no pause
        assert panel.edits() == {("git", "isolation"): "worktree"}
        await pilot.pause()
        assert row.read() == "worktree"                       # and after it lands

    form(cfg, capfd, body, CORPUS)


def test_apply_clears_the_dirty_marks_without_waiting_for_a_tick(cfg, capfd, monkeypatch):
    monkeypatch.setattr(
        cf.probes, "swarm",
        lambda *a, **k: type("R", (), {"text": "", "unavailable": False})(),
    )

    async def body(pilot, panel, dash):
        row_of(panel, "max_workers").write("6")
        row_of(panel, "git_isolation").write("worktree")
        panel.action_apply()
        assert not any(r.dirty for r in panel._rows)
        assert panel.query_one("#set-panel").border_subtitle == "no changes"
        parsed = tomllib.loads(dash.config_path.read_text(encoding="utf-8"))
        assert parsed["swarm"]["max_workers"] == 6
        assert parsed["git"]["isolation"] == "worktree"
        await pilot.pause()

    form(cfg, capfd, body, CORPUS)
