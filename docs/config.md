# `.swarm.toml` reference

Every key the loader reads (`src/swarm_orchestrator/config.py`), with its default,
the environment variable that overrides it, and what `swarm reload` does with an
edit to it. The file lives at the project root (or pass `--config`). A missing
table or key takes the default, so a minimal file works.

A key the loader does not read is ignored, so a file that still sets a retired
key (`[worker].done_hook`, `[tasks].roadmap`) loads unchanged.

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
  with `swarm down` and `swarm up`.

A worker's own `swarm done` runs in its mirror under worktree isolation, so it
reads the `.swarm.toml` copy that was branched when the phase launched. That is
why `[operator].enabled`, `triage_model` and `done_grace_s` are "next".

## `[swarm]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `max_workers` | `4` | | hot | Worker slots. Must be at least 1. Growing adds panes live; shrinking marks the extra slots retiring, and a busy one finishes its phase first. |
| `master_model` | `""` | | next | `--model` for the init pass and, by default, the Overseer. `""` inherits the user's setting. |
| `slug` | `<dirname>-<sha1 of path, 8 chars>` | `SWARM_SLUG` | restart | Names the state dir. The hash keeps two projects with the same folder name apart. |
| `driver` | `"tmux"` | `SWARM_DRIVER` | restart | `tmux`, or `bare` (headless subprocesses, no panes; the hermetic tests use it). |
| `master_cmd` | `""` | `SWARM_MASTER_CMD` | next | Replaces the built-in `claude` command in the overseer window (the init pass, and the Overseer when `[overseer].cmd` is empty). With it set, no prompt is typed in. The tests use it to inject a fake master. |
| `resolver_cmd` | `""` | `SWARM_RESOLVER_CMD` | next | Replaces `cd <repo> && exec claude` for the merge-conflict resolver. With it set, no prompt is typed in. |
| `watchdog_s` | `300` | `SWARM_WATCHDOG` | hot | Seconds between the supervisor's liveness sweeps. `0` turns the sweep off and leaves the loop purely event-driven. See the README's watchdog section. |

## `[worker]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `command_template` | `"/prime {phase}"` | | next | The line typed into a worker's pane once `claude` has booted. |
| `command_file` | `".claude/commands/prime.md"` | | next | The project's slash-command file. The init pass patches it for swarm mode, and `swarm check` lints it. |
| `env_marker` | `"SWARM_PHASE"` | | next | The variable that carries the phase id into the worker's environment. |
| `worker_cmd` | `"claude -n worker:{phase}"` | `SWARM_WORKER_CMD` | next | The base command a slot pane is respawned with, as `cd <cwd> && exec <worker_cmd> --settings … --effort …`. |
| `ready_marker` | `""` | `SWARM_READY_MARKER` | next | Text that means "claude has booted". `""` means the running `claude --version`, which the boot banner prints; if that cannot be read, `Claude Code`. |
| `worker_settings` | `'{"teammateMode":"in-process"}'` | `SWARM_WORKER_SETTINGS` | next | JSON merged over the user's settings via `--settings`. The meters status-line tap is added unless this JSON sets its own `statusLine`. Register `scripts/stop-hook.py` here as a `Stop` hook to get recaps (see below). `""` passes no settings. |
| `effort` | `"high"` | `SWARM_WORKER_EFFORT` | next | `claude --effort` for every worker: one of `low`, `medium`, `high`, `xhigh` or `max`. `""` inherits the user's setting. |
| `done_grace_s` | `0` | `SWARM_DONE_GRACE` | next | Seconds a worker keeps its slot after `swarm done` before the supervisor is poked. A detached child sleeps and then delivers the poke, so `swarm done` itself returns at once. |
| `park_after` | `120` | `SWARM_PARK_AFTER` | hot | Seconds a worker may sit in `swarm waiting` before it is parked: its pane moves to `wait:<phase>` and its slot is refilled. `0` disables parking. |

Registering the Stop hook (the path is wherever this repo lives):

```toml
worker_settings = '{"teammateMode":"in-process","hooks":{"Stop":[{"hooks":[{"type":"command","command":"/path/to/swarm-orchestrator/scripts/stop-hook.py","timeout":10}]}]}}'
```

## `[tasks]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `ledger` | `"docs/PHASE-LEDGER.md"` | | hot | The phase ledger, relative to the project root. The launcher parses it on every decision. |
| `exclude` | `[]` | | hot | Phase ids never to launch, for example rows blocked outside the swarm. `swarm why` quotes the comment next to an entry. |

## `[telegram]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `notify` | `<this repo>/scripts/notify.sh` | | hot | The sender. It is called with the message as `$1`. The bundled script reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from this repo's `.env`, or from the file named by `SWARM_TG_ENV`. |

## `[tmux]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `session` | the project folder name, slugified | `SWARM_SESSION` | restart | The tmux session name. |
| `layout` | `"auto"` | `SWARM_LAYOUT` | hot | How worker windows arrange their panes. `auto` gives one pane the full window, puts two side by side, and tiles three or four. The others are `even-horizontal` (aliases `side-by-side`, `left-right`, `columns`), `even-vertical` (aliases `top-bottom`, `stacked`, `rows`), `tiled` (alias `grid`), `main-horizontal` and `main-vertical`. An unknown name fails the load. `swarm layout <name>` changes it live, and a layout set that way wins over a reload until the next `swarm up`. |

## `[tui]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `autostart` | `true` | `SWARM_TUI_AUTOSTART` | restart | Run the dashboard in window 0 at `swarm up`. |
| `cmd` | `"swarm tui"` | `SWARM_TUI_CMD` | restart | The command window 0 runs. |

## `[git]`

Leave the table out for in-place work (`isolation = "none"`).

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `isolation` | `"none"` | `SWARM_GIT_ISOLATION` | restart | `none`: workers commit in the project itself. `worktree`: every phase gets its own full-workspace mirror on `swarm/<phase>`, landed by the merge queue. |
| `main_branch` | `"master"` | `SWARM_GIT_MAIN` | hot, refused while a phase is in flight | The umbrella repo's integration branch. A component repo uses it when the branch exists there, and otherwise the branch it has checked out. |
| `repos` | `["*"]` | `SWARM_GIT_REPOS` (comma list) | hot, growing only | Globs, relative to the project root, that pick the component repos to mirror. Only directories with a `.git` count, and dot-names never match. A single-repo project matches none and gets an umbrella-only mirror. Dropping a repo while a phase is in flight is refused. |
| `auto_resolve` | `{}` | | hot | Path glob mapped to `"union"` or `"keyed:<regex>"`. The integrator tries these before it opens a resolver session (see the README). |

Example:

```toml
[git]
isolation    = "worktree"
main_branch  = "master"
repos        = ["*"]
auto_resolve = { "docs/PHASE-LEDGER.md" = "keyed:^- \\[[ x]\\] `([A-Za-z0-9_.-]+)`", "CHANGELOG.md" = "union" }
```

## `[build]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `max_concurrent` | `2` | `SWARM_BUILD_MAX` | next | The most `swarm build` commands that run at once. `0` turns the gate off; builds still get the jobs cap. |
| `jobs` | `6` | `SWARM_BUILD_JOBS` | next | `CARGO_BUILD_JOBS` for a `cargo` run through `swarm build`, and for every worker under worktree isolation. `0` means no cap. |
| `cache` | `true` | `SWARM_BUILD_CACHE` | next | Point each Rust worktree's `target/` at one shared per-repo cache, `<state>/cache/target/<repo>`. This happens only where the repo gitignores `target`. |

## `[operator]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `false` | `SWARM_OPERATOR` | next | Opt-in. `true` lets the swarm open an unattended session with your authority. While `false`, nothing is queued and each `operator` hand-off is telegrammed to you as a to-do. |
| `cmd` | `""` | `SWARM_OPERATOR_CMD` | next | Replaces the built-in session command. With it set, no brief is typed in. |
| `model` | `""` | | next | `--model` for operator sessions. `""` inherits the user's setting. |
| `triage_model` | `"haiku"` | | next | The model that answers now-or-later for each hand-off. Use an alias, not a dated build. |

## `[overseer]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `true` | `SWARM_OVERSEER` | hot | `false` leaves only launching and integrating. |
| `cmd` | `""` | `SWARM_OVERSEER_CMD` | next | Replaces the built-in session command. Falls back to `[swarm].master_cmd`. |
| `model` | `""` | | next | `""` uses `[swarm].master_model`. |
| `min_gap_s` | `600` | `SWARM_OVERSEER_MIN_GAP` | hot | The minimum time between the starts of two non-urgent passes. |
| `every_finished` | `3` | `SWARM_OVERSEER_EVERY_FINISHED` | hot | Run a pass every N finished phases. `0` turns this off. |
| `every_s` | `10800` | `SWARM_OVERSEER_EVERY` | hot | Run a pass at least this often. `0` turns this off. |
| `owner_wait_s` | `3600` | `SWARM_OVERSEER_OWNER_WAIT` | hot | A phase waiting on you this long triggers a pass, once per phase. |
| `starve_s` | `600` | `SWARM_OVERSEER_STARVE` | hot | Free slots, nothing launchable and backlog still open for this long triggers a pass, once per episode. |
| `timeout_s` | `2700` | `SWARM_OVERSEER_TIMEOUT` | next | A pass still running after this is killed, and whatever it committed is merged. `overseer-ask` stretches the deadline while you are being asked. |

## `[gc]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `auto` | `true` | `SWARM_GC_AUTO` | hot | Let the supervisor run `swarm gc` by itself. |
| `every_s` | `86400` | `SWARM_GC_EVERY` | hot | At most this often. `0` leaves only the idle trigger. |
| `idle_s` | `1800` | `SWARM_GC_IDLE` | hot | Also once per idle stretch (no busy slot) of this length. `0` turns this off. |
| `keep_days` | `3` | `SWARM_GC_KEEP_DAYS` | hot | Build output used within this many days survives `cargo sweep`. |

## `[web]`

| key | default | env | reload | meaning |
|---|---|---|---|---|
| `enabled` | `true` | `SWARM_WEB` | restart | Start the board in the last tmux window at `swarm up`. Under the `bare` driver it runs as a detached process instead. |
| `host` | `"0.0.0.0"` | `SWARM_WEB_HOST` | restart | The bind address. The default is every interface, because the LAN is the point. |
| `port` | `8765` | `SWARM_WEB_PORT` | restart | The TCP port. |

## Environment-only variables

| variable | effect |
|---|---|
| `SWARM_STATE_DIR` | Use this directory as the state dir instead of `$XDG_STATE_HOME/swarm-orchestrator/<slug>`. Every session the swarm starts gets it. |
| `XDG_STATE_HOME` | The state root. Default `~/.local/state`. |
| `SWARM_TG_SINK` | Append telegrams to this file instead of sending them. The tests use it; it also skips recap generation. |
| `SWARM_TG_ENV` | The credentials file the bundled `notify.sh` reads. |
| `SWARM_SUBMIT_SETTLE` | Seconds typed text gets to render before Enter is sent (default 1.5). Raise it on a slow host. |
| `SWARM_LOG_ECHO` | Also echo supervisor log lines to stderr. |
| `SWARM_BIN` | The `swarm` command that detached helpers (`recap`, `operator-triage`, the grace poke) run. A test seam. |
| `SWARM_RECAP_CMD`, `SWARM_TRIAGE_CMD` | Replace the model call for recaps and triage with a command that reads the prompt on stdin. Test seams. |
| `SWARM_CLAUDE_CONFIG` | The `~/.claude.json` that the folder-trust pre-seeding writes to. A test seam. |
