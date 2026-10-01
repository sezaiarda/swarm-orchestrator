# Command reference

Every `swarm` subcommand, grouped by purpose. `swarm <command> -h` prints the
exact flags.

`swarm [--config PATH] [--project-dir DIR] <command>`. Every command takes `-h`.

The project is `--project-dir`; without it, the project of the session the
command runs in (`SWARM_PROJECT`, set for every worker, operator job and
Overseer pass), and outside a session the current directory. A session's
command therefore answers for its project from a mirror, a component repo
inside one or any other checkout. To check the files of the folder you are in
from inside a session (a mirror's edited prompt, say), pass `--project-dir .`.

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
| `status [--all] [--json]` | A drain and what it still waits for, slots, merge queue, waiting and parked sessions, operator queue, owner-run rows only you can do (`owner_rows` in `--json`), the count of your to-dos (`owner to-dos:`; `owner_todos` in `--json`), owed pushes, the big-picture doc's last refresh, the whole ledger's standing counted as the dashboard counts it (`phases:`; `phases` in `--json`), this machine's done records as counts per status (naming failures), board address, kept processes, the build gate's line (slots busy, builds waiting), and a resources line (host CPU, memory and disk; an idle build holder gets a line of its own). `--all` prints the whole done map; `--json` prints the state as JSON. |
| `todo [--json]` | What waits on you that is not a question: owner-run and excluded rows that are ready and whose row says you do something, queued operator jobs that need your devices (folded into the row that closes them), to-dos a finish sent you, and the Overseer's latest "Left for the owner". Ordered by what each unblocks, then by whether time must pass afterwards. `--json` adds each item's full text, files and the commands that close it, plus rows coming later and owner rows left out as standing targets. |
| `guide` | Open the owner guide: a Claude chat in its own tmux window, `guide`, that walks you through `todo` one item at a time and records what you report with `record`, `operator-done`, `follow-up` and `lesson`. Again while it is open moves you back to it; it closes when the session ends. `g` in the dashboard does the same. |
| `console [--new]` | Open the owner console: your own Claude session for the swarm, in the `console` window after the dashboard. Reopens the stored conversation (`--resume <id>`) when you closed it, only moves you there while it runs, and attaches your terminal when run outside the session. `--new` starts a fresh conversation (refused while one runs: `/exit` it first). `o` in the dashboard does the same without `--new`. |
| `context` | The JSON snapshot the launcher works from: `ready`, `launchable`, free and busy slots, `waiting`, `parked`, `ledger_issues`, and `lanes` (`{"enabled": false}` unless [`[lanes]`](config.md#lanes) is on; then what each phase in flight holds and what each held-back row waits for). |
| `doctor [--json]` | Diagnose a stuck or unhealthy swarm. Exit 1 on any FAIL. |
| `why <phase> [--tree] [--json]` | Why this phase is not running, down to the root blocker. |
| `report [--decisions] [--phase P] [--json]` | What every phase did, with its recap and timings. |
| `resources [--hours H] [--days D] [--json]` | What the host, each gated build and each session use: now (CPU, pressure, memory with page cache apart, swap, disk throughput and real free space, each running build and session), the last `H` hours (default 24) as sparklines, the finished builds with the worst peaks, the builds the gate set aside as idle and for how long, and a capacity estimate over the last `D` days (default 30): p95 per build and per worker, and whether 2 concurrent builds, 8 workers or doubled jobs would fit this host, with the arithmetic. Reads what the supervisor's sampler wrote; see [components.md](components.md#resource-tracking-swarm-resources). |
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
| `widen <phase> <touch…>` | Add touches (the [`touches:`](config.md#lanes) grammar) to the lane `<phase>` holds, so the launcher keeps rows that overlap them waiting. Run it before editing outside your declared lane (`$SWARM_TOUCHES`). The lane only grows: it starts from what the phase holds now. Prints `<holder> holds <touch>: your merge will be re-tested against it and may need a resolver` for each phase in flight already holding an overlapping touch. Exit 0 once the touches are recorded, whatever it printed; 2, recording nothing, for a touch that does not parse or a phase not in flight. |
| `note <phase> [decision\|assumption\|risk] "text"` | Log a judgement call, silently (`decision` by default; `--kind K` also sets the kind). |
| `build [--timeout D] [--script FILE] [--hold] [--] <cmd…>` | Run a heavy build through the swarm-wide gate: it queues in arrival order (a usually-short build may pass a long one, boundedly), then runs with the build's own exit code. Light commands (no compile: `git`, `cargo update`/`metadata`/`fmt`, `bake --print`, …) run at once. A heavy one is checked first (program, `cd` target, `-f` file, manifest) and refused (127 / 2) if it cannot work. While queued it prints its place, the holders and an ETA on stderr. A holder that does nothing for `[build].idle_yield_s` is set aside (never stopped), and the next build starts beside it; the command is told so on stderr, then and as its last line. `--hold` takes a slot whatever the command is and keeps it however idle it looks (a measurement that sleeps), so nothing starts beside it. `--timeout` counts from the start (exit 124); `--script FILE` or `-- sh -c 'a && b'` runs several steps in one turn. See [components.md](components.md#build-gate-swarm-build). |
| `build --status [--json]` | The gate now: who holds each slot and for how long, the idle holders that were set aside (`yielded after 2m30s idle, still running 12m`: they keep running and do not count), the queue in the order it would start with ETAs, and the last builds' wait and run times. Read-only. |
| `notify "message" [--attention]` | Message the owner through the swarm's own sender, the only way a session should. `--attention` sends an Overseer summary that needs the owner whatever triggered the pass. |
| `notify --ack` | Acknowledge the pings that never reached the owner's phone: the dashboard and `doctor` count only drops after this. Sends nothing; the ping log is kept. |
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
| `done <phase> <outcome> ["recap"]` | (worker) Ticks the row when the phase lands (`ok`, `operator`), sets its short status (`done (date)`, `failed (date)`, `blocked (date)`, `later, after <date>`), and files the recap and the phase's `swarm note` decisions in its history. With lanes off, a tick carries the row's open needs to its open dependents; with `[lanes] enabled` it carries nothing and the supervisor log says `CARRY-SKIPPED <phase> lanes`. |
| `record <phase> done\|failed\|blocked\|later\|note ["text"] [--after YYYY-MM-DD]` | (operator, Overseer) The same for a row with no worker of its own: `done` ticks it, `note` only files the text. Refused for an id with no row. |
| `follow-up <phase> <new-id> --title "one line" [--needs a,b] [--dir d] [--touches t1,t2] [--tag t] ["what it must deliver"]` | File a new open row after `<phase>`'s section, with the text in the new phase's history. Refused at once for a taken id, a need with no row, or a new dependency cycle among rows not yet done; `[tasks].ledger_gate` is run when it is applied, and a rejected row goes into `<phase>`'s history instead. `--touches` (`repo/path`, `repo/dir/**`, `./path`, `@resource`) is written as the row's `touches:` field after `needs:` and as a `touches:` line in the history; it is refused for a touch outside `--dir` (the id prefix's repo when there is none) or a `--dir` repo with no touch, and it is **required while `[lanes] enabled`**. |
| `reshape <by> <row> [--needs a,b] [--add-needs a,b] [--drop-needs a,b] [--touches t1,t2] "why"` | Edit an open row's `needs:` or `touches:`, the one edit a session makes to an existing row. `--needs` replaces the list (`''` empties it), then `--drop-needs` and `--add-needs` edit it; `--touches` replaces the field, under `follow-up`'s rules. Refused at once for a ticked, unknown or `[tasks].exclude`d row, a need with no row, a dropped need the row does not have, a new dependency cycle, or an edit that changes nothing. It lands at once like `record`: the writer checks it again on the target branch and runs `[tasks].ledger_gate` on the edited ledger; a failing gate leaves the ledger byte for byte as it was and records the refusal in `<by>`'s history (the row's own when `<by>` is not a row). On success the row's history gets *reshaped by `<by>`: needs −x +y; touches → …; why: …*. With lanes on, `--touches` on a phase in flight (busy, waiting, parked or queued to merge) also replaces the lane it holds, so rows waiting on its old lane can launch (log: `LANE-RESHAPED`); a touch its worker widened after the reshape was filed is kept. It is refused, at once and again by the writer, naming each path, when the new lane leaves out a file the phase changed against its base in a repo its row names (commits on `swarm/<phase>`, edits and untracked files in its mirror; commons do not count), or drops a held touch whose changes cannot be read (a resource, an external repo, any repo under isolation `none`) unless it keeps that touch or its whole lane. `<by>` is your phase, or your role (`overseer`, `operator`). |
| `lesson <phase> "text" [--title T]` | Append `## (date, `phase`) title` and the text to `[tasks].lessons`. |

`python -m swarm_orchestrator.ledgermigrate --project-dir DIR [--status PATH] [--gate CMD] [--components] [--write]`
moves a ledger's accumulated notes into the history once (and the dated entries
of `docs/STATUS.md` into `<history>/STATUS-archive.md`), proving the ledger reads
the same before and after. `--components` does the same to each component repo's
`docs/STATUS.md`, into that repo's own archive; `--repos DIR…` does only those
repos. Run it with the swarm stopped.

## Operator

| command | what it does |
|---|---|
| `operator <phase>` | Open an operator session for a phase's hand-off now (or the moment the phase merges, if it has not yet), overriding a `later` triage. Builds the job from its sentinel if needed. |
| `operator-add "brief" [--phase P] [--not-before <when>]` | Queue an ad-hoc job. `--not-before` holds it until `<when>` (`90m`, `6h`, `3d`, `2027-01-15`, `"2027-01-15 08:00"`). |
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
| `_drain-down` | The stop a `down --drain` runs once the running work is finished. |
| `_console-pane` | What the `console` window runs: starts the console's `claude` (resuming its conversation), and when it exits waits for Enter to start it again. |
| `_poke-done <phase> <status>` | The delayed `done` poke a `done_grace_s` child delivers. |
| `_lane-check <phase> <repo>` | The landing re-test under lanes, started detached by the integrator. Runs `[lanes].check` for `<repo>` (its name, `.` for the project repo; else `check["*"]`, else nothing) in `<phase>`'s own worktree, through the build semaphore, for at most `check_timeout_s` (a timeout is red). Writes `<state>/landing/<phase>.<repo>.log` and pokes `lane-checked <phase> <repo> ok\|fail`. Exit 0 green, 1 red, 2 unknown repo. |
| `lane-checked <phase> <repo> ok\|fail` | The FIFO event `_lane-check` sends: the supervisor looks at the merge queue again. |
