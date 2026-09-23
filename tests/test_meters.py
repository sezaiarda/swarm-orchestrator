"""meters: the worker status-line tap that feeds the dashboard's context, cost and limit figures."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from swarm_orchestrator import launch, meters
from swarm_orchestrator.config import load


def payload(tokens=412_000, session="s1", week=41.0, resets=1_900_000_000, cost=12.5):
    return {
        "session_id": session,
        "context_window": {"total_input_tokens": tokens, "context_window_size": 1_000_000,
                           "used_percentage": tokens / 10_000},
        "cost": {"total_cost_usd": cost, "total_duration_ms": 3_600_000},
        "effort": {"level": "high"},
        "rate_limits": {"five_hour": {"used_percentage": 12, "resets_at": resets - 3600},
                        "seven_day": {"used_percentage": week, "resets_at": resets}},
    }


def test_a_render_is_recorded_per_phase(tmp_path):
    m = meters.record(payload(), tmp_path, "P1", now=100.0)

    on_disk = json.loads((tmp_path / "meters" / "P1.json").read_text())
    assert on_disk == m
    assert (m["context_tokens"], m["context_window"], m["peak_tokens"]) == (412_000, 1_000_000, 412_000)
    assert m["cost_usd"] == 12.5 and m["effort"] == "high"
    assert m["seven_day"] == {"pct": 41.0, "resets_at": 1_900_000_000}


def test_the_peak_survives_a_shrinking_context_but_not_a_new_session(tmp_path):
    meters.record(payload(tokens=500_000), tmp_path, "P1")
    assert meters.record(payload(tokens=90_000), tmp_path, "P1")["peak_tokens"] == 500_000
    assert meters.record(payload(tokens=90_000, session="s2"), tmp_path, "P1")["peak_tokens"] == 90_000


def test_weekly_samples_are_logged_only_when_the_figure_moves(tmp_path):
    for now, week in ((100.0, 41.0), (103.0, 41.0), (106.0, 42.0)):
        meters.record(payload(week=week), tmp_path, "P1", now=now)
    rows = (tmp_path / "meters" / "limits.jsonl").read_text().splitlines()
    assert [json.loads(r)["pct"] for r in rows] == [41.0, 42.0]


def test_a_render_writes_only_when_something_moved_and_at_most_every_two_seconds(tmp_path):
    """The tap runs several times a second per streaming worker: an unchanged
    payload never rewrites the file, and a changed one waits out MIN_WRITE_S."""
    path = tmp_path / "meters" / "P1.json"
    meters.record(payload(tokens=100_000), tmp_path, "P1", now=100.0)
    first = path.stat().st_mtime_ns

    meters.record(payload(tokens=100_000), tmp_path, "P1", now=105.0)  # nothing moved
    assert path.stat().st_mtime_ns == first
    meters.record(payload(tokens=120_000), tmp_path, "P1", now=100.5)  # moved, too soon
    assert json.loads(path.read_text())["context_tokens"] == 100_000

    meters.record(payload(tokens=120_000), tmp_path, "P1", now=102.5)  # moved, due
    assert json.loads(path.read_text())["context_tokens"] == 120_000

    # A new session replaces the old meter at once, however recent the last write.
    meters.record(payload(tokens=5_000, session="s2"), tmp_path, "P1", now=102.6)
    assert json.loads(path.read_text())["session_id"] == "s2"


def test_a_payload_without_limits_or_context_is_still_a_meter(tmp_path):
    m = meters.record({"session_id": "s1"}, tmp_path, "P1")
    assert m["context_tokens"] is None and m["seven_day"] is None and m["peak_tokens"] is None
    assert not (tmp_path / "meters" / "limits.jsonl").exists()


def test_the_tap_is_injected_only_into_a_settings_object_without_a_status_line(tmp_path):
    none = tmp_path / "no-settings.json"
    out = json.loads(meters.settings_with_tap(
        '{"teammateMode":"in-process"}', tmp_path, "P1", owner_settings=none))
    assert out["teammateMode"] == "in-process"
    assert "swarm_orchestrator.meters" in out["statusLine"]["command"]
    assert out["statusLine"]["command"].endswith(" P1 ''")  # no owner status line

    own = '{"statusLine":{"type":"command","command":"mine"}}'
    assert meters.settings_with_tap(own, tmp_path, "P1") == own
    assert meters.settings_with_tap("", tmp_path, "P1") == ""
    assert meters.settings_with_tap("not json", tmp_path, "P1") == "not json"


def test_every_worker_launch_carries_the_tap(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("SWARM_WORKER_SETTINGS", raising=False)
    cfg = load(project_dir=str(tmp_path))
    assert "swarm_orchestrator.meters" in launch._worker_shell(cfg, "P1", tmp_path)


def tap(stdin: str, state, home):
    """Run the tap as Claude Code does: a fresh process, the payload on stdin."""
    return subprocess.run(
        [sys.executable, "-m", "swarm_orchestrator.meters", str(state), "P1"],
        input=stdin, capture_output=True, text=True, env={**os.environ, "HOME": str(home)},
    )


def test_the_tap_draws_a_bar_the_pane_scraper_can_read_and_never_fails(tmp_path):
    from swarm_orchestrator.tui import probes

    ok = tap(json.dumps(payload()), tmp_path / "state", tmp_path)  # no owner status line
    assert ok.returncode == 0
    assert probes.parse_context(ok.stdout)[0] == 412_000
    assert (tmp_path / "state" / "meters" / "P1.json").is_file()

    broken = tap("{not json", tmp_path / "state", tmp_path)
    assert broken.returncode == 0 and broken.stdout.strip() == "P1"


def test_the_owner_status_line_is_chained_with_the_same_payload(tmp_path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    owner = "python3 -c 'import sys,json; print(\"mine\", json.load(sys.stdin)[\"session_id\"])'"
    (claude / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": owner}}))

    assert tap(json.dumps(payload()), tmp_path / "state", tmp_path).stdout.strip() == "mine s1"


def test_the_owner_command_is_resolved_at_launch_not_per_render(tmp_path):
    """settings.json is read once, by settings_with_tap; the tap then runs the
    command it was handed even with no settings.json in reach."""
    owner = "python3 -c 'import sys,json; print(\"cached\", json.load(sys.stdin)[\"session_id\"])'"
    settings = tmp_path / "owner.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": owner}}))
    merged = json.loads(meters.settings_with_tap("{}", tmp_path / "state", "P1", owner_settings=settings))
    argv = __import__("shlex").split(merged["statusLine"]["command"])
    assert argv[-1] == owner

    home = tmp_path / "empty-home"  # no ~/.claude/settings.json at render time
    home.mkdir()
    out = subprocess.run(argv, input=json.dumps(payload()), capture_output=True, text=True,
                         env={**os.environ, "HOME": str(home)})
    assert out.returncode == 0 and out.stdout.strip() == "cached s1"
