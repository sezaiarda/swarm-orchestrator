"""Thin, side-effecting wrappers over the ``tmux`` CLI.

Every target is a captured *id* (``@N`` window, ``%N`` pane), never an index or
name, because ``claude`` rewrites pane titles and index-based targets would
break. Slots are identified by the per-pane ``@swarm_slot`` user option, so slot
accounting is tag-driven and immune to stray teammate panes.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Callable, Sequence

SLOT_OPT = "@swarm_slot"
#: The session option naming the state dir of the swarm that created the
#: session, so ``swarm down`` never ends a same-named session it does not own.
OWNER_OPT = "@swarm_state_dir"

# -- pane arrangements ----------------------------------------------------
# How a worker window arranges its slot panes. ``auto`` is the historical rule
# (1 = full window, 2 = LEFT|RIGHT, 3+ = tiled grid); every other value pins ONE
# tmux preset at every pane count, so two workers can be stacked TOP/BOTTOM
# instead of side-by-side. Set it in ``[tmux].layout`` or flip it live with
# ``swarm layout <name>``.
AUTO_LAYOUT = "auto"
TMUX_LAYOUTS = (
    "even-horizontal",
    "even-vertical",
    "tiled",
    "main-horizontal",
    "main-vertical",
)
LAYOUTS = (AUTO_LAYOUT, *TMUX_LAYOUTS)
# Plain-English spellings of the two arrangements people actually ask for.
LAYOUT_ALIASES = {
    "side-by-side": "even-horizontal",
    "left-right": "even-horizontal",
    "columns": "even-horizontal",
    "top-bottom": "even-vertical",
    "stacked": "even-vertical",
    "rows": "even-vertical",
    "grid": "tiled",
}
# The split direction that already matches each preset, so a window never
# flashes the wrong arrangement between the split and ``select-layout``. Tiled
# takes tmux's default direction (as it always has).
_SPLIT_FLAG = {
    "even-horizontal": "-h",
    "main-vertical": "-h",
    "even-vertical": "-v",
    "main-horizontal": "-v",
}


def normalize_layout(name: str) -> str:
    """Canonical layout name for ``name``, resolving aliases and case.

    Raises ``ValueError`` on an unknown name — a typo in ``[tmux].layout`` must
    fail loudly at config load, not silently arrange the panes some other way.
    """
    key = (name or AUTO_LAYOUT).strip().lower()
    key = LAYOUT_ALIASES.get(key, key)
    if key not in LAYOUTS:
        choices = ", ".join(LAYOUTS)
        aliases = ", ".join(sorted(LAYOUT_ALIASES))
        raise ValueError(
            f"unknown tmux layout {name!r}; choose one of: {choices} "
            f"(aliases: {aliases})"
        )
    return key


def preset_for(count: int, layout: str = AUTO_LAYOUT) -> str | None:
    """The ``select-layout`` preset for a ``count``-pane window, or ``None``.

    ``None`` means "leave the window alone": a lone pane already fills it. Under
    ``auto`` a pair gets even-horizontal columns and 3+ tile; an explicit layout
    is used verbatim at every count >= 2.
    """
    if count < 2:
        return None
    if layout == AUTO_LAYOUT:
        return "even-horizontal" if count == 2 else "tiled"
    return layout


#: Every tmux command here returns at once; one still running after this is a
#: hung server, and must not freeze the caller (the supervisor's only loop
#: thread among them).
TIMEOUT_S = 15.0
#: The return code a timed-out call reports (coreutils ``timeout``'s).
TIMEOUT_RC = 124


def run(
    args: Sequence[str], check: bool = False, input_text: str | None = None
) -> subprocess.CompletedProcess:
    """Invoke ``tmux`` with ``args``; capture text output.

    ``input_text`` is fed to the command's stdin (``load-buffer -`` is the only
    caller) so that every tmux invocation still goes through this one function
    and stays stubbable in tests. A call that outlives :data:`TIMEOUT_S` fails
    like any other tmux error, with :data:`TIMEOUT_RC`.
    """
    try:
        return subprocess.run(
            ["tmux", *args],
            check=check,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        err = f"tmux {' '.join(args[:1])} timed out after {TIMEOUT_S:.0f}s"
        if check:
            raise subprocess.CalledProcessError(TIMEOUT_RC, ["tmux", *args], "", err) from exc
        return subprocess.CompletedProcess(["tmux", *args], TIMEOUT_RC, "", err)


def pane_states(session: str) -> dict[str, bool] | None:
    """``{pane_id: dead}`` for every pane in ``session``; None when tmux cannot
    answer (server down, hung, or the session gone). A pane missing from a real
    answer is gone; None means nothing is known, and nothing may be inferred."""
    out = run(["list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id} #{pane_dead}"])
    if out.returncode != 0:
        return None
    states: dict[str, bool] = {}
    for line in out.stdout.splitlines():
        pane, _, dead = line.partition(" ")
        if pane:
            states[pane] = dead.strip() == "1"
    return states


def session_exists(session: str) -> bool:
    return run(["has-session", "-t", f"={session}"]).returncode == 0


def mark_owner(session: str, owner: str) -> None:
    """Record ``owner`` on ``session`` (see :data:`OWNER_OPT`). The target is
    ``=<name>:`` because an option's target is a window, and a bare ``=<name>``
    does not resolve as one."""
    run(["set-option", "-t", f"={session}:", OWNER_OPT, owner])


def session_owner(session: str) -> str | None:
    """The owner recorded on ``session``; ``""`` when it carries none (made before
    the marker existed, or not by a swarm); None when tmux cannot say (no such
    session, no server)."""
    out = run(["show-options", "-v", "-t", f"={session}:", OWNER_OPT])
    if out.returncode == 0:
        return out.stdout.strip()
    return "" if "invalid option" in out.stderr else None


def window_session(target: str) -> str | None:
    """The name of the session a window or pane id is in; None if tmux has no such id."""
    out = run(["display-message", "-p", "-t", target, "#{session_name}"])
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def kill_session(session: str) -> None:
    run(["kill-session", "-t", f"={session}"])


def session_pane_pids(session: str) -> list[int]:
    """The pid of every pane's process in ``session``, across all its windows."""
    out = run(["list-panes", "-s", "-t", f"={session}", "-F", "#{pane_pid}"])
    return [int(ln) for ln in out.stdout.split() if ln.isdigit()]


def session_pane_windows(session: str) -> list[tuple[int, str]]:
    """``(pane pid, window name)`` for every pane in ``session``."""
    out = run(["list-panes", "-s", "-t", f"={session}", "-F", "#{pane_pid}\t#{window_name}"])
    pairs: list[tuple[int, str]] = []
    for ln in out.stdout.splitlines():
        pid, _, name = ln.partition("\t")
        if pid.isdigit():
            pairs.append((int(pid), name))
    return pairs


def server_pid() -> int | None:
    """The pid of the tmux server; None when there is none to ask."""
    out = run(["display-message", "-p", "#{pid}"])
    pid = out.stdout.strip()
    return int(pid) if out.returncode == 0 and pid.isdigit() else None


def new_session(session: str) -> str:
    """Create a detached session; return the id of its initial window."""
    out = run(
        [
            "new-session",
            "-d",
            "-s",
            session,
            "-x",
            "200",
            "-y",
            "50",
            "-P",
            "-F",
            "#{window_id}",
        ],
        check=True,
    )
    return out.stdout.strip()


def harden(session: str) -> None:
    """Stop claude's title escapes from renaming our windows, and keep dead panes
    on screen.

    ``remain-on-exit`` is set FIRST because it only governs panes created after
    it: without it a pane whose command exits simply vanishes, so "the worker
    crashed on startup" and "the worker was never launched" look identical —
    an empty slot either way, with whatever the command printed gone with it.
    """
    run(["set-option", "-t", f"={session}", "-g", "remain-on-exit", "on"])
    run(["set-option", "-t", f"={session}", "-g", "automatic-rename", "off"])
    run(["set-option", "-t", f"={session}", "-g", "allow-rename", "off"])
    run(["set-option", "-t", f"={session}", "-g", "renumber-windows", "off"])


def rename_window(window_id: str, name: str) -> None:
    run(["rename-window", "-t", window_id, name])


def kill_window(window_id: str) -> None:
    """Kill one window by id (used to close a transient resolver pane)."""
    run(["kill-window", "-t", window_id])


def find_window(session: str, name: str) -> str | None:
    """The id of the window named ``name`` in ``session``, or ``None``.

    By name, not by a recorded id: window ids restart at ``@0`` with every tmux
    server, so an id recorded before a ``swarm down`` can name another window.
    """
    out = run(["list-windows", "-t", f"={session}", "-F", "#{window_id}\t#{window_name}"])
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        wid, _, wname = line.partition("\t")
        if wname == name:
            return wid
    return None


def window_alive(window_id: str) -> bool:
    """Does ``window_id`` exist with a pane whose process is still running?"""
    out = run(["list-panes", "-t", window_id, "-F", "#{pane_dead}"])
    return out.returncode == 0 and any(v.strip() == "0" for v in out.stdout.splitlines())


def kill_pane(pane_id: str) -> None:
    """Kill one pane by id (used when a window's slot count shrinks)."""
    run(["kill-pane", "-t", pane_id])


def new_window(
    session: str, name: str, hold: str = "sleep infinity", after: str | None = None
) -> str:
    """Create a window running a holding command; return its window id.

    ``after`` (a window id) places it right after that window, moving later ones
    up, instead of at the next free index."""
    out = run(
        [
            "new-window",
            "-d",
            *(["-a", "-t", after] if after else ["-t", f"={session}:"]),
            "-n",
            name,
            "-P",
            "-F",
            "#{window_id}",
            hold,
        ],
        check=True,
    )
    return out.stdout.strip()


def split_layout(
    window_id: str,
    count: int,
    layout: str = AUTO_LAYOUT,
    hold: str = "sleep infinity",
) -> list[str]:
    """Grow ``window_id`` to ``count`` panes arranged per ``layout``.

    ``count==1`` leaves the lone pane untouched (full window); otherwise the
    window is split ``count-1`` times in the direction that matches the resolved
    preset (see :func:`preset_for`) and locked to it with ``select-layout``.
    Returns pane ids ordered top-left-to-bottom-right so slot indices track the
    visible layout.
    """
    preset = preset_for(count, layout)
    if preset is not None:
        flag = _SPLIT_FLAG.get(preset)
        args = ["split-window", *([flag] if flag else []), "-t", window_id, hold]
        for _ in range(count - 1):
            run(args, check=True)
        run(["select-layout", "-t", window_id, preset])
    return _panes_ordered(window_id)


def window_of(pane_id: str) -> str:
    """The id of the window that currently holds ``pane_id``."""
    return run(["display-message", "-p", "-t", pane_id, "#{window_id}"]).stdout.strip()


def window_name_of(pane_id: str) -> str:
    """The name of the window that currently holds ``pane_id`` ("" if none)."""
    return run(["display-message", "-p", "-t", pane_id, "#{window_name}"]).stdout.strip()


def apply_layout(window_id: str, count: int, layout: str = AUTO_LAYOUT) -> None:
    """Re-apply ``layout``'s preset to an EXISTING window (no splitting) — used
    after a pane is broken out so the survivors re-tidy, and by ``swarm layout``
    to re-arrange live windows. Resolves the same preset :func:`split_layout`
    does, so a re-tidy never contradicts how the window was built."""
    preset = preset_for(count, layout)
    if preset is not None:
        run(["select-layout", "-t", window_id, preset])


def split_one(window_id: str, hold: str = "sleep infinity") -> str:
    """Split one fresh holding pane into ``window_id``; return its pane id."""
    out = run(
        ["split-window", "-t", window_id, "-P", "-F", "#{pane_id}", hold], check=True
    )
    return out.stdout.strip()


def park_pane(
    window_id: str,
    old_pane: str,
    slot_id: int | None,
    wait_name: str,
    session: str,
    layout: str = AUTO_LAYOUT,
) -> tuple[str, str]:
    """Move the LIVE ``old_pane`` into its own window ``wait_name`` while leaving
    ``window_id``'s slot filled by a fresh replacement pane tagged ``slot_id``
    (``None``: the operator or master window, which carry no slot tag).

    Split-FIRST (add the replacement BEFORE breaking the waiter out) so the window
    always carries >= 2 panes at break time: :func:`break_pane` on a *single*-pane
    window renames it in place and returns an empty id, which split-first avoids.
    Returns ``(wait_window_id, replacement_pane_id)``."""
    replacement = split_one(window_id)
    wait_win = break_pane(old_pane, wait_name, session)
    apply_layout(window_id, len(list_panes(window_id)), layout)
    if slot_id is not None:
        set_slot(replacement, slot_id)
    return wait_win, replacement


def break_pane(pane_id: str, name: str, session: str) -> str:
    """Move ``pane_id`` into a new detached window ``name`` under ``session``
    WITHOUT killing its process; the pane id and its ``@swarm_slot`` tag survive
    the move. Returns the new window id.

    ``-t`` is load-bearing, not decoration: the supervisor runs fully detached (no
    ``TMUX`` in its environment), so with no destination tmux falls back to the
    *most recently used* session — whichever one the owner happens to be attached
    to — and the parked worker lands in the owner's own session instead of the
    swarm's. An empty window part means "next free index" in ``session``."""
    out = run(
        [
            "break-pane",
            "-d",
            "-s",
            pane_id,
            "-n",
            name,
            "-t",
            f"={session}:",
            "-P",
            "-F",
            "#{window_id}",
        ],
        check=True,
    )
    return out.stdout.strip()


def list_panes(window_id: str) -> list[str]:
    out = run(["list-panes", "-t", window_id, "-F", "#{pane_id}"])
    return [ln for ln in out.stdout.splitlines() if ln]


def _panes_ordered(window_id: str) -> list[str]:
    """Pane ids sorted top-left-to-bottom-right (stable geometric order)."""
    out = run(
        ["list-panes", "-t", window_id, "-F", "#{pane_top}\t#{pane_left}\t#{pane_id}"]
    )
    rows: list[tuple[int, int, str]] = []
    for ln in out.stdout.splitlines():
        if not ln:
            continue
        top, left, pane = ln.split("\t")
        rows.append((int(top), int(left), pane))
    rows.sort()
    return [pane for _, _, pane in rows]


def list_panes_with_slot(window_id: str) -> list[tuple[str, str]]:
    """Return ``(pane_id, slot_tag)`` pairs; slot_tag is '' when untagged."""
    out = run(
        ["list-panes", "-t", window_id, "-F", f"#{{pane_id}}\t#{{{SLOT_OPT}}}"]
    )
    pairs: list[tuple[str, str]] = []
    for ln in out.stdout.splitlines():
        if not ln:
            continue
        pane, _, tag = ln.partition("\t")
        pairs.append((pane, tag))
    return pairs


def set_slot(pane_id: str, slot: int) -> None:
    run(["set-option", "-p", "-t", pane_id, SLOT_OPT, str(slot)], check=True)


def respawn_pane(pane_id: str, cmd: str, env: dict[str, str] | None = None) -> None:
    """Kill and respawn ``pane_id`` running ``cmd`` (pane id + tags survive)."""
    args = ["respawn-pane", "-k", "-t", pane_id]
    for key, val in (env or {}).items():
        args += ["-e", f"{key}={val}"]
    args.append(cmd)
    run(args, check=True)


def capture(pane_id: str) -> str:
    return run(["capture-pane", "-p", "-t", pane_id]).stdout


def send_literal(pane_id: str, text: str) -> None:
    run(["send-keys", "-t", pane_id, "-l", "--", text], check=True)


def send_enter(pane_id: str) -> None:
    run(["send-keys", "-t", pane_id, "Enter"], check=True)


def capture_joined(pane_id: str) -> str:
    """Capture pane text with wrapped lines joined (``-J``).

    A long typed line wraps across visual rows; joining makes it a single
    contiguous line so it can be matched as one substring.
    """
    return run(["capture-pane", "-p", "-J", "-t", pane_id]).stdout


def _poll(pred: Callable[[], bool], timeout: float, interval: float = 0.15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def await_text(pane_id: str, needle: str, timeout: float = 30.0) -> bool:
    """Poll (bounded) until ``needle`` appears in the pane — i.e. claude booted."""
    return _poll(lambda: needle in capture_joined(pane_id), timeout)


def _flat(text: str) -> str:
    """Collapse every run of whitespace to a single space.

    claude soft-wraps a long line in its OWN renderer (a newline plus indent),
    which ``capture-pane -J`` does not join because tmux never saw those rows as
    wrapped. Wraps land on spaces, so flattening both sides makes a wrapped
    message match the same needle an unwrapped one does."""
    return " ".join(text.split())


#: How claude's input box shows a long chunk of input: folded, the text hidden.
_FOLDED = re.compile(r"\[Pasted text #\d+")


def _box_holds(pane_id: str, head: str) -> bool:
    """True while claude's input box still holds the typed text.

    The box is the text after the LAST ``❯`` in the pane (past messages render
    above it); on submit claude clears the box. ``head`` is matched ANYWHERE in
    the box, never anchored at its start: a stray byte in front of the prompt —
    a terminal Device-Attributes reply like ``10;1c`` is the one caught live —
    survives ``lstrip`` and used to make this return False permanently for that
    pane, which turned every later check into a false "the box is empty, so it
    must have been submitted".

    Input that arrives as one chunk longer than about 800 characters is shown
    folded, as ``[Pasted text #1]``, and the text itself is nowhere in the box.
    That placeholder is the text too (the box was cleared before typing, so it is
    ours). Read as "not there", it made a swallowed Enter look like a box that
    let go of the text: one Enter, UNCONFIRMED, and the message sat unsent."""
    text = capture_joined(pane_id)
    idx = text.rfind("❯")
    if idx < 0:
        return False
    box = _flat(text[idx + 1 :])
    return _flat(head) in box or _FOLDED.search(box) is not None


#: One colour-and-attribute sequence in a capture taken with ``-e``.
_SGR = re.compile(r"\x1b\[([0-9;]*)m")


def _faint(faint: bool, codes: str) -> bool:
    """Whether text is rendered faint after one SGR sequence: ``2`` turns it on,
    ``22`` and a reset turn it off. A colour's own arguments (``38;5;2``) are
    skipped, or a palette index would read as an attribute."""
    items = codes.split(";")
    i = 0
    while i < len(items):
        code = items[i]
        if code in ("38", "48", "58"):
            i += {"5": 3, "2": 5}.get(items[i + 1] if i + 1 < len(items) else "", 1)
            continue
        if code == "2":
            faint = True
        elif code in ("", "0", "22"):
            faint = False
        i += 1
    return faint


def _box_text(pane_id: str) -> str | None:
    """What claude's input box holds right now, row by row; None with no box.

    The box runs from the last ``❯`` down to its lower edge, a row of ``─``; the
    status lines under that edge are not input. Faint text is left out: an empty
    box shows a hint there (``Try "how do I log an error?"``) that no key
    removes, and read as input it would make an empty box look full forever.
    Typed text and a folded paste are never faint. Rows are kept apart, blank
    ones too, because a key that removes only a line break still changed the
    box."""
    raw = run(["capture-pane", "-p", "-e", "-t", pane_id]).stdout
    shown = typed = ""
    faint, pos = False, 0
    for seq in [*_SGR.finditer(raw), None]:
        chunk = raw[pos : seq.start() if seq else len(raw)]
        shown += chunk
        typed += re.sub(r"[^\n]", " ", chunk) if faint else chunk
        if seq:
            faint, pos = _faint(faint, seq.group(1)), seq.end()
    idx = shown.rfind("❯")
    if idx < 0:
        return None
    rows = []
    for full, kept in zip(shown[idx + 1 :].split("\n"), typed[idx + 1 :].split("\n")):
        if full.strip() and not full.strip().strip("─"):
            break
        rows.append(kept.replace("\xa0", " ").rstrip())
    return "\n".join(rows)


def _transcript(pane_id: str) -> str:
    """The flattened pane text ABOVE the input box — where a submitted message
    lands. With no box painted the whole pane counts, so a needle there can only
    be new if it truly appeared."""
    text = capture_joined(pane_id)
    idx = text.rfind("❯")
    return _flat(text if idx < 0 else text[:idx])


def _has_box(pane_id: str) -> bool:
    """True once the pane renders claude's input box at all, whatever is in it.

    Distinguishes a pane that will never have a box (the fake test scripts) from
    one whose box has not painted yet — which :func:`send_submit` otherwise
    cannot tell apart, and which is the difference between a harmless no-op and
    a silently discarded launch."""
    return "❯" in capture_joined(pane_id)


# -- prompt injection -----------------------------------------------------
# What one injection PROVED. Only the first two are successes, and they are not
# the same success: NO_BOX means the pane renders no claude TUI at all (the fake
# test scripts), so there was nothing to verify against. The two failures are
# not the same either — UNCONFIRMED must never be retried, because re-typing
# into a live agent double-submits, which is worse than a stall.
DELIVERED = "delivered"  # the text was seen rendered ABOVE the input box
NO_BOX = "no-box"  # the pane never renders claude's ❯ box
UNCONFIRMED = "unconfirmed"  # the box let go of the text, nothing proved a submit
NOT_DELIVERED = "not-delivered"  # the text is still sitting in the box
BOX_NOT_CLEARED = "box-not-cleared"  # the box kept what it held; nothing was typed
SUBMIT_OK = (DELIVERED, NO_BOX)

# How long typed keystrokes get to render before Enter goes out. The one number
# that has to track how loaded the host is, so it is env-tunable: on a busy
# host painting routinely misses the old 0.5s, and a settle that expires early
# used to trigger the "retype the whole prompt" recovery on a box that was
# simply mid-paint.
DEFAULT_SETTLE = 1.5


def _settle_default(settle: float | None) -> float:
    """``settle`` verbatim, else ``$SWARM_SUBMIT_SETTLE``, else the default."""
    if settle is not None:
        return settle
    try:
        return float(os.environ.get("SWARM_SUBMIT_SETTLE", ""))
    except ValueError:
        return DEFAULT_SETTLE


#: The keys that empty the box, one screen row a press: ``C-u`` takes the row in
#: front of the cursor, ``C-k`` the row behind it.
_CLEAR_KEYS = ("C-u", "C-k")
#: The most keys one :func:`clear_box` sends after its first ``C-u``. A box is a
#: few rows; this only ends a box that something else keeps filling.
CLEAR_PRESSES = 64
#: How long one key gets to repaint the box before it counts as changing nothing.
CLEAR_WAIT = 1.0


def _press(pane_id: str, key: str, held: str) -> str | None:
    """Send one clearing key and return what the box holds once it repainted
    (``held`` again when the key changed nothing within :data:`CLEAR_WAIT`)."""
    run(["send-keys", "-t", pane_id, key])
    seen: list[str | None] = [held]

    def moved() -> bool:
        seen[0] = _box_text(pane_id)
        return seen[0] != held

    _poll(moved, CLEAR_WAIT)
    return seen[0]


def clear_box(pane_id: str) -> bool:
    """Empty the input box before typing into it; False if it would not empty.

    One ``C-u`` goes out first, unconditionally: a no-op on an empty box and the
    cheapest possible defence against stray bytes: a terminal query reply lands
    in the box as ordinary input, and text typed after one is no longer at
    column 0 — which submits ``/prime ...`` as plain chat instead of as a slash
    command. On an empty box that is all that is sent.

    That one key is not the whole clear, though: ``C-u`` removes the screen row
    in front of the cursor, not the input. A line wider than the pane (every
    pointer line the swarm types, on a narrow pane) lost its last row and kept
    the rest, and the next message was typed behind it and submitted with it. So
    the box is read back and the key repeated while it still holds something;
    once ``C-u`` stops changing it (the cursor is at the start, text behind it)
    ``C-k`` takes over. Bounded by :data:`CLEAR_PRESSES`, and a box neither key
    changes any more (a dialog has the pane, not the input) ends it at once.

    True also when the pane renders no box at all: there is nothing to read,
    and the keys typed next are that pane's whole delivery."""
    run(["send-keys", "-t", pane_id, "C-u"], check=True)
    held = _box_text(pane_id)
    key = stalled = 0
    for _ in range(CLEAR_PRESSES):
        if not (held or "").strip():
            return True
        now = _press(pane_id, _CLEAR_KEYS[key], held)
        if now != held:
            held, stalled = now, 0
            continue
        stalled += 1
        if stalled == len(_CLEAR_KEYS):
            return False
        key = (key + 1) % len(_CLEAR_KEYS)
    return not (held or "").strip()


def _paste(pane_id: str, text: str) -> bool:
    """Deliver ``text`` into the box as ONE atomic chunk via a paste buffer.

    Typing a slash command character by character opens claude's
    slash-command autocomplete, and the dropdown can swallow the Enter that
    follows — and every worker prompt starts with ``/prime``. A paste arrives
    as a single chunk with no per-character autocomplete. Returns False if tmux
    would not take the buffer so the caller can fall back to plain typing."""
    buf = f"swarm-{pane_id.lstrip('%')}"
    if run(["load-buffer", "-b", buf, "-"], input_text=text).returncode != 0:
        return False
    return run(["paste-buffer", "-d", "-b", buf, "-t", pane_id]).returncode == 0


def _type(pane_id: str, text: str) -> None:
    """Put ``text`` in the input box, pasting it when typing it would trip
    claude's autocomplete (a ``/`` command or an ``@`` file reference)."""
    if (text.startswith("/") or "@" in text) and _paste(pane_id, text):
        return
    send_literal(pane_id, text)


def _enter(pane_id: str) -> None:
    """Unchecked Enter — a pane that exits mid-verify must not raise into the
    caller."""
    run(["send-keys", "-t", pane_id, "Enter"])


def _fill(pane_id: str, text: str, head: str, settle: float) -> bool:
    """Put ``text`` in the (cleared) input box and report whether it showed."""
    _type(pane_id, text)
    landed = _poll(lambda: _box_holds(pane_id, head), settle)
    if landed:
        time.sleep(0.2)  # keystrokes rendered; brief settle before Enter
    return landed


def _submit(pane_id: str, head: str, tries: int) -> str:
    """Press Enter until the pane PROVES the message left the input box.

    Proof is positive: the message has to appear above the box, compared against
    a snapshot taken before the first Enter (occurrence counts, so re-sending the
    same prompt into a pane that already shows it still registers). The old
    predicate — "the box no longer holds the text" — cannot tell *submitted*
    from *never landed*, and a box holding one stray byte satisfied it on the
    very first poll, so a lost launch reported success.

    Re-pressing Enter is always safe (a no-op on an empty box, the submit we
    wanted on a full one), so this never re-types. A box that quietly emptied
    without the message showing up is UNCONFIRMED, and UNCONFIRMED is where we
    STOP."""
    needle = _flat(head)
    before = _transcript(pane_id).count(needle)
    for _ in range(tries):
        _enter(pane_id)
        if _poll(lambda: _transcript(pane_id).count(needle) > before, 2.0):
            return DELIVERED
        if not _box_holds(pane_id, head):
            return UNCONFIRMED
    return NOT_DELIVERED


def send_submit_ex(
    pane_id: str,
    text: str,
    settle: float | None = None,
    tries: int = 4,
    box_wait: float = 6.0,
) -> str:
    """Type ``text`` into a pane, submit it, and report what was PROVEN.

    Returns :data:`DELIVERED`, :data:`NO_BOX`, :data:`UNCONFIRMED`,
    :data:`NOT_DELIVERED` or :data:`BOX_NOT_CLEARED`; see those constants for
    what each licenses the caller to do.

    The box is emptied first (:func:`clear_box`). A box that will not empty gets
    nothing typed into it: text typed behind what it holds is submitted with it,
    as one message.

    Exactly one path re-types the prompt, and it is this case: the pane had
    no input box at all when the keystrokes arrived,
    so claude DISCARDED them. That is costly to spot — right env, right worktree,
    claude running, empty prompt and a clean LAUNCH in the log — because
    `await_ready` waits for the version banner, which claude prints BEFORE its
    box accepts input, so a notice rendered after the banner swallows the text.
    A box that merely painted slowly is NOT that case and is never re-typed."""
    settle = _settle_default(settle)
    head = text[:40]
    had_box = _has_box(pane_id)
    if not clear_box(pane_id):
        return BOX_NOT_CLEARED
    if not _fill(pane_id, text, head, settle) and not had_box:
        # Nothing showed in a box, and there was no box to show it in when the
        # text went out: either this pane never renders one (a plain shell, the
        # fake test scripts) or claude's box painted too late. The single Enter
        # goes out either way — it is the whole delivery on a boxless pane — and
        # it may also submit text that landed unpainted, so check for that
        # before concluding the send went nowhere.
        before = _transcript(pane_id).count(_flat(head))
        _enter(pane_id)
        if _poll(lambda: _transcript(pane_id).count(_flat(head)) > before, 1.0):
            return DELIVERED
        if not _poll(lambda: _has_box(pane_id), box_wait):
            return NO_BOX
        if not clear_box(pane_id):
            return BOX_NOT_CLEARED
        _fill(pane_id, text, head, settle)  # box exists now; the first send was lost
    return _submit(pane_id, head, tries)


def send_submit(
    pane_id: str,
    text: str,
    settle: float | None = None,
    tries: int = 4,
    box_wait: float = 6.0,
) -> bool:
    """:func:`send_submit_ex` flattened to the bool its callers still take."""
    return send_submit_ex(pane_id, text, settle, tries, box_wait) in SUBMIT_OK


