"""The notification policy: exactly two kinds reach the owner.

Design decision: a run generates a lot of true, interesting status —
"next up = …", merge conflicts a resolver then fixes unattended, parks, per-phase
outcomes. Pushing all of it trains the owner to ignore the channel, and then the
two messages that mattered are lost with the rest.

So: a worker's QUESTION (once), and the run FINISHED (once). Everything else is
logged and dropped. These tests exist because suppression is easy to regress —
adding a `telegram.notify(...)` at a new call site is a one-line change that
silently reopens the firehose.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_orchestrator import telegram


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def line(self, text: str) -> None:
        self.lines.append(text)


@pytest.fixture
def sink(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "tg.log"
    monkeypatch.setenv("SWARM_TG_SINK", str(path))
    return path


def _sent(sink: Path) -> list[str]:
    return sink.read_text().splitlines() if sink.exists() else []


@pytest.mark.parametrize("kind", [telegram.QUESTION, telegram.FINISHED])
def test_owner_kinds_are_delivered(sink, kind):
    assert telegram.notify_owner("unused", kind, f"hello {kind}") is True
    assert _sent(sink) == [f"hello {kind}"]


@pytest.mark.parametrize(
    "kind",
    [
        "integrate-blocked",
        "park",
        "resolved-incomplete",
        "master-not-ready",
        "master-submit-lost",
        "worktree-failed",
        "worker-start-failed",
        "done-needs-owner",
        "done-fail",
        "",
    ],
)
def test_every_other_kind_is_dropped(sink, kind):
    log = _Log()
    assert telegram.notify_owner("unused", kind, "should not be sent", log) is False
    assert _sent(sink) == []
    assert any("NOTIFY-SUPPRESSED" in ln for ln in log.lines)


def test_notify_event_never_sends_but_always_records(sink):
    log = _Log()
    assert telegram.notify_event("park", "P1 moved to its own window", log) is False
    assert _sent(sink) == []
    assert log.lines and "P1 moved to its own window" in log.lines[0]


def test_suppression_is_visible_in_the_log_not_silent(sink):
    """A dropped message must still be findable, or debugging becomes guesswork."""
    log = _Log()
    telegram.notify_event("integrate-blocked", "conflict in myproject", log)
    assert "NOTIFY-SUPPRESSED integrate-blocked" in log.lines[0]


def test_check_finds_credentials_beside_the_script(tmp_path, monkeypatch):
    """`check` must look next to the script, not at a hardcoded location.

    The swarm has its own bot; a hardcoded env path would make `check` report
    telegram broken for every other channel while it worked.
    """
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    chan = tmp_path / "swarm-bot"
    chan.mkdir()
    script = chan / "notify.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o700)

    ok, detail = telegram.check(str(script))
    assert not ok and "env missing" in detail  # no .env yet

    (chan / ".env").write_text("TELEGRAM_BOT_TOKEN=x\nTELEGRAM_CHAT_ID=y\n")
    ok, detail = telegram.check(str(script))
    assert ok, detail
