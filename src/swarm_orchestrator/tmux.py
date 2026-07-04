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


def split_tiled(window_id: str, count: int, hold: str = "sleep infinity") -> list[str]:
    """Grow ``window_id`` to ``count`` panes, tiled; return pane ids in order."""
    for _ in range(count - 1):
        run(["split-window", "-t", window_id, hold], check=True)
        run(["select-layout", "-t", window_id, "tiled"])
    run(["select-layout", "-t", window_id, "tiled"])
    return list_panes(window_id)


def list_panes(window_id: str) -> list[str]:
    out = run(["list-panes", "-t", window_id, "-F", "#{pane_id}"])
    return [ln for ln in out.stdout.splitlines() if ln]


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


def select_layout_tiled(window_id: str) -> None:
    run(["select-layout", "-t", window_id, "tiled"])


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


def join_pane(src_pane: str, dst_window: str) -> None:
    """Move ``src_pane`` into ``dst_window`` (process-safe; %id is stable)."""
    run(["join-pane", "-d", "-s", src_pane, "-t", dst_window])


def kill_pane(pane_id: str) -> None:
    run(["kill-pane", "-t", pane_id])


def reconcile_teammates(workers_win: str, teammates_win: str) -> list[str]:
    """Break out untagged (teammate) panes from the workers window.

    Level-triggered: any pane in ``workers_win`` lacking an ``@swarm_slot`` tag
    is joined into ``teammates_win`` and the workers window is re-tiled. Returns
    the moved pane ids. Moving a pane never signals its process.
    """
    moved: list[str] = []
    for pane, tag in list_panes_with_slot(workers_win):
        if tag == "":
            join_pane(pane, teammates_win)
            moved.append(pane)
    if moved:
        select_layout_tiled(workers_win)
    return moved
