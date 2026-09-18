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
    discards them.
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
    ) -> None:
        self.calls: list[list[str]] = []
        self.typed: list[str] = []  # every payload we TRIED to put in the box
        self.buffers: dict[str, str] = {}
        self.transcript: list[str] = []
        self.contents = stray
        self.box = box
        self.box_after = box_after
        self.paint = paint
        self.pending = 0
        self.swallow = swallow
        self.echo = echo
        self.captures = 0

    def run(self, args, check=False, input_text=None):
        args = list(args)
        self.calls.append(args)
        if args[0] == "capture-pane":
            return self._capture()
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

    def _capture(self):
        self.captures += 1
        body = "\n".join(self.transcript)
        if not self._box_up():
            return _ok(body)
        shown = self.contents
        if self.pending > 0:
            self.pending -= 1
            shown = ""
        return _ok(f"{body}\n❯ {shown}" if body else f"❯ {shown}")

    def _keys(self, args):
        if "-l" in args:
            self._put(args[-1])
        elif args[-1] == "C-u":
            self.contents = ""
        elif args[-1] == "Enter":
            self._enter()
        return _ok()

    def _put(self, text: str) -> None:
        self.typed.append(text)
        if not self._box_up():
            return  # no box yet: claude drops the keystrokes on the floor
        self.contents += text
        self.pending = self.paint

    def _enter(self) -> None:
        if self.swallow > 0:
            self.swallow -= 1
            return
        if not self._box_up() or not self.contents:
            return
        if self.echo:
            self.transcript.append(f"> {self.contents}")
        self.contents = ""
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
def test_harden_sets_remain_on_exit_first(pane):
    """Set before any pane command runs, or a pane that crashed on startup just
    disappears and looks exactly like one that was never launched."""
    fake = pane()

    tmux.harden("swarm-test")

    opts = [c for c in fake.calls if c[0] == "set-option"]
    assert opts[0] == ["set-option", "-t", "=swarm-test", "-g", "remain-on-exit", "on"]


def test_kill_pane(pane):
    fake = pane()

    tmux.kill_pane("%7")

    assert fake.calls == [["kill-pane", "-t", "%7"]]
