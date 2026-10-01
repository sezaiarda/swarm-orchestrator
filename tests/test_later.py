"""A phase that finishes ``later``: its work is kept for its date, and until
then it waits for a date. It is not a failure.

On real git, through the supervisor's own ``done`` handler: the commits (and
the uncommitted edits) of a ``later`` finish are in the relaunched worker's
tree, on top of that day's main; a ``blocked`` finish is set aside as before.
Then the words: ``swarm status``, ``swarm why``, the Overseer's digest and its
triggers all say "waits for a date", with the date, also while the ledger has
not taken the report yet.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import cli, doctor, gitq, ledgerw, ovdigest
from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import master as master_mod
from swarm_orchestrator import overseer as ov
from swarm_orchestrator import procs
from swarm_orchestrator import restart as restart_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import why as why_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.eta import engine as eta_engine
from swarm_orchestrator.eta import plan as eta_plan
from swarm_orchestrator.supervisor import Supervisor
from swarm_orchestrator.tui import campaign, drawer
from swarm_orchestrator.tui.dash import Dash
from swarm_orchestrator.web.feed import Feed

LEDGER = """# Ledger

## Campaign A
- [x] `a-W1` · dir:`alpha` · needs:— · **the first wave**
- [ ] `a-W2` · dir:`alpha` · needs:`a-W1` · **the second wave**
- [ ] `a-W3` · dir:`alpha` · needs:`a-W2` · **the third wave**
"""

FAR = "2999-01-01"
KEPT = "refs/swarm-later"  # gitq.LATER
ATTIC = "refs/swarm-attic"
LEDGER_FILE = "docs/PHASE-LEDGER.md"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


def _has(repo: Path, ref: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
                          capture_output=True).returncode == 0


def _refs(repo: Path, prefix: str) -> list[str]:
    return _git(repo, "for-each-ref", "--format=%(refname)", prefix).split()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", "-b", "master", str(project)], check=True)
    for k, v in (("user.email", "swarm@test"), ("user.name", "swarm"), ("commit.gpgsign", "false")):
        _git(project, "config", k, v)
    (project / "docs").mkdir()
    (project / LEDGER_FILE).write_text(LEDGER)
    (project / "shared.txt").write_text("one\n")
    (project / ".swarm.toml").write_text(
        f'[swarm]\ndriver = "bare"\n[tasks]\nledger = "{LEDGER_FILE}"\n')
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_SLUG", "later")
    monkeypatch.setenv("SWARM_OVERSEER", "1")
    for leak in ("SWARM_SESSION_ID", "SWARM_PHASE", "SWARM_DRIVER", "SWARM_OVERSEER_PASS"):
        monkeypatch.delenv(leak, raising=False)
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    sup = Supervisor(cfg)
    monkeypatch.setattr(sup, "_fill_slots", lambda *a, **k: [])
    yield cfg, project, sup
    sup.log.close()


def _build(cfg, sup, phase: str = "a-W2", text: str = "built\n") -> Path:
    """A worker's attempt: a commit on its branch and an edit it never committed."""
    wt = gitq.worktree_add(cfg, phase, sup.log)
    (wt / "code.txt").write_text(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"{phase} work")
    (wt / "notes.txt").write_text("measured so far\n")
    with state_mod.transaction(cfg) as st:
        st.claim_slot(phase)
    return wt


def _finish(cfg, sup, phase: str, outcome: str, after: str = "") -> None:
    """``swarm done <phase> <outcome>`` as the supervisor receives it."""
    (cfg.done_dir / f"{phase}.fail").write_text(f"{phase} fail waiting\n")
    ledgerw.queue(cfg, phase, {"kind": "outcome", "outcome": outcome,
                               "note": "needs a week of data", "after": after})
    sup._on_done(phase, "fail")


def _main_moves(project: Path, name: str = "other.txt", text: str = "someone else\n") -> None:
    (project / name).write_text(text)
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", f"main: {name}")


# -- the work -----------------------------------------------------------------
def test_a_later_finish_is_relaunched_with_its_commit_on_that_days_main(ws):
    cfg, project, sup = ws
    _build(cfg, sup)
    work = _git(cfg.wt_dir / "a-W2", "rev-parse", "HEAD").strip()
    _finish(cfg, sup, "a-W2", "later", FAR)

    # Nothing landed, the mirror is gone, and the work is kept for the date.
    assert not (project / "code.txt").exists() and not (cfg.wt_dir / "a-W2").exists()
    assert not _has(project, "refs/heads/swarm/a-W2")
    assert _refs(project, KEPT) == [f"{KEPT}/a-W2"]
    assert _refs(project, ATTIC) == [] and (gitq.LATER, gitq.ATTIC) == (KEPT, ATTIC)
    log = cfg.supervisor_log.read_text()
    assert f"LATER-KEPT a-W2 project {KEPT}/a-W2" in log

    _main_moves(project)
    wt = gitq.worktree_add(cfg, "a-W2", sup.log)  # its date has come

    assert (wt / "code.txt").read_text() == "built\n"
    assert (wt / "notes.txt").read_text() == "measured so far\n"  # the uncommitted edit too
    assert (wt / "other.txt").read_text() == "someone else\n"  # on today's main
    assert subprocess.run(["git", "-C", str(wt), "merge-base", "--is-ancestor", work, "HEAD"]
                          ).returncode == 0
    assert "a-W2 work" in _git(wt, "log", "--format=%s", "master..HEAD")
    assert _git(wt, "status", "--porcelain").strip() == ""
    assert _refs(project, KEPT) == []  # it is on the branch now
    assert "LATER-RESTORED a-W2 project" in cfg.supervisor_log.read_text()


def test_work_that_no_longer_merges_comes_back_as_it_was(ws):
    cfg, project, sup = ws
    wt = gitq.worktree_add(cfg, "a-W2", sup.log)
    (wt / "shared.txt").write_text("the phase's line\n")
    _git(wt, "commit", "-qam", "a-W2 work")
    with state_mod.transaction(cfg) as st:
        st.claim_slot("a-W2")
    _finish(cfg, sup, "a-W2", "later", FAR)
    _main_moves(project, "shared.txt", "main's line\n")

    wt = gitq.worktree_add(cfg, "a-W2", sup.log)

    # The worker starts on its own work; the landing meets the conflict as it
    # would for any branch that fell behind.
    assert (wt / "shared.txt").read_text() == "the phase's line\n"
    assert "a-W2 work" in _git(wt, "log", "--format=%s", "master..HEAD")
    assert not (Path(_git(wt, "rev-parse", "--absolute-git-dir").strip()) / "MERGE_HEAD").exists()
    assert _git(wt, "status", "--porcelain").strip() == ""
    assert _refs(project, KEPT) == []
    assert "LATER-RESTORED a-W2 project as it was" in cfg.supervisor_log.read_text()


def test_a_blocked_finish_is_set_aside_as_before(ws):
    cfg, project, sup = ws
    _build(cfg, sup)
    _finish(cfg, sup, "a-W2", "blocked")

    assert _refs(project, KEPT) == []
    assert len(_refs(project, ATTIC)) == 1
    assert state_mod.read(cfg).done["a-W2"] == "fail"
    wt = gitq.worktree_add(cfg, "a-W2", sup.log)
    assert not (wt / "code.txt").exists()  # a retry starts from main


def test_a_later_without_a_date_is_set_aside_like_a_blocked_one(ws):
    cfg, project, sup = ws
    _build(cfg, sup)
    _finish(cfg, sup, "a-W2", "later")

    assert _refs(project, KEPT) == [] and len(_refs(project, ATTIC)) == 1
    assert state_mod.read(cfg).done["a-W2"] == "fail"


def test_swarm_up_keeps_a_later_finish_the_supervisor_never_saw(ws):
    """The worker reported while no supervisor ran: its branch is still there."""
    cfg, project, sup = ws
    _build(cfg, sup)
    (cfg.done_dir / "a-W2.fail").write_text("a-W2 fail waiting\n")
    ledgerw.queue(cfg, "a-W2", {"kind": "outcome", "outcome": "later", "note": "n", "after": FAR})

    gitq.reconcile(cfg, {"a-W2": "fail"}, sup.log, later=set(ledgerw.dated(cfg)))

    assert _refs(project, KEPT) == [f"{KEPT}/a-W2"]
    assert _refs(project, ATTIC) == [] and not _has(project, "refs/heads/swarm/a-W2")


def test_kept_work_of_a_row_closed_some_other_way_goes_to_the_attic(ws):
    cfg, project, sup = ws
    _build(cfg, sup)
    _finish(cfg, sup, "a-W2", "later", FAR)

    gitq.reconcile(cfg, {"a-W2": "ok"}, sup.log)  # closed by hand since

    assert _refs(project, KEPT) == [] and len(_refs(project, ATTIC)) == 1


# -- the words ----------------------------------------------------------------
def _waiting_unreported(cfg) -> None:
    """A ``later`` the supervisor recorded while the ledger could not take it."""
    (cfg.done_dir / "a-W2.fail").write_text("a-W2 fail waiting for a week of data\n")
    ledgerw.queue(cfg, "a-W2", {"kind": "outcome", "outcome": "later", "note": "n", "after": FAR})
    with state_mod.transaction(cfg) as st:
        st.mark_done("a-W2", "fail")


def test_once_the_ledger_has_the_date_the_failure_record_is_gone(ws):
    cfg, project, sup = ws
    _build(cfg, sup)
    _finish(cfg, sup, "a-W2", "later", FAR)

    assert f"status: later, after {FAR}" in (project / LEDGER_FILE).read_text()
    st = state_mod.read(cfg)
    assert "a-W2" not in st.done and not (cfg.done_dir / "a-W2.fail").exists()
    ctx = master_mod.build_context(cfg, st)
    assert ctx["deferred"] == {"a-W2": FAR} and "a-W2" not in ctx["ready"]
    assert f"LATER-WAITS a-W2 until {FAR}" in cfg.supervisor_log.read_text()


def test_status_counts_a_later_row_as_waiting_for_its_date(ws, capsys):
    cfg, project, sup = ws
    _build(cfg, sup)
    _finish(cfg, sup, "a-W2", "later", FAR)
    capsys.readouterr()

    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert "1 waiting for a date" in out and f"waiting for a date: a-W2 ({FAR})" in out
    assert "failed" not in out


@pytest.mark.parametrize("reported", [True, False])
def test_why_says_a_later_row_waits_for_its_date(ws, reported):
    cfg, project, sup = ws
    if reported:
        _build(cfg, sup)
        _finish(cfg, sup, "a-W2", "later", FAR)
    else:
        _waiting_unreported(cfg)

    exp = why_mod.explain(cfg, "a-W2")
    text = why_mod.render(exp)
    assert exp.reason == why_mod.DEFERRED and f"waits until {FAR}" in text
    assert "retry" not in text and "set aside" not in text
    behind = why_mod.explain(cfg, "a-W3")
    assert behind.root_cause == "a-W2" and behind.root_detail.startswith(f"waits until {FAR}")


@pytest.mark.parametrize("reported", [True, False])
def test_the_digest_lists_a_later_row_under_its_date_not_under_failures(ws, reported):
    cfg, project, sup = ws
    if reported:
        _build(cfg, sup)
        _finish(cfg, sup, "a-W2", "later", FAR)
    else:
        _waiting_unreported(cfg)

    data = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0)
    md = ovdigest.render(data)
    assert data["failures"] == [] and data["dated"] == [{"phase": "a-W2", "until": FAR}]
    assert "## Failed phases (0)" in md
    assert "## Waiting for a date (1)" in md and f"- a-W2: until {FAR}" in md


def test_status_and_the_launcher_hold_a_later_the_ledger_has_not_taken_yet(ws, capsys):
    cfg, project, sup = ws
    _waiting_unreported(cfg)

    assert cli.cmd_status(cfg) == 0
    out = capsys.readouterr().out
    assert f"waiting for a date: a-W2 ({FAR})" in out and "failed" not in out
    # Even with its record cleared by a `swarm retry`, it is not offered early.
    with state_mod.transaction(cfg) as st:
        st.done.pop("a-W2")
    ctx = master_mod.build_context(cfg, state_mod.read(cfg))
    assert "a-W2" not in ctx["ready"] and ctx["deferred"] == {"a-W2": FAR}


def test_a_later_finish_wakes_no_overseer_pass_and_a_fail_still_does(ws):
    cfg, project, sup = ws
    sup.overseer.observe(state_mod.read(cfg))  # baseline
    _waiting_unreported(cfg)
    sup.overseer.observe(state_mod.read(cfg))
    assert [r.key for r in sup.overseer.pending] == []

    with state_mod.transaction(cfg) as st:
        st.mark_done("a-W3", "fail")
    sup.overseer.observe(state_mod.read(cfg))
    assert [r.key for r in sup.overseer.pending] == [f"{ov.FAIL}:a-W3"]


# -- the dashboards and doctor read the same dates and the same done map ------
def _reported_or_queued(cfg, sup, reported: bool) -> None:
    if reported:
        _build(cfg, sup)
        _finish(cfg, sup, "a-W2", "later", FAR)
    else:
        _waiting_unreported(cfg)


def _tui_counts(dash: Dash) -> campaign.Campaign:
    """The counts the TUI's headline is drawn from (``tui.home.headline``)."""
    snap = dash.snapshot
    busy = {s.phase for s in snap.slots if s.busy and s.phase}
    return campaign.overall(campaign.summarise(
        dash.graph, snap.landed, busy, set(dash.cfg.exclude or []), dash.ticked, dash.deferred))


@pytest.mark.parametrize("reported", [True, False])
def test_the_tui_counts_a_later_row_as_waiting_for_its_date(ws, reported):
    cfg, project, sup = ws
    _reported_or_queued(cfg, sup, reported)
    dash = Dash(cfg)
    dash.poll()

    assert dash.deferred == {"a-W2": FAR}
    total = _tui_counts(dash)
    assert (total.dated, total.failed, total.ready, total.blocked) == (1, 0, [], 1)
    progress = dash.snapshot.progress
    assert (progress.failed, progress.ready, progress.blocked) == (0, 0, 2)
    assert [a.title for a in drawer.alerts(dash) if a.phase == "a-W2"] == []


def test_an_open_dashboard_sees_a_later_the_moment_it_is_queued(ws):
    """The report is not in the ledger yet, so the ledger file does not move."""
    cfg, project, sup = ws
    dash = Dash(cfg)
    dash.poll()
    assert dash.deferred == {} and _tui_counts(dash).ready == ["a-W2"]

    _waiting_unreported(cfg)
    assert "ledger" in dash.poll()
    total = _tui_counts(dash)
    assert dash.deferred == {"a-W2": FAR} and (total.dated, total.failed) == (1, 0)


def test_the_counts_never_call_a_row_that_waits_for_its_date_failed():
    graph = {"a-W1": set(), "a-W2": {"a-W1"}, "a-W3": {"a-W1"}}
    done = {"a-W1": "ok", "a-W2": "fail", "a-W3": "fail"}
    (a,) = campaign.summarise(graph, done, deferred={"a-W2": FAR})
    assert (a.dated, a.failed) == (1, 1)  # a-W3 has no date: it failed


@pytest.mark.parametrize("reported", [True, False])
def test_the_web_board_places_a_later_row_under_its_date(ws, reported):
    cfg, project, sup = ws
    _reported_or_queued(cfg, sup, reported)
    feed = Feed(cfg)
    feed.refresh(force=True)
    cards = {c["id"]: c for col in feed.board["columns"] for c in col["cards"]}

    assert (cards["a-W2"]["col"], cards["a-W2"]["sub"]) == ("blocked", f"waits until {FAR}")
    behind = cards["a-W3"]
    assert (behind["col"], behind["root"], behind["root_kind"]) == ("blocked", "a-W2", "dated")
    assert behind["root_sub"] == f"which waits until {FAR}"


@pytest.mark.parametrize("reported", [True, False])
def test_the_forecast_starts_a_later_row_on_its_date_and_never_calls_it_failed(ws, reported):
    cfg, project, sup = ws
    _reported_or_queued(cfg, sup, reported)

    plan = eta_engine.plan_of(eta_engine.from_files(cfg, state_mod.read(cfg)))
    assert plan.stuck == () and plan.gates == {"a-W2": eta_plan.day_start(FAR)}
    assert set(plan.rows) == {"a-W2", "a-W3"}  # both are forecast, behind the date


def test_doctor_does_not_call_a_later_the_ledger_has_not_taken_yet_failed(ws, monkeypatch):
    cfg, project, sup = ws
    monkeypatch.setattr(doctor, "_dir_size", lambda path: 0)
    _waiting_unreported(cfg)

    def failed() -> doctor.Check:
        return next(c for c in doctor.run_checks(cfg) if c.name == "phases.failed")

    assert (failed().status, failed().detail) == (doctor.OK, "no failed phases")
    with state_mod.transaction(cfg) as st:
        st.mark_done("a-W3", "fail")
    check = failed()
    assert check.status == doctor.WARN and "['a-W3']" in check.detail
    assert check.fix_hint.startswith("swarm retry a-W3")


def test_on_its_date_the_row_is_ready_again_and_the_launcher_is_run(ws, monkeypatch):
    cfg, project, sup = ws
    _build(cfg, sup)
    _finish(cfg, sup, "a-W2", "later", FAR)
    filled: list[str] = []
    monkeypatch.setattr(sup, "_fill_slots", lambda reason, **k: filled.append(reason) or [])

    sup._release_dated()
    assert filled == []  # the date is still ahead

    ledger = project / LEDGER_FILE
    ledger.write_text(ledger.read_text().replace(FAR, "2000-01-01"))
    _git(project, "commit", "-qam", "the date comes")
    sup._release_dated()

    assert filled == ["a `later` phase's date has come"]
    assert "LATER-DUE a-W2 its date has come" in cfg.supervisor_log.read_text()
    assert "a-W2" in master_mod.build_context(cfg, state_mod.read(cfg))["ready"]
    sup._release_dated()
    assert len(filled) == 1  # said once


def test_swarm_done_later_says_what_happens_to_the_work(ws):
    cfg, project, sup = ws
    with state_mod.transaction(cfg) as st:
        st.claim_slot("a-W2")
    text = launch_mod.done(cfg, "a-W2", "later", "needs a week of data", after=FAR).render()
    assert f"is kept until {FAR}" in text and "relaunched" in text

    with state_mod.transaction(cfg) as st:
        st.claim_slot("a-W3")
    text = launch_mod.done(cfg, "a-W3", "blocked", "the box is unreachable").render()
    assert "is kept until" not in text and "set aside" in text and ATTIC in text


def test_swarm_done_later_does_not_promise_what_an_older_supervisor_will_not_do(ws, monkeypatch):
    """The supervisor acts on the finish; one started on the earlier code and
    not restarted since still sets the work aside."""
    cfg, project, sup = ws
    pid = os.getpid()
    with state_mod.transaction(cfg) as st:
        st.claim_slot("a-W2")
    monkeypatch.setattr(restart_mod, "live_supervisor", lambda cfg, st=None: pid)
    mark = {"pid": pid, "ticks": procs.start_ticks(pid), "caps": ["handover", "restart-at"]}
    (cfg.state_dir / restart_mod.MARK_FILE).write_text(json.dumps(mark))

    text = launch_mod.done(cfg, "a-W2", "later", "needs a week of data", after=FAR).render()
    assert "is kept until" not in text and "swarm restart" in text and ATTIC in text

    restart_mod.mark_supervisor(cfg, adopted=False)  # the one this code starts
    text = launch_mod.done(cfg, "a-W2", "later", "needs a week of data", after=FAR,
                           force=True).render()
    assert f"is kept until {FAR}" in text
