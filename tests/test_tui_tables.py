"""Tests for the three data tabs' row- and detail-building.

Only the pure half is tested, deliberately. The widgets are Textual's problem;
what is *this* project's problem is that every value on the screen is derived
from a file some other process is appending to right now, and that a single
malformed record must cost one row rather than the owner's only view of the run.

So the cases here are the awkward ones: a slot whose pane died, a slot row that
grew (or lost) a member, a phase run with no timestamps at all, a notification
that never got delivered, a recap containing Rich markup, and every builder
pointed at nothing. The assertions check semantics — is the failure red, is the
recap there — not exact layout, so a spacing tweak does not break the suite.
The plain-text charts moved to ``tests/test_tui_charts.py`` with their module.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

import pytest

from swarm_orchestrator.tui import tables
from swarm_orchestrator.tui.data import Meter, Note, Notification, PhaseRun, Recap, SlotView
from swarm_orchestrator.tui.theme import COLOR, BAD, MUTED, OK, WARN

BASE = datetime(2026, 8, 27, 9, 0, 0).timestamp()
MUTED_HEX = COLOR[MUTED]


def slot(**kwargs) -> SlotView:
    """A busy slot on ``dash-W7``, overridable field by field."""
    defaults = dict(
        id=2,
        busy=True,
        phase="dash-W7",
        pane_id="%14",
        branch="swarm/dash-W7",
        worktree="/tmp/wt/dash-W7",
        started_at=time.time() - 300,
    )
    defaults.update(kwargs)
    return SlotView(**defaults)


class FakeRepo:
    def __init__(self, commits=3, dirty=0):
        self.commits = commits
        self.dirty = dirty


class FakeDash:
    """Just the attributes the detail panes read."""

    def __init__(self, tmp_path: Path, **kwargs):
        self.recaps = kwargs.get("recaps", {})
        self.notes = kwargs.get("notes", {})
        self.tails = kwargs.get("tails", {})
        self.repos = kwargs.get("repos", {})
        self.history = kwargs.get("history", [])
        self.notifications = kwargs.get("notifications", [])
        self.notes_dir = tmp_path / "notes"
        self.cfg = type("Cfg", (), {"done_dir": tmp_path / "done", "max_workers": 4})()
        self.notifications_path = tmp_path / "notifications.jsonl"


# -- text helpers ----------------------------------------------------------
def test_clip_collapses_and_ellipsises():
    assert tables.clip("  a   b\nc ", 40) == "a b c"
    assert tables.clip("x" * 30, 10) == "x" * 9 + "…"
    assert tables.clip(None, 10) == ""
    assert tables.clip("anything", 0) == ""


def test_cell_escapes_worker_written_markup():
    """A recap saying `[skip]` must not be read as a colour tag by DataTable."""
    out = tables.cell("done [skip] the rest", 40)
    assert "\\[skip]" in out


def test_unique_keys_only_suffixes_collisions():
    assert tables.unique_keys(["a", "b", "a", "a"]) == ["a", "b", "a#1", "a#2"]
    assert tables.unique_keys([]) == []


# -- workers ---------------------------------------------------------------
def test_worker_row_has_one_cell_per_column():
    row = tables.worker_row((slot(), "busy", "", 42.0, None), FakeRepo())
    assert len(row) == len(tables.WORKER_COLUMNS)
    assert "dash-W7" in row[2]
    assert "busy" in row[3]


def test_worker_row_gone_is_unmissable():
    """A dead pane is the failure that looks healthy everywhere else."""
    row = tables.worker_row((slot(), "gone", "", None, None))
    assert "✖" in row[0]
    assert "GONE" in row[3]
    assert COLOR[BAD] in row[0] and COLOR[BAD] in row[2] and COLOR[BAD] in row[3]


def col(row, name: str) -> str:
    """A worker row's cell by column name, so a new column cannot shift the asserts."""
    return row[[n for n, _ in tables.WORKER_COLUMNS].index(name)]


def test_worker_row_context_colours_by_pressure():
    """A context window at 95% is an emergency; at 20% it is not."""
    hot = tables.worker_row((slot(), "busy", "", 95.0, None))
    cool = tables.worker_row((slot(), "busy", "", 20.0, None))
    assert COLOR[BAD] in col(hot, "context")
    assert COLOR[OK] in col(cool, "context")


def test_measured_context_is_judged_against_the_budget_not_the_window():
    """420k of a 1M window is 42% — fine by the window, over budget by the audit."""
    m = Meter(phase="dash-W7", ts=0.0, context_tokens=420_000, context_window=1_000_000)
    cell = col(tables.worker_row((slot(), "busy", "", 42.0, None), meter=m), "context")
    assert "420k" in cell and COLOR[BAD] in cell


def test_worker_row_eta_flags_a_phase_past_the_typical_one():
    done = [PhaseRun(phase=f"P{i}", status="ok", started_at=0.0, ended_at=3600.0) for i in range(3)]
    fresh = col(tables.worker_row((slot(started_at=time.time() - 600), "busy", "", None, None),
                                  history=done), "eta")
    late = col(tables.worker_row((slot(started_at=time.time() - 7200), "busy", "", None, None),
                                 history=done), "eta")
    assert "left" in fresh and COLOR[WARN] not in fresh
    assert "over" in late and COLOR[WARN] in late


def test_worker_row_idle_slot_shows_nothing_rather_than_stale_values():
    row = tables.worker_row((slot(busy=False, phase=None, started_at=None), "idle", "", None, None))
    assert "—" in row[2] and MUTED_HEX in row[2]
    assert "—" in col(row, "elapsed") and "—" in col(row, "eta") and "—" in col(row, "context")


def test_worker_row_dirty_files_are_warned_commits_are_not():
    row = tables.worker_row((slot(), "busy", "", None, None), FakeRepo(commits=2, dirty=7))
    assert "2" in col(row, "+")
    assert "7" in col(row, "~") and COLOR[WARN] in col(row, "~")


def test_elapsed_state_thresholds():
    assert tables.elapsed_state(None) is None
    assert tables.elapsed_state(60.0) is None
    assert tables.elapsed_state(tables.STALE_ELAPSED_S) == WARN
    assert tables.elapsed_state(tables.DEAD_ELAPSED_S) == BAD


@pytest.mark.parametrize(
    "entry",
    [
        (),                                  # empty
        (slot(),),                           # producer dropped every extra member
        (slot(), "busy"),                    # the historic 2-tuple
        (slot(), "busy", "", 10.0, None, "future field"),  # producer grew one
        (None, None, None, None, None),      # torn: no slot at all
        ("not a tuple",),
    ],
)
def test_worker_row_survives_a_torn_slot_row(entry):
    """The slot_rows() tuple has grown a member before and will again."""
    row = tables.worker_row(entry)
    assert len(row) == len(tables.WORKER_COLUMNS)


def test_worker_key_is_the_slot_number_not_the_phase():
    """A slot that swaps phases must keep the cursor, not jump."""
    assert tables.worker_key((slot(phase="a"), "busy")) == tables.worker_key(
        (slot(phase="b"), "idle")
    )


def test_worker_detail_reports_a_dead_pane_in_words(tmp_path):
    dash = FakeDash(tmp_path, tails={"%14": "line one\nline two\n"})
    out = tables.worker_detail((slot(), "gone", "", None, None), dash)
    assert "PANE GONE" in out
    assert COLOR[BAD] in out


def test_worker_detail_joins_worktree_recap_notes_and_pane(tmp_path):
    dash = FakeDash(
        tmp_path,
        recaps={"dash-W7": Recap(phase="dash-W7", summary="rewrote the four tabs")},
        notes={"dash-W7": [Note(phase="dash-W7", kind="risk", text="skipped the migration")]},
        tails={"%14": "compiling…\ndone\n"},
        repos={"dash-W7": FakeRepo(commits=4, dirty=2)},
    )
    out = tables.worker_detail((slot(), "busy", "", 62.0, None), dash)
    assert "/tmp/wt/dash-W7" in out
    assert "swarm/dash-W7" in out
    assert "rewrote the four tabs" in out
    assert "skipped the migration" in out
    assert "compiling" in out
    assert "4 commit(s), 2 dirty file(s)" in out


def test_worker_detail_of_a_free_slot_says_so(tmp_path):
    out = tables.worker_detail((slot(busy=False, phase=None), "idle", "", None, None),
                               FakeDash(tmp_path))
    assert "free" in out


# -- history ---------------------------------------------------------------
def test_history_row_has_one_cell_per_column_and_a_campaign():
    run = PhaseRun(
        phase="dash-W7",
        status="ok",
        started_at=BASE,
        ended_at=BASE + 600,
        summary="rebuilt the tables",
    )
    row = tables.history_row(run)
    assert len(row) == len(tables.HISTORY_COLUMNS)
    assert "dash" in row[2]
    assert "ok" in row[3]
    assert "rebuilt the tables" in row[6]


def test_history_row_marks_a_run_still_in_flight():
    row = tables.history_row(PhaseRun(phase="P1", started_at=time.time() - 60))
    assert "running" in row[3]


def test_history_row_falls_back_to_the_sentinel_note_then_says_nothing():
    with_note = tables.history_row(PhaseRun(phase="P1", status="ok", note="wrote the parser"))
    assert "wrote the parser" in with_note[6]
    bare = tables.history_row(PhaseRun(phase="P1", status="ok"))
    assert "no recap" in bare[6] and COLOR[MUTED] in bare[6]


def test_history_row_survives_a_run_with_no_times_or_status():
    row = tables.history_row(PhaseRun(phase=""))
    assert len(row) == len(tables.HISTORY_COLUMNS)


def test_history_key_survives_the_table_reordering():
    """Newest-first means every completion shifts the rows; the key must not."""
    run = PhaseRun(phase="dash-W7", started_at=BASE, ended_at=BASE + 10)
    later = PhaseRun(phase="dash-W7", started_at=BASE, ended_at=BASE + 900, status="ok")
    assert tables.history_key(run) == tables.history_key(later)
    assert tables.history_key(PhaseRun(phase="dash-W8", started_at=BASE)) != tables.history_key(run)


def test_history_key_handles_a_run_with_no_timestamps():
    assert tables.history_key(PhaseRun(phase="P1")) == "P1@0"


def test_history_detail_reads_back_notes_and_attempts(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "P1.jsonl").write_text(
        json.dumps({"phase": "P1", "kind": "assumption", "text": "assumed utf-8"}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "done").mkdir()
    (tmp_path / "done" / "P1.jsonl").write_text(
        json.dumps({"ts": BASE, "status": "fail", "note": "flaky test"}) + "\n"
        + json.dumps({"ts": BASE + 60, "status": "ok", "note": "green"}) + "\n",
        encoding="utf-8",
    )
    run = PhaseRun(phase="P1", status="ok", started_at=BASE, ended_at=BASE + 120,
                   summary="did the thing", note="left the flag off")
    out = tables.history_detail(run, FakeDash(tmp_path))
    assert "did the thing" in out
    assert "left the flag off" in out
    assert "assumed utf-8" in out
    assert "flaky test" in out and "green" in out
    assert "attempts (2)" in out


def test_history_detail_with_nothing_on_disk_says_where_a_recap_comes_from(tmp_path):
    out = tables.history_detail(PhaseRun(phase="P9", status="ok"), FakeDash(tmp_path))
    assert "no recap on disk" in out


# -- notifications ---------------------------------------------------------
def note(**kwargs) -> Notification:
    defaults = dict(
        ts=BASE,
        kind="waiting",
        phase="P1",
        source="supervisor.wait",
        text="which schema should I use?",
        delivered=True,
        error="",
    )
    defaults.update(kwargs)
    return Notification(**defaults)


def test_notification_row_has_one_cell_per_column():
    row = tables.notification_row(note())
    assert len(row) == len(tables.NOTIFICATION_COLUMNS)
    assert "✓" in row[0] and COLOR[OK] in row[0]
    assert "supervisor.wait" in row[4]


def test_notification_row_shouts_about_a_dropped_ping():
    """A ping that never landed is how an unattended run goes wrong in silence."""
    row = tables.notification_row(note(delivered=False, error="telegram 429"))
    assert "✗" in row[0]
    assert COLOR[BAD] in row[0] and COLOR[BAD] in row[2] and COLOR[BAD] in row[5]


def test_notification_row_survives_an_empty_record():
    row = tables.notification_row(Notification(ts=None, kind="", phase=None, source="",
                                               text="", delivered=False, error=""))
    assert len(row) == len(tables.NOTIFICATION_COLUMNS)
    assert "unknown" in row[4]


def test_notification_matches_searches_every_worded_column():
    n = note()
    assert tables.notification_matches(n, "")
    assert tables.notification_matches(n, "SCHEMA")
    assert tables.notification_matches(n, "supervisor")
    assert tables.notification_matches(n, "p1")
    assert not tables.notification_matches(n, "nothing-like-this")
    assert tables.notification_matches(note(error="429"), "429")


def test_notification_detail_leads_with_the_failure():
    out = tables.notification_detail(note(delivered=False, error="telegram 429"))
    assert out.splitlines()[0].count("NOT DELIVERED")
    assert "telegram 429" in out
    assert "which schema should I use?" in out


def test_notification_detail_of_a_delivered_ping():
    out = tables.notification_detail(note())
    assert "delivered" in out
    assert "supervisor.wait" in out


# -- markup safety ---------------------------------------------------------
MARKUP_TAG = re.compile(r"(?<!\\)\[([^\]/][^\]]*)\]")
VALID_TAGS = set(COLOR.values()) | {"bold", "dim"}


def test_builders_only_ever_emit_real_colours(tmp_path):
    """A semantic token is not a style name.

    ``paint(x, MUTED)`` resolves the token to a hex; an f-string writing
    ``[{MUTED}]`` emits the literal tag ``[muted]``, which Rich cannot resolve —
    the text still appears, so the mistake is invisible until someone notices the
    dashboard is monochrome. This walks every builder's output and insists each
    tag is a colour the theme actually defines.
    """
    dash = FakeDash(
        tmp_path,
        recaps={"dash-W7": Recap(phase="dash-W7", summary="did it")},
        notes={"dash-W7": [Note(phase="dash-W7", kind="risk", text="a call", ts=BASE)]},
        tails={"%14": "output line\n"},
        repos={"dash-W7": FakeRepo()},
    )
    run = PhaseRun(phase="dash-W7", status="ok", started_at=BASE, ended_at=BASE + 60,
                   summary="did it", note="and this", slot="0")
    samples: list[str] = [
        *tables.worker_row((slot(), "gone", "", 91.0, None), FakeRepo(dirty=3)),
        *tables.history_row(run),
        *tables.notification_row(note(delivered=False, error="boom")),
        tables.worker_detail((slot(), "gone", "waiting on you", 91.0, None), dash),
        tables.history_detail(run, dash),
        tables.notification_detail(note(delivered=False, error="boom")),
    ]
    for text in samples:
        for tag in MARKUP_TAG.findall(text):
            assert tag in VALID_TAGS, f"{tag!r} is not a colour the theme defines"


# -- fitting columns to the width ----------------------------------------------
def _need(widths, shown, flex=None, flex_w=None, pad=tables.CELL_PAD):
    return sum((flex_w if i == flex else widths[i]) + pad for i in shown)


def test_fit_columns_keeps_everything_when_it_fits():
    shown, flex_w = tables.fit_columns([4, 10, 6], [0, 1, 2], 100)
    assert shown == (0, 1, 2) and flex_w is None


def test_fit_columns_drops_the_least_useful_first_and_never_a_zero():
    widths, prios = [4, 10, 6, 8], [0, 1, 2, 2]
    # 2+8 dropped first (rightmost of the priority-2 pair), then 6, then 10.
    assert tables.fit_columns(widths, prios, 33)[0] == (0, 1, 2)
    assert tables.fit_columns(widths, prios, 20)[0] == (0, 1)
    assert tables.fit_columns(widths, prios, 3)[0] == (0,)  # 0 stays even if it cannot fit


def test_fit_columns_gives_the_flex_column_every_cell_left_over():
    widths, prios = [2, 20, 11, 58], [0, 0, 0, 0]
    shown, flex_w = tables.fit_columns(widths, prios, 120, flex=3)
    assert shown == (0, 1, 2, 3)
    assert _need(widths, shown, 3, flex_w) == 120


def test_fit_columns_drops_before_starving_the_prose_column():
    widths, prios = [2, 20, 12, 11, 13, 8, 58], list(tables.HISTORY_PRIORITY)
    shown, flex_w = tables.fit_columns(widths, prios, 76, flex=6, flex_min=16)
    assert 6 in shown and 1 in shown and 3 in shown
    assert 2 not in shown  # campaign goes first
    assert flex_w >= 16 and _need(widths, shown, 6, flex_w) <= 76


@pytest.mark.parametrize("avail", [60, 76, 96, 136])
def test_every_table_degrades_to_fit_without_losing_its_key_columns(avail):
    for columns, prios, flex in (
        (tables.WORKER_COLUMNS, tables.WORKER_PRIORITY, None),
        (tables.HISTORY_COLUMNS, tables.HISTORY_PRIORITY, tables.HISTORY_FLEX),
        (tables.NOTIFICATION_COLUMNS, tables.NOTIFICATION_PRIORITY, tables.NOTIFICATION_FLEX),
        (tables.RUN_COLUMNS, tables.RUN_PRIORITY, None),
    ):
        assert len(prios) == len(columns)
        widths = [w for _, w in columns]
        shown, flex_w = tables.fit_columns(widths, prios, avail, flex, 16)
        assert all(i in shown for i, p in enumerate(prios) if p == 0)
        assert _need(widths, shown, flex, flex_w) <= avail
