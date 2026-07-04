#!/usr/bin/env bash
# Hermetic fake worker: stands in for a `claude` worker session (bash: read -t).
#
# 1. print a readiness banner (exercises the launcher's readiness detector),
# 2. accept the driver command typed via send-keys (tmux) if any,
# 3. do "work" (sleep), then signal completion with `swarm done` -- unless it is
#    told to PARK (simulate a gate failure that blocks holding its slot, emitting
#    no `done`).
#
# Env knobs (all optional):
#   SWARM_READY_MARKER  banner to print                 (default fake-ready-marker)
#   FAKE_WORKER_SLEEP   seconds of "work"               (default 2)
#   FAKE_WORKER_STATUS  ok|fail passed to `swarm done`  (default ok)
#   FAKE_WORKER_PARK    1 => park (no done, hold slot)  (default 0)
#   SWARM_BIN           how to invoke the CLI           (default swarm)
#   SWARM_PHASE         phase id (set by the launcher)
set -u

echo "${SWARM_READY_MARKER:-fake-ready-marker}"

# In tmux mode a "/prime <phase>" line is typed via send-keys; consume it if
# present so it does not leak. Non-blocking-ish: a 1s timeout in tmux, EOF in
# bare mode (stdin is /dev/null).
IFS= read -r -t 1 _cmd 2>/dev/null || true

sleep "${FAKE_WORKER_SLEEP:-2}"

if [ "${FAKE_WORKER_PARK:-0}" = "1" ]; then
    # Parked: a real worker would block on an AskUserQuestion. We just exit
    # without `done`; the slot stays busy in state, so finish cannot fire.
    exit 0
fi

# shellcheck disable=SC2086
${SWARM_BIN:-swarm} done "$SWARM_PHASE" "${FAKE_WORKER_STATUS:-ok}"
