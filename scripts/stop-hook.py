#!/usr/bin/env python3
"""Claude Code ``Stop`` hook: capture each worker turn's final text for recaps.

Registered on every swarm worker via ``[worker].worker_settings``, this fires at
every assistant turn boundary inside a worker session and appends that turn's
closing text to ``<SWARM_STATE_DIR>/turns/<SWARM_PHASE>.jsonl``. That file is
what ``swarm_orchestrator.recap`` summarises when a phase completes or the owner
asks.

Why this and not the transcript, or ``/recap``:

* the ``Stop`` payload carries ``last_assistant_message`` — the exact final text
  of the turn — so there is no transcript to parse and no lag behind the live
  conversation;
* it costs nothing: no API call, no model, no token;
* it works while the worker is busy. ``/recap`` is not an ``immediate`` command,
  so sending it into a live worker queues behind a turn that can run for hours.

Contract with the harness, in priority order:

1. **Never fail the turn.** Every path exits 0. A hook that errors on a worker
   that is 90 minutes into a phase costs far more than a missing recap line.
2. **Never block.** No network, no locks, one append.
3. **Say nothing on stdout.** The worker's transcript is the owner's window into
   the run; a status line per turn would be noise in it.

Only ``Stop`` is registered, never ``SubagentStop``: workers run their teammates
in-process, and capturing every subagent's sign-off would bury the worker's own
narrative under a dozen unrelated voices.
"""

from __future__ import annotations

import json
import os
import sys
import time

# A final message can be arbitrarily long (a worker pasting a whole diff into its
# sign-off). Recaps only ever read the last few turns, so an unbounded file buys
# nothing and costs a state dir. The head is kept: a sign-off leads with what it
# did, then elaborates.
MAX_TEXT_CHARS = 8000
_TRUNC_MARK = " ...[truncated]"


def main() -> int:
    phase = os.environ.get("SWARM_PHASE")
    state_dir = os.environ.get("SWARM_STATE_DIR")
    if not phase or not state_dir:
        # Not a swarm worker — a plain claude session that happens to share these
        # settings, or a `claude -p` the recap itself spawned. Do nothing.
        return 0

    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0

    text = str(payload.get("last_assistant_message") or "").strip()
    if not text:
        return 0
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - len(_TRUNC_MARK)] + _TRUNC_MARK

    row = {
        "ts": time.time(),
        "session_id": payload.get("session_id"),
        "text": text,
    }
    turns_dir = os.path.join(state_dir, "turns")
    path = os.path.join(turns_dir, f"{phase}.jsonl")
    line = (json.dumps(row) + "\n").encode("utf-8")
    try:
        os.makedirs(turns_dir, exist_ok=True)
        # One O_APPEND write of one whole line: concurrent writers (a phase
        # relaunched into a second session) interleave whole records rather than
        # corrupting each other's, and no lock is taken on a worker's hot path.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException:  # noqa: BLE001 - a hook must never fail its turn
        pass
    sys.exit(0)
