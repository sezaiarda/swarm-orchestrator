"""The one-time move of ledger row notes and the STATUS journal into history."""

from __future__ import annotations

from swarm_orchestrator import ledgermigrate, ledgerw


FAT = """# L

> a header paragraph that stays

## A
- [x] `a-W1` · dir:`alpha` · needs:— · *(ordering only: `a-W0` (see x))* **the lead sentence.** Then pages of detail · more · **TAG:`v1`**
      _(2026-07-03: a continuation note)_
  - a nested bullet

- [ ] `a-W2` · dir:`alpha` · needs:`a-W1`,`a-W9` · short row
Some prose at the margin.
"""


def test_migration_slims_rows_and_proves_the_ledger_reads_the_same(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "L.md").write_text(FAT)
    (tmp_path / "docs" / "S.md").write_text(
        "# STATUS\n\nIntro line.\n\n**`a-W1` CLOSED** entry\n- bullet\n\n## Standing\n\nkept\n\n"
        "## Session log\n\nold\n")
    report = ledgermigrate.migrate(tmp_path, "docs/L.md", "h", 256, "docs/S.md", True, "2026-09-27")
    assert any(ln.startswith("equivalence: the ledger reads the same") for ln in report), report
    slim = (tmp_path / "docs" / "L.md").read_text()
    assert "- [x] `a-W1` · dir:`alpha` · needs:— · **the lead sentence.** · **TAG:`v1`**" in slim
    assert "- [ ] `a-W2` · dir:`alpha` · needs:`a-W1`,`a-W9` · short row" in slim
    assert "continuation" not in slim and "Some prose at the margin." in slim
    hist = ledgerw.history_text(tmp_path, "h", "a-W1")
    assert "a continuation note" in hist and "- a nested bullet" in hist
    page = (tmp_path / "docs" / "S.md").read_text()
    assert "CLOSED" not in page and "## Standing" in page and "Session log" not in page
    assert "CLOSED" in (tmp_path / "h" / "STATUS-archive.md").read_text()
    # A second run finds nothing to move.
    again = ledgermigrate.migrate(tmp_path, "docs/L.md", "h", 256, "docs/S.md", True, "2026-09-28")
    assert again[0].endswith("0 of 2 rows slimmed") and "unchanged" in again[1]
    assert (tmp_path / "docs" / "L.md").read_text() == slim


COMPONENT = """# repo — STATUS

> Living status doc.

**Last updated:** 2026-09-07 (a top journal paragraph)

## State — 2026-09-27: `x-W2`, the newest

new entry

## Next action

do the thing

## Previous state: **older** (0.2.0)

older entry

## Gates (Phase 1 — all green, 2026-07-05)

gate list

## Decision log

- ADR-1
"""


def test_a_component_status_keeps_its_standing_sections_and_archives_the_journal(tmp_path):
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / "docs" / "STATUS.md").write_text(COMPONENT)
    note = ledgermigrate.component_note("docs/phases")
    line = ledgermigrate.move_status(repo, "docs/STATUS.md", "docs/phases", note, True)
    assert "->" in line and "FAILED" not in line
    page = (repo / "docs" / "STATUS.md").read_text()
    assert page.startswith("# repo — STATUS\n\n> Living status doc.\n\n**Where things are now.**")
    assert "## Next action" in page and "## Decision log" in page
    for gone in ("Last updated", "newest", "older entry", "gate list"):
        assert gone not in page
    arch = (repo / "docs" / "phases" / "STATUS-archive.md").read_text()
    assert arch.index("Last updated") < arch.index("newest") < arch.index("older entry") < arch.index("gate list")
    assert ledgermigrate.component_repos(tmp_path) == [repo]
    again = ledgermigrate.move_status(repo, "docs/STATUS.md", "docs/phases", note, True)
    assert again.endswith("unchanged") and (repo / "docs" / "STATUS.md").read_text() == page
