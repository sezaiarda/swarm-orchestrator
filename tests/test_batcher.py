"""Batch selection (``batcher.py``): which ready rows ride with a seed.

The rule is the module docstring's. Every test builds a
throwaway project with a fixture ledger and real repo folders (so lanes parse),
and none spawns ``claude``: the model call is the ``_ask_model`` seam, and
``SWARM_TG_SINK`` keeps the unpatched seam from running at all.
"""

from __future__ import annotations

import json

import pytest

from swarm_orchestrator import batcher
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load

REPOS = ("app", "api", "web", "lib")

LEDGER = (
    "# Ledger\n\n"
    "- [x] `base-W0` · dir:`app` · needs:— · touches:`app/src/base.rs` · **base**\n"
    "- [ ] `kern-W1` · dir:`app` · needs:`base-W0` · touches:`app/src/kern/a.rs` · **kern one**\n"
    "- [ ] `kern-F2` · dir:`app` · needs:— · touches:`app/src/kern/b.rs` · **kern fix**\n"
    "- [ ] `kern-W3` · dir:`app` · needs:`kern-W1` · touches:`app/src/kern/c.rs` · **successor**\n"
    "- [ ] `kern-W4` · dir:`app` · touches:`app/src/kern/d.rs` · **four**\n"
    "- [ ] `kern-W5` · dir:`app` · touches:`app/src/kern/e.rs` · **five**\n"
    "- [ ] `kern-W6` · dir:`app` · touches:`app/src/kern/f.rs` · **six**\n"
    "- [ ] `ui-W1` · dir:`web` · touches:`web/src/a.ts` · **ui**\n"
    "- [ ] `son-W1` · dir:`app` · model:`sonnet` · touches:`app/src/kern/g.rs` · **sonnet row**\n"
    "- [ ] `son-W2` · dir:`app` · model:`sonnet` · touches:`app/src/kern/h.rs` · **sonnet two**\n"
    "- [ ] `wide-W1` · dir:`.` · touches:`./**` · **whole umbrella**\n"
    "- [ ] `cross-W1` · touches:`app/x.rs` `api/x.rs` `web/x.ts` · **cross-cutting**\n"
    "- [ ] `late-W1` · dir:`app` · needs:`ui-W9` · touches:`app/src/kern/z.rs` · **needs an absent row**\n"
    "- [ ] `ui-W9` · dir:`web` · needs:`cross-W1` · touches:`web/src/z.ts` · **behind a solo row**\n"
)

KERN = ["kern-W1", "kern-F2", "kern-W3", "kern-W4", "kern-W5", "kern-W6"]


@pytest.fixture(autouse=True)
def _batching_on(monkeypatch):
    monkeypatch.setenv("SWARM_BATCHING", "1")  # the suite runs with it off (conftest)


def _project(tmp_path, monkeypatch, ledger=LEDGER, toml="", lanes=True):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_WORKER_CMD", "true")  # the swarm's own model: none named
    monkeypatch.setenv("SWARM_WORKER_SETTINGS", "")
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)
    if lanes:
        monkeypatch.setenv("SWARM_LANES", "1")
    else:
        monkeypatch.delenv("SWARM_LANES", raising=False)
    for repo in REPOS:
        (tmp_path / repo / ".git").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(ledger, encoding="utf-8")
    (tmp_path / ".swarm.toml").write_text(toml, encoding="utf-8")
    cfg = load(project_dir=str(tmp_path))
    state_mod.init_state(cfg)
    return cfg


def _st(cfg):
    return state_mod.read(cfg)


class _Log:
    def __init__(self):
        self.lines: list[str] = []

    def line(self, message: str) -> None:
        self.lines.append(message)


# -- candidates -----------------------------------------------------------------
def test_candidates_are_same_model_open_rows_and_chain_successors(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch)
    got = batcher.candidates(cfg, _st(cfg), "kern-W1")
    # kern-W3 needs the seed: a chain successor. ui-W1 is eligible (compat is greedy's call).
    assert got == ["kern-F2", "kern-W3", "kern-W4", "kern-W5", "kern-W6", "ui-W1"]
    # model mismatch: neither sonnet row rides with an opus seed, and vice versa
    assert batcher.candidates(cfg, _st(cfg), "son-W1") == ["son-W2"]
    # solo rows and rows behind one are never candidates
    assert not {"wide-W1", "cross-W1", "ui-W9", "late-W1"} & set(got)


def test_busy_taken_and_batched_rows_are_excluded(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch)
    with state_mod.transaction(cfg) as st:
        st.slots[0].busy, st.slots[0].phase = True, "kern-F2"
        st.parked.append("kern-W4")
        st.waiting["kern-W5"] = 9e9
    st = _st(cfg)
    st.batches = {"x-W1": ["x-W1", "kern-W6"]}
    got = batcher.candidates(cfg, st, "kern-W1", taken={"ui-W1"})
    assert got == ["kern-W3"]


def test_a_lane_in_flight_blocks_overlapping_candidates(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch)
    with state_mod.transaction(cfg) as st:
        st.slots[0].busy, st.slots[0].phase = True, "base-X"
        st.lanes["base-X"] = ["app/src/kern/d.rs", "app/src/kern/e.rs"]
    got = batcher.candidates(cfg, _st(cfg), "kern-W1")
    assert "kern-W4" not in got and "kern-W5" not in got and "kern-W6" in got
    assert batcher.check(cfg, _st(cfg), "kern-W1", ["kern-W1", "kern-W4"]) == \
        "kern-W4 is not eligible"


# -- greedy -----------------------------------------------------------------------
def test_greedy_groups_one_family_up_to_max_rows_in_dependency_order(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch, toml="[batch]\nmodel = \"\"\n")
    st = _st(cfg)
    b = batcher.greedy(cfg, st, "kern-W1", batcher.candidates(cfg, st, "kern-W1"))
    # the successor joins first (affinity +4), then ledger order; it runs after its need
    assert b.rows == ["kern-W1", "kern-F2", "kern-W4", "kern-W5", "kern-W3"]
    assert b.source == "fallback" and "kern" in b.reason
    assert batcher.check(cfg, st, "kern-W1", b.rows) is None


def test_max_rows_is_capped_at_five(tmp_path, monkeypatch):
    cfg = _project(tmp_path / "a", monkeypatch, toml="[batch]\nmax_rows = 9\nmodel = \"\"\n")
    b = batcher.choose(cfg, _st(cfg), "kern-W1")
    assert len(b.rows) == 5
    cfg = _project(tmp_path / "b", monkeypatch, toml="[batch]\nmax_rows = 2\nmodel = \"\"\n")
    assert batcher.choose(cfg, _st(cfg), "kern-W1").rows == ["kern-W1", "kern-W3"]


def test_max_points_caps_a_batch(tmp_path, monkeypatch):
    # every row here is 3 + 0.5 per touch = 3.5 pts
    cfg = _project(tmp_path, monkeypatch, toml="[batch]\nmax_points = 8\nmodel = \"\"\n")
    b = batcher.choose(cfg, _st(cfg), "kern-W1")
    assert len(b.rows) == 2
    assert batcher.check(cfg, _st(cfg), "kern-W1", ["kern-W1", "kern-W3", "kern-F2"]) == \
        "10 pts > max_points 8"


MULTI = (
    "- [ ] `multi-W1` · touches:`app/a.rs` · **one**\n"
    "- [ ] `multi-W2` · touches:`api/a.rs` · **two**\n"
    "- [ ] `multi-W3` · touches:`web/a.ts` · **three**\n"
    "- [ ] `multi-W4` · touches:`lib/a.rs` · **four**\n"
    "- [ ] `other-W1` · touches:`app/b.rs` · **same repo, other family**\n"
    "- [ ] `other-W2` · touches:`lib/b.rs` · **nothing shared**\n"
    "- [ ] `doc-W1` · touches:`./docs/x.md` · **docs rider**\n"
)


def test_max_repos_caps_how_far_a_family_spreads(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch, MULTI, toml="[batch]\nmax_repos = 2\nmodel = \"\"\n")
    b = batcher.choose(cfg, _st(cfg), "multi-W1")
    repos = set().union(*(batcher.code_repos(batcher._view(cfg, _st(cfg), "multi-W1"), r)
                          for r in b.rows))
    assert len(repos) <= 2
    assert {"multi-W1", "multi-W2", "other-W1", "doc-W1"} == set(b.rows)
    assert "other-W2" not in b.rows  # another family in another repo never joins
    assert batcher.check(cfg, _st(cfg), "multi-W1", ["multi-W1", "multi-W2", "multi-W3"]) == \
        "3 repos > max_repos 2"


# -- solo rows ----------------------------------------------------------------------
def test_solo_classes_stay_single(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch, toml="[batch]\nmodel = \"\"\n")
    cfg.done_dir.mkdir(parents=True, exist_ok=True)
    (cfg.done_dir / "kern-W4.jsonl").write_text(
        json.dumps({"status": "fail", "note": "x", "fresh": True}) + "\n", encoding="utf-8")
    for seed, why in (("wide-W1", "whole umbrella"), ("cross-W1", "cross-cutting"),
                      ("kern-W4", "failed before")):
        b = batcher.choose(cfg, _st(cfg), seed)
        assert b.rows == [seed] and b.source == "single" and why in b.reason, seed
    assert "kern-W4" not in batcher.candidates(cfg, _st(cfg), "kern-W1")
    assert batcher.check(cfg, _st(cfg), "kern-W1", ["kern-W1", "kern-W4"]).startswith(
        "kern-W4 must run alone")


def test_batching_off_is_one_row(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_BATCHING")  # the file decides
    cfg = _project(tmp_path, monkeypatch, toml="[batch]\nenabled = false\n")
    assert batcher.choose(cfg, _st(cfg), "kern-W1") == batcher.Batch(
        ["kern-W1"], "batching off", "single")


# -- check ---------------------------------------------------------------------------
@pytest.mark.parametrize("rows, why", [
    (["kern-F2", "kern-W1"], "seed kern-W1 not first"),
    (["kern-W1", "nope-W1"], "unknown id nope-W1"),
    (["kern-W1", "kern-F2", "kern-F2"], "duplicate id"),
    (["kern-W1", "kern-F2", "kern-W4", "kern-W5", "kern-W6", "kern-W3"], "6 rows > max_rows 5"),
    (["kern-W1", "son-W1"], "mixed model"),
    (["kern-W1", "ui-W9"], "ui-W9 is not eligible"),
    ([], "empty batch"),
])
def test_check_rejects(tmp_path, monkeypatch, rows, why):
    cfg = _project(tmp_path, monkeypatch)
    assert batcher.check(cfg, _st(cfg), "kern-W1", rows) == why


def test_check_rejects_a_dependency_out_of_order(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch)
    assert batcher.check(cfg, _st(cfg), "kern-F2", ["kern-F2", "kern-W3", "kern-W1"]) == \
        "kern-W3 needs kern-W1 before it"
    assert batcher.check(cfg, _st(cfg), "kern-F2", ["kern-F2", "kern-W1", "kern-W3"]) is None


# -- choose: the model and its fallback ---------------------------------------------
def _answer(monkeypatch, result):
    seen = []

    def fake(cfg, text):
        seen.append(text)
        return result

    monkeypatch.setattr(batcher, "_ask_model", fake)
    return seen


def test_choose_takes_a_valid_model_answer(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch)
    seen = _answer(monkeypatch, (
        '```json\n{"batch":["kern-W1","kern-W3"],"reason":"chain"}\n```', "", 0.0012))
    log = _Log()
    b = batcher.choose(cfg, _st(cfg), "kern-W1", log=log)
    assert b == batcher.Batch(["kern-W1", "kern-W3"], "chain", "model")
    assert log.lines == ["BATCH-MODEL kern-W1 model=claude-sonnet-5-5 cost=$0.0012"]
    sent = json.loads(seen[0].split("INPUT:\n", 1)[1])
    assert sent["seed"] == "kern-W1" and sent["max_rows"] == 5
    ids = [r["id"] for r in sent["rows"]]
    assert ids[0] == "kern-W1" and "son-W1" not in ids and "wide-W1" not in ids
    w3 = next(r for r in sent["rows"] if r["id"] == "kern-W3")
    assert w3["needs"] == ["kern-W1"] and w3["repos"] == ["app"]
    assert w3["touch"] == ["app/src/kern/*"] and w3["fam"] == "kern"
    kw1 = next(r for r in sent["rows"] if r["id"] == "kern-W1")
    assert kw1["needs"] == []  # a landed need is not sent


@pytest.mark.parametrize("result, why", [
    ("I would batch kern-W1 with kern-W3", "answer is not JSON"),
    ('{"batch":["kern-W3","kern-W1"],"reason":"x"}', "seed kern-W1 not first"),
    ('{"batch":["kern-W1","made-up"],"reason":"x"}', "unknown id made-up"),
    ('{"batch":"kern-W1","reason":"x"}', "batch is not a list of ids"),
    (None, "model-error"),
    (None, "model-timeout"),
])
def test_choose_falls_back_to_greedy(tmp_path, monkeypatch, result, why):
    cfg = _project(tmp_path, monkeypatch)
    _answer(monkeypatch, (result, why if result is None else "", None))
    log = _Log()
    b = batcher.choose(cfg, _st(cfg), "kern-W1", log=log)
    assert b.source == "fallback"
    assert b.rows == ["kern-W1", "kern-F2", "kern-W4", "kern-W5", "kern-W3"]
    assert log.lines == [f"BATCH-FALLBACK kern-W1 {why}"]


def test_choose_never_spawns_claude_in_a_hermetic_run(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise AssertionError("spawned a process")

    monkeypatch.setattr(batcher.subprocess, "run", boom)
    log = _Log()
    b = batcher.choose(cfg, _st(cfg), "kern-W1", log=log)
    assert b.source == "fallback" and len(b.rows) == 5 and log.lines == []


def test_choose_with_no_model_is_the_rule_and_no_call(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch, toml="[batch]\nmodel = \"\"\n")
    _answer(monkeypatch, AssertionError)  # would fail the unpack if called
    assert batcher.choose(cfg, _st(cfg), "kern-W1").source == "fallback"


# -- still_free ------------------------------------------------------------------------
def test_still_free_drops_taken_rows_and_their_in_batch_dependents(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch)
    rows = ["kern-F2", "kern-W1", "kern-W3", "kern-W4"]
    assert batcher.still_free(cfg, _st(cfg), rows) == rows
    assert batcher.still_free(cfg, _st(cfg), rows, taken={"kern-W1"}) == ["kern-F2", "kern-W4"]
    with state_mod.transaction(cfg) as st:
        st.done["kern-W4"] = "ok"
        st.slots[0].busy, st.slots[0].phase = True, "kern-F2"  # the seed itself is kept
    assert batcher.still_free(cfg, _st(cfg), rows) == ["kern-F2", "kern-W1", "kern-W3"]


# -- plan and `swarm batch` -------------------------------------------------------------
def test_plan_forms_batches_in_launch_order_without_reusing_a_row(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch, toml="[lanes]\nper_repo = 5\n")
    got = [b.rows for b in batcher.plan(cfg, _st(cfg))]
    # kern-W6 has no related row left, so it pairs with the next row that fits
    # rather than run alone ([batch] min_rows = 2).
    assert got == [["kern-W1", "kern-F2", "kern-W4", "kern-W5", "kern-W3"], ["kern-W6", "ui-W1"],
                   ["son-W1", "son-W2"], ["wide-W1"], ["cross-W1"]]


def test_plan_counts_a_batch_once_per_repo_and_respects_lanes(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch)  # per_repo = 2
    with state_mod.transaction(cfg) as st:
        st.slots[0].busy, st.slots[0].phase = True, "base-X"
        st.lanes["base-X"] = ["web/src/**"]
    got = [b.rows for b in batcher.plan(cfg, _st(cfg))]
    # app: the kern batch and kern-W6 hold it twice; son-W1 and cross-W1 wait.
    # web: base-X holds web/src, so ui-W1 waits.
    assert got == [["kern-W1", "kern-F2", "kern-W4", "kern-W5", "kern-W3"], ["kern-W6"],
                   ["wide-W1"]]


def test_swarm_batch_json(tmp_path, monkeypatch, capsys):
    from swarm_orchestrator import cli

    cfg = _project(tmp_path, monkeypatch, lanes=False)
    assert cli.main(["--project-dir", str(cfg.project_dir), "batch", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0] == {"rows": ["kern-W1", "kern-F2", "kern-W4", "kern-W5", "kern-W3"],
                      "reason": out[0]["reason"], "source": "fallback"}
    assert {"rows": ["wide-W1"], "reason": "runs alone: touches the whole umbrella",
            "source": "single"} in out
    assert cli.main(["--project-dir", str(cfg.project_dir), "batch"]) == 0
    assert capsys.readouterr().out.splitlines()[0].startswith(
        "kern-W1 kern-F2 kern-W4 kern-W5 kern-W3  (")


def test_an_open_batch_holds_one_union_lane(tmp_path, monkeypatch):
    cfg = _project(tmp_path, monkeypatch)  # per_repo = 2
    with state_mod.transaction(cfg) as st:
        st.slots[0].busy, st.slots[0].phase = True, "kern-W4"
    st = _st(cfg)
    st.batches = {"kern-W4": ["kern-W4", "kern-W5"]}
    # the open batch counts once in app, so a second app batch still launches
    assert [b.rows for b in batcher.plan(cfg, st)] == [
        ["kern-W1", "kern-F2", "kern-W6", "kern-W3"], ["ui-W1"], ["wide-W1"]]
    # and its rows' lanes are held: a row overlapping kern-W5 cannot ride
    assert batcher.check(cfg, st, "kern-W1", ["kern-W1", "kern-W5"]) == "kern-W5 is not eligible"


# -- at least two rows, and never a lane-starved slot -------------------------------
def test_a_seed_with_no_related_row_pairs_with_the_best_fitting_one(tmp_path, monkeypatch):
    ledger = ("# Ledger\n\n"
              "- [ ] `a-W1` · dir:`app` · touches:`app/src/a.rs` · **alone in its family**\n"
              "- [ ] `b-W1` · dir:`web` · touches:`web/src/b.ts` · **another family**\n")
    cfg = _project(tmp_path, monkeypatch, ledger=ledger)
    got = batcher.greedy(cfg, _st(cfg), "a-W1", batcher.candidates(cfg, _st(cfg), "a-W1"))
    assert got.rows == ["a-W1", "b-W1"]
    # And a model that answers with the seed alone while a partner fits is not taken.
    assert "min_rows" in batcher.check(cfg, _st(cfg), "a-W1", ["a-W1"])
    # With min_rows = 1 the old rule stands: unrelated rows do not ride.
    cfg.batch_min_rows = 1
    assert batcher.greedy(cfg, _st(cfg), "a-W1", ["b-W1"]).rows == ["a-W1"]


def test_a_row_whose_lane_would_starve_the_next_slot_stays_out(tmp_path, monkeypatch):
    ledger = ("# Ledger\n\n"
              "- [ ] `k-W1` · dir:`app` · touches:`app/src/a.rs` · **one**\n"
              "- [ ] `k-W2` · dir:`app` · touches:`app/**` · **the whole repo**\n"
              "- [ ] `z-W1` · dir:`app` · touches:`app/src/z.rs` · **another family**\n"
              "- [ ] `s-W1` · dir:`app` · model:`sonnet` · touches:`app/src/s.rs` · **for a"
              " slot of its own**\n")
    cfg = _project(tmp_path, monkeypatch, ledger=ledger, toml="[lanes]\nper_repo = 5\n")
    st = _st(cfg)
    # With k-W2's `app/**` in the batch, s-W1 (another model, so never a rider)
    # could launch nowhere: k-W2 waits for its own turn.
    assert batcher.greedy(cfg, st, "k-W1", batcher.candidates(cfg, st, "k-W1")).rows == [
        "k-W1", "z-W1"]
    assert "lane" in batcher.check(cfg, st, "k-W1", ["k-W1", "k-W2"])
