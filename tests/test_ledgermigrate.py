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
