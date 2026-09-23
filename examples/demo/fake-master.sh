#!/usr/bin/env bash
# Hermetic fake master: stands in for the `claude` session in the master pane,
# in both of its roles.
#
# init (the bootstrap pass): the init master no longer launches anything -- the
# supervisor launches ready phases itself once this pass idles. So the fake does
# what the real one does around that: read `swarm context`, take a moment
# "deciding" (patching the worker command, in the real one), declare itself idle,
# then block until the supervisor kills it.
#
# overseer (SWARM_MASTER_KIND=overseer): read the digest, fill in the pass record
# the supervisor created, and sign off with `swarm overseer-done` -- or, with
# FAKE_OVERSEER_HANG=1, never sign off, so the supervisor's timeout must kill it.
#
# Either way it never self-terminates, exactly like the real master.
#
# Env knobs:
#   SWARM_BIN           how to invoke the CLI                    (default swarm)
#   FAKE_MASTER_WAIT    seconds the bootstrap pass takes         (default 1)
#   FAKE_OVERSEER_WAIT  seconds an Overseer pass takes           (default 0)
#   FAKE_OVERSEER_HANG  1 => the pass never calls overseer-done  (default 0)
set -u

BIN="${SWARM_BIN:-swarm}"

if [ "${SWARM_MASTER_KIND:-init}" = "overseer" ]; then
    saw="the digest was missing"
    if [ -s "${SWARM_OVERSEER_DIGEST:-/nonexistent}" ]; then
        saw="read the digest: $(grep -c '^- ' "$SWARM_OVERSEER_DIGEST") list lines"
    fi
    sleep "${FAKE_OVERSEER_WAIT:-0}"
    if [ "${FAKE_OVERSEER_HANG:-0}" = "1" ]; then
        exec sleep infinity
    fi
    record="${SWARM_OVERSEER_RECORD:-/dev/null}"
    tmp="$record.fake"
    awk -v saw="$saw" '
        { print }
        $0 == "## Saw" { print saw }
        $0 == "## Did" { print "nothing to do (fake overseer)" }
        $0 == "## Left for the owner" { print "nothing" }
    ' "$record" > "$tmp" && mv "$tmp" "$record"
    $BIN overseer-done "fake overseer pass: $saw"
    exec sleep infinity
fi

$BIN context >/dev/null 2>&1 || true
sleep "${FAKE_MASTER_WAIT:-1}"

$BIN master-idle
exec sleep infinity
