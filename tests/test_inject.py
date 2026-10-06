"""Hermetic coverage for prompt injection into a claude pane.

``send_submit`` shells out for every step, so :func:`swarm_orchestrator.tmux.run`
is stubbed with a tiny model of claude's TUI — a transcript, an input box, and
knobs for the failure modes that actually bit us live: stray bytes sitting in the
box, a box that paints late, an Enter the TUI swallows. ``tmux.time`` is stubbed
too, so a 1.5s settle costs nothing and every poll runs a fixed number of
iterations.

The regressions pinned here all produced the SAME symptom — a clean LAUNCH line
for a worker that never got its prompt, or got it twice — because the old
verifier only asked "is the box empty now?", which a corrupted box answers "yes"
immediately and a discarded send answers "yes" forever.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from swarm_orchestrator import tmux


def _ok(stdout: str = ""):
    return SimpleNamespace(stdout=stdout, returncode=0)


#: Claude Code 2.1.286's folder-trust question: a ``❯`` that is not the input box.
_DIALOG = (
    " Quick safety check: Is this a project you created or one you trust?\n"
    " ❯ No, exit\n   Yes, I trust this folder\n\n Enter to confirm · Esc to cancel"
)


class _Clock:
    """Deterministic stand-in for ``time``: sleeping just moves the clock, so
    polls complete instantly and always run the same number of iterations."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs


class _FakeClaude:
    """One pane's worth of claude TUI: a transcript above, an input box below.

    Knobs: ``stray`` seeds the box with junk that no keystroke put there (a
    terminal Device-Attributes reply), ``box_after`` delays the box painting at
    all, ``paint`` delays typed text becoming visible in a box that already
    exists, ``swallow`` eats Enters, ``echo=False`` submits without rendering the
    message. Keystrokes typed before the box exists are DISCARDED, as claude
    discards them. ``fold`` is the length past which one chunk of input is shown
    in the box as ``[Pasted text #N]`` instead of as its text (about 800
    characters on the real TUI); submitted, it renders in full above the box.

    The box is drawn as the real one is: ``❯``, the text wrapped at ``width``
    columns, a lower edge of ``─`` and a status line under it. The two clearing
    keys work a screen ROW at a time, as measured on Claude Code 2.1.286:
    ``C-u`` removes the row in front of the cursor, ``C-k`` the row behind it,
    and neither touches the other side. ``hint`` is the faint suggestion an
    empty box shows (only a capture with ``-e`` can tell it from typed text),
    ``dialog`` puts a selection dialog in the pane instead of the box (its own
    ``❯``, no key changes it), ``lag`` is how many captures still show the old
    box after a clearing key, and ``refill`` has somebody else typing into the
    box after every clearing key.
    """

    def __init__(
        self,
        *,
        box: bool = True,
        stray: str = "",
        box_after: int = 0,
        paint: int = 0,
        swallow: int = 0,
        echo: bool = True,
        fold: int = 0,
        width: int = 80,
        hint: str = "",
        dialog: bool = False,
        lag: int = 0,
        refill: bool = False,
    ) -> None:
        self.calls: list[list[str]] = []
        self.typed: list[str] = []  # every payload we TRIED to put in the box
        self.buffers: dict[str, str] = {}
        self.transcript: list[str] = []
        self.contents = stray
        self.cursor = len(stray)
        self.width = width
        self.hint = hint
        self.dialog = dialog
        self.lag = lag
        self.lagging = 0
        self.before_key = ("", 0)  # what the box showed before the last clearing key
        self.refill = refill
        self.kills = 0
        self.box = box
        self.box_after = box_after
        self.paint = paint
        self.pending = 0
        self.swallow = swallow
        self.echo = echo
        self.fold = fold
        self.folded = 0  # the paste number the box shows instead of its text
        self.pastes = 0
        self.captures = 0

    def run(self, args, check=False, input_text=None):
        args = list(args)
        self.calls.append(args)
        if args[0] == "capture-pane":
            return self._capture("-e" in args)
        if args[0] == "send-keys":
            return self._keys(args)
        if args[0] == "load-buffer":
            self.buffers[args[args.index("-b") + 1]] = input_text
            return _ok()
        if args[0] == "paste-buffer":
            self._put(self.buffers.pop(args[args.index("-b") + 1], ""))
            return _ok()
        return _ok()

    # -- the pane's own behaviour ------------------------------------------
    def _box_up(self) -> bool:
        return self.box and self.captures >= self.box_after

    def _capture(self, colour: bool = False):
        self.captures += 1
        body = "\n".join(self.transcript)
        if self.dialog:
            return _ok(f"{body}\n{_DIALOG}")
        if not self._box_up():
            return _ok(body)
        contents, folded = self.contents, self.folded
        if self.lagging > 0:
            self.lagging -= 1
            contents, folded = self.before_key
        if folded:
            shown = f"[Pasted text #{folded}]"
        elif contents:
            rows = [contents[i : i + self.width] for i in range(0, len(contents), self.width)]
            shown = "\n  ".join(rows)
        else:
            shown = f"\x1b[2m{self.hint}\x1b[0m" if colour else self.hint
        if self.pending > 0:
            self.pending -= 1
            shown = ""
        edge = "─" * self.width
        if colour:
            box = (
                f"\x1b[39m❯\xa0{shown}\n\x1b[38;5;244m{edge}\n"
                "\x1b[39m  \x1b[38;5;180mOpus 5.5\x1b[38;5;2m  2%\x1b[39m"
            )
        else:
            box = f"❯\xa0{shown}\n{edge}\n  Opus 5.5  2%"
        return _ok(f"{body}\n{box}" if body else box)

    def _keys(self, args):
        if "-l" in args:
            self._put(args[-1])
        elif args[-1] in ("C-u", "C-k"):
            self._kill(args[-1])
        elif args[-1] == "Enter":
            self._enter()
        return _ok()

    def hold(self, text: str, cursor: int | None = None) -> None:
        """Leave ``text`` in the box, the cursor at its end unless told where."""
        self.contents = text
        self.cursor = len(text) if cursor is None else cursor

    def _kill(self, key: str) -> None:
        """One clearing key: a screen row on one side of the cursor."""
        if self.dialog or not self._box_up():
            return
        self.kills += 1
        self.before_key = (self.contents, self.folded)
        self.lagging = self.lag
        cur = self.cursor
        if self.folded:  # a folded paste is one row, whatever its length
            self.contents, self.folded, cur = "", 0, 0
        elif key == "C-u":
            start = (cur - 1) // self.width * self.width if cur else 0
            self.contents = self.contents[:start] + self.contents[cur:]
            cur = start
        else:
            end = (cur // self.width + 1) * self.width
            self.contents = self.contents[:cur] + self.contents[end:]
        if self.refill:
            self.contents += f"more{self.kills} "
        self.cursor = cur

    def _put(self, text: str) -> None:
        self.typed.append(text)
        if self.dialog or not self._box_up():
            return  # no box yet: claude drops the keystrokes on the floor
        self.contents = self.contents[: self.cursor] + text + self.contents[self.cursor :]
        self.cursor += len(text)
        self.pending = self.paint
        if self.fold and len(text) > self.fold:
            self.pastes += 1
            self.folded = self.pastes

    def _enter(self) -> None:
        if self.swallow > 0:
            self.swallow -= 1
            return
        if self.dialog or not self._box_up() or not self.contents:
            return
        if self.echo:
            self.transcript.append(f"> {self.contents}")
        self.contents = ""
        self.cursor = 0
        self.folded = 0
        self.pending = 0


@pytest.fixture
def pane(monkeypatch):
    """Factory installing one :class:`_FakeClaude` as the tmux + clock backend."""
    monkeypatch.delenv("SWARM_SUBMIT_SETTLE", raising=False)

    def _make(**kwargs) -> _FakeClaude:
        fake = _FakeClaude(**kwargs)
        monkeypatch.setattr(tmux, "run", fake.run)
        monkeypatch.setattr(tmux, "time", _Clock())
        return fake

    return _make


def _enters(fake: _FakeClaude) -> list[list[str]]:
    return [c for c in fake.calls if c[0] == "send-keys" and c[-1] == "Enter"]


def _literals(fake: _FakeClaude) -> list[list[str]]:
    return [c for c in fake.calls if c[0] == "send-keys" and "-l" in c]


# -- the box-matching predicate -------------------------------------------
def test_box_holds_matches_past_stray_bytes(pane):
    """The defect verbatim: a Device-Attributes reply in front of the prompt.

    ``lstrip().startswith()`` returned False here forever, which made "the box
    no longer holds the text" true on the first poll of every later check."""
    fake = pane()
    fake.contents = "10;1c/prime demo-P3"

    assert tmux._box_holds("%1", "/prime demo-P3") is True


def test_box_holds_sees_a_soft_wrapped_line(pane):
    """claude wraps in its own renderer, which ``capture-pane -J`` cannot join."""
    fake = pane()
    fake.contents = "read the ledger and\n   follow it exactly"

    assert tmux._box_holds("%1", "read the ledger and follow it") is True


# -- send_submit_ex outcomes ----------------------------------------------
def test_stray_bytes_still_deliver_exactly_once(pane):
    fake = pane(stray="10;1c")

    assert tmux.send_submit_ex("%1", "/prime demo-P3") == tmux.DELIVERED
    assert fake.typed == ["/prime demo-P3"]  # never retyped
    assert fake.transcript == ["> /prime demo-P3"]  # the stray never got submitted
    assert len(_enters(fake)) == 1


def test_late_paint_does_not_resend(pane):
    """A box that exists but renders slowly is NOT a discarded send.

    The old code could not tell the two apart and retyped + resubmitted the whole
    prompt whenever painting outran the settle — routine on a loaded host."""
    fake = pane(paint=40)

    assert tmux.send_submit_ex("%1", "hello world prompt") == tmux.DELIVERED
    assert fake.typed == ["hello world prompt"]
    assert len(_enters(fake)) == 1


def test_boxless_pane_reports_no_box(pane):
    """The fake worker scripts: no ``❯`` ever, so nothing can be verified. One
    Enter still goes out — on such a pane it IS the delivery."""
    fake = pane(box=False)

    assert tmux.send_submit_ex("%1", "hello") == tmux.NO_BOX
    assert fake.typed == ["hello"]
    assert len(_enters(fake)) == 1


def test_late_box_retypes_once(pane):
    """A late box: it painted after the keystrokes, so they were dropped.
    This is the ONLY path allowed to type the prompt a second time."""
    fake = pane(box_after=6)

    assert tmux.send_submit_ex("%1", "hello world") == tmux.DELIVERED
    assert fake.typed == ["hello world", "hello world"]
    assert fake.transcript == ["> hello world"]


def test_stuck_text_reports_not_delivered(pane):
    """Every Enter swallowed: retry the Enter, never the text."""
    fake = pane(swallow=99)

    assert tmux.send_submit_ex("%1", "hello world") == tmux.NOT_DELIVERED
    assert fake.typed == ["hello world"]
    assert len(_enters(fake)) == 4  # == tries
    assert fake.contents == "hello world"  # still sitting there, unsent


def test_silent_submit_is_unconfirmed_and_not_retried(pane):
    """The box emptied but nothing rendered above it: cannot prove either way.

    Stopping here is the point — a resend into a live agent double-submits,
    which is worse than a stall."""
    fake = pane(echo=False)

    assert tmux.send_submit_ex("%1", "hello world") == tmux.UNCONFIRMED
    assert fake.typed == ["hello world"]
    assert len(_enters(fake)) == 1


# -- a brief long enough for claude to fold it into "[Pasted text #N]" -------
# The box then shows the placeholder, never the text. Reading that as "the box
# let go of the text" is what stranded a merge resolver: one Enter went out, the
# TUI swallowed it into the paste, and the verdict was UNCONFIRMED — where the
# sender stops — with the whole brief still sitting in the box, unsent.
_LONG = "Read the resolver prompt and follow it exactly. " + "x" * 1200


def test_a_folded_paste_counts_as_text_in_the_box(pane):
    fake = pane(fold=800)
    fake.run(["send-keys", "-t", "%1", "-l", "--", _LONG])

    assert "[Pasted text #1]" in tmux.capture_joined("%1")
    assert tmux._box_holds("%1", _LONG[:40])


def test_a_folded_paste_is_delivered(pane):
    """Fold alone is survivable: the Enter submits and the text renders above."""
    fake = pane(fold=800)

    assert tmux.send_submit_ex("%1", _LONG) == tmux.DELIVERED
    assert fake.typed == [_LONG]
    assert fake.contents == ""


def test_a_swallowed_enter_on_a_folded_paste_is_pressed_again(pane):
    """The stranded resolver: folded brief, first Enter eaten. The text is still
    in the box, so the Enter is retried (never the text) until it submits."""
    fake = pane(fold=800, swallow=1)

    assert tmux.send_submit_ex("%1", _LONG) == tmux.DELIVERED
    assert fake.typed == [_LONG]  # typed once: a second copy would submit twice
    assert len(_enters(fake)) == 2
    assert fake.contents == ""


def test_a_folded_paste_that_never_submits_is_not_delivered(pane):
    """Still sitting in the box after every Enter is NOT_DELIVERED, which says
    where the text is. UNCONFIRMED would claim the box let go of it."""
    fake = pane(fold=800, swallow=99)

    assert tmux.send_submit_ex("%1", _LONG) == tmux.NOT_DELIVERED
    assert fake.typed == [_LONG]
    assert len(_enters(fake)) == 4  # == tries
    assert fake.contents == _LONG


def test_repeat_prompt_is_proven_by_a_new_occurrence(pane):
    """Injecting the same line twice must not be confirmed by the FIRST copy
    still showing in the transcript."""
    fake = pane()
    fake.transcript.append("> hello world")

    assert tmux.send_submit_ex("%1", "hello world") == tmux.DELIVERED
    assert fake.transcript == ["> hello world", "> hello world"]


# -- how the text gets into the box ---------------------------------------
def test_box_is_cleared_before_typing(pane):
    fake = pane(stray="junk")

    tmux.send_submit_ex("%1", "hello")

    clear = next(
        i for i, c in enumerate(fake.calls) if c[0] == "send-keys" and c[-1] == "C-u"
    )
    typed = next(
        i
        for i, c in enumerate(fake.calls)
        if c[0] == "paste-buffer" or (c[0] == "send-keys" and "-l" in c)
    )
    assert clear < typed


# -- emptying the box before typing ---------------------------------------
# One C-u removes one screen row, not the input. Measured on Claude Code 2.1.286
# at 180 columns: a ten-row line lost a row a press and needed ten. A pointer
# line is wider than a narrow pane, so one left in the box after a lost send
# kept its first rows, and the next message was typed behind them and submitted
# with them as one.
_POINTER = "Read the file /state/operator/brief.md and follow it exactly. " * 4


def _clears(fake: _FakeClaude) -> list[str]:
    return [c[-1] for c in fake.calls if c[0] == "send-keys" and c[-1] in ("C-u", "C-k")]


def test_clear_box_empties_a_box_of_nine_rows(pane):
    fake = pane(width=80)
    fake.hold("x" * 700)  # nine rows of 80

    cleared = tmux.clear_box("%1")

    assert fake.contents == ""
    assert cleared is True
    assert _clears(fake) == ["C-u"] * 9


def test_a_line_left_in_the_box_is_not_submitted_with_the_next_message(pane):
    """The harm itself: an earlier send ended NOT_DELIVERED, its line still in
    the box, and the next injection into that pane ran the two together."""
    fake = pane(width=80, swallow=4)
    assert tmux.send_submit_ex("%1", _POINTER) == tmux.NOT_DELIVERED
    assert fake.contents == _POINTER

    assert tmux.send_submit_ex("%1", "second message here") == tmux.DELIVERED
    assert fake.transcript == ["> second message here"]


@pytest.mark.parametrize("cursor", [0, 130, 240])
def test_text_behind_the_cursor_is_cleared_too(pane, cursor):
    """C-u never removes what is behind the cursor; with the cursor moved off
    the end, C-u alone leaves the box full however often it is pressed."""
    fake = pane(width=80)
    fake.hold("y" * 300, cursor=cursor)

    cleared = tmux.clear_box("%1")

    assert fake.contents == ""
    assert cleared is True
    assert "C-k" in _clears(fake)


def test_an_empty_box_gets_one_ctrl_u_and_nothing_more(pane):
    fake = pane()

    assert tmux.clear_box("%1") is True
    assert _clears(fake) == ["C-u"]


def test_stray_bytes_are_cleared_by_the_one_ctrl_u(pane):
    fake = pane(stray="10;1c")

    assert tmux.clear_box("%1") is True
    assert _clears(fake) == ["C-u"]


def test_a_faint_hint_is_an_empty_box(pane):
    """A fresh session's empty box shows a suggestion no key removes. Read as
    input it would refuse every send into a new session."""
    fake = pane(hint='Try "how do I log an error?"')
    assert 'Try "how do I log an error?"' in tmux.capture("%1")

    assert tmux.clear_box("%1") is True
    assert _clears(fake) == ["C-u"]
    assert tmux.send_submit_ex("%1", "hello world") == tmux.DELIVERED
    assert fake.transcript == ["> hello world"]


def test_the_status_line_under_the_box_is_not_input(pane):
    pane()

    assert "Opus 5.5" in tmux.capture("%1")
    assert tmux._box_text("%1").strip() == ""


def test_a_boxless_pane_gets_one_ctrl_u(pane):
    fake = pane(box=False)

    assert tmux.clear_box("%1") is True
    assert _clears(fake) == ["C-u"]


def test_a_slow_repaint_is_not_a_key_that_does_nothing(pane):
    """The capture straight after a key can still show the box as it was."""
    fake = pane(width=80, lag=3)
    fake.hold("x" * 700)

    cleared = tmux.clear_box("%1")

    assert fake.contents == ""
    assert cleared is True


def test_a_box_that_will_not_empty_gets_nothing_typed(pane):
    """A dialog has the pane: its ``❯`` is a menu cursor and no key changes it.
    Typing a message there and pressing Enter answers the dialog."""
    fake = pane(dialog=True)

    assert tmux.clear_box("%1") is False
    assert len(_clears(fake)) == 3  # the first C-u, then each key once more

    assert tmux.send_submit_ex("%1", "hello world") == tmux.BOX_NOT_CLEARED
    assert fake.typed == []
    assert _enters(fake) == []


def test_clearing_is_bounded(pane):
    """Somebody else typing into the box: it changes at every key and never
    empties. The clear gives up instead of pressing for ever."""
    fake = pane(width=80, refill=True)
    fake.hold("x" * 200)

    assert tmux.clear_box("%1") is False
    assert len(_clears(fake)) == 1 + tmux.CLEAR_PRESSES


@pytest.mark.parametrize(
    "codes, before, after",
    [
        ("2", False, True),
        ("1;2", False, True),
        ("0", True, False),
        ("", True, False),
        ("22", True, False),
        ("39", True, True),
        ("38;5;2", False, False),  # palette colour 2, not the faint attribute
        ("38;2;2;2;2", False, False),  # an RGB colour
        ("38;5;2;2", False, True),
    ],
)
def test_faint_follows_the_attribute_not_a_colour_argument(codes, before, after):
    assert tmux._faint(before, codes) is after


@pytest.mark.parametrize(
    "text, pasted",
    [
        ("/prime demo-P3", True),  # typing "/" opens claude's autocomplete
        ("read @ledger.txt then start", True),  # "@" opens the file picker
        ("plain prompt text", False),
    ],
)
def test_autocomplete_triggers_go_through_a_paste_buffer(pane, text, pasted):
    fake = pane()

    tmux.send_submit_ex("%1", text)

    loads = [c for c in fake.calls if c[0] == "load-buffer"]
    pastes = [c for c in fake.calls if c[0] == "paste-buffer"]
    assert bool(loads) is pasted
    assert bool(pastes) is pasted
    assert bool(_literals(fake)) is not pasted
    assert fake.typed == [text]
    if pasted:
        assert loads[0] == ["load-buffer", "-b", "swarm-1", "-"]
        assert "-d" in pastes[0]  # the buffer is dropped after pasting
        assert fake.buffers == {}


def test_paste_falls_back_to_typing(pane, monkeypatch):
    """A tmux that refuses the buffer must not cost us the prompt."""
    fake = pane()
    real = fake.run

    def refuse(args, check=False, input_text=None):
        if list(args)[0] == "load-buffer":
            fake.calls.append(list(args))
            return SimpleNamespace(stdout="", returncode=1)
        return real(args, check=check, input_text=input_text)

    monkeypatch.setattr(tmux, "run", refuse)

    assert tmux.send_submit_ex("%1", "/prime demo-P3") == tmux.DELIVERED
    assert _literals(fake)[0][-1] == "/prime demo-P3"


# -- knobs and the bool the callers still take ----------------------------
@pytest.mark.parametrize(
    "result, ok",
    [
        (tmux.DELIVERED, True),
        (tmux.NO_BOX, True),
        (tmux.UNCONFIRMED, False),
        (tmux.NOT_DELIVERED, False),
        (tmux.BOX_NOT_CLEARED, False),
    ],
)
def test_send_submit_bool_mapping(monkeypatch, result, ok):
    monkeypatch.setattr(tmux, "send_submit_ex", lambda *a, **k: result)
    assert tmux.send_submit("%1", "x") is ok


def test_settle_default_and_override(monkeypatch):
    monkeypatch.delenv("SWARM_SUBMIT_SETTLE", raising=False)
    assert tmux.DEFAULT_SETTLE == 1.5
    assert tmux._settle_default(None) == 1.5
    assert tmux._settle_default(0.25) == 0.25
    monkeypatch.setenv("SWARM_SUBMIT_SETTLE", "4.5")
    assert tmux._settle_default(None) == 4.5
    monkeypatch.setenv("SWARM_SUBMIT_SETTLE", "slowly")
    assert tmux._settle_default(None) == tmux.DEFAULT_SETTLE


def test_settle_env_shortens_the_wait(pane, monkeypatch):
    """The env knob has to reach the poll, not just parse."""
    short = pane(paint=99, swallow=99)
    monkeypatch.setenv("SWARM_SUBMIT_SETTLE", "0.15")
    tmux.send_submit_ex("%1", "hello world", tries=1)

    long_ = pane(paint=99, swallow=99)
    monkeypatch.delenv("SWARM_SUBMIT_SETTLE")
    tmux.send_submit_ex("%1", "hello world", tries=1)

    assert long_.captures > short.captures


# -- session hardening ----------------------------------------------------
def test_a_window_is_hardened_by_itself_and_remain_on_exit_comes_first(pane):
    """Set before any pane command runs, or a pane that crashed on startup just
    disappears and looks exactly like one that was never launched. And set on
    the window alone: the tmux server is the owner's, shared with their own
    sessions and with other swarms."""
    fake = pane()

    tmux.harden_window("@7")

    assert fake.calls == [["set-option", "-w", "-t", "@7", "remain-on-exit", "on"],
                          ["set-option", "-w", "-t", "@7", "automatic-rename", "off"],
                          ["set-option", "-w", "-t", "@7", "allow-rename", "off"]]


def test_hardening_a_session_sets_nothing_server_wide(pane):
    fake = pane()

    tmux.harden("swarm-test")

    opts = [c for c in fake.calls if c[0] == "set-option"]
    assert opts and not [c for c in opts if "-g" in c]
    assert opts[-1] == ["set-option", "-t", "=swarm-test:", "renumber-windows", "off"]


def test_kill_pane(pane):
    fake = pane()

    tmux.kill_pane("%7")

    assert fake.calls == [["kill-pane", "-t", "%7"]]
