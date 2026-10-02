"""Work kept for a ``later`` row whose row is closed some other way since.

A phase finishes ``later``: its work is kept under ``refs/swarm-later/<phase>``
and its failure record goes once the ledger carries the date. If the row is
then closed with ``swarm record <phase> done``, or ticked by hand, nothing is
left in the done map to say so: the ledger is the only place that knows. The
kept work then goes to the attic, at once on the record, on the supervisor's
next sweep for a tick by hand, and on ``swarm up``. Nothing relaunches from it.

The row decides, as committed on the target branch. A row that still waits for
its date, a tick nobody committed, a ticked row that says ``later``, a row a
report is still queued for and a phase with a worker on it keep their work.

On real git, through the supervisor's own handlers.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import cli, gitq, ledgerw
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.supervisor import Supervisor

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
    (project / ".swarm.toml").write_text(
        f'[swarm]\ndriver = "bare"\n[tasks]\nledger = "{LEDGER_FILE}"\n')
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_SLUG", "laterclosed")
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


def _kept(cfg, sup, project: Path, phase: str = "a-W2") -> str:
    """A worker's attempt that finishes ``later``, as the supervisor receives
    it: a commit and an uncommitted edit, kept for the date. Returns the kept tip."""
    wt = gitq.worktree_add(cfg, phase, sup.log)
    (wt / "code.txt").write_text("built\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", f"{phase} work")
    (wt / "notes.txt").write_text("measured so far\n")
    with state_mod.transaction(cfg) as st:
        st.claim_slot(phase)
    (cfg.done_dir / f"{phase}.fail").write_text(f"{phase} fail waiting\n")
    ledgerw.queue(cfg, phase, {"kind": "outcome", "outcome": "later",
                               "note": "needs a week of data", "after": FAR})
    sup._on_done(phase, "fail")
    # The row carries the date, so no record is left: the ledger alone knows.
    assert f"status: later, after {FAR}" in (project / LEDGER_FILE).read_text()
    assert phase not in state_mod.read(cfg).done
    assert _refs(project, KEPT) == [f"{KEPT}/{phase}"] and _refs(project, ATTIC) == []
    return _git(project, "rev-parse", f"{KEPT}/{phase}").strip()


def _record_done(cfg, sup, phase: str = "a-W2") -> None:
    """``swarm record <phase> done`` from another session, and the supervisor
    reading the poke."""
    assert cli.cmd_record(cfg, phase, "done", "measured by hand, it holds", "") == 0
    sup._handle("ledger")


def _edit_row(project: Path, phase: str, *, status: str | None) -> None:
    """Tick the row in the working file; ``status`` replaces its short status."""
    ledger = project / LEDGER_FILE
    text = ledger.read_text().replace(f"- [ ] `{phase}`", f"- [x] `{phase}`")
    if status is not None:
        text = re.sub(rf"(`{phase}`.*status: )[^·\n]*", rf"\g<1>{status} ", text)
    assert text != ledger.read_text()
    ledger.write_text(text)


def _tick_by_hand(project: Path, phase: str = "a-W2") -> None:
    """Someone closes the row in the ledger, as the swarm would have written it."""
    _edit_row(project, phase, status="done (by hand)")
    _git(project, "commit", "-qam", f"{phase}: closed by hand")


def _in_the_attic(cfg, project: Path, tip: str, phase: str = "a-W2") -> bool:
    """The kept ref is gone and one attic ref holds exactly the kept work."""
    attic = _refs(project, ATTIC)
    return (_refs(project, KEPT) == [] and gitq.kept_later(cfg) == set()
            and len(attic) == 1 and attic[0].startswith(f"{ATTIC}/{phase}/")
            and _git(project, "rev-parse", attic[0]).strip() == tip
            and _git(project, "show", f"{attic[0]}:code.txt") == "built\n"
            and _git(project, "show", f"{attic[0]}:notes.txt") == "measured so far\n")


def _still_kept(cfg, project: Path, tip: str, phase: str = "a-W2") -> bool:
    return (_refs(project, KEPT) == [f"{KEPT}/{phase}"] and _refs(project, ATTIC) == []
            and gitq.kept_later(cfg) == {phase}
            and _git(project, "rev-parse", f"{KEPT}/{phase}").strip() == tip)


# -- the running supervisor ----------------------------------------------------
def test_recording_the_row_done_sends_its_kept_work_to_the_attic(ws):
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)

    _record_done(cfg, sup)

    assert "- [x] `a-W2`" in (project / LEDGER_FILE).read_text()
    assert _in_the_attic(cfg, project, tip)
    log = cfg.supervisor_log.read_text()
    assert "LATER-CLOSED a-W2" in log and "(its row is closed)" in log


def test_a_tick_by_hand_sends_its_kept_work_to_the_attic_on_the_next_sweep(ws):
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)

    _tick_by_hand(project)
    assert _still_kept(cfg, project, tip)  # nothing has looked yet
    sup._watchdog_tick()

    assert _in_the_attic(cfg, project, tip)
    assert "LATER-CLOSED a-W2" in cfg.supervisor_log.read_text()


def test_the_sweep_moves_it_once(ws):
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)
    _tick_by_hand(project)

    assert sup._release_dated() is False  # no record changed, nothing new is ready
    sup._release_dated()

    assert _in_the_attic(cfg, project, tip)
    assert cfg.supervisor_log.read_text().count("LATER-CLOSED a-W2") == 1


def test_a_closed_row_never_restarts_from_the_kept_work(ws):
    """What the attic is for: a rerun of the row starts from main."""
    cfg, project, sup = ws
    _kept(cfg, sup, project)
    _tick_by_hand(project)
    sup._watchdog_tick()

    wt = gitq.worktree_add(cfg, "a-W2", sup.log)

    assert not (wt / "code.txt").exists() and not (wt / "notes.txt").exists()


# -- swarm up ------------------------------------------------------------------
def test_swarm_up_sends_the_kept_work_of_a_row_ticked_by_hand_to_the_attic(ws):
    """Ticked while no supervisor ran."""
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)
    _tick_by_hand(project)

    cli._reconcile_orphans(cfg)

    assert _in_the_attic(cfg, project, tip)


def test_swarm_up_sends_the_kept_work_of_a_row_recorded_done_to_the_attic(ws, monkeypatch):
    """Recorded done under a supervisor that did not move kept work yet (the
    code before this rule): the row is closed as the swarm writes it, and the
    kept ref is still there when ``swarm up`` comes."""
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)
    with monkeypatch.context() as earlier:
        earlier.setattr(ledgerw, "release_kept", lambda *a, **k: [], raising=False)
        _record_done(cfg, sup)
    assert "- [x] `a-W2`" in (project / LEDGER_FILE).read_text()
    assert _still_kept(cfg, project, tip)

    cli._reconcile_orphans(cfg)

    assert _in_the_attic(cfg, project, tip)


def test_kept_work_that_is_on_main_already_is_dropped_without_an_attic_copy(ws):
    """Landed by hand, then ticked: nothing is left to keep."""
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)
    _git(project, "merge", "-q", "--no-edit", tip)
    _tick_by_hand(project)

    sup._watchdog_tick()

    assert _refs(project, KEPT) == [] and _refs(project, ATTIC) == []
    assert (project / "code.txt").read_text() == "built\n"


# -- what keeps its work -------------------------------------------------------
def test_a_row_that_still_waits_for_its_date_keeps_its_work(ws):
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)

    sup._watchdog_tick()
    cli._reconcile_orphans(cfg)

    assert _still_kept(cfg, project, tip)
    assert "LATER-CLOSED" not in cfg.supervisor_log.read_text()


def test_a_row_whose_date_has_come_keeps_its_work_for_its_relaunch(ws):
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)
    ledger = project / LEDGER_FILE
    ledger.write_text(ledger.read_text().replace(FAR, "2000-01-01"))
    _git(project, "commit", "-qam", "the date comes")
    assert "a-W2" not in ledgerw.dated(cfg)

    sup._watchdog_tick()
    cli._reconcile_orphans(cfg)

    assert _still_kept(cfg, project, tip)
    wt = gitq.worktree_add(cfg, "a-W2", sup.log)
    assert (wt / "code.txt").read_text() == "built\n"


def test_a_tick_that_is_not_committed_moves_nothing(ws):
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)
    _edit_row(project, "a-W2", status="done (by hand)")

    sup._watchdog_tick()
    cli._reconcile_orphans(cfg)

    assert _still_kept(cfg, project, tip)


def test_a_ticked_row_that_says_later_keeps_its_work(ws):
    """Only the box was ticked: the row still says it waits for its date."""
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)
    _edit_row(project, "a-W2", status=None)
    _git(project, "commit", "-qam", "a-W2: the box only")

    sup._watchdog_tick()
    cli._reconcile_orphans(cfg)

    assert _still_kept(cfg, project, tip)


@pytest.mark.parametrize("report", ["its own", "a record"])
def test_a_closed_row_with_a_report_still_queued_keeps_its_work_until_it_lands(ws, report):
    """The row is about to change, so the ledger does not speak for it yet."""
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)
    _tick_by_hand(project)
    if report == "its own":  # the phase ran again and its report waits to land
        ledgerw.queue(cfg, "a-W2", {"kind": "outcome", "outcome": "fail",
                                    "note": "ran again, the box was down", "after": ""})
    else:
        assert cli.cmd_record(cfg, "a-W2", "note", "still being written about", "") == 0

    assert ledgerw.release_kept(cfg, sup.log) == []
    sup._release_dated()
    assert _still_kept(cfg, project, tip)

    if report == "its own":
        (ledgerw.queue_dir(cfg) / "a-W2.json").unlink()  # withdrawn
    else:
        sup._handle("ledger")  # the note lands; the row stays closed
    sup._release_dated()
    assert _in_the_attic(cfg, project, tip)


def test_the_usual_sweep_reads_no_ledger_from_git(ws, monkeypatch):
    """Every kept row waits for its date: nothing to decide, so nothing is read."""
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)

    def never(*a, **k):
        raise AssertionError("the committed ledger was read")

    monkeypatch.setattr(gitq, "committed_text", never)

    assert ledgerw.release_kept(cfg, sup.log) == []
    assert _still_kept(cfg, project, tip)


@pytest.mark.parametrize("where", ["slot", "landing"])
def test_a_closed_row_with_a_worker_on_it_keeps_its_work(ws, where):
    cfg, project, sup = ws
    tip = _kept(cfg, sup, project)
    _tick_by_hand(project)
    with state_mod.transaction(cfg) as st:
        if where == "slot":
            st.claim_slot("a-W2")
        else:
            st.integ_push("a-W2", "ok")

    assert ledgerw.release_kept(cfg, sup.log) == []
    assert _still_kept(cfg, project, tip)


def test_only_the_closed_row_is_moved(ws):
    """Two rows wait for a date; one is closed."""
    cfg, project, sup = ws
    ledger = project / LEDGER_FILE
    ledger.write_text(ledger.read_text().replace("needs:`a-W2`", "needs:`a-W1`"))
    _git(project, "commit", "-qam", "a-W3 no longer waits for a-W2")
    _kept(cfg, sup, project)
    _git(project, "update-ref", f"{KEPT}/a-W3", _git(project, "rev-parse", f"{KEPT}/a-W2").strip())
    ledger.write_text(ledger.read_text().replace(
        "- [ ] `a-W3` · dir:`alpha` · needs:`a-W1`",
        f"- [ ] `a-W3` · dir:`alpha` · needs:`a-W1` · status: later, after {FAR}"))
    _git(project, "commit", "-qam", "a-W3 waits for its date")
    _tick_by_hand(project)

    sup._watchdog_tick()

    assert _refs(project, KEPT) == [f"{KEPT}/a-W3"]
    assert [r.split("/")[2] for r in _refs(project, ATTIC)] == ["a-W2"]
