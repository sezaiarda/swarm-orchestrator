"""Tests for mechanical conflict resolution (``automerge`` + ``gitq._auto_resolve``).

Ledger conflicts come in two common shapes — two phases ticking
their own adjacent ledger line, and two phases prepending a dated block to a
journal — and each one otherwise takes a resolver session to splice by hand. This module
settles both with no model in the loop. What these tests hold it to:

* **correct, not merely conflict-free** — ``merge=union`` on the ledger "worked"
  and left a stale un-ticked line beside the ticked one, so every keyed case
  counts lines, not just the absence of ``None``;
* **declines rather than guesses** — a genuine both-sides-changed-it case must
  come back ``None`` so the resolver path takes over unchanged;
* **all or nothing at the git layer** — a partial resolve hands the resolver a
  half-fixed tree, which the module itself calls worse than the original.

``_auto_resolve`` is driven through a stub ``_git`` so no repository, identity or
merge is needed; the stub serves the three index stages a real conflict has.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm_orchestrator import automerge, gitq

#: The live ledger pattern, as a typical project configures it.
LEDGER_KEY = r"^- \[[ x]\] `([A-Za-z0-9_.-]+)`"
KEYED = f"keyed:{LEDGER_KEY}"

LEDGER = (
    "# Phase ledger\n"
    "\n"
    "- [ ] `P1` parser\n"
    "- [ ] `P2` lexer\n"
    "- [ ] `P3` codegen\n"
)


def lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def tick(text: str, phase: str) -> str:
    return text.replace(f"- [ ] `{phase}`", f"- [x] `{phase}`")


# -- merge3 -----------------------------------------------------------------
def test_one_sided_changes_are_taken_whole():
    base = ["a\n", "b\n", "c\n"]
    assert automerge.merge3(base, ["a\n", "B\n", "c\n"], base) == ["a\n", "B\n", "c\n"]
    assert automerge.merge3(base, base, ["a\n", "b\n", "C\n"]) == ["a\n", "b\n", "C\n"]


def test_an_identical_change_on_both_sides_lands_once():
    base = ["a\n", "b\n"]
    same = ["a\n", "B\n"]
    assert automerge.merge3(base, same, same) == same


def test_a_one_sided_deletion_is_honoured():
    base = ["a\n", "b\n", "c\n"]
    assert automerge.merge3(base, ["a\n", "c\n"], base) == ["a\n", "c\n"]


def test_a_real_conflict_declines_without_union():
    """Both sides rewrote the same line differently: nobody may guess."""
    base = ["a\n", "b\n", "c\n"]
    assert automerge.merge3(base, ["a\n", "X\n", "c\n"], ["a\n", "Y\n", "c\n"]) is None


def test_delete_versus_modify_is_a_conflict():
    base = ["a\n", "b\n", "c\n"]
    assert automerge.merge3(base, ["a\n", "c\n"], ["a\n", "B\n", "c\n"]) is None


def test_union_keeps_both_prepended_journal_blocks():
    """The zero-width insertion case opcode-walking silently dropped."""
    base = ["old entry\n"]
    ours = ["## 09-02 ours\n", "old entry\n"]
    theirs = ["## 09-02 theirs\n", "old entry\n"]
    assert automerge.merge3(base, ours, theirs, union=True) == [
        "## 09-02 ours\n",
        "## 09-02 theirs\n",
        "old entry\n",
    ]
    assert automerge.merge3(base, ours, theirs) is None


def test_union_keeps_both_appends_ours_first():
    base = ["head\n"]
    got = automerge.merge3(base, ["head\n", "x\n"], ["head\n", "y\n"], union=True)
    assert got == ["head\n", "x\n", "y\n"]


def test_union_of_two_new_files():
    """Both sides created the file: the base is empty and nothing is stable."""
    assert automerge.merge3([], ["x\n"], ["y\n"], union=True) == ["x\n", "y\n"]
    assert automerge.merge3([], ["x\n"], ["y\n"]) is None


def test_inserts_keeps_two_additions_but_not_two_rewrites():
    base = ["a", "b"]
    assert automerge.merge3(base, ["a", "x", "b"], ["a", "y", "b"], inserts=True) == [
        "a", "x", "y", "b",
    ]
    assert automerge.merge3(base, ["x", "b"], ["y", "b"], inserts=True) is None


# -- keyed_merge ------------------------------------------------------------
def test_adjacent_ticks_both_land_with_no_stale_line():
    """THE ledger case. Union leaves an extra stale line beside the ticked one."""
    got = automerge.keyed_merge(LEDGER, tick(LEDGER, "P1"), tick(LEDGER, "P2"), LEDGER_KEY)
    assert got == tick(tick(LEDGER, "P1"), "P2")
    assert len(lines(got)) == len(lines(LEDGER))
    assert "- [ ] `P1`" not in got and "- [ ] `P2`" not in got


def test_the_same_tick_on_both_sides_is_not_a_conflict():
    ticked = tick(LEDGER, "P1")
    assert automerge.keyed_merge(LEDGER, ticked, ticked, LEDGER_KEY) == ticked


def test_both_sides_rewriting_the_same_words_declines():
    ours = LEDGER.replace("`P1` parser", "`P1` tokenizer")
    theirs = LEDGER.replace("`P1` parser", "`P1` grammar")
    assert automerge.keyed_merge(LEDGER, ours, theirs, LEDGER_KEY) is None


def test_a_tick_and_a_note_on_one_row_both_land():
    """One side ticks a row, the other appends a note to the same line: two
    edits in different places of one record, not a disagreement."""
    ours = tick(LEDGER, "P2")
    theirs = LEDGER.replace("`P2` lexer", "`P2` lexer · *(note: rerun)*")
    got = automerge.keyed_merge(LEDGER, ours, theirs, LEDGER_KEY)
    assert got == tick(theirs, "P2")


def test_two_notes_appended_to_one_row_keep_both_ours_first():
    ours = LEDGER.replace("`P2` lexer", "`P2` lexer · A")
    theirs = LEDGER.replace("`P2` lexer", "`P2` lexer · B")
    got = automerge.keyed_merge(LEDGER, ours, theirs, LEDGER_KEY)
    assert got == LEDGER.replace("`P2` lexer", "`P2` lexer · A · B")


def test_continuation_lines_travel_with_their_record():
    ours = LEDGER.replace("`P1` parser\n", "`P1` parser\n  note: needs a rerun\n")
    theirs = tick(LEDGER, "P3")
    got = automerge.keyed_merge(LEDGER, ours, theirs, LEDGER_KEY)
    assert got is not None
    assert "- [ ] `P1` parser\n  note: needs a rerun\n- [ ] `P2`" in got
    assert "- [x] `P3`" in got


def test_a_record_added_on_one_side_is_kept_in_its_order():
    theirs = LEDGER + "- [ ] `P4` linker\n"
    got = automerge.keyed_merge(LEDGER, tick(LEDGER, "P1"), theirs, LEDGER_KEY)
    assert got == tick(LEDGER, "P1") + "- [ ] `P4` linker\n"


def test_both_sides_adding_records_declines_rather_than_interleaving():
    ours = LEDGER + "- [ ] `P4` linker\n"
    theirs = LEDGER + "- [ ] `P5` loader\n"
    assert automerge.keyed_merge(LEDGER, ours, theirs, LEDGER_KEY) is None


def test_the_preamble_is_three_way_merged_too():
    ours = LEDGER.replace("# Phase ledger", "# Phase ledger (campaign 2)")
    got = automerge.keyed_merge(LEDGER, ours, tick(LEDGER, "P2"), LEDGER_KEY)
    assert got == tick(ours, "P2")


def test_a_non_identifying_key_declines():
    dup = LEDGER + "- [ ] `P1` parser again\n"
    assert automerge.keyed_merge(LEDGER, dup, tick(LEDGER, "P2"), LEDGER_KEY) is None


def test_an_unparseable_pattern_declines_instead_of_raising():
    assert automerge.keyed_merge(LEDGER, LEDGER, LEDGER, "([unclosed") is None


def test_a_one_sided_record_deletion_is_honoured():
    ours = LEDGER.replace("- [ ] `P3` codegen\n", "")
    assert automerge.keyed_merge(LEDGER, ours, LEDGER, LEDGER_KEY) == ours


def test_delete_versus_tick_of_one_record_declines():
    ours = LEDGER.replace("- [ ] `P3` codegen\n", "")
    theirs = tick(LEDGER, "P3")
    assert automerge.keyed_merge(LEDGER, ours, theirs, LEDGER_KEY) is None


# -- ledger conflict fixtures ------------------------------------------------
#: Synthetic same-row ledger conflicts of the kind a resolver session would
#: otherwise splice by hand, cut down to the conflicting record (untouched
#: stretches of the row removed identically on every side). ``resolved.md`` is
#: what a resolver would commit.
CONFLICTS = Path(__file__).parent / "fixtures" / "ledger_conflicts"


def _conflict(name: str) -> tuple[str, ...]:
    return tuple(
        (CONFLICTS / name / f"{side}.md").read_text(encoding="utf-8")
        for side in ("base", "ours", "theirs")
    )


@pytest.mark.parametrize("name", ["api-F17", "web-W10-op2", "web-W10-op4"])
def test_same_row_conflicts_merge_as_a_resolver_would(name):
    """A tick or Overseer edit against a worker's note, and operator notes
    appended from mirrors that branched off an older main."""
    got = automerge.keyed_merge(*_conflict(name), LEDGER_KEY)
    assert got == (CONFLICTS / name / "resolved.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["web-W10-op3", "ask-web-W10-op3"])
def test_appended_notes_all_survive(name):
    """A resolver would tidy these by hand (a separator, a near-duplicate note);
    the mechanical merge keeps every word each side added."""
    base, ours, theirs = _conflict(name)
    got = automerge.keyed_merge(base, ours, theirs, LEDGER_KEY)
    assert got is not None
    for side in (ours, theirs):
        added = set(side.split()) - set(base.split())
        assert added <= set(got.split())


def test_a_row_whose_continuation_lines_both_sides_rewrote_declines():
    """One side dropped a blank continuation line where the other wrote a new
    one: a structural edit the resolver has to look at."""
    assert automerge.keyed_merge(*_conflict("api-F31"), LEDGER_KEY) is None


# -- strategy dispatch ------------------------------------------------------
def test_strategy_is_matched_by_glob_or_path_suffix():
    table = {"docs/*.md": "union", "PHASE-LEDGER.md": KEYED}
    assert automerge.strategy_for("docs/FINDINGS.md", table) == "union"
    assert automerge.strategy_for("src/main.rs", table) is None
    # a bare filename configures the file wherever the repo keeps it
    assert automerge.strategy_for("ledger/PHASE-LEDGER.md", table) == KEYED


def test_the_first_matching_entry_wins():
    table = {"docs/PHASE-LEDGER.md": KEYED, "docs/*.md": "union"}
    assert automerge.strategy_for("docs/PHASE-LEDGER.md", table) == KEYED


def test_a_bare_filename_does_not_match_a_longer_name():
    assert automerge.strategy_for("docs/release-notes.md", {"notes.md": "union"}) is None


def test_resolve_text_dispatches_each_strategy():
    base, ours, theirs = "a\n", "x\na\n", "y\na\n"
    assert automerge.resolve_text(base, ours, theirs, "union") == "x\ny\na\n"
    got = automerge.resolve_text(LEDGER, tick(LEDGER, "P1"), tick(LEDGER, "P3"), KEYED)
    assert got == tick(tick(LEDGER, "P1"), "P3")
    assert automerge.resolve_text(base, ours, theirs, "ours") is None


# -- gitq._auto_resolve -----------------------------------------------------
class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def line(self, text: str) -> None:
        self.lines.append(text)


MARKERS = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> swarm/P1\n"


class FakeMerge:
    """A repo stopped mid-merge: ``stages[path] = (base, ours, theirs)``."""

    def __init__(self, repo: Path, stages: dict[str, tuple[str, str, str]]) -> None:
        self.repo = repo
        self.stages = stages
        self.calls: list[tuple[str, ...]] = []
        for rel in stages:
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(MARKERS, encoding="utf-8")

    def __call__(self, repo, *args, check=True, timeout=None):
        self.calls.append(args)
        out, rc = "", 0
        if args[:1] == ("diff",):
            out = "".join(f"{rel}\n" for rel in self.stages)
        elif args[:1] == ("show",):
            num, rel = args[1][1:].split(":", 1)
            out = self.stages[rel][int(num) - 1]
        elif args[:1] == ("log",):
            out = "\n".join(self.stages) + "\n"
        return subprocess.CompletedProcess(["git", *args], rc, out, "")

    def did(self, verb: str) -> bool:
        return any(call[:1] == (verb,) for call in self.calls)


def _cfg(table: dict[str, str], checks: dict[str, str] | None = None):
    return SimpleNamespace(git_auto_resolve=table, git_auto_resolve_check=checks or {})


def test_auto_resolve_with_no_strategies_never_touches_git(tmp_path, monkeypatch):
    fake = FakeMerge(tmp_path, {"a.md": ("a\n", "x\na\n", "y\na\n")})
    monkeypatch.setattr(gitq, "_git", fake)
    assert gitq._auto_resolve(_cfg({}), tmp_path, "P1", _Log()) is False
    assert fake.calls == []


def test_auto_resolve_settles_every_file_then_commits(tmp_path, monkeypatch):
    fake = FakeMerge(
        tmp_path,
        {
            "docs/FINDINGS.md": ("old\n", "ours\nold\n", "theirs\nold\n"),
            "docs/PHASE-LEDGER.md": (LEDGER, tick(LEDGER, "P1"), tick(LEDGER, "P2")),
        },
    )
    monkeypatch.setattr(gitq, "_git", fake)
    log = _Log()
    table = {"docs/PHASE-LEDGER.md": KEYED, "docs/*.md": "union"}

    assert gitq._auto_resolve(_cfg(table), tmp_path, "P1", log) is True

    assert (tmp_path / "docs/FINDINGS.md").read_text() == "ours\ntheirs\nold\n"
    assert (tmp_path / "docs/PHASE-LEDGER.md").read_text() == tick(tick(LEDGER, "P1"), "P2")
    added = [c[-1] for c in fake.calls if c[:1] == ("add",)]
    assert added == ["docs/FINDINGS.md", "docs/PHASE-LEDGER.md"]
    assert fake.did("commit")
    assert any(ln.startswith("INTEGRATE-AUTORESOLVED P1") for ln in log.lines), log.lines
    # every touched file is on the record, not just the repaired ones — and the
    # log query must name a merge commit's files, which plain --name-only does not
    log_call = next(c for c in fake.calls if c[:1] == ("log",))
    assert "--diff-merges=first-parent" in log_call
    assert any(ln.startswith("AUTORESOLVE-TOUCHED P1") for ln in log.lines)


def test_a_file_without_a_strategy_hands_over_the_whole_merge(tmp_path, monkeypatch):
    fake = FakeMerge(
        tmp_path,
        {"src/lib.rs": ("a\n", "b\n", "c\n"), "docs/FINDINGS.md": ("o\n", "x\no\n", "y\no\n")},
    )
    monkeypatch.setattr(gitq, "_git", fake)
    log = _Log()

    assert gitq._auto_resolve(_cfg({"docs/*.md": "union"}), tmp_path, "P1", log) is False

    assert not fake.did("add") and not fake.did("commit")
    assert (tmp_path / "docs/FINDINGS.md").read_text() == MARKERS
    assert any("AUTORESOLVE-SKIP P1" in ln and "no-strategy" in ln for ln in log.lines)


def test_a_declined_merge_is_logged_and_nothing_is_staged(tmp_path, monkeypatch):
    base = "a\nb\n"
    fake = FakeMerge(tmp_path, {"docs/X.md": (base, "a\nX\n", "a\nY\n")})
    monkeypatch.setattr(gitq, "_git", fake)
    log = _Log()

    assert gitq._auto_resolve(_cfg({"docs/X.md": KEYED}), tmp_path, "P1", log) is False
    assert not fake.did("add") and not fake.did("commit")
    assert any(ln.startswith("AUTORESOLVE-DECLINED P1") for ln in log.lines)


def test_a_late_decline_leaves_earlier_files_as_the_merge_left_them(tmp_path, monkeypatch):
    fake = FakeMerge(
        tmp_path,
        {
            "docs/FINDINGS.md": ("o\n", "x\no\n", "y\no\n"),  # union: resolvable
            "docs/PHASE-LEDGER.md": (  # keyed: same words rewritten twice -> declines
                LEDGER,
                LEDGER.replace("parser", "tokenizer"),
                LEDGER.replace("parser", "grammar"),
            ),
        },
    )
    monkeypatch.setattr(gitq, "_git", fake)
    table = {"docs/PHASE-LEDGER.md": KEYED, "docs/*.md": "union"}

    assert gitq._auto_resolve(_cfg(table), tmp_path, "P1", _Log()) is False
    assert (tmp_path / "docs/FINDINGS.md").read_text() == MARKERS


def _ledger_merge(tmp_path, monkeypatch) -> FakeMerge:
    fake = FakeMerge(
        tmp_path,
        {"docs/PHASE-LEDGER.md": (LEDGER, tick(LEDGER, "P1"), tick(LEDGER, "P2"))},
    )
    monkeypatch.setattr(gitq, "_git", fake)
    return fake


def test_a_passing_check_runs_on_the_merged_text_then_commits(tmp_path, monkeypatch):
    fake = _ledger_merge(tmp_path, monkeypatch)
    checks = {"docs/PHASE-LEDGER.md": "grep -c 'x\\] `P2`' docs/PHASE-LEDGER.md > seen"}

    assert gitq._auto_resolve(_cfg({"docs/PHASE-LEDGER.md": KEYED}, checks), tmp_path, "P1", _Log())

    assert (tmp_path / "seen").read_text().strip() == "1"  # ran in the repo, on merged text
    assert fake.did("commit")


def test_a_failing_check_hands_the_untouched_conflict_to_the_resolver(tmp_path, monkeypatch):
    fake = _ledger_merge(tmp_path, monkeypatch)
    log = _Log()
    checks = {"docs/PHASE-LEDGER.md": "echo 'two same-repo phases runnable at once'; exit 1"}

    assert gitq._auto_resolve(_cfg({"docs/PHASE-LEDGER.md": KEYED}, checks), tmp_path, "P1", log) is False

    assert (tmp_path / "docs/PHASE-LEDGER.md").read_text() == MARKERS
    assert not fake.did("add") and not fake.did("commit")
    failed = [ln for ln in log.lines if ln.startswith("AUTORESOLVE-CHECK-FAILED P1")]
    assert failed and "same-repo phases" in failed[0], log.lines


def test_a_check_runs_only_for_the_paths_it_names(tmp_path, monkeypatch):
    """A component repo has no ledger gate: its journal merge must not run it."""
    fake = FakeMerge(tmp_path, {"docs/STATUS.md": ("o\n", "x\no\n", "y\no\n")})
    monkeypatch.setattr(gitq, "_git", fake)
    checks = {"docs/PHASE-LEDGER.md": "exit 1"}

    assert gitq._auto_resolve(_cfg({"STATUS.md": "union"}, checks), tmp_path, "P1", _Log())
    assert fake.did("commit")


def test_a_bare_journal_key_matches_that_journal_in_every_repo():
    """Keys match the path inside whichever repo is being merged, so a bare
    ``CHANGELOG.md`` covers the root changelog of every component and a nested
    crate's too — but never a file that merely ends in the same letters."""
    table = {"CHANGELOG.md": "union", "tasks/lessons.md": "union"}
    for path in ("CHANGELOG.md", "crates/core/CHANGELOG.md"):
        assert automerge.strategy_for(path, table) == "union", path
    assert automerge.strategy_for("tasks/lessons.md", table) == "union"
    assert automerge.strategy_for("OLD-CHANGELOG.md", table) is None
    assert automerge.strategy_for("tasks/lessons.md.bak", table) is None
    # a glob works too, for projects that want to be explicit about it
    assert automerge.strategy_for("docs/CHANGELOG.md", {"**/CHANGELOG.md": "union"}) == "union"
