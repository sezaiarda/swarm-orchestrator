"""Batch claims as every reader sees them: a rider is running, in its seed's slot.

A batch's riders hold no slot (``State.batches``): the seed's session builds
them one after another, each reports on its own (``State.batch_done``,
``BATCH-ROW``) and nothing is recorded before the batch lands. Every view that
shows or counts what runs must count a rider as running, pair its ``CLAIM``
with its ``EVENT done``, and raise no alarm over its sentinel. The run time
itself is ``test_batch.py``'s.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

from swarm_orchestrator import cli, doctor, ovdigest, report
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import why as why_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.eta import engine
from swarm_orchestrator.pace import History
from swarm_orchestrator.tui import campaign, data
from swarm_orchestrator.web import board

LEDGER = """# Ledger

- [x] `k-W0` · needs:— · **the base**
- [ ] `k-W1` · needs:`k-W0` · **one**
- [ ] `k-W2` · needs:`k-W0` · **two**
- [ ] `k-W3` · needs:`k-W0` · **three**
- [ ] `z-W1` · needs:— · **unrelated**
"""
ROWS = ["k-W1", "k-W2", "k-W3"]


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.delenv("SWARM_GIT_ISOLATION", raising=False)
    for leak in ("SWARM_SESSION_ID", "SWARM_PHASE", "SWARM_WORKER_CMD"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    return c


def _batch(cfg, reported: dict[str, str] | None = None) -> state_mod.State:
    """Slot 0 runs ``k-W1``'s session over the batch ``ROWS``, as ``launch_outcome``
    claims it; ``reported`` rows have said ``swarm done`` (and left a sentinel)."""
    reported = reported or {}
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(state_mod.State.fresh(2).__dict__)
        st.claim_slot(ROWS[0])
        st.batches[ROWS[0]] = list(ROWS)
        st.batch_done.update(reported)
    for row, status in reported.items():
        (cfg.done_dir / f"{row}.{status}").write_text(f"{row} recap\n", encoding="utf-8")
    return state_mod.read(cfg)


def _log(cfg, *entries: tuple[float, str]) -> None:
    """``(epoch, message)`` lines in the supervisor's own format."""
    with cfg.supervisor_log.open("a", encoding="utf-8") as fh:
        for ts, msg in entries:
            stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            fh.write(f"{stamp} 1.000 {msg}\n")


T0 = time.time() - 3 * 3600


def _claims() -> list[tuple[float, str]]:
    return [(T0, "CLAIM k-W1 slot=0"), (T0, "CLAIM k-W2 slot=0 batch=k-W1"),
            (T0, "CLAIM k-W3 slot=0 batch=k-W1"), (T0 + 5, "LAUNCH k-W1 slot=0")]


def _events(*entries: tuple[float, str]) -> list[data.Event]:
    return [data.parse_event(datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                             + f" 1.000 {msg}") for ts, msg in entries]


# -- status and the dashboard -----------------------------------------------------
def test_status_and_the_dashboard_count_every_row_of_a_batch_as_running(cfg, capsys):
    st = _batch(cfg, {"k-W1": "ok"})
    assert campaign.counts(cfg, st)["running"] == 3

    snap = data.build_snapshot(cfg, asdict(st), graph=data.load_graph(cfg),
                               ticked=data.load_ticked(cfg))
    slot = snap.slots[0]
    assert slot.rows == tuple(ROWS) and slot.label == "k-W1 +2"
    assert snap.progress.running == 3 and snap.progress.ready == 1  # z-W1 alone
    assert data.batch_line(slot.batch, snap.batch_done) == "k-W1 ok · k-W2 at work · k-W3"

    assert cli.cmd_status(cfg) == 0
    assert "BUSY k-W1 batch=k-W1:ok,k-W2,k-W3" in capsys.readouterr().out


def test_the_board_builds_a_rider_in_its_seeds_slot(cfg):
    st = _batch(cfg, {"k-W2": "ok"})
    snap = data.build_snapshot(cfg, asdict(st), graph=data.load_graph(cfg))
    assert board._in_flight(snap, {}, []) == {r: "building" for r in ROWS}
    subs = {}
    for row in ROWS:
        extra = {"slot": 0}
        board._batch_sub(row, extra, snap)
        subs[row] = extra["sub"]
    assert subs == {"k-W1": "its session builds a batch of 3",
                    "k-W2": "reported ok · lands with its batch",
                    "k-W3": "rides in k-W1's batch"}


# -- run history: each row its own run ----------------------------------------------
def test_a_riders_run_is_its_turn_from_the_report_before_it_to_its_own(cfg):
    st = _batch(cfg, {"k-W1": "ok", "k-W2": "ok"})
    events = _events(*_claims(), (T0 + 600, "BATCH-ROW k-W1 ok batch=k-W1 left=2"),
                     (T0 + 1500, "BATCH-ROW k-W2 ok batch=k-W1 left=1"))
    runs = {r.phase: r for r in data.build_history(events, state=asdict(st))}
    # Nothing is lost while the batch builds on: k-W3 is at work, the others wait to land.
    assert runs["k-W3"].running and runs["k-W3"].status is None
    assert runs["k-W2"].hold == "integrating"
    assert (runs["k-W2"].started_at, runs["k-W2"].ended_at) == pytest.approx((T0 + 600, T0 + 1500),
                                                                             abs=0.01)

    events += _events((T0 + 2000, "BATCH-ROW k-W3 ok batch=k-W1 left=0"),
                      (T0 + 2100, "EVENT done k-W2 ok"), (T0 + 2100, "EVENT done k-W3 ok"),
                      (T0 + 2101, "EVENT done k-W1 ok"))
    runs = {r.phase: r for r in data.build_history(events, state={"slots": []})}
    assert {p: r.status for p, r in runs.items()} == {r: "ok" for r in ROWS}
    assert [round(runs[r].duration_s) for r in ROWS] == [595, 900, 500]


def test_a_rider_handed_back_ends_its_run_with_its_reason(cfg):
    events = _events(*_claims(), (T0 + 60, "UNBATCH k-W3 batch=k-W1"),
                     (T0 + 60, "RUN-ENDED k-W3 reason=unbatched"))
    run = next(r for r in data.build_history(events) if r.phase == "k-W3")
    assert run.status == data.LOST and run.why == "unbatched"
    assert "swarm unbatch" in data.ENDED_WHY[run.why]


def test_report_pairs_a_riders_claim_with_its_done_and_counts_it_live(cfg):
    _batch(cfg, {"k-W2": "ok"})
    _log(cfg, *_claims(), (T0 + 900, "BATCH-ROW k-W2 ok batch=k-W1 left=2"))
    rep = {p.phase: p for p in report.build_report(cfg).phases}
    assert rep["k-W3"].live == "busy" and rep["k-W2"].live == "integrating"
    assert rep["k-W2"].runs[0].claimed == pytest.approx(T0, abs=0.01)
    assert not rep["k-W2"].warnings  # its sentinel waits for the batch: no orphan

    _log(cfg, (T0 + 3000, "EVENT done k-W2 ok"))
    run = report._read_log(cfg)[0]["k-W2"][0]
    assert (run.status, run.done_at) == ("ok", pytest.approx(T0 + 3000, abs=0.01))


# -- why ----------------------------------------------------------------------------
def test_why_says_a_rider_rides_in_its_seeds_batch(cfg):
    st = _batch(cfg, {"k-W2": "ok"})
    rider = why_mod.explain(cfg, "k-W3", st)
    assert rider.reason == why_mod.BUSY and "rides in k-W1's batch" in rider.detail
    assert "slot 0" in rider.detail
    done = why_mod.explain(cfg, "k-W2", st)
    assert done.reason == why_mod.INTEGRATING and "reported `ok`" in done.detail
    assert "2 more rows have reported" in done.detail
    seed = why_mod.explain(cfg, "k-W1", st)
    assert seed.reason == why_mod.BUSY and "k-W2, k-W3 in its batch" in seed.detail


# -- the forecast -------------------------------------------------------------------
def test_the_forecast_runs_a_batch_one_row_after_another_in_its_slot(cfg):
    st = _batch(cfg, {"k-W1": "ok"})
    events = _events(*_claims(), (T0 + 600, "BATCH-ROW k-W1 ok batch=k-W1 left=2"))
    inputs = engine.gather(cfg, st, events=events, history=[], ledger_history=History(),
                           usage=[], text=LEDGER, now=T0 + 900)
    assert engine.at_work(st) == ["k-W2"]
    assert inputs.started["k-W2"] == pytest.approx(T0 + 600, abs=0.01)  # its turn

    plan = engine.plan_of(inputs)
    assert set(plan.running) == {"k-W2"}
    assert plan.running["k-W2"] == pytest.approx(300, abs=0.01)
    assert "k-W1" in plan.finishing  # reported: lands with the batch
    assert "k-W2" in plan.rows["k-W3"].needs  # waits its turn, not a free seat
    book = plan.books["k"]
    assert set(book.running) >= {"k-W2", "k-W3"} and not set(book.ready) & set(ROWS)


# -- doctor and the Overseer's digest ---------------------------------------------------
def test_doctor_raises_no_alarm_over_a_riders_report(cfg):
    st = _batch(cfg, {"k-W1": "ok", "k-W2": "ok"})
    check = doctor._check_sentinels(cfg, st)
    assert check.status == doctor.OK and "2 landing" in check.detail
    # The seed reported, but its session builds on: it has not finished.
    assert not doctor._reported(cfg, st, "k-W1")


def test_the_digest_counts_riders_as_building(cfg):
    st = _batch(cfg, {"k-W2": "ok"})
    assert ovdigest.in_flight(st) == {r: "building" for r in ROWS}
    assert "2 building now" in ovdigest.own_summary(cfg, st, since=T0)  # k-W1, k-W3
