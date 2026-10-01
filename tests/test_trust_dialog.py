"""Hermetic coverage for answering claude's folder-trust dialog.

:func:`swarm_orchestrator.launch.await_ready` is the second line of defence when
a session opens in a folder claude has not been told to trust. It shells out for
every look and every key, so :func:`swarm_orchestrator.tmux.run` is stubbed with
a small model of the dialog: a list of options, a cursor that Up and Down move
(the list wraps, as measured on Claude Code 2.1.286) and an Enter that takes
whatever the cursor is on. ``launch.time`` is stubbed too, so a 45 s wait costs
nothing.

What is pinned here is the day the dialog changed under the swarm: 2.1.286 asks
"Is this a project you created or one you trust?" with the cursor on "No, exit",
so the old code did not see it, and the Enter it would have sent closes the
session.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import tmux

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "trust_dialog"
READY = "fake-ready-marker"
PANE = "%7"

#: The dialog as 2.1.286 draws it: the cursor starts on the answer that exits.
NEW = ("No, exit", "Yes, I trust this folder")
NEW_HEAD = (
    " Accessing workspace:\n\n /tmp/scratch\n\n"
    " Quick safety check: Is this a project you created or one you trust? (Like\n"
    " your own code, a well-known open source project, or work from your team).\n\n"
    " Security guide\n"
)
NEW_FOOT = " Enter to confirm · Esc to cancel"

#: The earlier wording: numbered, inside a frame, the cursor on the trust answer.
OLD = ("Yes, proceed", "No, exit")
OLD_HEAD = (
    "╭──────────────────────────────────────────────╮\n"
    "│ Do you trust the files in this folder?       │\n"
    "│                                              │\n"
    "│ /tmp/scratch                                 │\n"
)
OLD_FOOT = "╰──────────────────────────────────────────────╯\n   Enter to confirm · Esc to exit"


class _Clock:
    """Deterministic stand-in for ``time``: sleeping just moves the clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def line(self, msg: str) -> None:
        self.lines.append(msg)


class _FakeDialog:
    """One pane showing the folder-trust dialog, then claude's banner.

    ``options`` top to bottom and ``start``, where the cursor begins. ``framed``
    draws the earlier look (numbers, a frame). ``lag`` is how many captures
    still show the screen from before the last key. ``mark=False`` draws no
    cursor. ``prompts`` is how many times the dialog must be answered, ``sticky``
    a dialog no Enter closes, ``after`` how many captures the pane is blank
    before the dialog paints. Enter on anything but the trust answer ends the
    session (``declined``), as "No, exit" does.
    """

    def __init__(
        self,
        options: tuple[str, ...] = NEW,
        start: int = 0,
        *,
        head: str = NEW_HEAD,
        foot: str = NEW_FOOT,
        framed: bool = False,
        lag: int = 0,
        mark: bool = True,
        prompts: int = 1,
        sticky: bool = False,
        after: int = 0,
    ) -> None:
        self.options, self.start, self.cursor = options, start, start
        self.head, self.foot, self.framed = head, foot, framed
        self.lag, self.mark, self.sticky, self.after = lag, mark, sticky, after
        self.prompts = prompts
        self.declined = False
        self.keys: list[str] = []
        self._stale: list[str] = []

    def _screen(self) -> str:
        if self.declined:
            return "[exited]\n"
        if self.prompts <= 0:
            return f"\n ▐▛███▛█   {READY}\n\n❯ \n"
        rows = []
        for i, label in enumerate(self.options):
            mark = "❯" if self.mark and i == self.cursor else " "
            if self.framed:
                rows.append(f"│ {mark} {i + 1}. {label:<38} │")
            else:
                rows.append(f" {mark} {label}")
        return self.head + "\n".join(rows) + "\n\n" + self.foot + "\n"

    def run(self, args, check=False, input_text=None):
        if args[0] == "capture-pane":
            if self.after > 0:
                self.after -= 1
                return SimpleNamespace(stdout="\n", returncode=0)
            if self._stale:
                return SimpleNamespace(stdout=self._stale.pop(0), returncode=0)
            return SimpleNamespace(stdout=self._screen(), returncode=0)
        assert args[:3] == ["send-keys", "-t", PANE], args
        key = args[-1]
        self.keys.append(key)
        if self.declined or self.prompts <= 0 or self.after > 0:
            return SimpleNamespace(stdout="", returncode=0)
        self._stale = [self._screen()] * self.lag
        if key == "Down":
            self.cursor = (self.cursor + 1) % len(self.options)
        elif key == "Up":
            self.cursor = (self.cursor - 1) % len(self.options)
        elif key == "Enter":
            if not self.options[self.cursor].lower().startswith("yes"):
                self.declined = True
            elif not self.sticky:
                self.prompts -= 1
                self.cursor = self.start
        return SimpleNamespace(stdout="", returncode=0)


@pytest.fixture
def pane(monkeypatch):
    """Factory installing one :class:`_FakeDialog` as the tmux + clock backend."""

    def _make(*args, **kwargs) -> _FakeDialog:
        fake = _FakeDialog(*args, **kwargs)
        monkeypatch.setattr(tmux, "run", fake.run)
        monkeypatch.setattr(launch_mod, "time", _Clock())
        return fake

    return _make


def _await(log: _Log | None = None) -> bool:
    cfg = SimpleNamespace(ready_marker=READY)
    return launch_mod.await_ready(cfg, PANE, log or _Log())


# --- reading the dialog -------------------------------------------------------


@pytest.mark.parametrize("name", ["claude-2.1.286-180col.txt", "claude-2.1.286-62col.txt"])
def test_the_2_1_286_dialog_is_read_from_a_real_capture(name):
    """Both files are ``capture-pane -p -J`` of a real session; at 62 columns
    claude's own wrapping splits "one you / trust?" across two rows."""
    dialog = launch_mod.read_trust_dialog((FIXTURES / name).read_text(encoding="utf-8"))
    assert dialog is not None
    assert dialog.options == NEW
    assert dialog.cursor == 0
    assert dialog.trust == 1


def test_the_earlier_wording_is_still_read(pane):
    fake = pane(OLD, head=OLD_HEAD, foot=OLD_FOOT, framed=True)
    dialog = launch_mod.read_trust_dialog(fake._screen())
    assert dialog is not None
    assert dialog.options == OLD
    assert (dialog.cursor, dialog.trust) == (0, 0)


def test_a_booted_pane_is_not_a_dialog(pane):
    fake = pane(prompts=0)
    assert launch_mod.read_trust_dialog(fake._screen()) is None


# --- answering it -------------------------------------------------------------


def test_the_2_1_286_dialog_is_answered_by_moving_to_trust(pane):
    """The regression: the cursor starts on "No, exit". Down, then Enter."""
    fake = pane()
    assert _await() is True
    assert fake.keys == ["Down", "Enter"]
    assert not fake.declined


def test_the_earlier_dialog_is_answered_with_enter_alone(pane):
    fake = pane(OLD, head=OLD_HEAD, foot=OLD_FOOT, framed=True)
    assert _await() is True
    assert fake.keys == ["Enter"]


def test_a_trust_answer_above_the_cursor_is_reached_with_up(pane):
    fake = pane(("Yes, I trust this folder", "No, exit"), start=1)
    assert _await() is True
    assert fake.keys == ["Up", "Enter"]


def test_a_longer_list_is_walked_one_step_at_a_time(pane):
    fake = pane(("No, exit", "No, show me the files first", "Yes, I trust this folder"))
    assert _await() is True
    assert fake.keys == ["Down", "Down", "Enter"]


@pytest.mark.parametrize("lag", [1, 3, 8])
def test_a_slow_repaint_never_gets_a_second_key(pane, lag):
    """A capture taken before the dialog repaints still shows the cursor on
    "No, exit". A second Down there wraps the cursor back onto it, and an Enter
    sent on a stale "Yes" would then answer No."""
    fake = pane(lag=lag)
    assert _await() is True
    assert fake.keys == ["Down", "Enter"]
    assert not fake.declined


def test_a_dialog_that_paints_late_is_still_answered(pane):
    fake = pane(after=6)
    assert _await() is True
    assert fake.keys == ["Down", "Enter"]


def test_a_dialog_that_asks_again_is_answered_again(pane):
    fake = pane(prompts=2)
    assert _await() is True
    assert fake.keys == ["Down", "Enter", "Down", "Enter"]
    assert not fake.declined


def test_the_earlier_dialog_asking_again_is_answered_again(pane):
    fake = pane(OLD, head=OLD_HEAD, foot=OLD_FOOT, framed=True, prompts=2)
    assert _await() is True
    assert fake.keys == ["Enter", "Enter"]


def test_an_answered_dialog_is_logged(pane):
    pane()
    log = _Log()
    assert _await(log) is True
    assert log.lines == [f"TRUST-ACCEPT pane={PANE} 'Yes, I trust this folder'"]


# --- when it cannot be answered -----------------------------------------------


def test_no_key_is_sent_when_the_cursor_cannot_be_read(pane):
    """Enter takes whatever is under the cursor, so without a cursor read from
    the pane nothing is pressed, and the timeout names the dialog."""
    fake = pane(mark=False)
    log = _Log()
    assert _await(log) is False
    assert fake.keys == []
    assert len(log.lines) == 1
    assert log.lines[0].startswith(f"READY-TIMEOUT pane={PANE} folder-trust dialog not answered: ")
    assert "No, exit" in log.lines[0]


def test_no_key_is_sent_when_no_option_trusts(pane):
    fake = pane(("No, exit", "Show me the files"))
    log = _Log()
    assert _await(log) is False
    assert fake.keys == []
    assert "folder-trust dialog not answered" in log.lines[-1]


def test_a_dialog_that_will_not_close_is_named_and_the_enters_are_bounded(pane):
    fake = pane(sticky=True)
    log = _Log()
    assert _await(log) is False
    assert not fake.declined
    assert fake.keys[0] == "Down"
    enters = fake.keys.count("Enter")
    assert set(fake.keys[1:]) == {"Enter"}
    assert 1 <= enters <= launch_mod.READY_TIMEOUT_S / launch_mod.TRUST_KEY_PATIENCE_S + 1
    assert log.lines[-1].startswith(f"READY-TIMEOUT pane={PANE} folder-trust dialog not answered: ")
    assert "Enter" in log.lines[-1]


def test_a_plain_timeout_stays_a_plain_timeout(pane):
    """No dialog and no banner: the line must not blame the trust dialog."""
    pane(after=10_000)
    log = _Log()
    assert _await(log) is False
    assert log.lines == [f"READY-TIMEOUT pane={PANE}"]


def test_readiness_is_never_declared_while_the_dialog_is_up(pane):
    """The banner text sitting above an unanswerable dialog is not a boot."""
    fake = pane(mark=False, head=f" {READY}\n" + NEW_HEAD)
    assert _await() is False
    assert fake.keys == []
