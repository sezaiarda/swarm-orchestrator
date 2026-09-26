"""Tests for owner notifications: how a ping is sent, logged, and who sends one.

Two failure classes cost the most here, and they pull in opposite directions:

* **the silent drop** — ``notify.sh`` failed on every send and every caller threw
  the exit code away, so a run that never reached the owner looked identical to
  one that did. Hence the ledger: every attempt, delivered or not, is one JSON
  line with its error, and ``notify`` never raises into the caller.
* **the firehose** — a status that pings when it should not trains the owner to
  ignore the channel. Exactly one worker finish rings the phone now (``fail``),
  a re-run of ``swarm done`` must not ring it twice, and ``operator`` hands its
  action to a session instead of a person.

Everything runs against the ``SWARM_TG_SINK`` file or a throwaway ``notify.sh``;
nothing touches the network. ``SWARM_STATE_DIR`` is always pinned so the ledger
can never fall through to the real project config.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_orchestrator import config as config_mod
from swarm_orchestrator import launch, owner, statuses, telegram
from swarm_orchestrator.config import load


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "state"
    monkeypatch.setenv("SWARM_STATE_DIR", str(path))
    return path


@pytest.fixture
def sink(tmp_path: Path, state_dir: Path, monkeypatch) -> Path:
    path = tmp_path / "tg.log"
    monkeypatch.setenv("SWARM_TG_SINK", str(path))
    return path


@pytest.fixture
def cfg(tmp_path: Path, sink: Path, monkeypatch):
    """A bare-driver Config whose telegrams land in the sink."""
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\nP1 needs:P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_SLUG", "notifytest")
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    for leak in ("SWARM_MASTER_CMD", "SWARM_WORKER_CMD", "SWARM_SESSION", "SWARM_LAYOUT",
                 "SWARM_GIT_ISOLATION"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    return c


def sent(sink: Path) -> list[str]:
    return sink.read_text(encoding="utf-8").splitlines() if sink.is_file() else []


def ledger(state_dir: Path) -> list[dict]:
    path = state_dir / telegram.LEDGER_NAME
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def script(tmp_path: Path, body: str) -> Path:
    """An executable stand-in for ``notify.sh``."""
    path = tmp_path / "bin" / "notify.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


# -- the send ---------------------------------------------------------------
def test_sink_send_is_delivered_and_logged(sink, state_dir):
    ok = telegram.notify(
        "unused", "swarm: P3 FAILED — tests red", kind="worker-done", phase="P3",
        source="launch.done",
    )
    assert ok is True
    assert sent(sink) == ["swarm: P3 FAILED — tests red"]
    [row] = ledger(state_dir)
    assert row["kind"] == "worker-done" and row["phase"] == "P3"
    assert row["source"] == "launch.done"
    assert row["delivered"] is True and row["error"] is None
    assert row["text"] == "swarm: P3 FAILED — tests red"
    assert isinstance(row["ts"], float)


def test_a_suppressed_message_is_logged_never_sent_and_never_a_drop(sink, state_dir, tmp_path):
    """Held back on purpose: in the alerts, off the phone, and not read as a failure."""
    from swarm_orchestrator.tui.data import parse_notification

    ok = telegram.notify("unused", "swarm: job done", kind="operator-done", phase="J",
                         suppressed="routine outcome")
    assert ok is False and sent(sink) == []
    [row] = ledger(state_dir)
    assert row["delivered"] is False and row["error"] is None
    assert row["suppressed"] == "routine outcome"
    note = parse_notification(json.dumps(row))
    assert note.suppressed == "routine outcome" and note.dropped is False
    assert parse_notification(json.dumps({**row, "suppressed": None})).dropped is True


def test_an_oversized_message_is_clamped_not_rejected(sink, state_dir):
    """Telegram refuses >4096 chars outright; a refused send is a lost one."""
    result = telegram.notify_detail("unused", "x" * 10_000)
    assert result.delivered
    assert len(result.text) == telegram.MAX_MESSAGE_CHARS
    assert result.text.endswith("...[truncated]")
    # the ledger records what actually went out, not what was asked for
    assert ledger(state_dir)[0]["text"] == result.text


def test_a_message_at_the_limit_is_untouched(sink):
    text = "y" * telegram.MAX_MESSAGE_CHARS
    assert telegram.notify_detail("unused", text).text == text


def test_an_unwritable_sink_fails_loudly_in_the_ledger(tmp_path, state_dir, monkeypatch):
    blocked = tmp_path / "sink-is-a-dir"
    blocked.mkdir()
    monkeypatch.setenv("SWARM_TG_SINK", str(blocked))
    result = telegram.notify_detail("unused", "hello", kind="waiting", phase="P1")
    assert result.delivered is False and result.error
    [row] = ledger(state_dir)
    assert row["delivered"] is False and row["error"] == result.error


def test_script_success(tmp_path, state_dir, monkeypatch):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    out = tmp_path / "got.txt"
    sh = script(tmp_path, f'printf "%s" "$1" > {out}\n')
    assert telegram.notify(str(sh), "ping") is True
    assert out.read_text() == "ping"
    assert ledger(state_dir)[0]["delivered"] is True


def test_script_failure_keeps_the_api_error(tmp_path, state_dir, monkeypatch):
    """The redacted API error used to be captured and discarded."""
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    sh = script(tmp_path, 'echo "400 can\'t parse entities" >&2\nexit 1\n')
    result = telegram.notify_detail(str(sh), "ping")
    assert result.delivered is False
    assert result.error == "400 can't parse entities"
    assert ledger(state_dir)[0]["error"] == "400 can't parse entities"


@pytest.mark.parametrize(
    ("body", "error"),
    [("echo from-stdout\nexit 2\n", "from-stdout"), ("exit 3\n", "exit 3")],
)
def test_script_failure_with_no_stderr_still_says_something(tmp_path, state_dir, monkeypatch,
                                                            body, error):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    assert telegram.notify_detail(str(script(tmp_path, body)), "ping").error == error


def test_a_missing_script_never_raises(tmp_path, state_dir, monkeypatch):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    result = telegram.notify_detail(str(tmp_path / "nope.sh"), "ping")
    assert result.delivered is False and result.error
    assert ledger(state_dir)[0]["delivered"] is False


def test_a_long_script_error_is_capped(tmp_path, state_dir, monkeypatch):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    sh = script(tmp_path, 'printf "%0600d" 0 >&2\nexit 1\n')
    assert len(telegram.notify_detail(str(sh), "ping").error) == 400


# -- the ledger -------------------------------------------------------------
def test_an_explicit_state_dir_beats_the_environment(tmp_path, sink, state_dir):
    mine = tmp_path / "other-state"
    telegram.notify("unused", "hi", state_dir=mine)
    assert ledger(mine) and not ledger(state_dir)


def test_with_nowhere_to_log_the_send_still_goes_out(sink, monkeypatch):
    monkeypatch.delenv("SWARM_STATE_DIR", raising=False)

    def no_project(*_a, **_kw):
        raise ValueError("no project here")

    monkeypatch.setattr(config_mod, "load", no_project)
    assert telegram.notify("unused", "orphan ping") is True
    assert sent(sink) == ["orphan ping"]


def test_an_unwritable_ledger_never_fails_the_send(tmp_path, sink):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    assert telegram.notify("unused", "hi", state_dir=not_a_dir) is True
    assert sent(sink) == ["hi"]


def test_every_send_appends_one_whole_line(sink, state_dir):
    for i in range(5):
        telegram.notify("unused", f"line {i}\nwith a newline", kind="other")
    rows = ledger(state_dir)
    assert [r["text"] for r in rows] == [f"line {i}\nwith a newline" for i in range(5)]


# -- check ------------------------------------------------------------------
def test_check_passes_under_the_sink(sink):
    assert telegram.check("/does/not/exist") == (True, "sink")


def test_check_reads_the_env_file_the_sender_reads(tmp_path, monkeypatch):
    """Beside the repo root (``bin/..``), not the owner's own Claude bot."""
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    monkeypatch.delenv("SWARM_TG_ENV", raising=False)
    sh = script(tmp_path, "exit 0\n")
    ok, detail = telegram.check(str(sh))
    assert not ok and "telegram env missing" in detail and str(tmp_path / ".env") in detail

    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=abc\n")
    ok, detail = telegram.check(str(sh))
    assert not ok and "TELEGRAM_CHAT_ID" in detail

    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=42\n")
    assert telegram.check(str(sh)) == (True, "ok")


def test_check_honours_the_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    env = tmp_path / "elsewhere.env"
    env.write_text("TELEGRAM_BOT_TOKEN=a\nTELEGRAM_CHAT_ID=1\n")
    monkeypatch.setenv("SWARM_TG_ENV", str(env))
    assert telegram.check(str(script(tmp_path, "exit 0\n"))) == (True, "ok")


def test_check_rejects_a_script_it_cannot_run(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    sh = script(tmp_path, "exit 0\n")
    sh.chmod(0o644)
    ok, detail = telegram.check(str(sh))
    assert not ok and "not executable" in detail


# -- who pings: the completion ping -----------------------------------------
@pytest.mark.parametrize("status", statuses.ALL)
def test_only_pinging_statuses_have_a_completion_ping(status):
    ping = launch._completion_ping("P1", status, "a recap")
    assert (ping is not None) == (status in statuses.PINGS)


def test_the_fail_ping_carries_the_collapsed_recap():
    assert launch._completion_ping("P1", "fail", "  build\n  broke ") == "swarm: P1 FAILED — build broke"
    assert launch._completion_ping("P1", "fail", "   ") == "swarm: P1 FAILED"


@pytest.mark.parametrize(
    ("status", "why"),
    [("ok", "is a silent success"), ("operator", "hands off to a session")],
)
def test_non_pinging_finishes_say_why_they_stayed_silent(status, why):
    plan, detail = launch._ping_decision("P1", status, "did it", None, "written", False)
    assert plan == "skipped" and why in detail


def test_a_failure_always_pings_however_thin_the_note():
    """Silence here would turn a bad recap into an invisible dead run."""
    assert launch._ping_decision("P1", "fail", "x", None, "written", False) == ("send", "")


def test_a_repeated_identical_failure_is_deduped():
    plan, detail = launch._ping_decision("P1", "fail", "tests  red", "tests red", "written", False)
    assert plan == "deduped" and "identical recap" in detail


def test_a_refused_thinner_failure_is_deduped():
    plan, _ = launch._ping_decision("P1", "fail", "x", "a much fuller recap", "refused", False)
    assert plan == "deduped"


def test_force_always_pings():
    assert launch._ping_decision("P1", "fail", "same", "same", "written", True)[0] == "send"


def test_only_operator_routes_and_only_with_a_real_brief():
    assert launch._route_decision("P1", "fail", "x" * 200)[0] == "skipped"
    thin = launch._route_decision("P1", "operator", "done")
    assert thin[0] == "skipped" and "too thin" in thin[1]
    rich = "rotate the deploy key on the staging box, then re-run the smoke suite"
    assert launch._route_decision("P1", "operator", rich) == ("dispatch", "")


# -- who pings: `swarm done` / `swarm waiting` end to end -------------------
def test_done_fail_pings_once_and_logs_the_ping(cfg, sink):
    result = launch.done(cfg, "P1", "fail", "cargo test red on the parser")
    assert result.ping == "sent"
    assert sent(sink) == ["swarm: P1 FAILED — cargo test red on the parser"]
    [row] = ledger(cfg.state_dir)
    assert (row["kind"], row["phase"], row["source"]) == ("worker-done", "P1", "launch.done")
    assert "owner telegrammed" in result.render()


def test_done_fail_run_twice_rings_the_phone_once(cfg, sink):
    """A phase that ran `swarm done` several times used to telegram once per run."""
    launch.done(cfg, "P1", "fail", "cargo test red")
    again = launch.done(cfg, "P1", "fail", "cargo test red")
    assert again.ping == "deduped"
    assert len(sent(sink)) == 1
    assert "no telegram: identical recap" in again.render()


def test_done_ok_is_silent(cfg, sink):
    result = launch.done(cfg, "P0", "ok", "wrote the parser")
    assert result.ping == "skipped"
    assert sent(sink) == [] and ledger(cfg.state_dir) == []


def test_done_operator_hands_off_instead_of_pinging(cfg, sink, monkeypatch):
    from swarm_orchestrator import opqueue

    cfg.operator_enabled = True
    spawned = []
    monkeypatch.setattr(launch, "_detach_triage", lambda c, p: spawned.append(p) or True)
    note = "rotate the deploy key on the staging box, then re-run the smoke suite"
    result = launch.done(cfg, "P1", "operator", note)
    assert (result.ping, result.route) == ("skipped", "dispatch")
    assert sent(sink) == [] and ledger(cfg.state_dir) == []
    assert [i.phase for i in opqueue.load_all(cfg)] == ["P1"]
    assert spawned == ["P1"]


def test_the_retired_spelling_pings_nobody(cfg, sink, monkeypatch):
    """`needs-owner` OWED a ping when it was live; today it is `operator`."""
    monkeypatch.setattr(launch, "_detach_triage", lambda c, p: True)
    assert statuses.NEEDS_OWNER in statuses.OWED_PING
    result = launch.done(cfg, "P1", "needs-owner", "check the auth change")
    assert result.status == "operator" and result.ping == "skipped"
    # The completion ping stays silent. What reaches the phone with the operator
    # off (this cfg's default) is the hand-off itself, as a to-do — never silence.
    assert result.route == "owner"
    assert sent(sink) == [
        "swarm: to-do for you from P1 (no operator is running) — check the auth change"
    ]


def test_a_dropped_fail_ping_is_reported_by_done(cfg, tmp_path, monkeypatch):
    blocked = tmp_path / "sink-dir"
    blocked.mkdir()
    monkeypatch.setenv("SWARM_TG_SINK", str(blocked))
    result = launch.done(cfg, "P1", "fail", "boom")
    assert result.ping == "failed" and result.ping_detail
    assert "telegram FAILED" in result.render()
    assert ledger(cfg.state_dir)[0]["delivered"] is False


def test_waiting_pings_the_question_under_its_own_kind(cfg, sink):
    owner.waiting(cfg, "P1", "  which\n schema? ")
    cost, head = sent(sink)[:2]
    assert head == "swarm: P1 is waiting on you — which schema?"
    # The first line says what waiting costs (unit-tested in test_owner_history).
    assert "slot held · asked " in cost
    [row] = ledger(cfg.state_dir)
    assert (row["kind"], row["phase"], row["source"]) == ("waiting", "P1", "cli.waiting")


def test_call_site_kinds_are_declared():
    """The dashboard renders from KINDS; a kind nobody declared is a blank label."""
    for kind in ("worker-done", "waiting", "owner-row", "operator-abandoned",
                 "integrate-hold", "worktree-fail", "spawn-fail", "other"):
        assert kind in telegram.KINDS


def test_the_finish_ping_is_filed_as_finish(swarm):
    swarm.env["FAKE_WORKER_SLEEP"] = "2"  # a full run, as test_lifecycle drives it
    swarm.up()
    assert swarm.wait(lambda: "ACTION finish" in swarm.log_text(), timeout=40), swarm.log_text()
    rows = ledger(swarm.state_dir)
    finish = [r for r in rows if str(r.get("text", "")).startswith("swarm finished")]
    assert len(finish) == 1, rows
    assert finish[0]["delivered"] is True
    assert finish[0]["kind"] == "finish"


def test_a_resolver_messaging_the_owner_tells_the_supervisor_it_gave_up(
    sink: Path, tmp_path: Path, monkeypatch
):
    from swarm_orchestrator import cli

    project = tmp_path / "proj"
    project.mkdir()
    cfg = load(project_dir=str(project))
    poked: list[str] = []
    monkeypatch.setattr(launch, "_poke_fifo", lambda cfg, line: poked.append(line) or True)
    monkeypatch.setenv("SWARM_SESSION_ID", "resolver:P1")
    assert cli.cmd_notify(cfg, "cannot merge the ledger rows of P1") == 0
    assert poked == ["resolver-escalated P1\n"]
    monkeypatch.setenv("SWARM_SESSION_ID", "overseer:x")
    cli.cmd_notify(cfg, "hello")
    assert poked == ["resolver-escalated P1\n"]


# -- a burst of `blocked` finishes is one ping -----------------------------------
def test_blocked_phases_of_one_burst_are_one_ping_grouped_by_reason(cfg, sink):
    from swarm_orchestrator import blockedping
    from swarm_orchestrator.logutil import Log

    wall = "The live box refuses this host's ssh key. Deploy needs it."
    for phase in ("P1", "P2", "P3"):
        result = launch.done(cfg, phase, "blocked", f"{phase}: {wall}")
        assert result.ping == "held"
    launch.done(cfg, "P4", "blocked", "the vendor API is down")
    assert sent(sink) == []
    assert [r["suppressed"] for r in ledger(cfg.state_dir)] == [blockedping.HELD] * 4

    log = Log(cfg.supervisor_log)
    start = blockedping.deadline(cfg) - blockedping.GATHER_S
    assert not blockedping.flush(cfg, log, start + blockedping.GATHER_S - 1)
    assert blockedping.flush(cfg, log, start + blockedping.GATHER_S)
    assert not blockedping.flush(cfg, log, start + 2 * blockedping.GATHER_S)  # nothing left
    log.close()
    assert "\n".join(sent(sink)) == (
        "swarm: 4 phases are blocked and need you\n"
        "- The live box refuses this host's ssh key. (P1, P2, P3)\n"
        "- the vendor API is down (P4)"
    )
    assert blockedping.deadline(cfg) is None


def test_every_blocked_phase_pings_under_all_pings(cfg, sink, monkeypatch):
    monkeypatch.setattr(cfg, "telegram_pings", telegram.ALL)
    launch.done(cfg, "P1", "blocked", "the box refuses the key")
    launch.done(cfg, "P2", "blocked", "the box refuses the key")
    assert len(sent(sink)) == 2
