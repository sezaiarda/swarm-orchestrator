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
    """The external-script fallback looks next to the script, not at a fixed path.

    Only reachable when the in-repo bot is unconfigured, so this test points
    SWARM_TG_ENV at a file that does not exist to get there.
    """
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SWARM_TG_ENV", str(tmp_path / "absent.env"))
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


# -- the in-repo bot ------------------------------------------------------
class _FakeAPI:
    """Stand-in for the Bot API: records calls, scripts the replies."""

    def __init__(self, *, updates_chat=None, send_ok=True) -> None:
        self.updates_chat = updates_chat
        self.send_ok = send_ok
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, token, method, params=None):
        self.calls.append((method, params or {}))
        if method == "getUpdates":
            if self.updates_chat is None:
                return {"ok": True, "result": []}
            return {
                "ok": True,
                "result": [{"message": {"chat": {"id": self.updates_chat}}}],
            }
        if method == "sendMessage":
            return {"ok": self.send_ok}
        return None


@pytest.fixture
def repo_env(tmp_path, monkeypatch):
    """Point the notifier at a throwaway .env and disable the test sink."""
    env = tmp_path / ".env"
    monkeypatch.setenv("SWARM_TG_ENV", str(env))
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return env


def test_no_env_falls_back_to_the_external_script(repo_env, monkeypatch):
    """An unconfigured bot must not swallow the message — it defers."""
    called: list[str] = []
    monkeypatch.setattr(
        telegram.subprocess,
        "run",
        lambda *a, **k: called.append("script") or _ok(),
    )
    telegram.notify("/bin/true", "hello")
    assert called == ["script"]


def _ok():
    import subprocess as sp

    return sp.CompletedProcess([], 0, "", "")


def test_in_repo_bot_sends_without_any_script(repo_env, monkeypatch):
    repo_env.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=42\n")
    api = _FakeAPI()
    monkeypatch.setattr(telegram, "_api", api)
    monkeypatch.setattr(
        telegram.subprocess, "run", lambda *a, **k: pytest.fail("used the script")
    )

    assert telegram.notify("/does/not/exist", "hello") is True
    assert api.calls == [
        ("sendMessage", {"chat_id": "42", "text": "hello", "disable_web_page_preview": "true"})
    ]


def test_chat_id_is_discovered_and_persisted_on_first_send(repo_env, monkeypatch):
    """The owner's only obligation is to message the bot once, whenever."""
    repo_env.write_text("TELEGRAM_BOT_TOKEN=abc\n")
    api = _FakeAPI(updates_chat=777)
    monkeypatch.setattr(telegram, "_api", api)

    assert telegram.notify("/does/not/exist", "hello") is True
    assert "TELEGRAM_CHAT_ID=777" in repo_env.read_text()
    assert [m for m, _ in api.calls] == ["getUpdates", "sendMessage"]

    # Second send must reuse the persisted id, not re-query.
    api.calls.clear()
    telegram.notify("/does/not/exist", "again")
    assert [m for m, _ in api.calls] == ["sendMessage"]


def test_token_but_no_message_yet_reports_failure_not_fallback(repo_env, monkeypatch):
    """Configured-but-unreachable must NOT silently fall through to a script."""
    repo_env.write_text("TELEGRAM_BOT_TOKEN=abc\n")
    monkeypatch.setattr(telegram, "_api", _FakeAPI(updates_chat=None))
    monkeypatch.setattr(
        telegram.subprocess, "run", lambda *a, **k: pytest.fail("fell back")
    )
    assert telegram.notify("/does/not/exist", "hello") is False


def test_check_accepts_a_token_with_no_chat_id_yet(repo_env):
    repo_env.write_text("TELEGRAM_BOT_TOKEN=abc\n")
    ok, detail = telegram.check("/does/not/exist")
    assert ok and "in-repo bot" in detail


def test_env_parsing_ignores_comments_and_quotes(repo_env):
    repo_env.write_text('# a note\n\nTELEGRAM_BOT_TOKEN="abc"\ngarbage\n')
    assert telegram._load_env()["TELEGRAM_BOT_TOKEN"] == "abc"
