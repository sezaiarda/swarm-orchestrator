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
#   FAKE_WORKER_DETACH  path prefix => start a setsid'd `sleep` and write its
#                       pid to <prefix>.<phase> (a child that left the tree)
#   FAKE_WORKER_KEEP    1 => `swarm keep` a `sleep` as keep-<phase>
#   SWARM_BIN           how to invoke the CLI           (default swarm)
#   SWARM_PHASE         phase id (set by the launcher)
set -u

# Optional: simulate claude's workspace-trust dialog before the banner. It
# RE-PROMPTS once (models a dialog that lingers / renders late), and only a bare
# Enter dismisses it -- so a launcher that latches after one dismissal, or that
# declares readiness while the dialog is up, gets stuck here and never boots.
if [ "${FAKE_WORKER_TRUST:-0}" = "1" ]; then
    for _ in 1 2; do
        printf '\033[2J\033[H'   # redraw the screen (as claude's TUI does)
        echo "╭─ Do you trust the files in this folder? ──────╮"
        echo "│  1. Yes, proceed        2. No, exit           │"
        echo "╰───────────────────────────────────────────────╯"
        while IFS= read -r _l; do [ -z "$_l" ] && break; done
    done
    printf '\033[2J\033[H'       # dialog gone once dismissed
fi

echo "${SWARM_READY_MARKER:-fake-ready-marker}"

# In tmux mode a "/prime <phase>" line is typed via send-keys; consume it if
# present so it does not leak. Non-blocking-ish: a 1s timeout in tmux, EOF in
# bare mode (stdin is /dev/null).
IFS= read -r -t 1 _cmd 2>/dev/null || true

if [ "${FAKE_WORKER_PARK:-0}" = "1" ]; then
    # Parked: a real worker would block on an AskUserQuestion after a gate
    # failure. We exit fast without `done`; the slot stays busy in state (the
    # claim lives in state.json, not in this process), so finish cannot fire.
    exit 0
fi

if [ -n "${FAKE_WORKER_DETACH:-}" ]; then
    setsid sleep 300 </dev/null >/dev/null 2>&1 &
    echo $! > "${FAKE_WORKER_DETACH}.${SWARM_PHASE}"
fi
if [ "${FAKE_WORKER_KEEP:-0}" = "1" ]; then
    # shellcheck disable=SC2086
    ${SWARM_BIN:-swarm} keep --name "keep-$SWARM_PHASE" --why "a test stand-in" -- sleep 300
fi

sleep "${FAKE_WORKER_SLEEP:-2}"

# shellcheck disable=SC2086
${SWARM_BIN:-swarm} done "$SWARM_PHASE" "${FAKE_WORKER_STATUS:-ok}"
