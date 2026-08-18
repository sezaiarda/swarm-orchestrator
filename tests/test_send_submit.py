"""`send_submit` must verify by persistence, not by one snapshot.

Regression for a stall: a master pane was spawned, its prompt was
typed, the Enter was swallowed, and `send_submit` returned True anyway — so the
supervisor believed a master was driving the run while the prompt sat unsubmitted
in the box. The swarm idled with free slots and ready phases.

The cause was treating an empty input box as proof of submission. Immediately
after typing, an empty box means EITHER "submitted" OR "not rendered yet", and
the two are indistinguishable in a single capture. Under load — two parallel builds
on a small box — rendering routinely misses the 0.5 s settle window.

These tests drive `tmux.run` through a stub, so no tmux server is involved.
"""

from __future__ import annotations

import subprocess

import pytest

from swarm_orchestrator import tmux

PROMPT = "Read /home/dev/prompts/step_master.md and follow every instruction exactly."
HEAD = PROMPT[:40]


class _Pane:
    """A fake claude pane whose render timing and Enter handling are scriptable."""

    def __init__(self, *, renders_after: int = 0, enters_needed: int = 1) -> None:
        self.captures = 0
        self.renders_after = renders_after  # captures before the text appears
        self.enters_needed = enters_needed  # Enters swallowed before it submits
        self.enters = 0
        self.typed = False
        self.submitted = False
        self.sent: list[list[str]] = []

    def run(self, args, check=False):
        self.sent.append(list(args))
        verb = args[0]
        if verb == "send-keys":
            if "-l" in args:
                self.typed = True
            elif "Enter" in args:
                self.enters += 1
                if self.typed and self.enters >= self.enters_needed:
                    self.submitted = True
            return subprocess.CompletedProcess(args, 0, "", "")
        if verb == "capture-pane":
            self.captures += 1
            holding = (
                self.typed
                and not self.submitted
                and self.captures > self.renders_after
            )
            out = f"❯ {PROMPT}\n" if holding else "❯ \n"
            return subprocess.CompletedProcess(args, 0, out, "")
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.fixture
def fast(monkeypatch):
    """Collapse sleeps so the retry loop runs at test speed."""
    monkeypatch.setattr(tmux.time, "sleep", lambda _s: None)


def _install(monkeypatch, pane: _Pane) -> None:
    monkeypatch.setattr(tmux, "run", pane.run)


def test_submit_on_the_first_enter(monkeypatch, fast):
    pane = _Pane(enters_needed=1)
    _install(monkeypatch, pane)
    assert tmux.send_submit("%1", PROMPT) is True
    assert pane.submitted


def test_late_render_still_gets_an_enter(monkeypatch, fast):
    """THE regression: the box looks empty only because nothing is drawn yet.

    With the old snapshot check this returned True on an unsubmitted prompt.
    """
    pane = _Pane(renders_after=6, enters_needed=2)
    _install(monkeypatch, pane)
    assert tmux.send_submit("%1", PROMPT) is True
    assert pane.submitted, "text rendered late and never got a second Enter"
    assert pane.enters >= 2


def test_a_swallowed_enter_is_retried(monkeypatch, fast):
    pane = _Pane(enters_needed=3)
    _install(monkeypatch, pane)
    assert tmux.send_submit("%1", PROMPT) is True
    assert pane.enters >= 3


def test_reports_failure_when_every_enter_is_swallowed(monkeypatch, fast):
    """Must return False rather than claim a success nobody can see."""
    pane = _Pane(enters_needed=99)
    _install(monkeypatch, pane)
    assert tmux.send_submit("%1", PROMPT) is False
    assert not pane.submitted


def test_non_claude_pane_reports_success(monkeypatch, fast):
    """A pane that never renders a `❯` box (test/fake panes) must not stall."""

    class _Bare(_Pane):
        def run(self, args, check=False):
            if args[0] == "capture-pane":
                return subprocess.CompletedProcess(args, 0, "no box here\n", "")
            return super().run(args, check)

    pane = _Bare()
    _install(monkeypatch, pane)
    assert tmux.send_submit("%1", PROMPT) is True


class _DropsTyping(_Pane):
    """A pane that ignores the first ``drops`` literal sends (TUI not ready).

    This is the OTHER way injection fails, and the reason an empty input box
    proves nothing: text that never arrived looks exactly like text that was
    submitted.
    """

    def __init__(self, *, drops: int = 1, **kw) -> None:
        super().__init__(**kw)
        self.drops = drops
        self.literal_sends = 0

    def run(self, args, check=False):
        if args[0] == "send-keys" and "-l" in args:
            self.literal_sends += 1
            self.sent.append(list(args))
            if self.literal_sends <= self.drops:
                return subprocess.CompletedProcess(args, 0, "", "")  # swallowed
        return super().run(args, check)


def test_dropped_keystrokes_are_retyped(monkeypatch, fast):
    """Text that never arrived must be typed again, not called a success.

    Without the `_pane_has` check this returns True with nothing ever sent —
    the same false success as the original bug, reached the other way.
    """
    pane = _DropsTyping(drops=1, enters_needed=1)
    _install(monkeypatch, pane)
    assert tmux.send_submit("%1", PROMPT) is True
    assert pane.literal_sends >= 2, "never retyped after the keystrokes vanished"
    assert pane.submitted


def test_reports_failure_when_typing_never_lands(monkeypatch, fast):
    pane = _DropsTyping(drops=99, enters_needed=1)
    _install(monkeypatch, pane)
    assert tmux.send_submit("%1", PROMPT) is False
    assert not pane.submitted


def test_does_not_retype_once_the_text_has_been_seen(monkeypatch, fast):
    """A transcript that scrolls away must not cause a duplicate submission."""
    pane = _Pane(enters_needed=2)
    _install(monkeypatch, pane)
    assert tmux.send_submit("%1", PROMPT) is True
    literal = [a for a in pane.sent if a[0] == "send-keys" and "-l" in a]
    assert len(literal) == 1, f"retyped an already-seen prompt: {len(literal)}x"
