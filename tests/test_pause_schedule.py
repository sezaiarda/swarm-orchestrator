"""``swarm pause --in 12h`` / ``--at HH:MM``: a pause that happens later.

What is load-bearing here:

* the schedule is one timestamp in ``state.json``: a new one replaces it, and it
  survives ``swarm up``;
* the supervisor sets ``paused`` at or after the moment, never before, and its
  wake-up counts the moment in, so it fires on time rather than on a sweep;
* one that came due while the supervisor was down fires on its first wake;
* ``swarm pause --cancel`` and ``swarm resume`` both drop it;
* ``swarm status`` and the dashboard say when it happens and how to drop it.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

import pytest

from swarm_orchestrator import pauseat
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.supervisor import Supervisor
from swarm_orchestrator.tui import data, home
from test_deps import _bare_cfg

LEDGER = "- [ ] `P0` · needs:—\n- [ ] `P1` · needs:—\n"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = _bare_cfg(tmp_path, monkeypatch, LEDGER)
    state_mod.init_state(c)
    return c


@pytest.fixture
def sup(cfg):
    s = Supervisor(cfg)
    yield s
    s.log.close()


def _swarm(cfg, *args: str) -> int:
    return cli_main(["--project-dir", str(cfg.project_dir), *args])


def _log(cfg) -> str:
    return cfg.supervisor_log.read_text() if cfg.supervisor_log.is_file() else ""


# -- parsing --------------------------------------------------------------------------
@pytest.mark.parametrize("text,seconds", [
    ("12h", 12 * 3600), ("90m", 90 * 60), ("1h30m", 5400), ("2d", 2 * 86400),
    ("1d12h", 36 * 3600), (" 45M ", 45 * 60),
])
def test_durations_parse(text, seconds):
    assert pauseat.parse_in(text) == seconds


@pytest.mark.parametrize("text", ["", "12", "h", "12x", "1m30h", "-5m", "1.5h", "0h", "0m"])
def test_bad_durations_are_refused_in_words(text):
    with pytest.raises(ValueError, match=r"12h|swarm pause"):
        pauseat.parse_in(text)


def test_at_is_the_next_such_local_time():
    now = datetime(2026, 9, 28, 14, 30).timestamp()
    assert datetime.fromtimestamp(pauseat.next_at("16:00", now)) == datetime(2026, 9, 28, 16, 0)
    assert datetime.fromtimestamp(pauseat.next_at("3:05", now)) == datetime(2026, 9, 29, 3, 5)
    assert datetime.fromtimestamp(pauseat.next_at("14:30", now)) == datetime(2026, 9, 29, 14, 30)


@pytest.mark.parametrize("text", ["24:00", "12:60", "noon", "1200", "12:5"])
def test_bad_times_are_refused(text):
    with pytest.raises(ValueError, match="not a time of day"):
        pauseat.next_at(text, time.time())


def test_the_line_is_plain_english():
    now = datetime(2026, 9, 28, 15, 14).timestamp()
    at = datetime(2026, 9, 29, 3, 12).timestamp()
    assert pauseat.line(at, now) == (
        "pause scheduled: Tue 03:12 (in 11h 58m) — swarm pause --cancel to drop it")
    assert pauseat.line(now + 7 * 60, now).endswith("(in 7m) — swarm pause --cancel to drop it")
    assert "(in 2d 0h)" in pauseat.line(now + 2 * 86400, now)
    assert "(due now)" in pauseat.line(now - 5, now)
    assert pauseat.line(0.0, now) == ""


# -- the CLI --------------------------------------------------------------------------
def test_pause_in_persists_the_schedule_and_does_not_pause(cfg, capsys):
    before = time.time()
    assert _swarm(cfg, "pause", "--in", "12h") == 0
    st = state_mod.read(cfg)
    assert not st.paused
    assert before + 12 * 3600 <= st.pause_at <= time.time() + 12 * 3600
    out = capsys.readouterr().out
    assert "swarm pauses at" in out and "swarm pause --cancel" in out
    assert re.search(r"\(in (12h 0m|11h 59m)\)", out)
    assert "PAUSE-SCHEDULED at=" in _log(cfg) and "in=43200s" in _log(cfg)


def test_pause_at_schedules_the_next_such_time(cfg):
    assert _swarm(cfg, "pause", "--at", "03:00") == 0
    at = datetime.fromtimestamp(state_mod.read(cfg).pause_at)
    assert (at.hour, at.minute) == (3, 0) and 0 < at.timestamp() - time.time() <= 86400


def test_a_new_schedule_replaces_the_old(cfg, capsys):
    _swarm(cfg, "pause", "--in", "12h")
    first = state_mod.read(cfg).pause_at
    assert _swarm(cfg, "pause", "--in", "30m") == 0
    st = state_mod.read(cfg)
    assert st.pause_at < first and st.pause_at - time.time() <= 1800
    assert "this replaces the pause that was scheduled for" in capsys.readouterr().out
    assert f"replaces={pauseat.stamp(first)}" in _log(cfg)


def test_a_bad_duration_changes_nothing(cfg, capsys):
    assert _swarm(cfg, "pause", "--in", "soon") == 2
    assert "not a duration" in capsys.readouterr().err
    assert state_mod.read(cfg).pause_at == 0.0


def test_in_and_at_are_one_or_the_other(cfg):
    with pytest.raises(SystemExit):
        _swarm(cfg, "pause", "--in", "1h", "--at", "03:00")


def test_plain_pause_still_pauses_now_and_leaves_a_schedule_alone(cfg):
    _swarm(cfg, "pause", "--in", "1h")
    at = state_mod.read(cfg).pause_at
    assert _swarm(cfg, "pause") == 0
    st = state_mod.read(cfg)
    assert st.paused and st.pause_at == at


def test_cancel_drops_the_schedule(cfg, capsys):
    _swarm(cfg, "pause", "--in", "1h")
    at = state_mod.read(cfg).pause_at
    capsys.readouterr()
    assert _swarm(cfg, "pause", "--cancel") == 0
    st = state_mod.read(cfg)
    assert st.pause_at == 0.0 and not st.paused
    assert "cancelled — launching carries on" in capsys.readouterr().out
    assert f"PAUSE-SCHEDULE-CANCELLED at={pauseat.stamp(at)} by=cancel" in _log(cfg)
    assert _swarm(cfg, "pause", "--cancel") == 0
    assert "no pause is scheduled" in capsys.readouterr().out


def test_resume_drops_the_schedule_too(cfg, capsys):
    _swarm(cfg, "pause", "--in", "1h")
    _swarm(cfg, "pause")
    capsys.readouterr()
    assert _swarm(cfg, "resume") == 0
    st = state_mod.read(cfg)
    assert st.pause_at == 0.0 and not st.paused
    assert "scheduled pause at" in capsys.readouterr().out
    assert "PAUSE-SCHEDULE-CANCELLED" in _log(cfg) and "by=resume" in _log(cfg)


def test_the_schedule_survives_up(cfg):
    _swarm(cfg, "pause", "--in", "2h")
    at = state_mod.read(cfg).pause_at
    state_mod.init_state(cfg)
    assert state_mod.read(cfg).pause_at == at


def test_status_shows_it(cfg, capsys):
    _swarm(cfg, "pause", "--in", "12h")
    capsys.readouterr()
    assert _swarm(cfg, "status") == 0
    out = capsys.readouterr().out
    assert re.search(r"pause scheduled: .* \(in (12h 0m|11h 59m)\) — swarm pause --cancel to drop it",
                     out)
    assert _swarm(cfg, "status", "--json") == 0
    data_ = json.loads(capsys.readouterr().out)
    assert data_["pause_at"] > 0 and data_["pause_line"].startswith("pause scheduled: ")


# -- the supervisor -------------------------------------------------------------------
def _schedule(cfg, at: float) -> None:
    with state_mod.transaction(cfg) as st:
        st.pause_at = at


def test_it_fires_at_the_moment_and_not_before(sup, cfg, monkeypatch):
    at = time.time() + 600
    _schedule(cfg, at)
    clock = [at - 1]
    monkeypatch.setattr("swarm_orchestrator.supervisor.time.time", lambda: clock[0])
    sup._pause_at_tick()
    st = state_mod.read(cfg)
    assert not st.paused and st.pause_at == at
    clock[0] = at
    sup._pause_at_tick()
    st = state_mod.read(cfg)
    assert st.paused and st.pause_at == 0.0
    assert f"PAUSE-SCHEDULED-FIRED due={pauseat.stamp(at)} late=0s" in _log(cfg)


def test_a_fired_pause_launches_nothing(sup, cfg):
    _schedule(cfg, time.time() - 1)
    sup._pause_at_tick()
    assert sup._fill_slots("test") == []
    assert getattr(sup, "stub_launches", []) == []


def test_the_wake_up_counts_the_moment_in(sup, cfg):
    """Not the 300 s watchdog sweep: the select timeout ends at the moment."""
    sup._last_sweep = time.time()
    _schedule(cfg, time.time() + 42)
    assert 40 < sup._select_timeout() <= 42
    _schedule(cfg, time.time() - 3600)  # due while the supervisor was down
    assert sup._select_timeout() == 0.0


def test_a_past_due_schedule_fires_as_the_supervisor_comes_up(swarm):
    swarm.state_dir.mkdir(parents=True, exist_ok=True)
    (swarm.state_dir / "state.json").write_text(json.dumps({"pause_at": time.time() - 3600}))
    swarm.up()
    assert swarm.wait(lambda: "PAUSE-SCHEDULED-FIRED" in swarm.log_text(), timeout=20), (
        swarm.log_text())
    st = swarm.state()
    assert st["paused"] and st["pause_at"] == 0.0
    assert swarm.wait(lambda: "MASTER-IDLE paused" in swarm.log_text(), timeout=20), (
        swarm.log_text())
    assert swarm.busy_phases() == [] and "LAUNCH-READY" not in swarm.log_text()


def test_a_running_supervisor_fires_on_time(swarm):
    """Scheduled through the CLI's own path (state + poke), due in two seconds:
    it fires within a second or two, long before any sweep."""
    swarm.env["FAKE_WORKER_PARK"] = "1"
    swarm.up()
    assert swarm.wait(lambda: swarm.busy_phases() == ["P0"], timeout=20), swarm.log_text()
    raw = swarm.state()
    raw["pause_at"] = time.time() + 2
    (swarm.state_dir / "state.json").write_text(json.dumps(raw))
    with open(swarm.state_dir / "control.fifo", "w") as fifo:
        fifo.write("pause-scheduled\n")
    assert swarm.wait(lambda: "PAUSE-SCHEDULED-FIRED" in swarm.log_text(), timeout=6), (
        swarm.log_text())
    assert swarm.state()["paused"]
    swarm.cli("done", "P0", "ok")
    assert swarm.wait(lambda: swarm.busy_phases() == [], timeout=10), swarm.log_text()
    time.sleep(1.5)
    assert swarm.busy_phases() == []  # paused: P1 never launched


# -- the dashboard --------------------------------------------------------------------
class _Dash:
    def __init__(self, snap):
        self.snapshot = snap
        self.notifications = []


def test_the_dashboard_footer_shows_it(cfg):
    now = time.time()
    snap = data.Snapshot(ok=True, supervisor_pid=1, supervisor_alive=True,
                         last_event_at=now, pause_at=now + 3 * 3600 + 90)
    text = home.footer_line(_Dash(snap), 160, now=now)
    assert "pause scheduled: " in text and "(in 3h 1m)" in text
    assert "all clear" not in text


def test_the_snapshot_reads_it_from_state(cfg):
    _schedule(cfg, 1_800_000_000.0)
    snap = data.build_snapshot(cfg, state_mod.read(cfg).to_dict())
    assert snap.pause_at == 1_800_000_000.0
