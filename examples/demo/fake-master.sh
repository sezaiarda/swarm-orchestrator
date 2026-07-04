#!/usr/bin/env bash
# Hermetic fake master: stands in for the ephemeral `claude` master session.
# Requires bash: `read -t` (timeout) is not available in dash/POSIX sh.
#
# Runs one launch pass, then reacts to injected nudges (one per line on stdin in
# bare mode; typed via send-keys in tmux mode), then declares itself idle. It
# never self-terminates -- after `master-idle` it blocks, waiting for the
# supervisor to kill it, exactly like the real master.
#
# This exercises the exact spawn / launch / idle / kill handshake with no LLM.
#
# Env knobs:
#   SWARM_BIN         how to invoke the CLI       (default swarm)
#   FAKE_MASTER_WAIT  seconds to await a nudge    (default 1) -- the fake's
#                     stand-in for the LLM deciding it has nothing left to do.
set -u

BIN="${SWARM_BIN:-swarm}"

pass() {
    # Launch every phase the supervisor reports as launchable (ready AND a slot
    # is free). Slot claiming is check-and-set under flock inside `swarm launch`.
    ctx=$($BIN context 2>/dev/null) || return 0
    printf '%s\n' "$ctx" | jq -r '.launchable[]?' | while IFS= read -r phase; do
        [ -n "$phase" ] && $BIN launch "$phase" >/dev/null 2>&1
    done
}

pass
while IFS= read -r -t "${FAKE_MASTER_WAIT:-1}" _nudge; do
    pass
done

$BIN master-idle
exec sleep infinity
