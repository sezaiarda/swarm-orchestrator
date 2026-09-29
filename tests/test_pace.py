"""The pace, the counts and the chart come from the ledger, not from one machine's log.

A swarm that has run on another machine and pushed many ticks from there:
this machine's log misses them, so the chart would end early, `swarm status` would
count more done than the ticks show, and the ETA would be a median phase length
timed on a stale run at a different worker count. These pin the replacements: the
git history of the ledger, one count, and a pace that knows its own basis.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm_orchestrator import cli, ledger, pace
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.tui import campaign, data

H = 3600.0
NOW = 1_790_000_000.0


# -- reading the ledger's history --------------------------------------------
LOG = """\
@@@ 100
diff --git a/docs/PHASE-LEDGER.md b/docs/PHASE-LEDGER.md
+- [ ] `a-W1` · needs:
+- [ ] `a-W2` · needs:`a-W1`
diff --git a/.swarm.toml b/.swarm.toml
+max_workers = 2                  # two workers
@@@ 200
diff --git a/docs/PHASE-LEDGER.md b/docs/PHASE-LEDGER.md
-- [ ] `a-W1` · needs:
+- [x] `a-W1` · needs:
@@@ 300
diff --git a/docs/PHASE-LEDGER.md b/docs/PHASE-LEDGER.md
-- [x] `a-W1` · needs:
+- [x] `a-W1` · needs: · closed, with a note
+- [x] `b-W1` · needs:
+- [x] `b-W2` · needs:
+- [x] `b-W3` · needs:
+- [x] `b-W4` · needs:
@@@ 400
diff --git a/docs/PHASE-LEDGER.md b/docs/PHASE-LEDGER.md
+- [ ] `b-W4` · needs: · reopened
diff --git a/.swarm.toml b/.swarm.toml
-max_workers = 2
+max_workers = 1
"""


def test_a_row_counts_from_the_commit_that_ticked_it():
    ticks, workers = {}, []
    pace.fold(LOG, ticks, workers, "docs/PHASE-LEDGER.md", ".swarm.toml")
    assert ticks["a-W1"] == (200.0, 1)  # a later edit of the ticked row keeps its time
    assert ticks["b-W1"] == (300.0, 4)  # four rows in one commit: bookkeeping
    assert "b-W4" not in ticks  # reopened
    assert "a-W2" not in ticks
    assert workers == [(100.0, 2), (400.0, 1)]
    assert pace.workers_at(workers, 50) is None and pace.workers_at(workers, 250) == 2


def test_a_row_is_filed_by_the_commit_it_first_appears_in():
    """The ETA engine measures growth from filings: an edit, a tick or a reopen of
    a row already filed is not a new row."""
    ticks, workers, added = {}, [], {}
    pace.fold(LOG, ticks, workers, "docs/PHASE-LEDGER.md", ".swarm.toml", added)
    assert added["a-W1"][0] == 100.0 and added["a-W2"][0] == 100.0
    assert added["b-W1"] == (300.0, 4)  # b-W1..b-W4 were filed in one commit
    assert set(added) == {"a-W1", "a-W2", "b-W1", "b-W2", "b-W3", "b-W4"}


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "master", str(project)], check=True)
    for args in (("user.email", "swarm@test"), ("user.name", "swarm"),
                 ("commit.gpgsign", "false")):
        _git(project, "config", *args)
    (project / ".swarm.toml").write_text("[swarm]\nmax_workers = 2\n")
    _commit(project, "- [ ] `a-W1`\n- [ ] `a-W2`\n", "seed")
    return SimpleNamespace(project_dir=project, ledger="docs/PHASE-LEDGER.md",
                           state_dir=tmp_path / "state")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(project: Path, ledger: str, msg: str) -> None:
    (project / "docs" / "PHASE-LEDGER.md").write_text(ledger)
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", msg)


def test_history_is_cached_by_head_and_extended_when_it_moves(repo, monkeypatch):
    calls = []
    real = pace._log
    monkeypatch.setattr(pace, "_log", lambda *a: (calls.append(a[1]), real(*a))[1])

    first = pace.load(repo)
    assert first.ticks == {} and first.workers and first.workers[0][1] == 2
    assert pace.load(repo) == first and len(calls) == 1  # HEAD unchanged: no git log

    _commit(repo.project_dir, "- [x] `a-W1`\n- [ ] `a-W2`\n", "a-W1 done")
    got = pace.load(repo)
    assert set(got.ticks) == {"a-W1"}
    assert calls[-1] == f"{first.head}..{got.head}"  # only the new commit was read

    # History rewritten under the cache: its head is no ancestor, so read it all again.
    _git(repo.project_dir, "reset", "-q", "--hard", first.head)
    _commit(repo.project_dir, "- [ ] `a-W1`\n- [x] `a-W2`\n", "a-W2 instead")
    got = pace.load(repo)
    assert set(got.ticks) == {"a-W2"} and calls[-1] == got.head


def test_no_repo_is_no_history_not_an_error(tmp_path):
    cfg = SimpleNamespace(project_dir=tmp_path, ledger="docs/PHASE-LEDGER.md", state_dir=None)
    assert pace.load(cfg) == pace.History()


# -- the pace ------------------------------------------------------------------
def spaced(n, every=0.5 * H, end=NOW):
    return {f"p{i}": end - (n - 1 - i) * every for i in range(n)}


def test_the_pace_is_the_recent_finishes_only():
    got = pace.measure(spaced(30), set(), [], [(0.0, 2)], NOW)
    assert got.phases == pace.WINDOW and got.per_hour == pytest.approx(2.0)
    assert got.workers == 2
    # A week-old burst says nothing about today.
    stale = {f"old{i}": NOW - 8 * 86400 + i for i in range(50)}
    assert pace.measure(stale | spaced(3), set(), [], [], NOW) is None


def test_too_few_recent_finishes_is_no_pace():
    assert pace.measure(spaced(pace.MIN_PHASES - 1), set(), [], [], NOW) is None


def test_a_bookkeeping_commit_is_not_throughput():
    burst = {f"seed{i}": NOW - 0.1 * H for i in range(35)}
    got = pace.measure(spaced(6) | burst, set(burst), [], [], NOW)
    assert got.phases == 6 and got.per_hour == pytest.approx(2.0)


def test_a_hold_is_not_working_time_unless_something_finished_in_it():
    times = spaced(6, every=H)
    t = sorted(times.values())
    hold = [(t[2] + 0.1 * H, t[2] + 0.6 * H)]  # half an hour held between two finishes
    assert pace.measure(times, set(), hold, [], NOW).hours == pytest.approx(4.5)
    # A two-day "hold" on this machine while another one ticked rows was not a hold.
    elsewhere = [(t[0] - H, t[-1] + H)]
    assert pace.measure(times, set(), elsewhere, [], NOW).hours == pytest.approx(5.0)


def test_the_worker_count_is_the_config_s_at_the_time():
    times = spaced(5, every=H)
    t = sorted(times.values())
    got = pace.measure(times, set(), [], [(0.0, 2), (t[2], 1)], NOW)
    assert got.workers == pytest.approx(1.5)
    assert pace.measure(times, set(), [], [(t[2], 1)], NOW).workers is None  # half unknown


def test_the_outlook_is_a_range_between_a_shared_bottleneck_and_independent_workers():
    at_two = pace.Pace(phases=20, hours=10, per_hour=2.0, workers=2.0)
    assert pace.outlook(at_two, 4, 1) == (2 * H, 4 * H)
    assert pace.outlook(at_two, 4, 4) == (1 * H, 2 * H)
    assert pace.outlook(at_two, 4, 2) == (2 * H, 2 * H)
    unknown = pace.Pace(phases=20, hours=10, per_hour=2.0, workers=None)
    assert pace.outlook(unknown, 4, 1) == (2 * H, 2 * H)


def test_the_idle_spans_come_from_the_log_s_down_paused_and_held_lines():
    events = data.parse_events("\n".join([
        "2026-09-28 10:00:00.000 1.0 SUPERVISOR-STOP",
        "2026-09-28 11:00:00.000 2.0 SUPERVISOR-START pid=1",
        "2026-09-28 11:00:00.100 2.0 WATCHDOG idle=0s busy=[] paused=True",
        "2026-09-28 11:30:00.000 3.0 WATCHDOG idle=0s busy=[] paused=False",
        "2026-09-28 12:00:00.000 4.0 USAGE-HOLD week 62% limit=60%",
        "2026-09-28 12:10:00.000 5.0 SUPERVISOR-STOP",
        "2026-09-28 12:20:00.000 6.0 SUPERVISOR-START pid=2",
        "2026-09-28 12:20:00.100 6.0 USAGE-CHECK five_hour=3% week=62% held=week",
        "2026-09-28 13:00:00.000 7.0 USAGE-CHECK five_hour=3% week=58% held=-",
    ]))
    ts = [e.ts for e in events]
    # Down, then paused, then held — through a restart that only re-logs the hold.
    assert data.idle_spans(events) == [(ts[0], ts[1]), (ts[2], ts[3]), (ts[4], ts[8])]
    assert data.idle_spans(events[:5], now=ts[4] + 60)[-1] == (ts[4], ts[4] + 60)


# -- one definition of done ------------------------------------------------------
def test_finish_times_prefer_the_ledger_and_fall_back_to_this_machine():
    landed = {"t-1": "ledger", "t-2": "ok", "h-1": "operator", "s-1": "skip", "f-1": "fail"}
    ticked = {"t-1", "t-2", "s-9"}
    history = pace.History(ticks={"t-1": (100.0, 1), "t-2": (200.0, 9), "h-1": (1.0, 1)})
    runs = [data.PhaseRun("t-2", "ok", 0.0, 150.0), data.PhaseRun("h-1", "operator", 0.0, 300.0),
            data.PhaseRun("h-1", "operator", 0.0, 250.0), data.PhaseRun("f-1", "fail", 0.0, 5.0)]
    got, bulk = data.finish_times(landed, ticked, history, runs)
    # t-2: the ledger's time wins over this machine's; h-1: held done, never ticked,
    # so its latest local run (its old tick was undone); a skip nobody ticked and a
    # failure never finished.
    assert got == {"t-1": 100.0, "t-2": 200.0, "h-1": 300.0}
    assert bulk == {"t-2"}


def test_counts_include_ticks_this_machine_never_saw_and_held_rows_and_leave_exclusions_out():
    # The mixed shape: 25 perf rows ticked (only some built here), 5 held done with
    # the ledger still `[ ]` (3 of them excluded standing targets), 4 left.
    graph = {f"perf-F{i}": set() for i in range(1, 35)}
    ticked = {f"perf-F{i}" for i in range(1, 26)}
    local = {f"perf-F{i}": "skip" for i in range(1, 14)}  # the rest were built elsewhere
    local |= {"perf-F26": "ok", "perf-F27": "ok", "perf-F28": "operator",
              "perf-F29": "operator", "perf-F30": "operator"}
    excluded = {"perf-F26", "perf-F27", "perf-F28", "perf-F34"}
    landed = ledger.with_ticked(local, ticked)
    (perf,) = campaign.summarise(graph, landed, busy={"perf-F31"}, excluded=excluded,
                                 ticked=ticked)
    # perf-F34 is excluded and not done: out of the count. The three excluded rows
    # the swarm holds done are done — the scheduler releases their dependents too.
    assert (perf.built, perf.live_total, perf.held) == (30, 33, 5)
    assert (perf.running, perf.excluded, len(perf.ready)) == (["perf-F31"], 1, 2)
    whole = campaign.overall([perf])
    assert (whole.built, whole.live_total, whole.held) == (30, 33, 5)
    # The overview's done no longer counts a failure as done.
    prog = data.phase_progress(graph, landed | {"perf-F32": "fail"}, {"perf-F31"}, excluded)
    assert (prog.done, prog.failed) == (30, 1)


def test_the_headline_names_the_campaign_a_worker_is_on_over_a_bigger_one_that_waits():
    graph = {f"big-W{i}": set() for i in range(1, 30)} | {"small-W1": set(), "small-W2": set()}
    camps = campaign.summarise(graph, {}, busy={"small-W1"})
    assert campaign.active(camps).name == "small"
    assert campaign.active(campaign.summarise(graph, {})).name == "big"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / ".swarm.toml").write_text('[swarm]\ndriver = "bare"\n[web]\nenabled = false\n'
                                          '[tasks]\nexclude = ["a-P5"]\n', encoding="utf-8")
    (project / "docs" / "PHASE-LEDGER.md").write_text(
        "- [x] `a-P0`\n- [x] `a-P1`\n- [x] `a-P2`\n- [ ] `a-P3`\n- [ ] `a-P4` · needs:`a-P3`\n"
        "- [ ] `a-P5`\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.delenv("SWARM_DRIVER", raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    with state_mod.transaction(c) as st:
        st.done = {"a-P0": "ok", "a-P3": "operator"}  # a-P1, a-P2 were built elsewhere
    return c


def test_swarm_status_counts_the_ledger_the_way_the_dashboard_does(cfg, capsys):
    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert ("phases: 4 of 5 done (1 of them done by the swarm, still open in the ledger)"
            " · 1 ready · 1 yours to do, not counted") in out
    assert "records here: 2 (ok=1 operator=1)" in out
    assert cli.cmd_status(cfg, as_json=True) == 0
    phases = json.loads(capsys.readouterr().out)["phases"]
    assert (phases["done"], phases["total"], phases["held"]) == (4, 5, 1)
