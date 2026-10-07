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
  touches, and `[lanes] per_repo` caps phases per repo. A touch that matches
  `[lanes] commons` is in none of the three: `lanes.owned` leaves it out, for the
  pick, the launch backstop, `swarm why`, `swarm widen` and a reshape alike, so it
  is never held, reserved or counted, and a row that names only commons launches
  beside anything.
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
  window)`; one that still asks is not. When that is nothing it sends your last
  summary (the swarm is shutting down, as you asked) and starts `swarm _drain-down` in a session of its own, which runs
  `swarm down` and then the after-command
  (in a `systemd-run --user --scope` where it can, so neither the tmux teardown
  nor a logout takes it).

**What it may not do:** it never builds, never retries a phase that finished
`fail`, and never restarts a worker. An exception in one event handler is logged,
stepped over and handed to the Overseer to look at. Only a failure of the loop
itself ends the process, and that announces itself (`SUPERVISOR-CRASH` plus an
ask to restart it). Everything it does is
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
- retries owed pushes, at most every 15 minutes, and clears at once one that
  was pushed by hand since.

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
dashboard, the web board (the machine's one, so the other swarms' pages blink)
and the machine's Telegram listener are started again. The tmux
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
5. If no supervisor comes up (two attempts), you are asked to run
   `swarm restart`, which brings it back. Sessions are still
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

## Freeze and thaw (`swarm freeze`, `swarm thaw`)

`swarm pause` stops launches and `swarm down` ends the run. A freeze does
neither: every session stops where it stands, holding its memory and its place,
and carries on from the same instruction at the thaw. It is for giving the
machine to something else for a while (another user, a job that needs the
memory) without losing a minute of anybody's context. The swarm only freezes;
what is done with the memory meanwhile is the caller's business.

**The freezer is the kernel's.** A group is frozen by writing `1` to its
`cgroup.freeze` and is frozen once its `cgroup.events` says `frozen 1`. Stop
signals are never used: tmux continues a stopped pane by itself.

**What is frozen is decided by membership, never by a name.** Every process
that carries the run's `SWARM_STATE_DIR`, plus everything under a pane of the
run's tmux session, is mapped to its group through `/proc/<pid>/cgroup`. Each
group goes by the session in it (`worker P3`, `operator <job>`, `overseer
<pass>`, `resolver`, `bigpic`, `console`, `dashboard`, else `other`), which is
what gives the thaw its order.

**What stays awake, and why.** The groups of the supervisor, the Telegram
listener, the web board, the tmux server and whoever ran the command are
never frozen: something has to be there to thaw, to answer `status`, and to
keep the panes' terminals alive. One of those groups that also holds sessions
is reported `shared`, and its sessions stay awake with it. So that this does
not happen by accident, the swarm starts its own long-lived processes in a
systemd user scope of their own where one can be made (`systemd-run --user
--scope`): the supervisor, the listener, the web board and each lane check.
Freezing the terminal `swarm up` was typed in then never freezes the supervisor,
and a lane check is frozen with the rest. Where no scope can be made (no user
systemd, or `SWARM_SCOPE=0`) `up` and `freeze` say that the supervisor shares
the group of whatever started it.

**The sequence.**

1. `freeze` writes the record (`State.frozen`, stage `freezing`) and tells the
   supervisor, which from then on starts nothing and writes what it is still in
   the middle of into the record (a worker starting, an Overseer or big-picture
   session starting, a clean-up, a backup push), then the moment that is
   nothing.
2. The command waits for that moment, at most `--wait` seconds, then freezes
   anyway and reports `quiesced: false`. With no supervisor running it does not
   wait.
3. It looks at the run again (a launch that was under way has its session by
   now) and names every group it is about to stop in the record. No group is
   told to freeze before it is named there, so whatever cuts a freeze short
   (an interrupt, a kill), `swarm thaw` finds everything it has to wake.
4. It takes the state lock and the build queue lock, writes `1` to every group
   and keeps both locks until each group says it is frozen: a process frozen
   with either in its hand would stop everything still awake, and the build
   queue lock is the machine's, so that would be every swarm's builds.
5. A group that was told to freeze and has not said so within 10 seconds takes
   the whole freeze back: everything is woken, the record is dropped, exit 1
   (`FREEZE-ROLLBACK` in the log). A group this user may not write is not that
   case. It is **left** as it is and named, and the rest are frozen without it.

**The supervisor stands still.** While the record is there its loop wakes once
a second, looks whether it still is, and does nothing else: no park deadline, no
watchdog sweep, no launch or retry, no Overseer or big-picture look, no operator
lease, no gc, no backup, no ping. It handles `freeze`, `thaw` and `shutdown`;
every other event (`done`, `waiting`, `resumed`, a launch that settled) waits
and is handled, in the order it came, at the thaw. The resource sampler stops.
A worker launch the freeze catches half way is undone, counts as no failure and
starts again at the thaw.

**One verb at a time.** `freeze` and `thaw` each hold `<state>/freeze.lock`
from their first step to their last, and nothing else ever takes it, so no
frozen session can be holding it. A freeze that arrives while a thaw is waking
the sessions waits for it to finish and then freezes from scratch; a thaw that
arrives while a freeze is being made waits and then wakes what it froze. The
run ends as the verb that came last asked. One that has waited ten minutes for
the other gives up, changes nothing and exits 1.

**The thaw** first says in the record that it has begun (stage `thawing`), so
nothing reads a half-woken run as frozen, then writes `0` to each group with no
lock in hand that a session can hold: whoever froze the sessions may have
frozen something that holds one. (If the state lock is in such a hand, saying
it waits until the sessions are awake.) A `freeze` that finds a thaw cut short
finishes it first. The order is your
console, the dashboard, sessions asking you, the operator, the Overseer, a
resolver, the workers by slot, then the rest, with `--gap` seconds after each
Claude session. Then it stamps the end into the record, moves the clocks, adds
the span to `<state>/history/frozen.jsonl`, drops the record and tells the
supervisor how long it lasted.

**Frozen time is not elapsed time.** Two things make that true.

- *Every deadline the swarm keeps is moved along by the length of the freeze.*
  In `state.json`: park deadlines, when each session asked and was answered,
  the last event, the operator lease, the Overseer pass's deadline, each owed
  push's age and last try, each landing's stage, each failed launch's back-off.
  In the other files: operator jobs (lease, declared long work, a failed
  start's back-off, how long a job and a question have waited), the Overseer's
  policy (gap, cadence, hold, owner wait, starvation), the big-picture memory
  (a running pass, the doc's age, the back-off), a gathered burst of blocked
  pings, and the build gate's idle samples of this swarm's own builds (the
  other swarms' builds were never frozen). In the supervisor's memory: the
  watchdog sweep, ping cooldowns, crash counts, launch back-offs, the gc and
  backup clocks and an adoption's recheck. Each file is named in the record's
  `shifted` once it is moved, so a thaw that was cut short and is run again
  moves nothing twice. A supervisor that was inside one long event from before
  the freeze until after the thaw never stood still for it; it moves its
  clocks when it reads the thaw (`THAW-MISSED`).
- *Not moved:* a time somebody chose on the clock (a scheduled pause, a
  scheduled restart, an operator job's `--not-before`), anything the usage
  provider set (reset times, the hold they end, the usage check itself, which
  runs on the first wake), the run's start, and every timestamp that is a
  record of when something happened (log lines, sentinels, notes).
- *Whatever measures from a moment nothing can move takes the frozen spans
  out.* `swarm build --timeout` and the wait for a build slot, the timeout of a
  command the swarm runs itself (post-merge, a lane check), a build's recorded
  run time, a waiter's ticket (a frozen waiter has not left the queue),
  doctor's start grace, activity check, stall and ages, a phase's duration and
  a running worker's age on the dashboard, and the forecast's work times. The
  frozen hours of a run are neither work nor idle time with a free slot.

**What a caller sees.** `swarm freeze --json` prints, on success (exit 0):

```json
{
  "frozen": [{"path": "/user.slice/…/tmux-spawn-….scope", "kind": "worker", "id": "P3"}],
  "left":   [{"path": "/system.slice/….scope", "kind": "other", "id": ""}],
  "awake":  [{"path": "/user.slice/…/run-….scope", "kind": "supervisor", "shared": false}],
  "quiesced": true
}
```

`frozen` is what this freeze stopped and what `thaw` will wake. `left` is what
it could not write: a caller with the right to should freeze those itself
after this returns, and wake them before it runs `swarm thaw`. `awake` is the
run's own groups, which a caller freezing more than the swarm (a whole login,
say) must leave alone, or nothing is there to thaw. On exit 1 nothing is
frozen and stderr says why.

**A record nothing stands behind.** After a reboot the groups are gone and the
record is still in `state.json`. `swarm up` closes its span at that moment,
drops it and starts (`FROZEN-STALE`). A record whose groups are still frozen
refuses `up`: run `swarm thaw` first.

**Could not tell is not thawed.** The supervisor reads the record without the
state lock. A read that fails says nothing either way, and it goes on believing
what it believed. `swarm thaw` exits 1 on a state it cannot read rather than
say nothing is frozen. And the record is dropped at the end of a thaw whatever
becomes of the bookkeeping around it: a span or a log line that cannot be
written is not a reason to keep the supervisor standing still.

**Limits.**

- Only a worker launch is undone when a freeze catches it. Another session
  being started at that instant (an operator job, an Overseer or big-picture
  pass, a resolver) with the supervisor not yet quiet is frozen before it is
  ready and counts as a start that failed: it is retried the way any failed
  start is.
- A supervisor in the middle of one long event (a merge running its post-merge
  command) cannot answer inside `--wait`: the freeze is `quiesced: false`, that
  command runs on, awake, in the supervisor's group, and the supervisor stands
  still from the end of that event.
- The span runs from when the freeze was asked for to when the last group was
  woken, so it includes the wait and the gaps. Deadlines move by a little more
  than the sessions stood still, never by less.
- A timer inside a frozen process that is not the swarm's (a tool's own
  timeout, a script's `timeout 600 …`) still sees the clock jump. So does a
  helper's call to a model (a recap, an operator triage): it gives up at the
  thaw and falls back the way it does on any failed call.
- Notes held for a shared ledger commit keep the time they were written, so
  the ones due inside the freeze are committed on the first flush after it.
- A group reported `left` that its owner wakes after `swarm thaw` stood frozen
  for longer than the span says: a build in it can meet its `--timeout` early.
- A thaw killed between moving one of the other files and noting it moves that
  one file again when it is rerun. The state's own clocks are moved and noted
  in one write.

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
  branched at launch. An explicit `--project-dir` that names another project
  is refused: the session carries this swarm's state dir, and a command there
  can only act on this swarm;
- a private `TMPDIR` on disk under `<state>/tmp/<phase>`;
- `CARGO_INCREMENTAL=0`;
- under worktree isolation, also `SWARM_WORKTREE`, `SWARM_MAIN` and the
  build-gate limits.

The folder-trust dialog is accepted ahead of time for every new mirror, by
adding the folder to `~/.claude.json`. That file is the owner's, shared with
every Claude session and with every other swarm on the machine, so the
rewrite is done under one lock for the machine (`claude-json.lock` in the
[machine directory](#several-swarms-on-one-machine-machinepy)) and through a
temp file of its own. A lock that stays taken for five seconds is given up on
and the file left alone (`PRETRUST-SKIPPED`). If the dialog
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
  - `fail`: it is rolled back under worktree isolation, and the phase's
    dependents stay blocked until `swarm retry`. The Overseer retries it once;
    you are asked when it fails again, or at once with the Overseer off.

**What they may not do:** under worktree isolation, they commit on `swarm/<phase>`
and never push; the integrator pushes. Nothing else is clamped: tools,
permissions and scope are the session's own.

`swarm done` prints what it did: whether the sentinel was written, whether you
were asked, the operator route, and the FIFO poke. A second call with a shorter recap never
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
- runs `swarm context` and asks you to fix ledger cycles or unknown
  dependencies (`swarm notify`);
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

- **When it runs:** a pass is triggered by events and by one clock, collected into one
  pending list (see [the diagram](../README.md#init-pass-and-overseer)). Only one
  pass runs at a time. Passes are `min_gap_s` apart, unless a reason is urgent: a held merge
  queue, a doctor FAIL, starvation, or `swarm overseer --now`.
- **The summary clock:** every `every_s` (4 hours by default) a pass starts to
  write you its summary (`swarm overseer-summary`, two short sentences). It is
  the only pass whose summary goes to your phone; on any other the command only
  records it. The clock counts from the last summary pass (from the swarm's
  start for the first), so the passes a busy run starts for other reasons never
  put it off. If that pass ends, is parked on a question or never starts
  without having sent one, the supervisor sends a summary of its own with the
  bare counts (phases landed and failed since the last summary, how many are
  building, what waits on you), and it does the same on the clock while the
  Overseer is off. So a summary goes out every `every_s` whatever becomes of
  the session. The one exception is a swarm that stood still: when nothing
  landed or failed, nothing was held back and nothing is building since the
  last summary (paused, idle, or waiting on an answer you were already asked
  for), no pass starts and nothing is sent; the clock starts again
  (`SUMMARY-SKIPPED` in the supervisor log). A summary that comes due as the
  run ends does not hold the finish: the finish is itself the last summary.
- **No pass per number of finished phases.** A pass used to start every few
  finished phases. What it did for them (read their recaps, file rows for the
  risks they noted) the next pass does, the clock's at the latest. The rest of
  what it did has triggers of its own: a `fail`, starvation, and the box.
- **The box:** RAM, swap, `/tmp` or the state disk at a dangerous level (the
  digest's own warnings) for five minutes in a row triggers a pass, once until
  the box has recovered. The supervisor looks once a minute.
- **Merge holds:** a conflict the resolver is working on is left to it. The hold
  triggers a pass only when no resolver opened, the resolver gave up (it messaged
  you, or ran `swarm resolved` on an unfinished merge), or the hold is older than
  `hold_wait_s`. The trigger is checked again just before a pass starts.
- **Owed pushes:** a push the repo's own pre-push check refused triggers a pass
  at once. A push that failed any other way (the remote turned it away, a fetch
  timed out) triggers one only if it is still owed two minutes later; the next
  push settles most of them within seconds.
- **What it reads:** before each pass the supervisor writes
  `<state>/overseer/digest-<id>.md` (and `.json`). It holds the trigger, the swarm
  now, every phase finished since the last pass with its recap and notes, every
  operator job finished since then with its outcome (ones with an ask first),
  what the swarm held back for the summary since the last one, failures, questions waiting on you, the `owner-run` rows whose dependencies
  have landed and are holding rows up, a starvation map (which root
  blockers hold how much backlog), and a snapshot of RAM, swap, `/tmp` and disk.
  Those figures are the whole machine's, so beside them the digest says whose
  load it is: the machine's build gate now (its limit, each build on it as
  `this swarm` or `another swarm [name]`, how many wait and how many of those
  are this swarm's) and the other swarms on the machine with their status.
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
  - ask you for what only you can do (`swarm notify`, or `swarm waiting` when
    it needs your answer to go on);
  - write your summary on the pass the clock starts (`swarm overseer-summary`).
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
`[operator].enabled = true`**. While it is off, each hand-off asks you to do it
(the recap is in `swarm todo`) instead of being dropped.

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

**Waits longer than the hour:** a session still there when its lease runs out is
taken for hung: it is closed, and its job is queued again with the attempt
spent. A wait is not a hang, so the session says which kind it has:

- *The thing runs without it* (a measurement left running on a host, a time
  window): it puts the job back with
  `swarm operator-done <job> "<where it stopped>" --not-before <when>`. The
  window is free for other jobs, the attempt is given back, and the job opens
  again at `<when>` even if triage said `later`: that only decided when to open
  it first. The note is kept on the item (`resume_note`) apart from the last
  error, so a reopening that fails cannot lose it, and it is in the next brief.
  The job does not reopen while its own mirror is still in the merge queue.
- *The session has to stay* (a build of its own queued behind another, which
  ending the session would end): `swarm operator-hold <job> <how long> "<why>"`
  moves the item lease and the state lease together to the time it gives. It is
  bounded (4 h per call, `opqueue.HOLD_MAX_S`; a session still at work runs it
  again, which a hung one cannot), it never shortens a lease, only the session
  carrying the job out may run it (`SWARM_OPERATOR_JOB`), and it is logged
  (`OPERATOR-HOLD`) and shown by `swarm status` and both dashboards. An answer
  from you in the middle does not cut it short.

Past the time a session declared, it is reclaimed exactly as at the hour. That
expiry does not count as an attempt, up to three times per job
(`opqueue.MAX_LAPSES`): after that it counts like any other, so a job whose
sessions only ever declare a wait and hang still reaches the cap and you.
A session that declared nothing is reclaimed at the hour, as before.

**Where it works:** under worktree isolation, in its own mirror `op-<job>` (reused
by a retry), merged through the ordinary queue on `operator-done`. Without
worktree isolation, it works in the project itself.

**What it decides:** how to carry out the brief. It first checks whether a later
phase or you already did the work. It narrates each action and prefers the step
it can undo. A decision only you can make is asked before the job finishes:
`swarm waiting <job> "<question>"`, then, once you answer in its pane,
`swarm resumed <job> "<answer>"`; your answer is recorded as your decision on
the job's phase, and follow-up work is queued with `swarm operator-add`. It
ends with `swarm operator-done <job> "<outcome>"`. When the outcome leaves
something only you can do (you must act, something the brief asked for is still
owed, or a check came back bad), the session adds `--ask "<what you must do,
then why>"`: that short ask goes to your phone, and the outcome stays on the
job, the board and `swarm todo`. An ask too long for a notification is refused
before anything is recorded. Every other outcome is recorded (on the job, in
`notifications.jsonl` as folded, on the dashboard) and the Overseer's next
summary accounts for it: its digest lists every operator job finished since its
last pass. Questions and abandoned jobs always ask.
`swarm operator-done <job> "<why>" --not-before <when>` means the job's moment
has not come yet: it goes back in the queue until `<when>` (`90m`, `6h`, `3d`,
`2026-09-30`, `"2026-09-30 08:00"`) instead of finishing, the attempt is not
counted, and its next brief says where the last session stopped.
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

**The ask.** The text after `<who>` is what your phone shows, as
`[<swarm>] Asks you: <text>`: what the session needs from you, then why, in one
or two short sentences. One that does not fit a notification is refused before
anything is recorded, with the limit, and the session rewrites it; the question
in full, with its options, is asked in the session's own pane. `swarm status`,
the needs-you drawer and the board say which tmux window that is. A worker's
ask goes every time it asks; an operator job's or the Overseer's only when the
question is new, so a re-run cannot ring you twice.

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
Overseer's digest. The supervisor asks you once per such row when it starts
holding rows up — recorded in `<state>/owner_rows.json`, so never twice, across
restarts too, with rows found together sharing one ask — to do it and then tick
it in the ledger or run `swarm skip <row>`, because only you can and rows wait
on it.

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

**One commit per landed phase** (`[tasks].ledger_in_merge`, on by default). A
phase that landed used to leave two commits in the project's history, its merge
and the writer's `ledger:` commit. Now the reports due with it ride in the merge
(`ledgerw.Ride`): once `swarm/<phase>` is merged into the project checkout, and
before the repo's `[git].post_merge` command and the push, still under the repo
lock, the writer applies them exactly as a flush does (the ledger gate included)
and amends the merge commit with the ledger, the history directory and the
lessons file, nothing else (`gitq.amend_merge`). The commit keeps its two
parents and the start of its subject, which gains what was written:
`Merge branch 'swarm/<phase>': <phase> done; follow-up <id>`. What rides is the
phase's own reports and everything waiting to be written at once, the notes and
lessons held under `ledger_batch_s` included. The command and the repo's
pre-push check then read the tree that is pushed, and it is pushed once. The
tick is still written only after the phase's merges succeeded and its landing
check passed: the umbrella merges last.

Each of these keeps the separate `ledger:` commit, written by the flush that
follows the landing:

- a phase with no commit in the umbrella (its work is in component repos only):
  there is no merge commit to amend;
- `isolation = "none"`, and a `swarm integrate` by hand;
- a merge that conflicted and was finished by a resolver;
- a phase with no report of its own queued, and an operator job's or an
  Overseer pass's mirror;
- any failure of the amend (`LEDGER-RIDE-ERROR`: a hook that refuses it, or a
  commit someone made in the checkout meanwhile, which is never amended), or
  files it may not take (`LEDGER-RIDE-HELD`): the merge stands and is pushed as
  it is, the three paths are put back, and the report stays queued.

A push that loses a race merges origin into main as before; the amended commit
is under that merge and its reports are already off the queue, so nothing is
written twice. `pace.py` counts the lines of a merge that neither parent had,
so a tick inside one still dates its row.

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
repo (components first, those with a `[git].post_merge` command after the
others, umbrella last), each under a per-repo `flock`:

- **Untouched repos** (no commits on the branch, main level with origin) are
  pruned with no network at all.
- **Changed repos:**
  1. Check that the canonical tree has no uncommitted tracked edits.
  2. Merge `origin/main` in.
  3. `merge --no-ff swarm/<phase>`.
  4. Run the repo's `[git].post_merge` command, if it has one (below).
  5. Push. A non-fast-forward rejection is reconciled by merging origin again and
     retrying, up to 5 rounds.
  6. Remove the worktree and branch.

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
resolver on the worktree. Each run writes `<state>/landing/<phase>.<repo>.log`
afresh and keeps the log of the run before beside it as `.log.prev`, so the
output of a red check can still be read after `swarm resolved` starts it again.

A check takes minutes and the ledger writer commits to main every few minutes,
so main has often moved again by the time a check is green. The green check
stands when everything main gained since the tested merge is `[lanes] commons`:
main is merged into the worktree once more, without a second check, and the
phase lands (`LANE-CHECK-KEPT` in the supervisor log). If main gained any other
file, the pair is tested again (`LANE-MAIN-MOVED <phase> <repo> main gained N
file(s)`). A conflict at the unchecked merge goes to the resolver on the
worktree, and what the resolver wrote is checked before it lands. To see how
often checks are repeated, count both lines in the supervisor log.

**The prepare command** (`[lanes].prepare`, `repocmd.py`): a lane check, like a
repo's pre-push check, can read things git does not hold. The usual one is the
dependencies installed beside the checkout. A worker installs them in the
worktree it builds in, so a phase that never built in a repo has none there, and
a catch-up merge that moves a pin leaves the ones the old pin named. The check
was then red within seconds for a reason no merge can settle, the queue was
held, and a resolver session was opened only to run the install. A project
names the command that session would run, per repo, and `swarm _lane-check` runs
it in the worktree before the check:

- `[lanes].prepare_if` is asked first, at once and outside the build gate. Only
  when it exits 0 does anything run, so a worktree that is ready costs one quick
  test. One that cannot be asked is a no, and the check's log says so.
- The command runs through the build gate like the check that follows it, for
  at most `[lanes].prepare_timeout_s`. Nothing waits on it but this landing: it
  runs in the detached check, and its output is in the check's log.
- Passed: the check runs.
- Failed, timed out, or tracked files left changed (the check would test a tree
  that is not the one that lands): no check runs, and the result is
  `unprepared`, not red. `LANE-UNPREPARED <phase> <repo> <reason>` in the
  supervisor log gives the command's own last lines. No resolver opens and the
  merge queue is not held. It is recorded for the Overseer's next summary, once
  per phase, with the reason and the log's path. The phase stops holding the repo's landing lock, so other
  phases land there meanwhile.
- It is tried again five minutes later, at the queue's next look (any event, or
  the watchdog tick): main is merged in again, the quick test is asked again,
  and a command that passes is followed by the check. A failure whose cause is
  gone clears by itself; `swarm resolved` is not needed and does nothing here.

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
before releasing the queue. A premature call keeps the hold, records why, and
hands the hold to the Overseer.

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
whose worker reported while no supervisor ran. Kept work goes to the attic when
its row has been closed some other way (`swarm record <phase> done`, or a tick by
hand), logged as `LATER-CLOSED`: at once on the record, on the supervisor's next
sweep for a tick, and on `swarm up`. The row decides as committed on the target
branch, with no report about it still queued and no worker on it; a row that
still waits for its date is never touched. Work that is on main already is
dropped with no attic copy. `swarm up` does the same for a phase the run has
since recorded done (`swarm skip`), once its row no longer waits for a date. A
`blocked` or dateless finish goes to the attic as before: nothing says when, or
whether, that work is wanted.

[The integration flow diagram](../README.md#integrator-and-merge-conflict-resolver) is in the README.

**Owed pushes** (`pushowed.py`): later workers branch from local main, so a push
that failed does not hold anything back. The repo is recorded in `push_owed`.
Nothing waits on it and nothing is lost, so you are not asked: the debt is
folded into the Overseer's next summary, and the Overseer gets a pass for it
(above) and asks you if the fix is yours. The push is
retried after every integration and on the watchdog tick, and it clears as soon
as origin has local main, whoever pushed it: a push made by hand from the same
checkout is seen at the next tick, without waiting for the spaced retry. `swarm
status` and `swarm doctor` show the standing debt. A push the remote turns away
after the repo's check passed (another push held the branch for a moment) is
not a refusal: it is pushed again twice, and owed only if the remote still
turns it away, with git's own line as the reason.

**The post-merge command** (`[git].post_merge`, `repocmd.py`): a repo's
pre-push check can read things git does not hold. The usual one is the
dependencies installed beside the checkout: a phase that moves a pin installs
it in its own mirror, the merge brings only the manifest to the main checkout,
and the check then refuses every push until someone installs the pin there by
hand. A project names the command that someone would run, per repo, and the
integrator runs it in the main checkout between the merge and the push:

- `[git].post_merge_if` is asked first, at once and outside the build gate.
  Only when it exits 0 does anything run, so a merge that changes nothing the
  command cares about costs one quick test. One that cannot be run is a no
  (`POST-MERGE-IF-FAILED`).
- The command runs through the build gate, like the landing's lane check:
  queued unless it is light, on the build event log with the merged phase's
  name. The supervisor waits on it, so both its wait for a slot and its run are
  bounded by `[git].post_merge_timeout_s`. With no slot in that time it leaves
  the queue and nothing ran (a `left` event).
- Passed: `POST-MERGE <repo> ok queued=Ns ran=Ns`, and the push follows.
- Failed, timed out, no slot, or tracked files left changed (the next merge
  would be held as dirty for them): `POST-MERGE-FAILED <repo> <reason>`, no
  push is tried, and the repo owes a push with that reason, which is the
  command's own last lines. It is not a refusal, so the Overseer hears of it
  only if it is still owed two minutes later. The merge queue is never held.
- An owed push's retry runs the command again when the checkout sits cleanly
  on main, so a debt whose cause is gone clears by itself. A checkout you are
  working in is never touched, and neither is a push of the ledger writer's
  own commit.
- A repo with a command lands after the component repos without one: what it
  installs usually comes from a sibling, and a phase that changed both has then
  landed the sibling first.

The output of a repo's last run is `<state>/logs/post-merge.<repo>.log`.

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
a tmux session this swarm created, which is one marked with its state dir
(`@swarm_state_dir`): a session of the same name that carries another swarm's
mark, or none, is left alone and said to be. It signals the
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
  it serves and `swarm keep --stop <name>`. When you have to open what the
  operator kept, its outcome carries an `--ask`.

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
  - A cache outlives the mirrors that build into it, and a Rust test compiles in
    paths that cargo spells through the worktree it ran in and never rebuilds
    for: `env!("CARGO_BIN_EXE_<name>")` (the binary it starts) and
    `env!("CARGO_TARGET_TMPDIR")`, both under `target/`, and
    `env!("CARGO_MANIFEST_DIR")` (where it finds its fixtures). Left alone, the
    next mirror runs a test that starts its binary, or reads its fixtures,
    through the mirror that built it, and every such test fails once that mirror
    is removed.
  - So making a mirror writes `<state>/.cargo/config.toml`, which sets
    `build.rustc-wrapper` to `<state>/.cargo/rustc-wrap`. cargo reads the config
    of every directory above the one it runs in, so this reaches every build in
    every mirror, started in a repo or at the mirror root with `--manifest-path`,
    and puts no file in any checkout. The wrapper rewrites the two paths under
    `target/` to their real path, the cache itself: the same string in every
    mirror, and one that stays. It changes nothing else, costs a few
    milliseconds per compiled crate, and adding or removing it rebuilds nothing.
  - The wrapper is a short Python script: under `/usr/bin/python3` where that
    runs it, else under the interpreter the swarm itself runs on. It is tried
    each time a mirror is made, and a new one before it replaces the old. While
    none works the config is removed and the log says `RUSTC-WRAP-SKIP`: builds
    then run without it, as before. Should the interpreter it names go away in
    between, builds under the state dir fail to start their compiler until the
    next mirror is made, or until you delete `<state>/.cargo/config.toml`.
  - It does not reach a build with `RUSTC_WRAPPER` or `CARGO_BUILD_RUSTC_WRAPPER`
    set in its environment (they win over any config; empty turns the wrapper
    off), or one started outside the state dir. Inside the state dir it replaces
    a `build.rustc-wrapper` from your own `~/.cargo/config.toml` (`sccache`).
    With `[build].cache = false` the next mirror made removes the config, and
    each worktree has a `target/` of its own.
  - The sources' path is the worktree's own, so no wrapper can make it last, and
    an executable already in a cache keeps the paths it was built with. So when
    a mirror is made, resumed or removed, each cache is swept: an executable
    whose dep-info (`deps/<name>-<hash>.d`) records one of the three paths
    through a directory that no longer exists is deleted, and cargo rebuilds it
    on its next run (`TARGET-CACHE-STALE <repo> dropped N …` in the log). One
    built through a mirror that still exists is left alone: it works, and may be
    running.
  - What this does not cure: a shared cache still holds whatever was built
    last. If another mirror built the same crate after your last edit, its
    artifacts are newer than your sources and cargo runs them, and a test that
    reads fixtures by `CARGO_MANIFEST_DIR` reads that mirror's while it exists.
    Before a run you will report, touch the files you changed (or
    `cargo clean -p <crate>`).

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
at most `max_concurrent` heavy builds run at once on the machine; the rest wait
their turn. For `cargo` it also sets `CARGO_BUILD_JOBS` to the project's
`[build].jobs`.

**One gate for the machine.** The limit is the machine's, not a swarm's. With a
gate per swarm, three swarms that each set `max_concurrent = 1` ran three heavy
builds at once, which on a machine whose disk is what builds strain is exactly
what the limit is there to prevent. So there is one gate, and every swarm on the
machine queues at it.

- *Where it lives.* Its files (seats, slots, the queue and its lock,
  `idle.json`, `pair.json`, `gc`, `events.jsonl`) are in the machine directory,
  `<state root>/machine/buildsem/`. The state root is
  `$XDG_STATE_HOME/swarm-orchestrator`, where each swarm's own state dir also
  sits (see [Several swarms on one machine](#several-swarms-on-one-machine-machinepy)).
  Below, `buildsem/` is that directory.
- *Whose limits.* `max_concurrent`, `short_s`, `overtake`, `idle_yield_s`,
  `idle_yield_max`, `pair` and `alone` are `[build]` in `machine.toml`
  ([config.md](config.md#the-machine-file-machinetoml)), and wherever this
  section names one of them, that is the table meant. None has an environment
  variable, and a worker's environment carries none of them (it still carries
  `SWARM_BUILD_JOBS` and `CARGO_BUILD_JOBS`). The file is read through each
  time a limit is looked at, so an edit applies to every swarm at once, with no
  reload or restart. A mistyped key or value is an error that stops every
  new command (a process that had read the sound file keeps what it read until
  the file is fixed); nothing falls back to a default in silence.
- *What stays a project's.* `[build]` in `.swarm.toml` keeps `jobs`, `cache`,
  `heavy` and `light`. A `.swarm.toml` that still sets one of the machine's
  keys does not load, and the error names the key and `machine.toml`.
- *Whose build.* Every ticket, seat record and event names its swarm: `swarm`
  (the slug: the state dir's name, as `swarm ls` lists it) and `swarm_name`.
- *Coming from a gate per swarm.* A `swarm build` started by a version whose
  gate was the swarm's own holds its locks in that swarm's
  `<state>/buildsem/`, which nothing reads any more: the machine's gate does
  not see it, and it does not see the machine's. Let such builds end, or stop
  the swarm, before the first build on this version. The limits that were in
  each `.swarm.toml` go into `machine.toml` once (a project file that still
  has one does not load), and the old `<state>/buildsem/` directories can be
  deleted: the run-time history in their logs is not carried over, so the
  first builds are predicted from the defaults.

**Slots and seats.** A build holds two `flock`s, both on descriptors it
inherits, so its whole process tree holds them: however the build ends (exit,
crash, SIGKILL, a tool timeout), they free when its last process is gone. There
is no daemon and no counter to leak.

- A **seat**, `buildsem/seat<K>`, held exclusively: one per build alive.
  There are `max_concurrent + idle_yield_max` of them, so the kernel caps the
  builds the gate can have alive at once. The seat file holds a small record of
  its build (id, swarm, phase, pid, command, start), which is what `--status`
  and the waiting line show.
- A **slot**, `buildsem/slot<N>` (`N < max_concurrent`), held *shared*. It
  is a lock and nothing else. gc takes every slot exclusively while it sweeps,
  and so waits for every build on them; while it holds them no build starts.

A build may start when fewer than `max_concurrent` builds *count* (alive and not
set aside as idle, see below), a seat is free, and some slot has no counted
build on it.

**A process a build left behind is not a build.** When a build's own `swarm
build` sees its command end, it marks the seat's record `over`. Whatever still
holds that seat afterwards is a process the command started and did not wait
for (a test's server, a gated `ffmpeg`, a daemon), and nobody knows when it
will end. It counts on no slot, is not measured for idle yield, and does not
use up a seat: the next build takes the next seat number, so such processes,
however many, never fill the gate. Only gc still waits for one, since it may be
using build output. (They used to count until set aside as idle, at most
`idle_yield_max` of them, and to keep their seats. On 2026-10-07 three of them,
left by a failing test, were the three seats of `max_concurrent = 2` plus
`idle_yield_max = 1`: no build of either swarm on the machine started for 72
minutes, until the session that started them ended.)

**The queue.** A heavy command takes a ticket, `buildsem/queue/<seq>-<id>.json`,
numbered under `queue.lock` and locked by its waiter for as long as it waits.
A ticket whose lock can be taken belongs to a dead waiter and is deleted, so a
killed waiter never blocks anyone. Only the waiter whose turn it is tries for a
free slot (under `queue.lock`), so arrivals are served in order; with several
slots, the next free one goes to the next in line. A waiter that stops polling
(a stopped process) is passed over until it polls again.

**Short builds first, boundedly.** A command whose recent runs took at most
`short_s` (the median of its last runs in the same place, from the log below)
may start ahead of older waiters predicted to be long. Each long waiter counts
the times it is passed (`queue.json`), and once it has been passed `overtake`
times nothing more may go ahead of it. So a waiter starts
after at most the waiters older than it plus `overtake` short ones: nobody
starves, and a 30-second targeted test does not sit behind a 15-minute browser
suite. Unknown commands are never "short". `overtake = 0` is plain FIFO.

**Fairness between swarms.** The queue knows no swarm and gives none a
priority. Arrivals are served first come, first served, whoever they belong
to. A build predicted short may pass a long one, and each waiter is passed at
most `overtake` times in all, whichever swarms the passing builds belong to.
So one swarm with many builds queued cannot starve another: its builds stand
in the same line, and no waiter of any swarm starts later than the waiters
older than it plus `overtake` younger ones. The predictions are each swarm's
own. Whether a command is short, and its ETA, come from that swarm's past
builds in the shared log, because a command and the place it ran in mean
something only inside one project. What to assume for a command never seen
comes from every build on the machine.

**Idle yield.** A command can hold a slot and do nothing: a script waiting out
a 20-minute timeout, a test runner waiting on a server that never comes up.
Such a holder is never stopped or signalled; it ends when it ends. Instead it is
*set aside*: once its whole process tree has been quiet for `idle_yield_s`
(default 150) it stops counting against `max_concurrent`, and the next waiter
starts beside it, on the same slot.

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
- *The caps.* At most `idle_yield_max` (default 2) holders are set aside at
  once; a further idle holder keeps counting and the queue waits, as it does
  with `idle_yield_s = 0`. The seats bound the builds alive at
  `max_concurrent + idle_yield_max` whatever happens (a process a build left
  behind is none of them, see *Slots and seats*).
- *Who measures.* The waiters, from `/proc`, under `queue.lock`, keeping the
  running figures in `buildsem/idle.json`. It needs no supervisor and no daemon:
  when nobody waits, nobody needs the answer. The holders are the machine's, so
  a waiter of one swarm measures the builds of every other. The counters compared are the
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

**A holder that is gone, and one that stands frozen.** The gate trusts a lock
over a record. With every swarm on one gate, a holder that will never come
back and one that will must not be taken for each other.

- *Gone.* A seat is a kernel lock. A holder that died, with its whole swarm or
  without, holds none: its seat is free the moment its last process is gone,
  whichever swarm looks, and the first to notice writes the `end` it never
  wrote.
- *Frozen.* A build whose swarm is frozen (`swarm freeze`) is alive and keeps
  its seat: it will resume, and nothing takes a seat from it. What it stops
  doing is keeping the machine waiting. It is set aside as idle at the first
  quiet sample instead of after `idle_yield_s` (a `yield` event whose `why` is
  `its swarm is frozen`), so the next waiter starts beside it. It counts toward
  `idle_yield_max`, and it counts again when `swarm thaw` wakes it. Under the
  pairing rules it keeps its repo and, if it runs alone, the gate.
- *Frozen and not set aside.* With `idle_yield_max = 0`, or for a build that
  never yields (`--hold`, a daemon's client), a frozen build keeps its slot
  until its swarm is thawed or stopped. `--status` marks it
  `frozen with its swarm`.
- *Frozen waiters.* A frozen swarm's waiters keep their tickets and their
  places. The other swarms pass them over until they poll again. Inside the one
  frozen swarm the order stands still: only the time its run was awake counts
  toward a ticket going stale, so the first of its waiters to wake does not
  pass the rest.
- *The freeze itself.* `swarm freeze` takes the machine's queue lock while it
  freezes and keeps it until every group says it is frozen, so no process is
  frozen holding the lock that every swarm's builds need.

**Pairing rules.** `max_concurrent = 2` on its own lets any two heavy builds
run side by side. Where the disk is what builds strain (two builds can be fine,
two builds in one tree or a build beside an image build are not), set
`pair = "distinct-repo"` and the gate also checks *which* builds are alive
before it starts one. `"any"`, the default, checks nothing.

- *Never two builds in one repository.* A build starts only if it shares no
  repo with any build alive. Its repo is the one its working directory is in.
  The rules know a repository by its place on the machine: a phase's mirror has
  the project's layout, so `<state>/wt/<phase>/lib` stands for the project's
  own checkout at `lib`, for every phase; any other checkout is traced through
  its git common directory, so every worktree of a repo is that repo. What a
  waiter is told, and what the log records, is the name its own swarm has for
  the repo: its path inside the project (`lib`; `.` for the project's own
  repo), which is its lane and the directory of its shared build cache
  (`[build].cache`); the lane, for a repo outside the project that
  `[lanes].external` declares; else its path. The
  repos the command itself names count too (a `cd` target, a
  `--manifest-path`, a `-C` directory, a script's own place: what pre-flight
  checks): `cargo test --manifest-path lib/Cargo.toml` from the project root
  builds in `.` and in `lib`. What a script does once it runs is not seen.
- *Across swarms.* The rules hold between the builds of every swarm on the
  machine, as the gate does. A repository's place is the one thing two swarms
  agree on, so two swarms whose projects hold the same checkout (an umbrella
  project and one of its components) never build in it side by side, and two
  projects are not taken for one because each calls its own repo `.`. A build
  that runs alone waits for every swarm's builds to end, and no swarm's build
  starts while it runs.
- *Some builds run alone*, both ways: such a build waits until no other build
  is alive, and nothing starts while it runs. They are:
  - a command `alone` names. A pattern is a command prefix whose words are
    globs, like a project's `heavy` and `light`, matched against every simple command
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
     A script known to build images belongs in `alone`
     (`alone = ["docker", …, "bash ci/bake.sh"]`).
- *Who counts.* Every build alive on a seat, whichever swarm's, including one
  set aside as idle (a build whose swarm is frozen among them).
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
  first*: each waiter is passed at most `overtake` times in all, whatever the
  reason, and once it has been, nothing starts before it. A
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

- on joining, and every 45 s while queued: its place among the waiters of
  every swarm, who is ahead, who holds each slot (phase, command, how long, how
  long it usually takes) and an estimated start, with the swarm's name in front
  of a build that is another swarm's:
  ``queued 12s — #3 of 4 for 2 slot(s) on this machine, behind [glasheim] W1
  `cargo build`, W7 `cargo test`; slot 0: …``;
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
20 MB. It is the machine's: every swarm's builds are in it, and each line names
its swarm. Every line has exactly these keys:

```json
{"ts": 1790000000.123, "swarm": "app-3f9c2a1b", "swarm_name": "app",
 "event": "start", "id": "3f2a9c01be44", "phase": "P-1",
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
| `yield` | a running build was set aside as idle; it keeps running | as in its `start` | `idle_s` = how long its tree was quiet; `run_s` = how long it had run; `why` = `its swarm is frozen` when that, and not a full `idle_yield_s`, is why |
| `unyield` | a set-aside build is working again and counts again | as in its `start` | `idle_s` = how long it was set aside; `why` = what the measurement saw (`it is using CPU again`) |
| `passed` | a pairing rule held this waiter back and a younger one started ahead of it | the waiting `swarm build` | `why` = the rule (`same repo as slot 0 (lib)`); `by` = the `id` of the build that started |
| `alone` | a running build was found to hold a command that runs alone; nothing starts beside it from now on | as in its `start` | `why` = what was seen; `run_s` = how long it had run |
| `left` | a gc's wait for the gate ran out and it left the queue; it held nothing. With `cls` `heavy`: a build the supervisor runs itself (a `[git].post_merge` command) got no slot in the time it may wait, and nothing ran | the process gc runs in; the supervisor | `wait_s` = how long it waited; `why` = what was still alive |

`cls` is `heavy` for a queued build, `light` for a command that skipped the
gate, and `gc` for gc's own turn at the gate (see *gc and the gate* below):
`queued` when it joins the queue, `start` when it holds every slot (`slot` is
null, `wait_s` how long it queued), `end` when it lets go (`run_s` is how long
no build could run; `exit` 0, or 1 if the sweep failed), or `left` instead of a
`start`. `pid` is the process gc runs in (the supervisor, for the automatic
one), `swarm` the swarm whose build output it sweeps, `alone` is true. A gc is
not a build: the run-time history and the resource sampler skip these lines.

`swarm` is the slug of the swarm the build belongs to (its state dir's name,
unique on the machine, as `swarm ls` lists it) and `swarm_name` what its owner
calls it, on every event of that build, whoever wrote the line: a `yield` is
written by whichever waiter measured the holder, often another swarm's. Both
are null only for a holder whose record could not be read.

`phase` is `$SWARM_PHASE` (null outside a worker); `argv` is at most 300
characters. `repo` is the repository a queued build works in, by its own
swarm's name for it (its path in that swarm's project; see *Pairing rules*;
logged under `pair = "any"` too), on every event of that build; it is null when the working directory is in no git
checkout, and for a light command. `yield` and `unyield` carry the `id`, `pid` and `slot` of the build
they are about and are written by whoever measured it; a build that ends while
set aside gets no `unyield` (its `end` closes the stretch), and a `yield` after
a build's `end` was a process that build left behind, set aside in its turn,
before such a process stopped counting at all (*Slots and seats*). A
`queued` with no `start` gave up while waiting. A `start` whose
`end` never came and whose `pid` is gone died unrecorded: the gate writes a
synthetic `end` with `exit` null as soon as it notices (when a waiter reports,
or when the slot is next taken).

**gc and the gate.** gc deletes build output, so it holds every build slot
exclusively while it works, and the kernel grants that only while no build is
alive on any of them: set aside as idle or not, started by an older `swarm
build` or not, a process a build left behind included. The slots are the
machine's, so that is no build of any swarm: a gc deletes only its own swarm's
build output, but the disk the builds strain is one. It gets there through
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
  back only for builds at work: while a holder set aside as idle, one whose
  swarm is frozen, a process a build left behind, or another gc is in the way,
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
`swarm status` and `swarm doctor` carry a one-line summary. All three show
every swarm's builds, since the gate is the machine's
(`build gate: 2 slot(s) on this machine, 1 busy, …`), and put the swarm's name
in front of a build, a waiter or a gc that is another swarm's:
``slot 0: [glasheim] W3 `cargo nextest run` running 4m``. A holder whose swarm
is frozen reads `frozen with its swarm`. In `--json` every holder, waiter, gc
and finished call carries `swarm`, `swarm_name` and `mine` (is it the swarm
that asked), a holder also `frozen`, and the top level carries the `swarm` and
`swarm_name` of the swarm that asked. Holders that were
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
back says `held back: gc runs first`. A slot shown as `busy, no record` is a
gc in the moment it takes the gate or lets it go. Under the pairing rules
each holder's line ends with its repo and, if so, `runs alone (why)`, a waiter
the rules hold back says `held back: …`, and `--json` carries `pair`, `alone`
(the patterns), `repo`/`alone` on each holder and `repo`/`alone`/`blocked` on
each waiter.

Workers are told to wrap their gates in it (`swarm build cargo nextest run`), and
to give those commands a generous timeout, since they may queue. The landing's
lane check and a repo's `[git].post_merge` command queue like any other build,
and like any other build they do not queue when the command is light: by the
built-in rules, or because the project names it in `[build].light`. That is
for a check that compiles nothing (a lint-only gate that takes half a minute
should not wait ten behind a compile); its log starts with `# light command
(...): not queued for a build slot`, and the build event log records it as a
`bypass`.

**Sizing.** `max_concurrent` describes the machine, and a project's
`[build].jobs` is its share of the cores for one build. Derive them from the
host's cores, memory and disk (one build's peak memory times `max_concurrent`
must fit with room to spare; each project's `jobs` times `max_concurrent`
should not exceed the cores, and the other swarms' `jobs` count toward the
same total), never copy them from another machine's files.

## Resource tracking (`swarm resources`)

Whether the machine's `max_concurrent` (`machine.toml`), or a project's
`[build].jobs` or `[swarm].max_workers`, can go up is a question about what a
build and a worker actually take on this host.
The supervisor answers it with a sampler thread of its own
(`resources/sampler.py`), started with the loop and stopped with it. It only
reads `/proc`, the state dir and the build gate's files in the machine
directory, and writes under `<state>/meters/`.

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
  gate's event log, `buildsem/events.jsonl` in the machine directory, which
  holds every swarm's builds (`queued`, `start` with the build's
  pid, `end` with its run time and exit code, `yield`/`unyield` when the gate
  sets an idle build aside or counts it again; a `start` whose process has been
  gone for five seconds counts as ended, since a SIGKILLed build writes no
  `end`, while a build that just exited is waited for, because the gate writes
  its `end`, with the exit code, a moment after the process is gone). Without that log,
  the slot file's `flock` holder, read from `/proc/locks`, is the build. Each
  sample sums over the build's process tree: CPU seconds (live processes'
  `utime+stime+cutime+cstime`, which counts reaped compiler processes once),
  anon and total RSS, and storage IO from `/proc/<pid>/io`. The gate is the
  machine's, so every swarm's builds are in that log and every one is measured:
  that is what takes a neighbour's build out of "everything else on the host".
  Each build carries `swarm`, `swarm_name` and `mine` (is it this swarm's). A
  sample row keeps `nb` (builds alive on the machine) and `b` (each one's
  cores and anon memory, by id) and adds `bo`, the ids among them that are
  another swarm's (absent when there is none). A build nobody names (found by
  its lock alone) counts as this swarm's, so that some supervisor reports it.
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
  `yielded_s` (how long the gate had it set aside as idle), and whose it was:
  `swarm`, `swarm_name`, `mine`. The rows are every swarm's builds on this
  machine, each swarm's sampler keeping its own copy.
  Byte caps (48, 32 and 8 MiB) win over the age limits. `<state>/resources-now.json`
  holds the latest sample, what is running and the sampler's own cost: the
  builds on the machine's gate (each with `swarm`, `swarm_name`, `mine`), how
  many wait (`queued`, of which `queued_mine` are this swarm's), and this
  swarm's sessions and idle holders.
- **Idle holders.** A heavy build of this swarm that holds a slot for `[resources].idle_s`
  (default 600) with its whole tree under 1% of a core shows in `swarm status`,
  as a `swarm doctor` WARN, in the dashboard's resources box, and is recorded
  for the Overseer's next summary (again hourly while it stays idle). The report says what the gate did about
  it: its slot was released (the gate set it aside, so builds start beside it),
  or it was kept and why (a command that never yields). This is a report only;
  setting a holder aside is the gate's own doing, by its own measurement, long
  before this warning. The gate's own holder record (`seatK` in the machine's
  `buildsem/`) confirms it first: a
  record saying the build ended, or seats that name only other builds, means the sampler
  missed an `end` and nothing is reported; a matching one supplies the phase
  and command. Nothing is killed. Another swarm's idle build is that swarm's
  supervisor's to report: here it is only shown as idle, under its swarm's
  name, or the owner would hear of one build once per swarm.
- **Cost.** The thread's CPU time (`time.thread_time`) and `du`'s (from
  `wait4`) are published in the snapshot, with the bytes written per day. On a
  24-core host with a few hundred processes a full sample costs about 5 ms, so
  1 s sampling is about half a percent of one core while building.
- **`swarm resources`** prints now (host, each build on the machine's gate,
  each session of this swarm; a build the gate has set aside reads
  `YIELDED 12m`, and another swarm's build has that swarm's name in front of
  its phase, `[glasheim] W3`), the last day as sparklines, the
  finished builds with the worst peaks (with a `yielded` column), the builds
  that sat idle longest and how long in all, and a capacity
  section: p95 per heavy build and per worker, and what 2 concurrent builds, 8
  workers or doubled jobs would have needed against this host's RAM (85% of it,
  the rest left to page cache and the kernel) and cores, with the arithmetic shown
  and the data called thin under 5 builds or an hour of worker samples. `--json`
  for scripts.
- **Whose load.** The capacity arithmetic is about the machine's gate, so its
  builds are builds of any swarm: the build figure comes from every measured
  build, a neighbour's included (the section says how many were), and
  `max_concurrent` is the machine's limit. The workers are this swarm's, and
  `jobs` and `max_workers` this project's settings. "Everything else on the
  host" holds no swarm's builds, since each sample's builds are taken out of
  it whoever they belong to, and it does hold the other swarms' sessions. So
  every swarm on the machine reaches the same answer to "would another build
  fit", from the same builds.

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
middle register between finishing quietly and stopping to ask. A note messages
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
[Telegram](#telegram-and-asking-the-owner)) answers with (`usage.brief`, then
`tgbot.usage_text`):

```
Weekly 41%, resets Wed 11:00.
5-hour 23%, resets 16:00.
Read at 14:05, 3 min ago.
Every running swarm pauses at weekly 60% and 5-hour 90%, and stops at weekly 70%.
```

The figures are the account's, so they are read from every swarm's samples
together and said once. The lines after them are the caps, and each swarm a cap
holds. A sample arrives only when some session renders its status line, so the
answer says how old it is.
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
- **a limit reset inside its window:** when an account's limit is reset before
  the window ends, the window keeps its reset time and the figure falls to zero.
  From a status line that looks like a lagging reading, so the supervisor asks
  the endpoint whenever every fresh reading sits well under the window's
  highest. The endpoint reading 5 points or more under it, same reset, is the
  reset: the charts drop there, usage counts from it, a hold lifts, and an
  override or a fired `down` from before it is dropped, so the cap acts again
  if the window fills a second time. Status lines that still show the figures
  from before are ignored.
- **down:** runs `swarm down`, once per window of each account. The swarm stays
  down.

A stale or missing reading never creates a hold and never lifts one. A pause
and its lifting are recorded for the Overseer's next summary; a stop asks you,
because the swarm stays down until you start it. The hold and the reading show on
the TUI, the web board, `swarm status`, `swarm why` and `swarm doctor`
(`usage.caps`). Hold, lift and endpoint events are logged as `USAGE-*`.

## Doctor, why and gc

**`swarm doctor [--json]`** answers "what is wrong right now?". It is read-only,
and exits 1 if any check FAILs. It checks:

- **supervisor:** pid alive, FIFO has a reader, no stray second supervisor (the fix
  names the pid that holds this swarm's FIFO, never every swarm's supervisor);
- **slots:** busy panes run `claude`; a busy slot with no edits or commits
  20 minutes after launch (the lost-prompt signature);
- **run:** frozen (`run.frozen`, a WARN for as long as it lasts: since when,
  how many groups, and that `swarm thaw` ends it), watchdog (which dead slots
  it will free, and what frees the others), finished with ready work, free
  slots beside ready phases (a phase waiting to be retried after a failed
  launch is named, not offered; one given up on is named as that), no event
  for 90 minutes. No grace, age or stall counts the time the run stood frozen;
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
- ready, with what is stopping it (frozen, paused, or no free slot).

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
- **Safety:** it holds every slot of the machine's build gate while it deletes,
  so no build of any swarm runs during the sweep. It gets them by waiting its
  turn in the build queue (see *gc and the gate*), refuses while a compiler
  runs in a tree it would touch (unless `--force`), and re-checks every path at
  delete time against a protected list.
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
  `swarm console` starts a new one; while it runs they only move you there, so
  there is never a second one. A missing window is recreated after `dash`.
- **A fresh conversation every time.** No start passes `--resume`, `--continue`
  or `--session-id`, and the swarm keeps no conversation id: after `/exit`,
  after `swarm up`, after a dead keeper, the console opens empty. Earlier ones
  stay in Claude Code's own history as `swarm · console`; `/resume` inside the
  console brings one back. A primer edit reaches the next start.
- **Not a worker.** It carries no phase marker and no `SWARM_SESSION_ID`, even if
  the tmux server's environment does, so no `Stop`-hook recap, slot, ETA,
  per-phase usage or session reaper counts it, and its pane has no `@swarm_slot`
  tag for a watchdog to look at. It carries the run's `SWARM_STATE_DIR` and
  `SWARM_PROJECT`, so its `swarm` commands find the run from any folder and
  `swarm down` ends it; the next `swarm up` opens a fresh one. A command typed
  there for another project is refused, like in any session.

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

The status bar ends with where this swarm's page on the web board (below) is,
or with why the board is not there; `u` copies that address. The dashboard does
not serve the board: it is the machine's, a process of its own.

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
     space, and each build running on the machine's gate (an idle holder of
     this swarm in red, or in yellow once the gate has released its slot;
     another swarm's build under that swarm's name, muted). History and
     capacity are `swarm resources`;
   - **alerts & notifications:** what needs you (`◆`), what is wrong now (the
     footer's list, and any warning or failure from the last doctor run), then
     every ping newest first: `✓` delivered, `·` held, `✗` never arrived.
     `enter` opens one; `x` acknowledges the ones that never arrived;
   - **shells:** what `swarm keep` left running.

   Under 110 columns the right column moves under the feed and needs you
   returns as a strip at the top.
2. **workers:** one row per slot. A busy slot whose pane died shows `gone`.
   A parked worker, still asking you or at work on your answer, is in no slot:
   it has a row of its own after the slots, as it has in home's working now
   box (which shows every one of them under its slots, with its `wait:<phase>`
   window). The row and its detail read like a slot's worker's, from the pane
   in that window: `waiting` while it asks you, else what `claude agents` says,
   its elapsed time, eta, context, git counts and `swarm/<phase>` branch, and in
   the detail its pane, window, worktree, branch work and last lines. If its
   session is gone (its window closed, or nothing of it left running: the rule
   the watchdog's sweep settles it by) the row says `GONE` like a slot's, and
   its detail says what is missing and whether the running supervisor's sweep
   settles it. The detail's border names the selected row: `slot`, or
   `parked worker` for one that holds none.
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

A read-only board for a phone or a browser, **one for the machine**: one
process, one port and one address, whatever the number of swarms. `/` is the
overview of every swarm and `/s/<slug>/` is one swarm's board, where `<slug>` is
the swarm's state dir name (what `swarm ls --json` lists it by; a rename of the
swarm does not move it). `swarm up`, `swarm status`, `swarm doctor` and
`swarm ls` print the address: the machine's Tailscale IP (`tailscale ip -4`), or
its LAN addresses when Tailscale is not running. `up`, `status` and `doctor`
also print the swarm's own page.

**Who runs it.** Nobody has to. It is a detached process of its own, not the
dashboard's and not a tmux window:

- Any `swarm up` starts it when it is not already answering (`/healthz`), and
  does nothing when it is: a second swarm's `up` finds the first one's board.
- The `swarm down` of the last swarm that is up stops it. While another swarm
  is up it stays, and the swarm that went down is still on it, marked as down.
- `swarm web` does the same by hand: `swarm web` (or `swarm web start`) starts
  it unless it is running and prints its address and every swarm's page,
  `swarm web stop` stops it, `swarm web status` only says, and
  `swarm web serve` runs it in the foreground. It needs no project.
- `swarm restart` starts it again with the supervisor, so it runs the code on
  disk. Its pages find it again by themselves.

It stops with the last swarm, instead of serving on until `swarm web stop`, so
that after you have taken everything down nothing of the tool still holds a port
open to the network, and so that a board never outlives the code it was started
with. A run that finishes by itself is not a `down`: its board stays and shows
how it ended. To read swarms that are down, `swarm web` starts it over them.

Its pid file, log and lock are `web.pid`, `web.log` and `web.lock` in the
machine directory (`<state root>/machine/`). It is started with no swarm's
environment (no `SWARM_*` variable), so nothing typed for one project decides
what it shows for all of them, and it reads each swarm on the settings that
swarm's supervisor last recorded (`<state>/config.json`): an edit of
`.swarm.toml` shows once the swarm has taken it (`swarm reload`, or its next
`swarm up`). A board that starts while the previous one is still closing waits
up to 5 s for the port instead of failing.

**Where it listens** is `[web] host` and `port` in
[`machine.toml`](config.md#the-machine-file-machinetoml), not in any project's
file. When the port is held by something else, `swarm up`, `swarm status`,
`swarm doctor` and `swarm ls` say what holds it (the program and its pid; or
which swarm's board, for one started before boards were one per machine; or
which state root's) and what to change. Nothing is started against a taken
port.

**Which swarms it shows.** Every state dir under the state root whose project
folder still exists and whose `[web] enabled` is on, running or not. A state
dir no supervisor ever ran in, one whose project is gone and one that opted out
are not shown; the overview's last line counts them, and `swarm ls` lists them.
It looks again every few seconds while a page is open, so a swarm that comes
up or goes shows without a restart.

**The overview (`/`)**, in reading order:

- **What waits for you**, across every swarm: the questions (a session asks and
  waits for your answer; newest first) and the to-dos (what `swarm todo`
  lists), each naming its swarm and opening that swarm's page on the row. When
  nothing waits it says so, and how the swarms stand.
- **Every swarm together:** phases built, building and to go; worker slots busy;
  the builds running and waiting at the machine's one build gate, and whose
  they are ("atlas 1", "1 waiting: borealis 1"); the account's 5-hour and
  weekly usage, from whichever swarm read it last.
- **A card per swarm:** its name, its standing in a plain word (running, paused,
  frozen, held on a usage limit, finished, down), its progress and likely
  finish, what it is building now, how many things wait for you there, and a
  button into its board. The swarms that are up come first.

Every swarm page carries the same swarms as buttons along its top, each with
its count of what waits for you, and the way back to the overview. A swarm that
is not up says so under its header; its page shows how it stood. Both pages
follow the device between a dark and a light scheme.

**A swarm's tabs:**

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
  phase. The gate is the machine's, so the running builds, the queue and the
  finished builds are every swarm's: another swarm's build is named with that
  swarm in front of its phase and opens in that swarm's page, and one whose
  swarm is frozen says so. *Capacity*: the scenarios `swarm resources` works out (as configured,
  2 builds, 8 workers, both, more jobs) with their arithmetic and its notes
  (too little data, pressure already seen during builds). The Overview carries
  one line of it.
- **Activity:** recent finishes with their recaps, notifications, Overseer
  passes, and what `swarm keep` left running.
- **Row sheet:** the ledger row, recap, notes, dependencies, operator jobs and
  attempts. Deep links use `#<tab>&phase=<id>` (the old `#phase=<id>` still works).

Endpoints. For the machine: `/api/machine` (the overview's data, and every
page's swarm buttons) and `/healthz`. For one swarm, all under `/s/<slug>/`:
`api/board`, `api/graph?mode=open|all&book=<name>`, `api/usage`,
`api/phase/<id>`, `api/search?q=`, `api/resources`,
`api/resources/history?window=1h|6h|24h|7d|30d`,
`api/resources/builds?window=&sort=&dir=&phase=&limit=`,
`api/resources/capacity`. All are gzip + ETag. A page polls them only while it
is visible, only the tab on screen asks for its data, and an unchanged answer
is a 304. (`/s/<slug>/events`, Server-Sent Events, is still served.) A slug no
swarm here has is a 404, and a swarm's routes serve that swarm only: the slug
picks one of the swarms the board found, and is never joined into a path.

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

It is plain `http.server`, GET and HEAD only, and no URL path ever maps to a file:
each page is one self-contained document with no external host in it. Every
payload is scrubbed of credential-shaped strings. It listens on every interface
**with no token**, by the owner's choice (`[web].host = "0.0.0.0"` in
`machine.toml`). `/healthz` answers `{"app": "swarm-web", "machine": <state
root>, "pid": …}`, which is how `swarm up` tells this machine's board from
another program holding the port, and from the board of another state root.

Tailscale inside WSL needs nothing more. Without it, under WSL with mirrored
networking, a phone on the LAN reaches the board only once Windows lets the port in:

```powershell
New-NetFirewallRule -DisplayName "swarm web" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow
```

`8765` is the default; use the port `[web].port` in `machine.toml` names. It is
one rule for the machine: every swarm is behind that one port.

If it still does not answer, also run
`Set-NetFirewallHyperVVMSetting -Name '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -DefaultInboundAction Allow`.
Both need an elevated PowerShell.

## Telegram and asking the owner

The machine has one bot, and every swarm on it sends through it. The sender is
`scripts/notify.sh` by default; it reads `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID` from this repo's `.env`, which is gitignored;
`scripts/resolve-chat-id.sh` fills in the chat id. `[telegram]` in
[`machine.toml`](config.md#telegram-1) points `notify` at any other script that
takes the message as `$1`, and `env` at another credentials file; the swarm
hands the script that file as `SWARM_TG_ENV`. A project's `.swarm.toml` never
names the bot: one that still sets `[telegram].notify` does not load.

**One sender, two kinds of message.** Everything the swarm sends goes through
`telegram.py`, which names the swarm at the front of every message, once, so
several swarms can share one bot and one chat. Only two kinds of message ever
reach your phone:

- **An ask:** `[<swarm>] Asks you: <what you must do or decide, and why>`. Sent
  when the swarm, one phase or the operator cannot move forward until you do or
  decide something.
- **The Overseer's summary:** `[<swarm>] Overseer: <what landed and what is
  running since the last summary; whether anything waits on you>`. Sent on a
  clock, every `[overseer].every_s` (4 hours by default), and once more when the
  run ends. There is no summary per number of finished phases, and none for a
  stretch in which the swarm stood still.

Both are read in a phone notification, so the whole message, prefix included,
is at most **280 characters** and its first sentence carries the point. 280 is
two plain sentences of about twenty words each plus the prefix: what you take
in at a glance, and no more than you said you read. A notification banner
shows only the start of a message, which is why the ask comes first and the
reason second. Messages are plain text.

Everything else the swarm has to say is **held back**: written to
`<state>/notifications.jsonl` and never sent. A held-back row is either
*folded* (the next summary accounts for it: the Overseer's digest lists what
was folded since the last summary) or *log only*. The dashboard's alerts tab
reads the same log and shows a held-back row as `·` with the reason, a sent one
as `✓` and one that failed to send as `✗`. Only a failed send is a drop:
`swarm doctor` counts those, and `swarm notify --ack` (or `x` on home or the
alerts tab) acknowledges the drops so far without touching the log; the footer,
the drawer and `swarm doctor` then count only later ones, and doctor only fails
on a drop that is recent or on sends that are still failing.

**A session's words are a short field, never a cut.** Where an ask or the
summary is written by a session, the command that takes it refuses text that
does not fit, before it records anything, and says how long it may be and to
rewrite it. Nothing is ever truncated to fit: half a recap explains nothing.
The long form stays where it was (the recap, the operator outcome, the pass
record) and shows on the board and in `swarm todo`.

| command | the short field | what stays long |
|---|---|---|
| `swarm waiting <who> "<ask>"` | the ask itself | the question in full, asked in the session's own pane |
| `swarm notify "<ask>"` | the ask itself | the session's record or recap |
| `swarm operator-done <job> "<outcome>" --ask "<ask>"` | `--ask` | the outcome, on the job, the board and `swarm todo` |
| `swarm overseer-summary "<summary>"` | the summary | the pass record |

`swarm done` takes no ask and is never refused over one: a finish is recorded
first, whatever else happens. The asks that come out of a finish (a failure
nothing will retry, a follow-up with the operator off) are worded by the swarm
and point at the recap. Where the swarm words an ask itself, only a fragment it
put there (a row's title, git's error, a list of ids) is ever shortened.

`swarm notify "<ask>"` is the only way a session should message you. Every
shipped prompt, and the init pass's patch to the worker command, says so in so
many words: use `swarm notify` even when a brief, a ledger row, a recap or a
project document names another script (a `notify.sh`, say). A message sent that
way would not come from the swarm's own bot and would not be logged.

**What is sent, and what is held back.** The test for an ask is strict:
progress is stopped, or will stop, on something only you can do.

| what happened | kind | class |
|---|---|---|
| a worker, an operator job or the Overseer needs your answer (`swarm waiting`) | `waiting` | asks you |
| a session asks you for something it cannot do: the init pass on a broken plan, a resolver that cannot fix a conflict, the Overseer on a failure it will not retry (`swarm notify`) | `session-ask` | asks you |
| an operator job finished and left something only you can do (`operator-done --ask`) | `operator-done` | asks you |
| the operator gave up on a follow-up job after its attempts | `operator-abandoned` | asks you |
| a phase left a follow-up and the operator is off, so only you will do it | `operator-todo` | asks you |
| a row only you can do started holding other rows up (once per row; rows found together share one ask) | `owner-row` | asks you |
| a phase failed again after the Overseer's retry, or failed at all with the Overseer off | `worker-done` | asks you |
| phases finished `blocked` past that retry: one ask 15 minutes after the first of a burst names them all | `blocked` | asks you |
| a merge is held by a conflict and no resolver would start | `integrate-hold` | asks you |
| a merge is held by uncommitted changes in your checkout | `integrate-hold` | asks you |
| a merge is held because your checkout is on another branch | `integrate-hold` | asks you |
| a merge is held because git itself failed | `integrate-hold` | asks you |
| a merge is held on a push (a hold an older supervisor recorded) | `integrate-hold` | asks you |
| a landing is held (catch-up conflict or failed re-test) and no resolver would start | `integrate-hold` | asks you |
| `swarm up` found finished work it could not merge | `integrate-hold` | asks you |
| the supervisor crashed | `other` | asks you |
| a worker died three times in an hour, so its phase is no longer restarted | `other` | asks you |
| a phase would not start three times in a row, so the launcher gave up | `other` | asks you |
| the run ended with phases that were ready and never started | `other` | asks you |
| the Overseer failed three times in a row (would not start, or ran past its limit), and at every third after | `overseer` | asks you |
| a usage cap stopped the swarm (it stays down until you start it) | `usage-cap` | asks you |
| a drain finished but the swarm could not shut itself down | `drain` | asks you |
| a restart failed and left the swarm without a supervisor | `restart` | asks you |
| a restart failed and left the swarm down | `restart` | asks you |
| the Overseer's summary on the clock (`swarm overseer-summary`) | `summary` | summary |
| the swarm's own summary on the clock, when no pass wrote one or the Overseer is off | `summary` | summary |
| the run finished: what landed, what failed, what is left for you | `finish` | summary |
| a drain you asked for finished and the swarm is shutting down | `drain` | summary |
| a first `fail` with the Overseer on (it gets a pass, and retries the phase once) | `worker-done` | folded |
| an operator job finished with nothing for you | `operator-done` | folded |
| a repo started owing a push (the Overseer gets a pass if its own check refused it, or it is still owed two minutes on) | `push-owed` | folded |
| a landing's prepare command failed, the first time for that phase | `lane-unprepared` | folded |
| a worker died without finishing and its phase was started again | `other` | folded |
| the supervisor hit an internal error on one event and stepped over it (the Overseer gets a pass) | `other` | folded |
| a `swarm reload` found an error in the config file; the old settings stay | `other` | folded |
| a reload could open only some of the extra worker places | `other` | folded |
| `swarm resolved` came too early and the merge is still held | `integrate-hold` | folded |
| a usage cap paused the swarm (it resumes by itself) | `usage-cap` | folded |
| a usage pause lifted | `usage-cap` | folded |
| a usage pause lifted because the account changed | `usage-cap` | folded |
| a heavy build holds a build slot with its whole process tree idle (`[resources].idle_s`), again hourly while it stays idle | `idle-build` | folded |
| a restart did not happen and the swarm runs on unchanged | `restart` | folded |
| a waiting session moved to its own window (you were asked when it started waiting) | `park` | log only |
| a merge conflict a resolver is working on | `integrate-hold` | log only |
| a held landing a resolver is working on | `integrate-hold` | log only |
| a phase finished `blocked` and was gathered for the burst's one ask | `worker-done` | log only |
| a workspace could not be set up for a phase (the launcher retries, and asks if it gives up) | `worktree-fail` | log only |
| a worker failed to start (the same) | `spawn-fail` | log only |
| an owed push went through | `push-owed` | log only |
| a landing's prepare command failed again | `lane-unprepared` | log only |
| the web board did not start (`swarm up` prints it) | `web-board` | log only |
| the init pass or an Overseer session never became ready | `master-timeout` | log only |
| the init pass or an Overseer session would not take its instructions | `master-timeout` | log only |
| an Overseer pass would not start (not yet three in a row) | `overseer` | log only |
| an Overseer pass ran past its limit (not yet three in a row) | `overseer` | log only |
| a drain finished and the swarm is restarting (an ask follows only if it does not come back) | `drain` | log only |
| `swarm overseer-summary` on a pass with no summary due | `summary` | log only |

The bot's answer to a command you typed (`/status`, `/usage`, `/help`) is not
in the table: no swarm starts it. It answers for every swarm, so it names none
and goes into no swarm's log, and it is as long as its answer.
`ok` finishes say nothing at all.

**Why some things are not asks.** A usage pause that resumes by itself needs
nothing from you; the stop does. A first failure is the Overseer's to retry. A
conflict is the resolver's until it gives up, and then the resolver asks. An
owed push loses nothing and holds nothing up: work keeps merging on this
machine, and the Overseer is given a pass to fix it or ask. With the Overseer
on, anything folded that turns out to need you reaches you as its ask; with it
off, the swarm's own summary still tells you every `every_s` how many phases
failed and what waits on you.

**Commands (`tgbot.py`).** The bot also listens, so you can ask it. One
listener answers for every swarm on the machine, and a reply names no swarm,
because it is about all of them:

- `/status`: one line per swarm, those waiting on you first, then the ones that
  are up, then the ones that finished, then the ones that are down. Each line
  says how the swarm stands in a word (running, paused, paused by a usage cap,
  frozen, finished, down), how many phases are done of how many, and how many
  things wait on you there (questions and to-dos). A line for the machine's
  build gate follows when a build runs or waits. A state dir whose project is
  gone, one no supervisor ran in, and a swarm with `[telegram].commands = false`
  are not listed.

      beta: paused, 5 of 8 done, 2 wait on you
      alpha: running, 3 of 12 done
      gamma: finished
      delta: down, 2 of 9 done
      Builds: 1 running, 2 waiting.

- `/usage`: the account's limits once (they are the account's, so every swarm's
  readings are read together and the newest counts), then the caps, once when
  every running swarm has the same ones, then each swarm a cap holds: paused
  until a window resets, or stopped until you run `swarm up`.

      Weekly 61%, resets Sat 11:00.
      5-hour 12%, resets 16:00.
      Read at 14:03, 2 min ago.
      Every running swarm pauses at weekly 60% and 5-hour 90%, and stops at weekly 70%.
      beta: paused at weekly 61% (cap 60%) until Sat 11:00.
      gamma: stopped at the weekly cap; down until you run swarm up (the window resets Sat 11:00).

- `/help` (and `/start`): the list. Any other command gets a one-line pointer
  to `/help`.

The listener long-polls `getUpdates` with the same token and answers through the
same sender. It answers only messages from the chat in `TELEGRAM_CHAT_ID`, and
ignores every other chat without a reply. It also ignores plain text, edits, and
commands older than 15 minutes (sent while nothing listened). The next update id
is saved in the machine directory's `telegram-bot.offset.json` before an update
is answered, so none is ever answered twice. Its replies go into no swarm's
`notifications.jsonl`; a reply that did not leave is in its log.

- **Lifecycle:** a machine service, like the [web board](#the-web-board-swarm-web):
  any `swarm up` of a swarm with `[telegram].commands` on starts it when it is
  not running, every later `up` leaves it alone, and the `swarm down` of the
  last swarm that is up stops it. It is detached under either driver (no tmux
  window), started with no swarm's environment, and its pid, log, offset and
  status are `telegram-bot.*` in the machine directory. `swarm restart` starts
  it again on the code on disk; a freeze leaves it awake. `swarm telegram-bot`
  starts, stops (`stop`), reports (`status`) or runs it in the foreground
  (`serve`) by hand. `swarm status`, `swarm ls` and `swarm doctor`
  (`telegram.bot`) say whether it runs and what it is doing. A machine with no
  token in the bot's env file starts none, and `swarm up` says so.
- **One poller per token:** Telegram answers `409 Conflict` when two programs poll
  one bot (or a webhook is set). The service makes one listener per state root,
  but two can still meet on one token: a `swarm telegram-bot serve` typed by
  hand, a second state root (`SWARM_STATE_DIR` pointed elsewhere), or a
  per-swarm listener left running from before the bot was the machine's. So a
  lock per token (in `$XDG_RUNTIME_DIR`) keeps the second listener waiting to
  take over, retrying every minute. A 409 from anything else is logged, shown
  by doctor, and backed off from 60 s up to 10 minutes.
  `scripts/resolve-chat-id.sh` polls the same token, so run it while the
  listener is stopped.
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

## Several swarms on one machine (`machine.py`)

One machine can run several swarms, one per project. Each has its own
`.swarm.toml`, its own state dir, its own supervisor, tmux session and FIFO,
and none of that is shared. Three things do describe the machine rather than a
project, and `machine.py` owns them.

**The state root** is `$XDG_STATE_HOME/swarm-orchestrator` (default
`~/.local/state/swarm-orchestrator`). Every swarm's state dir is a folder in
it, named `<folder>-<hash of the project path>`. Inside a session the state
root is the folder that session's own state dir is in, which is the same
place.

**The machine directory** is `machine/` in the state root
(`machine.directory()`), beside the swarms' state dirs: the place for run
state that every swarm on the machine shares. The build gate is kept in it,
`machine/buildsem/` ([Build gate](#build-gate-swarm-build)). It
moves with the state root, so a test that redirects a run's state redirects
what the run shares too. No swarm can take its name: `[swarm].slug =
"machine"` is refused.

**The machine file** is `machine.toml` in the user's config folder, for
settings that are true of the box whichever project asks
([docs/config.md](config.md#the-machine-file-machinetoml)): the build gate's
limits, `[build]`. `machine.settings()` reads it.

**The registry** is the list of swarms, `machine.swarms()`, and `swarm ls`
prints it. No file holds it: it is read from the state root each time, one
entry per state dir, so a swarm cannot be running and missing from the list.
For each it gives:

- the name, the tmux session and the project, from what the swarm's last
  supervisor recorded in `<state>/config.json`;
- whether it is running: something reads its control FIFO, the same test
  `swarm up` uses to refuse a second supervisor;
- paused, frozen, held by a usage cap, finished, and how many sessions are
  asking you, from `state.json` (read without the lock, so a frozen session
  holding it never blocks the list);
- ledger rows done, running and open, counted by the function `swarm status`
  uses, and the number of to-dos `swarm todo` lists.

A state dir whose project folder is gone is listed as `stale`, and one no
supervisor ever ran in as `empty`. Neither is hidden: each is a leftover, and
the list is where you find it. One swarm that cannot be read (a torn
`state.json`) shows what could be read and says why the rest is missing; it
does not hide the others.

`machine.swarm_config(state_dir)` gives the config of the swarm that owns a
state dir, bound to that state dir whatever the caller's own environment
names. That is how one process reads another swarm without becoming a session
of it.

**One tmux server.** The swarms share the owner's tmux server with each other
and with the owner's own sessions. Each swarm has one session, named after the
swarm unless `[tmux].session` says otherwise, and marks it with its state dir.
What a swarm sets in tmux it sets on its own: dead panes staying on screen and
no renaming by a program's title are set on each of its windows as the window
is made, and no renumbering on its session, never server-wide, so the owner's
windows and another swarm's keep the options they had. Two swarms cannot share
a session name. `swarm up` refuses when a session of its name exists and says
whose it is: this swarm's own (already up), another swarm's (its name and
project, and that this one needs a `[tmux].session` of its own), or one of the
owner's own (rename it, or name this swarm's differently). Attaching names the
session exactly, so a session name that is the start of another's still gets
its own.

**A command still acts on one swarm only.** Listing is the one thing that
crosses swarms. Every other command resolves one project (see
[docs/cli.md](cli.md)), and a session of one swarm is refused a command that
names another.

## Smaller modules

- **`blockedping.py`:** gathers a burst of `blocked` outcomes that would each
  ask you (past the Overseer's retry, or with the Overseer off) and sends one ask
  naming the phases, 15 minutes after the first; the reasons, grouped, are kept
  in the log beside it.
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
- **`repocmd.py`:** runs a project's own command in a checkout the swarm names,
  through the build gate, with a timeout and its output in the caller's log:
  the landing's lane check, the `[lanes].prepare` command before it, and a
  repo's `[git].post_merge` command.
- **`pauseat.py`:** `swarm pause --in 12h` / `--at 03:00`, a pause scheduled in
  `state.json` that survives `swarm down` and `up`.
- **`procs.py`:** reads `/proc` for the process table and process identity (pid plus
  start time), shared by the reaper and `swarm keep`.
- **`ledgermigrate.py`:** one-time move of a ledger's accumulated notes and journal
  into history.
- **`logutil.py`:** the supervisor's structured, greppable log lines.
