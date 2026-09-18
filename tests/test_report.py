"""``swarm report`` — finally reading the recaps back, and the disagreements.

Every one of these fixtures is a hand-built state dir: the point of the module is
that it *joins* five independent records of the same phase, so the tests write
those records by hand and check the join, rather than driving a run and hoping it
leaves the right traces.

Two assertions carry most of the weight. The first is simply that the sentinel
BODY is read at all — :func:`gitq.sentinel_done` parses the filename and never
opens the file, so until now every recap a worker ever wrote was write-only. The
second is the pair of discrepancies the join makes visible: a sentinel status that
disagrees with the ``done`` map (for example phases whose sentinels all say
``needs-owner`` while state says ``ok`` — owner questions nobody was ever
asked) and a recap overwritten by a later ``swarm done``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from swarm_orchestrator import notes as notes_mod
from swarm_orchestrator import recap as recap_mod
from swarm_orchestrator import report as report_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import telegram as telegram_mod
from swarm_orchestrator.config import load

LEDGER = "- [ ] `P0` · needs:—\n- [ ] `P1` · needs:`P0`\n"
BASE = 1_700_000_000.0  # a fixed epoch so every duration assertion is exact


def _cfg(tmp_path: Path, monkeypatch, ledger: str = LEDGER):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True, exist_ok=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text(ledger, encoding="utf-8")
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    return cfg


def _sentinel(cfg, phase: str, status: str, note: str, at: float) -> None:
    """Write a sentinel exactly as :func:`launch._write_sentinel` does."""
    path = cfg.done_dir / f"{phase}.{status}"
    path.write_text(f"{phase} {status} {note}\n", encoding="utf-8")
    os.utime(path, (at, at))


def _log(cfg, lines: list[tuple[float, str]]) -> None:
    """A supervisor log in the current ``<iso> <mono> <msg>`` format."""
    cfg.supervisor_log.write_text(
        "".join(
            f"{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}"
            f" {ts - BASE:.3f} {msg}\n"
            for ts, msg in lines
        ),
        encoding="utf-8",
    )


def _done(cfg, mapping: dict[str, str]) -> None:
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(state_mod.State.fresh(2).__dict__)
        st.done = dict(mapping)


def _phase(rep, name):
    return next(p for p in rep.phases if p.phase == name)


def _kinds(rep):
    return {(w.phase, w.kind) for w in rep.warnings}


# -- the recap is finally read back ---------------------------------------
def test_the_sentinel_body_is_read_not_just_its_filename(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "needs-owner", "picked a 30d window; confirm before GA", BASE)
    _done(cfg, {"P0": "needs-owner"})

    rep = report_mod.build_report(cfg)

    assert _phase(rep, "P0").note == "picked a 30d window; confirm before GA"
    assert _phase(rep, "P0").sentinel_status == "needs-owner"


def test_a_sentinel_with_no_note_yields_an_empty_recap(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "", BASE)
    _done(cfg, {"P0": "ok"})

    assert _phase(report_mod.build_report(cfg), "P0").note == ""


def test_a_generated_recap_beats_the_sentinel_note(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "terse", BASE)
    (cfg.state_dir / "recaps").mkdir()
    (cfg.state_dir / "recaps" / "P0.json").write_text(
        json.dumps({"phase": "P0", "summary": "Schema v3 landed."}), encoding="utf-8"
    )
    _done(cfg, {"P0": "ok"})

    row = _phase(report_mod.build_report(cfg), "P0")
    assert row.recap == "Schema v3 landed."
    assert row.decision == "Schema v3 landed."  # the recap is the better answer
    assert row.note == "terse"  # but the raw note is still there


def test_the_recap_is_read_through_the_module_that_owns_the_format(
    tmp_path, monkeypatch
):
    # recap.py owns recaps/<phase>.json and its from_dict already drops unknown
    # keys. Going through its loader instead of re-parsing here is what stops the
    # two from drifting the first time a field is renamed.
    cfg = _cfg(tmp_path, monkeypatch)
    (cfg.state_dir / "recaps").mkdir()
    payload = recap_mod.Recap(
        phase="P0", status="ok", summary="Schema v3 landed.", ts=BASE
    ).to_dict()
    payload["a_field_from_a_future_version"] = 1
    (cfg.state_dir / "recaps" / "P0.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    assert _phase(report_mod.build_report(cfg), "P0").recap == "Schema v3 landed."


def test_a_recap_that_could_not_be_produced_keeps_its_reason(tmp_path, monkeypatch):
    # A dashboard cell reading "no turns captured" is worth far more than a blank
    # one nobody can explain, so the reason survives the join.
    cfg = _cfg(tmp_path, monkeypatch)
    (cfg.state_dir / "recaps").mkdir()
    (cfg.state_dir / "recaps" / "P0.json").write_text(
        json.dumps(
            recap_mod.Recap(phase="P0", summary=None, reason="no turns captured")
            .to_dict()
        ),
        encoding="utf-8",
    )

    row = _phase(report_mod.build_report(cfg), "P0")
    assert row.recap == "" and row.recap_reason == "no turns captured"


def test_a_corrupt_recap_or_jsonl_line_is_skipped_not_fatal(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    (cfg.state_dir / "recaps").mkdir()
    (cfg.state_dir / "recaps" / "P0.json").write_text("{not json", encoding="utf-8")
    (cfg.done_dir / "P1.jsonl").write_text(
        'not json\n{"ts": 1, "status": "ok", "note": "kept"}\n', encoding="utf-8"
    )

    rep = report_mod.build_report(cfg)

    assert _phase(rep, "P0").recap == ""
    assert [a.note for a in _phase(rep, "P1").attempts] == ["kept"]


# -- durations: the worker's time versus the merge queue's ----------------
def test_in_slot_and_integration_durations_are_split(tmp_path, monkeypatch):
    # CLAIM -> sentinel is the WORKER's time; sentinel -> `EVENT done` is the
    # integrator's, because that line is logged only after the merge and push.
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "landed", BASE + 3600)
    _log(cfg, [
        (BASE, "CLAIM P0 slot=0"),
        (BASE + 5, "LAUNCH P0 slot=0"),
        (BASE + 3720, "EVENT done P0 ok freed_slot=0 parked=False"),
    ])
    _done(cfg, {"P0": "ok"})

    row = _phase(report_mod.build_report(cfg), "P0")

    assert row.started == BASE
    assert row.finished == BASE + 3600
    assert row.slot_s == 3600.0
    assert row.integ_s == 120.0


def test_a_relaunched_phase_yields_two_runs_not_one_long_interval(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _log(cfg, [
        (BASE, "CLAIM P0 slot=0"),
        (BASE + 100, "EVENT done P0 fail freed_slot=0 parked=False"),
        (BASE + 200, "CLAIM P0 slot=1"),
        (BASE + 500, "EVENT done P0 ok freed_slot=1 parked=False"),
    ])
    _sentinel(cfg, "P0", "ok", "second time lucky", BASE + 480)
    _done(cfg, {"P0": "ok"})

    row = _phase(report_mod.build_report(cfg), "P0")

    assert [r.status for r in row.runs] == ["fail", "ok"]
    assert row.started == BASE  # the FIRST claim
    assert row.integrated == BASE + 500  # the LAST integration


def test_a_launch_denial_is_recorded(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _log(cfg, [(BASE, "LAUNCH-DENIED P1 unmet-deps [P0]")])

    assert _phase(report_mod.build_report(cfg), "P1").denials == ["unmet-deps [P0]"]


def test_a_legacy_monotonic_only_line_does_not_invent_a_wall_clock(
    tmp_path, monkeypatch
):
    # parse_ts refuses to decode a monotonic stamp from an earlier boot rather
    # than returning a confidently wrong time; the run still shapes correctly.
    cfg = _cfg(tmp_path, monkeypatch)
    cfg.supervisor_log.write_text(
        "999999999.000 CLAIM P0 slot=0\n"
        "999999999.500 EVENT done P0 ok freed_slot=0 parked=False\n",
        encoding="utf-8",
    )

    row = _phase(report_mod.build_report(cfg), "P0")

    assert row.runs and row.runs[0].status == "ok"
    assert row.started is None and row.slot_s is None


# -- the discrepancies the join makes visible ------------------------------
def test_a_sentinel_disagreeing_with_the_done_map_is_a_warning(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "needs-owner", "need a retention window", BASE)
    _done(cfg, {"P0": "ok"})  # a typical finished phase

    rep = report_mod.build_report(cfg)

    assert ("P0", "sentinel-disagrees") in _kinds(rep)
    assert "the owner was never asked" in rep.warnings[0].detail


def test_an_overwritten_recap_is_recoverable_from_the_jsonl(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "fail", "blew up on the migration", BASE)
    _sentinel(cfg, "P0", "ok", "reran with the fix", BASE + 60)
    (cfg.done_dir / "P0.jsonl").write_text(
        json.dumps({"ts": BASE, "status": "fail", "note": "blew up on the migration"})
        + "\n"
        + json.dumps({"ts": BASE + 60, "status": "ok", "note": "reran with the fix"})
        + "\n",
        encoding="utf-8",
    )
    _done(cfg, {"P0": "ok"})

    rep = report_mod.build_report(cfg)
    row = _phase(rep, "P0")

    assert {"multiple-sentinels", "recap-overwritten"} <= {
        k for p, k in _kinds(rep) if p == "P0"
    }
    assert [a.note for a in row.attempts] == [
        "blew up on the migration",
        "reran with the fix",
    ]
    assert row.note == "reran with the fix"  # the surviving sentinel


def test_a_done_phase_with_no_sentinel_is_a_warning(tmp_path, monkeypatch):
    # The sentinels ARE the durable record: gitq.sentinel_done rebuilds the whole
    # done map from them on restart, so a missing one is a real hole.
    cfg = _cfg(tmp_path, monkeypatch)
    _done(cfg, {"P0": "ok"})

    assert ("P0", "no-sentinel") in _kinds(report_mod.build_report(cfg))


def test_a_skipped_phase_is_not_a_missing_sentinel(tmp_path, monkeypatch):
    # `swarm skip` marks the done map directly and writes no sentinel by design.
    # On a large ledger that can be most phases -- warning on each one
    # buries the handful of real discrepancies underneath them.
    cfg = _cfg(tmp_path, monkeypatch)
    _done(cfg, {"P0": "skip"})

    assert ("P0", "no-sentinel") not in _kinds(report_mod.build_report(cfg))


def test_a_sentinel_the_supervisor_never_saw_is_a_warning(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "finished but the poke was lost", BASE)
    _done(cfg, {})

    assert ("P0", "orphan-sentinel") in _kinds(report_mod.build_report(cfg))


def test_an_in_flight_phase_is_not_an_orphan_sentinel(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "just finished", BASE)
    with state_mod.transaction(cfg) as st:
        st.__dict__.update(state_mod.State.fresh(2).__dict__)
        st.integ_queue = ["P0"]

    rep = report_mod.build_report(cfg)

    assert _phase(rep, "P0").live == "integrating"
    assert ("P0", "orphan-sentinel") not in _kinds(rep)


def test_the_jsonl_sidecar_is_not_mistaken_for_a_sentinel(tmp_path, monkeypatch):
    # `<phase>.jsonl` sits in done/ next to `<phase>.<status>`; only a real status
    # suffix makes a file a sentinel.
    cfg = _cfg(tmp_path, monkeypatch)
    (cfg.done_dir / "P0.jsonl").write_text(
        json.dumps({"ts": BASE, "status": "ok", "note": "n"}) + "\n", encoding="utf-8"
    )

    assert _phase(report_mod.build_report(cfg), "P0").sentinel_status is None


# -- did the owner ping actually land? ------------------------------------
def _ping(cfg, phase, kind="done", delivered=True, error=None, ts=BASE):
    """Append a notifications.jsonl row exactly as telegram._record does."""
    with (cfg.state_dir / telegram_mod.LEDGER_NAME).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": ts, "kind": kind, "phase": phase, "source": "worker",
            "text": "swarm: ...", "delivered": delivered, "error": error,
        }) + "\n")


def test_an_undelivered_owner_ping_is_a_warning(tmp_path, monkeypatch):
    # A recap saying "confirm before GA" and a telegram that failed to send look
    # identical in every other file; the send ledger is the only record.
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "needs-owner", "confirm before GA", BASE)
    _ping(cfg, "P0", delivered=False, error="connection refused")
    _done(cfg, {"P0": "needs-owner"})

    rep = report_mod.build_report(cfg)

    assert ("P0", "ping-undelivered") in _kinds(rep)
    assert "connection refused" in rep.warnings[0].detail
    assert _phase(rep, "P0").pings[0].delivered is False


def test_a_needs_owner_phase_with_no_ping_at_all_is_a_warning(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "needs-owner", "confirm before GA", BASE)
    _ping(cfg, "P1")  # the ledger exists, but nothing was sent for P0
    _done(cfg, {"P0": "needs-owner"})

    assert ("P0", "owner-never-pinged") in _kinds(report_mod.build_report(cfg))


def test_a_delivered_ping_is_not_a_warning(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "needs-owner", "confirm before GA", BASE)
    _ping(cfg, "P0", delivered=True)
    _done(cfg, {"P0": "needs-owner"})

    kinds = {k for p, k in _kinds(report_mod.build_report(cfg)) if p == "P0"}
    assert "ping-undelivered" not in kinds and "owner-never-pinged" not in kinds


def test_one_delivered_ping_among_failures_still_counts_as_reaching_them(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "fail", "blew up", BASE)
    _ping(cfg, "P0", delivered=False, error="timeout")
    _ping(cfg, "P0", delivered=True)  # a retry got through
    _done(cfg, {"P0": "fail"})

    assert ("P0", "ping-undelivered") not in _kinds(report_mod.build_report(cfg))


def test_an_ok_phase_owes_no_ping(tmp_path, monkeypatch):
    # launch._completion_ping stays silent for `ok`; only needs-owner and fail
    # owe the owner a telegram.
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "landed", BASE)
    _ping(cfg, "P1")
    _done(cfg, {"P0": "ok"})

    assert not [w for w in report_mod.build_report(cfg).warnings if w.phase == "P0"]


def test_no_ping_ledger_means_no_ping_warnings_at_all(tmp_path, monkeypatch):
    # A run from before telegram.py kept a ledger has records for no phase, which
    # is indistinguishable from every send having been lost. Stay silent.
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "needs-owner", "confirm before GA", BASE)
    _done(cfg, {"P0": "needs-owner"})

    kinds = {k for p, k in _kinds(report_mod.build_report(cfg)) if p == "P0"}
    assert "owner-never-pinged" not in kinds


def test_a_run_level_ping_belongs_to_no_phase(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    with (cfg.state_dir / telegram_mod.LEDGER_NAME).open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": BASE, "kind": "finish", "phase": None,
                             "delivered": True}) + "\n")
        fh.write("torn line with no closing brace\n")

    rep = report_mod.build_report(cfg)

    assert all(not p.pings for p in rep.phases)


def test_a_missing_recap_shows_its_reason_instead_of_a_blank(tmp_path, monkeypatch):
    # recap.py: summary and reason are mutually exclusive, and a null summary is
    # normal rather than corruption -- "no-turns-captured" beats an empty cell.
    cfg = _cfg(tmp_path, monkeypatch)
    (cfg.state_dir / "recaps").mkdir()
    (cfg.state_dir / "recaps" / "P0.json").write_text(
        json.dumps(recap_mod.Recap(phase="P0", reason="no-turns-and-no-sentinel")
                   .to_dict()),
        encoding="utf-8",
    )

    assert "(no recap: no-turns-and-no-sentinel)" in report_mod.render(
        report_mod.build_report(cfg)
    )


# -- totals, filters, rendering -------------------------------------------
def test_totals_count_by_status_and_sum_slot_hours(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "one", BASE + 3600)
    _sentinel(cfg, "P1", "fail", "two", BASE + 7200)
    _log(cfg, [
        (BASE, "CLAIM P0 slot=0"),
        (BASE + 3600, "EVENT done P0 ok freed_slot=0 parked=False"),
        (BASE + 3600, "CLAIM P1 slot=0"),
        (BASE + 7200, "EVENT done P1 fail freed_slot=0 parked=False"),
    ])
    _done(cfg, {"P0": "ok", "P1": "fail"})

    totals = report_mod.build_report(cfg).totals

    assert totals.by_status == {"fail": 1, "ok": 1}
    assert totals.slot_hours == pytest.approx(2.0)  # 1h + 1h
    assert totals.median_s == pytest.approx(3600.0)
    assert totals.mean_s == pytest.approx(3600.0)


def test_since_keeps_only_recent_phases_and_drops_untimed_ones(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "old", BASE)
    _sentinel(cfg, "P1", "ok", "new", BASE + 7200)
    _done(cfg, {"P0": "ok", "P1": "ok"})

    names = [p.phase for p in report_mod.build_report(cfg, since=BASE + 3600).phases]

    assert names == ["P1"]  # P0 is older; a phase with no stamps at all also drops


def test_decisions_keeps_only_phases_that_said_something(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "ok", BASE)  # filler: not a decision
    _sentinel(cfg, "P1", "needs-owner", "chose the 30d retention window", BASE)
    _done(cfg, {"P0": "ok", "P1": "needs-owner"})

    rep = report_mod.build_report(cfg)
    text = report_mod.render(rep, decisions=True)

    assert _phase(rep, "P1").substantive() and not _phase(rep, "P0").substantive()
    assert "chose the 30d retention window" in text
    assert "1 of 2 phases recorded a decision" in text


def test_swarm_note_entries_surface_in_decisions(tmp_path, monkeypatch):
    # notes.py's own docstring names `swarm report --decisions` as where a note
    # is reviewed, so a phase with a note is substantive even if its recap is
    # the filler a worker types because the argument is required.
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "ok", BASE)
    notes_mod.add(cfg, "P0", "used a 30d window; reversible via config", "decision")
    notes_mod.add(cfg, "P0", "assumed the upstream schema is frozen", "assumption")
    _done(cfg, {"P0": "ok"})

    rep = report_mod.build_report(cfg)
    row = _phase(rep, "P0")
    text = report_mod.render(rep, decisions=True)

    assert len(row.notes) == 2 and row.substantive()
    assert "[decision] used a 30d window; reversible via config" in text
    assert "[assumption] assumed the upstream schema is frozen" in text
    assert "2 explicit `swarm note` entries" in text


def test_a_refused_done_call_leaves_its_recap_only_in_the_jsonl(tmp_path, monkeypatch):
    # `written | forced | refused` is launch._write_sentinel's verdict. A refused
    # duplicate never reached the sentinel, so the jsonl is the only copy.
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "ok", "the first, surviving recap", BASE)
    (cfg.done_dir / "P0.jsonl").write_text(
        json.dumps({"ts": BASE, "status": "ok", "note": "the first, surviving recap",
                    "verdict": "written"}) + "\n"
        + json.dumps({"ts": BASE + 1, "status": "ok", "note": "a second thought",
                      "verdict": "refused"}) + "\n",
        encoding="utf-8",
    )
    _done(cfg, {"P0": "ok"})

    rep = report_mod.build_report(cfg)

    assert ("P0", "recap-refused") in _kinds(rep)
    assert [a.verdict for a in _phase(rep, "P0").attempts] == ["written", "refused"]


def test_a_phase_filter_narrows_to_one(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _done(cfg, {"P0": "ok", "P1": "ok"})

    assert [p.phase for p in report_mod.build_report(cfg, phase="P1").phases] == ["P1"]


def test_report_is_json_serialisable_and_renders(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _sentinel(cfg, "P0", "needs-owner", "needs a window", BASE)
    _done(cfg, {"P0": "ok"})

    rep = report_mod.build_report(cfg)
    payload = json.loads(json.dumps(rep.to_dict()))
    text = report_mod.render(rep)

    assert payload["totals"]["phases"] == 2
    assert payload["warnings"][0]["kind"] == "sentinel-disagrees"
    assert "P0" in text and "warnings (1)" in text


def test_an_empty_state_dir_reports_nothing_rather_than_crashing(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch, ledger="")
    rep = report_mod.build_report(cfg)
    assert rep.phases == [] and "no phases recorded" in report_mod.render(rep)


# -- `--since` parsing -----------------------------------------------------
@pytest.mark.parametrize(
    "text, delta",
    [("90s", 90), ("30m", 1800), ("6h", 21600), ("3d", 259200), ("2w", 1209600)],
)
def test_parse_since_relative(text, delta):
    assert report_mod.parse_since(text, now=BASE) == BASE - delta


def test_parse_since_absolute():
    assert report_mod.parse_since("2026-08-27", now=BASE) == datetime(
        2026, 8, 27
    ).timestamp()


def test_parse_since_rejects_nonsense_instead_of_reporting_everything():
    with pytest.raises(ValueError, match="unparseable"):
        report_mod.parse_since("last tuesday")
