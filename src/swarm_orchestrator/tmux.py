"""Thin, side-effecting wrappers over the ``tmux`` CLI.

Every target is a captured *id* (``@N`` window, ``%N`` pane), never an index or
name, because ``claude`` rewrites pane titles and index-based targets would
break. Slots are identified by the per-pane ``@swarm_slot`` user option, so slot
accounting is tag-driven and immune to stray teammate panes.
"""

from __future__ import annotations

import subprocess
import time
from typing import Callable, Sequence

SLOT_OPT = "@swarm_slot"

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


def run(args: Sequence[str], check: bool = False) -> subprocess.CompletedProcess:
    """Invoke ``tmux`` with ``args``; capture text output."""
    return subprocess.run(
        ["tmux", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def session_exists(session: str) -> bool:
    return run(["has-session", "-t", f"={session}"]).returncode == 0


def kill_session(session: str) -> None:
    run(["kill-session", "-t", f"={session}"])


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
    """Stop claude's title escapes from renaming our windows."""
    run(["set-option", "-t", f"={session}", "-g", "automatic-rename", "off"])
    run(["set-option", "-t", f"={session}", "-g", "allow-rename", "off"])
    run(["set-option", "-t", f"={session}", "-g", "renumber-windows", "off"])


def rename_window(window_id: str, name: str) -> None:
    run(["rename-window", "-t", window_id, name])


def kill_window(window_id: str) -> None:
    """Kill one window by id (used to close a transient resolver pane)."""
    run(["kill-window", "-t", window_id])


def new_window(session: str, name: str, hold: str = "sleep infinity") -> str:
    """Create a window running a holding command; return its window id."""
    out = run(
        [
            "new-window",
            "-d",
            "-t",
            f"={session}:",
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
    slot_id: int,
    wait_name: str,
    layout: str = AUTO_LAYOUT,
) -> tuple[str, str]:
    """Move the LIVE ``old_pane`` into its own window ``wait_name`` while leaving
    ``window_id``'s slot filled by a fresh replacement pane tagged ``slot_id``.

    Split-FIRST (add the replacement BEFORE breaking the waiter out) so the window
    always carries >= 2 panes at break time: :func:`break_pane` on a *single*-pane
    window renames it in place and returns an empty id, which split-first avoids.
    Returns ``(wait_window_id, replacement_pane_id)``."""
    replacement = split_one(window_id)
    wait_win = break_pane(old_pane, wait_name)
    apply_layout(window_id, len(list_panes(window_id)), layout)
    set_slot(replacement, slot_id)
    return wait_win, replacement


def break_pane(pane_id: str, name: str) -> str:
    """Move ``pane_id`` into a new detached window ``name`` WITHOUT killing its
    process; the pane id and its ``@swarm_slot`` tag survive the move. Returns the
    new window id."""
    out = run(
        [
            "break-pane",
            "-d",
            "-s",
            pane_id,
            "-n",
            name,
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


def _box_holds(pane_id: str, head: str) -> bool:
    """True while claude's input box still holds the typed text.

    The box is the text after the LAST ``❯`` in the pane (past messages render
    above it); on submit claude clears the box. ``head`` must fit the box's
    first visual row — claude soft-wraps the rest onto indented rows that ``-J``
    does not join."""
    text = capture_joined(pane_id)
    idx = text.rfind("❯")
    if idx < 0:
        return False
    return text[idx + 1 :].lstrip().startswith(head)


def _pane_has(pane_id: str, head: str) -> bool:
    """True if ``head`` appears anywhere in the pane — box or transcript."""
    return head in capture_joined(pane_id)


def _has_prompt_box(pane_id: str) -> bool:
    """True if the pane renders claude's ``❯`` box at all.

    A pane without one is not a claude TUI (the fake-script test panes), so the
    box-based verification below cannot apply to it.
    """
    return "\u276f" in capture_joined(pane_id)


#: How long the input box must stay empty before a submit is believed.
CONFIRM_WINDOW_S = 3.0


def _stays_empty(pane_id: str, head: str, window: float, interval: float = 0.3) -> bool:
    """True only if the box holds no trace of ``head`` for the whole ``window``."""
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        if _box_holds(pane_id, head):
            return False
        time.sleep(interval)
    return not _box_holds(pane_id, head)


def send_submit(pane_id: str, text: str, settle: float = 0.5, tries: int = 4) -> bool:
    """Type ``text`` then submit it — and verify the submit actually took.

    Injected input into claude's TUI fails in two different ways, and the whole
    difficulty is that both leave the input box EMPTY, which is also what success
    looks like:

    * **Enter swallowed** — the text lands in the box and just sits there. The
      run then stalls with the supervisor believing a master is driving it (seen
      e.g. a master sits idle with its prompt written and
      unsent).
    * **Keystrokes dropped** — the pane was not ready for input, so the text
      never arrives at all.

    So an empty box proves nothing on its own; the pane must also show the text
    *somewhere* (claude echoes a submitted message into the transcript). That
    single extra check is what separates "submitted" from "never typed":

    ==========================  ==========  ================================
    pane state                  meaning     action
    ==========================  ==========  ================================
    box holds the text          unsent      Enter again
    text absent entirely        never typed retype, then Enter
    text present, box empty     submitted   done
    ==========================  ==========  ================================

    Retrying Enter is always safe — on a full box it is the submit we wanted, on
    an empty box a no-op. Retyping is only done while the text has *never* been
    seen, so a transcript that scrolls away cannot cause a duplicate submission.
    A pane that renders no ``❯`` box keeps the old single-Enter behaviour and
    reports success. Returns False only if every attempt left it unsent.
    """
    send_literal(pane_id, text)
    head = text[:40]
    landed = _poll(lambda: _box_holds(pane_id, head), settle)
    if landed:
        time.sleep(0.2)  # keystrokes rendered; brief settle before Enter
    send_enter(pane_id)

    if not _has_prompt_box(pane_id):
        return True  # not a claude TUI: nothing here applies

    ever_seen = landed
    for _ in range(tries):
        if _poll(lambda: not _box_holds(pane_id, head), 2.0):
            # Watch the box STAY empty rather than sampling it once. A single
            # delayed sample can land in a gap: a resolver pane once
            # reported success and its prompt was still sitting there afterwards,
            # because the text rendered later than the one 0.6 s check. Sampling
            # across a window costs seconds on a rare spawn and removes a class
            # of silent stall that costs hours.
            if _stays_empty(pane_id, head, CONFIRM_WINDOW_S):
                if ever_seen or _pane_has(pane_id, head):
                    return True
                # Box empty AND the text is nowhere: the keystrokes never
                # arrived. Reporting success here is the original bug in its
                # other form, so type it again rather than assume.
                send_literal(pane_id, text)
                time.sleep(0.3)
        if _box_holds(pane_id, head):
            ever_seen = True
        run(["send-keys", "-t", pane_id, "Enter"])
    return False


