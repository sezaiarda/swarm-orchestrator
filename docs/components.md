# The cast, in detail

What every moving part of swarm-orchestrator is, when it runs, what it decides and
what it may not do. The [README](../README.md) has the short version and the
diagrams; [config.md](config.md) has every setting; [cli.md](cli.md) every command.

## Supervisor

**What it is:** one long-running Python process (`swarm _supervise`), started
detached by `swarm up`, with no terminal of its own. It is the only reader of
`control.fifo`, so every event reaches it in a single total order. It is the only
process that spawns or kills the overseer pane, opens the operator session, and
drives the merge queue.

**When it runs:** from `swarm up` until the run finishes, `swarm finish`, or
`swarm down`. `swarm restart` replaces the process and leaves the run going
(see [Restart](#restart-swarm-restart)).

**What it decides:**

- **Launching.** It launches ready phases straight from the ledger, in ledger
  order, into free slots, one thread per launch. No model decides what starts.
  A row ticked `[x]` with no record of the swarm's counts as done, as on the web
  board; a recorded `fail` still wins over a tick.
  With `[lanes] enabled` it walks `lanes.pick` instead of the plain ready list:
  rows whose touches overlap nothing in flight launch, waiting rows reserve their
  touches, and `[lanes] per_repo` caps phases per repo.
- **Merging and finishing.** It merges finished phases through the queue, and it
  finishes the run once nothing is busy, waiting, parked, launching, queued, held,
  owed as a push or owed as an operator hand-off, and no Overseer pass is due.
- **Failed launches.** A launch that fails (the worktree, the pane, or the boot,
  which is retried once) waits 60 s before it is tried again. After three failures
  in a row the phase is given up, and you are told once. `swarm launch <phase>` or
  `swarm resume` hands it back.
- **Scheduling.** It decides when to park a waiting worker, when an Overseer pass
  is due, when to run gc, and when to push a backup of unmerged work
  (`[backup]`).
- **Draining.** While `swarm down --drain` holds, nothing launches and no
  operator job or Overseer pass opens. On every event it records what the stop
  still waits for (`State.drain`, shown by `status`, the dashboard and the
  board). A parked session the owner has answered (`State.answered`) holds no
  slot but is at work, so it is waited for and named: `1 worker (P3 in its own
  window)`; one that still asks is not. When that is nothing it telegrams once
  and starts `swarm _drain-down` in a session of its own, which runs
  `swarm down` and then the after-command
  (in a `systemd-run --user --scope` where it can, so neither the tmux teardown
  nor a logout takes it).

**What it may not do:** it never builds, never retries a phase that finished
`fail`, and never restarts a worker. An exception in one event handler is logged,
telegrammed and stepped over. Only a failure of the loop itself ends the process,
and that announces itself (`SUPERVISOR-CRASH` plus a ping). Everything it does is
logged to `<state>/logs/supervisor.log`.

**Watchdog** (`[swarm].watchdog_s`, default 300, `0` = off): the supervisor's one
periodic poll. On each sweep it:

- frees a busy slot whose worker is gone, once seen on two sweeps in a row, and
  keeps that phase's work for its next launch, which resumes on the same branch.
  Gone is a pane tmux no longer has, and a pane that is still there with nothing
  of the worker in it: no `swarm done`, a command other than `claude`, and no
  process of the worker's session left. A slot that was claimed and whose worker
  was never started counts once the claim is 3 minutes old, and as a failed
  launch: it waits a minute, is retried, and after 3 in a row you are told. A
  slot whose session still has a process, or whose launch the supervisor is
  still running, is left alone, and `swarm doctor` says which. A tmux that
  errors, hangs or has lost the session reaps nothing. A worker that dies 3
  times within an hour is not restarted again, and you are told;
- settles a parked session that is gone, once seen on two sweeps in a row. A
  parked session is in no slot, so the check above never sees it, and it
  leaves `parked` only by reporting. It is gone when its `wait:` window is
  gone or nothing runs in it any more, and, where no window says, when no
  process of its session is left. Each kind is settled the way it is when it
  dies anywhere else, with its marks and its window cleared, whatever of it
  still runs ended, and its work kept: a worker like one that died in its slot
  (started again on its branch, held after 3 deaths in an hour), an operator
  job back to its queue for another attempt in the same mirror, an Overseer
  pass as `interrupted` with what it committed landed. A worker that has
  reported and is waiting for its work to land is not gone, nor is a session a
  restart is carrying, nor one tmux could not be asked about. The supervisor
  says it does this with `reap-parked` in its mark file;
- relaunches when the swarm has been idle for a full interval with free slots and
  ready phases;
- finishes a run that has settled;
- tells you if a finished run still had ready phases;
- retries owed pushes, at most every 15 minutes.

A swarm that is moving is never touched. The sweep exists because a supervisor
that only reacts to input cannot notice a worker killed out-of-band, which never
sends `done`.

## Restart (`swarm restart`)

The CLI is an editable install, so every `swarm` command runs the code on disk.
The supervisor and the dashboard are long-running processes and run what was
there when they started. `swarm doctor` warns when the supervisor is older than
the installed code (`restart`), and `swarm restart` is what loads it.

The request is one record, `<state>/restart.json`: an id, the kind, when it
happens, who asked (the session the command ran in, from its `SWARM_SESSION_ID`
or phase marker, else `owner terminal`), and how far it has got. It is kept out
of `state.json` because a supervisor on older code rewrites that file without
the keys it does not know. `status`, `doctor`, the dashboard and the board read
it, and every step is a `RESTART-*` line in the supervisor log.

**In place (the default).** Only the supervisor process is replaced, then the
dashboard, the web board and the Telegram listener are started again. The tmux
session, the worker panes, parked sessions, the operator, the master pane and
the owner console are not touched, and nothing is drained.

1. `swarm _restart-run` starts, detached from every session, and opens the
   control FIFO itself. A pipe keeps what is written to it while anything has it
   open, so a `swarm done`, `swarm waiting` or `swarm resolved` sent while no
   supervisor is reading waits in the pipe instead of being dropped.
2. It sends `handover <id>`. The supervisor launches nothing new and hands over
   at the next safe point: no launch thread, Overseer or big-picture session
   start, clean-up or backup push under way. A merge is never interrupted: it
   runs inside one event, and the request is read between events. Until then
   `status` says what it waits for. After 15 minutes without a safe point the
   restart is given up, the supervisor carries on, and you are told.
3. The supervisor writes what it held only in memory to `handover.json` (failed
   launches and their back-offs, crash counts, ping cooldowns, the backup
   clock), puts any event it had read and not handled back into the pipe, and
   exits without ending the session in the master pane, the operator's session
   or a big-picture pass.
4. A new supervisor starts with `--adopt`, in the old one's environment (read
   from `/proc`, so every `SWARM_*` override still applies). It does not rebuild
   state. It takes the slots, panes, `waiting` and `parked` as recorded; gives a
   slot back the pane tmux tags as its own if the state names one that is gone;
   moves from the old supervisor's settings (`config.json`) to the file's the
   way `swarm reload` does, so a changed worker count adds or retires slots and
   restart-class settings stay held;
   picks up the pass in the master pane and the operator's lease; frees a slot
   that is claimed with no worker in it; acts on a `done` sentinel whose poke
   nobody read (after `done_grace_s`, and only one written since the phase was
   last claimed: an older one is from an earlier attempt); drops a `shutdown`
   or `handover` left in the pipe for the supervisor before it; then pumps the
   merge queue and fills free slots. Park deadlines, a scheduled pause, the Overseer's deadline and
   usage holds are timestamps in the state, so they carry over unchanged.
5. If no supervisor comes up (two attempts), you are telegrammed that the swarm
   is unsupervised and that `swarm restart` brings it back. Sessions are still
   running at that point; nothing was closed.

**A supervisor that predates the command** knows no `handover`. It is told to
`shutdown` instead, which on its way out ends whatever runs in the master pane
and the operator window and cuts its own launch threads off. So the restart
first pauses launching, waits until none of those is running (an init or
Overseer pass, an operator job in the operator window, a launch in flight, read
from the log), then stops it, starts the adopting supervisor and lifts the
pause. Workers and parked sessions are untouched, as above. A big-picture pass
in flight is ended and runs again later.

**With no supervisor running** (it crashed, or was killed) `swarm restart`
starts one that adopts the run as it stands. `swarm down && swarm up` rebuilds
the run instead and closes every parked session.

**Scheduled** (`--in`, `--at`). The supervisor fires it: the plan's time is one
of its wake-ups, and when it is due it starts `swarm _restart-run`. If that
helper dies without settling the plan (the new code does not import, say), the
supervisor is still running and tells you. A supervisor that predates the
command cannot fire it, so the helper is started at once and sleeps until the
time. `swarm up` and `swarm down` cancel a planned restart: the code on disk
loads anyway.

**Full (`--full`).** A drain (`State.drain`, with `restart` and `questions`
set), then `swarm _drain-down` runs `swarm down` and `swarm up` instead of the
after-command. A session waiting on you would die with the tmux session, so:

| | what happens to a session that waits on you |
|---|---|
| default | The restart is refused, naming each one and its question. |
| `--wait-questions` | The drain waits for them too (`N questions` in `status`). |
| `--keep-questions` | Each is carried across alive. |
| `--force` | Closed with the session. Its work is kept (`swarm up` sets it aside). |

A parked session the owner has answered does not wait on anyone: the drain
waits for it as for a worker in a slot, under every one of the four. The
supervisor says it can do that with `drain-parked` in its mark file. Under one
that started before it could (the command runs the code on disk, the supervisor
the code it started with), such a session is still listed with the questions as
`working on your answer`, the default still refuses for it, and a full restart
told to go ahead replaces the supervisor in place first, then drains. With no
supervisor running nothing waits for anything, and it is listed too.

Carrying one across: once the drain is over the supervisor parks every session
that asked and is still in its home pane; the windows are moved to a holding
tmux session (`<session>-kept`), recorded in `kept-sessions.json`; `swarm down`
spares the processes that carry those sessions' markers; `swarm up` leaves their
mirrors alone (no set-aside, no merge), keeps an operator job's item `waiting`,
and before the supervisor starts moves the windows into the new session and
puts the keys back in `parked` with their lanes. The process is never ended, so
the question is still on screen. `claude --resume` is not used for this: a
resumed session does not show the question it had open. If the swarm does not
come back up, the kept sessions stay in the holding session and the next
`swarm up` brings them back.

## Workers

**What they are:** full, unrestrained Claude Code sessions, one per slot, started
as `cd <cwd> && exec <worker_cmd> -n 'swarm · worker · <phase>' --settings <worker_settings> --effort <effort>`.
Their teammates run in-process (`teammateMode`), so they never open extra panes.
Every session the swarm opens carries such a display name, so they stand apart
in claude's `/resume` picker: `swarm · overseer`, `swarm · init`,
`swarm · operator · <job>`, `swarm · resolver · <phase>`, `swarm · guide`,
`swarm · big-picture`, `swarm · console`. A command that sets its own `-n` keeps it.
Each worker's environment carries:

- `SWARM_PHASE`, `SWARM_SESSION_ID=worker:<phase>` and `SWARM_STATE_DIR`
  (everything it starts dies with it: see [Processes](#processes-everything-dies-with-its-session-swarm-keep-is-the-exception));
- `SWARM_PROJECT`, the canonical project. The session's `swarm` commands read
  the settings and the ledger from it, whatever folder they are run from: a
  component repo has no `.swarm.toml`, and a mirror's ledger is the copy
  branched at launch. An explicit `--project-dir` wins;
- a private `TMPDIR` on disk under `<state>/tmp/<phase>`;
- `CARGO_INCREMENTAL=0`;
- under worktree isolation, also `SWARM_WORKTREE`, `SWARM_MAIN` and the
  build-gate limits.

The folder-trust dialog is accepted ahead of time for every new mirror. If it
appears anyway, in a worker's pane or any other session's, the wait for the
session to boot answers it: it reads the answers and the cursor from the pane,
walks the cursor to the answer that trusts the folder one Up or Down at a time,
and presses Enter only once two looks in a row show the cursor there (logged
`TRUST-ACCEPT`). Enter takes whatever the cursor is on, and since Claude Code
2.1.286 the cursor starts on "No, exit", so nothing is pressed when the cursor
or the trusting answer cannot be read; the wait then ends in
`READY-TIMEOUT … folder-trust dialog not answered: <why>`.

The prompt is typed in (pasted, for a slash command), then checked: it must
actually appear above the input box before the launch counts.

Every send, to a worker or to any other session, empties the input box first.
One Ctrl-U removes a single screen row, so the box is read back and the key
repeated (Ctrl-K for text behind the cursor) until it is empty; the faint hint
an empty box shows does not count as text. A box that will not empty, which is
what a dialog holding the pane looks like, gets nothing typed into it: the send
fails as `box-not-cleared` rather than run two messages together.

**What they decide:** everything inside their phase. The project's own worker
command (patched by the init pass) is their directive. It tells them to:

- build the phase;
- run heavy builds through `swarm build`;
- record the small calls they make with `swarm note`;
- ask you only about calls that are genuinely yours, or when in doubt about
  something that matters (`swarm waiting`, see
  [Answering the swarm](../README.md#answering-the-swarm));
- classify their own finish:
  - `ok`: clean success. It merges silently.
  - `operator`: merges exactly like `ok`, and the recap becomes the brief for an
    operator job.
  - `fail`: it is rolled back under worktree isolation, you are pinged, and the
    phase's dependents stay blocked until `swarm retry`.

**What they may not do:** under worktree isolation, they commit on `swarm/<phase>`
and never push; the integrator pushes. Nothing else is clamped: tools,
permissions and scope are the session's own.

`swarm done` prints what it did: whether the sentinel was written, the telegram,
the operator route, and the FIFO poke. A second call with a shorter recap never
overwrites a fuller one (`--force` to replace it), and every attempt is appended to
`done/<phase>.jsonl`. A recap under 20 characters or 4 words is too thin to brief
an operator. The sentinel is still written, but no job is queued.

## Init pass and the Overseer

Both run in the `overseer` window, one at a time, and both are spawned and killed
only by the supervisor.

**The init pass** runs once per `swarm up`, and the first launch waits for it
(prompt: `prompts/init_master.md`). It:

- runs `swarm doctor` for the Telegram preflight (a missing Telegram setup is
  noted, never blocking);
- runs `swarm context` and reports ledger cycles or unknown dependencies with
  `swarm notify`;
- patches `[worker].command_file` for swarm mode (skip the phase picker, the
  self-classified `swarm done`, `swarm build`, `swarm note` / `waiting` /
  `resumed`, handing a review to the operator (`swarm done <phase> operator
  "<brief>"`) instead of opening an ask, the cost rules) and commits it, so
  every mirror inherits the patch;
- runs `swarm master-idle`.

It never asks you anything. If it cannot start, or dies without idling, launching
goes ahead without it.

**The Overseer** is a full Claude session that looks over the whole run and acts on
it (prompt: `prompts/overseer.md`). It is on by default (`[overseer]`).

- **When it runs:** a pass is triggered by events and counters, collected into one
  pending list (see [the diagram](../README.md#init-pass-and-overseer)). Only one
  pass runs at a time. Passes are `min_gap_s` apart, unless a reason is urgent: a held merge
  queue, a doctor FAIL, starvation, or `swarm overseer --now`.
- **Merge holds:** a conflict the resolver is working on is left to it. The hold
  triggers a pass only when no resolver opened, the resolver gave up (it messaged
  you, or ran `swarm resolved` on an unfinished merge), or the hold is older than
  `hold_wait_s`. The trigger is checked again just before a pass starts.
- **What it reads:** before each pass the supervisor writes
  `<state>/overseer/digest-<id>.md` (and `.json`). It holds the trigger, the swarm
  now, every phase finished since the last pass with its recap and notes, every
  operator job finished since then with its outcome (flagged ones first),
  failures, questions waiting on you, the `owner-run` rows whose dependencies
  have landed and are holding rows up, a starvation map (which root
  blockers hold how much backlog), and a snapshot of RAM, swap, `/tmp` and disk.
- **Where it works:** under worktree isolation, in its own mirror `ovs-<id>`,
  which merges through the ordinary queue when the pass ends.
- **What it decides, on its own:**
  - retry a failed phase once;
  - free a dead slot;
  - `swarm resolved` a hold it has fixed;
  - edit the ledger so free slots have work (split serial chains, file follow-up
    rows from workers' risks and decisions);
  - queue operator jobs, including for an `owner-run` row that waits on a
    review or a pick (`swarm operator-add --phase <row> "<brief>"`);
  - run `swarm gc`;
  - `swarm pause` when the box is in danger;
  - send you a digest of six lines at most.
- **What it may not do:**
  - answer a worker's question;
  - restrain a worker: edit its prompt, limit its tools or narrow its phase;
  - lift a pause you made;
  - run `done`, `up`, `down` or `finish`;
  - make owner-level calls (money, taste, scope, deleting work, reversing your
    written decisions). Those go to you through `swarm waiting overseer "<question>"`.
- **How it ends:** it closes with `swarm overseer-done "<summary>"` and leaves
  `<state>/overseer/<id>.md` with *Saw*, *Did* and *Left for the owner*. A pass
  that runs past `timeout_s` is killed and its commits are merged anyway.
  `swarm overseer` lists recent passes.

## Operator

**What it is:** one Claude session in the `operator` window that carries out
concrete work a phase could not wait on: deploys and rolls, checks after a deploy,
provisioning, downloads, chores that span repos (prompt: `prompts/operator.md`). It
works with your authority, so it is **off unless you set
`[operator].enabled = true`**. While it is off, each hand-off is telegrammed to you
as a to-do instead of being dropped.

**Where jobs come from:**

- a phase finishing `swarm done <phase> operator "<recap>"`, where the recap is
  the entire brief;
- `swarm operator-add "<brief>" [--phase P]`, used by you or the Overseer;
- `swarm operator <phase>`, which builds a job from an old sentinel by hand.

**The queue:** one JSON file per job under `<state>/operator/`. `swarm done` writes
it after the sentinel and before the FIFO poke, so a crash between the two costs
nothing: `swarm up` rebuilds a missing item from its sentinel, and requeues jobs a
dead run left running or waiting. Every item file is changed under one lock
(`<state>/operator/.lock`), so a lease reclaim racing an `operator-done` can no
longer write a finished job back to queued.

**Triage:** `swarm operator-triage <job>` asks a cheap model (`triage_model`,
default `haiku`) whether the job should run `now` or `later`. Every odd answer
counts as `later`. `now` opens the session at once, or at the merge if its phase
is still building or merging: no job opens before its work is on main. Any job
opens when its phase has merged (unless triage said `later`), or from the queue
sweep, which runs on every supervisor wake and opens the oldest due job whose
phase is no longer building or merging. A `later` job waits for room: the sweep
holds it until a worker slot is free and nothing is launching or ready to launch
(a phase parked on you does not count; other phases may still build or merge,
since the session takes no slot). It never waits longer than
`[operator].later_wait_s` (3 h by default): then it opens anyway, oldest first.
`swarm operator <phase>` by hand turns a `later` into `now`.

**Leases and limits:** only one job runs at a time, under a lease in `state.json`.
The lease lasts 1 h, or 7 days while waiting on you. A job gets 3 attempts, with
5 minutes between them. After that it is `abandoned` and you are told once.

**Where it works:** under worktree isolation, in its own mirror `op-<job>` (reused
by a retry), merged through the ordinary queue on `operator-done`. Without
worktree isolation, it works in the project itself.

**What it decides:** how to carry out the brief. It first checks whether a later
phase or you already did the work. It narrates each action and prefers the step
it can undo. A decision only you can make is asked before the job finishes:
`swarm waiting <job> "<question>"`, then, once you answer in its pane,
`swarm resumed <job> "<answer>"`; your answer is recorded as your decision on
the job's phase, and follow-up work is queued with `swarm operator-add`. It
ends with `swarm operator-done <job> "<outcome>"`. `--attention` simply sends
that outcome to your phone (you must act, something the brief asked for is not
done or still owed, or a check came back bad); every other outcome is recorded
(on the job, in `notifications.jsonl` marked `suppressed`, on the dashboard)
and reaches you in the Overseer's next summary, which lists every operator job
finished since its last pass. `[operator].notify = "all"` pings every outcome
again; `"none"` pings none. Questions and abandoned jobs always ping.
`swarm operator-done <job> "<why>" --not-before <when>` means the job's moment
has not come yet: it goes back in the queue until `<when>` (`90m`, `6h`, `3d`,
`2026-09-30`, `"2026-09-30 08:00"`) instead of finishing, the attempt is not
counted, and its next brief says why the last attempt ended.
`swarm operator-add --not-before <when> ...` holds a new ad-hoc job the same
way.

**What it may not do:** it asks you only about money, taste, unrecoverable data
loss, or contradicting something you decided in writing, through
`swarm waiting <job> "<question>"`. It never answers a worker's question. It
never runs `done`, `launch` or `finish`, and never creates branches of its own.
`swarm finish` refuses to stop the run while jobs are queued, unless you pass
`--force`.

## Waiting on the owner (`owner.py`)

**What it is.** One door for every session that needs the owner — a worker, an
operator job, or the Overseer pass. It runs `swarm waiting <who> "<question>"`,
asks the same question in its own pane with AskUserQuestion, and once you
answer there, runs `swarm resumed <who> "<answer>"`. `<who>` is a worker's
phase, an operator job's id, or `overseer`; inside an operator or Overseer
session the bare job id or `overseer` resolves from the session's own
environment, and `operator:<job>` / `overseer:<pass>` are accepted as written.

**The ping.** One plain Telegram message: the question in one line (a worker's
still leads with its cost line), then which tmux window to open, for example
"Answer in tmux window operator (tmux attach -t myproject); after 2 min it
moves to its own window wait:op-web-F2." A worker's ping goes every time it
asks; an operator job's or the Overseer's only when the question is new, so a
re-run cannot ring you twice.

**Parking.** Past `[worker].park_after` seconds (default 120, `0` disables it)
a session still waiting on you is moved, alive, to its own window, freeing
whatever it held so the swarm carries on: a worker frees its grid slot (window
`wait:<phase>`, unchanged), an operator job frees the operator window (window
`wait:op-<job>`) so the next job can run, and an Overseer pass frees the master
pane (window `wait:overseer-<pass>`) so a later pass can run. The parked
session keeps waiting there, carries on once you answer, and ends with its
usual `swarm done` / `operator-done` / `overseer-done` (which closes that
window). A parked session keeps the run from finishing, the same as one still
building. `waiting`/`parked` in `state.json` hold the keys `<phase>`,
`operator:<job>` and `overseer:<pass>`.

A parked session stays in its own window whether or not you have answered it,
so state also says which it is. `asked` holds, per parked key, when its
question without an answer was asked; `swarm resumed` on a parked key moves it
to `answered` (with the time), and from then on it is working there and waits
on nobody. A later `swarm waiting` from it is a new question with a clock of
its own. Only a session asking now counts as waiting on you: for the
Overseer's owner trigger (once per unanswered question), `swarm doctor`'s
`owner.blocking`, the digest, `swarm status` and the boards. A parked key with
neither mark, from a state file written before they existed, reads as asking.

A parked session that dies in its window, or whose window is closed by hand,
never reports. The watchdog's sweep settles it (see **Watchdog**), so it stops
reading as at work or as a question for you, and a drain stops waiting for it.

**Owner-run rows** (`[tasks].exclude`) are never sessions, so nothing ever asks
about them. A ready one (its dependencies have landed, it is not done or
ticked) that is holding other rows up shows up in "Needs you" in the dashboard
and the web board (kind "yours to do"), in `swarm status`
("yours to do: <row> (holds up N)") and `--json` (`owner_rows`), and in the
Overseer's digest. The supervisor pings you once per such row when it starts
holding rows up — recorded in `<state>/owner_rows.json`, so never twice, across
restarts too, with rows found together sharing one message — saying only you
can do it, how many rows it holds up, and to tick it in the ledger or run
`swarm skip <row>` once it's done.

## The big-picture pass

**Why it exists.** Every worker used to start by spawning a read-only survey of
the project (ledger, roadmap, repo statuses) before its first edit, and then read
most of the same files again itself: a large part of a worker's cost went on
orientation. The survey now happens at swarm level, every so often, and its
result is an ordinary doc in the project.

**What it is.** A Claude session (prompt: `prompts/big_picture.md`) in its own
tmux window, `big-picture`, that rewrites one bounded document
(`[big_picture].doc`, default `docs/BIG-PICTURE.md`): where the project stands,
what just landed, what is next and eligible, cross-repo contracts and hot spots,
and the conventions workers keep rediscovering. Its brief names the doc, the
ledger, the worker command file, the umbrella commit of the last pass and every
phase finished since, with recaps and notes.

**When.** Every `[big_picture].every` integrated phases (default 10), and, with
`max_age_h` set, once the doc is that old and something has landed since. The
first pass runs as soon as the doc does not exist. `swarm big-picture --now`
starts one by hand. One pass at a time; it takes no worker slot, and no launch,
merge or finish ever waits on it. A pass that produces nothing (it would not
start, ran past 40 minutes, ended without a draft, or wrote one over the 16 KB
cap) gives its counted phases back and the next one waits 15 minutes.

**How the doc lands.** The session changes nothing in the project: it works in a
scratch directory, writes its draft to `<state>/bigpic/<id>.md` and ends with
`swarm big-picture-done`. The supervisor then closes the window and commits the
draft to the target branch itself, the way it commits every file it writes
there: under the umbrella's integration lock, committing only the doc's path,
then pushing, with a failed push owed like a merge's. A project that is
mid-merge, on another branch, or holding uncommitted edits to the doc is waited
out, never overwritten; no new pass starts while a draft waits. The swarm never hands the doc to a worker: every mirror branched after it
lands has it, and the worker's command file tells it the doc exists.

`swarm status`, the dashboard headline and the web board's header show when it
was last refreshed. `<state>/bigpic/state.json` holds the counter, the last
pass and any draft waiting to land, so a restart loses none of them; a pass
running when the supervisor stops is ended with it.

## The ledger writer

Sessions never edit the ledger, the phase history or the lessons file
(`ledgerw.py`). They used to: every worker ticked its own row and appended its
notes to it, and operator, ask and Overseer sessions did the same, each on its
own branch. Rows grew large and the merge queue kept meeting the same row edited twice
(a common source of conflicts).

Now a session reports (`swarm done`, `record`, `follow-up`, `lesson`) and the
report waits in `<state>/ledger/<key>.json`. The supervisor applies it with
`gitq.commit_to_target`: on the main branch of the project checkout, under the
umbrella's repo lock (so it is serialised with integration), committing only the
ledger, the history directory and the lessons file, then pushing. A worker's
report is applied in `_advance_done`, which runs after the merge succeeds, so a
row is ticked only once its work is on main; a `fail` never ticks. When the
checkout cannot take it (off main, mid-merge, someone's edit in those files) the
report is held and the watchdog retries it; nothing is lost across a restart.

- **The ledger keeps state:** box, id, dir, needs, touches, a short bold title,
  tags, an `after:` date and `status:`. `touches:` is meta, so a tick, a status or
  an `after:` never turns it into prose or a title. With lanes off, a ticked row's
  open needs are carried to its open dependents, so ticking never lets two rows
  its chain ordered run together; with `[lanes] enabled` the carry is skipped and
  the log says `CARRY-SKIPPED <phase> lanes`.
- **`reshape`** edits an open row's `needs:` or `touches:`. The writer re-checks it
  on the target branch and applies it through `[tasks].ledger_gate`; a failing
  gate leaves the ledger byte for byte as it was and records the refusal in the
  filer's history. A `--touches` reshape of a phase in flight also replaces its
  `State.lanes` snapshot once the edit lands (`launch.relane`), but only if the
  new lane still covers every file its mirror changed against its base; else it
  is refused and names them.
- **The history** is `<history>/<family>.md` (the id up to its first `-`), one
  `## ` section per phase with dated `### ` entries. Past `history_split_kb` it
  becomes `<history>/<family>/<id>.md`. The web board's detail sheet shows it
  under the row.
- **`later`** lands nothing, like `fail`, and writes `after:` on the row; the
  launcher leaves the row alone until that date. It is recorded `fail` only
  until the row carries the date (`LATER-WAITS` in the log); from then on the
  record and its sentinel are gone and the row *waits for a date*: `swarm
  status` counts it so and lists it with its date, `swarm why` says "waits
  until", the Overseer's digest lists it under "Waiting for a date" and no pass
  is woken for it. While the ledger cannot take the report (the checkout is
  busy), every reader takes the date from the queued report and reads the done
  map without the record (`ledgerw.dated`, `ledgerw.not_failed`: the one copy of
  the rule): the launcher, `swarm status`, `swarm why`, the digest, the TUI's
  counts and alerts, the web board, the forecast and doctor's `phases.failed`.
  An open dashboard reads the dates again when the report queue moves, not only
  when the ledger does. On the board the row sits in *Blocked* with "waits until
  <date>", and a card behind it says "waits on <row>, which waits until <date>".
  On the date the row is ready again (`LATER-DUE`) and the launcher runs. A
  `later` with no date is a `blocked`.
- **A row and a record that disagree** are settled in one place, and by
  changing the record, so every reader of the done map gets the same answer
  with no rule of its own.
  - *`fail` record, row closed since:* the row wins. `ledgerw.release_closed`
    drops `done/<phase>.fail` and the done-map entry and logs `FAIL-CLOSED
    <phase>`; the failure stays in the row's history. It runs in the ledger
    write that ticks a row (`swarm record <phase> done`), in the watchdog's
    sweep (a tick made by hand, or a record left from before) and at the end of
    `swarm up`'s re-seed, which is why a sentinel left beside a closed row no
    longer brings the failure back. The launcher runs when it frees something.
  - *What counts as closed* (`ledgerw.closed_rows`): the box is ticked and the
    row's short status names no failure, in the ledger **as committed on the
    target branch** (`gitq.committed_text`), with no report about the phase
    still queued (`ledgerw.reported`) and no worker on it. The status is what
    tells the order: every `failed`, `blocked` or `later` report writes its word
    on the row and leaves the box alone, so a ticked row that says `failed` was
    ticked first and failed since (a landed phase run again, or a row recorded
    done while its worker was still on it) and stays a failure. An edit that is
    not committed, or sits on another branch, may still be taken back; a
    retired record cannot. doctor's `phases.failed` names a failed phase whose
    box is ticked all the same, with the command that closes it; one whose row
    is closed and only waits for the supervisor's next sweep
    (`ledgerw.closable`) it lists as on its way out, with nothing to do unless
    the supervisor runs older code.
  - *Landed record (`ok`, `operator`), row still open:* the record wins. The
    work is merged and an open box cannot take that back, so the phase counts
    as done, releases its dependents and is never launched again on its own.
    `swarm status` counts it ("N of them done by the swarm, still open in the
    ledger") and doctor's `phases.open` warns with the names, leaving out a row
    whose tick is on its way (a report about it queued, the phase landing) and
    a `skip`, which never ticks. To settle one: `swarm record <phase> done` if
    the work stands, `swarm retry <phase>` to build it again.

## Integrator and merge-conflict resolver

Under `isolation = "worktree"` a finished phase joins the **merge queue**
(`integ_queue` in `state.json`). The supervisor lands one phase at a time, repo by
repo (components first, umbrella last), each under a per-repo `flock`:

- **Untouched repos** (no commits on the branch, main level with origin) are
  pruned with no network at all.
- **Changed repos:**
  1. Check that the canonical tree has no uncommitted tracked edits.
  2. Merge `origin/main` in.
  3. `merge --no-ff swarm/<phase>`.
  4. Push. A non-fast-forward rejection is reconciled by merging origin again and
     retrying, up to 5 rounds.
  5. Remove the worktree and branch.

Before it opens a session, a conflicted merge is offered to the mechanical
resolver (`automerge.py`) through `[git].auto_resolve`, a map from a path glob to
a strategy:

- **`union`:** a real three-way merge that keeps both sides where they differ.
  It suits append-only journals.
- **`keyed:<regex>`:** splits the file into records keyed by the regex's first
  group and merges per key. A record both sides changed is merged line by line,
  then word by word; two additions at the same place keep both. Only words both
  sides rewrote differently are a conflict. It suits a ledger where sessions
  tick rows and append notes to them.

`[git].auto_resolve_check` can name a command per path glob (the project's
ledger gate, say). It runs in that repo on the merged text, and a failure counts
as a decline.

It is all-or-nothing: if any conflicted file has no strategy, or a strategy or
check declines, the tree is left exactly as the failed merge left it.

With `[lanes] enabled`, a changed repo whose main moved since the phase branched
is re-tested first: main is merged into the phase's worktree and `[lanes].check`
runs there (`swarm _lane-check`, detached) against the sibling lanes that landed.
Green lands the tested tree; red or a text conflict holds the queue and opens the
resolver on the worktree.

A check takes minutes and the ledger writer commits to main every few minutes,
so main has often moved again by the time a check is green. The green check
stands when everything main gained since the tested merge is `[lanes] commons`:
main is merged into the worktree once more, without a second check, and the
phase lands (`LANE-CHECK-KEPT` in the supervisor log). If main gained any other
file, the pair is tested again (`LANE-MAIN-MOVED <phase> <repo> main gained N
file(s)`). A conflict at the unchecked merge goes to the resolver on the
worktree, and what the resolver wrote is checked before it lands. To see how
often checks are repeated, count both lines in the supervisor log.

A merge can end four ways:

| outcome | meaning | what happens |
|---|---|---|
| merged | every repo clean, pushed or push owed | the phase is recorded done, its mirror removed, the slot refilled |
| conflict | a repo is mid-merge | the queue is **held**; a **resolver** session opens in window `resolve-<phase>`, in that repo |
| dirty | a canonical repo has uncommitted tracked edits | the queue is held; you commit or stash, then `swarm resolved <phase>` |
| push failed | merged locally, the push was refused or the remote was unreachable | not a hold: the repo **owes a push** (below) |

The **resolver** is a Claude session (prompt: `prompts/resolver.md`, model
`[swarm].resolver_model`, Sonnet by default) that:

- resolves every conflict marker so that both sides' intent survives;
- runs the project's `auto_resolve_check` for a file it resolved, and builds only
  when a conflicted file is code;
- commits the merge and runs `swarm resolved <phase>`;
- if it cannot resolve correctly, tells you and stops, leaving the merge in
  progress;
- never pushes and never launches.

Its brief is written to `<state>/resolver/<phase>.brief.md` and the session is
typed one short line pointing at it. A session that does not take that line (or
never becomes ready) is closed and one more is opened. If that one fails too, no
resolver is on the hold: you are told at once (`RESOLVER-SPAWN-FAIL` in the log),
and the Overseer's next look treats it as a hold nobody is working on.

`swarm resolved` re-checks the repo (no merge in progress, clean tree, on main)
before releasing the queue. A premature call keeps the hold and pings again.

The merge happens in your checkout but never switches its branch. A checkout on
another branch holds the queue, with a message saying so, until you switch it
back and run `swarm resolved`.

A phase that finishes `fail` is rolled back in every repo: worktrees and branches
are removed, with no merge.

**Nothing is destroyed.** Whenever a worktree or branch holding work that is not
on main is removed (a `fail`, a reaped worker, `swarm retry`, gc of an orphan
mirror), its uncommitted edits are first committed onto the branch and the tip is
kept at `refs/swarm-attic/<phase>/<utc-stamp>`, logged as `ATTIC`. If that fails,
nothing is removed. gc drops attic refs older than `[gc].attic_days` (30).

A phase that finishes `later` with a date is the exception to the attic: its work
is wanted again. Its tip (uncommitted edits committed first) is kept at
`refs/swarm-later/<phase>` in each repo it changed, logged as `LATER-KEPT`, and
gc never prunes it; the backup pass copies it to origin as `swarm/<phase>`. When
the row is relaunched, each such repo's new
`swarm/<phase>` branch is made from that day's main and the kept work is merged
into it (`LATER-RESTORED ... onto <main>`), so the worker starts with what it
committed and everything that landed since. If the two no longer merge cleanly,
the branch is put back where the work left it (`... as it was`), as for a resumed
interrupted attempt, and the landing meets the conflict the usual way. The ref is
removed once the work is on the branch. `swarm up` keeps the work of a `later`
whose worker reported while no supervisor ran, and moves kept work to the attic
when its row has been closed some other way. A `blocked` or dateless finish goes
to the attic as before: nothing says when, or whether, that work is wanted.

[The integration flow diagram](../README.md#integrator-and-merge-conflict-resolver) is in the README.

**Owed pushes** (`pushowed.py`): later workers branch from local main, so a push
that failed does not hold anything back. The repo is recorded in `push_owed`, and
you are pinged once if it still owes a push after `[telegram].push_owed_grace_s`
(1 h), and once more when it clears (immediately and at clearing under
`[telegram].pings = "all"`). The push is
retried after every integration and on the watchdog tick, and it clears as soon
as origin has local main, whoever pushed it. `swarm status` and `swarm doctor`
show the standing debt.

`swarm integrate <phase>` runs the same integration by hand, outside the queue.

## Processes: everything dies with its session; `swarm keep` is the exception

Shells and helper processes must not be left lying around: when a worker is
done, all of its processes go too. So a session's end ends every process it
started.

**The marker.** Every session is spawned with `SWARM_STATE_DIR` and its own
`SWARM_SESSION_ID=<kind>:<id>`: `worker:<phase>` (a worker also carries
`SWARM_PHASE`), `operator:<job>`, `overseer:<pass>`, `resolver:<phase>`.
Everything the session starts inherits them, so a child that detached itself
(`setsid`, `nohup`, `&`, reparented to init) is still found by its environment.

**When a session ends:**

| session | ends at | what the supervisor does |
|---|---|---|
| worker | `swarm done` (any status), or the watchdog finding its pane dead | returns the slot's pane to `sleep` before the slot can be refilled, then reaps `worker:<phase>` |
| operator | `operator-done`, a lease that expired, the run stopping | respawns the operator pane to idle, then reaps `operator:<job>` |
| Overseer | `overseer-done`, or its timeout | clears the master pane, then reaps `overseer:<pass>` |
| resolver | `swarm resolved` closing its window | kills the window, then reaps `resolver:<phase>` |
| any of the first three, parked | its usual report, or the watchdog finding it gone from its `wait:` window | kills the window, then reaps the session |

Reaping is `swarm down`'s code narrowed to the session's markers within this run:
SIGHUP, then SIGTERM, then SIGKILL to what outlived each, process groups
included. It runs off the supervisor's loop, 2 s after the session's end (so its
own `swarm done` can print), and the supervisor waits for pending reaps before it
exits. It never signals itself, its ancestors or a tmux server. Each reap is
logged as `REAP <kind>:<id> ended=N`.
The swarm's own helpers that `swarm done` starts detached (the grace poke, the
recap, the operator triage) are spawned without the session's markers, so they
finish their job after the worker is gone.

**`swarm down`** still ends everything carrying the run's `SWARM_STATE_DIR`,
orphaned sessions and whatever detached from them included. It tears down only
a tmux session this swarm created (marked with its state dir; a session made
before that marker counts when state records its windows), and signals the
recorded supervisor pid only while that pid is still this project's
`swarm _supervise`. Every tmux call has a 15 s timeout, so a hung tmux server
cannot freeze the supervisor.

**`swarm keep`** is the one sanctioned way to leave something running, and
sessions are told to use it only when something must outlive them, such as a page
the owner needs to open:

    swarm keep --name look-mockups --why "serves the look mockups for the owner's layout picks" \
        --cwd "$SWARM_PROJECT" -- python3 -m http.server 8790 --bind 0.0.0.0 --directory tasks/mockups/look

- It starts the command fully detached (its own session), with `SWARM_STATE_DIR`,
  `SWARM_SESSION_ID`, `SWARM_SESSION`, `SWARM_PHASE` and a session `TMPDIR`
  removed from its environment, so no reaper matches it. The kept pid and its
  start time are also excluded from every sweep, as a belt.
- `--why` is required: one plain line (at most 120 characters) a non-developer
  can read. A name is unique: a live one is refused, a dead one replaced.
- It records `<state>/keep/<name>.json` (pid, start time, argv, cwd, who started
  it, why, log) and logs output to `<state>/keep/<name>.log`. It warns when its
  cwd is inside a mirror, which goes when the session's work merges.
- `swarm keep --list [--json]` lists every kept process, alive or dead;
  `swarm keep --stop <name>` sends its group SIGTERM, then SIGKILL, and forgets it.
- `swarm status`, `swarm doctor` (`keep`, a WARN for one alive past 7 days, never
  a FAIL) and the dashboard list them, with their why and how to stop them.
- A session that keeps something says so in its recap or outcome: the name, what
  it serves and `swarm keep --stop <name>`. For the operator that outcome needs
  the owner, so it gets `--attention`.

## Worktree isolation and mirrors

With `isolation = "none"` (the default) every worker commits in the project itself,
and the ledger's dependencies are what keep phases apart.

With `isolation = "worktree"` each phase gets a **full mirror of the whole
workspace** under `<state>/wt/<phase>`. That is a worktree of the umbrella repo on
branch `swarm/<phase>`, with a worktree of every component repo nested inside it at
its real path, on the same branch. Component worktrees are created up to 6 at a
time, one nesting level at a time. `[git].repos` globs pick the component repos
(default: every git repo directly under the project root).

- The worker's cwd looks exactly like the project, and nothing it does touches the
  canonical repos or another phase's mirror.
- Two phases may build in the same repo at the same time.
- A mirror starts from local main, unless `origin/main` strictly fast-forwards it,
  so unpushed commits of yours are never dropped.
- A mirror is all-or-nothing: if any repo fails to check out, the mirror is
  discarded, unless an earlier attempt left work in it, which is kept.
- A mirror an earlier attempt left holding work is resumed as it is: the worker
  starts on the same branch, with those commits.
- With `[build].cache`, a Rust worktree's `target/` is a symlink to one shared
  per-repo cache, so only changed crates recompile. This happens only where the
  repo gitignores `target`.

Operator jobs (`op-<job>`) and Overseer passes (`ovs-<id>`) get mirrors the
same way.

Unmerged mirrors are pushed to origin as backups (`[backup]`): the branch, a
snapshot of uncommitted edits, and kept attic refs. gc deletes an attic backup
when it drops the local ref. The umbrella's snapshot leaves the component repos
out, whether or not the umbrella gitignores them. Each pass records its outcome
in `<state>/backup.json`; `swarm doctor`'s `backup` check reports a snapshot
that failed, so unsaved work cannot hide behind the refs that did push.

On `swarm up`, leftover `swarm/*` branches are reconciled from the durable
sentinels, never from branch shape:

- A phase with an `ok` or `operator` sentinel has its integration completed.
- A branch with no sentinel was interrupted mid-build. Its uncommitted edits are
  saved as a commit, the branch and worktree are kept, and the phase's next
  launch resumes on them. One with nothing in it is discarded.
- A phase whose integration is still held is **not** recorded done. The run
  starts visibly held instead.

## Build gate (`swarm build`)

N workers in N worktrees means N independent builds. `swarm build <cmd…>` lets
at most `[build].max_concurrent` heavy builds run at once, swarm-wide; the rest
wait their turn. For `cargo` it also sets `CARGO_BUILD_JOBS` to `[build].jobs`.

**Slots and seats.** A build holds two `flock`s, both on descriptors it
inherits, so its whole process tree holds them: however the build ends (exit,
crash, SIGKILL, a tool timeout), they free when its last process is gone. There
is no daemon and no counter to leak.

- A **seat**, `<state>/buildsem/seat<K>`, held exclusively: one per build alive.
  There are `max_concurrent + idle_yield_max` of them, so the kernel caps the
  builds the gate can have alive at once. The seat file holds a small record of
  its build (id, phase, pid, command, start), which is what `--status` and the
  waiting line show.
- A **slot**, `<state>/buildsem/slot<N>` (`N < max_concurrent`), held *shared*.
  Whatever needs a slot to itself takes it exclusively and so waits for every
  build on it: gc, and a `swarm build` from before seats existed (which
  therefore never starts on a slot that has a build on it). The slot file
  carries a copy of the record of the last build that started on it.

A build may start when fewer than `max_concurrent` builds *count* (alive and not
set aside as idle, see below), a seat is free, and some slot has no counted
build on it.

**The queue.** A heavy command takes a ticket, `buildsem/queue/<seq>-<id>.json`,
numbered under `queue.lock` and locked by its waiter for as long as it waits.
A ticket whose lock can be taken belongs to a dead waiter and is deleted, so a
killed waiter never blocks anyone. Only the waiter whose turn it is tries for a
free slot (under `queue.lock`), so arrivals are served in order; with several
slots, the next free one goes to the next in line. A waiter that stops polling
(a stopped process) is passed over until it polls again.

**Short builds first, boundedly.** A command whose recent runs took at most
`[build].short_s` (the median of its last runs in the same place, from the log
below) may start ahead of older waiters predicted to be long. Each long waiter
counts the times it is passed (`queue.json`), and once it has been passed
`[build].overtake` times nothing more may go ahead of it. So a waiter starts
after at most the waiters older than it plus `overtake` short ones: nobody
starves, and a 30-second targeted test does not sit behind a 15-minute browser
suite. Unknown commands are never "short". `overtake = 0` is plain FIFO.

**Idle yield.** A command can hold a slot and do nothing: a script waiting out
a 20-minute timeout, a test runner waiting on a server that never comes up.
Such a holder is never stopped or signalled; it ends when it ends. Instead it is
*set aside*: once its whole process tree has been quiet for
`[build].idle_yield_s` (default 150) it stops counting against
`max_concurrent`, and the next waiter starts beside it, on the same slot.

- *Quiet* means, over one measurement (every 5 s), under 5% of one core and
  under 256 KiB/s of disk IO, summed over the build's process tree: its root and
  every descendant, plus any process that still has its seat file open (a child
  that detached); and no process of the tree runnable or waiting on disk at that
  instant, so a build starved on a stalling box does not pass for idle. Any
  other measurement restarts the clock, so the pauses a working build has
  (between compile and test, behind a lock, while linking) never add up to a
  window.
- *Waking up.* A set-aside holder that uses half a core (or 4 MiB/s of disk)
  over a measurement counts again from then on: no new build starts while the
  builds that count fill the slots. A build that already started beside it keeps
  running, so for that time more than `max_concurrent` builds work at once.
  The clock restarts, so it cannot be set aside again before another full
  window; between the two thresholds nothing changes, so a holder hovering at
  the edge does not flip back and forth.
- *Work that happens elsewhere is never idle.* `docker build`,
  `docker buildx bake` and `docker compose build` do their work in the docker
  daemon, and the client waits at 0% CPU. A command read as such (through the
  same wrappers and scripts the light/heavy rules read) never yields; the same
  goes for `podman`, `nerdctl`, `buildctl`, `kubectl`, `sccache`, `bazel`,
  `buck2`, `gradle`, `nix` and the like. Where the command could not be read (a
  script with `$(…)`), the holder is not quiet while one of those programs is
  alive in its tree. A tree with a process whose IO cannot be read (another
  user's, after `sudo`) is never quiet either; a process that is exiting
  refuses its IO counters too, and is told apart (it has no memory left), so a
  build that ends while set aside is not logged as counting again first.
  **Not covered:** containers a
  build started and then only waits for with `sleep`. Nothing of theirs is in
  the build's tree, so such a holder yields, unless it was started with
  `--hold`.
- *The opt-out: `swarm build --hold <cmd>`.* For a command that needs the
  machine to itself while it looks idle (a measurement script that sleeps while
  a container stack, or anything else outside its process tree, is measured).
  It takes a slot even if the command is light (`sleep 600` alone would skip
  the gate), keeps it for as long as it runs, and is never set aside, so no
  build starts beside it. Its `start` event says `"hold": true` and `--status`
  shows `keeps its slot while idle (--hold)`. The queue waits behind it, as it
  did before idle yield.
- *The command is told.* The `swarm build` that runs a command prints, on that
  command's stderr, the moment its slot is released (`this command was idle for
  2m30s, so its build slot was released at 14:02:11 and other builds may run
  beside it from now on (nothing was stopped). If it needs the machine to
  itself (a measurement), rerun it with …--hold…`) and again as its last line
  when the command ends, with how long builds may have run beside it. So
  whoever reads the output afterwards knows the run was not alone. (The
  landing's lane check, when it takes a slot, holds it inside the swarm's own
  process and prints nothing: its log is not a worker's.)
- *The caps.* At most `[build].idle_yield_max` (default 2) holders are set aside
  at once; a further idle holder keeps counting and the queue waits, as it does
  with `idle_yield_s = 0`. The seats bound the builds alive at
  `max_concurrent + idle_yield_max` whatever happens.
- *Who measures.* The waiters, from `/proc`, under `queue.lock`, keeping the
  running figures in `buildsem/idle.json`. It needs no supervisor and no daemon:
  when nobody waits, nobody needs the answer. The counters compared are the
  kernel's cumulative ones, so a waiter that dies loses nothing: the next one
  carries on from its last sample, and a build's start is itself a sample (zero
  used). A stretch nobody measured counts as quiet only if next to nothing was
  used in all of it, and one ordinary measurement must agree before anything is
  done about it (after a suspend the counters say nothing happened). A
  set-aside holder's own `swarm build` keeps measuring while nobody waits, so
  its wake-up is logged when it happens. A build starts beside a set-aside
  holder only on a look at most a second old that itself spans a second or two,
  which costs that start about a second.
- *Crashes.* The mark that a holder is set aside names its build id and is only
  honoured while that build's seat lock is held and the measurement behind it is
  fresh. A killed holder's seat is free and its mark is dropped; a killed waiter
  leaves a ticket that is pruned; a killed measurer leaves figures the next one
  checks again before using them.

**Pairing rules.** `max_concurrent = 2` on its own lets any two heavy builds
run side by side. Where the disk is what builds strain (two builds can be fine,
two builds in one tree or a build beside an image build are not), set
`[build].pair = "distinct-repo"` and the gate also checks *which* builds are
alive before it starts one. `"any"`, the default, checks nothing.

- *Never two builds in one repository.* A build starts only if it shares no
  repo with any build alive. Its repo is the one its working directory is in,
  under the name the swarm already uses for it: its path inside the project
  (`lib`; `.` for the project's own repo), which is its lane and the directory
  of its shared build cache (`[build].cache`). A phase's mirror has the
  project's layout, so `<state>/wt/<phase>/lib` is `lib` for every phase; any
  other checkout is traced through its git common directory, so every worktree
  of a repo is that repo. A repo outside the project is named by its lane if
  `[lanes].external` declares it, else by its path. The
  repos the command itself names count too (a `cd` target, a
  `--manifest-path`, a `-C` directory, a script's own place: what pre-flight
  checks): `cargo test --manifest-path lib/Cargo.toml` from the project root
  builds in `.` and in `lib`. What a script does once it runs is not seen.
- *Some builds run alone*, both ways: such a build waits until no other build
  is alive, and nothing starts while it runs. They are:
  - a command `[build].alone` names. A pattern is a command prefix whose words
    are globs, like `heavy` and `light`, matched against every simple command
    in what is run (through the same wrappers and scripts), and it counts where
    that command is heavy. The default is the container clients (`docker`,
    `docker-compose`, `docker-buildx`, `podman`, `podman-compose`, `buildah`,
    `nerdctl`, `buildctl`), so `docker build`, `docker buildx bake`,
    `docker compose build`/`up`, `docker run` run alone and `docker ps`,
    `docker compose down` or `bake --print` do not. An image build does its
    work in a daemon, outside the build's process tree and straight onto the
    disk;
  - a `swarm build --hold`;
  - a build whose repo cannot be told (its working directory is in no git
    checkout).
- *A script that cannot be read.* Three cases, from most to least known:
  1. a script the classifier reads to the end is judged command by command, as
     above;
  2. a script that is there but is not read to the end (`$(…)`, a heredoc, a
     function, `case`, `eval`, python that starts processes) runs alone if its
     text names one of those programs anywhere outside a comment;
  3. what shows no text at all (a binary, `make`, a script that calls another,
     a program taken from a variable) starts as an ordinary build, and is
     watched. Its own `swarm build` looks at the build's process tree every
     2 s, and whoever is about to start beside a running build looks at that
     build's processes first. A process that matches (by its real command line:
     `docker buildx bake …`, not `docker ps`) marks the build alone from then
     on, for the rest of its run: an `alone` event, a line on the command's
     stderr, `buildsem/pair.json`. Nothing new starts beside it, also after
     that process has gone. **Not covered:** a build that was already running
     beside it keeps running; that one overlap is what a late discovery costs.
     A script known to build images belongs in `[build].alone`
     (`alone = ["docker", …, "bash ci/bake.sh"]`).
- *Who counts.* Every build alive on a seat, including one set aside as idle.
  Idle yield frees a slot, not a repo: a set-aside holder may wake up, and then
  it works in its repo again. So beside an idle holder only a build in another
  repo starts, and a build that runs alone waits for it to end. When a
  set-aside holder does wake, the builds working may exceed `max_concurrent`
  (see *Idle yield*), but they are still in different repos and none of them
  runs alone. Only a build whose own `swarm build` saw its command end, with a
  process it left behind still on the seat, no longer counts for these rules.
  A build started by a `swarm build` from before these rules says nothing of
  its repo, so nothing starts beside it. A process left by a build whose
  `swarm build` was killed keeps the repo until it is gone (`--status` names
  it).
- *The queue.* A waiter the rules hold back does not hold up the ones they
  allow: the turn goes to the oldest waiter that may start (or a short one
  behind it). That passing comes out of the same budget as *short builds
  first*: each waiter is passed at most `[build].overtake` times in all,
  whatever the reason, and once it has been, nothing starts before it. A
  waiter that runs alone is never passed once it is the oldest in line, so a
  stream of builds that could each pair with the one running does not starve
  it: they wait, the running build ends, it runs. So a waiter starts after at
  most the waiters older than it plus `overtake` younger ones; with
  `overtake = 0` the queue is first come, first served, and the second slot is
  used only when the two oldest waiters may pair. Each time a rule-held waiter
  is passed, a `passed` event says by whom and why.
- *What it says.* The waiting line and `--status` give the reason in the same
  words: `same repo as slot 0 (lib)`,
  ``waits to run alone (`docker build` is in [build].alone)``,
  `slot 1 runs alone (started with --hold)`,
  ``slot 0 runs alone (`docker buildx bake` seen running in it)``. `--status` shows each
  holder's repo and whether it runs alone; `start` and `queued` events carry
  `repo`, `alone` and `why`.
- *Crashes.* The rules are read from the seat records of the builds whose seat
  lock is held, under `queue.lock`, each time a waiter looks for its turn:
  a killed holder's seat is free, so it holds no repo; a killed waiter's ticket
  is pruned, so it holds nobody back.

**Light commands skip the gate.** The command is read the way the shell would
run it. Wrappers are looked through (`env`, `timeout`, `nice`, `flock FILE cmd`,
`xargs`, `uv run`, `bunx`…), `sh -c` scripts and `#!` shell scripts are split into
their commands, `bun run <script>` is read from `package.json`, and python is
read for anything that starts processes. It is light only if every command in
it is known to do no compile, test, bundle or image work: `git`, `ls`, `cargo
update`/`metadata`/`fmt`/`tree`, `docker buildx bake --print`, a python script
that starts no processes. Anything it cannot read (an unknown program, `$(…)`,
`eval`, a heredoc, a shell function) stays heavy. `[build].heavy`/`[build].light`
add patterns over the rules (a command prefix whose words are globs).

**Pre-flight.** Before a heavy command queues, what it needs must exist: its
program, a literal `cd` target, an explicit `-f`/`--file`, `--manifest-path`
or `-C` directory, a `Cargo.toml` (here or above) for cargo, a `Makefile`, a
bake or compose file when none is given, a `bun run` script. Otherwise it is
refused at once (exit 127 for a missing program, 2 otherwise) instead of after
the queue. Only checks nothing earlier in the command could have made true (a
`mkdir` or a build step ends them).

**What it says** (stderr only; worker prompts are unchanged):

- on joining, and every 45 s while queued: its place, who holds each slot
  (phase, command, how long, how long it usually takes) and an estimated start;
  on joining, also that queue time does not count toward `--timeout` and how to
  batch steps;
- `queued 3m12s, starting on slot 0: cargo nextest run`, and when an idle holder
  made the room:
  ``… — beside P-7 `bash wait.sh`, idle 2m30s: its slot was yielded``;
- `ran 2m03s, exit 0 (queued 3m12s)`.

The waiting line also says when a holder has been quiet for a while (`idle
1m40s (yields its slot at 2m30s)`), which holder never yields and why, and
lists the holders already set aside. A command that was itself set aside is
told so twice, when it happens and as its last line (see *Idle yield*).

**Batching and timeouts.** `swarm build -- sh -c 'a && b'` or
`swarm build --script FILE` (run with `bash -e -o pipefail`) runs several steps
in one turn. `--timeout D` counts from the start, not the queue: at `D` the
build's process tree gets SIGTERM, then SIGKILL after 10 s, and the exit code is
124. Signals `swarm build` receives are passed on to the build's process tree;
if `swarm build` itself is SIGKILLed, the build gets SIGTERM. A `swarm build`
inside a build that already holds a slot runs straight through.

**The event log**, `buildsem/events.jsonl`, is append-only, one JSON object per
line, each written with one `O_APPEND` write, and rotated to `events.jsonl.1` at
20 MB. Every line has exactly these keys:

```json
{"ts": 1790000000.123, "event": "start", "id": "3f2a9c01be44", "phase": "P-1",
 "pid": 4242, "slot": 0, "cls": "heavy", "argv": "cargo nextest run",
 "cwd": "/…/wt/P-1/lib", "wait_s": 12.5, "run_s": null, "exit": null,
 "idle_s": null, "hold": false, "repo": "lib", "alone": false, "why": null,
 "by": null}
```

| event | when | `pid` | notes |
|---|---|---|---|
| `queued` | a heavy command joined the queue | the waiting `swarm build` | `alone`, and `why` when it is |
| `start` | it got slot `slot` | the build process | `wait_s` = time queued; `hold` = started with `--hold` (null on every other event); `alone` = the pairing rules made it run with no build beside it (`why` says why; always false under `pair = "any"`) |
| `bypass` | a light command (or any, gate off) started unqueued | the command's process | `slot` null |
| `end` | it finished | as in its `start`/`bypass` | `run_s`; `exit` (signal N → 128+N, `--timeout` → 124) |
| `preflight_fail` | refused before queueing; nothing ran | `swarm build` | |
| `yield` | a running build was set aside as idle; it keeps running | as in its `start` | `idle_s` = how long its tree was quiet; `run_s` = how long it had run |
| `unyield` | a set-aside build is working again and counts again | as in its `start` | `idle_s` = how long it was set aside; `why` = what the measurement saw (`it is using CPU again`) |
| `passed` | a pairing rule held this waiter back and a younger one started ahead of it | the waiting `swarm build` | `why` = the rule (`same repo as slot 0 (lib)`); `by` = the `id` of the build that started |
| `alone` | a running build was found to hold a command that runs alone; nothing starts beside it from now on | as in its `start` | `why` = what was seen; `run_s` = how long it had run |
| `left` | a gc's wait for the gate ran out and it left the queue; it held nothing | the process gc runs in | `wait_s` = how long it waited; `why` = what was still alive |

`cls` is `heavy` for a queued build, `light` for a command that skipped the
gate, and `gc` for gc's own turn at the gate (see *gc and the gate* below):
`queued` when it joins the queue, `start` when it holds every slot (`slot` is
null, `wait_s` how long it queued), `end` when it lets go (`run_s` is how long
no build could run; `exit` 0, or 1 if the sweep failed), or `left` instead of a
`start`. `pid` is the process gc runs in (the supervisor, for the automatic
one), `alone` is true. A gc is not a build: the run-time history and the
resource sampler skip these lines.

`phase` is `$SWARM_PHASE` (null outside a worker); `argv` is at most 300
characters. `repo` is the repository a queued build works in, by the swarm's
name for it (see *Pairing rules*; logged under `pair = "any"` too), on every
event of that build; it is null when the working directory is in no git
checkout, and for a light command. `yield` and `unyield` carry the `id`, `pid` and `slot` of the build
they are about and are written by whoever measured it; a build that ends while
set aside gets no `unyield` (its `end` closes the stretch), and a `yield` after
a build's `end` is a process that build left behind, set aside in its turn. A
`queued` with no `start` gave up while waiting. A `start` whose
`end` never came and whose `pid` is gone died unrecorded: the gate writes a
synthetic `end` with `exit` null as soon as it notices (when a waiter reports,
or when the slot is next taken).

**gc and the gate.** gc deletes build output, so it holds every build slot
exclusively while it works, and the kernel grants that only while no build is
alive on any of them: set aside as idle or not, started by an older `swarm
build` or not, a process a build left behind included. It gets there through
the queue, as a waiter that runs alone, and never by taking slots one at a
time. (It used to: with two slots it took slot 0, waited its ten minutes for a
long build on slot 1, gave up, and did the same at the next interval. A third
of one slot's time went to a gc that never ran.)

- **It holds nothing while it waits.** Its ticket is in the queue for
  `[gc].wait_s` (10 minutes). When no build is alive and no older waiter is
  ahead of it, it takes every slot in one step under the queue's lock, or none.
- **Builds pass it** for as long as any build is alive, so a free slot is
  never kept for it. The moment the gate is empty it goes first: no build
  queued after it starts ahead of it. With one slot that is between any two
  builds.
- **For the last `[gc].hold_s`** (2 minutes) **of its wait it is passed no
  more:** no build queued after it starts, the builds that are running end,
  and gc runs. That is what lets it run with two slots under constant load,
  where the gate is otherwise never empty and the disk fills. It holds builds
  back only for builds at work: while a holder set aside as idle, a process a
  build left behind, or a slot held from outside the queue is in the way,
  nobody knows when the gate will be empty, and builds go on passing.
- **When its wait is over** it leaves the queue (`left`), the supervisor logs
  `GC-AUTO-SKIP … busy` with what was still alive, and tries again ten
  minutes later.
- **What it costs a build:** at most `hold_s` of waiting behind a gc that is
  passed no more, plus the sweep itself, during which nothing starts. A sweep
  takes seconds (2 to 15 s on a run that frees up to 13 GB at a time); how long
  each one held the gate is the `run_s` of its `end` and the recent list of
  `--status`. `hold_s = 0` makes the first part zero and gc a pure opportunist.
- **Killed** (the supervisor died): a waiting gc's ticket is dropped by the
  next waiter to look, like any dead waiter's; one that held the gate frees
  every slot with its last process, and the gate writes the `end` it never
  wrote (`exit` null).

`swarm gc --yes` takes the gate the same way (it waits 5 minutes in all). So
does nothing else: the landing's lane check is an ordinary build with a seat
and one shared slot, and it too holds nothing while it queues.

**`swarm build --status [--json]`** shows the holders, the queue in the order it
would start with ETAs, and the last builds with their wait and run times;
`swarm status` and `swarm doctor` carry a one-line summary. Holders that were
set aside are listed apart (`yielded: P-7 … yielded after 2m30s idle, still
running 12m`; `yielded` in `--json`, and every build alive under `builds`), a
build that ended while one of its processes still holds the slot is marked so,
and a recent build that was set aside says for how long. A gc is shown as what
it is: `gc: waiting 2m10s to run alone; builds pass it for another 5m50s, then
none starts until it has run` while it queues, `slot N: gc running 4s` on every
slot while it sweeps, and in the recent list with how long it queued and ran
(or `gc waited 10m00s for the gate to empty and left; it held nothing`);
`--json` carries it under `gc` (`state` `waiting` or `running`, and for a
waiting one `firm_in_s`, `leaves_in_s` and `holding`), and a waiter it holds
back says `held back: gc runs first`. A slot that is busy with no current
record is an older `swarm build` or a process such a build left behind;
`--status` names the pids holding it open. Under the pairing rules
each holder's line ends with its repo and, if so, `runs alone (why)`, a waiter
the rules hold back says `held back: …`, and `--json` carries `pair`, `alone`
(the patterns), `repo`/`alone` on each holder and `repo`/`alone`/`blocked` on
each waiter.

Workers are told to wrap their gates in it (`swarm build cargo nextest run`), and
to give those commands a generous timeout, since they may queue. The landing's
lane check queues like any other build, and like any other build it does not queue when
its command is light: by the built-in rules, or because the project names it in
`[build].light`. That is for a check that compiles nothing (a lint-only gate
that takes half a minute should not wait ten behind a compile); its log starts
with `# light command (...): not queued for a build slot`, and the build event
log records it as a `bypass`.

**Sizing.** `[build].jobs` and `max_concurrent` describe the host the swarm runs
on. Derive them from that machine's cores and memory (one build's peak memory
times `max_concurrent` must fit with room to spare; `jobs` times
`max_concurrent` should not exceed the cores), never copy them from another
machine's config.

## Resource tracking (`swarm resources`)

Whether `[build].max_concurrent`, `[build].jobs` or `[swarm].max_workers` can go
up is a question about what a build and a worker actually take on this host.
The supervisor answers it with a sampler thread of its own
(`resources/sampler.py`), started with the loop and stopped with it. It only
reads `/proc` and the state dir, and writes under `<state>/meters/`.

- **Cadence.** Every second it makes a cheap check: has the gate's event log
  grown, does anyone hold a build slot? While a build runs it takes a full sample
  every second; otherwise every 15 s. A peak sampled every few seconds reads low
  (a 2 s sampler missed a quarter of a 4 s ramp), so the fast rate is kept while
  building.
- **The host** (`resources/host.py`): CPU busy % from `/proc/stat`, load,
  pressure from `/proc/pressure/{cpu,memory,io}` (the `total` stall counters
  differenced over the sample, not the smoothed `avg10`), `MemAvailable`, anon
  memory and page cache reported apart (`memory.peak` and `docker stats` count
  cache, which overstates need several times), swap, and disk read/write MB/s
  from `/proc/diskstats` (whole disks only).
- **Disk** (`resources/disk.py`): free space every minute. Under WSL `df /`
  shows the virtual disk's ceiling, not real room, so headroom is the vhdx's slack
  (file size minus bytes used inside the distro) plus the free space of the
  Windows drive holding it (`[resources].vhdx`, or found). Every ten minutes, on a
  thread of its own, `du` at nice 19 and idle IO class (60 s budget each) sizes
  the state dir, the worktrees and each shared build cache (`cache/target/*`,
  resolved), and the growth per hour between two measurements.
- **Builds** (`resources/builds.py`): the build holding each slot comes from the
  gate's event log, `buildsem/events.jsonl` (`queued`, `start` with the build's
  pid, `end` with its run time and exit code, `yield`/`unyield` when the gate
  sets an idle build aside or counts it again; a `start` whose process has been
  gone for five seconds counts as ended, since a SIGKILLed build writes no
  `end`, while a build that just exited is waited for, because the gate writes
  its `end`, with the exit code, a moment after the process is gone). Without that log,
  the slot file's `flock` holder, read from `/proc/locks`, is the build. Each
  sample sums over the build's process tree: CPU seconds (live processes'
  `utime+stime+cutime+cstime`, which counts reaped compiler processes once),
  anon and total RSS, and storage IO from `/proc/<pid>/io`.
- **Sessions** (`resources/ptree.py`): each process is labelled once from its
  environment (`SWARM_STATE_DIR` of this run, `SWARM_SESSION_ID`), children
  inherit it. A worker's figures leave its builds out (they are the build's) and
  count its processes' own CPU, so a reaped build is never charged to the shell
  that ran it. The supervisor and dashboards are "swarm itself". The owner
  console carries `SWARM_OWNER_CONSOLE` and no session id: it is shown apart
  ("owner console"), never as a worker or as the swarm's overhead, and the
  capacity maths leaves it with everything else on the host.
- **Storage** (`resources/store.py`): `meters/resources.jsonl` holds every
  sample for a day; hourly compaction folds older ones into
  `meters/resources-1m.jsonl`, one row a minute with `[min, avg, max]` per figure
  and per build/worker average cores and peak anon, kept 30 days. Each finished
  heavy build gets a row in `meters/builds.jsonl`: id, phase, argv, cwd, wait,
  run time, exit, CPU seconds, average and peak cores, peak anon and total RSS of
  the tree, the lowest `MemAvailable` and the highest pressure during it, IO,
  and `yielded_s`: how long the gate had it set aside as idle.
  Byte caps (48, 32 and 8 MiB) win over the age limits. `<state>/resources-now.json`
  holds the latest sample, what is running and the sampler's own cost.
- **Idle holders.** A heavy build that holds a slot for `[resources].idle_s`
  (default 600) with its whole tree under 1% of a core shows in `swarm status`,
  as a `swarm doctor` WARN, in the dashboard's resources box, and pings once
  (again hourly while it stays idle). The report says what the gate did about
  it: its slot was released (the gate set it aside, so builds start beside it),
  or it was kept and why (a command that never yields). This is a report only;
  setting a holder aside is the gate's own doing, by its own measurement, long
  before this warning. The gate's own holder record (`buildsem/seatK`, or
  `slotN` for a gate from before seats) confirms it first: a record saying the
  build ended, or naming another build, means the sampler missed an `end` and
  nothing is reported; a matching one supplies the phase and command. Nothing
  is killed.
- **Cost.** The thread's CPU time (`time.thread_time`) and `du`'s (from
  `wait4`) are published in the snapshot, with the bytes written per day. On a
  24-core host with a few hundred processes a full sample costs about 5 ms, so
  1 s sampling is about half a percent of one core while building.
- **`swarm resources`** prints now (host, each build, each session; a build the
  gate has set aside reads `YIELDED 12m`), the last day as sparklines, the
  finished builds with the worst peaks (with a `yielded` column), the builds
  that sat idle longest and how long in all, and a capacity
  section: p95 per heavy build and per worker, and what 2 concurrent builds, 8
  workers or doubled jobs would have needed against this host's RAM (85% of it,
  the rest left to page cache and the kernel) and cores, with the arithmetic shown
  and the data called thin under 5 builds or an hour of worker samples. `--json`
  for scripts.

## Stop hook, recaps, notes and the report

**Stop hook** (`scripts/stop-hook.py`, opt-in): register it as a `Stop` hook in
`[worker].worker_settings` (see [config.md](config.md)). At every turn
boundary it appends the worker's final message to `<state>/turns/<phase>.jsonl`. It
costs no API call, never fails the turn, and prints nothing. It feeds the recaps,
the board's "last turn", and the Overseer's digest.

**Recaps** (`swarm recap <phase> [--force]`): a summary of one or two sentences of
what a phase did, stored in `<state>/recaps/<phase>.json`. They are made at two
moments only:

- `swarm done` spawns one in the background (reusing a good existing recap);
- you ask for one with `swarm recap`.

They are never made on a timer. A short completion note is used as is. Otherwise
`claude -p --model haiku` summarizes the last turns.

**Notes** (`swarm note <phase> [decision|assumption|risk] "<text>"`): the silent
middle register between finishing quietly and stopping to ask. A note pings
nobody, parks nothing and costs no slot. Your own answers, relayed by
`swarm resumed`, are stored as `owner_decision` notes. All of them live in
`<state>/notes/<phase>.jsonl`.

**Report** (`swarm report [--decisions] [--phase P] [--json]`): one row per phase
with status, finish time, time in slot, time to integrate, and recap, plus
warnings where the records disagree. These include a sentinel that contradicts
`state.json`, a recap overwritten, and a ping that was owed or never delivered.
`--decisions` keeps only phases with notes or substantial recaps, and puts your
decisions first.

## Meters, usage and runs

Each worker's status line is swapped for a small tap (`meters.py`). It records
what Claude Code already hands its status bar:

- context size and session cost, in `<state>/meters/<phase>.json`;
- 5-hour and weekly subscription usage with their reset times, appended to
  `meters/limits.jsonl` whenever a figure moves.

It then runs your own status-line command, so the pane looks unchanged. A project
whose `worker_settings` sets its own `statusLine` keeps it and gets no tap.

A **run** is the span from `swarm up` to `swarm down`. `swarm reset` (or `R` in
the dashboard) closes the open run and starts a new one without restarting
anything, so usage counts from that moment. Runs live in
`<state>/history/runs/<id>/`, and a mid-run change to the worker count or
isolation splits the run's averages.

`swarm usage` shows the open run and the past ones: hours, phases, average
5-hour %/h and weekly %/h, the 5-hour windows spanned, and $/h. The 5-hour and
weekly figures are account-wide, so other Claude sessions on the same account
during a run count too.

Every sample carries the account it was read under: the tap and the endpoint
call tag it with the first 8 hex digits of a SHA-256 of `oauthAccount.accountUuid`
in `~/.claude.json` (never the uuid or the email). Every "now" figure is the
account in use (the login, else the newest tag), a switch starts that account
from its own figure instead of counting as a reset or as usage, and a run that
spans a switch lists each account's points used. Rows from before the tag load
as account unknown and are left out once the account is known.

Usage reaches your phone only when you ask: `/usage` to the bot (see
[Telegram](#telegram-and-asking-the-owner)) answers with (`usage.brief`):

```
Weekly 41%, resets Wed 11:00.
5-hour 23%, resets 16:00.
Read at 14:05, 3 min ago.
The swarm pauses at weekly 60% and 5-hour 90%, and stops at weekly 70%.
```

The last line is the usage caps' state: the hold, when one holds. A sample arrives
only when some session renders its status line, so the answer says how old it is.
`swarm usage` prints the same age on its `sample` line.

### Usage caps

`[usage].rules` (`caps.py`) act on the swarm, never inside a session: workers are
not told. Every `[usage].check_s` (10 minutes) the supervisor reads the tap's
newest figures. When they are older than `[usage].stale_s` (30 minutes), or a
window has reset since, it asks Claude Code's own usage endpoint
(`GET /api/oauth/usage` with the login in `~/.claude/.credentials.json`), at most
once every 30 minutes, and appends the answer to `limits.jsonl` as a sample with
`"src": "api"`. The token is used only while unexpired, never refreshed, written
or logged; a failed call is logged (`USAGE-API`) and the last reading stands.

- **pause:** no new workers while a fresh reading is at or over the limit;
  running workers finish. The hold is its own record (`usage_hold` in
  `state.json`), apart from `swarm pause`: lifting it never undoes your pause.
  It lifts by itself once the window has reset and a fresh reading is under the
  limit. The hold remembers its account: after a `/login` to another account,
  the supervisor checks at once (asking the endpoint if nothing fresh is in
  yet), and a fresh reading of the new account under the limit lifts it. A
  lagging reading of the held account never does. `swarm resume` leaves it in
  place and says so; `swarm resume --override-cap` runs through it until the
  window resets.
- **down:** runs `swarm down`, once per window of each account. The swarm stays
  down.

A stale or missing reading never creates a hold and never lifts one. Each
crossing pings once, and so does a hold lifting. The hold and the reading show on
the TUI, the web board, `swarm status`, `swarm why` and `swarm doctor`
(`usage.caps`). Hold, lift and endpoint events are logged as `USAGE-*`.

## Doctor, why and gc

**`swarm doctor [--json]`** answers "what is wrong right now?". It is read-only,
and exits 1 if any check FAILs. It checks:

- **supervisor:** pid alive, FIFO has a reader, no stray second supervisor;
- **slots:** busy panes run `claude`; a busy slot with no edits or commits
  20 minutes after launch (the lost-prompt signature);
- **run:** watchdog (which dead slots it will free, and what frees the others),
  finished with ready work, free slots beside ready phases (a phase waiting to
  be retried after a failed launch is named, not offered; one given up on is
  named as that), no event for 90 minutes;
- **parked:** every parked session is still there (`parked.sessions`). One
  that is gone is a WARN while the running supervisor's sweep will settle it,
  and a FAIL, with what settles it by hand, under a supervisor that started
  before it could or with the watchdog off;
- **integration:** hold age, owed pushes;
- **owner:** questions waiting on you (`owner.blocking`, a WARN, never a FAIL);
- **ledger:** cycles and unknown dependencies;
- **telegram:** config valid, and whether sends were ever made or dropped;
- **disk:** state-dir size and growth, incremental caches, a full `/tmp`;
- **records:** sentinel and state agreement, forced recaps, failed phases,
  operator jobs waiting over an hour or abandoned, prompt files present, and the
  web board.

**`swarm why <phase> [--tree]`** gives one answer per phase:

- not in the ledger, excluded (quoting your `.swarm.toml` comment), building,
  held, queued to merge, waiting, parked, done or failed;
- blocked, with the root blockers found by walking its unmet dependencies;
- ready, with what is stopping it (paused, or no free slot).

**`swarm gc`** reclaims disk. It is a dry run that prints a plan unless you pass
`--yes`.

- **Always planned:**
  - build caches of repos that no longer exist;
  - `incremental/` directories;
  - superseded cargo units: every unit that is not recent, not built by a live
    phase and not the newest of its kind (nor a dependency of one of those),
    so a shared cache holds about one generation per repo;
  - `cargo sweep` of build output unused for `[gc].keep_days` (needs
    `cargo-sweep`);
  - orphan mirrors in `wt/`;
  - stale session temp dirs;
  - a report of large leftovers in `/tmp`.
- **Opt-in flags:**
  - `--aggressive`: `release/`, `doc/` and more;
  - `--transcripts`: orphan worker transcripts in `~/.claude/projects`;
  - `--branches`: merged `swarm/*` branches;
  - `--canonical`: paths inside the project's own repos.
- **Safety:** it holds every build-gate slot while it deletes, which it gets by
  waiting its turn in the build queue (see *gc and the gate*), refuses while a
  compiler runs in a tree it would touch (unless `--force`), and re-checks every
  path at delete time against a protected list.
- **Automatic runs:** the supervisor runs a conservative gc by itself (`[gc]`) at
  most every 15 minutes, plus once per idle stretch, and never during a build.
  It never keeps a build slot while it waits for another.

## The owner console

**What it is:** your own Claude session for the swarm, in the `console` window
between `dash` and `overseer` (`[console]`, on by default). You talk to it as to
any Claude session: report a problem, ask for a change, add phases or a campaign,
reshape the ledger, check on the workers. Nothing is clamped; it keeps your own
settings and hooks.

- **Its primer** is appended to Claude Code's system prompt
  (`--append-system-prompt`): `prompts/console.md` (what the swarm is, where the
  ledger, history, lessons, state and log are, that rows go in through
  `follow-up`/`reshape`/`record` so the swarm builds them, that workers are
  observed rather than typed into), then every CLI subcommand with its one-line
  help, read off the parser so it cannot drift, then `[console] prompt_file`.
- **The window** runs a keeper (`swarm _console-pane`) that starts `claude` in
  the project directory. When you `/exit`, the keeper stays with one idle line
  and never relaunches by itself. Enter in the pane, `o` in the dashboard or
  `swarm console` starts it again; while it runs they only move you there, so
  there is never a second one. A missing window is recreated after `dash`.
- **One conversation.** The swarm picks its id and keeps it in
  `<state>/console.json`. Each start passes `--resume <id>` once Claude Code has
  its transcript, and `--session-id <id>` before; never `--continue`, which would
  take the newest session in the directory (an Overseer pass, say).
  `swarm console --new` stores a fresh id, and is refused while the console runs.
  Claude Code records the primer once per conversation, so a primer edit reaches
  a resumed console only after it compacts, or with `--new`.
- **Not a worker.** It carries no phase marker and no `SWARM_SESSION_ID`, even if
  the tmux server's environment does, so no `Stop`-hook recap, slot, ETA,
  per-phase usage or session reaper counts it, and its pane has no `@swarm_slot`
  tag for a watchdog to look at. It carries the run's `SWARM_STATE_DIR`, so its
  `swarm` commands find the run and `swarm down` ends it; the next `swarm up`
  resumes the conversation.

## The dashboard (`swarm tui`)

A Textual app in window 0 (`dash`), started by `swarm up` under tmux. It re-reads
the state every 2 s, and probes panes and git every 10 s. The status bar shows:

- live, paused, draining (and what for), finished, or supervisor down;
- busy slots;
- campaign progress: everything done (built, ticked in the ledger or skipped) over
  everything the campaign schedules, the same count as the web board and
  `swarm status`. An excluded row counts once it is done and is out of the count
  until then. The headline says how many done rows the ledger still shows `[ ]`.
  The campaign named is one a worker holds a row of, building it or asking you
  about it, before one that is only ready. A row whose worker waits on your
  answer is counted "waiting on you" in the headline and in `swarm status`,
  never ready or running; a row waiting for its date is not ready either;
- the ETA, from the forecast engine (`swarm_orchestrator.eta`): 500 seeded
  replays of the swarm working through the open ledger with the supervisor's own
  pick (`ledger.ready`, in ledger order), each row's work time drawn from a
  log-normal fitted to this machine's supervisor log and pooled by kind, repo and
  campaign, `after:` dates, a scheduled pause, the usage caps projected at the
  run's burn (the account's own 100% included), follow-up rows at each
  campaign's recent rate, and the share of its capacity the swarm has delivered
  over the last three days (the ledger's ticks on any machine, `git log` cached
  by `HEAD` in `<state>/cache/`). Each campaign ("phase book") gets P50–P85
  (P95 in its detail) for its last row in the shared schedule. Owner-run rows,
  workers waiting on an answer, failed rows and everything behind them are left
  out and listed as "waiting on you". It is computed on a thread of its own,
  remade when the ledger, state, config or caps move and every 5 minutes while
  rows run, and cached in `<state>/cache/eta.json` so the dashboard, the web
  board and `swarm status` share one answer. Until the first replay lands, a
  floor is shown (the longest chain, or the work over the workers). The method
  is described in `docs/research/eta-engine-literature.md`. The chart of phases
  done reads the same git history, so it runs to today;
- the time since the last event, which turns amber after 30 minutes and red after
  2 hours while a slot is busy;
- how many phases wait on you.

It also serves the web board (below), and the status bar ends with its address.

`n` opens the **needs-you** drawer; `o` reopens the owner console (or moves you
to it while it runs). `1`–`9` and `0` switch between the tabs (the
strip at the top lists them; the footer keeps the other keys, and `?` lists all):

1. **home:** the overall ETA with what it was made on and what waits on you,
   then a grid. Left: the phase books (every campaign with rows left, soonest
   finish first; `enter` opens one: its rows, what holds each, and P95), working
   now, a chart of phases done, and a feed of finishes, decisions, answers,
   operator outcomes and Overseer passes. Right:
   - **usage:** the 5-hour and weekly meters with the `[usage].rules` marked on
     them and when each resets, the rules, this run's burn, the next cap the run
     reaches (at the forecast's burn per busy worker-hour, times the workers busy
     now) against when the work is done, and a chart of each window over time
     (the last day of 5-hour, the current week) with the caps drawn across it;
   - **resources:** the resource sampler's latest reading: CPU, memory
     (available, anon, page cache), swap, pressure, disk write rate and free
     space, and each running build (an idle holder in red, or in yellow once
     the gate has released its slot). History and capacity are `swarm resources`;
   - **alerts & notifications:** what needs you (`◆`), what is wrong now (the
     footer's list, and any warning or failure from the last doctor run), then
     every ping newest first: `✓` delivered, `·` held, `✗` never arrived.
     `enter` opens one; `x` acknowledges the ones that never arrived;
   - **shells:** what `swarm keep` left running.

   Under 110 columns the right column moves under the feed and needs you
   returns as a strip at the top.
2. **workers:** one row per slot. A busy slot whose pane died shows `gone`.
   A parked worker you have answered is at work in no slot: it has a row of
   its own (`in window`, with its `wait:<phase>` window), as it has in home's
   working now box. If its session is gone (its window closed, or nothing of
   it left running: the rule the watchdog's sweep settles it by) the row says
   `GONE` like a slot's, and its detail says what is missing and whether the
   running supervisor's sweep settles it. The detail's border names the
   selected row: `slot`, or `parked worker` for one that holds none.
3. **history:** every phase run, with what it did. A run that is parked and at
   work names its window.
4. **alerts:** the whole notification log with each ping's detail. `F`
   cycles all, failed and delivered.
5. **disk:** sizes of mirrors, caches and the state dir. Scanned on demand (`r`).
6. **settings:** a typed form over every config key, with its reload class.
   Applying it edits `.swarm.toml` in place, keeping comments, then runs
   `swarm reload`.
7. **commands:** every subcommand, runnable with streamed output. Destructive
   ones (`down`, `finish`, `free`, `skip`, `done`, `retry`, `integrate`,
   `operator-done`, `gc --yes`, `reset`) ask for confirmation.
8. **doctor:** runs `swarm doctor` on demand.
9. **runs:** past runs with their per-hour figures.

One more sits beside them:

- `0` **shells:** what `swarm keep` left running, with why; `x` stops one.

`R` resets the run (after a confirmation), `D` drains then stops (it asks for an
optional after-command, then confirms), `?` opens help, and `q` quits the
dashboard only. It fits an 80×24 terminal: panels stack, the chart drops out and
columns hide as it narrows.

## The web board (`swarm web`)

A read-only board for a phone or a browser. Under tmux the dashboard
serves it from its own process and stops it when it exits; it first checks the
port, so a board already answering there (a `swarm web` run by hand) is left
alone and named in the status bar, and it takes over once that one stops. With no
dashboard (the `bare` driver, or `[tui] autostart = false`) `swarm up` starts it
as a detached process and `swarm down` stops it. A board that starts while the
previous one is still closing waits up to 5 s for the port instead of failing. `swarm up`, `swarm status` and
`swarm doctor` print its address: the machine's Tailscale IP (`tailscale ip -4`),
or its LAN addresses when Tailscale is not running.

**Tabs:**

- **Overview:** when every phase is done (the forecast's P50, P85 behind it, the
  method behind an info toggle), progress by status, what each worker is
  building, the next usage cap against that finish, and the phase books still
  open, soonest first.
- **Phase books:** every campaign ("phase book", a phase-id prefix) with its
  progress, open rows by status and finish range; one opens to its rows, each
  with what holds it and its own finish. *By status* is the status board:
  Needs you, Building, Merging, Ready, Blocked, Operator, Failed, Done, Yours to
  do. Rows ticked in the ledger count as done, owner-run ones included.
- **Graph:** phases as boxes and `needs:` as lines, laid out left to right in
  Python (`web/layout.py`: longest-path layers, barycenter ordering, drawn as
  the transitive reduction) and drawn as SVG. By default the open rows and the
  done rows they need; *All* shows every row, and a phase book narrows it.
  Colours are by status, the critical path to "all done" is gold, and a hover
  or click lights up everything upstream and downstream with a side panel. The
  layout is cached by shape, so a status change only re-colours it.
- **Usage:** the 5-hour and weekly windows over time with their pause and stop
  lines, resets, and a projection at the forecast's burn to the cap each will
  reach (or its reset). A sudden big jump in a reading, which is another
  account's counter, breaks the line rather than drawing a drop. Below it, the
  runs.
- **Resources:** what `swarm resources` prints, for a browser. *Host now*: CPU,
  load, MemAvailable, anon, page cache, swap, disk headroom and its parts, the
  state dir, worktrees and build caches with their growth, pressure (PSI) for
  CPU, memory and IO, each running build with its cores and memory, the build
  queue in the order it would start with each wait, and every session's cores
  and memory. *History* over 1 h, 6 h, 24 h, 7 d or 30 d: builds running and
  queued, CPU, MemAvailable, anon, memory and IO pressure, disk write. Every
  point is a bucket drawn as its lowest-to-highest band with the average on top,
  so a one-second burst is still there at 30 days; the builds are shaded columns
  behind every chart, and hovering names the build under the pointer. *Builds*:
  the finished builds of the same window, sortable by any column and filtered by
  phase. *Capacity*: the scenarios `swarm resources` works out (as configured,
  2 builds, 8 workers, both, more jobs) with their arithmetic and its notes
  (too little data, pressure already seen during builds). The Overview carries
  one line of it.
- **Activity:** recent finishes with their recaps, notifications, Overseer
  passes, and what `swarm keep` left running.
- **Row sheet:** the ledger row, recap, notes, dependencies, operator jobs and
  attempts. Deep links use `#<tab>&phase=<id>` (the old `#phase=<id>` still works).

Endpoints: `/api/board`, `/api/graph?mode=open|all&book=<name>`, `/api/usage`,
`/api/phase/<id>`, `/api/search?q=`, `/api/resources`,
`/api/resources/history?window=1h|6h|24h|7d|30d`,
`/api/resources/builds?window=&sort=&dir=&phase=&limit=`,
`/api/resources/capacity`, all gzip + ETag. The page polls them only
while it is visible, only the tab on screen asks for its data, and an unchanged
answer is a 304. (`/events`, Server-Sent Events, is still served.)

The Resources tab costs nothing while nobody has it open, and little when
somebody does. The sampler's history (`meters/resources.jsonl`, tens of megabytes
within a day) is read once, on the first request; after that only the lines
appended since are read, and each is folded on arrival into a fixed 336 to 360
buckets per window, so a request is answered from memory. The finished builds
are kept the same way, and sorted and filtered on the server, so the page holds
one screen of them. Capacity needs the month's samples: it is worked out at most
every ten minutes, from rows streamed off the files, with the same function
`swarm resources` calls. A build's command line is shown with this machine's
paths shortened (`<state>`, `<project>`, `~`), cut to 96 characters and scrubbed
like every other payload; its working directory is reduced to its place inside
the worktree.

It is plain `http.server`, GET and HEAD only, and no URL path ever maps to a file.
Every payload is scrubbed of credential-shaped strings. It listens on every
interface **with no token**, by the owner's choice (`[web].host = "0.0.0.0"`). `/healthz`
answers `{"app": "swarm-web", "project": …, "slug": …}`, which is how `swarm up` tells its own
board from another program holding the port. `project` is the swarm's display
name (`[swarm].name`); the board is recognised by `slug`, so one started under
an earlier name is still this swarm's.

Tailscale inside WSL needs nothing more. Without it, under WSL with mirrored
networking, a phone on the LAN reaches the board only once Windows lets the port in:

```powershell
New-NetFirewallRule -DisplayName "swarm web" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
```

If it still does not answer, also run
`Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow`.
Both need an elevated PowerShell.

## Telegram and asking the owner

The swarm has its own sender: `scripts/notify.sh`, with a bot of its own. It reads
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from this repo's
`.env`, which is gitignored; `scripts/resolve-chat-id.sh` fills in the chat id.
Point `[telegram].notify` at any script that takes the message as `$1`.

Messages are plain text, capped at 3800 characters. A `swarm notify` sent from an
Overseer pass is its summary. Every send, delivered or not,
is logged to `<state>/notifications.jsonl`, and the dashboard's alerts tab reads
that log. A message the swarm holds back on purpose is logged there too, with
`delivered: false` and a `suppressed` reason; the dashboard shows it as `·`, not
as a drop, and `swarm doctor` does not count it as one. `swarm notify --ack` (or
`x` on home or the alerts tab) acknowledges the drops so far without touching the log;
the footer, the drawer and `swarm doctor` then count only later ones, and doctor
only fails on a drop that is recent or on sends that are still failing.
`swarm notify "<text>"` is the only way a session should message you.
Every shipped prompt, and the init pass's patch to the worker command, says so
in so many words: use `swarm notify` even when a brief, a ledger row, a recap or
a project document names another script (a `notify.sh`, say). A message
sent that way would not come from the swarm's own bot and would not be logged.
An operator's result that needs you
(a URL to open, something only you can do) goes in its `operator-done` outcome
with `--attention`; a decision it needs first is asked with
`swarm waiting <job> "<question>"`.

**What pings you.** Only necessary messages ring by default
(`[telegram].pings = "necessary"`); the phone rings only for these:

- a worker, the operator or the Overseer asking you something (`swarm waiting`);
- an owner-run row that starts holding other rows up (once per row);
- a merge hold you must clear: a dirty tree, or a conflict no resolver could
  start. A conflict a resolver is working on is not sent; if the resolver cannot
  fix it, it messages you itself (`swarm notify`);
- an operator job's outcome flagged `--attention`, an abandoned job, or a to-do
  while the operator is off;
- a phase that fails again after the Overseer's retry. With the Overseer on, a
  first `fail` is its to handle (it retries a failed phase once); with it off,
  every `fail` pings. Which failure this is comes from `done/<phase>.jsonl`: a
  `swarm done` that finds no sentinel of its status opens a new episode
  (`"fresh": true`), and `swarm retry` removes the sentinel;
- phases that finish `blocked` (past that retry), gathered: one ping 15 minutes
  after the first of a burst lists every phase blocked since, grouped by reason
  (the recap's first sentence), so one outside cause is one ping;
- a repo still owing a push after `[telegram].push_owed_grace_s` (default 1 h),
  checked after every integration and on the watchdog tick; the "pushed" ping
  follows only if the "owed" one went out;
- a phase that would not start (`spawn-fail`, `worktree-fail`), once per phase;
  a launch given up after repeated failures; a worker that died without
  `swarm done`;
- a supervisor crash or error; a master that would not start, or an Overseer
  pass that would not start or ran past its timeout, on the third in a row (and
  every third after that);
- the Overseer's summary on a cadence pass
  (`[overseer].every_finished`) or a pass you asked for (`swarm overseer --now`).
  Any other pass (the clock, starvation, a hold, a doctor FAIL, an owner wait)
  records its summary without sending it, unless it runs
  `swarm notify --attention` because something needs you. The digest tells the
  pass which case it is in;
- a note from the init pass or a resolver (`swarm notify`);
- the finish summary;
- a usage cap pausing or stopping the swarm, and a usage pause lifting;
- a heavy build holding a build slot with its whole process tree idle
  (`[resources].idle_s`, default 10 minutes), again hourly while it stays idle;
  the message says whether the gate released its slot;
- the bot's answers to your `/usage` and `/help`.

**Logged, not sent:** routine operator outcomes (the Overseer's digest lists
them), parks (you were asked when the session started waiting), a first `fail`, a
push owed for less than the grace (and its clearing), a conflict a resolver is
working on, a web board that did not
start (`swarm up` prints it), a single master or Overseer failure, a repeat
failed start, and the summary of any other Overseer pass. Each goes to `notifications.jsonl` with `delivered: false` and a
`suppressed` reason, shows on the dashboard's alerts tab as `·`, and is not
counted as a drop. `[telegram].pings = "all"` sends all of them again, as before.
`ok` finishes are silent either way.

Question pings start with what the wait costs, for example
`holding up 3 phases · a worker place is tied up · asked 14:05`, and the question is
cut to 600 characters. The full text is on screen in the asker's pane. Every ping is
written for the owner: what is happening, what it means for the run, and whether to act
and where, with no code detail beyond a command to run.

**Commands (`tgbot.py`).** The bot also listens, so you can ask it:

- `/usage`: both limits and the usage caps' state, read when you ask;
- `/help` (and `/start`): the list.

The listener long-polls `getUpdates` with the same token and answers through the
same sender. It answers only messages from the chat in `TELEGRAM_CHAT_ID`, and
ignores every other chat without a reply. It also ignores plain text, edits, and
commands older than 15 minutes (sent while nothing listened). The next update id
is saved in `<state>/telegram-bot.offset.json` before an update is answered, so
none is ever answered twice.

- **Lifecycle:** `swarm up` starts it as a detached process under either driver
  (no tmux window), with its log in `<state>/logs/telegram-bot.log`. `swarm down`
  stops it, and its reaping would find it anyway, since it carries the run's
  `SWARM_STATE_DIR`. `swarm telegram-bot` runs it in the foreground. `swarm status`
  and `swarm doctor` (`telegram.bot`) say whether it runs and what it is doing.
  `[telegram].commands = false` turns it off.
- **One poller per token:** Telegram answers `409 Conflict` when two programs poll
  one bot (or a webhook is set). Two projects on one box share the bot through
  this repo's `.env`, so a lock per token (in `$XDG_RUNTIME_DIR`) keeps the second
  listener waiting to take over, retrying every minute; `/usage` then answers from
  the project that holds it. A 409 from anything else is logged, shown by
  doctor, and backed off from 60 s up to 10 minutes. `scripts/resolve-chat-id.sh`
  polls the same token, so run it while the listener is stopped.
- **Failures:** network errors back off from 5 s up to 5 minutes, a rejected token
  waits 10 minutes, a 429 waits as told. None of it touches the supervisor.

## Reload, layout and check

**`swarm reload [--dry-run]`** applies a `.swarm.toml` edit to the running swarm.
Every key is classed as *hot* (applied now), *next* (reaches the next session
launched) or *restart* (refused and held). A key that a `SWARM_*` variable
shadows is reported as such. A file that does not parse changes nothing. Growing
`max_workers` adds panes, filling the last worker window up to
`[tmux].panes_per_window` before opening the next `workers-N`. Shrinking it
retires the surplus slots once they are free.

**`swarm layout [name]`** re-arranges the worker panes live. The choices are
`auto`, `side-by-side`, `top-bottom`, `tiled`, `main-vertical` and the rest. With
no argument it prints the current layout and every valid name.

**`swarm check [--strict]`** is a preflight that needs no running supervisor:

- the Telegram sender and its credentials;
- ledger parse and structure;
- **promptlint** (`promptlint.py`) over your worker command file and the shipped
  prompts.

Promptlint flags sentences the code has made false as *contradicted*: a `swarm`
subcommand that does not exist, "an agent call is synchronous", the retired
`needs-owner` status, or a claim that no status sends a Telegram. It flags
measured time-wasters as *wasteful*: `sleep` loops, `git status` polling, and
heredoc string-replace edits. The command exits 1 on a
Telegram or ledger failure, or on a *contradicted* finding.

## Smaller modules

- **`blockedping.py`:** under `[telegram].pings = "necessary"`, gathers a burst of
  `blocked` outcomes and sends one ping listing the phases under each distinct
  reason, instead of one ping per phase.
- **`todo.py`, `guide.py`:** `swarm todo` lists everything waiting on the owner that
  is not a question (owner-run ledger rows, operator results, and so on). `swarm
  guide` (`g` in the dashboard) opens a chat session in its own tmux window that
  walks through that list.
- **`opqueue.py`:** the durable queue of operator hand-offs, one JSON file per phase
  under `<state>/operator/`, written by `swarm done` after the sentinel.
- **`ovdigest.py`, `ovrecord.py`:** the digest the Overseer reads before each pass,
  and the per-pass record (`<state>/overseer/<id>.md` and `.json`).
- **`landing.py`:** the re-test of a phase against what landed beside it when
  `[lanes]` is on (see the integrator section).
- **`pauseat.py`:** `swarm pause --in 12h` / `--at 03:00`, a pause scheduled in
  `state.json` that survives `swarm down` and `up`.
- **`procs.py`:** reads `/proc` for the process table and process identity (pid plus
  start time), shared by the reaper and `swarm keep`.
- **`ledgermigrate.py`:** one-time move of a ledger's accumulated notes and journal
  into history.
- **`logutil.py`:** the supervisor's structured, greppable log lines.
