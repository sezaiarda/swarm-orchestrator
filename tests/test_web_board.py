"""The web board's data layer: columns, campaigns, card detail, redaction.

Built through the real :class:`web.feed.Feed` over a synthetic project and state
dir — the same path the server takes — so a column rule and the file it reads
are tested together.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from swarm_orchestrator import config as config_mod
from swarm_orchestrator import opqueue
from swarm_orchestrator.web import board as board_mod
from swarm_orchestrator.web import campaigns, rows
from swarm_orchestrator.web.feed import Feed
from swarm_orchestrator.web.redact import MARK, deep, redact

LEDGER = """\
# Test ledger

Intro prose that is not a row.

## Alpha — the first thing (`al-W0`…`al-W9`)

> Alpha exists to put a phase in every column.

- [x] `al-W0` · dir:`repo1` · needs:— · **the root that landed.**
- [ ] `al-W1` · dir:`repo1`,`repo2` · needs:`al-W0` · **ready now.** more words
- [ ] `al-W2` · needs:`al-W0` · **building in a slot.**
  - a continuation line that belongs to the row
- [ ] `al-W3` · needs:`al-W0` · **waiting on the owner.**
- [ ] `al-W4` · needs:`al-W0` · **parked.**
- [ ] `al-W5` · needs:`al-W0` · **in the merge queue.**
- [ ] `al-W6` · needs:`al-W0` · **holding the merge queue.**
- [ ] `al-W7` · needs:`al-W0` · **operator job queued.**
- [ ] `al-W8` · needs:`al-W0` · **failed.**
- [ ] `al-W9` · needs:`al-W8` · **blocked behind the failure.**

### beta (owner-run things)

- [ ] `be-W1` · needs:— · **owner-run, and it holds a row up.**
- [ ] `be-W2` · needs:`be-W1` · **blocked behind an excluded row.**
- [ ] `be-W3` · needs:— · **operator asks.**
- [x] `be-W4` · needs:— · **ticked only in the ledger.**
- [ ] `be-W5` · needs:— · **skipped.**
- [ ] `be-W6` · needs:— · **merged, push owed.** token=ghp_abcdefghijklmnopqrstuvwxyz0123
- [ ] `be-W7` · needs:`be-W4` · **ready: its dep is ticked, which counts as landed.**
- [x] `be-W8` · needs:— · **owner-run, and the owner ticked it.**
- [ ] `be-W9` · needs:— · **owner-run, holding nothing up.**
"""

EXPECTED = {
    "al-W0": "done", "al-W1": "ready", "al-W2": "building", "al-W3": "needs_you",
    "al-W4": "needs_you", "al-W5": "merging", "al-W6": "merging", "al-W7": "operator",
    "al-W8": "failed", "al-W9": "blocked", "be-W1": "needs_you", "be-W2": "blocked",
    "be-W3": "needs_you", "be-W4": "done", "be-W5": "done", "be-W6": "merging",
    "be-W7": "ready", "be-W8": "done", "be-W9": "excluded",
}


def _item(phase: str, state: str, **kw) -> dict:
    return opqueue.Item(phase=phase, status="operator", note=f"job for {phase}",
                        queued_at=time.time() - 60, state=state, **kw).to_dict()


def make_run(tmp_path: Path, monkeypatch) -> config_mod.Config:
    """A project + state dir holding one phase in every column."""
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "LEDGER.md").write_text(LEDGER, encoding="utf-8")
    (project / ".swarm.toml").write_text(
        '[tasks]\nledger = "docs/LEDGER.md"\nexclude = ["be-W1", "be-W8", "be-W9"]\n', encoding="utf-8")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SWARM_STATE_DIR", str(state_dir))
    cfg = config_mod.load(project_dir=str(project))
    cfg.ensure_dirs()
    state = {
        "slots": [{"id": 0, "pane_id": "%1", "busy": True, "phase": "al-W2",
                   "branch": "swarm/al-W2", "worktree": "/wt/al-W2"},
                  {"id": 1, "pane_id": "%2", "busy": True, "phase": "al-W3"},
                  {"id": 2, "pane_id": "%3", "busy": False, "phase": None}],
        "done": {"al-W0": "ok", "al-W7": "operator", "al-W8": "fail", "be-W3": "operator",
                 "be-W5": "skip"},
        "waiting": {"al-W3": time.time() + 300},
        "parked": ["al-W4"],
        "integ_queue": ["al-W6", "al-W5"],
        "integ_blocked": "al-W6",
        "integ_blocked_kind": "conflict",
        "push_owed": {"/repo": {"phase": "be-W6", "reason": "hook refused"}},
        "overseer_pass": "20260923T100000Z",
    }
    cfg.state_path.write_text(json.dumps(state), encoding="utf-8")
    op = cfg.operator_dir
    op.mkdir(parents=True)
    (op / "al-W7.json").write_text(json.dumps(_item("al-W7", opqueue.QUEUED)))
    (op / "be-W3.json").write_text(json.dumps(
        _item("be-W3", opqueue.WAITING, question="deploy now or tonight?")))
    (op / "op-1790000000.json").write_text(json.dumps(_item("op-1790000000", opqueue.QUEUED)))
    ov = state_dir / "overseer"
    ov.mkdir()
    (ov / "20260923T100000Z.json").write_text(json.dumps({
        "id": "20260923T100000Z", "status": "running", "started_at": time.time() - 30,
        "question": "retry al-W8 or leave it?", "reasons": [{"key": "failed", "text": "al-W8 failed"}]}))
    notes = state_dir / "notes"
    notes.mkdir()
    lines = [{"ts": 1.0, "phase": "al-W0", "kind": k, "text": f"{k} text"}
             for k in ("risk", "decision", "owner_decision", "assumption")]
    (notes / "al-W0.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    (cfg.done_dir / "al-W0.ok").write_text("al-W0 ok built it with sk-ant-api03-abcdefghijklmnopqrstu")
    (cfg.done_dir / "al-W0.jsonl").write_text(json.dumps(
        {"ts": 2.0, "status": "ok", "note": "first and only attempt"}) + "\n")
    (state_dir / "recaps").mkdir()
    (state_dir / "recaps" / "al-W0.json").write_text(json.dumps(
        {"phase": "al-W0", "status": "ok", "summary": "It landed the root.", "ts": 3.0}))
    return cfg


@pytest.fixture
def feed(tmp_path, monkeypatch) -> Feed:
    f = Feed(make_run(tmp_path, monkeypatch))
    f.refresh(force=True)
    return f


def _cards(board: dict) -> dict[str, dict]:
    return {c["id"]: c for col in board["columns"] for c in col["cards"]}


# -- columns ----------------------------------------------------------------
def test_every_ledger_phase_lands_in_its_column(feed):
    cards = _cards(feed.board)
    assert {p: cards[p]["col"] for p in EXPECTED} == EXPECTED
    assert [c["key"] for c in feed.board["columns"]] == [k for k, _, _ in board_mod.COLUMNS]


def test_every_column_is_populated(feed):
    counts = {col["key"]: len(col["cards"]) for col in feed.board["columns"]}
    assert all(counts[k] for k, _, _ in board_mod.COLUMNS), counts


def test_blocked_names_its_root_and_how_much_it_holds(feed):
    cards = _cards(feed.board)
    assert cards["al-W9"]["root"] == "al-W8"
    assert cards["al-W9"]["root_kind"] == "failed"
    assert cards["al-W9"]["root_blocks"] == 1
    assert cards["be-W2"]["root"] == "be-W1"
    assert cards["be-W2"]["root_kind"] == "excluded"


def test_cards_say_why_they_sit_where_they_do(feed):
    cards = _cards(feed.board)
    assert cards["al-W6"]["held"] is True and "conflict" in cards["al-W6"]["sub"]
    assert cards["al-W5"]["sub"] == "waiting to merge, #2"
    assert cards["be-W6"]["sub"] == "merged; the push is retried"
    assert cards["be-W3"]["q"] == "deploy now or tonight?"
    assert cards["be-W4"]["sub"] == "ticked in the ledger"
    assert cards["be-W8"]["sub"] == "ticked in the ledger"  # owner-run, and done
    assert cards["be-W5"]["sub"] == "skipped"
    assert cards["al-W0"]["sub"] == "built"
    assert cards["al-W2"]["slot"] == 0
    assert cards["al-W1"]["r"] == ["repo1", "repo2"]
    assert cards["al-W1"]["t"] == "ready now."


def test_non_phase_cards_join_the_board(feed):
    cards = _cards(feed.board)
    assert cards["op-1790000000"]["col"] == "operator"
    assert cards["op-1790000000"]["kind"] == "job"
    ask = cards["overseer:20260923T100000Z"]
    assert ask["col"] == "needs_you" and ask["q"] == "retry al-W8 or leave it?"


def test_header_counts_and_slots(feed):
    h = feed.board["header"]
    # waiting, parked, operator ask, Overseer ask, an owner-run row holding one up
    assert h["needs_you"] == 5
    assert h["slots"] == {"busy": 2, "total": 3}
    assert h["eta"]["remaining"] == sum(
        1 for p, col in EXPECTED.items() if col in board_mod.OPEN)
    assert set(h["usage"]) == {"five", "week"}
    assert h["big_picture"] == {"text": "off · no pass yet", "at": 0.0}  # the suite turns it off


def test_ledger_ordering_and_done_newest_first(feed):
    blocked = next(c for c in feed.board["columns"] if c["key"] == "blocked")["cards"]
    assert [c["id"] for c in blocked] == ["al-W9", "be-W2"]


def test_an_unchanged_rebuild_does_not_bump_the_version(feed):
    v = feed.version
    assert feed.refresh(force=True) is False
    assert feed.version == v


def test_a_state_change_bumps_the_version(feed):
    v = feed.version
    st = json.loads(feed.cfg.state_path.read_text())
    st["paused"] = True
    feed.cfg.state_path.write_text(json.dumps(st))
    assert feed.refresh() is True
    assert feed.version == v + 1 and feed.board["header"]["paused"] is True


def test_the_header_says_a_drain_in_words(feed):
    st = json.loads(feed.cfg.state_path.read_text())
    st["drain"] = {"since": 1.0, "then": "", "waiting": ["2 workers"]}
    feed.cfg.state_path.write_text(json.dumps(st))
    feed.refresh()
    assert feed.board["header"]["drain"] == "Draining: waiting for 2 workers, then stop"


def test_an_edited_exclude_list_moves_the_card(feed):
    toml = feed.cfg.project_dir / ".swarm.toml"
    toml.write_text('[tasks]\nledger = "docs/LEDGER.md"\nexclude = ["be-W1", "be-W8", "be-W9", "al-W1"]\n')
    feed.refresh()
    assert _cards(feed.board)["al-W1"]["col"] == "excluded"


# -- campaigns --------------------------------------------------------------
def test_campaign_headers_say_what_it_is_and_how_far(feed):
    camps = {c["name"]: c for c in feed.board["campaigns"]}
    assert camps["al"]["what"] == "Alpha — the first thing"
    assert camps["al"]["about"] == "Alpha exists to put a phase in every column."
    assert (camps["al"]["done"], camps["al"]["total"]) == (1, 10)
    assert camps["be"]["what"] == "beta — owner-run things"
    # be-W9 is excluded; be-W8 is excluded too, but ticked, so it counts as done;
    # be-W1 is the owner's to do, so it counts.
    assert (camps["be"]["done"], camps["be"]["total"]) == (3, 8)
    assert camps["al"]["active"] is True
    assert feed.board["campaigns"][0]["name"] == "al"


def test_rows_keep_full_text_heading_path_and_box():
    got, heads = rows.parse(LEDGER)
    assert list(got)[:2] == ["al-W0", "al-W1"]
    assert got["al-W0"].checked and not got["al-W1"].checked
    assert "continuation line" in got["al-W2"].text
    assert [h.text for h in got["be-W1"].heads] == [
        "Test ledger", "Alpha — the first thing (`al-W0`…`al-W9`)", "beta (owner-run things)"]
    assert "Intro prose" not in "".join(r.text for r in got.values())


def test_clean_lifts_the_adr_and_drops_the_id_range():
    assert campaigns.clean(
        "Polish — ADR-0002: Settings and sharing (`coral-W0`…`coral-W33`)",
        "coral") == ("Polish — Settings and sharing", "ADR-0002")
    assert campaigns.clean("ADR-0001 — a team owns its files (`ruby-W0`…`ruby-W10`)",
                           "ruby") == ("a team owns its files", "ADR-0001")
    assert campaigns.clean("`read-W*` — make the report readable (ADR-none)", "read")[0] == \
        "make the report readable"


def test_a_heading_naming_other_campaigns_is_not_borrowed():
    text = ("## ADR-0001 — a team owns its files (`ruby-W0`…`ruby-W2`)\n\n"
            "- [ ] `ruby-W0` · **r0**\n- [ ] `ruby-W1` · **r1**\n"
            "- [ ] `perf-F1` · **p1**\n- [ ] `perf-F2` · **p2** ADR-0003\n"
            "- [ ] `perf-F3` · **p3** ADR-0003\n")
    got, _ = rows.parse(text)
    metas = campaigns.describe(got, {"ADR-0003": "Load time budget"})
    assert metas["ruby"].what == "a team owns its files"
    assert metas["perf"].source == "adr" and metas["perf"].what == "Load time budget"


# -- detail -----------------------------------------------------------------
def test_phase_detail_carries_everything_about_one_card(feed):
    d = json.loads(feed.detail("al-W0"))
    assert d["row"]["checked"] is True and "the root that landed" in d["row"]["text"]
    assert d["row"]["dirs"] == ["repo1"]
    assert d["campaign"]["what"] == "Alpha — the first thing"
    assert {b["id"] for b in d["blocks"]} >= {"al-W1", "al-W8"}
    assert d["recap"]["summary"] == "It landed the root."
    assert d["completion"]["status"] == "ok"
    assert MARK in d["completion"]["note"] and "sk-ant" not in d["completion"]["note"]
    assert list(d["notes"]) == ["owner_decision", "decision", "assumption", "risk"]
    assert d["attempts"][0]["note"] == "first and only attempt"


def test_detail_of_a_building_and_a_waiting_phase(feed):
    b = json.loads(feed.detail("al-W2"))
    assert b["build"]["slot"] == 0 and b["build"]["branch"] == "swarm/al-W2"
    assert "continuation line" in b["row"]["text"]
    w = json.loads(feed.detail("be-W3"))
    assert w["question"] == "deploy now or tonight?"
    assert w["jobs"][0]["state"] == opqueue.WAITING
    n = json.loads(feed.detail("al-W9"))
    assert [x["id"] for x in n["needs"]] == ["al-W8"] and n["needs"][0]["col"] == "failed"
    x = json.loads(feed.detail("be-W9"))
    assert x["card"]["col"] == "excluded"


def test_nothing_waits_behind_a_row_whose_dependents_are_ticked(tmp_path, monkeypatch):
    cfg = make_run(tmp_path, monkeypatch)
    path = cfg.project_dir / cfg.ledger
    # be-W4 is ticked (landed); make it need the skipped be-W5.
    path.write_text(path.read_text().replace(
        "`be-W4` · needs:—", "`be-W4` · needs:`be-W5`"))
    f = Feed(cfg)
    f.refresh(force=True)
    d = json.loads(f.detail("be-W5"))
    assert [x["id"] for x in d["blocks"]] == ["be-W4"]
    assert d["blocks_total"] == 0


def test_detail_of_an_unknown_phase_is_none(feed):
    assert feed.detail("nope-W1") is None


def test_search_reaches_full_row_text(feed):
    assert feed.search("continuation line") == ["al-W2"]
    assert feed.search("") == []


# -- redaction --------------------------------------------------------------
@pytest.mark.parametrize("secret", [
    "sk-ant-api03-abcdefghijklmnopqrstu",
    "ghp_abcdefghijklmnopqrstuvwxyz0123",
    "github_pat_11ABCDEFG0123456789_abcdefghijk",
    "xoxb-123456789012-abcdefghijkl",
    "AKIAABCDEFGHIJKLMNOP",
    "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
])
def test_provider_shaped_tokens_are_redacted(secret):
    out = redact(f"used {secret} here")
    assert secret not in out and MARK in out and out.startswith("used ")


def test_named_values_and_headers_are_redacted_but_prose_is_not():
    assert redact("Authorization: Bearer abc.def.ghi-jkl") == f"Authorization: Bearer {MARK}"
    assert redact('API_KEY="abcdefghijklmnop1234"') == f'API_KEY="{MARK}"'
    assert redact("secret: c2VjcmV0c2VjcmV0c2VjcmV0") == f"secret: {MARK}"
    assert redact("token abc123def456ghi789jkl0") == f"token {MARK}"
    assert redact("https://me:hunter2@example.com/x") == f"https://me:{MARK}@example.com/x"
    for prose in ("the key decision was to keep the token budget small",
                  "rotate the secret ingredient_of_everything"):
        assert redact(prose) == prose


def test_deep_redacts_every_string_in_a_payload():
    got = deep({"a": ["ghp_abcdefghijklmnopqrstuvwxyz0123"], "b": {"c": "fine"}, "n": 3})
    assert got == {"a": [MARK], "b": {"c": "fine"}, "n": 3}


def test_the_served_board_is_redacted(feed):
    assert b"ghp_abcdefghijklmnopqrstuvwxyz0123" not in feed.body
    assert b"ghp_abcdefghijklmnopqrstuvwxyz0123" not in (feed.detail("be-W6") or b"")


def test_a_cycle_through_a_landed_row_is_not_an_issue(tmp_path, monkeypatch):
    cfg = make_run(tmp_path, monkeypatch)
    path = cfg.project_dir / cfg.ledger
    # al-W0 has landed; an ordering edge added later makes it "need" al-W1.
    path.write_text(path.read_text().replace(
        "`al-W0` · dir:`repo1` · needs:—", "`al-W0` · dir:`repo1` · needs:`al-W1`"))
    f = Feed(cfg)
    f.refresh(force=True)
    assert not [i for i in f.board["issues"] if "cycle" in i]
