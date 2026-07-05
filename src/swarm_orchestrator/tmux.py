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


def split_layout(window_id: str, count: int, hold: str = "sleep infinity") -> list[str]:
    """Grow ``window_id`` to ``count`` panes per the owner's layout rule.

    ``count==1`` leaves the lone pane untouched (full window); ``count==2`` makes
    a LEFT|RIGHT pair via ``split-window -h`` locked to even-horizontal columns (a
    vertical divider, never stacked); ``count`` in 3..4 uses the tiled grid preset.
    Returns pane ids ordered top-left-to-bottom-right so slot indices track the
    visible layout.
    """
    if count == 2:
        run(["split-window", "-h", "-t", window_id, hold], check=True)
        run(["select-layout", "-t", window_id, "even-horizontal"])
    elif count >= 3:
        for _ in range(count - 1):
            run(["split-window", "-t", window_id, hold], check=True)
        run(["select-layout", "-t", window_id, "tiled"])
    return _panes_ordered(window_id)


def window_of(pane_id: str) -> str:
    """The id of the window that currently holds ``pane_id``."""
    return run(["display-message", "-p", "-t", pane_id, "#{window_id}"]).stdout.strip()


def apply_layout(window_id: str, count: int) -> None:
    """Re-apply the owner's per-count layout preset to an EXISTING window (no
    splitting) — used after a pane is broken out so the survivors re-tidy. Mirrors
    :func:`split_layout`'s presets (2 -> even-horizontal, 3-4 -> tiled, 1 -> full)."""
    if count == 2:
        run(["select-layout", "-t", window_id, "even-horizontal"])
    elif count >= 3:
        run(["select-layout", "-t", window_id, "tiled"])


def split_one(window_id: str, hold: str = "sleep infinity") -> str:
    """Split one fresh holding pane into ``window_id``; return its pane id."""
    out = run(
        ["split-window", "-t", window_id, "-P", "-F", "#{pane_id}", hold], check=True
    )
    return out.stdout.strip()


def park_pane(window_id: str, old_pane: str, slot_id: int, wait_name: str) -> tuple[str, str]:
    """Move the LIVE ``old_pane`` into its own window ``wait_name`` while leaving
    ``window_id``'s slot filled by a fresh replacement pane tagged ``slot_id``.

    Split-FIRST (add the replacement BEFORE breaking the waiter out) so the window
    always carries >= 2 panes at break time: :func:`break_pane` on a *single*-pane
    window renames it in place and returns an empty id, which split-first avoids.
    Returns ``(wait_window_id, replacement_pane_id)``."""
    replacement = split_one(window_id)
    wait_win = break_pane(old_pane, wait_name)
    apply_layout(window_id, len(list_panes(window_id)))
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


def send_submit(pane_id: str, text: str, settle: float = 0.5) -> None:
    """Type ``text`` then submit it. The short settle lets the keystrokes land
    before Enter, so Enter can't race ahead and submit an empty box."""
    send_literal(pane_id, text)
    time.sleep(settle)
    send_enter(pane_id)


