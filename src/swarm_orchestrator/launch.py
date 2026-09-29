"""Worker launch and the ``swarm done`` completion signal.

``launch`` claims a slot check-and-set, then either respawns the slot's tmux
pane (real driver) or spawns a detached process (bare driver used by the
hermetic logic tests). Readiness is *detected* (polling ``capture-pane`` for the
idle marker) rather than slept for. ``done`` is the only thing a worker does to
signal completion: write a durable sentinel, then a best-effort non-blocking
FIFO poke that must never hang the worker if the supervisor is down. It reports
what it did (:class:`DoneResult`) rather than returning in silence — silence is
what made workers re-run it, clobber their own recaps and re-ping the owner.
"""

from __future__ import annotations

import dataclasses
import errno
import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import blockedping
from . import gitq
from . import lanes as lanes_mod
from . import ledger as ledger_mod
from . import ledgerw
from . import meters, opqueue
from . import state as state_mod
from . import statuses
from . import telegram, tmux
from .config import Config, ready_needle
from . import logutil
from .logutil import Log
from .procs import SESSION_ENV

# 30 s was measured too tight once launches stopped queueing behind an LLM
# master: several sessions now boot at the same moment on a loaded box, and a
# cold claude start under a parallel cargo build overran it. A boot that misses
# the window is also retried once (:func:`_launch_tmux`) before the launch fails.
READY_TIMEOUT_S = 45.0
POLL_INTERVAL_S = 0.25

# What :func:`launch_outcome` reports. ``denied`` never claimed a slot (paused,
# no free slot, the phase already in flight, unmet deps); ``failed`` claimed one
# and could not start the worker, and has already rolled the claim back. The
# supervisor retries only ``failed`` -- a denial is the state machine working.
LAUNCHED = "launched"
DENIED = "denied"
FAILED = "failed"

# `swarm done` statuses that SATISFY a dependent phase's `needs:`. A `fail` is a
# *recorded outcome*, not a completed dependency — its work was rolled back, so
# anything that needed it must stay blocked (before this, membership alone
# counted and a failed phase silently unblocked everything downstream of it).
# Byte-identical to the resolver's set, so it IS the resolver's set now.
DEP_SATISFYING = statuses.SATISFIES_DEPS

# What counts as a recap someone can act on. It guarded the owner's phone, where
# probe-grade notes — "test", "recheck" — arrived as real alerts; it now guards
# the operator session, whose entire brief is this one note. Below either floor
# the call still writes its sentinel (durability is never traded for politeness);
# it just does not dispatch.
MIN_RECAP_CHARS = 20
MIN_RECAP_WORDS = 4
# Substrings that identify claude's workspace-trust dialog (matched
# case-insensitively against joined pane text, so wrapping never hides them).
TRUST_MARKERS = ("do you trust", "trust the files", "trust the authors")


def _claude_config_path() -> Path:
    """Where claude stores per-directory trust (`~/.claude.json`).

    Overridable via ``SWARM_CLAUDE_CONFIG`` so tests never touch the real file.
    """
    override = os.environ.get("SWARM_CLAUDE_CONFIG")
    return Path(override).expanduser() if override else Path.home() / ".claude.json"


#: How often :func:`pretrust_dir` re-reads a ``~/.claude.json`` it could not
#: parse, or that changed under it, before it gives up; and the pause between.
PRETRUST_TRIES = 5
PRETRUST_RETRY_S = 0.2


def _read_claude_config(cfg_path: Path) -> tuple[dict, tuple[int, int] | None] | None:
    """``(data, (mtime_ns, size))``, ``({}, None)`` for a file that does not
    exist, or None when it cannot be read or is not a JSON object with a
    ``projects`` object (a torn read while claude rewrites it looks like that)."""
    try:
        before = cfg_path.stat()
    except FileNotFoundError:
        return {}, None
    except OSError:
        return None
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("projects", {}), dict):
        return None
    return data, (before.st_mtime_ns, before.st_size)


def _stamp(cfg_path: Path) -> tuple[int, int] | None:
    try:
        st = cfg_path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def pretrust_dir(path: Path, log: Log) -> None:
    """Mark ``path`` trusted in claude's config so no folder-trust dialog appears.

    Worktree mode launches every worker in a brand-new directory claude has never
    seen, which would otherwise pop the "Do you trust the files in this folder?"
    dialog for *every* phase. We pre-seed
    ``projects[<path>].hasTrustDialogAccepted = true`` before the pane starts.
    Additive (never drops other projects), written atomically so a concurrent
    claude write can't see a half file. A no-op if already trusted.

    The file is shared with every live claude session and holds the owner's
    login and settings, so it is only ever written back from a clean read: one
    that cannot be parsed, or that changed between the read and the write, is
    read again a few times and otherwise left alone (the dialog then appears,
    which is harmless).
    """
    cfg_path = _claude_config_path()
    key = str(path.resolve())
    for attempt in range(PRETRUST_TRIES):
        if attempt:
            time.sleep(PRETRUST_RETRY_S)
        got = _read_claude_config(cfg_path)
        if got is None:
            continue
        data, stamp = got
        projects = data.setdefault("projects", {})
        entry = projects.get(key)
        if isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True:
            return  # already trusted -> don't rewrite (avoids racing a live claude)
        if not isinstance(entry, dict):
            entry = projects[key] = {}
        entry["hasTrustDialogAccepted"] = True
        entry.setdefault("hasCompletedProjectOnboarding", True)
        tmp = cfg_path.with_name(cfg_path.name + ".swarm-tmp")
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            if _stamp(cfg_path) != stamp:
                tmp.unlink(missing_ok=True)
                continue  # claude wrote it meanwhile; start over from its version
            os.replace(tmp, cfg_path)
        except OSError as exc:
            log.line(f"PRETRUST-FAIL {key} {exc}")
            return
        log.line(f"PRETRUST {key}")
        return
    log.line(f"PRETRUST-SKIPPED {key} {cfg_path} unreadable or changing; left untouched")


def _worker_shell(cfg: Config, phase: str, cwd: Path, cmd: str | None = None) -> str:
    """``cd <cwd> && exec <claude ...>`` for one session, worker-configured.

    ``cmd`` overrides the ``worker_cmd`` base for a session that is not a phase
    worker — the operator runs its own model — while keeping everything else a
    worker gets: in-process teammates, the meters tap and the effort level. One
    builder, so the two sessions can never drift apart on those.
    """
    cmd = cmd or cfg.worker_cmd.format(phase=phase)
    if cfg.worker_settings:
        # Force in-process teammates: a worker's own subagents then never open
        # extra tmux panes in the workers window. Merges over the user's
        # settings, so bypassPermissions etc. are preserved. The status line is
        # swapped for the meters tap, which still draws the owner's own bar.
        settings = meters.settings_with_tap(cfg.worker_settings, cfg.state_dir, phase)
        cmd += f" --settings {shlex.quote(settings)}"
    if cfg.worker_effort:
        cmd += f" --effort {shlex.quote(cfg.worker_effort)}"
    return f"cd {shlex.quote(str(cwd))} && exec {cmd}"


def _worker_env(
    cfg: Config, phase: str, worktree: Path | None = None
) -> dict[str, str]:
    """Env vars a worker (and its ``swarm done``) need to find this run: the
    phase marker plus everything :func:`session_env` gives any session, and with
    lanes on ``SWARM_TOUCHES``, the lane it was launched with, space-separated."""
    env = {cfg.env_marker: phase,
           **session_env(cfg, worktree, tmp=phase, session=f"worker:{phase}")}
    if cfg.lanes_enabled:
        env["SWARM_TOUCHES"] = " ".join(state_mod.read(cfg).lanes.get(phase, []))
    return env


def tmp_env(cfg: Config, name: str) -> dict[str, str]:
    """``TMPDIR``/``TMP``/``TEMP`` pointing at the session's own on-disk temp dir.

    ``/tmp`` can be a small tmpfs, i.e. RAM: a worker's ad-hoc
    ``CARGO_TARGET_DIR=/tmp/...`` plus Claude Code's own diff cache under
    ``/tmp/claude-<uid>`` can fill it and push the host into swap. Claude Code,
    cargo, rustc and Python all honour ``TMPDIR``; ``TMP``/``TEMP`` are set to the
    same place so nothing that reads one of those instead lands back in RAM. One
    dir per session so it can be dropped whole when the session's work lands
    (:func:`drop_session_tmp`, ``gitq._rmtree_mirror``) and so ``swarm gc`` can
    tell whose it is. Empty when the directory cannot be made: a session on the
    default ``/tmp`` beats a session that does not start.
    """
    path = cfg.session_tmp(name)
    if path is None:
        return {}
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {}
    return {"TMPDIR": str(path), "TMP": str(path), "TEMP": str(path)}


def drop_session_tmp(cfg: Config, name: str) -> None:
    """Remove a finished session's temp dir (best effort, idempotent)."""
    path = cfg.session_tmp(name)
    if path is not None:
        shutil.rmtree(path, ignore_errors=True)


def session_env(
    cfg: Config, worktree: Path | None = None, tmp: str | None = None,
    session: str | None = None,
) -> dict[str, str]:
    """Env vars any swarm session needs so ``swarm`` inside it finds this run.

    Shared by workers and the operator. The phase marker is deliberately NOT
    here: it names the phase a *worker* builds, and an operator carrying it
    would look like that phase's worker to anything keyed on it.

    Under ``isolation = worktree`` the worker also gets ``SWARM_WORKTREE`` (its
    cwd — a full isolated mirror of the whole workspace on branch
    ``swarm/<phase>``, umbrella + every component repo at its real path),
    ``SWARM_MAIN`` (the integration target branch) and ``SWARM_PROJECT`` (the
    canonical project path). The worker just works inside the mirror as if it
    were the real project; the integrator merges whatever repos it changed.

    ``CARGO_INCREMENTAL=0`` because incremental state is pure dead weight here:
    every phase builds a *different* source tree against one shared ``target/``,
    so no phase can ever reuse another's incremental cache — it only fills the disk
    with dead weight. An explicit ``CARGO_INCREMENTAL`` in the environment still
    wins (``setdefault`` over the inherited value).

    ``tmp`` names the session's own ``TMPDIR`` (:func:`tmp_env`): the phase for a
    worker, the mirror name for the operator and the Overseer.

    ``session`` is the session's own marker (``SWARM_SESSION_ID``, e.g.
    ``worker:<phase>``): everything the session starts inherits it, and when the
    session ends whatever still carries it is ended (:func:`session.reap_session`).
    """
    env = {"SWARM_STATE_DIR": str(cfg.state_dir)}
    if session:
        env[SESSION_ENV] = session
    if tmp:
        env.update(tmp_env(cfg, tmp))
    env.setdefault("CARGO_INCREMENTAL", os.environ.get("CARGO_INCREMENTAL") or "0")
    for key in ("SWARM_SLUG", "SWARM_TG_SINK", "SWARM_BIN", "SWARM_DRIVER"):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    if worktree is not None:
        env["SWARM_WORKTREE"] = str(worktree)
        env["SWARM_MAIN"] = cfg.git_main_branch
        env["SWARM_PROJECT"] = str(cfg.project_dir)
        # Worktree mode only (the parallel-build OOM problem is worktree-specific):
        # carry the build-gate config so `swarm build` honours it from a worktree
        # cwd (no .swarm.toml there), and cap raw `cargo` fan-out as a backstop for
        # any build not routed through `swarm build`. In-place (`isolation="none"`)
        # workers keep their full build parallelism untouched.
        env["SWARM_BUILD_MAX"] = str(cfg.build_max_concurrent)
        env["SWARM_BUILD_JOBS"] = str(cfg.build_jobs)
        if cfg.build_jobs:
            env["CARGO_BUILD_JOBS"] = str(cfg.build_jobs)
    return env


def _unmet_deps(
    cfg: Config, phase: str, done: dict[str, str], in_flight=frozenset()
) -> list[str]:
    """Known-phase deps of ``phase`` not satisfied by ``done`` (launch backstop).

    A dep counts only when its recorded *status* is in :data:`DEP_SATISFYING` —
    membership in ``done`` is not enough. A ``fail`` recorded the phase's work as
    rolled back, so a dependent built on top of it would be building on nothing.
    A dep ticked ``[x]`` in the ledger that the swarm has no record of counts as
    landed (:func:`ledger.with_ticked`), unless it is ``in_flight``.

    Resolves the same ledger the master reasons over (``cfg.project_dir /
    cfg.ledger``). Best-effort: an empty list — never a block — is returned when
    the ledger can't be loaded or ``phase`` isn't a declared phase, so a project
    with no machine ledger (or a phase the resolver doesn't know) launches exactly
    as before.
    """
    path = cfg.project_dir / cfg.ledger
    graph = ledger_mod.load(path)
    if phase not in graph:
        return []
    done = ledger_mod.with_ticked(done, ledger_mod.load_ticked(path), in_flight)
    return sorted(d for d in graph[phase] if done.get(d) not in DEP_SATISFYING)


def known_lanes(cfg: Config) -> frozenset[str]:
    """The lanes a touch may start with: every component repo, the
    project repo ``.``, each ``[lanes] external`` repo and ``@<name>`` for each
    ``[lanes] resources`` entry. Repos are globbed as a mirror would glob them
    whatever the isolation: a lane names a repo, not a mirror of it."""
    repos = gitq.discovered_repos(dataclasses.replace(cfg, git_isolation="worktree"))
    return frozenset(
        {p.name for p in repos} | {"."} | set(cfg.lanes_external)
        | {f"@{r}" for r in cfg.lanes_resources}
    )


@dataclass(frozen=True)
class LaneView:
    """The ledger's lanes and the ones in flight, read together."""

    rows: dict[str, frozenset[lanes_mod.Touch]]  # each row's lane; a lane-less row is absent
    held: dict[str, frozenset[lanes_mod.Touch]]  # each in-flight phase's lane
    issues: list[str]  # rows whose touches did not parse
    ledger: frozenset[str]  # every row id, lane-less ones included


def _revive(snapshot: list[str] | None) -> frozenset[lanes_mod.Touch] | None:
    """A snapshot read back. Each touch is checked against its own lane only: a
    repo that left the config since launch must not free a lane still in use."""
    if not snapshot:
        return None
    try:
        return frozenset(
            lanes_mod.parse_touch(t, {t.split("/", 1)[0]}) for t in snapshot
        )
    except lanes_mod.LaneError:
        return None


def lane_view(cfg: Config, st: state_mod.State) -> LaneView:
    """Every row's lane, and the lane each phase in flight holds: a busy slot, a
    worker waiting on the owner or parked, or finished work waiting to merge.
    Its snapshot when there is one, else its row's lane now, else its whole home
    repo — a phase in flight always holds something."""
    text = _ledger_text(cfg)
    issues: list[str] = []
    rows = ledger_mod.lanes(text, known_lanes(cfg), issues)
    row_dirs = ledger_mod.dirs(text)
    held = {
        phase: (_revive(st.lanes.get(phase)) or rows.get(phase)
                or lanes_mod.legacy(ledger_mod.home(phase, row_dirs)))
        for phase in {*st.claimed_phases(), *st.integrating()}
    }
    return LaneView(rows, held, issues, frozenset(ledger_mod.parse(text)))


def _lane_busy(cfg: Config, st: state_mod.State, phase: str) -> str | None:
    """The lane backstop, under the claiming flock: record ``phase``'s lane, or
    say why it may not launch (its touches do not parse, or a phase in flight
    holds an overlapping touch — two launch threads raced past the scheduler)."""
    view = lane_view(cfg, st)
    held = {p: lane for p, lane in view.held.items() if p != phase}
    lane = view.rows.get(phase)
    if lane is None:
        if phase in view.ledger:
            return "lane-invalid"
        lane = lanes_mod.legacy(ledger_mod.home(phase, {}))
    for holder in sorted(held):
        pair = lanes_mod.collide(lane, held[holder])
        if pair is not None:
            return f"lane-busy [{holder} {pair[1]}]"
    st.lanes[phase] = sorted(str(t) for t in lane)
    return None


def _ledger_text(cfg: Config) -> str:
    try:
        return (cfg.project_dir / cfg.ledger).read_text(encoding="utf-8")
    except OSError:
        return ""


#: A failed start: the owner hears about it once per phase, not on every retry.
_LAUNCH_FAIL_KINDS = ("worktree-fail", "spawn-fail")


def _launch_fail_hold(cfg: Config, phase: str) -> str | None:
    """Why a failed start of ``phase`` is not pinged again, or ``None`` to ping."""
    if not telegram.already_sent(cfg.state_dir, _LAUNCH_FAIL_KINDS, phase):
        return None
    return telegram.hold(cfg, "a start of this phase already failed and you were told")


def launch(cfg: Config, phase: str, log: Log) -> bool:
    """Claim a slot and start a worker for ``phase``. Returns success.

    The ``swarm launch`` form: a denial is also printed, because the caller is a
    person (or a session) at a terminal. See :func:`launch_outcome`."""
    return launch_outcome(cfg, phase, log) == LAUNCHED


def launch_outcome(cfg: Config, phase: str, log: Log, *, quiet: bool = False) -> str:
    """Claim a slot and start a worker for ``phase``; return what happened.

    One of :data:`LAUNCHED`, :data:`DENIED` or :data:`FAILED`. ``quiet``
    suppresses the stdout line a denial prints for a terminal caller: the
    supervisor launches from its own loop, where stdout is nobody's, and the
    reason is in the log either way.

    Under ``isolation = worktree`` a full-workspace mirror on ``swarm/<phase>``
    is created before the pane is respawned; the branch and umbrella worktree
    path (both deterministic from the phase) are recorded on the slot *in the
    claiming transaction*, so a crash between claim and worktree creation can
    still be reconciled/freed. Concurrent phases in the same repo are fine —
    each has its own worktree/branch — so there is no per-repo launch gate.
    """
    with state_mod.transaction(cfg) as st:
        if st.paused:
            log.line(f"LAUNCH-DENIED {phase} paused")
            return DENIED
        if st.usage_hold:
            log.line(f"LAUNCH-DENIED {phase} usage-cap")
            return DENIED
        if st.drain:
            log.line(f"LAUNCH-DENIED {phase} draining")
            return DENIED
        if phase in st.integrating():
            log.line(f"LAUNCH-DENIED {phase} its finished work is waiting to merge")
            if not quiet:
                print(f"LAUNCH-DENIED {phase}: its finished work is waiting to merge"
                      f" (`swarm resolved {phase}` once the hold is cleared)")
            return DENIED
        slot = st.claim_slot(phase)
        if slot is None:
            log.line(f"LAUNCH-DENIED {phase} no-free-slot")
            return DENIED
        # Dependency backstop: st.done is written only after a phase's worktree is
        # merged, so gating on it means a phase can never start before every dep it
        # depends on has merged — even if the LLM master mis-reasons over the prose
        # ledger and asks to launch it out of order. Undo the just-claimed slot
        # under the same flock so it isn't stranded.
        in_flight = {s.phase for s in st.busy_slots() if s.phase} | set(st.parked) | set(st.waiting)
        missing = _unmet_deps(cfg, phase, st.done, in_flight)
        if missing:
            st.free_slot_for(phase)
            detail = " ".join(missing)
            log.line(f"LAUNCH-DENIED {phase} unmet-deps [{detail}]")
            if not quiet:
                print(f"LAUNCH-DENIED {phase}: unmet deps [{detail}]")
            return DENIED
        # Lane backstop, beside the dependency one and under the
        # same flock: the scheduler already kept overlapping rows apart, but two
        # launch threads can race past it. On success this records the lane.
        busy = _lane_busy(cfg, st, phase) if cfg.lanes_enabled else None
        if busy:
            st.free_slot_for(phase)
            log.line(f"LAUNCH-DENIED {phase} {busy}")
            if not quiet:
                print(f"LAUNCH-DENIED {phase}: {busy}")
            return DENIED
        sid, pane = slot.id, slot.pane_id
        if cfg.git_isolation == "worktree":
            slot.branch = f"swarm/{phase}"
            slot.worktree = str(cfg.wt_dir / phase)
    log.line(f"CLAIM {phase} slot={sid}")

    worktree: Path | None = None
    if cfg.git_isolation == "worktree":
        try:
            worktree = gitq.worktree_add(cfg, phase, log)
        except gitq.GitError as exc:
            with state_mod.transaction(cfg) as st:
                st.free_slot_for(phase)
                st.release_lane(phase)
            logutil.run_ended(log, phase, "launch-failed")
            telegram.notify(
                cfg.telegram_notify,
                f"swarm: could not set up a workspace for {phase}, so its worker did not"
                f" start ({exc}). The swarm tries again shortly and tells you if it"
                " keeps failing.",
                kind="worktree-fail",
                phase=phase,
                source="launch.launch",
                state_dir=cfg.state_dir,
                suppressed=_launch_fail_hold(cfg, phase),
            )
            log.line(f"WORKTREE-FAIL {phase} {exc}")
            return FAILED
        # Pre-trust the fresh worktree so claude never pops the folder-trust dialog.
        pretrust_dir(worktree, log)

    if cfg.driver == "bare":
        ok = _launch_bare(cfg, phase, worktree, log)
    else:
        ok = _launch_tmux(cfg, phase, pane, worktree, log)

    if not ok:
        # Kill the half-started claude BEFORE dropping its worktree, so we never
        # yank the cwd out from under a live process (which would strand an
        # orphaned claude in a deleted directory).
        if cfg.driver != "bare" and pane is not None:
            try:
                tmux.respawn_pane(pane, "exec sleep infinity")
            except subprocess.CalledProcessError:
                pass  # the pane is gone: nothing left running in the worktree
        with state_mod.transaction(cfg) as st:
            st.free_slot_for(phase)
            st.release_lane(phase)
        logutil.run_ended(log, phase, "launch-failed")
        if worktree is not None:
            # An empty mirror goes; one an earlier attempt left work in stays.
            gitq.set_aside(cfg, phase, log)
        telegram.notify(
            cfg.telegram_notify,
            f"swarm: the worker for {phase} failed to start. The swarm tries again"
            " shortly and tells you if it keeps failing.",
            kind="spawn-fail",
            phase=phase,
            source="launch.launch",
            state_dir=cfg.state_dir,
            suppressed=_launch_fail_hold(cfg, phase),
        )
        log.line(f"LAUNCH-FAIL {phase} slot={sid}")
        return FAILED
    log.line(f"LAUNCH {phase} slot={sid}")
    return LAUNCHED


def _launch_bare(cfg: Config, phase: str, worktree: Path | None, log: Log) -> bool:
    """Spawn a detached worker process (no tmux)."""
    cwd = worktree or cfg.project_dir
    env = {**os.environ, **_worker_env(cfg, phase, worktree)}
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", _worker_shell(cfg, phase, cwd)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError as exc:
        log.line(f"LAUNCH-SPAWN-ERROR {phase} {exc}")
        return False


def _launch_tmux(
    cfg: Config, phase: str, pane: str | None, worktree: Path | None, log: Log
) -> bool:
    """Respawn the slot pane, detect readiness, drive the worker command.

    A boot that misses :data:`READY_TIMEOUT_S` is respawned and awaited once
    more before the launch fails. A slow boot on a loaded box is the common
    cause, and the alternative -- fail, discard a freshly built 13-repo mirror,
    telegram the owner and rebuild it all on the next launch -- costs far more
    than a second wait. A pane tmux no longer knows is a failed launch (rolled
    back by the caller), not an exception that strands the claimed slot."""
    if pane is None:
        log.line(f"LAUNCH-FAIL {phase} no-pane")
        return False
    cwd = worktree or cfg.project_dir
    shell, env = _worker_shell(cfg, phase, cwd), _worker_env(cfg, phase, worktree)
    for attempt in (1, 2):
        try:
            tmux.respawn_pane(pane, shell, env=env)
        except subprocess.CalledProcessError as exc:
            log.line(f"LAUNCH-FAIL {phase} respawn pane={pane} {exc}")
            return False
        if _await_ready(cfg, pane, log):
            break
        if attempt == 2:
            return False
        log.line(f"READY-RETRY {phase} pane={pane}")
    command = cfg.command_template.format(phase=phase)
    if not tmux.send_submit(pane, command):
        log.line(f"SUBMIT-LOST {phase} pane={pane}")
        return False
    return True


def _trust_prompt_showing(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in TRUST_MARKERS)


def await_ready(cfg: Config, pane: str, log: Log) -> bool:
    """Wait until claude has booted (its version banner shows), dismissing the
    folder-trust dialog if it appears. Shared by worker, master, and resolver
    pane launches. Returns False on timeout (a launch failure to the caller).

    The trust dialog is checked on **every** poll (not latched) and re-accepted
    with Enter each time it shows, and readiness is never declared while it is up
    — so a dialog that renders late, re-prompts, or wraps can't be mistaken for a
    ready pane. (Belt-and-suspenders: :func:`pretrust_dir` normally stops the
    dialog from ever appearing.)"""
    needle = ready_needle(cfg)
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        text = tmux.capture_joined(pane)
        if _trust_prompt_showing(text):
            tmux.send_enter(pane)  # accept (default = trust); re-sent if it lingers
            time.sleep(POLL_INTERVAL_S)
            continue  # never 'ready' while the dialog is up
        if needle in text:
            return True
        time.sleep(POLL_INTERVAL_S)
    log.line(f"READY-TIMEOUT pane={pane}")
    return False


# Backwards-compatible aliases.
_await_ready = await_ready
TRUST_PROMPT = TRUST_MARKERS[1]


def _collapse(note: str) -> str:
    """Trim + collapse a free-text recap to its comparable, sendable form."""
    return " ".join(note.split())


def _sentinel_note(cfg: Config, phase: str, status: str) -> str | None:
    """The recap already recorded for ``phase``/``status``, or ``None`` if there
    is no sentinel yet. A sentinel is one line: ``<phase> <status> <note>``."""
    try:
        body = (cfg.done_dir / f"{phase}.{status}").read_text(encoding="utf-8")
    except OSError:
        return None
    parts = body.strip("\n").split(" ", 2)
    return parts[2] if len(parts) > 2 else ""


def _write_sentinel(
    cfg: Config, phase: str, status: str, note: str, force: bool = False
) -> str:
    """Write ``done/<phase>.<status>``; return the verdict.

    ``written`` (nothing there, or the incoming recap is at least as informative),
    ``refused`` (a fuller recap is already on disk and was kept), or ``forced``
    (``--force`` deliberately replaced a fuller recap).

    This was an unconditional ``os.replace``, so a second ``swarm done`` carrying
    a throwaway note (a one-word probe, say) destroyed the real recap the owner
    needed. Only a *downgrade*
    is refused: an equal-or-fuller note always overwrites, and an empty recorded
    note is never fuller than anything, so a first real recap always lands. The
    full history of every attempt goes to ``done/<phase>.jsonl`` either way, so
    even a refused note is recoverable.
    """
    cfg.done_dir.mkdir(parents=True, exist_ok=True)
    recorded = _sentinel_note(cfg, phase, status)
    if recorded is not None and len(_collapse(note)) < len(_collapse(recorded)):
        if not force:
            return "refused"
        verdict = "forced"
    else:
        verdict = "written"
    dest = cfg.done_dir / f"{phase}.{status}"
    tmp = cfg.done_dir / f".{phase}.{status}.tmp"
    tmp.write_text(f"{phase} {status} {note}\n", encoding="utf-8")
    os.replace(tmp, dest)
    return verdict


def _append_history(
    cfg: Config, phase: str, status: str, note: str, verdict: str, fresh: bool = True
) -> None:
    """Append this ``swarm done`` attempt to ``done/<phase>.jsonl``.

    Every invocation, whatever the verdict — the sentinel keeps one recap, this
    keeps them all (including the ones the refusal above rejected), so nothing a
    worker ever reported is truly lost and downstream tooling can see how many
    times a phase signalled done. Best-effort: a diagnostics file must never fail
    the completion signal.
    """
    # `fresh`: no sentinel of this status was there before, so this call opens
    # a new episode (a first finish, or one after `swarm retry` removed the
    # sentinel) rather than repeating one.
    row = {"ts": time.time(), "status": status, "note": note, "verdict": verdict,
           "fresh": fresh}
    try:
        cfg.done_dir.mkdir(parents=True, exist_ok=True)
        with (cfg.done_dir / f"{phase}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _fail_episode(cfg: Config, phase: str, fresh: bool) -> int:
    """Which failure of ``phase`` this one is: 1 the first, 2 after one retry, ….

    Counted from ``done/<phase>.jsonl`` before this call is appended. A re-run of
    ``swarm done`` inside one attempt is not fresh (its sentinel is still there),
    so it does not count again. A row from before ``fresh`` was recorded counts
    as an episode: over-counting only ever sends a ping, never loses one.
    """
    episodes = 0
    try:
        lines = (cfg.done_dir / f"{phase}.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("status") == statuses.FAIL and row.get("fresh", True):
            episodes += 1
    return episodes + (1 if fresh else 0)


def _log_poke_drop(cfg: Config, detail: str) -> None:
    """Record a dropped FIFO poke centrally (best-effort; never raises)."""
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        with cfg.supervisor_log.open("a", encoding="utf-8") as fh:
            fh.write(f"POKE-DROP {detail}\n")
    except OSError:
        pass


def _poke_fifo(cfg: Config, line: str) -> bool:
    """Non-blocking one-line FIFO poke. Never hangs the caller.

    A missing FIFO or ``ENXIO`` (no reader attached) is the expected
    supervisor-down case and is skipped silently. Any *other* write error is
    unexpected and logged to the supervisor log (no retry, no backstop) rather
    than swallowed, so a genuinely dropped event is diagnosable.
    """
    if not cfg.fifo_path.exists():
        return False
    try:
        fd = os.open(cfg.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ENXIO:  # no reader attached
            return False
        _log_poke_drop(cfg, f"open {line.strip()}: {exc}")
        return False
    try:
        os.write(fd, line.encode("utf-8"))
        return True
    except OSError as exc:
        _log_poke_drop(cfg, f"write {line.strip()}: {exc}")
        return False
    finally:
        os.close(fd)


def _completion_ping(phase: str, status: str, note: str) -> str | None:
    """The owner telegram for a finishing worker, or ``None`` for no ping.

    Exactly one status reaches the owner's phone now (:data:`statuses.PINGS`):
    ``fail``, which rolled its work back and blocks every dependent. ``ok`` was
    always a silent success, and ``operator`` — the finish that leaves a concrete
    action behind — opens a session for it instead, which is the whole point of
    replacing ``needs-owner``: a phone ping is easy to miss, a session is not. The
    ping carries the worker's one-line ``note`` recap so the owner sees *why*
    without opening the pane; an empty/whitespace recap just drops the dash.
    """
    if status not in statuses.PINGS:
        return None
    recap = _collapse(note)
    tail = f" — {recap.rstrip('.')}" if recap else ""
    return (f"swarm: {phase} FAILED{tail}. The phases that depend on it wait; once the"
            f" cause is fixed, `swarm retry {phase}` puts it back in play.")


def _thin_recap(note: str) -> bool:
    """Is this note too thin to brief an operator session on? (see MIN_RECAP_*)"""
    recap = _collapse(note)
    return len(recap) < MIN_RECAP_CHARS or len(recap.split()) < MIN_RECAP_WORDS


@dataclass
class Outcome:
    """The two things a finish can trigger, decided on two separate axes.

    They used to be one question — "does this reach the owner?" — because every
    status that carried an action for a human also telegrammed one. ``operator``
    breaks that: it carries an action and telegrams nobody, handing the action to
    a session instead. So the ping and the route are decided independently, and
    a status may answer yes to either, both or neither.
    """

    ping: str  # send | skipped | deduped
    ping_detail: str
    route: str  # dispatch | skipped
    route_detail: str


def _ping_decision(
    phase: str, status: str, note: str, recorded: str | None, verdict: str, force: bool
) -> tuple[str, str]:
    """Whether the owner's phone rings, as ``(plan, detail)``.

    ``plan`` is ``send``/``skipped``/``deduped``; ``detail`` explains a non-send
    in the words the CLI prints.

    This has to happen here, worker-side. The supervisor's DONE-DUPLICATE guard
    runs *after* the FIFO poke — downstream of the send — so it is architecturally
    incapable of suppressing a duplicate ping, and a phase that ran ``swarm done``
    three times would telegram the owner three times. ``recorded`` is
    the recap on disk *before* this call rewrote it.
    """
    if _completion_ping(phase, status, note) is None:
        why = (
            "hands off to a session, not the owner's phone"
            if status in statuses.ROUTES else "is a silent success"
        )
        return "skipped", f"`{status}` {why}"
    if force:
        return "send", ""
    if verdict == "refused":
        return "deduped", "a fuller recap for this phase was already sent"
    if recorded is not None and _collapse(recorded) == _collapse(note):
        return "deduped", "identical recap already recorded — the owner has it"
    # Whatever survives to here is a `fail`, and a failure ALWAYS pings, however
    # thin the note: it blocks every dependent and stays filtered out of `ready`
    # until someone runs `swarm retry`, so silence would turn a bad recap into an
    # invisible dead run. The recap floor moved to the route axis for that reason.
    return "send", ""


def _route_decision(
    phase: str, status: str, note: str, enabled: bool = True
) -> tuple[str, str]:
    """Where this finish's hand-off goes, as ``(plan, detail)``.

    ``dispatch`` queues it for an operator session, ``owner`` telegrams it to the
    owner as a to-do, ``skipped`` sends it nowhere.

    Only :data:`statuses.ROUTES` routes at all, and only with a recap worth
    reading: an ``operator`` recap IS the session's entire brief — nothing else is
    handed over — so a probe-grade note would start a session that cannot know
    what it was started for. ``--force`` deliberately does not override that;
    forcing a thin note replaces a recap, it does not create the missing brief.

    ``enabled`` is ``[operator].enabled``. It is honoured here because ignoring it
    would drop hand-offs silently: the queue would refuse to store them, nothing
    would telegram anyone, and ``swarm done`` would still promise each worker an
    operator session. With no operator to run it, the owner is the only one left who can, so the
    note goes to their phone instead of nowhere.
    """
    if status not in statuses.ROUTES:
        return "skipped", f"`{status}` leaves nothing for a session to pick up"
    if _thin_recap(note):
        return "skipped", (
            f"recap too thin (needs >= {MIN_RECAP_CHARS} chars and"
            f" >= {MIN_RECAP_WORDS} words) and it is the session's whole brief —"
            f' re-run with a real recap: swarm done {phase} {status}'
            ' "<what you did, what is left to do>"'
        )
    if not enabled:
        return "owner", "the operator is off ([operator].enabled = false)"
    return "dispatch", ""


def _outcome_plan(
    phase: str,
    status: str,
    note: str,
    recorded: str | None,
    verdict: str,
    force: bool,
    enabled: bool = True,
) -> Outcome:
    """Decide, BEFORE anything is sent, what this finish triggers."""
    ping, ping_detail = _ping_decision(phase, status, note, recorded, verdict, force)
    route, route_detail = _route_decision(phase, status, note, enabled)
    return Outcome(ping, ping_detail, route, route_detail)


def _todo_ping(phase: str, note: str) -> str:
    """The owner's to-do for a hand-off no operator will run. Plain words: which
    phase, and the action — the recap is already written as one."""
    return (f"swarm: a to-do for you from {phase} (the operator is switched off, so"
            f" nobody else will do it): {_collapse(note)}")


def _send_todo(
    cfg: Config, phase: str, note: str, recorded: str | None, verdict: str
) -> tuple[str, str]:
    """Telegram an ``owner``-routed hand-off; ``(route, detail)`` for the report.

    Deduplicated on the same evidence as the completion ping: a refused verdict
    kept a fuller recap that already went out, and an identical recorded one
    means this is a re-run of a ``swarm done`` the owner has already heard.
    """
    if verdict == "refused" or (
        recorded is not None and _collapse(recorded) == _collapse(note)
    ):
        return "owner", "already telegrammed to the owner as a to-do"
    sent = telegram.notify_detail(
        cfg.telegram_notify,
        _todo_ping(phase, note),
        kind="operator-todo",
        phase=phase,
        source="launch.done",
        state_dir=cfg.state_dir,
    )
    if not sent.delivered:
        return "owner", f"the to-do telegram FAILED: {sent.error or 'unknown error'}"
    return "owner", "telegrammed to the owner as a to-do"


@dataclass
class DoneResult:
    """What ``swarm done`` actually did, so the CLI can say it out loud.

    ``done`` used to return ``None`` and print nothing — the only state-changing
    command that said nothing at all. Workers read that silence as success, so some
    sessions ran it again (some repeatedly), and a refusal, a
    dropped telegram and a lost poke were all indistinguishable from a clean
    finish. Every branch is now reportable.
    """

    phase: str
    status: str  # canonical; what the sentinel, the history and the poke carry
    spelling: str  # the status exactly as the caller typed it
    note: str
    sentinel: Path
    history: Path
    verdict: str  # written | refused | forced
    ping: str  # sent | failed | skipped | deduped
    ping_detail: str  # why it was skipped/deduped, or the send error
    route: str  # dispatch | owner | skipped
    route_detail: str  # why no session, in the words the CLI prints
    poke: str  # delivered | detached | no-reader
    grace_s: int

    def render(self) -> str:
        """The human-readable multi-line summary; the CLI prints it verbatim."""
        sentinel = {
            "written": f"recap written to {self.sentinel}",
            "forced": f"recap REPLACED in {self.sentinel} (--force)",
            "refused": (
                f"kept the fuller recap already in {self.sentinel}"
                " (--force to replace it)"
            ),
        }[self.verdict]
        ping = {
            "sent": "owner telegrammed",
            "failed": f"telegram FAILED: {self.ping_detail}",
            "skipped": f"no telegram: {self.ping_detail}",
            "deduped": f"no telegram: {self.ping_detail}",
            "held": f"no telegram (logged): {self.ping_detail}",
        }[self.ping]
        route = {
            "dispatch": "operator job queued — this recap is its brief",
            "owner": f"no operator session (operator is off): {self.route_detail}",
            "skipped": f"no operator session: {self.route_detail}",
        }[self.route]
        poke = {
            "delivered": "supervisor poked",
            "detached": f"supervisor poke detached (lands after {self.grace_s}s grace)",
            "no-reader": (
                "supervisor NOT poked (nothing reading the control FIFO) — it will"
                " pick this up from the sentinel on restart"
            ),
        }[self.poke]
        lines = [
            f"done {self.phase} {self.status} [{self.verdict}]"
            + (f" (as `{self.spelling}`)" if self.spelling in statuses.ALIASES else ""),
            f"  {sentinel}",
            f"  {ping}",
            f"  {route}",
            f"  {poke}",
            f"  history: {self.history}",
            "  ledger: the swarm ticks or updates your row and files this note in the"
            " phase history when the phase lands; do not edit the ledger yourself",
        ]
        if self.spelling in statuses.RETIRED:
            lines.append(
                f"  NOTE: `{self.spelling}` is retired and was recorded as"
                f" `{self.status}` — say `{self.status}` next time"
            )
        return "\n".join(lines)


def _detach_recap(cfg: Config, phase: str) -> bool:
    """Generate this phase's recap in a detached process.

    Detached for the same reason the grace poke is: ``swarm done`` runs inside
    the worker's bash tool call, and a model round-trip can take a minute or
    more. Blocking there would put the whole recap on the worker's clock and
    hand it a timeout to misread as failure.

    Fire-and-forget on purpose. A missing recap is a cosmetic gap in the
    dashboard; nothing downstream depends on it, so it must never be able to
    affect whether a phase is recorded done. ``--completion`` reuses an existing
    successful recap rather than regenerating, so a re-reported sentinel never
    bills a second model call.
    """
    bin_ = os.environ.get("SWARM_BIN", "swarm")
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", f"exec {bin_} recap {shlex.quote(phase)} --completion"],
            cwd=str(cfg.project_dir),
            env=detached_env(cfg),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (OSError, ValueError):
        return False


def _detach_triage(cfg: Config, phase: str) -> bool:
    """Triage a freshly queued operator hand-off in a detached process.

    Same shape and same reason as :func:`_detach_recap`: ``swarm done`` runs
    inside the worker's bash tool call, and a model round-trip there puts the
    whole call on the worker's clock and hands it a timeout to misread as a
    failed finish. Spawned only when an item was actually *created*, so a re-run
    of ``swarm done`` cannot bill a second triage for a decision already made.
    """
    bin_ = os.environ.get("SWARM_BIN", "swarm")
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", f"exec {bin_} operator-triage {shlex.quote(phase)}"],
            cwd=str(cfg.project_dir),
            env=detached_env(cfg),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (OSError, ValueError):
        return False


def _queue_operator(
    cfg: Config, phase: str, status: str, note: str, verdict: str, plan: Outcome
) -> bool:
    """Record this finish's operator hand-off, and triage when it is a new one.

    Placed between the sentinel and the poke on purpose. After the sentinel,
    because :func:`opqueue.reconcile` can rebuild the item from it, so a crash in
    between costs nothing. Before the poke, because the poke is what makes the
    supervisor merge the branch and free the slot — an item written after it means
    a crash in that window leaves the work merged and recorded ``done`` with
    nothing queued at all.

    A ``refused`` verdict kept a fuller recap that is already on disk, and that
    recap is the hand-off's whole brief; queueing this thinner one would hand the
    session the note the sentinel just rejected.
    """
    if verdict == "refused" or plan.route != "dispatch":
        return False
    branch = f"swarm/{phase}" if cfg.git_isolation == "worktree" else ""
    item = opqueue.add(cfg, phase, status=status, note=note, branch=branch)
    if item is None:
        return False
    _detach_triage(cfg, phase)
    return True


def detached_env(cfg: Config) -> dict[str, str]:
    """This environment without the session's own markers, for the swarm's own
    detached helpers (the grace poke, the recap, the triage) that ``swarm done``
    starts from inside a worker: the worker's end reaps whatever carries its
    markers, and these must outlive it. ``SWARM_STATE_DIR`` stays, so they still
    find the run, and ``swarm down`` still ends them."""
    drop = {SESSION_ENV, cfg.env_marker}
    return {k: v for k, v in os.environ.items() if k not in drop}


def _detach_poke(cfg: Config, phase: str, status: str) -> bool:
    """Deliver the delayed ``done`` poke from a detached survivor process.

    ``start_new_session`` detaches it from the worker's pane, so neither the
    bash tool's timeout nor the next launch respawning the slot can kill the
    grace timer. ``SWARM_BIN`` is the test seam (may be multi-word, hence
    unquoted); production is the plain ``swarm`` entry point.
    """
    bin_ = os.environ.get("SWARM_BIN", "swarm")
    script = (
        f"sleep {cfg.done_grace_s}; "
        f"exec {bin_} _poke-done {shlex.quote(phase)} {shlex.quote(status)}"
    )
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", script],
            cwd=str(cfg.project_dir),
            env=detached_env(cfg),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def done(
    cfg: Config, phase: str, status: str, note: str = "", force: bool = False,
    after: str = "",
) -> DoneResult:
    """Signal phase completion; report what happened (see :class:`DoneResult`).

    A retired input spelling is canonicalised HERE and nowhere else, so the
    sentinel, the history row and the poke all carry the current status while a
    worker whose prompt still says ``needs-owner`` keeps working. It must never
    happen on a read path: ``doctor._check_sentinels`` compares the status in a
    sentinel's filename against the one in ``state.json``, so canonicalising
    either side as it is read would report a healthy state dir as four mismatches.

    Order matters: (1) write the durable sentinel — refusing to overwrite a fuller
    recap unless ``force`` — and append the attempt to the per-phase history;
    (2) telegram the owner *from the worker itself*, but ONLY for a ``fail``, and
    only when that ping is not a duplicate of one already sent; (3) hold the slot
    for ``done_grace_s`` so the worker has a buffer to flush any last work before
    the supervisor reclaims it; (4) best-effort poke. The grace does NOT sleep in
    the worker's process: ``swarm done`` runs inside the worker's bash tool call,
    whose timeout would kill a long sleep — and the poke with it. A detached child
    sleeps and delivers the poke instead, so ``swarm done`` returns immediately and
    the worker spends the whole grace closing out its session. Falls back to the
    old in-process sleep if the detach can't spawn; ``done_grace_s = 0`` (the
    default) keeps the immediate path.

    Durability is never traded for politeness: the sentinel is written (and the
    history appended) even when the recap is too thin to brief a session on.
    """
    spelling, status = status, statuses.canonical(status)
    recorded = _sentinel_note(cfg, phase, status)  # before we rewrite it
    fresh = recorded is None
    episode = _fail_episode(cfg, phase, fresh) if status == statuses.FAIL else 0
    verdict = _write_sentinel(cfg, phase, status, note, force=force)
    _append_history(cfg, phase, status, note, verdict, fresh)
    if verdict != "refused":
        # The ledger tick, status and history entry: applied by the supervisor
        # when the phase lands (or is rolled back), never by the worker.
        ledgerw.queue(cfg, phase, {"kind": "outcome", "outcome": spelling,
                                   "note": note, "after": after})

    plan = _outcome_plan(
        phase, status, note, recorded, verdict, force, cfg.operator_enabled
    )
    if spelling == statuses.LATER:
        plan.ping, plan.ping_detail = "skipped", "`later` waits for its date; nobody is paged"
    ping, detail = plan.ping, plan.ping_detail
    if ping == "send":
        # The Overseer retries a failed phase once, so a first failure is its to
        # handle; the owner hears when the retry fails too, or when no Overseer runs.
        held = None
        if status == statuses.FAIL and episode <= 1 and cfg.overseer_enabled:
            held = telegram.hold(cfg, "a first fail: the Overseer retries it once")
        elif spelling == statuses.BLOCKED:
            # One outside cause blocks many phases at once: one ping for the burst.
            held = telegram.hold(cfg, blockedping.HELD)
            if held:
                blockedping.gather(cfg, phase, note)
        sent = telegram.notify_detail(
            cfg.telegram_notify,
            _completion_ping(phase, status, note) or "",
            kind="worker-done",
            phase=phase,
            source="launch.done",
            state_dir=cfg.state_dir,
            suppressed=held,
        )
        if held:
            ping, detail = "held", held
        else:
            ping, detail = ("sent", "") if sent.delivered else ("failed", sent.error or "")

    if plan.route == "owner":
        plan.route, plan.route_detail = _send_todo(cfg, phase, note, recorded, verdict)
    _queue_operator(cfg, phase, status, note, verdict, plan)

    poke = "no-reader"
    if not os.environ.get("SWARM_TG_SINK"):
        _detach_recap(cfg, phase)  # hermetic tests must not spawn a model call
    if cfg.done_grace_s > 0 and _detach_poke(cfg, phase, status):
        poke = "detached"
    else:
        if cfg.done_grace_s > 0:
            time.sleep(cfg.done_grace_s)
        if _poke_fifo(cfg, f"done {phase} {status}\n"):
            poke = "delivered"
    return DoneResult(
        phase=phase,
        status=status,
        spelling=spelling,
        note=note,
        sentinel=cfg.done_dir / f"{phase}.{status}",
        history=cfg.done_dir / f"{phase}.jsonl",
        verdict=verdict,
        ping=ping,
        ping_detail=detail,
        route=plan.route,
        route_detail=plan.route_detail,
        poke=poke,
        grace_s=cfg.done_grace_s,
    )


#: How much of a question an owner ping carries. The owner answers from a phone;
#: a 140-word dump quoting source lines is not answerable there, and the full
#: text is on screen in the asker's own window anyway.
PING_QUESTION_CHARS = 600
_CUT_MARK = " … (full question in its window)"


def ping_question(question: str, limit: int = PING_QUESTION_CHARS) -> str:
    """The question as a phone ping carries it: collapsed, cut to one screen."""
    text = _collapse(question)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + _CUT_MARK


def cost_line(blocked: int | None, held: str, asked_at: float) -> str:
    """What this question costs while it waits, as the ping's first line.

    No ping used to say it, so the owner could not tell a question holding up a
    chain of phases from one holding up nothing. ``blocked`` is ``None`` when the
    ledger could not be read — the line then says what it does know rather than
    guessing a number.
    """
    parts = []
    if blocked is not None:
        parts.append(f"holding up {blocked} phase{'' if blocked == 1 else 's'}")
    parts.append(held)
    parts.append("asked " + time.strftime("%H:%M", time.localtime(asked_at)))
    return " · ".join(parts)


def _blocked_behind(cfg: Config, phase: str) -> int | None:
    """:func:`ledger.blocked_behind` against the live ledger and ``done`` map."""
    try:
        graph = ledger_mod.load(cfg.project_dir / cfg.ledger)
        done = state_mod.read(cfg).done
    except (OSError, ValueError):
        return None
    if phase not in graph:
        return None
    return ledger_mod.blocked_behind(graph, phase, done, set(cfg.exclude))
