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
#   FAKE_ROW_STATUS     "<row>=<status> ..." => that status for that row of a batch
#   SWARM_BIN           how to invoke the CLI           (default swarm)
#   SWARM_PHASE         phase id (set by the launcher)
#   SWARM_BATCH         the rows this session builds, in order (set by the launcher)
set -u

# Optional: simulate claude's folder-trust dialog before the banner, with a
# cursor that works as claude's does: Up and Down move it (the list wraps),
# Enter takes the answer under it, and any answer but the trusting one ends the
# session with exit 1, as does Esc. It ASKS TWICE (models a dialog that lingers
# or re-prompts) -- so a launcher that latches after one answer, or that declares
# readiness while the dialog is up, gets stuck here and never boots.
#   FAKE_WORKER_TRUST=1  the earlier wording: the cursor starts on "Yes, proceed"
#   FAKE_WORKER_TRUST=2  Claude Code 2.1.286's: the cursor starts on "No, exit"
trust_dialog() {
    local -a opts
    local cur=0 key rest i
    if [ "$1" = "2" ]; then
        opts=("No, exit" "Yes, I trust this folder")
    else
        opts=("1. Yes, proceed" "2. No, exit")
    fi
    while :; do
        printf '\033[2J\033[H'   # redraw the screen (as claude's TUI does)
        if [ "$1" = "2" ]; then
            echo " Accessing workspace:"
            echo
            echo " Quick safety check: Is this a project you created or one you trust?"
        else
            echo " Do you trust the files in this folder?"
        fi
        echo
        for i in "${!opts[@]}"; do
            if [ "$i" = "$cur" ]; then echo " ❯ ${opts[$i]}"; else echo "   ${opts[$i]}"; fi
        done
        echo
        echo " Enter to confirm · Esc to cancel"
        IFS= read -rsn1 key || exit 1
        case "$key" in
            "")
                case "${opts[$cur]}" in *Yes*) return 0 ;; *) exit 1 ;; esac ;;
            $'\033')
                IFS= read -rsn2 -t 0.2 rest || exit 1   # a bare Esc cancels
                case "$rest" in
                    "[A" | "OA") cur=$(((cur + ${#opts[@]} - 1) % ${#opts[@]})) ;;
                    "[B" | "OB") cur=$(((cur + 1) % ${#opts[@]})) ;;
                esac ;;
        esac
    done
}
if [ "${FAKE_WORKER_TRUST:-0}" != "0" ]; then
    for _ in 1 2; do trust_dialog "$FAKE_WORKER_TRUST"; done
    printf '\033[2J\033[H'       # dialog gone once answered
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

# A batch is built one row at a time, each reported on its own, the session's
# own row included; a lone row is a batch of one.
for row in ${SWARM_BATCH:-$SWARM_PHASE}; do
    sleep "${FAKE_WORKER_SLEEP:-2}"
    status="${FAKE_WORKER_STATUS:-ok}"
    for pick in ${FAKE_ROW_STATUS:-}; do
        [ "${pick%%=*}" = "$row" ] && status="${pick#*=}"
    done
    # shellcheck disable=SC2086
    ${SWARM_BIN:-swarm} done "$row" "$status"
done
