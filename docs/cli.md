# Command reference

Every `swarm` subcommand, grouped by purpose. `swarm <command> -h` prints the
exact flags.

`swarm [--config PATH] [--project-dir DIR] <command>`. Every command takes `-h`.

## Run control

| command | what it does |
|---|---|
| `up [--no-attach]` | Reconcile leftovers, build the tmux session, start the supervisor, the init pass, the board and the bot's command listener, then attach. Refused, before anything is touched, while a supervisor or the tmux session is already up. |
| `down [--then CMD]` | Stop the supervisor, the board and the command listener, end every session process (anything carrying the run's `SWARM_STATE_DIR`, detached ones included; never what `swarm keep` holds), push unmerged work to origin (`[backup].on_down`), tear the session down, close the run. Only a tmux session this swarm made (or, made before that was recorded, one whose windows its state holds) is torn down, and the recorded supervisor pid is signalled only while it still is this project's supervisor. `--then` runs `CMD` afterwards, detached from the swarm, with its output in `<state>/logs/after-down.log`. |
| `down --drain [--then CMD]` | Launch nothing new and stop once the running work is finished: busy workers (through their done grace), launches, the moving merge queue, a merge-conflict resolver, a running operator job, the init pass or an Overseer pass. Whatever waits on you (a waiting or parked worker, an operator job that asked, a queue held on a dirty tree) does not hold it; the down keeps its work. Then `down`, then `CMD` (e.g. `'sleep 120; sudo shutdown now'`), with one telegram when it starts stopping. Warns if `CMD` uses `sudo` and sudo would ask for a password. With no supervisor running it stops at once. `D` in the dashboard does the same. |
| `down --cancel` | Cancel a pending drain. `resume` cancels one too. Refused once the stop has begun. |
| `finish [--force]` | Ask the supervisor to stop now. Refused while operator jobs are queued unless `--force`. |
| `pause [--in 12h \| --at HH:MM \| --cancel]` | Hold new launches now; running workers finish. `--in` (`12h`, `90m`, `1h30m`, `2d`) or `--at` (the next such local time) schedules the same pause for later, replacing any earlier schedule; it survives `swarm down`/`up` and one that came due while the swarm was down fires on the next `up`. `--cancel` drops it, and so does `resume`. |
| `resume [--override-cap]` | Fill free slots again, and hand given-up phases back. A usage cap's hold is not lifted by `resume`, which says what holds and when it lifts; `--override-cap` runs through it until its window resets. |
| `reset` | Close the open run and start a new one: usage counts from now. Nothing restarts. |
| `reload [--dry-run]` | Apply a `.swarm.toml` edit to the running swarm. |
| `layout [name]` | Show or change the worker-pane arrangement, live. |

## Looking

| command | what it does |
|---|---|
| `status [--all] [--json]` | A drain and what it still waits for, slots, merge queue, waiting and parked sessions, operator queue, owner-run rows only you can do (`owner_rows` in `--json`), owed pushes, the big-picture doc's last refresh, the whole ledger's standing counted as the dashboard counts it (`phases:`; `phases` in `--json`), this machine's done records as counts per status (naming failures), board address, kept processes. `--all` prints the whole done map; `--json` prints the state as JSON. |
| `context` | The JSON snapshot the launcher works from: `ready`, `launchable`, free and busy slots, `waiting`, `parked`, `ledger_issues`. |
| `doctor [--json]` | Diagnose a stuck or unhealthy swarm. Exit 1 on any FAIL. |
| `why <phase> [--tree] [--json]` | Why this phase is not running, down to the root blocker. |
| `report [--decisions] [--phase P] [--json]` | What every phase did, with its recap and timings. |
| `usage [-n N] [--json]` | This run's and past runs' usage per hour, and how old the newest sample is. |
| `overseer [--now] [-n N] [--json]` | Recent Overseer passes and pending reasons. `--now` requests a pass. |
| `big-picture [--now] [--json]` | When the big-picture doc was last refreshed and how that pass ended. `--now` requests a pass. |
| `tui` | The dashboard (window 0). |
| `web [--host H] [--port N]` | Serve the read-only board. |
| `telegram-bot` | Answer `/usage` and `/help` from the owner's Telegram chat, in the foreground. `swarm up` starts it detached. |
| `check [--strict]` | Preflight: Telegram, ledger, prompt lint. A dependency cycle counts only among rows not yet done: a ticked or recorded row holds nobody up, as on the board. Exit 1 on a failure or a contradicted prompt line; `--strict` also fails on a wasteful one. |

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
| `done <phase> [ok\|operator\|fail\|blocked\|later] ["recap"] [--after YYYY-MM-DD] [--force]` | Signal completion. Writes the sentinel, then pings, routes and pokes as the status says. `blocked` and `later` are handled as `fail`; `blocked` phases that finish close together share one ping; `later` pings nobody and, with `--after`, keeps the row from running before that date. The recap is also queued for the ledger (below). Refused, before anything is written, for a malformed phase id, a phase no worker is running, or (inside a worker) a phase other than the worker's own. A repeat for a phase already recorded is a no-op. |
| `waiting <who> ["question"]` | Tell the owner you are blocked on them: `<who>` is a worker's phase, an operator job's id, or `overseer`. Pings once, arms the park timer. |
| `resumed <who> ["answer"]` | The owner answered. Records the answer as an owner decision and cancels the park. |
| `note <phase> [decision\|assumption\|risk] "text"` | Log a judgement call, silently. |
| `build <cmd…>` | Run a heavy build through the swarm-wide gate. |
| `notify "message"` | Message the owner through the swarm's own sender, the only way a session should. |
| `keep --name N --why "one line" [--cwd DIR] -- <cmd…>` | Leave one process running after your session ends (everything else a session starts is ended with it). Starts it detached without the session's markers, records `<state>/keep/N.json`, prints its pid and log. `--why` is required (≤120 chars); a live name is refused, a dead one replaced. Use it only when something must outlive the session, and name it in your recap. |
| `keep --list [--json]` / `keep --stop N` | Every kept process, alive or dead, with why, who and age / stop one (SIGTERM, then SIGKILL, to its group) and forget it. |

## Reporting: the ledger and the history

The swarm is the only writer of the ledger, the phase history (`[tasks].history`)
and the lessons file. Sessions report; the supervisor applies each report on the
target branch in the project checkout, under the umbrella's merge lock, and
commits and pushes it itself. A worker's reports land with its phase: after the
merge for an outcome that integrates, at once for one that does not. Everything
else lands at once. See [components.md](components.md#the-ledger-writer).

| command | what it does |
|---|---|
| `done <phase> <outcome> ["recap"]` | (worker) Ticks the row when the phase lands (`ok`, `operator`), sets its short status (`done (date)`, `failed (date)`, `blocked (date)`, `later, after <date>`), and files the recap and the phase's `swarm note` decisions in its history. |
| `record <phase> done\|failed\|blocked\|later\|note ["text"] [--after YYYY-MM-DD]` | (operator, Overseer) The same for a row with no worker of its own: `done` ticks it, `note` only files the text. Refused for an id with no row. |
| `follow-up <phase> <new-id> --title "one line" [--needs a,b] [--dir d] [--tag t] ["what it must deliver"]` | File a new open row after `<phase>`'s section, with the text in the new phase's history. Refused at once for a taken id, a need with no row, or a new dependency cycle among rows not yet done; `[tasks].ledger_gate` is run when it is applied, and a rejected row goes into `<phase>`'s history instead. |
| `lesson <phase> "text" [--title T]` | Append `## (date, `phase`) title` and the text to `[tasks].lessons`. |

`python -m swarm_orchestrator.ledgermigrate --project-dir DIR [--gate CMD] [--components] [--write]`
moves a ledger's accumulated notes into the history once (and the dated entries
of `docs/STATUS.md` into `<history>/STATUS-archive.md`), proving the ledger reads
the same before and after. `--components` does the same to each component repo's
`docs/STATUS.md`, into that repo's own archive; `--repos DIR…` does only those
repos. Run it with the swarm stopped.

## Operator

| command | what it does |
|---|---|
| `operator <phase>` | Open an operator session for a phase's hand-off now (or the moment the phase merges, if it has not yet), overriding a `later` triage. Builds the job from its sentinel if needed. |
| `operator-add "brief" [--phase P] [--not-before <when>]` | Queue an ad-hoc job. `--not-before` holds it until `<when>` (`90m`, `6h`, `3d`, `2026-09-30`, `"2026-09-30 08:00"`). |
| `operator-triage <job>` | Decide `now` or `later` for a queued job. `swarm done` spawns it. |
| `operator-done <job> ["outcome"] [--attention] [--not-before <when>]` | The session is finished. Records the outcome, merges its mirror. `--attention` sends the outcome to the owner's phone; a decision only the owner can make is asked first with `swarm waiting <job> "<question>"`. `--not-before` means "not yet": the job goes back in the queue until `<when>` instead of finishing, the attempt is not counted, and its next brief says why the last attempt ended. |

## Overseer

| command | what it does |
|---|---|
| `overseer-done ["summary"]` | End the pass. Records the summary and merges its mirror. |

## Big-picture pass

| command | what it does |
|---|---|
| `big-picture-done ["summary"]` | The draft is written. Refused while the draft is missing or over the size cap, so the session can fix it; otherwise the swarm closes the window and commits the doc. |

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
