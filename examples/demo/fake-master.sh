#!/usr/bin/env bash
# Hermetic fake master: stands in for the `claude` init (bootstrap) master.
#
# The init master no longer launches anything -- the supervisor launches ready
# phases itself once this pass idles. So the fake does what the real one does
# around that: read `swarm context`, take a moment "deciding" (patching the
# worker command, in the real one), declare itself idle, then block until the
# supervisor kills it. It never self-terminates, exactly like the real master.
#
# This exercises the spawn / idle / kill handshake and the launch hold behind
# it with no LLM.
#
# Env knobs:
#   SWARM_BIN         how to invoke the CLI              (default swarm)
#   FAKE_MASTER_WAIT  seconds the bootstrap pass takes   (default 1)
set -u

BIN="${SWARM_BIN:-swarm}"

$BIN context >/dev/null 2>&1 || true
sleep "${FAKE_MASTER_WAIT:-1}"

$BIN master-idle
exec sleep infinity
