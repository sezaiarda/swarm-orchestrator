"""meters: the worker status-line tap that feeds the dashboard's context, cost and limit figures."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from swarm_orchestrator import launch, meters, usage
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


def test_limit_samples_are_logged_only_when_a_figure_moves(tmp_path):
    for now, week in ((100.0, 41.0), (103.0, 41.0), (106.0, 42.0)):
        meters.record(payload(week=week), tmp_path, "P1", now=now)
    rows = [json.loads(r) for r in (tmp_path / "meters" / "limits.jsonl").read_text().splitlines()]
    assert [r["week_pct"] for r in rows] == [41.0, 42.0]
    # Both windows ride on every row, tagged with the run (none open here) and
    # the account (none logged in here).
    assert rows[0] == {"ts": 100.0, "run_id": None, "five_pct": 12, "five_resets_at": 1_900_000_000 - 3600,
                       "week_pct": 41.0, "week_resets_at": 1_900_000_000, "account": None}


def _login(tmp_path, monkeypatch, uuid):
    path = tmp_path / ".claude.json"
    path.write_text(json.dumps({"oauthAccount": {
        "accountUuid": uuid, "emailAddress": "someone@example.com"}}))
    monkeypatch.setenv("SWARM_CLAUDE_JSON", str(path))


def test_a_sample_carries_a_hash_of_the_login_never_the_login(tmp_path, monkeypatch):
    uuid_a = "00000000-0000-0000-0000-00000000000a"
    _login(tmp_path, monkeypatch, uuid_a)
    meters.record(payload(week=41.0), tmp_path, "P1", now=100.0)
    text = (tmp_path / "meters" / "limits.jsonl").read_text()
    row = json.loads(text)
    assert row["account"] == usage.account_key(uuid_a) and len(row["account"]) == 8
    assert uuid_a not in text and "example.com" not in text
    assert "example.com" not in (tmp_path / "meters" / "P1.json").read_text()
    # A switch to another account with the very same figures is still a new row.
    _login(tmp_path, monkeypatch, "00000000-0000-0000-0000-00000000000b")
    meters.record(payload(week=42.0, session="s2"), tmp_path, "P2", now=101.0)
    meters.record(payload(week=42.0), tmp_path, "P1", now=110.0)
    rows = [json.loads(r) for r in (tmp_path / "meters" / "limits.jsonl").read_text().splitlines()]
    assert [r["week_pct"] for r in rows] == [41.0, 42.0]
    assert rows[1]["account"] == usage.account_key("00000000-0000-0000-0000-00000000000b")


def test_the_account_is_looked_up_only_when_a_figure_moves(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(usage, "login_account", lambda: calls.append(1) or "acct-a")
    meters.record(payload(week=41.0), tmp_path, "P1", now=100.0)
    meters.record(payload(week=41.0, tokens=500_000), tmp_path, "P1", now=110.0)
    assert len(calls) == 1
    assert json.loads((tmp_path / "meters" / "P1.json").read_text())["account"] == "acct-a"


def test_a_limit_change_seen_by_two_workers_is_logged_once_with_the_open_run(tmp_path):
    from swarm_orchestrator import runs

    rec, _ = runs.start(tmp_path, 1, "none", now=50.0)
    meters.record(payload(week=41.0), tmp_path, "P1", now=100.0)
    meters.record(payload(week=41.0, session="s9"), tmp_path, "P2", now=101.0)  # same account figures
    rows = [json.loads(r) for r in (tmp_path / "meters" / "limits.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["run_id"] == rec["run_id"]


def test_a_five_hour_move_alone_is_logged(tmp_path):
    p1 = payload()
    meters.record(p1, tmp_path, "P1", now=100.0)
    p2 = payload(tokens=500_000)
    p2["rate_limits"]["five_hour"]["used_percentage"] = 13
    meters.record(p2, tmp_path, "P1", now=110.0)
    rows = [json.loads(r) for r in (tmp_path / "meters" / "limits.jsonl").read_text().splitlines()]
    assert [r["five_pct"] for r in rows] == [12, 13]


def test_a_replaced_session_is_kept_for_the_cost(tmp_path):
    meters.record(payload(cost=3.0), tmp_path, "P1", now=100.0)
    meters.record(payload(cost=1.0, session="s2"), tmp_path, "P1", now=200.0)
    rows = [json.loads(r) for r in (tmp_path / "meters" / "sessions.jsonl").read_text().splitlines()]
    assert [(r["session_id"], r["cost_usd"]) for r in rows] == [("s1", 3.0)]


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


def test_the_dashboard_rereads_only_the_meter_files_that_were_replaced(tmp_path, monkeypatch):
    import json

    from swarm_orchestrator.tui import data

    for phase in ("a-W1", "a-W2"):
        (tmp_path / f"{phase}.json").write_text(json.dumps({"phase": phase, "ts": 1.0}))
    cache: dict = {}
    assert data.load_meters(tmp_path, cache) == data.load_meters(tmp_path)
    reads = []
    real = data._read_meter
    monkeypatch.setattr(data, "_read_meter", lambda p: reads.append(p.name) or real(p))
    tmp = tmp_path / ".a-W2.json.1"
    tmp.write_text(json.dumps({"phase": "a-W2", "ts": 2.0}))
    tmp.replace(tmp_path / "a-W2.json")  # the tap's atomic replace: a new inode
    got = data.load_meters(tmp_path, cache)
    assert reads == ["a-W2.json"] and got["a-W2"].ts == 2.0 and got["a-W1"].ts == 1.0
