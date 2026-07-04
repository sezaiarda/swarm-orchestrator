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

1. **Add a `.swarm.toml`** to your project root (see
   `examples/multi-repo.swarm.toml`).
2. **The worker command needs a completion hook.** The command named in
   `[worker].command_file` must, *when `$SWARM_PHASE` is set*, (a) skip any
   initial "which phase?" prompt and build `$SWARM_PHASE` directly, and (b) run
   `[worker].done_hook` (`swarm done "$SWARM_PHASE" ok`) once the phase is green.
   On the first `swarm up` the **init master** proposes those two edits for you
   to approve — or add them yourself.
3. **Run it** from the project root (or pass `--project-dir`):

   ```bash
   swarm up               # create tmux session + supervisor + init master
   tmux attach -t swarm   # watch — window 0 = master, window 1 = 4 workers
   ```

### What happens
- `swarm up` builds a tmux session `swarm` (a **master** window, a **workers**
  window of 4 tiled slots, a **teammates** parking window), starts the
  background **supervisor**, and launches the **init master**.
- The init master checks telegram, reads your ledger, (offers to) patch the
  worker command, asks you to confirm the first batch, then `swarm launch`es up
  to 4 phases — each a real `claude` running `/prime <phase>` in a slot.
- When a worker finishes it calls `swarm done`; the supervisor spawns a fresh
  master (or nudges the live one) to launch the next ready phase into the freed
  slot. Loops until nothing is left, then `finish` telegrams you.

### Watching & answering
- **Attach:** `tmux attach -t swarm`; switch windows `Ctrl-b 0/1/2`, detach
  `Ctrl-b d`.
- A worker only stops for you when it genuinely needs a decision — it
  **telegram-pings first**, then asks in its own pane; switch to the workers
  window and answer there. The master's questions (batch confirm, the `prime.md`
  diff) show in the master window.

### Owner escape hatches
| command | use |
|---|---|
| `swarm status` | human-readable state dump (shows `paused`) |
| `swarm pause` | stop launching new workers; in-flight ones finish and hold |
| `swarm resume` | resume — fill free slots again |
| `swarm context` | the JSON snapshot the master reasons over |
| `swarm launch <phase>` | manually start a phase in a free slot |
| `swarm free <slot\|phase>` | free a stuck slot |
| `swarm skip <phase>` | mark a phase done without building it |
| `swarm down` | stop the supervisor + tear the session down |

> `finish` leaves the tmux windows up so you can inspect results; run
> `swarm down` to tear everything down.

## Deploy a multi-repo project

```bash
uv tool install --editable ~/Projects/swarm-orchestrator          # once
cp ~/projects/swarm-orchestrator/examples/multi-repo.swarm.toml \
   ~/projects/myproject/.swarm.toml                               # once
cd ~/projects/myproject && swarm up && tmux attach -t swarm       # go
```

The multi-repo example config points the master at `docs/PHASE-LEDGER.md`, excludes the
externally-blocked phases (`intake-P1..5`, `I1..6`), and has the init master
patch `.claude/commands/prime.md` on first run. Stop anytime with `swarm down`.

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
