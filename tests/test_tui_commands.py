"""The Commands tab: discovery, filtering, the destructive set, prefill, history.

The pure half is tested as plain functions. The widget is driven through
Textual's pilot against a real temp project, because the parts that matter —
the confirm actually stopping a destructive command, output actually arriving
from a subprocess, ``ctrl+x`` actually ending one — only exist end to end.
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
import time

import pytest

pytest.importorskip("textual")

from swarm_orchestrator import cli  # noqa: E402
from swarm_orchestrator import reload as reload_mod  # noqa: E402
from swarm_orchestrator.config import load as load_config  # noqa: E402
from swarm_orchestrator.tui import commands as cm  # noqa: E402


# -- discovery -------------------------------------------------------------
def names() -> list[str]:
    return [spec.name for spec in cm.discover()]


def test_every_public_subcommand_is_offered_and_nothing_else():
    """The whole point of the tab: the CLI and the cockpit cannot drift apart."""
    assert set(names()) == cli._known_commands() - cm.HIDDEN


def test_private_plumbing_and_the_dashboard_itself_are_not_offered():
    offered = names()
    assert not [n for n in offered if n.startswith("_")]
    assert "tui" not in offered
    assert len(offered) == len(set(offered))


def test_commands_keep_the_parsers_order():
    assert names()[:2] == ["up", "down"]


def test_every_offered_command_says_what_it_does():
    assert [s.name for s in cm.discover() if not s.help] == []


def test_a_new_subcommand_appears_without_touching_the_tab():
    parser = argparse.ArgumentParser(prog="swarm")
    sub = parser.add_subparsers(dest="command")
    fresh = sub.add_parser("frobnicate", description="Frob the thing.\nAt length.")
    fresh.add_argument("phase")
    fresh.add_argument("--hidden", help=argparse.SUPPRESS)
    fresh.add_argument("--loud", action="store_true", help="shout")
    sub.add_parser("_internal")

    specs = cm.discover(parser)
    assert [s.name for s in specs] == ["frobnicate"]
    spec = specs[0]
    assert spec.help == "Frob the thing."  # no help=: first line of the description
    assert spec.phase_arg == "phase"
    assert [a.name for a in spec.args] == ["phase", "--loud"]
    assert spec.usage.startswith("swarm frobnicate")


def test_the_phase_argument_is_found_where_the_cli_names_it():
    by_name = {s.name: s for s in cm.discover()}
    assert by_name["why"].phase_arg == "phase"
    assert by_name["retry"].phase_arg == "phases"
    assert by_name["free"].phase_arg == "target"
    # `report --phase` is an option: prefilling it would narrow the report.
    assert by_name["report"].phase_arg is None
    assert by_name["status"].phase_arg is None


def test_suppressed_flags_stay_hidden():
    recap = next(s for s in cm.discover() if s.name == "recap")
    assert "--completion" not in [a.name for a in recap.args]
    assert "--completion" not in cm.spec_detail(recap)


# -- the destructive set ---------------------------------------------------
def test_the_destructive_set_names_only_real_commands():
    """A stale entry would be a confirm guarding a command that no longer exists
    — and, worse, a sign the one it was meant for got renamed unguarded."""
    assert set(cm.DESTRUCTIVE) <= set(names())


def test_the_destructive_set_is_exactly_the_commands_that_lose_work():
    assert set(cm.DESTRUCTIVE) == {
        "down", "finish", "free", "skip", "done", "retry", "integrate",
        "operator-done", "gc",
    }
    for harmless in ("status", "why", "report", "doctor", "pause", "resume",
                     "layout", "reload", "launch", "check", "context", "note"):
        assert cm.destructive_reason(harmless, []) is None, harmless


def test_gc_confirms_only_when_it_will_actually_delete():
    assert cm.destructive_reason("gc", []) is None
    assert cm.destructive_reason("gc", ["--aggressive"]) is None
    assert "deletes" in cm.destructive_reason("gc", ["--yes"])


def test_an_always_destructive_command_confirms_whatever_its_arguments():
    assert cm.destructive_reason("down", []) is not None
    assert cm.destructive_reason("retry", ["P3", "--keep-branch"]) is not None


def test_the_list_marks_destructive_commands_and_the_detail_says_why():
    by_name = {s.name: s for s in cm.discover()}
    assert by_name["down"].destructive and not by_name["status"].destructive
    assert "confirms first" in cm.spec_detail(by_name["down"])
    assert "confirms first" in cm.spec_detail(by_name["gc"])
    assert "with --yes" in cm.spec_detail(by_name["gc"])
    assert "confirms first" not in cm.spec_detail(by_name["status"])


# -- filtering -------------------------------------------------------------
def test_the_filter_searches_names_and_help_case_blind():
    specs = cm.discover()
    assert [s.name for s in specs if cm.matches(s, "STATUS")] == ["status"]
    assert "gc" in [s.name for s in specs if cm.matches(s, "reclaim disk")]
    assert all(cm.matches(s, "") for s in specs)
    assert not [s for s in specs if cm.matches(s, "no-such-thing")]


def test_every_word_of_the_filter_must_match():
    spec = cm.CommandSpec(name="retry", help="put a failed phase back in play", usage="")
    assert cm.matches(spec, "retry failed")
    assert not cm.matches(spec, "retry disk")


# -- arguments -------------------------------------------------------------
def test_a_phase_command_is_prefilled_with_the_selected_phase():
    why = cm.CommandSpec(name="why", help="", usage="", phase_arg="phase")
    status = cm.CommandSpec(name="status", help="", usage="")
    assert cm.prefill(why, "P3") == "P3"
    assert cm.prefill(why, None) == ""
    assert cm.prefill(status, "P3") == ""
    assert shlex.split(cm.prefill(why, "odd phase")) == ["odd phase"]


def test_the_phase_on_the_argument_line_is_the_tabs_selection():
    free = cm.CommandSpec(name="free", help="", usage="", phase_arg="target")
    retry = cm.CommandSpec(name="retry", help="", usage="", phase_arg="phases")
    status = cm.CommandSpec(name="status", help="", usage="")
    assert cm.phase_in(retry, "--cascade P7 P8") == "P7"
    assert cm.phase_in(free, "3") is None  # a slot id, not a phase
    assert cm.phase_in(free, "W2") == "W2"
    assert cm.phase_in(status, "P7") is None
    assert cm.phase_in(retry, "'unclosed") is None
    assert cm.phase_in(None, "P7") is None


def test_an_unclosed_quote_is_an_error_not_a_guess():
    assert cm.split_args('note P1 "two words"') == ["note", "P1", "two words"]
    with pytest.raises(ValueError):
        cm.split_args('note "P1')


def test_it_runs_this_interpreters_cli_against_the_dashboards_project(tmp_path):
    argv = cm.build_argv("why", ["P3"], tmp_path)
    assert argv == [sys.executable, "-m", "swarm_orchestrator",
                    "--project-dir", str(tmp_path), "why", "P3"]
    assert cm.command_line("note", ["P1", "two words"]) == "swarm note P1 'two words'"


# -- history ---------------------------------------------------------------
def test_a_run_reads_as_running_then_ok_failed_or_stopped():
    run = cm.Run(name="status", args=(), started_at=100.0)
    assert run.status == "running"
    assert run.took(now=103.0) == 3.0
    run.ended_at, run.code = 101.5, 0
    assert run.status == "ok" and run.took() == 1.5
    run.code = 2
    assert run.status == "failed"
    run.stopped = True
    assert run.status == "stopped"


def test_history_is_capped_and_shown_newest_first():
    history: list[cm.Run] = []
    for i in range(12):
        cm.remember(history, cm.Run(name=f"c{i}", args=(), started_at=float(i),
                                    ended_at=float(i) + 1, code=i % 2), keep=10)
    assert [r.name for r in history] == [f"c{i}" for i in range(2, 12)]
    lines = cm.history_lines(history, now=20.0, shown=3)
    assert len(lines) == 3
    assert "swarm c11" in lines[0] and "swarm c9" in lines[2]
    assert "failed 1" in lines[0] and "ok" in lines[1]


def test_short_runs_keep_their_tenths():
    assert cm.fmt_took(0.34) == "0.3s"
    assert cm.fmt_took(75) == "1m15s"


# -- the widget, end to end ------------------------------------------------
@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A real :class:`Config` on a temp project, with the SWARM_* env cleared.

    The subprocess inherits this environment, so the temp state dir is what
    every command the tab runs reads and writes.
    """
    project = tmp_path / "project"
    project.mkdir()
    for policy in reload_mod.POLICY.values():
        if policy.env:
            monkeypatch.delenv(policy.env, raising=False)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    cfg = load_config(project_dir=str(project))
    cfg.ensure_dirs()
    return cfg


def drive(cfg, capfd, body, phase: str | None = None):
    """Boot a one-tab app around :class:`Commands` and run ``body``.

    Capture is suspended because Textual's headless driver writes to the real
    stdout and deadlocks under pytest's fd capture; the run is bounded so a
    wedged event loop fails the test instead of hanging the suite.
    """
    from textual.app import App, ComposeResult

    from swarm_orchestrator.tui.dash import Dash

    dash = Dash(cfg)

    class Harness(App):
        def __init__(self) -> None:
            super().__init__()
            self.cfg = cfg

        def compose(self) -> ComposeResult:
            yield cm.Commands(id="tab-commands")

        def selected_phase(self):
            return phase

    async def run() -> None:
        app = Harness()
        async with app.run_test(size=(140, 48)) as pilot:
            tab = app.query_one(cm.Commands)
            tab.update(dash)
            await pilot.pause()
            await body(pilot, tab)

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(run(), timeout=60))


async def until(pilot, cond, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "timed out waiting on the tab"
        await pilot.pause(0.05)


def output(tab) -> str:
    log = tab.query_one("#cmd-output")
    return "\n".join("".join(seg.text for seg in strip) for strip in log.lines)


def last_run(tab):
    return tab._cmd_history[-1] if tab._cmd_history else None


def test_filter_pick_and_run_a_harmless_command(cfg, capfd):
    async def body(pilot, tab):
        assert len(tab._cmd_rows) == len(cm.discover())
        await pilot.press("j", "j", "k")
        assert tab.selected.name == tab._cmd_rows[1].name
        await pilot.press("slash")
        await pilot.press(*"status")
        await pilot.pause()
        assert [s.name for s in tab._cmd_rows] == ["status"], (tab._cmd_filter, pilot.app.focused)

        await pilot.press("escape")
        await pilot.pause()
        assert tab._cmd_filter == "" and len(tab._cmd_rows) == len(cm.discover())

        await pilot.press("slash", *"skip")  # `k` types into the box, not the cursor
        await pilot.pause()
        assert tab._cmd_filter == "skip"
        await pilot.press("escape")

        await pilot.press("slash", *"status", "enter")  # enter leaves the filter
        await pilot.press("enter")                      # enter on the row picks it
        await pilot.pause()
        assert tab._cmd_picked.name == "status"
        assert tab.query_one("#cmd-args").has_focus

        await pilot.press("f5")
        await until(pilot, lambda: last_run(tab) and last_run(tab).ended_at)
        await pilot.pause()
        run = last_run(tab)
        assert run.status == "ok", output(tab)
        assert "$ swarm status" in output(tab)
        assert "slug=" in output(tab)  # the real CLI's own output, streamed
        assert "exit 0" in output(tab)

        await pilot.press("ctrl+r")
        await until(pilot, lambda: len(tab._cmd_history) == 2 and last_run(tab).ended_at)
        assert last_run(tab).line == "swarm status"

    drive(cfg, capfd, body)


def test_a_phase_command_is_prefilled_from_the_apps_selection(cfg, capfd):
    async def body(pilot, tab):
        tab.set_filter("why")
        await pilot.pause()
        tab.table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert tab._cmd_picked.name == "why"
        assert tab.args_text == "P3"
        assert tab.selected_phase() == "P3"

    drive(cfg, capfd, body, phase="P3")


def test_a_destructive_command_runs_only_after_y(cfg, capfd):
    async def body(pilot, tab):
        tab.set_filter("skip")
        await pilot.pause()
        tab.table.focus()
        await pilot.press("enter", *"P9", "f5")
        await pilot.pause()
        assert isinstance(pilot.app.screen, cm.ConfirmRun)
        assert "swarm skip P9" in pilot.app.screen.line

        await pilot.press("n")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, cm.ConfirmRun)
        assert tab._cmd_history == []
        assert "not run: swarm skip P9" in output(tab)

        await pilot.press("f5")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert tab._cmd_history == []

        await pilot.press("f5")
        await pilot.pause()
        await pilot.press("y")
        await until(pilot, lambda: last_run(tab) and last_run(tab).ended_at)
        assert last_run(tab).status == "ok", output(tab)
        assert "skipped P9" in output(tab)

    drive(cfg, capfd, body)


def test_ctrl_x_stops_a_running_command(cfg, capfd):
    async def body(pilot, tab):
        tab.set_filter("build")
        await pilot.pause()
        tab.table.focus()
        await pilot.press("enter", *"sleep 30", "f5")
        await until(pilot, lambda: tab._cmd_proc is not None)
        await pilot.press("f5")  # one at a time
        await pilot.pause()
        assert "still running" in output(tab)
        assert len(tab._cmd_history) == 1

        await pilot.press("ctrl+x")  # focus is in the argument box, where Input owns ctrl+x
        await until(pilot, lambda: last_run(tab).ended_at, timeout=10)
        assert last_run(tab).status == "stopped"
        assert last_run(tab).took() < 10

    drive(cfg, capfd, body)


def test_quitting_mid_command_ends_it_instead_of_hanging_on_it(cfg, capfd):
    """The reader thread blocks on the child's stdout, and the app waits for its
    workers on the way out: a still-running ``build`` would hold ``q`` hostage."""
    started = {}

    async def body(pilot, tab):
        tab.set_filter("build")
        await pilot.pause()
        tab.table.focus()
        await pilot.press("enter", *"sleep 30", "f5")
        await until(pilot, lambda: tab._cmd_proc is not None)
        started["proc"] = tab._cmd_proc
        started["at"] = time.monotonic()

    drive(cfg, capfd, body)
    assert time.monotonic() - started["at"] < 15
    assert started["proc"].wait(timeout=5) is not None


def test_the_real_app_mounts_the_tab_instead_of_the_fallback(cfg, capfd, monkeypatch):
    from swarm_orchestrator.tui.app import SwarmApp
    from swarm_orchestrator.tui.dash import Dash

    monkeypatch.setattr(Dash, "probe", lambda self: None)
    app = SwarmApp(cfg)

    async def run() -> None:
        async with app.run_test(size=(140, 48)) as pilot:
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.query_one("#tab-commands"), cm.Commands)
            tab = app.query_one("#tab-commands")
            assert app.active_tab is tab
            assert app.focused is tab.table  # arrives ready for `/`, enter and f5

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(run(), timeout=30))
