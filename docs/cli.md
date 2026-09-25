# Command reference

Every `swarm` subcommand, grouped by purpose. `swarm <command> -h` prints the
exact flags.

`swarm [--config PATH] [--project-dir DIR] <command>`. Every command takes `-h`.

## Run control

| command | what it does |
|---|---|
| `up [--no-attach]` | Reconcile leftovers, build the tmux session, start the supervisor, the init pass and the board, then attach. |
| `down` | Stop the supervisor, end every session process, tear the session down, close the run. |
| `finish [--force]` | Ask the supervisor to stop now. Refused while operator jobs are queued, unless `--force`. |
| `pause` / `resume` | Hold new launches (running workers finish) / fill free slots again, and hand given-up phases back. |
| `reset` | Close the open run and start a new one: ETA and usage count from now. Nothing restarts. |
| `reload [--dry-run]` | Apply a `.swarm.toml` edit to the running swarm. |
| `layout [name]` | Show or change the worker-pane arrangement, live. |

## Looking

| command | what it does |
|---|---|
| `status [--all] [--json]` | Slots, merge queue, waiting and parked phases, operator queue, owed pushes, the done map as counts per status (naming failures), board address. `--all` prints the whole done map; `--json` prints the state as JSON. |
| `context` | The JSON snapshot the launcher works from: `ready`, `launchable`, free and busy slots, `waiting`, `parked`, `ledger_issues`. |
| `doctor [--json]` | Diagnose a stuck or unhealthy swarm. Exit 1 on any FAIL. |
| `why <phase> [--tree] [--json]` | Why this phase is not running, down to the root blocker. |
| `report [--decisions] [--phase P] [--json]` | What every phase did, with its recap and timings. |
| `usage [-n N] [--json]` | This run's and past runs' usage per hour. |
| `overseer [--now] [-n N] [--json]` | Recent Overseer passes and pending reasons. `--now` requests a pass. |
| `tui` | The dashboard (window 0). |
| `web [--host H] [--port N]` | Serve the read-only board. |
| `check [--strict]` | Preflight: Telegram, ledger, prompt lint. Exit 1 on a failure or a contradicted prompt line; `--strict` also fails on a wasteful one. |

## Phases

| command | what it does |
|---|---|
| `launch <phase>` | Claim a free slot and start a phase by hand. Says why when it cannot. |
| `skip <phase>` | Record a phase `skip` (done without building). Releases its slot, park or window. |
| `retry [phases…] [--all-failed] [--cascade] [--launch] [--keep-branch]` | Put failed phases back in play. Clears their record and sentinels, and discards their branches. |
| `free <slot\|phase>` | Free a slot whose worker died without `swarm done`, then wake the launcher. |
| `resolved <phase>` | Release a held merge queue after the conflict or dirty tree is fixed. |
| `integrate <phase>` | Integrate `swarm/<phase>` by hand, outside the queue. |

## Inside a worker

| command | what it does |
|---|---|
| `done <phase> [ok\|operator\|fail] ["recap"] [--force]` | Signal completion. Writes the sentinel, then pings, routes and pokes as the status says. |
| `waiting <phase> ["question"]` | Tell the owner you are blocked on them. Arms the park timer. |
| `resumed <phase> ["answer"]` | The owner answered. Records the answer and cancels the park. |
| `note <phase> [decision\|assumption\|risk] "text"` | Log a judgement call, silently. |
| `build <cmd…>` | Run a heavy build through the swarm-wide gate. |
| `notify "message"` | Message the owner through the swarm's own sender. |

## Operator

| command | what it does |
|---|---|
| `operator <phase>` | Open an operator session for a phase's hand-off now (or the moment the phase merges, if it has not yet), overriding a `later` triage. Builds the job from its sentinel if needed. |
| `operator-add "brief" [--phase P]` | Queue an ad-hoc job. |
| `operator-triage <job>` | Decide `now` or `later` for a queued job. `swarm done` spawns it. |
| `operator-done <job> ["outcome"]` | The session is finished. Records and telegrams the outcome, merges its mirror. |
| `operator-ask <job> "question"` | Ping the owner and keep the session alive while it asks. |
| `operator-resumed <job> ["answer"]` | The owner answered. Records the answer, back to a normal lease. |

## Overseer

| command | what it does |
|---|---|
| `overseer-done ["summary"]` | End the pass. Records the summary and merges its mirror. |
| `overseer-ask "question"` | Ping the owner and hold the pass while it asks. |
| `overseer-resumed ["answer"]` | The owner answered. Records the answer, back to the normal timeout. |

## Housekeeping

| command | what it does |
|---|---|
| `gc [--yes] [--older-than N] [--aggressive] [--transcripts] [--branches] [--canonical] [--force] [-v]` | Reclaim disk. A dry run unless `--yes`. |
| `recap <phase> [--force]` | Summarize one phase now. |

## Plumbing

Sent by the tooling; you rarely type these.

| command | what it does |
|---|---|
| `bootstrap` | Ask the supervisor to run the init pass. `swarm up` sends it. |
| `master-idle` | The init pass is finished. |
| `_supervise` | The supervisor process itself. |
| `_poke-done <phase> <status>` | The delayed `done` poke a `done_grace_s` child delivers. |
