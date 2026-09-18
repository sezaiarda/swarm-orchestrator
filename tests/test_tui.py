"""Tests for the dashboard's data layer.

The dashboard is the one component that reads *every* file the swarm writes,
including several written by features that may not exist yet on a given day. It
runs unattended in tmux window 0 for the whole life of a run, so a panel that
raises on a half-written JSONL line takes the owner's only view of the swarm with
it. These tests therefore split into two halves:

* **correctness** — a log/sentinel/notification/recap corpus in, the right view
  model out, including the awkward cases (a retried phase, a parked worker whose
  ``done`` frees no slot, a legacy monotonic-only log line);
* **never raises** — every loader pointed at a missing directory, a truncated
  file, and a file full of garbage.

Rendering is deliberately not tested; the two Textual harness tests at the bottom
only assert that the app boots and switches tabs against an empty state dir,
which is the "must not crash when the run has never started" requirement.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from swarm_orchestrator.config import load as load_config
from swarm_orchestrator.tui import data, probes

BASE = datetime(2026, 8, 27, 9, 0, 0).timestamp()


def stamp(offset: float) -> str:
    """A wall-clock + monotonic log prefix, ``offset`` seconds into the run."""
    when = datetime.fromtimestamp(BASE + offset)
    return f"{when.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {1000.0 + offset:.3f}"


def line(offset: float, message: str) -> str:
    return f"{stamp(offset)} {message}"


def log_text(*entries: tuple[float, str]) -> str:
    return "".join(f"{line(off, msg)}\n" for off, msg in entries)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A real :class:`Config` bound to an empty temp project + state dir."""
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text(
        "P0\nP1 needs:P0\nP2 needs:P0\nP3 needs:P1,P2\n", encoding="utf-8"
    )
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "test")
    cfg = load_config(project_dir=str(project))
    cfg.ensure_dirs()
    return cfg


# -- log parsing ----------------------------------------------------------
def test_parse_event_reads_the_wall_clock_format():
    ev = data.parse_event(line(12, "LAUNCH P1 slot=2"))
    assert ev.kind == "launch"
    assert ev.phase == "P1"
    assert ev.fields["slot"] == "2"
    assert ev.ts == pytest.approx(BASE + 12, abs=0.01)


def test_parse_event_unwraps_the_event_container_verb():
    ev = data.parse_event(line(30, "EVENT done P1 ok freed_slot=2 parked=False"))
    assert (ev.kind, ev.phase, ev.status) == ("done", "P1", "ok")
    assert ev.fields == {"freed_slot": "2", "parked": "False"}


def test_parse_event_reads_a_legacy_monotonic_only_line():
    # Pre-wall-clock logs are still on disk in every live state dir; they must
    # parse into a usable event even when their epoch is undecodable.
    ev = data.parse_event("163262.783 EVENT done P9 fail freed_slot=0 parked=False")
    assert (ev.kind, ev.phase, ev.status) == ("done", "P9", "fail")


@pytest.mark.parametrize("bad", ["", "   ", "\n", "not a log line at all", "EVENT"])
def test_parse_event_never_raises_on_junk(bad):
    assert isinstance(data.parse_event(bad), data.Event)


def test_parse_event_only_reads_a_phase_shaped_token_as_a_phase():
    assert data.parse_event(line(1, "SUPERVISOR-START pid=41 driver=tmux")).phase is None


def test_log_tail_is_incremental_and_survives_truncation(tmp_path):
    path = tmp_path / "supervisor.log"
    path.write_text(log_text((0, "SUPERVISOR-START pid=1 driver=tmux")), encoding="utf-8")
    tail = data.LogTail(path)

    assert len(tail.poll()) == 1
    assert tail.poll() == []  # nothing appended -> no work, no re-read

    with path.open("a", encoding="utf-8") as fh:
        fh.write(log_text((5, "LAUNCH P0 slot=0")))
    fresh = tail.poll()
    assert [e.kind for e in fresh] == ["launch"]
    assert len(tail.events) == 2

    # A state dir recreated under the same path restarts the file; the tail must
    # reset rather than seek past the end and return nothing forever.
    path.write_text(log_text((0, "SUPERVISOR-START pid=2 driver=tmux")), encoding="utf-8")
    assert len(tail.poll()) == 1
    assert len(tail.events) == 1


def test_log_tail_holds_back_a_partial_line(tmp_path):
    path = tmp_path / "supervisor.log"
    path.write_text(log_text((0, "LAUNCH P0 slot=0")), encoding="utf-8")
    tail = data.LogTail(path)
    tail.poll()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp(3)} EVENT done P0")  # mid-write, no newline yet
    assert tail.poll() == []
    with path.open("a", encoding="utf-8") as fh:
        fh.write(" ok freed_slot=0 parked=False\n")
    assert [e.status for e in tail.poll()] == ["ok"]


def test_log_tail_on_a_missing_file_is_empty(tmp_path):
    assert data.LogTail(tmp_path / "nope.log").poll() == []


# -- state normalisation --------------------------------------------------
def test_read_state_returns_none_before_the_first_run(cfg):
    # And without creating the state dir: the dashboard is strictly read-only,
    # and a synthesised "fresh" state would render a never-started project as a
    # healthy run with N idle slots.
    assert data.read_state(cfg) is None


def test_read_state_falls_back_to_raw_json_for_unknown_fields(cfg):
    # A state file written by a newer build carries slot fields this one may not
    # know; the typed load would TypeError and blank the whole dashboard.
    cfg.state_path.write_text(
        json.dumps({"slots": [{"id": 0, "busy": False, "some_future_field": 7}], "done": {}}),
        encoding="utf-8",
    )
    state = data.read_state(cfg)
    assert state is not None and state["slots"][0]["id"] == 0


def test_read_state_survives_a_corrupt_state_file(cfg):
    cfg.state_path.write_text("{not json", encoding="utf-8")
    assert data.read_state(cfg) is None


@pytest.mark.parametrize(
    "raw,statuses,expected",
    [
        (["P1", "P2"], None, [("P1", "ok"), ("P2", "ok")]),
        (["P1"], {"P1": "needs-owner"}, [("P1", "needs-owner")]),
        ([["P1", "needs-owner"]], None, [("P1", "needs-owner")]),
        ([{"phase": "P1", "status": "queued"}], None, [("P1", "queued")]),
        ([{"phase": "P1"}], {"P1": "ok"}, [("P1", "ok")]),
        (None, None, []),
        ("P1", None, []),
        ([None, 3, {}, []], None, []),
    ],
)
def test_normalize_queue_reads_every_shape(raw, statuses, expected):
    assert data.normalize_queue(raw, statuses) == expected


# -- snapshot -------------------------------------------------------------
def test_build_snapshot_with_no_state_degrades_instead_of_raising(cfg):
    snap = data.build_snapshot(cfg, None)
    assert snap.ok is False and snap.reason
    assert snap.slots == [] and snap.progress.total == 0


def test_build_snapshot_joins_slots_ledger_and_timings(cfg):
    state = {
        "slots": [
            {"id": 0, "busy": True, "phase": "P1", "pane_id": "%1", "branch": "swarm/P1"},
            {"id": 1, "busy": False, "phase": None, "pane_id": "%2", "retiring": True},
        ],
        "done": {"P0": "ok"},
        "supervisor_pid": None,
        "last_event_at": BASE + 40,
    }
    snap = data.build_snapshot(
        cfg,
        state,
        graph=data.load_graph(cfg),
        launch_times={"P1": BASE + 10},
        started_at=BASE,
    )
    assert snap.ok is True
    assert snap.slots[0].phase == "P1" and snap.slots[0].started_at == BASE + 10
    assert snap.slots[1].retiring is True
    assert snap.supervisor_alive is False  # no pid recorded -> never claim it is up
    assert snap.last_event_at == BASE + 40
    # P0 done, P1 running, P2 ready (P0 satisfied it), P3 blocked on P1+P2.
    assert (snap.progress.done, snap.progress.running) == (1, 1)
    assert "P2" in snap.progress.next_up
    assert snap.progress.blocked == 1


def test_build_snapshot_surfaces_every_kind_of_owner_blocker(cfg):
    state = {
        "slots": [],
        "done": {"P4": "needs-owner"},
        "waiting": {"P1": BASE + 300},
        "parked": ["P2"],
        "integ_blocked": "P3",
        "integ_blocked_kind": "conflict",
        "integ_blocked_repo": "payments",
    }
    snap = data.build_snapshot(
        cfg, state, questions={"P1": "which auth scheme?", "P4": "check the migration"}
    )
    by_phase = {b.phase: b for b in snap.blockers}
    assert set(by_phase) == {"P1", "P2", "P3", "P4"}
    assert by_phase["P1"].kind == "waiting" and by_phase["P1"].deadline == BASE + 300
    assert by_phase["P1"].question == "which auth scheme?"
    assert by_phase["P2"].kind == "parked"
    assert "conflict" in by_phase["P3"].question and "payments" in by_phase["P3"].question
    assert by_phase["P4"].kind == "needs-owner"


def test_waiting_and_parked_phases_are_not_counted_ready(cfg):
    # A parked worker holds no slot but is still building; counting its phase as
    # ready would have the master relaunch a phase that is already in flight.
    state = {"slots": [], "done": {"P0": "ok"}, "parked": ["P1"], "waiting": {"P2": BASE}}
    snap = data.build_snapshot(cfg, state, graph=data.load_graph(cfg))
    assert snap.progress.next_up == []
    assert snap.progress.running == 2


def test_phase_progress_with_no_ledger_is_all_zero():
    assert data.phase_progress({}, {"P0": "ok"}, set(), set()).total == 0


def test_pid_alive_is_true_for_this_process_and_false_for_nothing():
    assert data.pid_alive(os.getpid()) is True
    assert data.pid_alive(None) is False
    assert data.pid_alive(0) is False


# -- notifications --------------------------------------------------------
def test_load_notifications_reads_the_fields_the_owner_asked_for(tmp_path):
    path = tmp_path / "notifications.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": BASE,
                "kind": "waiting",
                "phase": "P1",
                "source": "launch.waiting",
                "text": "which auth scheme?",
                "delivered": True,
                "error": "",
            }
        )
        + "\n"
        + json.dumps(
            {
                "ts": "2026-08-27T09:05:00",
                "kind": "done",
                "phase": "P2",
                "source": "launch.done",
                "text": "P2 FAILED",
                "delivered": False,
                "error": "curl: (28) timed out",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    notes = data.load_notifications(path)
    assert [n.phase for n in notes] == ["P1", "P2"]
    assert notes[0].delivered is True and notes[0].source == "launch.waiting"
    assert notes[1].delivered is False and "timed out" in notes[1].error
    assert notes[1].ts == pytest.approx(datetime(2026, 8, 27, 9, 5).timestamp())


def test_load_notifications_skips_a_half_written_trailing_line(tmp_path):
    path = tmp_path / "notifications.jsonl"
    path.write_text(
        json.dumps({"ts": BASE, "kind": "done", "text": "ok", "delivered": True})
        + '\n{"ts": 1, "kind": "do',
        encoding="utf-8",
    )
    assert len(data.load_notifications(path)) == 1


def test_load_notifications_missing_file_is_empty(tmp_path):
    assert data.load_notifications(tmp_path / "never-written.jsonl") == []


def test_a_notification_without_delivered_is_not_reported_as_delivered(tmp_path):
    path = tmp_path / "n.jsonl"
    path.write_text(json.dumps({"kind": "done", "text": "hi"}) + "\n", encoding="utf-8")
    assert data.load_notifications(path)[0].delivered is False


def test_question_index_prefers_the_latest_notification_over_the_sentinel():
    sentinels = {"P1": data.Sentinel("P1", "needs-owner", "look at the schema")}
    notes = [
        data.Notification(BASE, "waiting", "P1", "s", "first question", True, ""),
        data.Notification(BASE + 1, "waiting", "P1", "s", "second question", True, ""),
    ]
    assert data.question_index(notes, sentinels)["P1"] == "second question"


def test_question_index_falls_back_to_the_sentinel_note():
    sentinels = {"P1": data.Sentinel("P1", "needs-owner", "look at the schema")}
    assert data.question_index([], sentinels)["P1"] == "look at the schema"


# -- sentinels + recaps ---------------------------------------------------
def test_parse_sentinel_strips_the_phase_status_prefix_from_the_note():
    s = data.parse_sentinel("P1.ok", "P1 ok wired the resolver and added 4 tests\n")
    assert s.phase == "P1" and s.status == "ok"
    assert s.note == "wired the resolver and added 4 tests"


@pytest.mark.parametrize("name", ["P1", "P1.pending", ".P1.ok", "notes.txt"])
def test_parse_sentinel_rejects_a_non_sentinel_name(name):
    assert data.parse_sentinel(name, "body") is None or name.startswith(".")


def test_load_sentinels_prefers_a_completed_status_over_a_failed_one(cfg):
    (cfg.done_dir / "P1.fail").write_text("P1 fail first try blew up\n", encoding="utf-8")
    (cfg.done_dir / "P1.ok").write_text("P1 ok second try landed\n", encoding="utf-8")
    (cfg.done_dir / ".P1.ok.tmp").write_text("junk", encoding="utf-8")
    sentinels = data.load_sentinels(cfg.done_dir)
    assert set(sentinels) == {"P1"}
    assert sentinels["P1"].status == "ok" and "second try" in sentinels["P1"].note


def test_load_sentinels_on_a_missing_directory_is_empty(tmp_path):
    assert data.load_sentinels(tmp_path / "gone") == {}


def test_load_recap_reads_the_haiku_summary(cfg):
    recaps = cfg.state_dir / "recaps"
    recaps.mkdir()
    (recaps / "P1.json").write_text(
        json.dumps(
            {"phase": "P1", "status": "ok", "summary": "Added the ledger parser.", "ts": BASE}
        ),
        encoding="utf-8",
    )
    assert data.load_recap(recaps, "P1").summary == "Added the ledger parser."
    assert data.load_recaps(recaps)["P1"].status == "ok"


def test_load_recap_degrades_when_the_feature_has_not_written_anything(cfg):
    assert data.load_recap(cfg.state_dir / "recaps", "P1") is None
    assert data.load_recaps(cfg.state_dir / "recaps") == {}


def test_load_recap_on_malformed_json_is_none(cfg):
    recaps = cfg.state_dir / "recaps"
    recaps.mkdir()
    (recaps / "P1.json").write_text("{{{", encoding="utf-8")
    assert data.load_recap(recaps, "P1") is None
    assert data.load_recaps(recaps) == {}


def test_load_attempts_reads_the_jsonl_history_and_skips_junk(cfg):
    (cfg.done_dir / "P1.jsonl").write_text(
        json.dumps({"ts": BASE, "status": "fail", "note": "compile error"})
        + "\n\nnot json\n"
        + json.dumps({"ts": BASE + 100, "status": "ok", "note": "green"})
        + "\n",
        encoding="utf-8",
    )
    attempts = data.load_attempts(cfg.done_dir, "P1")
    assert [a["status"] for a in attempts] == ["fail", "ok"]


def test_load_attempts_missing_file_is_empty(cfg):
    assert data.load_attempts(cfg.done_dir, "nope") == []


# -- worker notes ---------------------------------------------------------
def test_load_notes_reads_the_decisions_a_worker_recorded_silently(cfg):
    notes_dir = cfg.state_dir / "notes"
    notes_dir.mkdir()
    (notes_dir / "P1.jsonl").write_text(
        json.dumps({"ts": BASE, "phase": "P1", "kind": "decision", "text": "kept the v1 route"})
        + "\n"
        + json.dumps({"ts": BASE + 5, "phase": "P1", "kind": "risk", "text": "untested on arm"})
        + "\n",
        encoding="utf-8",
    )
    notes = data.load_notes(notes_dir, "P1")
    assert [n.kind for n in notes] == ["decision", "risk"]
    assert notes[0].text == "kept the v1 route" and notes[0].ts == pytest.approx(BASE)
    assert list(data.load_all_notes(notes_dir)) == ["P1"]


def test_load_notes_keeps_what_parsed_when_the_last_line_is_torn(cfg):
    notes_dir = cfg.state_dir / "notes"
    notes_dir.mkdir()
    (notes_dir / "P1.jsonl").write_text(
        json.dumps({"phase": "P1", "text": "one"}) + '\n{"phase": "P1", "te', encoding="utf-8"
    )
    assert [n.text for n in data.load_notes(notes_dir, "P1")] == ["one"]


def test_load_notes_with_no_notes_directory_is_empty(cfg):
    assert data.load_notes(cfg.state_dir / "notes", "P1") == []
    assert data.load_all_notes(cfg.state_dir / "notes") == {}


def test_build_history_counts_notes_against_the_latest_run():
    events = data.parse_events(
        log_text((1, "LAUNCH P1 slot=0"), (50, "EVENT done P1 ok freed_slot=0 parked=False"))
    )
    runs = data.build_history(events, notes={"P1": [data.Note("P1", "decision", "kept v1")]})
    assert runs[0].notes == 1


# -- history --------------------------------------------------------------
def test_build_history_pairs_launches_with_dones_newest_first():
    events = data.parse_events(
        log_text(
            (0, "SUPERVISOR-START pid=1 driver=tmux"),
            (1, "LAUNCH P0 slot=0"),
            (2, "LAUNCH P1 slot=1"),
            (60, "EVENT done P0 ok freed_slot=0 parked=False"),
            (120, "EVENT done P1 fail freed_slot=1 parked=False"),
        )
    )
    runs = data.build_history(events)
    assert [r.phase for r in runs] == ["P1", "P0"]
    assert runs[1].status == "ok"
    assert runs[1].duration_s == pytest.approx(59, abs=0.01)


def test_build_history_gives_a_retried_phase_one_run_per_attempt():
    events = data.parse_events(
        log_text(
            (1, "LAUNCH P1 slot=0"),
            (50, "EVENT done P1 fail freed_slot=0 parked=False"),
            (60, "LAUNCH P1 slot=1"),
            (200, "EVENT done P1 ok freed_slot=1 parked=False"),
        )
    )
    runs = data.build_history(events)
    assert [r.status for r in runs] == ["ok", "fail"]


def test_build_history_marks_an_unfinished_run_as_running():
    events = data.parse_events(log_text((1, "LAUNCH P1 slot=0")))
    run = data.build_history(events)[0]
    assert run.running is True and run.ended_at is None and run.duration_s is not None


def test_build_history_includes_a_phase_that_only_has_a_sentinel(cfg):
    # A log that has rotated (or a state dir carried across a reboot) still knows
    # the phase finished, because the sentinel is the durable record.
    sentinels = {"P9": data.Sentinel("P9", "ok", "did the thing", mtime=BASE + 500)}
    runs = data.build_history([], sentinels)
    assert runs[0].phase == "P9" and runs[0].note == "did the thing"


def test_build_history_attaches_the_recap_and_note_to_the_last_run(cfg):
    events = data.parse_events(
        log_text(
            (1, "LAUNCH P1 slot=0"),
            (50, "EVENT done P1 fail freed_slot=0 parked=False"),
            (60, "LAUNCH P1 slot=1"),
            (200, "EVENT done P1 ok freed_slot=1 parked=False"),
        )
    )
    runs = data.build_history(
        events,
        {"P1": data.Sentinel("P1", "ok", "landed on the retry")},
        {"P1": data.Recap("P1", "ok", "Rewrote the parser.")},
    )
    assert runs[0].summary == "Rewrote the parser." and runs[0].note == "landed on the retry"
    assert runs[1].summary == "" and runs[1].note == ""


def test_phase_run_search_matches_any_field():
    run = data.PhaseRun("P1", "ok", BASE, BASE + 10, summary="Rewrote the parser.")
    assert run.matches("") and run.matches("parser") and run.matches("P1")
    assert not run.matches("telegram")


def test_launch_times_keeps_the_most_recent_launch():
    events = data.parse_events(log_text((1, "LAUNCH P1 slot=0"), (60, "LAUNCH P1 slot=1")))
    assert data.launch_times(events)["P1"] == pytest.approx(BASE + 60, abs=0.01)


def test_run_started_at_uses_the_latest_supervisor_start():
    events = data.parse_events(
        log_text((0, "SUPERVISOR-START pid=1 driver=tmux"), (900, "SUPERVISOR-START pid=2 driver=tmux"))
    )
    assert data.run_started_at(events) == pytest.approx(BASE + 900, abs=0.01)
    assert data.run_started_at([]) is None


# -- graph series ---------------------------------------------------------
def test_completions_series_is_cumulative_and_ignores_failures():
    events = data.parse_events(
        log_text(
            (10, "EVENT done P0 ok freed_slot=0 parked=False"),
            (20, "EVENT done P1 fail freed_slot=1 parked=False"),
            (30, "EVENT done P2 ok freed_slot=1 parked=False"),
        )
    )
    assert data.completions_series(events).values == [1.0, 2.0]


def test_occupancy_does_not_double_count_a_parked_worker():
    # PARK frees the slot; the EVENT done that follows carries freed_slot=None.
    # Decrementing on both would drive occupancy negative and understate
    # utilisation -- the exact number this dashboard exists to report.
    events = data.parse_events(
        log_text(
            (1, "LAUNCH P1 slot=0"),
            (2, "LAUNCH P2 slot=1"),
            (10, "PARK P1 slot=0"),
            (60, "EVENT done P1 ok freed_slot=None parked=True"),
            (70, "EVENT done P2 ok freed_slot=1 parked=False"),
        )
    )
    series = data.occupancy_series(events, max_workers=4)
    assert series.values == [1.0, 2.0, 1.0, 0.0]


def test_utilisation_is_time_weighted_not_a_point_average():
    # 4 slots busy for 10s, then 1 slot busy for 90s. The point average says
    # (4+1)/2/4 = 62%; the honest answer is (4*10 + 1*90) / (100*4) = 32%.
    series = data.Series("busy", [(BASE, 4.0), (BASE + 10, 1.0)])
    assert data.utilisation(series, 4, now=BASE + 100) == pytest.approx(0.325, abs=0.001)


def test_utilisation_with_no_history_is_zero():
    assert data.utilisation(data.Series("busy", []), 4) == 0.0
    assert data.utilisation(data.Series("busy", [(BASE, 2.0)]), 0) == 0.0


def test_phase_durations_are_longest_first_and_exclude_running_phases():
    runs = [
        data.PhaseRun("P1", "ok", BASE, BASE + 10),
        data.PhaseRun("P2", "ok", BASE, BASE + 300),
        data.PhaseRun("P3", None, BASE, None),
    ]
    assert data.phase_durations(runs) == [("P2", 300.0), ("P1", 10.0)]


def test_integration_holds_measure_blocked_to_resolved():
    events = data.parse_events(
        log_text(
            (100, "INTEGRATE-BLOCKED P1 conflict"),
            (400, "RESOLVED P1"),
            (500, "INTEGRATE-BLOCKED P2 dirty"),
        )
    )
    # P2 is still held, so it has no measurable duration yet.
    assert data.integration_holds(events) == [("P1", 300.0)]


def test_completion_density_buckets_by_local_hour():
    density = data.completion_density(
        data.parse_events(
            log_text(
                (0, "EVENT done P0 ok freed_slot=0 parked=False"),
                (60, "EVENT done P1 ok freed_slot=0 parked=False"),
                (3600 * 5, "EVENT done P2 ok freed_slot=0 parked=False"),
            )
        )
    )
    assert len(density) == 24
    assert density[9] == 2 and density[14] == 1


def test_series_from_an_empty_log_is_empty():
    assert data.completions_series([]).points == []
    assert data.occupancy_series([], 4).points == []
    assert data.integration_holds([]) == []
    assert data.completion_density([]) == [0] * 24


# -- rendering primitives -------------------------------------------------
def test_bar_uses_partial_blocks_so_small_progress_is_visible():
    assert data.bar(0, 30, 20) == "·" * 20
    assert data.bar(30, 30, 20) == "█" * 20
    small = data.bar(1, 30, 20)
    assert small != "·" * 20 and len(small) == 20


@pytest.mark.parametrize("maximum", [0, -1])
def test_bar_with_no_maximum_is_empty(maximum):
    assert data.bar(5, maximum, 10) == "·" * 10


def test_bar_clamps_an_overshoot():
    assert data.bar(99, 10, 8) == "█" * 8


def test_spark_scales_against_an_explicit_maximum():
    assert data.spark([0, 4], maximum=4) == "▁█"
    assert data.spark([]) == ""
    assert data.spark([2, 2, 2], maximum=None) == "▁▁▁"  # flat series, no false peak


def test_spark_downsamples_by_peak_so_spikes_survive():
    values = [0.0] * 40 + [9.0] + [0.0] * 39
    assert "█" in data.spark(values, width=10, maximum=9)


@pytest.mark.parametrize(
    "seconds,expected", [(None, "—"), (0, "0s"), (45, "45s"), (125, "2m05s"), (3725, "1h02m")]
)
def test_fmt_duration(seconds, expected):
    assert data.fmt_duration(seconds) == expected


def test_fmt_clock_and_stamp_handle_none():
    assert data.fmt_clock(None) == "—" and data.fmt_stamp(None) == "—"
    assert data.fmt_clock(BASE) == "09:00:00"


def test_fmt_ago_reads_as_elapsed():
    assert data.fmt_ago(BASE, now=BASE + 90) == "1m30s ago"


@pytest.mark.parametrize(
    "value,expected",
    [(BASE, BASE), (None, None), ("", None), ("nonsense", None), ("2026-08-27T09:00:00", BASE)],
)
def test_coerce_ts_accepts_epochs_and_iso_strings(value, expected):
    got = data.coerce_ts(value)
    assert got is None if expected is None else got == pytest.approx(expected)


# -- probes: pure parsing -------------------------------------------------
def test_parse_agents_reads_the_live_worker_status():
    payload = json.dumps(
        [
            {
                "pid": 4242,
                "cwd": "/state/wt/P1",
                "kind": "worker",
                "startedAt": "2026-08-27T09:00:00",
                "sessionId": "abc",
                "name": "worker:P1",
                "status": "waiting",
                "waitingFor": "which auth scheme?",
            }
        ]
    )
    agent = probes.parse_agents(payload)[0]
    assert agent.pid == 4242 and agent.status == "waiting"
    assert agent.waiting_for == "which auth scheme?"
    assert agent.started_at == pytest.approx(BASE)


def test_parse_agents_accepts_a_wrapped_payload():
    assert len(probes.parse_agents(json.dumps({"agents": [{"name": "worker:P1"}]}))) == 1


@pytest.mark.parametrize("bad", ["", "null", "not json", "[1, 2, 3]", '{"agents": 4}'])
def test_parse_agents_never_raises(bad):
    assert probes.parse_agents(bad) == [] or all(
        isinstance(a, probes.AgentInfo) for a in probes.parse_agents(bad)
    )


def test_match_agent_prefers_the_worktree_cwd():
    agents = [
        probes.AgentInfo(cwd="/state/wt/P1", name="worker:other"),
        probes.AgentInfo(cwd="/elsewhere", name="worker:P1"),
    ]
    assert probes.match_agent(agents, "P1", "/state/wt/P1").name == "worker:other"


def test_match_agent_falls_back_to_a_whole_token_name_match():
    # isolation=none puts every worker in the same cwd, so the name is the only
    # discriminator -- and a substring match would hand P1 the session for P10.
    agents = [probes.AgentInfo(cwd="/project", name="worker:P10")]
    assert probes.match_agent(agents, "P1", None) is None
    assert probes.match_agent(agents, "P10", None) is not None


def test_match_agent_with_nothing_running_is_none():
    assert probes.match_agent([], "P1", "/state/wt/P1") is None


def test_parse_panes_reads_the_title_and_running_command():
    text = (
        "%1\t✳ Swarm orchestrator step master\tnode\t@2\tworkers\t0\n"
        "%2\t\tbash\t@2\tworkers\t0\n"
        "\n"
    )
    panes = probes.parse_panes(text)
    assert panes["%1"].title.endswith("step master") and panes["%1"].alive is True
    assert panes["%1"].window_id == "@2"
    # A pane running `bash` is ALIVE. This assertion used to say the opposite,
    # on the theory that a slot which fell back to a shell had stalled — but
    # [worker].worker_cmd is configurable, so every project not launching a
    # binary literally called `claude` had all of its live workers reported
    # GONE, while the home panel (which decides liveness differently) called the
    # same slots healthy. A dashboard that contradicts itself is worse than one
    # that says nothing.
    assert panes["%2"].alive is True


def test_pane_is_dead_when_tmux_says_so_whatever_it_was_running():
    """Liveness comes from `#{pane_dead}`, not from guessing at the command.

    Only sufficient because tmux.harden sets `remain-on-exit on` before any pane
    exists: an exited command now leaves the pane in place and flagged, rather
    than vanishing or reverting to a shell.
    """
    panes = probes.parse_panes(
        "%1\tworker:A\tclaude\t@2\tworkers\t1\n"
        "%2\tworker:B\tbash\t@2\tworkers\t0\n"
    )
    assert panes["%1"].alive is False  # ran claude, but the command exited
    assert panes["%2"].alive is True  # a custom worker_cmd is not a stall


def test_parse_panes_tolerates_short_rows():
    assert probes.parse_panes("%1\ttitle\n")["%1"].command == ""


def test_parse_context_reads_the_status_bar_meter():
    used, total, pct = probes.parse_context("... 420k/1.0M  ctx")
    assert (used, total) == (420_000.0, 1_000_000.0) and pct == pytest.approx(42.0)


def test_parse_context_ignores_unsuffixed_ratios():
    # Pane text is full of innocent `1/2`-shaped tokens; without the k/M anchor
    # the context meter reads whichever one last scrolled past.
    assert probes.parse_context("running step 1/3 of the plan") is None
    assert probes.parse_context("") is None


def test_parse_context_takes_the_last_match_because_the_bar_is_at_the_bottom():
    text = "earlier 100k/1.0M\nlater 900k/1.0M\n"
    assert probes.parse_context(text)[0] == 900_000.0


def test_parse_rev_count_and_dirty():
    assert probes.parse_rev_count("7\n") == 7
    assert probes.parse_rev_count("") is None
    assert probes.parse_rev_count("fatal: bad revision") is None
    assert probes.parse_dirty(" M a.py\n?? b.py\n\n") == 2
    assert probes.parse_dirty("") == 0


def test_repo_stat_on_a_missing_worktree_is_blank():
    stat = probes.repo_stat("/definitely/not/a/worktree", "master")
    assert stat.commits is None and stat.dirty == 0
    assert probes.repo_stat(None, "master").commits is None


def test_a_missing_flag_is_not_reported_as_a_missing_command():
    # `swarm doctor --json` before --json exists is a completely different
    # situation from `swarm doctor` not existing: the first is worth retrying
    # bare, and collapsing them reports a working doctor as "not available".
    flag = probes.CommandResult(
        argv=["swarm", "doctor", "--json"],
        returncode=2,
        stderr="swarm doctor: error: unrecognized arguments: --json",
    )
    assert probes.looks_missing_flag(flag) is True
    assert probes.looks_unavailable(flag) is False
    missing = probes.CommandResult(returncode=2, stderr="invalid choice: 'doctor'")
    assert probes.looks_missing_flag(missing) is False
    assert probes.looks_missing_flag(probes.CommandResult(returncode=0)) is False


def test_looks_unavailable_recognises_an_argparse_missing_subcommand():
    missing = probes.CommandResult(
        argv=["swarm", "doctor"],
        returncode=2,
        stderr="swarm: error: argument command: invalid choice: 'doctor'",
    )
    assert probes.looks_unavailable(missing) is True
    real_failure = probes.CommandResult(argv=["swarm", "free"], returncode=1, stderr="boom")
    assert probes.looks_unavailable(real_failure) is False
    assert probes.looks_unavailable(probes.CommandResult(returncode=0)) is False


@pytest.mark.parametrize(
    "payload",
    [
        '[{"name": "tmux", "status": "ok", "detail": "server up"}]',
        '{"checks": [{"check": "tmux", "level": "ok", "message": "server up"}]}',
        '[{"name": "tmux", "ok": true, "hint": "server up"}]',
    ],
)
def test_parse_doctor_accepts_the_plausible_shapes(payload):
    checks = probes.parse_doctor(payload)
    assert checks[0]["name"] == "tmux" and checks[0]["status"] == "ok"


@pytest.mark.parametrize("bad", ["", "not json", "3", '{"checks": "nope"}'])
def test_parse_doctor_never_raises(bad):
    assert probes.parse_doctor(bad) == []


# -- the "never raises" sweep ---------------------------------------------
def test_every_loader_survives_a_state_dir_that_does_not_exist(tmp_path, monkeypatch):
    """The state dir is missing until the first `swarm up`; nothing may raise."""
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "never-created"))
    monkeypatch.setenv("SWARM_SLUG", "test")
    cfg = load_config(project_dir=str(project))
    assert data.load_graph(cfg) == {}
    assert data.load_sentinels(cfg.done_dir) == {}
    assert data.load_recaps(cfg.state_dir / "recaps") == {}
    assert data.load_notifications(cfg.state_dir / "notifications.jsonl") == []
    assert data.LogTail(cfg.supervisor_log).poll() == []
    snap = data.build_snapshot(cfg, data.read_state(cfg))
    assert snap.ok is False
    assert data.build_history([], {}, {}, done_dir=cfg.done_dir) == []
    assert data.load_all_notes(cfg.state_dir / "notes") == {}


def test_every_loader_survives_files_full_of_garbage(cfg):
    cfg.supervisor_log.write_text("\x00\x01 not a log\n\n???\n", encoding="utf-8")
    (cfg.state_dir / "notifications.jsonl").write_text("[]\n{}\nnope\n", encoding="utf-8")
    (cfg.done_dir / "P1.ok").write_text("", encoding="utf-8")
    (cfg.done_dir / "junk").write_text("x", encoding="utf-8")
    (cfg.state_dir / "recaps").mkdir()
    (cfg.state_dir / "recaps" / "P1.json").write_text("[]", encoding="utf-8")
    cfg.state_path.write_text("[]", encoding="utf-8")

    tail = data.LogTail(cfg.supervisor_log)
    tail.poll()
    sentinels = data.load_sentinels(cfg.done_dir)
    notes = data.load_notifications(cfg.state_dir / "notifications.jsonl")
    snap = data.build_snapshot(cfg, data.read_state(cfg), questions=data.question_index(notes, sentinels))
    assert snap.ok is False  # a JSON list is not a state object
    assert data.build_history(tail.events, sentinels, data.load_recaps(cfg.state_dir / "recaps"))
    assert data.occupancy_series(tail.events, 4).points == []


# -- Textual harness smoke tests ------------------------------------------
def _boot(cfg, keys: list[str], capfd, monkeypatch) -> None:
    """Boot the real app headlessly and press ``keys``. Raises on any UI error.

    Two harness quirks are worked around here. Textual's headless driver writes
    to the real stdout and deadlocks under pytest's fd capture, so capture is
    suspended for the duration. And the run is bounded by ``wait_for`` so a
    future regression that wedges the event loop fails the suite in 30s instead
    of hanging CI forever.
    """
    from swarm_orchestrator.tui.app import SwarmApp
    from swarm_orchestrator.tui.dash import Dash  # noqa: F401 - boot smoke

    monkeypatch.setattr(Dash, "probe", lambda self: None)  # no claude/tmux/git here
    app = SwarmApp(cfg)

    async def drive() -> None:
        async with app.run_test() as pilot:
            for key in keys:
                await pilot.press(key)
            await pilot.pause()

    with capfd.disabled():
        asyncio.run(asyncio.wait_for(drive(), timeout=30))


def test_app_boots_against_a_run_that_never_started(cfg, capfd, monkeypatch):
    _boot(
        cfg,
        ["1", "2", "3", "4", "5", "6", "7", "question_mark", "escape"],
        capfd,
        monkeypatch,
    )


def test_app_boots_against_a_populated_state_dir(cfg, capfd, monkeypatch):
    cfg.state_path.write_text(
        json.dumps(
            {
                "slots": [{"id": 0, "busy": True, "phase": "P1", "pane_id": "%1"}],
                "done": {"P0": "ok"},
                "waiting": {"P2": time.time() + 60},
                "integ_queue": ["P0"],
                "supervisor_pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )
    cfg.supervisor_log.write_text(
        log_text(
            (0, "SUPERVISOR-START pid=1 driver=tmux"),
            (1, "LAUNCH P0 slot=0"),
            (60, "EVENT done P0 ok freed_slot=0 parked=False"),
            (61, "LAUNCH P1 slot=0"),
        ),
        encoding="utf-8",
    )
    (cfg.done_dir / "P0.ok").write_text("P0 ok wrote the parser\n", encoding="utf-8")
    (cfg.state_dir / "notifications.jsonl").write_text(
        json.dumps(
            {
                "ts": BASE,
                "kind": "waiting",
                "phase": "P2",
                "source": "launch.waiting",
                "text": "which auth scheme?",
                "delivered": False,
                "error": "timeout",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _boot(cfg, ["2", "j", "3", "slash", "escape", "4", "F", "5", "G", "1"], capfd, monkeypatch)
