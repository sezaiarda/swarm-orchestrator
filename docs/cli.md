# Command reference

Every `swarm` subcommand, grouped by purpose. `swarm <command> -h` prints the
exact flags.

`swarm [--config PATH] [--project-dir DIR] <command>`. Every command takes `-h`.

## Run control

| command | what it does |
|---|---|
| `up [--no-attach]` | Reconcile leftovers, build the tmux session, start the supervisor, the init pass, the board and the bot's command listener, then attach. Refused, before anything is touched, while a supervisor or the tmux session is already up. |
| `down` | Stop the supervisor, the board and the command listener, end every session process (anything carrying the run's `SWARM_STATE_DIR`, detached ones included; never what `swarm keep` holds), tear the session down, close the run. Only a tmux session this swarm made (or, made before that was recorded, one whose windows its state holds) is torn down, and the recorded supervisor pid is signalled only while it still is this project's supervisor. |
| `finish [--force]` | Ask the supervisor to stop now. Refused while operator jobs are queued or an ask is open, unless `--force`. |
| `pause` / `resume` | Hold new launches (running workers finish) / fill free slots again, and hand given-up phases back. |
| `reset` | Close the open run and start a new one: ETA and usage count from now. Nothing restarts. |
| `reload [--dry-run]` | Apply a `.swarm.toml` edit to the running swarm. |
| `layout [name]` | Show or change the worker-pane arrangement, live. |

## Looking

| command | what it does |
|---|---|
| `status [--all] [--json]` | Slots, merge queue, waiting and parked phases, operator queue, open asks, owed pushes, the done map as counts per status (naming failures), board address, kept processes. `--all` prints the whole done map; `--json` prints the state as JSON. |
| `context` | The JSON snapshot the launcher works from: `ready`, `launchable`, free and busy slots, `waiting`, `parked`, `ledger_issues`. |
| `doctor [--json]` | Diagnose a stuck or unhealthy swarm. Exit 1 on any FAIL. |
| `why <phase> [--tree] [--json]` | Why this phase is not running, down to the root blocker. |
| `report [--decisions] [--phase P] [--json]` | What every phase did, with its recap and timings. |
| `usage [-n N] [--json]` | This run's and past runs' usage per hour, and how old the newest sample is. |
| `overseer [--now] [-n N] [--json]` | Recent Overseer passes and pending reasons. `--now` requests a pass. |
| `tui` | The dashboard (window 0). |
| `web [--host H] [--port N]` | Serve the read-only board. |
| `telegram-bot` | Answer `/usage` and `/help` from the owner's Telegram chat, in the foreground. `swarm up` starts it detached. |
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
| `done <phase> [ok\|operator\|fail] ["recap"] [--force]` | Signal completion. Writes the sentinel, then pings, routes and pokes as the status says. Refused, before anything is written, for a malformed phase id, a phase no worker is running, or (inside a worker) a phase other than the worker's own. A repeat for a phase already recorded is a no-op. |
| `waiting <phase> ["question"]` | Tell the owner you are blocked on them. Arms the park timer. |
| `resumed <phase> ["answer"]` | The owner answered. Records the answer and cancels the park. |
| `note <phase> [decision\|assumption\|risk] "text"` | Log a judgement call, silently. |
| `build <cmd…>` | Run a heavy build through the swarm-wide gate. |
| `notify "message"` | Message the owner through the swarm's own sender, the only way a session should. From an Overseer pass, the usage block is appended. |
| `keep --name N --why "one line" [--cwd DIR] -- <cmd…>` | Leave one process running after your session ends (everything else a session starts is ended with it). Starts it detached without the session's markers, records `<state>/keep/N.json`, prints its pid and log. `--why` is required (≤120 chars); a live name is refused, a dead one replaced. Use it only when something must outlive the session, and name it in your recap. |
| `keep --list [--json]` / `keep --stop N` | Every kept process, alive or dead, with why, who and age / stop one (SIGTERM, then SIGKILL, to its group) and forget it. |

## Asks

Where the owner answers review questions: a session in its own tmux window,
`ask:<name>`. See [components.md](components.md#asks-where-the-owner-answers-review-questions).

| command | what it does |
|---|---|
| `ask --name N --rows R1[,R2…] --why "one line" "brief"` | Record an ask (`<state>/ask/N.json`) and poke the supervisor to open window `ask:N`: a session that shows the owner what the brief points at, asks with AskUserQuestion and records the picks in the rows. Takes no worker slot and never times out; the owner is pinged once when the window opens. `--why` is required (≤120 chars). A name whose window is alive is refused; an open one whose window is gone takes the new brief and opens again. With no supervisor running it opens at the next `swarm up`. |
| `ask --list [--json]` | Open asks, then the last ten answered: name, rows, why, age, and the `tmux select-window` that reaches each window. |
| `ask --reopen N` | Open an open ask's window again, with the brief it has (no second ping). |
| `ask-done N ["outcome"] [--stop-keep K]… [--attention]` | (ask session) The owner's answers are recorded. Records the outcome, stops each kept process named with `--stop-keep`, then the supervisor closes the window, ends everything the session started and, under worktree isolation, merges its mirror `ask-N`. Pings the owner only with `--attention` (`[operator].notify` applies). |

## Operator

| command | what it does |
|---|---|
| `operator <phase>` | Open an operator session for a phase's hand-off now (or the moment the phase merges, if it has not yet), overriding a `later` triage. Builds the job from its sentinel if needed. |
| `operator-add "brief" [--phase P]` | Queue an ad-hoc job. |
| `operator-triage <job>` | Decide `now` or `later` for a queued job. `swarm done` spawns it. |
| `operator-done <job> ["outcome"] [--attention] [--ask "question"]` | The session is finished. Records the outcome, merges its mirror. `--attention` or `--ask` opens an ask window where the owner answers (its ping carries the question). |
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
