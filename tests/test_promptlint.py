"""promptlint: the sentences an audit of the shipped prompts measured, and the fixes that must stay clean."""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_orchestrator import promptlint
from swarm_orchestrator.cli import _known_commands

REPO = Path(__file__).resolve().parent.parent
KNOWN = _known_commands()


def rules(text: str) -> list[tuple[int, str, str]]:
    return [(f.line, f.severity, f.rule) for f in promptlint.lint(text, known_commands=KNOWN)]


# -- the shipped prompts ----------------------------------------------------
@pytest.mark.parametrize("name", ["init_master.md", "resolver.md", "operator.md", "overseer.md"])
def test_every_shipped_prompt_lints_clean(name):
    text = (REPO / "prompts" / name).read_text(encoding="utf-8")
    assert promptlint.lint(text, known_commands=KNOWN) == []


# -- contradicted -------------------------------------------------------------
def test_the_audited_sentence_is_contradicted_even_with_its_subject_a_line_up():
    text = "Spawn a read-only survey agent for the big picture.\nRun it synchronously; you need its report before you can ask.\n"
    assert rules(text) == [(2, "contradicted", "agent-synchronous")]


def test_saying_agents_are_not_synchronous_is_clean():
    assert rules("The `Agent` call is not synchronous: subagents report back later.\n") == []


def test_an_unknown_subcommand_in_code_is_contradicted():
    assert rules("When stuck, run `swarm frobnicate W3`.\n") == [(1, "contradicted", "unknown-command")]


def test_real_subcommands_and_prose_are_not_commands():
    text = "The swarm has a supervisor, and the swarm can wait.\nRun `swarm done W3 ok \"recap\"` and `swarm context`.\n"
    assert rules(text) == []


def test_unknown_commands_are_read_from_fenced_blocks_too():
    text = "```bash\nswarm launch W3\nswarm teleport W4\n```\n"
    assert rules(text) == [(3, "contradicted", "unknown-command")]


def test_without_known_commands_the_command_rule_is_skipped_not_guessed():
    assert promptlint.lint("run `swarm frobnicate`\n") == []


def test_claiming_fail_is_silent_is_contradicted():
    text = "None of `ok`, `operator` or `fail` sends a Telegram.\n"
    assert rules(text) == [(1, "contradicted", "telegram-silent")]


def test_the_silent_statuses_may_be_called_silent():
    """`needs-owner` used to ping; `operator` replaced it and does not (statuses.PINGS)."""
    assert rules("`operator` never sends a Telegram; it opens a session.\n") == []


def test_a_finish_that_never_telegrams_is_true_and_clean():
    assert rules("The finish that sent you here never telegrams anyone, and that is deliberate.\n") == []


def test_a_retired_status_as_an_instruction_is_contradicted():
    text = "Finish with `needs-owner` when the owner should review something.\n"
    assert rules(text) == [(1, "contradicted", "retired-status")]


def test_naming_a_retired_status_as_retired_is_clean():
    assert rules("If the retired `needs-owner` status is present, upgrade it.\n") == []


# -- wasteful -----------------------------------------------------------------
def test_a_sleep_loop_is_wasteful():
    text = "Wait for it:\n```bash\nuntil [ -f done ]; do sleep 30; done\n```\n"
    assert rules(text) == [(3, "wasteful", "sleep-wait")]


def test_banning_sleep_loops_is_clean():
    assert rules("No `sleep 30` loops and no `git status` polling — use `Monitor`.\n") == []


def test_polling_git_status_is_wasteful():
    assert rules("Poll `git status --short` until the teammate has committed.\n") == [
        (1, "wasteful", "status-poll")
    ]


def test_sed_in_place_edits_are_wasteful_unless_advised_against():
    assert rules("Apply it with `sed -i 's/a/b/' src/x.rs`.\n") == [(1, "wasteful", "heredoc-edit")]
    assert rules("Use `Edit`, not a heredoc:\nthose `sed -i` edits failed 6x as often.\n") == []


# -- render / swarm check -------------------------------------------------------
def test_render_names_the_path_the_line_and_the_rule():
    out = promptlint.render(promptlint.lint("run `swarm frobnicate`\n", known_commands=KNOWN), path="p.md")
    assert out.splitlines()[0] == "p.md: 1 finding(s), 1 contradicted"
    assert "1  contradicted" in out and "unknown-command" in out and "> run `swarm frobnicate`" in out


def test_render_of_nothing_is_clean():
    assert promptlint.render([], path="p.md") == "p.md: clean"


def test_swarm_check_fails_on_a_contradicted_worker_prompt(swarm):
    cmd = swarm.project / ".claude" / "commands" / "prime.md"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text("Spawn a survey agent.\nRun it synchronously.\n", encoding="utf-8")

    result = swarm.cli("check", check=False)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "agent-synchronous" in result.stdout


def test_swarm_check_passes_a_worker_prompt_that_is_only_wasteful(swarm):
    cmd = swarm.project / ".claude" / "commands" / "prime.md"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text("Wait with `sleep 60` between checks.\n", encoding="utf-8")

    result = swarm.cli("check", check=False)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "sleep-wait" in result.stdout
    assert "passed with warnings" in result.stdout


def test_swarm_check_strict_fails_on_a_wasteful_worker_prompt(swarm):
    """`--strict` makes a warning fatal; without it, it only prints."""
    cmd = swarm.project / ".claude" / "commands" / "prime.md"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text("Wait with `sleep 60` between checks.\n", encoding="utf-8")

    result = swarm.cli("check", "--strict", check=False)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "fatal under --strict" in result.stdout
