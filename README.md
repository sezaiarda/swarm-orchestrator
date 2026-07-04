# swarm-orchestrator

Run many `claude` (Claude Code CLI) sessions in tmux to build a project's
**phases** in parallel. A CLI `swarm` drives a tmux session with a `master`
window (an ephemeral Claude "master" that decides which phases to launch) and a
`workers` window of exactly **4 fixed slots** (2×2 tiled). A long-running
**supervisor** owns a FIFO event queue, `state.json`, and the master lifecycle.
Workers signal completion with `swarm done`, which the supervisor turns into
either spawning a fresh master or injecting a nudge into the live one.

Pure glue over tmux + the `claude` CLI, so it orchestrates projects in **any
language**, driven by a per-project `.swarm.toml`.

## The lifecycle — pure injection, four rules

The supervisor is the **sole** FIFO reader (⇒ total event order), the sole
writer of `state.json` (flock), and the sole killer of the master pane. It
implements EXACTLY four rules — no redo, no re-derivation, no reconcile pass, no
safety timer, no crash watchdog, no auto-retry:

1. **`done <phase> <ok|fail>`** — free the phase's slot; if **no master is
   alive → spawn** a fresh master, else **→ inject** a `send-keys` nudge into the
   live master.
2. **`master-idle`** — the supervisor **kills** the master pane.
3. After a kill: if **no slot is busy → finish** (teardown signal + telegram).
4. A worker that fails a gate **parks** (emits no `done`) → its slot stays busy →
   finish can't fire until the owner resolves it.

Slot accounting is **state-based**: 4 slots are fixed pane-ids tagged
`@swarm_slot N`; we never count raw tmux panes, so stray teammate panes can't
corrupt accounting. `swarm launch` claims a slot check-and-set under
`flock(state.json)` before it respawns a pane, preventing double-launch.

## Install

```bash
uv tool install --editable ~/Projects/swarm-orchestrator   # puts `swarm` on PATH
```

## Use

Add a `.swarm.toml` to your project (see `examples/multi-repo.swarm.toml`), then:

```bash
swarm up        # set up the tmux session + start the supervisor + init master
swarm status    # human-readable state dump
swarm down      # stop the supervisor + tear down
```

Everything else (`launch`, `done`, `context`, `master-idle`, `reconcile`,
`reap`, `free`, `skip`, `finish`) is used by the master/worker sessions or as an
owner escape hatch. `swarm context` prints the read-only JSON snapshot the
master reasons over.

## Config (`.swarm.toml`)

```toml
[swarm]
max_workers = 4
master_model = ""                 # "" = inherit; else "opus"/"sonnet"/...

[worker]
command_template = "/prime {phase}"           # sent via send-keys into each slot
command_file    = ".claude/commands/prime.md" # init master inspects/patches this
env_marker      = "SWARM_PHASE"
done_hook       = 'swarm done "$SWARM_PHASE" ok'

[tasks]
ledger  = "docs/PHASE-LEDGER.md"
roadmap = "docs/ROADMAP-MASTER.md"
exclude = []                                   # externally-blocked phases

[telegram]
notify = "/path/to/swarm-orchestrator/scripts/notify.sh"

[tmux]
session = "swarm"
```

Runtime state lives outside the repo, under
`~/.local/state/swarm-orchestrator/<project-slug>/` (`state.json`,
`control.fifo`, `done/`, `logs/`).

## Tests

Hermetic and LLM-free: a **real supervisor** against **fake** master/worker
shell scripts, a throwaway tmux session, and temp dirs — no `claude`, torn down
in `finally`.

```bash
uv run pytest
```

- **Tier A** (`test_lifecycle.py`, bare driver) — the state machine, FIFO/
  sentinel plumbing, dep gating, fan-out, injection, convergence, parked-worker
  deadlock-freedom, and best-effort `done` that never hangs.
- **Tier B** (`test_selftest.py`, real tmux, no claude) — session bring-up +
  tagged slots, the launcher's respawn-pane / readiness-detect / send-keys /
  echo-verify path against the fake banner, and teammate break-out.
- **Unit** (`test_state.py`) — slot claim, `flock` under real concurrency, the
  dependency resolver, and the FIFO line format.

The fake scripts require **bash** (`read -t` timeout); `/bin/sh` is dash on the
target host and lacks it.

## Accepted design trade-off

Pure injection has **no safety net** (by design). If an injected
keystroke is ever lost (a tmux `send-keys` phenomenon — the bare-pipe path in
tests is reliable), that slot's next phase waits until the following worker
finishes and self-heals on the next `done`; if it was the *last* worker, finish
fires with the ready phase logged (`FINISH-WITH-READY`) so the owner can
`swarm launch` it manually.
