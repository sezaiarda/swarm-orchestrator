# `.swarm.toml` reference

Every key the loader reads, with its default, the environment variable that
overrides it, and what `swarm reload` does with an edit to it. Each key is
declared once, on its `Config` field in `src/swarm_orchestrator/config.py`; the
loader, `swarm reload` and the TUI config form all read that declaration, and a
test checks these tables against it. The file lives at the project root (or pass `--config`). A missing
table or key takes the default, so a minimal file works, and an empty one too.
The file is what makes a folder a project: `swarm` finds it from the folder a
command is typed in upward, and refuses every command where there is none (see
[docs/cli.md](cli.md)).

A key the loader does not read is ignored, so a file that still sets a retired
key (`[worker].done_hook`, `[tasks].roadmap`) loads unchanged. The one exception
is a key that has moved to [the machine file](#the-machine-file-machinetoml)
(the build gate's `[build]` `max_concurrent`, `short_s`, `overtake`,
`idle_yield_s`, `idle_yield_max`, `pair`, `alone`; the board's `[web]` `host`
and `port`; the bot's `[telegram]` `notify` and `env`): a `.swarm.toml` that
still sets one does not load, and the error names the key and `machine.toml`.
Ignoring it would leave the owner believing in a setting that is not in force.

**Precedence:** an environment variable, when set, beats the file, which beats the
default. An integer that does not parse falls back to the next source instead of
failing the load. A `SWARM_*` variable set in the supervisor's environment keeps
winning after a reload; `swarm reload` reports such fields as shadowed.

**Reload class** (`swarm reload`, see `reload.py`):

- **hot**: applied to the running swarm straight away.
- **next**: accepted, and reaches the next worker, master or session launched.
  Anything already running keeps the value it was started with.
- **restart**: refused and held at the old value. These name the run itself
  (state dir, tmux session, driver, isolation mode, board socket). Change them
  with `swarm down` and `swarm up`, or with `swarm restart --full`, which does
  both without closing a session that waits on you. A plain `swarm restart`
  replaces only the supervisor: it applies hot and next settings as a reload
  does and holds these at the old value too.

A launched session (a worker, an operator job, the Overseer) runs its own
`swarm` commands from its mirror, a component repo inside it or another
checkout. They read this file, the project's, not a copy in the folder they
are run from: `SWARM_PROJECT` names the project. So what a worker's
`swarm done` or an operator's `swarm operator-done` reads
(`[operator].enabled`, `triage_model`, `notify`, `done_grace_s`) is "hot":
the next such command uses the saved value. If the
file does not load, the command says so and runs on the settings the supervisor
last recorded (`<state>/config.json`), never on another folder's file.

## `[swarm]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `name` | the project folder's name | `SWARM_NAME` | hot | The swarm's display name: what you read wherever the swarm names itself. `""` means the folder's name. It names nothing on disk: the slug, the state dir, the worktrees and `SWARM_PROJECT` follow the folder, so renaming the swarm moves no state and needs no folder rename. See [the display name](#the-display-name-swarmname) for what an edit reaches when. |
| `max_workers` | `4` | | hot | Worker slots. Must be at least 1. Growing adds panes live; shrinking marks the extra slots retiring, and a busy one finishes its phase first. |
| `master_model` | `""` | | next | `--model` for the init pass and, by default, the Overseer. `""` inherits the user's setting. |
| `slug` | `<dirname>-<sha1 of path, 8 chars>` | `SWARM_SLUG` | restart | Names the state dir. The hash keeps two projects with the same folder name apart. `machine` is refused: that folder of the state root is shared by every swarm. So is a slug another project already runs under: two swarms cannot share a state dir, and the second is told both paths. A project that is moved keeps its state when its slug is set here. |
| `driver` | `"tmux"` | `SWARM_DRIVER` | restart | `tmux`, or `bare` (headless subprocesses, no panes; the hermetic tests use it). |
| `master_cmd` | `""` | `SWARM_MASTER_CMD` | next | Replaces the built-in `claude` command in the overseer window (the init pass, and the Overseer when `[overseer].cmd` is empty). With it set, no prompt is typed in. The tests use it to inject a fake master. |
| `resolver_cmd` | `""` | `SWARM_RESOLVER_CMD` | next | Replaces `cd <repo> && exec claude` for the merge-conflict resolver. With it set, no prompt is typed in. |
| `resolver_model` | `"sonnet"` | | next | `--model` for the merge-conflict resolver. `""` inherits the user's setting. Ignored when `resolver_cmd` is set. |
| `lean_sessions` | `true` | `SWARM_LEAN_SESSIONS` | next | Every `claude` the swarm opens (workers, operator, Overseer, init pass, resolver, big picture, guide, console, and the `claude -p` behind recaps and triage) runs on the project's setup, not your personal one: `--setting-sources project,local` drops `~/.claude/CLAUDE.md`, your user skills, agents and commands, your plugins and your user-scope MCP servers; auto-memory and the claude.ai connectors are turned off too. The project's CLAUDE.md, `.claude/settings*.json`, `.claude/commands` and `.mcp.json` still load. Your `~/.claude/settings.json` (permission mode, model, `env`, attribution, …) is carried over in `--settings`, less `enabledPlugins`, `extraKnownMarketplaces` and `claudeMdExcludes`, with the project's own settings files over it. A `*_cmd` that replaces the whole command is left alone, and so is a `worker_cmd`/`[console].cmd` that passes its own `--setting-sources`. `false` gives every session your full setup. |
| `watchdog_s` | `300` | `SWARM_WATCHDOG` | hot | Seconds between the supervisor's liveness sweeps. `0` turns the sweep off and leaves the loop purely event-driven. See the README's watchdog section. |

### The display name (`[swarm].name`)

One name, shown in several places. An edit reaches them at different times:

| where you read it | when a new name shows |
|---|---|
| Telegram messages that name the swarm ("… has stopped", "… did not restart") | **hot**: the next message after `swarm reload` |
| `swarm status` (`name=`, and `config.name` in `--json`) | **hot**: the next command |
| the web board: the swarm's card, its button and its page's title | **hot**: after `swarm reload`. The board shows each swarm on the settings its supervisor recorded, so for a swarm that is down the new name shows at its next `swarm up` |
| the dashboard's status bar | **next**: when the dashboard next starts; a plain `swarm restart` restarts it |
| the bot's `/status` and `/usage` answers | **hot**: after `swarm reload`. The listener reads each swarm on the settings its supervisor recorded, as the board does |
| the tmux session (`tmux ls`, `tmux attach -t …`), unless `[tmux].session` sets it | **restart**: `swarm down` then `swarm up`, or `swarm restart --full` |

The tmux session cannot be renamed under a run: every pane and window the run
recorded is in it. So after the name changes, a running swarm stays in its old
session, every `swarm` command (`attach`, `console`, `down`, …) keeps
addressing that one, and `swarm reload` lists `[tmux].session: <old> -> <new>`
under *REFUSED*, each time, until the restart that renames it. `swarm down`
ends the old session and the `swarm up` after it creates the new one.

The web board and the Telegram listener are the machine's, and know a swarm
by its state dir, which a rename does not move: the swarm's page keeps its
address, and the listener answers for it under the new name.

## `[worker]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `command_template` | `"/prime {phase}"` | | next | The line typed into a worker's pane once `claude` has booted. |
| `command_file` | `".claude/commands/prime.md"` | | next | The project's slash-command file. The init pass patches it for swarm mode, and `swarm check` lints it. |
| `builder_model` | `"sonnet"` | | next | The model a worker's lead session gives each builder subagent it spawns. |
| `env_marker` | `"SWARM_PHASE"` | | next | The variable that carries the phase id into the worker's environment. |
| `worker_cmd` | `"claude"` | `SWARM_WORKER_CMD` | next | The base command a slot pane is respawned with, as `cd <cwd> && exec <worker_cmd> -n 'swarm · worker · <phase>' --settings … --effort …`. `{phase}` expands. The `-n` display name is added only when the command sets none itself (an older `-n worker:{phase}` is kept as it is). The `--model` it names is the swarm's own model: a row whose `model:` field names another runs with that model swapped in, and its worker gets `--append-system-prompt` with the rule for `swarm escalate`. |
| `ready_marker` | `""` | `SWARM_READY_MARKER` | next | Text that means "claude has booted". `""` means the running `claude --version`, which the boot banner prints; if that cannot be read, `Claude Code`. |
| `worker_settings` | `'{"teammateMode":"in-process"}'` | `SWARM_WORKER_SETTINGS` | next | JSON merged over the user's settings via `--settings`. The meters status-line tap is added unless this JSON sets its own `statusLine`. Register `scripts/stop-hook.py` here as a `Stop` hook to get recaps (see below). `""` passes no settings. |
| `effort` | `"high"` | `SWARM_WORKER_EFFORT` | next | `claude --effort` for every worker: one of `low`, `medium`, `high`, `xhigh` or `max`. `""` inherits the user's setting. |
| `done_grace_s` | `0` | `SWARM_DONE_GRACE` | hot | Seconds a worker keeps its slot after `swarm done` before the supervisor is poked. A detached child sleeps and then delivers the poke, so `swarm done` itself returns at once. |
| `park_after` | `120` | `SWARM_PARK_AFTER` | hot | Seconds any session — worker, operator job or Overseer pass — may sit in `swarm waiting` before it is parked, alive, to its own window, freeing what it held (a worker's slot, the operator window, the master pane). `0` disables parking. |

Registering the Stop hook (the path is wherever this repo lives):

```toml
worker_settings = '{"teammateMode":"in-process","hooks":{"Stop":[{"hooks":[{"type":"command","command":"/path/to/swarm-orchestrator/scripts/stop-hook.py","timeout":10}]}]}}'
```

## `[tasks]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `ledger` | `"docs/PHASE-LEDGER.md"` | | hot | The phase ledger, relative to the project root. The launcher parses it on every decision. |
| `exclude` | `[]` | | hot | Phase ids never to launch, for example rows blocked outside the swarm. `swarm why` quotes the comment next to an entry. |
| `history` | `"docs/phases"` | | hot | Where the swarm files what was written about each phase: `<history>/<family>.md`, where the family is the id up to its first `-` (`api-W12` goes to `api.md`). Sessions never edit it; see [cli.md](cli.md#reporting-the-ledger-and-the-history). |
| `history_split_kb` | `256` | | hot | A family file that grows past this many KB becomes a directory with one file per phase (`<history>/api/api-W12.md`). `0` never splits. |
| `lessons` | `"tasks/lessons.md"` | | hot | The file `swarm lesson` appends to. |
| `ledger_gate` | `""` | | hot | A command that checks the ledger, run in the project checkout with `SWARM_LEDGER` set to the ledger's path each time the swarm adds a follow-up row. If it exits non-zero the row is not added and the refusal goes into the filing phase's history. |
| `ledger_batch_s` | `1800` | | hot | How long a note (`swarm record <row> note`) or a lesson filed outside its phase may wait to share a commit. It is written with the next ledger commit (a phase landing, a row filed, reshaped or recorded), before a worker or an Overseer pass starts, at once when an operator job for the row it names is due, and otherwise once the oldest has waited this long. `0` commits each one at once. |
| `ledger_in_merge` | `true` | | hot | Under worktree isolation, a phase that lands with a merge commit in the project checkout has its reports written into that commit: its tick and status, its history entry, the lessons and follow-up rows it filed, and the notes and lessons waiting under `ledger_batch_s`. The merge's subject gains what was written (`Merge branch 'swarm/<phase>': <phase> done; follow-up <id>`), and the two are pushed once, so `git log --first-parent` reads one line per landed phase. Everything else keeps the separate `ledger:` commit; see [components.md](components.md#the-ledger-writer). `false` writes a `ledger:` commit after every merge, as before. |

## `[telegram]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `commands` | `true` | `SWARM_TG_COMMANDS` | restart | The machine's bot answers for this swarm: its `swarm up` starts the machine's listener when it is not running, and `/status` and `/usage` list it. `false` leaves it out of both, and its `swarm up` starts none. |

The bot is the machine's, one for every swarm, so which script sends and where
its token is are not a project's to say: they are [`[telegram]` in
`machine.toml`](#telegram-1). A `.swarm.toml` that still sets `[telegram].notify`
is refused by every command, with the key and the machine file named.

## `[tmux]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `session` | `[swarm].name`, slugified | `SWARM_SESSION` | restart | The tmux session name. Unset, it follows `[swarm].name`; set here (or by the variable), it wins. It must be this swarm's alone on the tmux server: `swarm up` refuses a name that another swarm's session, or one of your own, already has, and says whose it is. A running swarm keeps the session it was started in either way: see [the display name](#the-display-name-swarmname). |
| `layout` | `"auto"` | `SWARM_LAYOUT` | hot | How worker windows arrange their panes. `auto` gives one pane the full window, puts two side by side, and tiles three or four. The others are `even-horizontal` (aliases `side-by-side`, `left-right`, `columns`), `even-vertical` (aliases `top-bottom`, `stacked`, `rows`), `tiled` (alias `grid`), `main-horizontal` and `main-vertical`. An unknown name fails the load. `swarm layout <name>` changes it live, and a layout set that way wins over a reload until the next `swarm up`. |
| `panes_per_window` | `4` | `SWARM_PANES_PER_WINDOW` | restart | Worker panes per tmux window. More slots page into further windows: `workers`, `workers-2`, …. At 2, five workers are 2, 2 and 1; three are 2 and 1. At least 1 (a lower value counts as 1). Restart, because the windows are paged at `swarm up`. |

## `[tui]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `autostart` | `true` | `SWARM_TUI_AUTOSTART` | restart | Run the dashboard in window 0 at `swarm up`. |
| `cmd` | `"swarm tui"` | `SWARM_TUI_CMD` | restart | The command window 0 runs. |

## `[console]`

The owner console: your own Claude session in the `console` window, between the
dashboard and the overseer. See [components](components.md#the-owner-console).

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `true` | `SWARM_CONSOLE` | restart | Open the `console` window at `swarm up`. |
| `prompt_file` | `""` | | next | A file, relative to the project, appended to the console's primer under "This project": what the project calls things, its rules for rows and campaigns. Read each time the console's `claude` starts. |
| `model` | `""` | | next | `--model` for the console. `""` inherits your own setting. |
| `cmd` | `""` | `SWARM_CONSOLE_CMD` | next | Replaces `claude` as the console's base command; the console's flags (`-n`, `--append-system-prompt`) are still appended. The tests use it to inject a fake. |

## `[git]`

Leave the table out for in-place work (`isolation = "none"`).

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `isolation` | `"none"` | `SWARM_GIT_ISOLATION` | restart | `none`: workers commit in the project itself. `worktree`: every phase gets its own full-workspace mirror on `swarm/<phase>`, landed by the merge queue. |
| `main_branch` | `"master"` | `SWARM_GIT_MAIN` | hot, refused while a phase is in flight | The umbrella repo's integration branch. A component repo uses it when the branch exists there, and otherwise the branch it has checked out. |
| `repos` | `["*"]` | `SWARM_GIT_REPOS` (comma list) | hot, growing only | Globs, relative to the project root, that pick the component repos to mirror. Only directories with a `.git` count, and dot-names never match. A single-repo project matches none and gets an umbrella-only mirror. Dropping a repo while a phase is in flight is refused. |
| `auto_resolve` | `{}` | | hot | Path glob mapped to `"union"` or `"keyed:<regex>"`. The integrator tries these before it opens a resolver session (see the README). |
| `auto_resolve_check` | `{}` | | hot | Path glob mapped to a shell command. After `auto_resolve` settles a matching file, the command runs in that repo on the merged text; a non-zero exit (or 5 minutes) puts the conflict back and opens the resolver instead. |
| `post_merge` | `{}` | | hot | Repo name (`.` for the project repo, `"*"` for every repo not named) mapped to a shell command. The integrator runs it in that repo's main checkout after a merge has landed there and before the push, through the build gate like any `swarm build`: for what the repo's pre-push check reads and git does not hold, such as the dependencies installed beside the checkout. A command that fails, times out, or leaves tracked files changed leaves the push owed with its last lines as the reason; it never holds the merge queue. An owed push's retry runs it again. A repo with a command lands after the component repos without one. |
| `post_merge_if` | `{}` | | hot | Repo name mapped to a quick shell test, run in the main checkout at once and outside the build gate (30 s at most). `post_merge` runs only when it exits 0. Without one the command runs, and queues for a build slot, at every merge into that repo. |
| `post_merge_timeout_s` | `300` | | hot | Seconds a `post_merge` command may wait for a build slot, and seconds it may then run. The supervisor does nothing else meanwhile, so past either the command is given up and the push is owed. |

Example:

```toml
[git]
isolation    = "worktree"
main_branch  = "master"
repos        = ["*"]
auto_resolve = { "docs/PHASE-LEDGER.md" = "keyed:^- \\[[ x]\\] `([A-Za-z0-9_.-]+)`", "CHANGELOG.md" = "union" }
auto_resolve_check = { "docs/PHASE-LEDGER.md" = "python3 ci/ledger-gate.py" }
post_merge    = { "app" = "sh scripts/sync-deps.sh" }
post_merge_if = { "app" = "sh scripts/sync-deps.sh --check" }
```

## `[lanes]`

Rows that touch different files run at the same time, even in one repo. A ledger row declares what it
edits in a `touches:` field (`- [ ] `web-F2` · dir:`web` · needs:`api-W1` · touches:`web/src/ui/a.tsx` `web/src/api/**` · **title**`).
A touch is `<repo>/<path>`, `<repo>/<dir>/**` (`<repo>/**` is the whole repo), `./<path>` for the
project repo itself, or `@<resource>` for something that is not a file. `*` may appear once, in the last
segment only; `**` only as the whole last segment. A row with no `touches:` owns every repo in its
`dir:` (or, with no `dir:`, the repo its id prefix names), so it runs alone in that repo.

With lanes on, the launcher walks the ready rows in ledger order. A row whose touches overlap nothing
in flight launches; one that overlaps waits, and reserves its touches so a later overlapping row cannot
overtake it while a later disjoint one still launches. A phase's lane is recorded in `state.json` at
launch and released when it merges, is discarded or skipped, or is freed. `swarm context` shows it as
`lanes`, and `swarm why <row>` names the phase a waiting row waits for. A row whose touches do not parse
never launches and is listed under `ledger_issues`.

A touch that matches `commons` is never part of a lane. A row may name its repo's `CHANGELOG.md` in
`touches:` to say that it edits it, and that costs nothing: the row neither waits for another row over
that file nor keeps one waiting, and `swarm why`, `swarm context` and `swarm doctor` never name it as
held. Two rows whose only common touch is a commons file run together, up to `per_repo`. A row whose
touches are all commons launches beside anything and counts toward no repo. A touch is compared with
the globs as it is written, so `web/**` is still a lane with a changelog under it. The lane recorded at
launch, and `$SWARM_TOUCHES`, keep every touch the row names. Two rows that edit the same lines of a
commons file meet at landing, as a text conflict.

Landing re-tests the combination. When a finished phase changed a repo whose main gained
anything but `commons` since the phase branched, the integrator takes that repo's landing lock, merges
main into the phase's own worktree and runs `check` there, detached (`swarm _lane-check`); the queue
lands other phases meanwhile, and nothing else lands in that repo until this one does. Green, it lands
the tree that was tested. Red, a timeout or a text conflict holds the queue and opens the resolver on the
worktree, never on your checkout; `swarm resolved <phase>` merges main again and re-checks. A green
check is kept when main moves again before the phase lands, as long as main gained only `commons` (the
swarm's own ledger writes, for one): main is merged in once more and the phase lands without a second
check. Anything else main gained starts the check again. The check queues for a build slot like any
`swarm build`, unless its command is light by the same rules: a check that compiles nothing and is
cheap to run beside a build (a lint-only gate) can be named in `[build].light`, and then starts at
once. Before the check, the repo's `prepare` command makes the worktree ready for it, when the repo's
`prepare_if` test says there is something to do. A `prepare` that fails is not a red check: the queue is
not held and no resolver opens, and the landing is tried again five minutes later. Files a phase
changed outside its lane (its touches, anything added with `swarm widen`, and `commons`) are noted in its
history and in the next Overseer digest; that never holds a merge.

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `false` | `SWARM_LANES` | restart | Schedule by touches. Off, the launcher is exactly the one-row-per-ready-slot launcher, and `state.json` gains no `lanes` key. |
| `per_repo` | `2` | | hot | At most this many phases in flight in one repo (the project repo counts as one), however disjoint their touches. Resources and commons touches do not count. |
| `commons` | `[]` | | hot | fnmatch globs over `<repo>/<path>` (`./<path>` for the project repo) that any row may edit without declaring them, such as the ledger and every `CHANGELOG.md`. A touch that matches one is not part of its row's lane, so they never count as an overlap. Read by every launch decision and at landing. |
| `resources` | `[]` | | hot | Names a row may touch as `@<name>`. Two rows touching the same resource never run together. |
| `external` | `{}` | | hot | Name mapped to the path of a repo outside the project that the swarm does not mirror. Its name is a lane a touch may start with. |
| `check` | `{}` | | hot | Repo name (`.` for the project repo) mapped to the command that re-tests a phase merged with the lanes that landed beside it. `"*"` is the default for a repo not named. Read at landing. |
| `check_timeout_s` | `2700` | | hot | Seconds a landing check may run before it counts as red. |
| `prepare` | `{}` | | hot | Repo name (`.` for the project repo, `"*"` for every repo not named) mapped to a shell command. It runs in the phase's worktree of that repo before the `check`, through the build gate like any `swarm build`: for what the check reads and git does not hold, such as the dependencies installed beside the checkout. A phase that never built in the repo has none, and a catch-up merge that moves a pin leaves the old ones. A command that fails, times out, or leaves tracked files changed is not a red check: no check runs, no resolver opens and the merge queue is not held. You are told once, with its last lines as the reason; other phases land in that repo meanwhile, and the landing is tried again every five minutes. |
| `prepare_if` | `{}` | | hot | Repo name mapped to a quick shell test, run in the worktree at once and outside the build gate (30 s at most). `prepare` runs only when it exits 0. Without one the command runs, and queues for a build slot, before every landing check in that repo. |
| `prepare_timeout_s` | `600` | | hot | Seconds a `prepare` command may run once it has a build slot. Past it the command is stopped and has failed. |

Example:

```toml
[lanes]
enabled   = true
per_repo  = 2
resources = ["live-box"]
external  = { "shared-lib" = "~/projects/shared-lib" }
commons   = ["./docs/PHASE-LEDGER.md", "./docs/phases/**", "*/CHANGELOG.md"]
check     = { "*" = "scripts/push-gate.sh", "." = "bash ci/push-gate.sh" }
prepare    = { "app" = "sh scripts/sync-deps.sh" }
prepare_if = { "app" = "sh scripts/sync-deps.sh --check" }
```

## `[batch]`

Batch claims: when a slot frees, the row the launcher would start (the *seed*) takes up to `max_rows`
related ready rows with it, and one lead session builds them in order, each row still its own commit
and its own `swarm done`. A row may ride with the seed when it is open and in flight nowhere, is not
excluded or dated, runs on the seed's model, overlaps no lane in flight, and every row it `needs` has
landed or comes earlier in the batch (a chain successor rides after its need). Rows that run alone
whatever the settings: one touching `./**`, one touching 3 or more code repos, an oversize row, and
one that failed or was handed up before. `model` picks among those rows with one `claude -p` call; the
answer is checked (seed first, known ids only, `needs` in order, one model, within the caps, no lane
clash) and any failure, timeout or error falls back to the deterministic rule, with a
`BATCH-FALLBACK <seed> <why>` line in the supervisor log. `swarm batch` prints what the rule would form
now (see [cli.md](cli.md)). A row's size in points is `max(3, its ledger text in KB) + 0.5` per touch.

At run time the batch is one claim: the seed's slot, mirror, `swarm/<seed>` branch and lane (the
union of the rows' touches), with the riders in flight beside it (`CLAIM <row> slot=N batch=<seed>`
in the log, `batches` in `state.json`, shown running everywhere the seed is). Every worker, a batch
of one included, gets `prompts/worker_lead.md` appended to its system prompt (`--append-system-prompt-file`,
written to `<state>/briefs/<seed>.lead.md`, with `$SWARM_BATCH` listing the rows): it orients once,
hands each row to a builder subagent on `[worker] builder_model`, reviews, commits one commit per
row, runs the full gate once for the batch, and reports each row with its own `swarm done`. Nothing
is merged or recorded before the last row reports; then the branch lands once and each row is
recorded with its own outcome and history. A row that fails leaves no commit (the lead stashes it)
and goes alone to the Overseer or the owner; `swarm unbatch` hands a row back unbuilt. A session
that dies lands the rows it reported (what it had not committed goes to `refs/swarm-attic/`), and
its unreported rows are ready again; with nothing reported the seed keeps its branch for its next
attempt, as a lone row does.

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `true` | `SWARM_BATCHING` | hot | Let a slot claim a batch. `false`: one row per slot, as before. |
| `max_rows` | `5` | | hot | Rows per batch, seed included. At least 1; anything above 5 counts as 5. |
| `max_points` | `60` | | hot | Most points one batch may sum to. A seed above it still runs, alone. |
| `max_repos` | `3` | | hot | Most distinct code repos (`.` and `@resources` aside) one batch may touch. |
| `model` | `"sonnet"` | | hot | `--model` for the call that picks the batch. `""` skips the call and always uses the deterministic rule. |
| `timeout_s` | `60` | | hot | Seconds that call may take before the rule is used instead. |

## `[build]`

What describes this project's builds. The gate they queue at is one for the
whole machine, shared with every other swarm on it, so its limits (how many
builds at once, the queue's rule, idle yield, the pairing rules) are not here:
they are [`[build]` in `machine.toml`](#build-1).

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `jobs` | `6` | `SWARM_BUILD_JOBS` | next | `CARGO_BUILD_JOBS` for a `cargo` run through `swarm build`, and for every worker under worktree isolation. `0` means no cap. |
| `cache` | `true` | `SWARM_BUILD_CACHE` | next | Point each Rust worktree's `target/` at one shared per-repo cache, `<state>/cache/target/<repo>`. This happens only where the repo gitignores `target`. With it comes a cargo config for every build under the state dir, `<state>/.cargo/config.toml`, whose `build.rustc-wrapper` keeps a test built in one mirror able to start its binary after that mirror is removed; it takes the place of a `rustc-wrapper` from your own cargo config there ([components.md](components.md#worktree-isolation-and-mirrors)). |
| `heavy` | `[]` | `SWARM_BUILD_HEAVY` | hot | Command patterns that always queue, over the built-in rules. A pattern is a command prefix whose words are globs (`"cargo check"`, `"scripts/*.sh"`). Heavy wins over light. |
| `light` | `[]` | `SWARM_BUILD_LIGHT` | hot | Command patterns that never queue (`"bun run lint*"`). Only for commands that compile, bundle and build nothing, and that are small enough to run beside a build: measure one first (CPU, peak memory, bytes written). The landing's lane check reads it too, so a `[lanes].check` named here starts without a build slot. |

`jobs` is this project's share of the cores for one build. The machine allows
`max_concurrent` builds at once, of any swarm, so `jobs` times that should not
exceed the cores; the other swarms' `jobs` count toward the same total.

## `[operator]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `false` | `SWARM_OPERATOR` | hot | Opt-in. `true` lets the swarm open an unattended session with your authority. While `false`, nothing is queued and each `operator` hand-off is telegrammed to you as a to-do. |
| `cmd` | `""` | `SWARM_OPERATOR_CMD` | next | Replaces the built-in session command. With it set, no brief is typed in. |
| `model` | `""` | | next | `--model` for operator sessions. `""` inherits the user's setting. |
| `triage_model` | `"haiku"` | | hot | The model that answers now-or-later for each hand-off. Use an alias, not a dated build. |
| `later_wait_s` | `10800` | | hot | The longest a job triaged `later` waits. It normally opens when a worker slot is free that no ready phase wants; with a deep backlog that never happens, so once it has been queued this long it opens anyway, oldest first, one session at a time, never before its phase has merged. The session takes no worker slot. `0` means no cap. |

## `[overseer]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `true` | `SWARM_OVERSEER` | hot | `false` leaves only launching and integrating. |
| `cmd` | `""` | `SWARM_OVERSEER_CMD` | next | Replaces the built-in session command. Falls back to `[swarm].master_cmd`. |
| `model` | `""` | | next | `""` uses `[swarm].master_model`. |
| `min_gap_s` | `600` | `SWARM_OVERSEER_MIN_GAP` | hot | The minimum time between the starts of two non-urgent passes. |
| `every_s` | `14400` | `SWARM_OVERSEER_EVERY` | hot | How often you get the Overseer's summary on your phone, in seconds. A pass that writes it (`swarm overseer-summary`) starts this long after the last one did (after the swarm started, for the first); passes for anything else do not move this clock. If that pass sends none, or the Overseer is off, the supervisor sends a summary of its own with the bare counts. A stretch in which nothing landed, failed, was held back or is building sends none. `0` means no summary on the clock: you still get asks, and the one at the end of the run. |
| `owner_wait_s` | `3600` | `SWARM_OVERSEER_OWNER_WAIT` | hot | A session asking you this long triggers a pass, once per unanswered question (one parked and answered is working, and does not count). |
| `starve_s` | `600` | `SWARM_OVERSEER_STARVE` | hot | Free slots, nothing launchable and backlog still open for this long triggers a pass, once per episode. |
| `hold_wait_s` | `600` | `SWARM_OVERSEER_HOLD_WAIT` | hot | A merge conflict the resolver is working on triggers a pass only once it has been held this long. A hold with no resolver, or one the resolver gave up on, triggers at once. |
| `timeout_s` | `2700` | `SWARM_OVERSEER_TIMEOUT` | next | A pass still running after this is killed, and whatever it committed is merged. `swarm waiting` stretches the deadline while you are being asked. |

## `[usage]`

Usage caps: what the swarm does when the 5-hour or weekly limit runs high. They
act on the swarm only; workers are never told. See
[components](components.md#usage-caps).

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `true` | `SWARM_USAGE` | hot | `false` turns the caps off and lifts a hold they placed. |
| `check_s` | `600` | `SWARM_USAGE_CHECK` | hot | How often the supervisor checks usage against the rules. At least 60. |
| `stale_s` | `1800` | `SWARM_USAGE_STALE` | hot | A reading older than this is not trusted. With nothing fresher, the supervisor asks Claude Code's usage endpoint, at most once every 30 minutes. At least 300. |
| `rules` | see below | | hot | A list of `{ window = "week" \| "five_hour", at = <percent>, action = "pause" \| "down" }`. A malformed rule fails the load. |

The default rules:

```toml
[usage]
rules = [
  { window = "week",      at = 60, action = "pause" },
  { window = "week",      at = 70, action = "down"  },
  { window = "five_hour", at = 90, action = "pause" },
]
```

`pause` stops new workers while a fresh reading is at or over `at`; running
workers carry on. `down` runs `swarm down` once per window; the swarm stays down
until you run `swarm up`.

## `[big_picture]`

The big-picture pass: a session that rewrites one bounded project doc so workers
read where the project stands instead of surveying it. See
[components.md](components.md#the-big-picture-pass).

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `every` | `10` | `SWARM_BIG_PICTURE_EVERY` | hot | Refresh the doc every N integrated phases. `0` turns the counter off. With both this and `max_age_h` at `0` no pass runs by itself (`swarm big-picture --now` still starts one). |
| `max_age_h` | `0` | `SWARM_BIG_PICTURE_MAX_AGE_H` | hot | Also refresh a doc this many hours old, once a phase has landed since. `0` turns this off. |
| `doc` | `"docs/BIG-PICTURE.md"` | | hot | Where the doc lives, relative to the project root. The swarm commits it to the target branch. When it does not exist yet, the first pass runs straight away. |
| `model` | `"opus"` | | next | `--model` for the session. `""` inherits the user's setting. |
| `cmd` | `""` | `SWARM_BIG_PICTURE_CMD` | next | Replaces the built-in session command (run as `cd <cwd> && <cmd>`). With it set, no brief is typed in. The tests use it to stand in for `claude`. |

## `[gc]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `auto` | `true` | `SWARM_GC_AUTO` | hot | Let the supervisor run `swarm gc` by itself. |
| `every_s` | `900` | `SWARM_GC_EVERY` | hot | At most this often (a build gate that did not empty within `wait_s` defers it ten minutes). `0` leaves only the idle trigger. |
| `idle_s` | `1800` | `SWARM_GC_IDLE` | hot | Also once per idle stretch (no busy slot) of this length. `0` turns this off. |
| `wait_s` | `600` | `SWARM_GC_WAIT` | hot | How long an automatic gc waits in the build queue for the gate to be empty before it leaves and retries later. It holds no slot while it waits and builds pass it, so waiting costs the builds nothing; it runs the moment no build is alive, ahead of the builds queued after it. `0` only takes a gate that is empty right now. |
| `hold_s` | `120` | `SWARM_GC_HOLD` | hot | For the last `hold_s` of that wait, gc is passed no more: no build queued after it starts until it has run or its wait is over. With two build slots under load the gate is never empty by itself, and the disk fills; this is what empties it. It is also the most a gc makes any build wait, plus the sweep itself (seconds). Larger: gc runs more surely, and a free slot may sit idle that long behind a long build. `0`: gc holds no build back and runs only when the gate happens to be empty. It also applies to `swarm gc --yes`, whose whole wait is 5 minutes. |
| `keep_days` | `3` | `SWARM_GC_KEEP_DAYS` | hot | Build output used within this many days survives `cargo sweep`. At least 1. |
| `attic_days` | `30` | `SWARM_GC_ATTIC_DAYS` | hot | Work the swarm set aside instead of deleting (a discarded phase's commits, under `refs/swarm-attic/<phase>/<utc-stamp>`) is kept this many days before gc drops the ref, and its backup branch on origin with it. At least 1. |

## `[backup]`

Backup pushes copy every unmerged phase's work to the `origin` each repo already
pushes to (worktree isolation only): the phase branch as `swarm/<phase>` when it
holds commits main lacks, its uncommitted edits as a snapshot commit
`swarm-wip/<phase>` (built in a throwaway index; the worker's files and index are
untouched), and each kept `refs/swarm-attic/<phase>/<stamp>` as the branch
`swarm-attic/<phase>-<stamp>`. Pushes use `--force-with-lease` and skip pre-push
hooks. A backup whose work has reached main is deleted on the next pass, and an
attic backup when gc drops its local ref (`[gc].attic_days`). A
failure is logged (`BACKUP` in `supervisor.log`) and never holds anything up.

A snapshot leaves out the component repos nested in the repo it is taken of: each
has its own backup. A snapshot that cannot be made is logged per phase
(`BACKUP-SNAPSHOT-FAILED <phase> <repo>: <git's reason>`) and counted in the
pass's summary line (`BACKUP 5 pushed, 0 deleted, 2 failed (2 snapshots)`), in
what `swarm down` prints, and by `swarm doctor`: its `backup` check reads the
last pass from `<state>/backup.json` and is a WARN after one pass that left
uncommitted work unsaved, a FAIL after two running, and a WARN for a ref that
would not push.

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `every_s` | `1800` | `SWARM_BACKUP_EVERY` | hot | A pass this often, on a supervisor thread, the first one a full interval after start-up. `0` turns the periodic pass off. |
| `on_down` | `true` | `SWARM_BACKUP_ON_DOWN` | hot | `swarm down` (and so a drain) runs a pass once its sessions have ended, for at most about 5 minutes. |

## `[resources]`

The supervisor's resource sampler (see [components.md](components.md#resource-tracking-swarm-resources)
and `swarm resources` in [cli.md](cli.md)). It reads `/proc` and never signals anything.

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `true` | `SWARM_RESOURCES` | hot | Sample the host, each gated build and each swarm session into `<state>/meters/` (one sample a second while a build runs, one every 15 s otherwise). `false` leaves the sampler idle. |
| `idle_s` | `600` | `SWARM_RESOURCES_IDLE` | hot | A heavy build that holds a slot this long with its whole process tree under 1% of a core is an idle holder: `swarm doctor` warns, `swarm status` says so, and one telegram goes out (again hourly while it stays idle). Nothing is killed. At least 60. |
| `vhdx` | `""` | `SWARM_RESOURCES_VHDX` | restart | Under WSL, the distro's virtual disk, used to compute real free space (slack inside the vhdx plus free space on the Windows drive). `""` finds it: the largest `ext4.vhdx` under `/mnt/*/Users/*/AppData/Local/wsl/` (or `…/Packages/*/LocalState/`). Ignored outside WSL. |

## `[web]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `true` | `SWARM_WEB` | restart | Show this swarm on the machine's web board. `swarm up` starts the board when it is not running; `false` keeps this swarm off it, and its `swarm up` starts none. |

The board is the machine's, one for every swarm, so where it listens is not a
project's to say: `host` and `port` are in [`machine.toml`](#web-1). A
`.swarm.toml` that still sets `[web].host` or `[web].port` is refused by every
command, with the key and the machine file named, because the value would
otherwise be dropped in silence. A session of a running swarm keeps working
through that: its commands fall back to the settings the supervisor recorded.

## The machine file: `machine.toml`

Some settings are true of the computer, whichever project asks. They do not
belong in a project's `.swarm.toml`: with several swarms on one machine each
would hold its own copy, and the copies would disagree. They live in one file
for the user who runs the swarms:

    $XDG_CONFIG_HOME/swarm-orchestrator/machine.toml
    (default ~/.config/swarm-orchestrator/machine.toml)

- The file is optional. Without it every key takes its default.
- **Precedence** is the same as for `.swarm.toml`: an environment variable,
  when a key has one and it is set, beats the file, which beats the default.
- **Mistakes are errors.** A table or key this section does not list, a value
  of the wrong type or out of range, or a file that is not TOML stops the
  command that read it, with the file and the key in the message. Nothing falls
  back to a default in silence. This is stricter than `.swarm.toml`, which
  ignores a key it does not know: this file holds the limits that protect the
  machine, and a mistyped key would otherwise leave the default in force with
  nothing to say so.
- It is read again whenever it changes. A swarm that is running follows an
  edit without a reload or a restart: the next time it looks at a limit, it has
  the new one. If the file stops reading as settings while a swarm runs, every
  new command stops with the error, and a process that had read the sound file
  keeps the values it read until the file is fixed.

Each table is listed here under a third-level heading with the table's name and
one row per key (key, default, environment variable, meaning). The keys are
declared once, on the `Settings` class in `src/swarm_orchestrator/machine.py`,
and a test checks this section against that class.

### `[build]`

The build gate: one for the machine. Every heavy `swarm build` of every swarm
queues at it, first come first served, so these limits hold across swarms: with
`max_concurrent = 2` two swarms run two builds between them, not two each. The
gate's files are in the machine directory, `<state root>/machine/buildsem/`.
How it works: [components.md](components.md#build-gate-swarm-build).

None of these keys has an environment variable. A limit one process could
raise for itself would not be the machine's. `SWARM_BUILD_MAX`,
`SWARM_BUILD_SHORT_S`, `SWARM_BUILD_OVERTAKE`, `SWARM_BUILD_IDLE_YIELD_S`,
`SWARM_BUILD_IDLE_YIELD_MAX`, `SWARM_BUILD_PAIR` and `SWARM_BUILD_ALONE` are no
longer read; a session that still carries one is not affected by it.

| key | default | env | meaning |
|---|---|---|---|
| `max_concurrent` | `2` | — | The most heavy `swarm build` commands that run at once on this machine, whichever swarms started them; the rest queue. `0` turns the gate off for every swarm; builds still get their project's jobs cap. |
| `short_s` | `60` | — | A command whose last runs took at most this long (median, from the gate's log of that swarm's own builds) counts as short and may go ahead of long ones. |
| `overtake` | `2` | — | How many short builds may go ahead of one long build that is waiting, whichever swarms they belong to. `0` is plain first-come, first-served. |
| `idle_yield_s` | `150` | — | A build that holds a slot while its whole process tree does nothing (under 5% of a core and next to no disk IO) for this long is set aside: it keeps running, but stops counting against `max_concurrent`, so the next waiting build starts beside it. If it starts working again it counts again. Commands whose work runs in a daemon (`docker build`, `sccache`, `bazel`…) never yield, nor does one started with `swarm build --hold`. A build whose swarm is frozen (`swarm freeze`) does not wait this long: it is set aside at the first quiet sample. `0` turns this off. |
| `idle_yield_max` | `2` | — | The most idle holders set aside at once; a further idle holder keeps its slot and the queue waits, as it would with `idle_yield_s = 0`. The builds alive at one time never exceed `max_concurrent` plus this. `0` also turns idle yield off. |
| `pair` | `"any"` | — | Which builds may run side by side. `"any"`: whatever fits in `max_concurrent`. `"distinct-repo"`: a build starts only if no build alive, of any swarm, is in the same repository, and a build that must run alone (`alone` below, `swarm build --hold`, a build in no git checkout) starts only when no build is alive and keeps the gate to itself. Any other value is an error. |
| `alone` | `["docker", "docker-compose", "docker-buildx", "podman", "podman-compose", "buildah", "nerdctl", "buildctl"]` | — | Under `pair = "distinct-repo"`: command patterns (as for a project's `heavy`) that run with no other build beside them, where the command is heavy. The default is the image-build family: `docker build`, `buildx bake`, `compose build`/`up`, `run`, but not `docker ps` or `bake --print`. A build that turns out to run one of them (a script that could not be read) is alone from that moment. `[]` leaves only the repo rule. |

`max_concurrent` describes the machine. Derive it from the host's cores, memory
and disk (roughly: one build's peak memory times `max_concurrent` must fit with
room to spare, and each project's `jobs` times `max_concurrent` should not
exceed the cores); never copy it from another machine's file.

`pair = "distinct-repo"` is for a machine where two builds are fine but not any
two: the disk, not the cores, is what builds strain. It only ever makes a build
wait. A repository is known by its place on the machine, so the rule holds
between swarms: two swarms whose projects hold the same checkout never build
in it at once, and two projects are never taken for one because each calls its
own repo `.`. A waiter the rules hold back is passed by one they allow, out of
the `overtake` budget (each waiter at most `overtake` times, for whatever
reason), and a build that must run alone is never passed once it is the oldest
waiter; `swarm build --status` says why each waiter waits. The rules count
every build alive, also one set aside as idle: it keeps its repo, and a build
that runs alone waits for it to end. How a repo is told, what "alone" covers
and what a script can hide:
[components.md](components.md#build-gate-swarm-build).

`idle_yield_s` trades waiting for overlap. A set-aside build that wakes up runs
beside whatever started in its place until one of them ends, so for that time
more than `max_concurrent` builds work at once. Size memory for
`max_concurrent + idle_yield_max` builds alive, or lower `idle_yield_max`. The
default window is well above the pauses a working build has (between compile
and test, behind a lock) and well below the holds worth freeing (a script
waiting out a 20-minute timeout); a tool that sits silent for minutes before
it starts real work will yield and then wake, which a longer window avoids.

A swarm that is frozen (`swarm freeze`) keeps its builds' seats: they are alive
and will resume, so nothing takes a seat from them. They are set aside as idle
at once, up to `idle_yield_max` of them, so the other swarms' builds do not
wait on a swarm that stands still; at the thaw they count again, beside
whatever started meanwhile. With `idle_yield_max = 0`, or for a build that
never yields (`--hold`, a daemon's client), a frozen build keeps its slot until
its swarm is thawed or stopped, and `swarm build --status` names it. Its
waiters keep their places in the queue and are passed over until they wake.

### `[web]`

Where the machine's one [web board](components.md#the-web-board-swarm-web)
listens. It is read when the board starts: after an edit, `swarm web stop` and
`swarm web` (or the next `swarm up` once every swarm is down) put it in force.
Neither key has an environment variable: a port one shell could set for itself
would send that shell's commands looking for the board where it is not.
`SWARM_WEB_HOST` and `SWARM_WEB_PORT` are no longer read.

| key | default | env | meaning |
|---|---|---|---|
| `host` | `"0.0.0.0"` | — | The bind address. The default is every interface, so the board answers on the Tailscale IP that `swarm status` and `swarm ls` print (or the LAN address when Tailscale is absent). |
| `port` | `8765` | — | The TCP port, one for every swarm on the machine. At least 1. When another program holds it, `swarm up` and `swarm status` say which, and this is the key to change. |

### `[telegram]`

The machine's one Telegram bot: every swarm on the machine sends through it,
each message named for its swarm, and one listener answers `/status`, `/usage`
and `/help` for all of them ([components.md](components.md#telegram-and-asking-the-owner)).
With neither key set, the bot is the bundled `scripts/notify.sh` of the
installed swarm-orchestrator, with its token and chat id in that checkout's
`.env`: what a single swarm used before the bot was the machine's. A sender
reads the file each time it sends; the listener reads the credentials when it
starts (`swarm telegram-bot stop`, then `swarm telegram-bot`, after an edit).
Neither key has an environment variable: a file one shell named for itself
would have that shell's swarm send from a bot the machine's listener does not
poll. `SWARM_TG_ENV` is no longer read by the swarm; it is what the swarm hands
the script.

| key | default | env | meaning |
|---|---|---|---|
| `notify` | `""` | — | The script that sends, called with the message as `$1` and with `SWARM_TG_ENV` set to the env file below. `""` is the bundled `scripts/notify.sh`. |
| `env` | `""` | — | The file holding `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, in shell `KEY=value` form; the listener reads the same file. `""` is `.env` in the folder above the script's, where the bundled script looks. |

## Environment-only variables

| variable | effect |
|---|---|
| `SWARM_STATE_DIR` | Use this directory as the state dir instead of `$XDG_STATE_HOME/swarm-orchestrator/<slug>`. Every session the swarm starts gets it, and so does the supervisor. Once a supervisor has run there the directory belongs to that project: a command that names any other project while this is set is refused. |
| `XDG_STATE_HOME` | The state root is `$XDG_STATE_HOME/swarm-orchestrator`. Default `~/.local/state`. Every swarm on the machine keeps its state dir there, beside the machine directory `machine/`; `swarm ls` lists them. |
| `XDG_CONFIG_HOME` | The machine file is `$XDG_CONFIG_HOME/swarm-orchestrator/machine.toml`. Default `~/.config`. |
| `SWARM_TG_SINK` | Append telegrams to this file instead of sending them. The tests use it; it also skips recap generation. |
| `SWARM_TG_API` | The Bot API base URL the machine's listener polls (default `https://api.telegram.org`). The tests point it at a fake. It and `SWARM_TG_SINK` are the two `SWARM_*` variables the listener is started with. |
| `SWARM_SUBMIT_SETTLE` | Seconds typed text gets to render before Enter is sent (default 1.5). Raise it on a slow host. |
| `SWARM_LOG_ECHO` | Also echo supervisor log lines to stderr. |
| `SWARM_SCOPE` | `0`: never start the supervisor, the bot's listener, the web board or a lane check in a systemd user scope of its own (see [`swarm freeze`](components.md#freeze-and-thaw-swarm-freeze-swarm-thaw)). The tests set it. |
| `SWARM_CGROUP_ROOT`, `SWARM_CGROUP_PROC` | Where `swarm freeze` and `swarm thaw` find the cgroup tree and `/proc` (defaults `/sys/fs/cgroup` and `/proc`). Test seams. |
| `SWARM_BIN` | The `swarm` command that detached helpers (`recap`, `operator-triage`, the grace poke) run. A test seam. |
| `SWARM_RECAP_CMD`, `SWARM_TRIAGE_CMD` | Replace the model call for recaps and triage with a command that reads the prompt on stdin. Test seams. |
| `SWARM_CLAUDE_CONFIG` | The `~/.claude.json` that the folder-trust pre-seeding writes to. A test seam. |
