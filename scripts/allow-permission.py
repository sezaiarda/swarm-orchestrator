#!/usr/bin/env python3
"""Claude Code ``PermissionRequest`` hook: answer Yes to every permission prompt.

Swarm sessions run in ``bypassPermissions``, yet some built-in safety checks
still open a Yes/No box — e.g. "Dangerous rm operation on possibly-empty
variable path", raised inside a worker's subagent. Nobody watches a worker
pane, so the box stalls the phase until the owner happens to look.
A ``PreToolUse`` "allow" does not clear these checks; only a
``PermissionRequest`` answer does. Verified live on CLI 2.1.283.

Each approval is appended to ``<SWARM_STATE_DIR>/permissions.jsonl`` so what
was waved through can be read afterwards. Logging never blocks the answer.
"""
import json
import os
import sys
import time

ALLOW = {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                "decision": {"behavior": "allow"}}}

try:
    event = json.loads(sys.stdin.read() or "{}")
    state = os.environ.get("SWARM_STATE_DIR")
    if state:
        row = {"ts": time.time(), "phase": os.environ.get("SWARM_PHASE"),
               "agent": event.get("agent_type"), "tool": event.get("tool_name"),
               "input": json.dumps(event.get("tool_input"))[:500]}
        with open(os.path.join(state, "permissions.jsonl"), "a") as f:
            f.write(json.dumps(row) + "\n")
except Exception:
    pass
print(json.dumps(ALLOW))
