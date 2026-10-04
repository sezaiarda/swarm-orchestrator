"""``swarm`` command-line entry point.

Thin dispatch over the library. Commands that only signal the supervisor poke
the FIFO (best-effort, never hang); commands that own an action (launch, done,
free, skip, status) act directly under the state lock.
"""

from __future__ import annotations

import argparse
import json
import re
import os
import signal
import subprocess
import sys
import time

from dataclasses import asdict, replace
from pathlib import Path

from . import caps
from . import backup as backup_mod
from . import bigpic as bigpic_mod
from . import buildsem
from . import console as console_mod
from . import notes as notes_mod
from . import operator as operator_mod
from . import owner as owner_mod
from . import pauseat
from . import opqueue
from . import overseer as overseer_mod
from . import ovrecord
from . import tui as tui_mod
from . import doctor as doctor_mod
from . import drain as drain_mod
from . import gc as gc_mod
from . import guide as guide_mod
from . import promptlint
from . import procs
from . import pushowed
from . import recap as recap_mod
from . import reload as reload_mod
from . import restart as restart_mod
from . import report as report_mod
from . import why as why_mod
from . import gitq
from . import keep as keep_mod
from . import lanes as lanes_mod
from . import landing as landing_mod
from . import ledger as ledger_mod
from . import ledgerw
from . import launch as launch_mod
from . import session as session_mod
from . import state as state_mod
from . import statuses
from . import supervisor as sup_mod
from . import telegram, tgbot, tmux
from . import todo as todo_mod
from . import usage as usage_mod
from .resources import view as resources_view
from .web import lifecycle as web_lifecycle
from .config import Config, load, session_project
from . import logutil
from .logutil import Log
from .procs import SESSION_ENV
from . import master as master_mod
from . import models as models_mod
from .master import build_context


def _poke(cfg: Config, verb: str) -> bool:
    return launch_mod._poke_fifo(cfg, f"{verb}\n")


def _wait_pid_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


def _wait_supervisor_up(cfg: Config, timeout: float = 10.0) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = state_mod.read(cfg)
        if st.supervisor_pid:
            return st.supervisor_pid
        time.sleep(0.05)
    return None


def _supervisor_running(cfg: Config) -> bool:
    """True if a supervisor is already attached to this project's control FIFO.

    The supervisor opens the FIFO ``O_RDWR`` at startup and holds it for its whole
    life (it is the *sole* reader), so a non-blocking write-open that succeeds
    means one is running. Unlike checking the recorded ``supervisor_pid`` this also
    catches a *stray* supervisor whose pid was overwritten by an earlier ``up``:
    the failure mode where a session was killed out-of-band, its detached
    supervisor survived, and a second ``up`` co-opted the same FIFO — leaving two
    supervisors racing every event (each poke read by only one of them). A missing
    FIFO or ``ENXIO`` (no reader) means none is running.
    """
    if not cfg.fifo_path.exists():
        return False
    try:
        fd = os.open(cfg.fifo_path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return False  # ENXIO (no reader attached) or transient — not running
    os.close(fd)
    return True


# -- commands -------------------------------------------------------------
def _reconcile_orphans(cfg: Config) -> None:
    """Rehydrate done-state and reconcile leftover ``swarm/*`` branches on ``up``.

    Sentinel-driven, not topology-derived: the durable ``swarm done`` sentinels
    are the record of which phases actually finished. A finished phase's
    (possibly incomplete) integration is completed; an *interrupted* phase (a
    leftover branch with no sentinel) is discarded and left NOT done so the
    master rebuilds it.

    A phase whose integration is HELD is deliberately NOT seeded into ``done``.
    It used to be, unconditionally — so a supervisor that died mid-integration
    produced a run claiming the phase was complete while its branch had never
    merged, with ``integ_blocked`` null, no resolver and no telegram. Dependents
    then built on a main lacking the code, and the *next* ``swarm up`` took the
    "already done" path straight into ``discard()`` and deleted the branch. The
    finished work was destroyed by the recovery routine. Now a held phase boots
    the run into a visibly blocked state instead of a phantom-complete one.
    """
    log = Log(cfg.supervisor_log)
    try:
        seed = gitq.sentinel_done(cfg)
        st = state_mod.read(cfg)
        try:
            result = gitq.reconcile(cfg, dict(st.done), log, operator=_mirror_plan(cfg),
                                    later=set(ledgerw.dated(cfg)))
        except gitq.GitError as exc:
            print(f"reconcile skipped (git error): {exc}", file=sys.stderr)
            log.line(f"RECONCILE-ERROR {exc}")
            result = gitq.ReconcileResult()
        held_phases = {h.phase for h in result.held}
        # A landing under way under lanes is not done either: it goes back on the
        # queue, and the supervisor lands it when its check reports.
        held_phases |= set(result.landing)
        if seed or result.integrated:
            with state_mod.transaction(cfg) as s:
                for phase, status in seed.items():
                    if phase in held_phases:
                        continue  # not done: its branch never merged
                    s.mark_done(phase, status)
                for phase in result.integrated:
                    # The sentinel's status, not a hardcoded "ok" — the same bug
                    # `supervisor._pump_integrations` was fixed for: rewriting a
                    # `needs-owner` as a clean success the moment its branch merged.
                    s.mark_done(phase, seed.get(phase, "ok"))
        # A `<phase>.fail` left beside a row closed since must not come back
        # as a failure: the seed above read it, this retires it for good. Work
        # kept for the date of a row closed since goes to the attic with it.
        try:
            ledgerw.release_closed(cfg, log)
            ledgerw.release_kept(cfg, log)
        except (gitq.GitError, OSError) as exc:
            log.line(f"FAIL-CLOSED-ERROR {exc}")
        if result.integrated:
            print(f"reconciled orphan branches: {', '.join(result.integrated)}")
        if result.operator_integrated:
            print(f"landed operator/overseer mirrors: {', '.join(result.operator_integrated)}")
        for phase, pushes in result.pushes.items():
            pushowed.settle(cfg, phase, pushes, log)  # a failed push is owed, not held
        if result.landing:
            with state_mod.transaction(cfg) as s:
                for phase in result.landing:
                    s.integ_push(phase, seed.get(phase, "ok"))
            print(f"landing under way (lanes): {', '.join(result.landing)}")
        if result.held:
            first = result.held[0]
            plan = operator_mod.mirror_plan(cfg)
            passes = ovrecord.mirror_plan(cfg)
            with state_mod.transaction(cfg) as s:
                s.integ_blocked = first.phase
                s.integ_blocked_kind = first.kind
                s.integ_blocked_repo = str(first.repo) if first.repo else None
                for h in result.held:
                    # Queued, so `swarm resolved` re-lands it and the launcher
                    # never builds over it (the first is the queue's head).
                    if plan.get(h.phase) == operator_mod.INTEGRATE:
                        s.integ_push(h.phase, operator_mod.INTEG_STATUS)
                    elif h.phase in passes:
                        s.integ_push(h.phase, ovrecord.INTEG_STATUS)
                    else:
                        s.integ_push(h.phase, seed.get(h.phase, "ok"))
            names = ", ".join(f"{h.phase} ({h.kind})" for h in result.held)
            print(f"integration HELD: {names}", file=sys.stderr)
            print("  these phases are NOT marked done — their branches never merged.")
            print("  resolve, then `swarm resolved <phase>`; `swarm doctor` for detail.")
            log.line(f"RECONCILE-HELD-BOOT {names}")
            telegram.notify(
                cfg.telegram_notify,
                f"swarm: {cfg.name} started, but finished work could not be merged:"
                f" {names}. All merging waits on it. `swarm doctor` shows what is in"
                " the way; fix it, then run `swarm resolved <phase>`.",
                kind="integrate-hold",
                phase=first.phase,
                source="cli._reconcile_orphans",
                state_dir=cfg.state_dir,
            )
    finally:
        log.close()


def _mirror_plan(cfg: Config) -> dict[str, str]:
    """Every ``swarm/*`` branch that has no sentinel by design and must not be
    discarded as an interrupted phase: operator jobs' and Overseer passes', and
    the mirror of any session a restart carried across alive (left as it is)."""
    return {**operator_mod.mirror_plan(cfg), **ovrecord.mirror_plan(cfg),
            **{name: "live" for name in restart_mod.kept_mirrors(cfg)}}


def _attach(cfg: Config) -> None:
    """Attach the caller's terminal to the swarm tmux session.

    Interactive-only: a no-op when stdout is not a TTY (scripts and the hermetic
    test harness run ``swarm up`` with captured output) or for the bare driver.
    Inside an existing tmux session we switch the client instead of nesting;
    otherwise we ``exec`` into ``tmux attach`` so the ``swarm`` process simply
    becomes the tmux client (the detached supervisor keeps running).
    """
    if cfg.driver != "tmux" or not sys.stdout.isatty():
        return
    if not tmux.session_exists(cfg.session):
        return
    if os.environ.get("TMUX"):
        subprocess.run(["tmux", "switch-client", "-t", cfg.session], check=False)
        return
    try:
        os.execvp("tmux", ["tmux", "attach", "-t", cfg.session])
    except OSError as exc:
        print(f"could not attach to tmux session {cfg.session!r}: {exc}", file=sys.stderr)


#: How long ``up`` waits for the dashboard to boot and bind the board it serves.
WEB_HOSTED_WAIT_S = 20.0


def web_hosted(cfg: Config) -> bool:
    """Whether the dashboard serves the web board (``up`` then starts none): only
    under tmux, where ``up`` opens the dashboard in window 0."""
    return cfg.driver == "tmux" and bool(cfg.tui_autostart)


def _report_web_board(cfg: Config, hosted: bool = False) -> None:
    """After ``up`` starts the board (in the dashboard, or a detached process),
    say whether it actually came up — port-taken and crash both used to go
    silent: the pane died, ``swarm up`` printed the URLs anyway, and nothing but
    a since-corrected ``status``/``doctor`` connect check ever disagreed."""
    state, detail = web_lifecycle.wait_probe(cfg, WEB_HOSTED_WAIT_S if hosted else 5.0)
    if state == web_lifecycle.OURS:
        # ``detail``: the dashboard holds the port and has not answered yet.
        print(f"web board: {' '.join(web_lifecycle.urls(cfg))}"
              + (f" ({detail})" if detail else ""))
        return
    if state == web_lifecycle.TAKEN:
        who = f" ({detail})" if detail else ""
        reason = f"port :{cfg.web_port} is held by another program{who}"
    else:
        where = ("the dashboard's status bar says why" if hosted
                 else "check <state>/logs/web.log")
        reason = f"nothing answered on :{cfg.web_port} — {where}"
    hint = "set [web].port in .swarm.toml to a free port and restart"
    print(f"web board: FAILED to start — {reason}", file=sys.stderr)
    print(f"  fix: {hint}", file=sys.stderr)
    telegram.notify(
        cfg.telegram_notify,
        f"swarm: {cfg.name} is running, but the web board did not start ({reason})."
        " The TUI still works. To fix it, give the board a free port in .swarm.toml"
        " and restart the swarm.",
        kind="web-board",
        source="cli._report_web_board",
        state_dir=cfg.state_dir,
        # `swarm up` has just printed it; the Overseer's summary carries it too.
        suppressed=telegram.hold(cfg, "printed by `swarm up`; the board is not the run"),
    )


def cmd_up(cfg: Config, attach: bool = True) -> int:
    cfg.ensure_dirs()
    if _supervisor_running(cfg):
        # Refuse to start a second supervisor on the same FIFO. The tmux-session
        # guard in session.setup misses the case where the session was killed
        # out-of-band but the detached supervisor survived; without this a second
        # `up` silently spawns a co-reader and both race every event.
        print(
            f"a supervisor is already running for {cfg.slug!r} "
            "(control FIFO has a reader) -- run `swarm down` first",
            file=sys.stderr,
        )
        return 1
    if cfg.driver == "tmux" and tmux.session_exists(cfg.session):
        # Checked before anything is touched: session.setup refuses the same
        # thing, but only after state was reset and the run restarted.
        print(
            f"the swarm is already up: tmux session {cfg.session!r} exists -- "
            f"`tmux attach -t {cfg.session}` to look at it, or `swarm down` first",
            file=sys.stderr,
        )
        return 1
    # A restart planned before this `up` is moot: the code on disk loads now.
    planned = restart_mod.load(cfg)
    if planned.get("stage") == restart_mod.PLANNED:
        restart_mod.update(cfg, planned["id"], stage=restart_mod.CANCELLED,
                           detail="a `swarm up` came first", ended_at=time.time())
    log = Log(cfg.supervisor_log)
    try:
        state_mod.init_state(cfg, log=log, carried=restart_mod.kept_phases(cfg))
        _start_run(cfg, "up")
        # Every up, whatever the isolation: it is what re-queues a hand-off whose
        # sentinel outlived its item — and the swarm also runs with isolation "none".
        rebuilt = opqueue.reconcile(cfg, log)
    finally:
        log.close()
    if rebuilt:
        print(f"operator queue: {', '.join(rebuilt)}")
    if cfg.git_isolation == "worktree":
        _reconcile_orphans(cfg)
    if not cfg.fifo_path.exists():
        os.mkfifo(cfg.fifo_path)
    if cfg.driver == "tmux":
        session_mod.setup(cfg)
    # Sessions a full restart carried across, still waiting on the owner: back
    # into the run before the supervisor starts, so nothing launches over them.
    log = Log(cfg.supervisor_log)
    try:
        carried = restart_mod.carry_in(cfg, log)
    finally:
        log.close()
    if carried:
        print(f"kept across the restart, still waiting on you: {', '.join(carried)}")
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarm_orchestrator", "_supervise"],
        cwd=str(cfg.project_dir),
        # `swarm up` typed in the owner console: the supervisor is the swarm's own.
        env={k: v for k, v in os.environ.items() if k != console_mod.CONSOLE_ENV},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid = _wait_supervisor_up(cfg)
    if pid is None:
        print("supervisor did not start", file=sys.stderr)
        proc.terminate()
        return 1
    _poke(cfg, "bootstrap")
    print(f"swarm up: supervisor pid={pid} driver={cfg.driver}")
    if cfg.driver == "tmux" and cfg.console_enabled:
        print(f"console: tmux window {console_mod.WINDOW} — your own Claude session"
              " (o in the dashboard, or `swarm console`)")
    if cfg.web_enabled:
        # The dashboard serves the board (tui.webboard); with no dashboard — the
        # headless driver, or `[tui] autostart` off — it gets its own process.
        hosted = web_hosted(cfg)
        if not hosted:
            web_lifecycle.start_detached(cfg)
        _report_web_board(cfg, hosted)
    if cfg.telegram_commands:
        # A side helper, never part of the run: whatever happens to it, `up` goes on.
        _, what = tgbot.start_detached(cfg)
        print(f"telegram bot: {what}")
    if attach:
        _attach(cfg)  # interactive: hand the terminal to the swarm window
    return 0


def cmd_web(cfg: Config, host: str | None, port: int | None, pidfile: str | None,
            explicit: str | None = None) -> int:
    """Serve the board. Deferred import: `swarm done` must not pay for http.server."""
    from .web import server as web_server

    return web_server.serve(
        cfg,
        cfg.web_host if host is None else host,
        cfg.web_port if port is None else port,
        pidfile=pidfile,
        explicit_config=explicit,
    )


def cmd_telegram_bot(cfg: Config, pidfile: str | None) -> int:
    """Answer ``/usage`` and ``/help`` from the owner's chat, in the foreground."""
    return tgbot.serve(cfg, pidfile)


def cmd_supervise(cfg: Config, adopt: bool = False) -> int:
    sup_mod.main(cfg, adopt=adopt)
    return 0


def _our_supervisor(cfg: Config, pid: int) -> bool:
    """Whether ``pid`` is this project's supervisor and not a process that has
    since been given the same number. The pid recorded in state outlives the
    supervisor, and ``down`` escalates to SIGKILL."""
    return restart_mod.is_supervisor(cfg, pid)


def cmd_down(cfg: Config) -> int:
    st = state_mod.read(cfg)
    # A restart that has not begun its own down is over: the swarm is stopping.
    plan = restart_mod.load(cfg)
    if restart_mod.active(plan) and not (
            plan.get("mode") == restart_mod.FULL and plan.get("stage") == restart_mod.RESTARTING):
        restart_mod.update(cfg, plan["id"], stage=restart_mod.CANCELLED,
                           detail="a `swarm down` came first", ended_at=time.time())
    _poke(cfg, "shutdown")
    pid = st.supervisor_pid
    if pid and procs.alive(pid) and not _our_supervisor(cfg, pid):
        print(f"swarm down: pid {pid} is no longer this project's supervisor; not"
              " signalling it", file=sys.stderr)
        pid = None
    if pid and not _wait_pid_gone(pid, timeout=30.0):
        # Still alive — likely mid-integration. Escalate before tearing down the
        # session, so we never kill the master/worker/resolver panes out from
        # under a live integration (which would fail its pane ops mid-merge).
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        if not _wait_pid_gone(pid, timeout=10.0):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            _wait_pid_gone(pid, timeout=5.0)
    # Found BEFORE the teardown, which takes the pane pids with it. Killing the
    # session only hangs up on its panes, and a claude can outlive its SIGHUP:
    # some workers, the Overseer and an operator could keep running after
    # `down` until the owner killed them by hand.
    owned = cfg.driver == "tmux" and session_mod.owns_session(cfg, st.windows)
    if cfg.driver == "tmux" and not owned and tmux.session_exists(cfg.session):
        print(f"swarm down: tmux session {cfg.session!r} is not this swarm's; left"
              " alone", file=sys.stderr)
    roots = tmux.session_pane_pids(cfg.session) if owned else []
    sessions = session_mod.session_processes(cfg, roots)
    if owned:
        session_mod.teardown(cfg)
    # After the teardown: under tmux the board died with its window, and this
    # only clears the pid file; under the headless driver it is what stops it.
    web_lifecycle.stop(cfg)
    tgbot.stop(cfg)
    ended, left = session_mod.end_processes(cfg, sessions)
    # After the sessions end, so their work is final. Printed only once the run
    # is closed: a dashboard that ran this is gone by now, and a write to its
    # dead pipe must not cut the down short.
    backup = _backup_on_down(cfg) if cfg.backup_on_down else []
    closed = usage_mod.close_run(cfg, "down")
    print("swarm down" + (f" — {_run_line(closed)}" if closed else ""))
    for text, to_err in backup:
        print(text, file=sys.stderr if to_err else sys.stdout)
    if ended:
        print(f"  ended {ended} session process(es)")
    if left:
        print(f"swarm down: {len(left)} process(es) survived SIGKILL: "
              f"{' '.join(map(str, left))}", file=sys.stderr)
        return 1
    return 0


def _backup_on_down(cfg: Config) -> list[tuple[str, bool]]:
    """Push every unmerged phase's work to origin; the lines to print, and
    whether each goes to stderr."""
    log = Log(cfg.supervisor_log)
    try:
        res = backup_mod.run(cfg, log, budget_s=backup_mod.DOWN_BUDGET_S)
    finally:
        log.close()
    lines = [(f"  backup to origin: {res.line()}", False)] if (
        res.pushed or res.deleted or res.failed) else []
    return lines + [(f"  backup failed: {what}", True) for what in res.failed[:5]]


def _down_then(cfg: Config, then: str) -> int:
    """``swarm down``, then the owner's after-command, detached, whatever down said."""
    try:
        rc = cmd_down(cfg)
    except BrokenPipeError:
        # Run from the dashboard, whose pipe died with the session: the down
        # itself had finished, and the after-command must still run.
        rc = 0
    # The stop a drain waited for has happened: `status` must not still say it is coming.
    with state_mod.transaction(cfg) as st:
        st.drain = {}
    if then:
        log = drain_mod.run_after(cfg, then)
        try:
            if log is None:
                print(f"could not start the after-command: {then}", file=sys.stderr)
                return 1
            print(f"after-command started, detached: {then}  (output: {log})")
        except BrokenPipeError:
            pass
    return rc


def cmd_drain(cfg: Config, then: str) -> int:
    """Launch nothing new, and stop once the running work is finished.

    The supervisor does the waiting: only it knows what is mid-launch, and it
    sees every event that could be the last one. With no supervisor there is
    nothing left to wait for, so this is an ordinary down."""
    warning = drain_mod.sudo_warning(then)
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
    if not _supervisor_running(cfg):
        print("no supervisor is running, so there is nothing to wait for: stopping now")
        return _down_then(cfg, then)
    with state_mod.transaction(cfg) as st:
        if st.drain.get("stopping_at"):
            print("the drain already finished and the swarm is stopping", file=sys.stderr)
            return 1
        st.drain = {"since": st.drain.get("since") or time.time(), "then": then,
                    "waiting": list(st.drain.get("waiting") or [])}
    _poke(cfg, "drain")
    tail = f", then run: {then}" if then else ""
    print(f"swarm draining — nothing new launches; it stops once the running work is finished{tail}")
    print("  `swarm status` says what it waits for; `swarm down --cancel` or `swarm resume` cancels")
    asked = restart_mod.questions(cfg, state_mod.read(cfg))
    if asked:
        lost = "what they asked is not" if all(q.asking for q in asked) else "the sessions are not"
        print(f"NOTE: the stop does not wait for the {restart_mod.standing(asked)},"
              f" and it closes them: their work is kept, {lost}.",
              file=sys.stderr)
        for line in restart_mod.question_lines(asked):
            print(line, file=sys.stderr)
        print("  to restart without losing them: `swarm down --cancel`, then `swarm restart`"
              " (nothing is stopped) or `swarm restart --full --keep-questions`",
              file=sys.stderr)
    return 0


def cmd_drain_cancel(cfg: Config) -> int:
    with state_mod.transaction(cfg) as st:
        had = dict(st.drain)
        if had and not had.get("stopping_at"):
            st.drain = {}
        paused = st.paused
    if not had:
        print("no drain to cancel")
        return 0
    if had.get("stopping_at"):
        print("too late: the drain finished and the swarm is already stopping", file=sys.stderr)
        return 1
    _drop_draining_restart(cfg, had, "down --cancel")
    _poke(cfg, "drain")
    print("drain cancelled — " + ("the swarm is still paused" if paused else "launching resumes"))
    return 0


def _drop_draining_restart(cfg: Config, drain: dict, by: str) -> None:
    """A drain that was a full restart's has been cancelled: so is the restart."""
    plan_id = drain.get("restart")
    if plan_id and restart_mod.update(cfg, plan_id, stage=restart_mod.CANCELLED,
                                      ended_at=time.time()) is not None:
        _log_pause_schedule(cfg, f"RESTART-CANCELLED id={plan_id} by={by}")


def cmd_drain_down(cfg: Config) -> int:
    """The drain's last step, started detached by the supervisor: down, then the
    after-command — or, for a full restart's drain, down and up again."""
    drain = state_mod.read(cfg).drain
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    if drain.get("restart"):
        print(f"== {stamp} drain finished: restarting the swarm")
        return restart_mod.finish_full(
            cfg, drain["restart"], down=cmd_down, up=lambda c: cmd_up(c, attach=False))
    print(f"== {stamp} drain finished: stopping the swarm")
    return _down_then(cfg, drain.get("then") or "")


# -- restart ---------------------------------------------------------------
#: How long `swarm restart` follows a restart it started before it says it is
#: still going and returns; the restart itself carries on, detached.
RESTART_FOLLOW_S = 1200.0


def _stale_restart(plan: dict) -> bool:
    """An in-place restart whose helper is gone without settling the plan."""
    if plan.get("stage") not in (restart_mod.STOPPING, restart_mod.RESTARTING):
        return False
    if plan.get("mode") != restart_mod.SUPERVISOR:
        return False
    pid = plan.get("runner_pid")
    return not (pid and procs.alive(int(pid)))


def cmd_restart(cfg: Config, delay: str | None = None, at: str | None = None,
                cancel: bool = False, full: bool = False, policy: str | None = None,
                follow: bool = True) -> int:
    """Restart the supervisor in place (the default), or the whole swarm
    (``--full``), now or later. Never drops a session that waits on the owner
    unless told to. There is only ever one planned: a new one replaces it."""
    if cancel:
        return cmd_restart_cancel(cfg)
    now = time.time()
    try:
        when = now + pauseat.parse_in(delay) if delay else pauseat.next_at(at, now) if at else now
    except ValueError as exc:
        print("swarm restart: " + str(exc).replace(
            "for a pause now, run `swarm pause`", "for a restart now, run `swarm restart`"),
            file=sys.stderr)
        return 2
    if policy and not full:
        print("swarm restart: --wait-questions, --keep-questions and --force are for --full;"
              " a plain restart replaces only the supervisor and never touches a session"
              " that waits on you", file=sys.stderr)
        return 2
    scheduled = bool(delay or at)
    mode = restart_mod.FULL if full else restart_mod.SUPERVISOR
    policy = policy or restart_mod.REFUSE
    st = state_mod.read(cfg)
    prior = restart_mod.load(cfg)
    if restart_mod.active(prior) and prior.get("stage") != restart_mod.PLANNED \
            and not _stale_restart(prior):
        print(f"a restart is already under way (asked by {prior.get('by')}): "
              f"{restart_mod.line(prior, *restart_mod.counts(st), now)}", file=sys.stderr)
        print("  `swarm restart --cancel` stops it where it can still be stopped",
              file=sys.stderr)
        return 1
    asked = restart_mod.questions(cfg, st)
    if full and asked and policy == restart_mod.REFUSE:
        print(f"swarm restart --full: refused — {restart_mod.standing(asked, 'are ')},"
              " and a full restart closes them:", file=sys.stderr)
        for line in restart_mod.question_lines(asked):
            print(line, file=sys.stderr)
        print("  nothing was changed. To go ahead:\n"
              "    swarm restart                         replace only the supervisor: nothing"
              " is closed (enough to load new code)\n"
              "    swarm restart --full --wait-questions  drain, and wait until you have"
              " answered them\n"
              "    swarm restart --full --keep-questions  carry them across the restart,"
              " alive and still asking\n"
              "    swarm restart --full --force           close them (their work is kept,"
              " the question is not)", file=sys.stderr)
        return 1
    running = _supervisor_running(cfg)
    if scheduled and not running:
        print("no supervisor is running, so there is nothing to restart later: `swarm"
              " restart` picks a stopped swarm up now, `swarm up` starts one", file=sys.stderr)
        return 1
    plan = restart_mod.new_plan(cfg, mode, when, restart_mod.requester(cfg),
                                questions=policy, now=now)
    sup = restart_mod.live_supervisor(cfg, st)
    by_supervisor = scheduled and restart_mod.capable(cfg, sup, "restart-at")
    plan["timer"] = "supervisor" if by_supervisor else "runner"
    restart_mod.save(cfg, plan)
    replaced = (f" replaces={prior.get('id')}"
                if prior.get("stage") == restart_mod.PLANNED else "")
    _log_pause_schedule(
        cfg, f"RESTART-PLANNED id={plan['id']} mode={mode} at={pauseat.stamp(when)}"
             f" in={when - now:.0f}s by={plan['by']!r} questions={policy}{replaced}")
    if by_supervisor:
        _poke(cfg, "restart-scheduled")
    else:
        proc = restart_mod.spawn_runner(cfg, plan["id"], at=when if scheduled else None)
        if proc is None:
            restart_mod.update(cfg, plan["id"], stage=restart_mod.FAILED,
                               detail="the restart helper could not be started",
                               ended_at=time.time())
            print("swarm restart: could not start the restart helper", file=sys.stderr)
            return 1
        restart_mod.update(cfg, plan["id"], runner_pid=proc.pid)
    if scheduled:
        print(restart_mod.line(plan, *restart_mod.counts(st), now))
        if replaced:
            print(f"  this replaces the restart that was planned for"
                  f" {pauseat.when(float(prior.get('at') or now), now)}")
        return 0
    what = ("full restart: draining, then down and up" if full
            else "restarting the supervisor in place — workers, parked sessions, the operator"
                 " and your console are not touched")
    print(f"{what} (asked by {plan['by']})")
    if not follow:
        print("  started, detached; `swarm status` says how it goes")
        return 0
    return _follow_restart(cfg, plan["id"], full)


def _follow_restart(cfg: Config, plan_id: str, full: bool) -> int:
    """Say how a restart this command started goes, until it is settled (a full
    one: until its drain is under way, which can take hours)."""
    deadline = time.monotonic() + RESTART_FOLLOW_S
    said: list[str] = []
    while True:
        plan = restart_mod.load(cfg)
        stage = plan.get("stage") if plan.get("id") == plan_id else restart_mod.CANCELLED
        if stage == restart_mod.DONE:
            print(f"restarted: {plan.get('detail')}")
            return 0
        if stage == restart_mod.FAILED:
            print(f"restart FAILED: {plan.get('detail')}", file=sys.stderr)
            return 1
        if stage == restart_mod.CANCELLED:
            print("restart cancelled")
            return 1
        if stage == restart_mod.DRAINING and full:
            print("  draining now: it restarts once the running work is finished"
                  " (`swarm status` says what it waits for; `swarm restart --cancel` stops it)")
            return 0
        waits = list(plan.get("waiting") or [])
        if waits and waits != said:
            said = waits
            print(f"  waiting for a safe moment: {', '.join(waits)}")
        if time.monotonic() >= deadline:
            print("  still going; it carries on, detached — `swarm status` says how it goes")
            return 0
        time.sleep(0.2)


def cmd_restart_cancel(cfg: Config) -> int:
    plan = restart_mod.load(cfg)
    if not restart_mod.active(plan):
        print("no restart to cancel")
        return 0
    stage, plan_id = plan.get("stage"), plan["id"]
    by = restart_mod.requester(cfg)
    if stage == restart_mod.RESTARTING and not _stale_restart(plan):
        print("too late: the restart is already happening", file=sys.stderr)
        return 1
    if stage == restart_mod.DRAINING:
        with state_mod.transaction(cfg) as st:
            mine = st.drain.get("restart") == plan_id
            late = bool(st.drain.get("stopping_at"))
            if mine and not late:
                st.drain = {}
        if mine and late:
            print("too late: the drain finished and the swarm is already restarting",
                  file=sys.stderr)
            return 1
    restart_mod.update(cfg, plan_id, stage=restart_mod.CANCELLED, ended_at=time.time())
    _log_pause_schedule(cfg, f"RESTART-CANCELLED id={plan_id} stage={stage} by={by!r}")
    _poke(cfg, "drain" if stage == restart_mod.DRAINING else "restart-scheduled")
    if stage == restart_mod.PLANNED:
        print(f"restart planned for {pauseat.when(float(plan.get('at') or 0), time.time())}"
              f" (asked by {plan.get('by')}) cancelled")
    else:
        print("restart cancelled — the swarm runs on as it was")
    return 0


def cmd_restart_run(cfg: Config, plan_id: str, at: float | None) -> int:
    """The restart itself, detached (see :func:`restart.run`)."""
    print(f"== {time.strftime('%Y-%m-%d %H:%M:%S')} restart {plan_id}")
    return restart_mod.run(cfg, plan_id, at)


def cmd_down_verb(cfg: Config, drain: bool, then: str, cancel: bool) -> int:
    if cancel:
        return cmd_drain_cancel(cfg)
    if drain:
        return cmd_drain(cfg, then)
    return _down_then(cfg, then)


def _run_line(rec: dict) -> str:
    """One line for a run just closed: what it did and what it used per hour."""
    s = rec.get("summary") or {}

    def f(v):
        return "—" if v is None else f"{v:.2f}"

    return (f"run {rec['run_id']} closed: {s.get('hours', 0):.1f} h, "
            f"{s.get('phases_finished', 0)} phase(s), 5-hour {f(s.get('five_pct_per_h'))} %/h, "
            f"weekly {f(s.get('week_pct_per_h'))} %/h (`swarm usage` for the history)")


def open_run(cfg: Config, reason: str) -> tuple[dict, dict | None]:
    """Open a run and mirror it into ``state.json``. Shared with the dashboard's ``R``."""
    rec, closed = usage_mod.start_run(cfg, reason)
    with state_mod.transaction(cfg) as st:
        st.run_id, st.run_epoch = rec["run_id"], rec["epoch_ts"]
    return rec, closed


def _start_run(cfg: Config, reason: str) -> dict:
    """:func:`open_run`, saying what it closed, if anything."""
    rec, closed = open_run(cfg, reason)
    if closed:
        print(_run_line(closed))
    return rec


def cmd_reset(cfg: Config) -> int:
    """Close the open run and start a fresh one; nothing is restarted.

    Resetting the ETA and the usage: both are measured from the run's
    epoch, so a new epoch is the reset. The closed run keeps its summary in
    ``history/runs/``, where ``swarm usage`` lists it.
    """
    rec = _start_run(cfg, "reset")
    print(f"run {rec['run_id']} started — ETA and usage now count from here")
    return 0


def cmd_usage(cfg: Config, as_json: bool, last: int) -> int:
    """The open run's usage per hour, then the last ``last`` closed runs."""
    now = time.time()
    src = usage_mod.Sources(cfg)
    cur = usage_mod.live_summary(cfg, src, now)
    past = usage_mod.past_summaries(cfg.state_dir, last)
    if as_json:
        account = usage_mod.active_account(src.samples)
        mine = usage_mod.of_account(src.samples, account)
        return _dump({"current": cur, "runs": past, "account": account,
                      "now": {w: usage_mod.latest(mine, w, now) for w in ("five", "week")},
                      "note": usage_mod.SKEW_NOTE})
    print(usage_mod.render(cur, past, src.samples, now))
    return 0


def cmd_launch(cfg: Config, phase: str) -> int:
    """Claim a slot and start a worker — and say what happened either way.

    This printed nothing and its exit code was the only signal, which is the same
    trap `swarm done` was in: a launch that is denied (no free slot, unmet deps,
    the swarm paused) and a launch whose pane never became ready both looked
    exactly like success from the terminal. The reason is always in the log, so
    point at it rather than making the caller go and find it.
    """
    log = Log(cfg.supervisor_log)
    ok = launch_mod.launch(cfg, phase, log)
    log.close()
    if ok:
        print(f"launched {phase}")
        return 0
    tail = ""
    try:
        lines = cfg.supervisor_log.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines[-40:]):
            _, msg = logutil.parse_ts(line)
            if msg.startswith(("LAUNCH-DENIED", "LAUNCH-FAIL", "READY-TIMEOUT",
                               "SUBMIT-LOST", "WORKTREE-FAIL")):
                tail = msg
                break
    except OSError:
        pass
    print(f"could not launch {phase}" + (f": {tail}" if tail else ""), file=sys.stderr)
    print(f"  see {cfg.supervisor_log}", file=sys.stderr)
    return 1


def cmd_done(cfg: Config, phase: str, status: str, note: str, force: bool = False,
             after: str = "") -> int:
    """Signal phase completion — and say, in full, what that did.

    This used to be two lines that printed nothing and returned 0 unconditionally,
    discarding both the telegram result and the FIFO poke result. It was the only
    state-changing command that said nothing, so a worker had no way to tell
    whether it had worked and was liable to call it again just to check (a repeat
    call rewrites the recap).

    A worker that can see the sentinel path, the ping verdict and whether anything
    was listening has no reason to retry.

    Refused before anything is written when the call cannot be about a live
    worker: a malformed id, a phase not in flight, or a worker session naming a
    phase other than its own. A ``fail`` removes the phase's worktree, and the
    supervisor ends the named phase's session, so a stray call must not reach it.
    """
    st = state_mod.read(cfg)
    if phase in st.done and not st.in_flight(phase) and ledger_mod.safe_id(phase):
        print(f"{phase} is already recorded {st.done[phase]}; nothing to do")
        return 0
    refusal = _done_refusal(cfg, phase, st)
    if refusal is not None:
        print(f"swarm done refused: {refusal}", file=sys.stderr)
        return 2
    if after:
        try:
            ledgerw.check_date(after)
        except ledgerw.ReportError as exc:
            # Past the point of no return: record the finish, just without a date.
            print(f"swarm done: {exc}; recorded without --after", file=sys.stderr)
            after = ""
    if status == statuses.LATER and not after:
        print("swarm done: `later` without --after YYYY-MM-DD waits for nobody;"
              " it is recorded like `blocked`", file=sys.stderr)
    result = launch_mod.done(cfg, phase, status, note, force=force, after=after)
    print(result.render())
    # Exit non-zero only when a ping the owner was owed did not go out. A missing
    # supervisor is NOT a failure: the sentinel is durable and `swarm up`
    # reconciles from it, which is precisely what the last line of render() says.
    return 1 if result.ping == "failed" else 0


def _done_refusal(cfg: Config, phase: str, st: state_mod.State) -> str | None:
    """Why ``swarm done <phase>`` must not run, or None."""
    if not ledger_mod.safe_id(phase):
        return f"{phase!r} is not a phase id (letters, digits, '.', '_', '-')"
    own = os.environ.get(cfg.env_marker)
    if own and own != phase:
        return (f"this session is the worker for {own}; it cannot report {phase}."
                f" Did you mean `swarm done {own} ...`?")
    if not st.in_flight(phase):
        return f"{phase} is not in flight (no worker is running it)"
    return None


def _dump(obj) -> int:
    print(json.dumps(obj, indent=2, default=str))
    return 0


def cmd_doctor(cfg: Config, as_json: bool) -> int:
    """Answer "what is wrong with my swarm right now?".

    The supervisor is deliberately minimal (its watchdog only reaps dead panes),
    and the failure modes that matter most produce no log line at all -- a held integration, a dead
    supervisor whose recorded pid still looks alive, a busy slot whose worker
    never received its prompt. Checking for these by hand is slow and error-prone,
    so this command makes the check part of the tool instead of a per-run
    script.
    """
    checks = doctor_mod.run_checks(cfg)
    if as_json:
        _dump([asdict(c) for c in checks])
    else:
        print(doctor_mod.render(checks))
    return doctor_mod.exit_code(checks)


def cmd_why(cfg: Config, phase: str, as_json: bool, tree: bool) -> int:
    """Why is this phase not running? Walks the deps to the root blocker."""
    exp = why_mod.explain(cfg, phase)
    if as_json:
        return _dump(asdict(exp))
    print(why_mod.render(exp, show_tree=tree))
    return 0


def cmd_report(cfg: Config, as_json: bool, decisions: bool, phase: str | None) -> int:
    """What every phase actually did -- the first reader the recaps ever had.

    ``gitq.sentinel_done`` parses the sentinel FILENAME and never opens it, so
    every recap ever written was read once as a phone notification or not at all.
    """
    rep = report_mod.build_report(cfg, phase=phase)
    if as_json:
        return _dump(asdict(rep))
    print(report_mod.render(rep, decisions=decisions))
    return 0


def cmd_gc(cfg: Config, opts: gc_mod.GcOptions, verbose: bool) -> int:
    """Reclaim disk. --dry-run is the default; deletion needs --yes."""
    try:
        plan = gc_mod.plan_gc(cfg, opts)
    except gc_mod.GcRefused as exc:
        print(f"swarm gc refused: {exc}", file=sys.stderr)
        return 1
    if opts.yes:
        try:
            plan = gc_mod.apply(plan, Log(cfg.supervisor_log))
        except gc_mod.GcRefused as exc:
            # Re-checked under the build gate at delete time: a build (or a
            # phase) can have started since the plan was made. Nothing was deleted.
            print(f"swarm gc refused: {exc} (nothing deleted)", file=sys.stderr)
            return 1
    print(gc_mod.render(plan, verbose=verbose))
    return 0


def cmd_recap(cfg: Config, phase: str, force: bool, completion: bool = False) -> int:
    """Summarise one phase.

    Two entry points, both deliberate, neither on a timer. ``--completion`` is
    what ``swarm done`` spawns detached when a phase finishes; it reuses an
    existing good recap rather than regenerating. Without it the caller is the
    owner asking right now, so it always regenerates.
    """
    r = recap_mod.summarize(cfg, phase, on_demand=not completion, force=force)
    print(r.summary or f"no recap for {phase}: {getattr(r, 'reason', 'no source text')}")
    return 0


def cmd_operator_triage(cfg: Config, phase: str) -> int:
    """Decide when one queued operator hand-off should run.

    Spawned detached by ``swarm done`` the moment an item is created, and safe to
    re-run by hand. It always leaves a decision behind — a timeout, prose or an
    unknown verb records ``later``, never ``now`` — so nothing downstream ever has
    to invent one after the model call has already been billed.
    """
    item = opqueue.triage(cfg, phase)
    if item is None:
        print(f"no operator hand-off queued for {phase}", file=sys.stderr)
        return 1
    decision = item.triage
    if decision.get("when") == opqueue.NOW:
        # `now` means it cannot keep. Poke rather than dispatch here: the
        # supervisor is the sole opener of the session, exactly as it is the sole
        # spawner of the master, and it holds the job until its phase has
        # merged. A `later` item drains from the queue sweep once a slot is free.
        _poke(cfg, f"operator {phase}")
    print(
        f"{phase}: {decision.get('when')} "
        f"[{opqueue.group_of(item)}] — {decision.get('why') or 'no reason given'}"
    )
    return 0


def cmd_operator(cfg: Config, phase: str) -> int:
    """Hand one phase to an operator session by hand.

    The owner's way in, and the only way for a hand-off the run will never route
    on its own: a legacy `needs-owner` sentinel has no queue item and
    :func:`opqueue.reconcile` deliberately does not build one (it only rebuilds
    from `operator` sentinels), because auto-routing notes that landed days ago
    would open a session per note on the next ``swarm up``. So the item is built
    here, from the sentinel, one phase at a time and only when asked.
    """
    if not cfg.operator_enabled:
        print(
            "swarm operator: `[operator].enabled` is false — nothing drains the "
            "queue, so nothing is dispatched",
            file=sys.stderr,
        )
        return 2
    item = opqueue.load(cfg, phase)
    if item is None:
        status, note = recap_mod.sentinel(cfg, phase)
        if status is None:
            print(f"swarm operator: no `swarm done` sentinel for {phase}", file=sys.stderr)
            return 1
        item = opqueue.add(cfg, phase, status=status, note=note)
        if item is None:
            print(f"swarm operator: could not queue {phase}", file=sys.stderr)
            return 1
        print(f"queued {phase} from its {status} sentinel")
    if item.terminal:
        print(f"swarm operator: {phase} hand-off is already {item.state}", file=sys.stderr)
        return 1
    if str((item.triage or {}).get("when", "")) == opqueue.LATER:
        # Asking by hand is the owner saying it cannot keep: a `later` triage
        # must not hold it past its merge. Read off the triage itself, not
        # `deferred()`: a supervisor started before a job could be put back
        # mid-work still defers on the triage alone.
        opqueue.set_triage(cfg, phase, when=opqueue.NOW, why="opened by hand",
                           group=str(item.triage.get("group", "")), source="owner")
    heard = _poke(cfg, f"operator {phase}")
    print(f"operator {phase}: {item.note or '(no brief)'}")
    if operator_mod._in_flight(state_mod.read(cfg), phase):
        print(f"  held until {phase} merges: a job never opens before its work is on main")
    print(f"  supervisor: {'poked' if heard else 'NOT RUNNING — nothing will open'}")
    return 0


def _operator_done_hold(mode: str, attention: bool) -> str | None:
    """Why an ``operator-done`` outcome is not sent to the phone, or ``None``.

    ``[operator].notify``: ``attention`` (the default) sends only an outcome the
    session flagged, ``all`` sends every one, ``none`` sends none.
    """
    if mode == "all" or (mode == "attention" and attention):
        return None
    if mode == "none":
        return "[operator].notify = none"
    return "routine outcome (no --attention); it goes in the Overseer's summary"


def cmd_operator_done(
    cfg: Config, phase: str, outcome: str = "", attention: bool = False,
    not_before: str = "",
) -> int:
    """The session signals its job is finished, with a one-line outcome.

    The outcome is recorded on the item, in the notification ledger, in the
    phase's history (through the ledger writer) and in the next Overseer digest. Routine outcomes arrive folded into the Overseer's
    summary, unless ``[operator].notify`` says otherwise; ``--attention`` sends
    it. A decision the owner has to make is never an outcome: the session asks
    it with ``swarm waiting`` while it is still there to act on the answer.

    ``--not-before`` is "not yet": the job goes back in the queue until then
    (:func:`opqueue.later`) instead of being finished, for work whose moment has
    not come — a date the owner set, data that lands tomorrow.
    """
    if not_before:
        try:
            when = opqueue.parse_not_before(not_before)
        except ValueError as exc:
            print(f"swarm operator-done: {exc}", file=sys.stderr)
            return 2
        item = opqueue.later(cfg, phase, when, outcome)
        if item is None:
            print(f"swarm operator-done: no live operator job {phase}", file=sys.stderr)
            return 1
        heard = _poke(cfg, f"operator-done {phase}")
        print(f"operator-done {phase}: queued again, not before {_clock(when)}")
        print(f"  supervisor: {'poked' if heard else 'not running — lease clears on expiry'}")
        return 0
    item = opqueue.complete(cfg, phase, outcome, attention)
    if item is None:
        print(f"swarm operator-done: no live operator job {phase}", file=sys.stderr)
        return 1
    # The outcome goes in its phase's history, written by the swarm like every report.
    row = opqueue.owning_phase(phase)
    if item.outcome and row in ledger_mod.load(cfg.project_dir / cfg.ledger):
        ledgerw.queue(cfg, ledgerw.NOW, {"kind": "record", "phase": row, "outcome": "note",
                                         "note": item.outcome, "by": f"operator job {phase}"})
        _poke(cfg, "ledger")
    tail = f" — {item.outcome}" if item.outcome else " (no outcome given)"
    mode = cfg.operator_notify
    if mode == "attention" and telegram.sends_all(cfg):
        mode = "all"  # `[telegram].pings = "all"` restores every ping, this one too
    hold = _operator_done_hold(mode, attention)
    head = f"swarm: operator job {phase} {'needs your attention' if attention else 'is done'}"
    telegram.notify(
        cfg.telegram_notify,
        f"{head}{tail}",
        kind="operator-done",
        phase=phase,
        source="cli.operator-done",
        state_dir=cfg.state_dir,
        suppressed=hold,
    )
    # The item is settled whatever happens next; only the session's own lease
    # (and, under worktree isolation, the merge of its mirror) rides on the
    # poke, and a lost one holds the lease until it expires. Say so.
    heard = _poke(cfg, f"operator-done {phase}")
    print(f"operator-done {phase}")
    print(f"  owner: {'pinged' if hold is None else 'not pinged — ' + hold}")
    print(f"  supervisor: {'poked' if heard else 'not running — lease clears on expiry'}")
    return 0


def cmd_operator_hold(cfg: Config, job: str, span: str, why: str) -> int:
    """The session declares long work it has to sit through: a longer lease.

    A lease is one hour, and a session still there when it ran out was closed
    in the middle of its work. This moves it to the time the session gives,
    with what the work is; ``swarm status`` shows both, and the lease is
    reclaimed past that time as it was past the hour. Only the session carrying
    the job out may run it (it is told its job in ``SWARM_OPERATOR_JOB``), and
    never for longer than :data:`opqueue.HOLD_MAX_S` at once: a longer wait is
    one the session does not have to sit through, and goes back to the queue
    with ``operator-done --not-before``.
    """
    why = " ".join((why or "").split())
    now = time.time()
    try:
        until = opqueue.parse_not_before(span, now)
    except ValueError as exc:
        print(f"swarm operator-hold: {exc}", file=sys.stderr)
        return 2
    limit = opqueue.HOLD_MAX_S
    if not why or until <= now or until > now + limit:
        problem = (
            "say what the long work is" if not why
            else f"{span!r} is not in the future" if until <= now
            else f"{span!r} is longer than {limit / 3600:g}h, the most one call may hold."
                 " If the wait needs no session, put the job back instead: swarm"
                 f' operator-done {job} "<where you stopped>" --not-before <when>.'
                 " If it does, hold for less and run this again before it runs out"
        )
        print(f"swarm operator-hold: {problem}", file=sys.stderr)
        return 2
    if os.environ.get(operator_mod.JOB_ENV) != job:
        print(f"swarm operator-hold: only the session carrying out {job} can hold its lease",
              file=sys.stderr)
        return 1
    log = Log(cfg.supervisor_log)
    try:
        item = operator_mod.hold(cfg, job, until, why, log, now)
    finally:
        log.close()
    if item is None:
        print(f"swarm operator-hold: no running operator job {job}", file=sys.stderr)
        return 1
    print(f"operator-hold {job}: yours until {_clock(item.lease_until)} — {item.hold_why}")
    print("  past that time the session is closed and the job queued again;")
    print("  run this again before then if the work needs longer")
    return 0


def _clock(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _job_brief(note: str, limit: int = 160) -> str:
    """A job's brief cut to what fits beside a question on a phone screen."""
    return note if len(note) <= limit else note[: limit - 1].rstrip() + "…"


def cmd_operator_add(cfg: Config, brief: str, phase: str | None = None,
                     not_before: str = "") -> int:
    """Queue an ad-hoc operator job — the Overseer's (and the owner's) way in.

    It joins the same queue as a phase's hand-off and is dispatched by the same
    sweep, oldest first; the poke only wakes the supervisor to look.
    ``--not-before`` holds it until then.
    """
    if not cfg.operator_enabled:
        print(
            "swarm operator-add: `[operator].enabled` is false — no operator would"
            " ever run it, so nothing was queued",
            file=sys.stderr,
        )
        return 2
    if not brief.strip():
        print("swarm operator-add: empty brief", file=sys.stderr)
        return 2
    if phase is not None and not opqueue.ID_RE.match(phase):
        print(f"swarm operator-add: {phase!r} is not a phase id", file=sys.stderr)
        return 2
    try:
        when = opqueue.parse_not_before(not_before) if not_before else 0.0
    except ValueError as exc:
        print(f"swarm operator-add: {exc}", file=sys.stderr)
        return 2
    item = opqueue.add_adhoc(cfg, brief, phase, not_before=when)
    if item is None:
        print("swarm operator-add: could not queue the job", file=sys.stderr)
        return 1
    heard = _poke(cfg, f"operator-queued {item.phase}")
    print(f"queued operator job {item.phase}: {item.note}")
    if when:
        print(f"  not before {_clock(when)}")
    print(f"  supervisor: {'poked' if heard else 'NOT RUNNING — it runs on the next swarm up'}")
    return 0


def _live_pass(cfg: Config) -> tuple[str | None, state_mod.State]:
    """The pass a session means: its own (``SWARM_OVERSEER_PASS``), else the current one."""
    st = state_mod.read(cfg)
    return os.environ.get("SWARM_OVERSEER_PASS") or st.overseer_pass, st


def cmd_overseer(cfg: Config, now: bool, as_json: bool, limit: int) -> int:
    """List recent Overseer passes, or (``--now``) ask for one straight away."""
    if now:
        if not cfg.overseer_enabled:
            print("swarm overseer: `[overseer].enabled` is false — no pass would run",
                  file=sys.stderr)
            return 2
        heard = _poke(cfg, "overseer-now")
        print("Overseer pass requested")
        print(f"  supervisor: {'poked — it starts once the pane is free' if heard else 'NOT RUNNING'}")
        return 0 if heard else 1
    st = state_mod.read(cfg)
    passes = ovrecord.load_passes(cfg, limit=limit, live=st.live_passes())
    pending = overseer_mod.Policy(cfg).pending
    if as_json:
        return _dump({
            "enabled": cfg.overseer_enabled,
            "live": st.overseer_pass,
            "pending": [{"key": r.key, "text": r.text, "urgent": r.urgent, "at": r.at}
                        for r in pending],
            "passes": [p.to_dict() for p in passes],
        })
    head = "overseer" + ("" if cfg.overseer_enabled else " (OFF)")
    print(f"{head}: live={st.overseer_pass or '-'} pending={len(pending)}")
    for r in pending:
        print(f"  pending: {'[urgent] ' if r.urgent else ''}{r.text}")
    if not passes:
        print("no passes yet")
    for p in passes:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(p.started_at)) if p.started_at else "?"
        dur = f" {p.duration_s / 60:.0f}m" if p.duration_s is not None else ""
        why = ",".join(str(r.get("key", "?")) for r in p.reasons) or "-"
        print(f"{when} {p.id} [{p.status}{dur}] ({why}) — {p.summary or '(no summary)'}")
        if p.left:
            print(f"    left for the owner: {' '.join(p.left.split())[:200]}")
        if p.question and not p.answer:
            print(f"    WAITING ON YOU: {p.question}")
    return 0


def cmd_big_picture(cfg: Config, now: bool, as_json: bool) -> int:
    """Where the big-picture doc stands, or (``--now``) ask for a pass straight away."""
    if now:
        heard = _poke(cfg, "big-picture-now")
        print("big-picture pass requested")
        print(f"  supervisor: {'poked — it starts unless one is running' if heard else 'NOT RUNNING'}")
        return 0 if heard else 1
    mem = bigpic_mod.load(cfg)
    if as_json:
        return _dump({"enabled": bigpic_mod.enabled(cfg), "doc": cfg.big_picture_doc,
                      "every": cfg.big_picture_every, "max_age_h": cfg.big_picture_max_age_h,
                      **asdict(mem)})
    print(bigpic_mod.status_text(cfg, mem))
    print(f"  doc: {cfg.big_picture_doc}"
          + (f" (last commit {mem.last_commit})" if mem.last_commit else ""))
    if mem.last_summary:
        print(f"  last pass said: {mem.last_summary}")
    return 0


def cmd_big_picture_done(cfg: Config, summary: str) -> int:
    """The big-picture session signals its draft is written. Refused while the
    draft is missing or over the cap, so the session can still fix it."""
    pid = os.environ.get(bigpic_mod.PASS_ENV) or bigpic_mod.load(cfg).live
    if not pid:
        print("swarm big-picture-done: no big-picture pass is running", file=sys.stderr)
        return 1
    draft = bigpic_mod.draft_path(cfg, pid)
    try:
        text = draft.read_text(encoding="utf-8")
    except OSError:
        text = ""
    bad = bigpic_mod.check_draft(text)
    if bad == bigpic_mod.NO_DRAFT:
        print(f"swarm big-picture-done: write the draft to {draft} first", file=sys.stderr)
        return 1
    if bad == bigpic_mod.TOO_BIG:
        print(f"swarm big-picture-done: the draft is {len(text.encode('utf-8'))} bytes;"
              f" cut it to {bigpic_mod.MAX_BYTES} or less, then run this again", file=sys.stderr)
        return 1
    summary = " ".join(summary.split())[: bigpic_mod.SUMMARY_MAX]
    heard = _poke(cfg, f"big-picture-done {pid} {summary}".rstrip())
    print(f"big-picture-done {pid}")
    print(f"  supervisor: {'poked — it lands the doc' if heard else 'not running'}")
    return 0 if heard else 1


def cmd_overseer_done(cfg: Config, summary: str) -> int:
    """The Overseer signals its pass is over, with a one-line summary.

    Recorded before the poke, like ``swarm done``: the record is the durable
    account of the pass whether or not the supervisor hears the signal."""
    pid, st = _live_pass(cfg)
    if not pid or ovrecord.load_json(cfg, pid) is None:
        print("swarm overseer-done: no Overseer pass is running", file=sys.stderr)
        return 1
    summary = " ".join(summary.split())
    ovrecord.update(cfg, pid, status=ovrecord.DONE, ended_at=time.time(), summary=summary)
    ovrecord.append_summary(cfg, pid, summary or "(no summary)")
    parked = state_mod.waiter_key(state_mod.OVERSEER, pid) in st.parked
    if st.overseer_pass != pid and not parked:
        print(f"swarm overseer-done: pass {pid} is no longer live (timed out?) — recorded only",
              file=sys.stderr)
        return 1
    heard = _poke(cfg, f"overseer-done {pid}")
    print(f"overseer-done {pid}")
    print(f"  supervisor: {'poked' if heard else 'not running — the pass ends on the next swarm up'}")
    return 0


def cmd_check(cfg: Config, strict: bool) -> int:
    """Preflight: config, ledger, telegram, prompts -- without a live supervisor.

    A FAIL (or a contradicted prompt line) exits 1. A wasteful prompt line is a
    warning: printed, and fatal only under ``strict``."""
    bad = False
    warned = False
    ok, detail = telegram.check(cfg.telegram_notify)
    print(f"telegram: {'ok' if ok else 'FAIL'} — {detail}")
    bad = bad or not ok
    try:
        ledger_path = cfg.project_dir / cfg.ledger
        graph = ledger_mod.load(ledger_path)
        text = ledger_path.read_text(encoding="utf-8") if ledger_path.is_file() else ""
        issues = ledger_mod.validate(graph, ledger_mod.done_rows(state_mod.read(cfg).done, text))
        print(f"ledger: {len(graph)} phases, {len(issues) or 'no'} issue(s)")
        for i in issues:
            print(f"  - {i}")
        bad = bad or bool(issues)
    except OSError as exc:
        print(f"ledger: FAIL — {exc}")
        bad = True
    cmds = _known_commands()
    for name, path in _prompt_files(cfg):
        try:
            findings = promptlint.lint(path.read_text(encoding="utf-8"), known_commands=cmds)
        except OSError:
            continue
        if findings:
            print(promptlint.render(findings, path=str(path)))
            bad = bad or any(f.severity == promptlint.CONTRADICTED for f in findings)
            warned = warned or any(f.severity != promptlint.CONTRADICTED for f in findings)
    if bad:
        return 1
    if warned:
        print("passed with warnings" + (" (fatal under --strict)" if strict else ""))
        return 1 if strict else 0
    print("all checks passed")
    return 0


def _known_commands() -> set[str]:
    """Every real subcommand, read off the parser so the linter cannot rot."""
    sub = next(
        a for a in _build_parser()._actions
        if isinstance(a, argparse._SubParsersAction)
    )
    return {k for k in sub.choices if not k.startswith("_")}


def _prompt_files(cfg: Config) -> list[tuple[str, Path]]:
    """Every prompt worth linting: the project's worker command file, plus ours.

    The worker command file is the one that actually shapes a run, so a false
    sentence in it (say, "run it synchronously") is paid for in every session
    that reads it.
    """
    out: list[tuple[str, Path]] = []
    cmd_file = str(getattr(cfg, "command_file", "") or "")
    if cmd_file:
        p = Path(cmd_file) if cmd_file.startswith("/") else cfg.project_dir / cmd_file
        if p.is_file():
            out.append((cmd_file, p))
    shipped = Path(__file__).resolve().parent / "prompts"
    if not shipped.is_dir():
        shipped = Path(__file__).resolve().parent.parent.parent / "prompts"
    for name in ("init_master.md", "resolver.md", "operator.md", "overseer.md",
                 "big_picture.md", "owner_guide.md", "console.md"):
        q = shipped / name
        if q.is_file():
            out.append((f"prompts/{name}", q))
    return out


def _snapshot_cfg(cfg: Config) -> Config | None:
    """The config the running supervisor is actually using, or None
    (:func:`reload.snapshot_cfg`)."""
    return reload_mod.snapshot_cfg(cfg)


def cmd_reload(cfg: Config, dry_run: bool) -> int:
    """Apply a .swarm.toml edit to the running swarm.

    Editing the file mid-run was never "requires a restart" -- it was already
    silently HALF applied. The supervisor froze its config at `swarm up`, while
    every `swarm launch` the master shells out re-read the current file. The worst
    case was [git].isolation: launch.py read the NEW value and built a worktree
    while the supervisor read the OLD one and skipped integration, so the phase's
    commits sat on swarm/<phase> forever and the mirror leaked.
    """
    old = _snapshot_cfg(cfg)
    if old is None:
        print("no running supervisor to reload (no config snapshot in the state dir)",
              file=sys.stderr)
        print("  the file is read fresh on the next `swarm up`.", file=sys.stderr)
        return 1
    try:
        new = load(explicit=None, project_dir=str(cfg.project_dir))
    except (ValueError, OSError) as exc:
        print(f"swarm reload: .swarm.toml did not parse — NOTHING was applied\n  {exc}",
              file=sys.stderr)
        print(f"\nstill running: max_workers={old.max_workers} "
              f"isolation={old.git_isolation} park_after={old.park_after}", file=sys.stderr)
        return 1
    st = state_mod.read(cfg)
    payload = reload_mod.plan(old, new, reload_mod.Facts.from_state(st))
    print(reload_mod.render(payload))
    if dry_run:
        return 0
    if not payload.changes:
        return 0
    if not _poke(cfg, "reload"):
        print("\nno supervisor is reading the control FIFO — nothing was applied.",
              file=sys.stderr)
        return 1
    print("\nsupervisor asked to reload.")
    return 0


def cmd_retry(cfg: Config, phases: list[str], all_failed: bool,
              cascade: bool, launch: bool, keep_branch: bool) -> int:
    """Put a failed phase back in play.

    A `fail` is terminal and, until now, invisible and irreversible: ledger.ready
    filters on membership in the done map, `swarm up` re-seeds the status from the
    sentinel, and there was no reset. Recovery meant editing state.json under the
    flock AND deleting the sentinel, or the next boot rehydrated the failure.

    Deleting the sentinel is not optional. gitq.sentinel_done reads the done/
    directory on every `up`, so a cleared map with a surviving `<phase>.fail`
    silently comes back.
    """
    with state_mod.transaction(cfg) as st:
        targets = sorted(p for p, v in st.done.items() if v == "fail") if all_failed else list(phases)
        if not targets:
            print("nothing to retry" if all_failed else "no phase given", file=sys.stderr)
            return 1
        if cascade:
            try:
                graph = ledger_mod.load(cfg.project_dir / cfg.ledger)
                grew = True
                while grew:
                    grew = False
                    for ph, deps in graph.items():
                        if ph not in targets and deps & set(targets) and ph in st.done:
                            targets.append(ph)
                            grew = True
            except OSError:
                pass
        cleared, freed = [], []
        for ph in targets:
            prior = st.done.pop(ph, None)
            if st.clear_phase(ph):
                freed.append(ph)
            if prior is not None:
                cleared.append((ph, prior))
    for ph, prior in cleared:
        for status in statuses.ALL:
            sentinel = cfg.done_dir / f"{ph}.{status}"
            if sentinel.exists():
                sentinel.unlink()
                print(f"  removed sentinel {sentinel.name}")
        print(f"cleared {ph} (was {prior})")
        if cfg.git_isolation == "worktree" and not keep_branch:
            log = Log(cfg.supervisor_log)
            try:
                gitq.discard(cfg, ph, log)
                print(f"  discarded branch swarm/{ph}")
            except gitq.GitError as exc:
                print(f"  branch not discarded: {exc}", file=sys.stderr)
            finally:
                log.close()
    if not cleared:
        print("nothing to retry (no matching done entries)", file=sys.stderr)
        return 1
    print(f"\n{len(cleared)} phase(s) are eligible again")
    if launch:
        for ph, _ in cleared:
            cmd_launch(cfg, ph)
    elif _poke(cfg, "resume"):
        print("supervisor poked — a free slot will pick them up")
    else:
        print("no supervisor is running: `swarm up` launches them,"
              " or `swarm launch <phase>` starts one by hand")
    return 0


def cmd_tui(cfg: Config) -> int:
    """Run the always-on dashboard (tmux window 0).

    The import at module scope is deliberately cheap -- ``tui/__init__`` pulls in
    nothing but ``sys`` and defers Textual to ``main()`` -- because this module is
    also what a worker loads for the ``swarm done`` inside its bash tool call.
    """
    return tui_mod.main(cfg)


def _note_words(words: list[str], kind: str) -> tuple[str, str]:
    """``(text, kind)`` from ``swarm note``'s positional words.

    ``swarm note P decision "…"`` reads naturally and is how the worker directive
    spells it, so a leading bare kind word is the kind rather than the first word
    of the text. Only a separate argument counts: a quoted ``"risk of X"`` is one
    word and stays text.
    """
    if len(words) > 1 and words[0] in notes_mod.KINDS:
        return " ".join(words[1:]), words[0]
    return " ".join(words), kind


def cmd_note(cfg: Config, phase: str, text: str, kind: str) -> int:
    """Record a decision without pinging anyone.

    The third register. `swarm waiting` costs a telegram, a park deadline, the
    grid slot and an unbounded stall; `swarm done ok "btw I decided X"` costs
    nothing. With only those two, every judgement call not worth stopping the
    world got buried in a recap -- and the recap channel is write-only, since
    nothing in the tool has ever read a sentinel note back. A note costs nothing
    AND is readable afterwards.
    """
    if not text.strip():
        print("swarm note: empty note", file=sys.stderr)
        return 2
    note = notes_mod.add(cfg, phase, text, kind)
    print(f"noted: {phase} · {note.kind}")
    print("  surfaces in `swarm report --decisions` and the run's finish summary")
    print("  (not a question — if you cannot proceed correctly without an answer,")
    print("   use `swarm waiting` instead)")
    return 0


def _report_queued(cfg: Config, key: str, what: str, *, quiet: bool = False) -> int:
    """Say when a queued report is written. ``quiet``: a note or a lesson, which
    shares a commit with what the swarm writes next (``[tasks].ledger_batch_s``)."""
    if key == ledgerw.NOW:
        poked = _poke(cfg, "ledger")
        wait = cfg.ledger_batch_s if quiet else 0
        when = (f" with its next ledger commit, within {max(1, round(wait / 60))} min"
                if wait else " now")
        print(f"{what}: queued; the swarm writes it on the target branch"
              + (when if poked else " when it next runs (no supervisor is reading)"))
    else:
        print(f"{what}: queued with {key}; the swarm writes it when {key} lands")
    print("  (the swarm is the ledger's only writer: do not edit the ledger,"
          " the phase history or the lessons file yourself)")
    return 0


def cmd_record(cfg: Config, phase: str, outcome: str, note: str, after: str) -> int:
    """Record an outcome on a row that has no worker of its own to report it.

    For the sessions that are not a phase's worker: an ask records the owner's
    pick on an owner-run row, an operator what it did for a phase, the Overseer
    a finding. A worker reports its own phase with ``swarm done``.
    """
    try:
        if outcome == "later":
            ledgerw.check_date(after)
        if phase not in ledger_mod.load(cfg.project_dir / cfg.ledger) and outcome != "note":
            raise ledgerw.ReportError(f"no ledger row {phase}")
    except ledgerw.ReportError as exc:
        print(f"swarm record: {exc}", file=sys.stderr)
        return 2
    by = os.environ.get("SWARM_SESSION_ID", "") or "owner"
    ledgerw.queue(cfg, ledgerw.NOW, {"kind": "record", "phase": phase, "outcome": outcome,
                                     "note": note, "after": after, "by": by})
    return _report_queued(cfg, ledgerw.NOW, f"{phase} {outcome}", quiet=outcome == "note")


def _ids(value: str | None) -> list[str]:
    """A comma- or space-separated option value as its words."""
    return [t for t in (value or "").replace(",", " ").split() if t]


def cmd_follow_up(cfg: Config, by: str, phase: str, title: str, needs: str, dirs: str,
                  tags: str, scope: str, touches: str = "") -> int:
    """File a new ledger row for work found while building ``by``."""
    try:
        key = ledgerw.file_follow_up(cfg, by, phase, title, _ids(needs), _ids(dirs),
                                     _ids(tags), scope, _ids(touches))
    except ledgerw.ReportError as exc:
        print(f"swarm follow-up: {exc}", file=sys.stderr)
        return 2
    return _report_queued(cfg, key, f"follow-up {phase}")


def cmd_reshape(cfg: Config, by: str, phase: str, why: str, needs: str | None, add: str,
                drop: str, touches: str | None) -> int:
    """Edit an open row's ``needs:`` or ``touches:``, at once.

    The one ledger edit a session makes on an existing row; the swarm applies it
    on the target branch through the ledger gate, and notes who changed what and
    why in the row's history. A refusal leaves the ledger as it was.
    """
    try:
        key = ledgerw.file_reshape(
            cfg, by, phase, why, needs=None if needs is None else _ids(needs),
            add=_ids(add), drop=_ids(drop), touches=None if touches is None else _ids(touches))
    except ledgerw.ReportError as exc:
        print(f"swarm reshape: {exc}", file=sys.stderr)
        return 2
    return _report_queued(cfg, key, f"reshape {phase}")


def cmd_model(cfg: Config, by: str, model: str, rows: list[str], why: str,
              file: str | None) -> int:
    """Set the model the workers of open rows run on, in one ledger write.

    ``model`` is a name claude takes (``sonnet``), or ``default`` to drop the
    field so the row runs on ``[worker].worker_cmd`` as configured. ``--file``
    takes a JSON list of ``{"row", "model", "why"}`` instead, for a sweep where
    each row has its own model and reason.
    """
    try:
        if file is not None:
            raw = json.loads(Path(file).read_text(encoding="utf-8"))
            picked = [(str(r["row"]), str(r["model"]), str(r.get("why") or why)) for r in raw]
        else:
            if not model or not rows:
                raise ledgerw.ReportError("give a model and at least one row, or --file")
            picked = [(r, model, why) for r in rows]
        picked = [(r, "" if m == models_mod.DEFAULT_WORD else m, w) for r, m, w in picked]
        key = ledgerw.file_models(cfg, by, picked)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"swarm model: {exc}", file=sys.stderr)
        return 2
    what = f"model of {picked[0][0]}" if len(picked) == 1 else f"model of {len(picked)} rows"
    return _report_queued(cfg, key, what)


def cmd_escalate(cfg: Config, phase: str, why: str) -> int:
    """Hand a phase back from its row's own model to the swarm's.

    For the worker of a row that runs on a model of its own (:mod:`models`),
    when the phase turns out to be other work than its row says. The hand-back
    is recorded first, so the next launch is on the swarm's model whatever else
    happens; the row's ``model:`` and its history follow through the ledger
    writer, and the supervisor ends this session, drops its mirror (what main
    lacks is archived, never merged) and starts the row again.
    """
    st = state_mod.read(cfg)
    refusal = _done_refusal(cfg, phase, st)
    own = models_mod.default(cfg)
    running = os.environ.get(models_mod.ENV, "") if os.environ.get(cfg.env_marker) \
        else models_mod.override(cfg, phase)
    if refusal is None and phase in st.integrating():
        refusal = f"{phase} has finished and its work is waiting to merge"
    if refusal is None and not any(s.busy and s.phase == phase for s in st.slots):
        refusal = f"{phase} is waiting on the owner, not building"
    if refusal is None and models_mod.handup(cfg, phase) is not None:
        refusal = f"{phase} was already handed up once"
    if refusal is None and not running:
        refusal = (f"{phase} already runs on the swarm's own model"
                   + (f" ({own})" if own else "") + "; there is nothing to hand it to")
    if refusal is None and len(why.strip()) < models_mod.MIN_WHY_CHARS:
        refusal = ("say what you found and why it needs the other model"
                   f" (at least {models_mod.MIN_WHY_CHARS} characters); it is what the"
                   " next worker starts from")
    if refusal is not None:
        print(f"swarm escalate refused: {refusal}", file=sys.stderr)
        return 2
    models_mod.record_handup(cfg, phase, running, own, why.strip())
    try:
        ledgerw.file_models(cfg, phase, [(phase, own, f"handed up by its {running} worker: "
                                          + why.strip())])
    except ledgerw.ReportError as exc:
        # The record above already decides the next launch; the row just keeps
        # its old field until someone sets it.
        print(f"swarm escalate: the row's model: field was not changed ({exc})", file=sys.stderr)
    poked = _poke(cfg, f"escalate {phase}")
    print(f"{phase}: handed up from {running} to {own or 'the default model'}")
    print("  your session ends now; nothing on this branch is merged (it is archived)")
    print(f"  a fresh worker starts {phase} from a clean tree, with your reason in its history")
    if not poked:
        print("  no supervisor is reading: it acts on this when it next starts")
    return 0


def cmd_lesson(cfg: Config, phase: str, text: str, title: str) -> int:
    """Append a lesson to the project's lessons file, through the swarm."""
    if not text.strip():
        print("swarm lesson: empty lesson", file=sys.stderr)
        return 2
    key = ledgerw.key_for(phase)
    ledgerw.queue(cfg, key, {"kind": "lesson", "phase": phase, "text": text, "title": title})
    return _report_queued(cfg, key, "lesson", quiet=True)


def cmd_build(cfg: Config, argv: list[str], *, status: bool = False, as_json: bool = False,
              timeout: str | None = None, script: str | None = None,
              hold: bool = False) -> int:
    """Run a build command through the swarm-wide gate (see :mod:`buildsem`).

    A worker's cwd is a worktree, whose ``.swarm.toml`` may not be the project's:
    :func:`_load_config` reads the project's own file for it, so a ``[build]``
    edit reaches the next call (frozen env overrides still win).
    """
    if status:
        from . import buildstatus

        snap = buildstatus.snapshot(cfg)
        if as_json:
            return _dump(snap)
        print(buildstatus.render(snap))
        return 0
    if argv[:1] == ["--"]:
        argv = argv[1:]
    limit = None
    if timeout is not None:
        limit = _timeout_s(timeout)
        if limit is None:
            print(f"swarm build: --timeout {timeout!r} is not a duration (600, 90s, 10m, 1h30m)",
                  file=sys.stderr)
            return 2
    if script is not None:
        argv = ["bash", "-e", "-o", "pipefail", script, *argv]
    return buildsem.run(cfg, argv, timeout=limit, hold=hold)


def _timeout_s(text: str) -> float | None:
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?", text.strip().lower())
    if not m or not any(m.groups()):
        return None
    h, mins, secs = m.groups()
    total = int(h or 0) * 3600 + int(mins or 0) * 60 + float(secs or 0)
    return total if total > 0 else None


def cmd_keep(cfg: Config, name: str | None, why: str | None, argv: list[str],
             listing: bool = False, stop: str | None = None, cwd: str | None = None,
             as_json: bool = False) -> int:
    """Leave one process running past its session, on the record — or list or stop one.

    Everything a session starts dies with it; this is the exception, for what the
    owner needs after the session is gone (a page to open). The command runs fully
    detached with the session's markers stripped, so no reaper — a session's end
    or ``swarm down`` — matches it.
    """
    if argv and argv[0] == "--":
        argv = argv[1:]
    if stop:
        rec = keep_mod.stop(cfg, stop)
        if rec is None:
            print(f"swarm keep: nothing kept as {stop}", file=sys.stderr)
            return 1
        if rec.alive:
            print(f"swarm keep: {stop} (pid {rec.pid}) survived SIGKILL", file=sys.stderr)
            return 1
        print(f"stopped {stop} (pid {rec.pid})")
        return 0
    if listing or (not name and not argv):
        recs = keep_mod.load_all(cfg)
        if as_json:
            return _dump([r.to_json() for r in recs])
        print("\n".join(keep_mod.line(r) for r in recs) if recs else "nothing kept")
        return 0
    if not name:
        print("swarm keep: --name is required to start one", file=sys.stderr)
        return 2
    try:
        rec = keep_mod.start(cfg, name, argv, why or "", cwd)
    except keep_mod.KeepError as exc:
        print(f"swarm keep: {exc}", file=sys.stderr)
        return 1
    print(f"kept {rec.name}: pid {rec.pid}, log {rec.log}")
    print(f"  why: {rec.why}")
    print(f"  stop: {rec.stop_cmd}")
    if Path(rec.cwd).is_relative_to(Path(cfg.wt_dir).resolve()):
        print(f"  warning: its cwd {rec.cwd} is inside a session mirror, which is removed"
              " when the session's work merges; `--cwd` a canonical path instead",
              file=sys.stderr)
    if not rec.alive:
        print(f"swarm keep: {rec.name} exited at once — read {rec.log}", file=sys.stderr)
        return 1
    return 0


def cmd_context(cfg: Config) -> int:
    st = state_mod.read(cfg)
    print(json.dumps(build_context(cfg, st)))
    return 0


def cmd_master_idle(cfg: Config) -> int:
    _poke(cfg, "master-idle")
    return 0


def cmd_resolved(cfg: Config, phase: str) -> int:
    """Signal that a merge-conflict resolver finished (unblocks the queue)."""
    _poke(cfg, f"resolved {phase}")
    print(f"resolved {phase}")
    return 0


def cmd_widen(cfg: Config, phase: str, texts: list[str]) -> int:
    """Add touches to the lane a phase in flight holds.

    A worker runs it before editing outside its declared lane, so the scheduler
    keeps overlapping rows waiting instead of launching them into its files. It
    never refuses over an overlap with another phase in flight (that work is
    already running); it names each holder, whose merge the widened phase will
    be re-tested against. The snapshot only grows: with none yet, it starts from
    the lane the phase holds now. With lanes off there is no lane to widen, and
    nothing is written: a lanes-off run keeps ``state.json`` free of lane keys.
    """
    if not cfg.lanes_enabled:
        print("swarm widen: lanes are off, so there is no lane to widen; nothing recorded")
        return 0
    try:
        known = launch_mod.known_lanes(cfg)
        touches = [lanes_mod.parse_touch(t, known) for t in texts]
    except lanes_mod.LaneError as exc:
        print(f"swarm widen: {exc}", file=sys.stderr)
        return 2
    with state_mod.transaction(cfg) as st:
        held = launch_mod.lane_view(cfg, st).held
        if phase not in held:
            print(f"swarm widen: {phase} is not in flight", file=sys.stderr)
            return 2
        st.lanes[phase] = sorted({str(t) for t in (*held[phase], *touches)})
    for holder in sorted(p for p in held if p != phase):
        pair = lanes_mod.collide(touches, held[holder], cfg.lanes_commons)
        if pair is not None:
            print(f"{holder} holds {pair[1]}: your merge will be re-tested against it"
                  " and may need a resolver")
    return 0


def cmd_waiting(cfg: Config, who: str, note: str) -> int:
    """Self-report that this session is blocked on the owner: one ping saying
    what is asked and which window to open, then a park deadline
    (:mod:`owner`). Workers, operator jobs and the Overseer alike."""
    try:
        key = owner_mod.resolve(cfg, who)
        what = owner_mod.waiting(cfg, key, note)
    except owner_mod.WaitError as exc:
        print(f"swarm waiting: {exc}", file=sys.stderr)
        return 1
    print(f"waiting {key}: the owner has been told ({what})")
    print("  now ask it here with AskUserQuestion and wait for the answer;")
    print(f'  then run: swarm resumed {who} "<the answer, in one line>"')
    return 0


def cmd_resumed(cfg: Config, who: str, answer: str = "") -> int:
    """Signal the owner answered — cancel a pending park (distinct from `resume`).

    ``answer`` is the owner's answer in one line. It is recorded as an
    ``owner_decision`` note before the poke, so the history keeps it whether or
    not a supervisor is listening; without it the owner's calls were the one kind
    of decision the history never held.
    """
    try:
        key = owner_mod.resolve(cfg, who)
        recorded = owner_mod.resumed(cfg, key, answer)
    except owner_mod.WaitError as exc:
        print(f"swarm resumed: {exc}", file=sys.stderr)
        return 1
    print(f"resumed {key}")
    if recorded:
        print("  recorded as an owner decision (`swarm report --decisions`)")
    return 0


def cmd_integrate(cfg: Config, phase: str) -> int:
    """Manually integrate ``swarm/<phase>`` into main (owner escape hatch)."""
    log = Log(cfg.supervisor_log)
    try:
        result = gitq.integrate(cfg, phase, log)
    finally:
        log.close()
    print(f"integrate {phase}: {result}")
    return 0 if result == gitq.MERGED else 1


def cmd_bootstrap(cfg: Config) -> int:
    _poke(cfg, "bootstrap")
    return 0


def cmd_poke_done(cfg: Config, phase: str, status: str) -> int:
    """Deliver a delayed ``done`` poke (spawned detached by ``swarm done``)."""
    _poke(cfg, f"done {phase} {status}")
    return 0


def cmd_finish(cfg: Config, force: bool = False) -> int:
    """Ask the supervisor to stop — unless a hand-off is still owed.

    A bare `shutdown` was fine while nothing outlived the run. It no longer is:
    the supervisor's `finally` idles the operator pane on the way out, so
    finishing here kills a full-authority session mid-action and throws away
    every queued hand-off without a word. Naming them and refusing costs one
    flag; the alternative costs exactly what `operator` was built to stop losing.
    """
    owed = opqueue.pending(cfg)
    if owed and not force:
        print(f"{len(owed)} operator hand-off(s) still queued:", file=sys.stderr)
        for item in owed:
            print(f"  {item.phase} [{item.state}] — {item.note or '(no brief)'}",
                  file=sys.stderr)
        print("drain them with `swarm operator <phase>`, or `swarm finish --force`",
              file=sys.stderr)
        return 1
    _poke(cfg, "shutdown")
    return 0


def _log_run_ended(cfg: Config, phase: str, reason: str) -> None:
    """Close ``phase``'s run in the history from a CLI command that ended its claim."""
    log = Log(cfg.supervisor_log)
    try:
        logutil.run_ended(log, phase, reason)
    finally:
        log.close()


def cmd_free(cfg: Config, target: str) -> int:
    """Free a slot by id or phase, and wake the supervisor to refill it.

    Two fixes over the original. It reports whether anything was actually freed
    — ``swarm free 9`` used to print ``freed 9`` for a slot that does not exist.
    And it pokes ``resume`` afterwards: ``free`` is reached for exactly one
    reason, a worker that died without ``swarm done``, so waiting for "some other
    worker to finish" to trigger a relaunch can easily mean waiting forever.

    Freeing by phase also clears any waiting/parked record, for the same reason
    ``skip`` does: otherwise ``pending()`` stays true and the run cannot finish.
    """
    freed = False
    was_parked = False
    wait_win = None
    ended: str | None = None  # the phase whose claim this ends, if any
    with state_mod.transaction(cfg) as st:
        if target.isdigit():
            slot = st.slot_by_id(int(target))
            if slot and slot.busy:
                phase = slot.phase
                slot.busy = False
                slot.phase = None
                slot.worktree = None
                slot.branch = None
                freed = True
                ended = phase
                if phase:
                    was_parked = st.clear_phase(phase)
                    wait_win = st.windows.pop(f"wait:{phase}", None)
        else:
            held = target in st.claimed_phases()
            freed = st.free_slot_for(target) is not None or target in st.done
            was_parked = st.clear_phase(target)
            wait_win = st.windows.pop(f"wait:{target}", None)
            freed = freed or was_parked
            ended = target if held else None
    if ended:
        _log_run_ended(cfg, ended, "freed")
    if wait_win and cfg.driver == "tmux":
        tmux.kill_window(wait_win)
    if not freed:
        print(f"nothing to free for {target!r}", file=sys.stderr)
        return 1
    print(f"freed {target}")
    if _poke(cfg, "resume"):
        print("  supervisor poked — a ready phase will fill the slot")
    else:
        print("  no supervisor reading the FIFO; the slot fills on the next `swarm up`")
    return 0


def _summary_hold(cfg: Config, attention: bool) -> str | None:
    """Why an Overseer pass's summary stays off the phone, or ``None`` to send it.

    It goes out on a cadence pass (``[overseer].every_finished``), a pass the
    owner asked for (``swarm overseer --now``), or with ``--attention`` when
    something needs the owner. Every other pass — the clock, starvation, a hold,
    a doctor FAIL, an owner wait — records it and sends nothing, so pings stay
    quiet unless something needs the owner.
    """
    if attention:
        return None
    pid, _ = _live_pass(cfg)
    rec = ovrecord.load_json(cfg, pid) if pid else None
    keys = {str(r.get("key", "")) for r in (rec.reasons if rec else [])}
    if keys & overseer_mod.SUMMARY_TRIGGERS:
        return None
    return telegram.hold(cfg, "not a cadence pass and nothing flagged --attention")


def _notify_entry(cfg: Config, a) -> int:
    if a.ack:
        if a.message is not None:
            print("swarm notify --ack takes no message", file=sys.stderr)
            return 2
        return cmd_notify_ack(cfg)
    if a.message is None:
        print("swarm notify: a message is required (or --ack)", file=sys.stderr)
        return 2
    return cmd_notify(cfg, a.message, a.attention)


def cmd_notify_ack(cfg: Config) -> int:
    """Acknowledge the dropped pings: only drops after now count as not delivered.

    An acknowledged drop stops nagging: a drop already known about need not
    stay in the count. ``notifications.jsonl`` stays as it is — it is the
    record of what happened; the acknowledgement is a moment beside it.
    """
    rows = []
    path = cfg.state_dir / telegram.LEDGER_NAME
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    drops = telegram.open_drops(rows, telegram.acked_at(cfg.state_dir))
    if not drops:
        print("no undelivered pings to acknowledge")
        return 0
    telegram.acknowledge(cfg.state_dir)
    print(f"acknowledged {len(drops)} ping(s) that never reached your phone;"
          " only drops from now on will be counted (the ping log is unchanged)")
    return 0


def cmd_notify(cfg: Config, message: str, attention: bool = False) -> int:
    """Send ``message`` to the owner through the swarm's own sender.

    The master prompts say "telegram the owner" and, until this existed, gave the
    master no swarm-side way to do it - so an LLM master reached for whatever it
    had, and run events then arrived from another sender and never reached
    ``notifications.jsonl``. This is the
    one door: the configured ``[telegram] notify`` script, logged like every other
    swarm ping. Best-effort, like all of them: a failed send is exit 1, never an
    exception.

    Sent from an Overseer pass, it is the pass's summary to the owner.
    """
    kind = "master-note"
    held = None
    if os.environ.get("SWARM_MASTER_KIND") == master_mod.OVERSEER:
        kind = "overseer-digest"
        held = _summary_hold(cfg, attention)
    session = os.environ.get(SESSION_ENV, "")
    if session.startswith("resolver:"):
        # A resolver only messages the owner when it gives up on the conflict.
        _poke(cfg, f"resolver-escalated {session.split(':', 1)[1]}")
    ok = telegram.notify(
        cfg.telegram_notify,
        message,
        kind=kind,
        source="cli.notify",
        state_dir=cfg.state_dir,
        suppressed=held,
    )
    if held:
        # Not a failure: the summary is recorded, just not sent to the phone.
        print(f"recorded, not sent — {held} (pass --attention if it needs the owner)")
        return 0
    print("sent" if ok else "not sent")
    return 0 if ok else 1


def cmd_skip(cfg: Config, phase: str) -> int:
    """Mark a phase done without running it — and let go of everything it held.

    This used to call ``mark_done`` alone, which is only correct for a phase that
    was never started. Skipping a *parked* or *waiting* phase left it in those
    maps, and ``pending()`` is ``any_busy() or parked or waiting`` — so the run
    could never finish, with no CLI able to clear it. Skipping a *busy* phase left
    its slot claimed forever, permanently losing capacity.

    It also writes the ``done/<phase>.skip`` sentinel and pokes a launch pass.
    Without the sentinel a skip lived only in ``state.json``, so the next
    ``swarm up`` forgot it and every dependent re-blocked (a skipped standing target
    starved every dependent after a restart). Without the poke
    a dependent the skip made ready waited for some unrelated event to launch.
    """
    with state_mod.transaction(cfg) as st:
        held = phase in st.claimed_phases()
        asking = st.asking(phase)
        was_parked = st.clear_phase(phase, "skip")
        wait_win = st.windows.pop(f"wait:{phase}", None)
    if held:
        _log_run_ended(cfg, phase, "skipped")
    if was_parked and wait_win and cfg.driver == "tmux":
        tmux.kill_window(wait_win)
    launch_mod._write_sentinel(cfg, phase, "skip", "skipped with `swarm skip`")
    print(f"skipped {phase}")
    if was_parked and asking:
        print("  (it was parked waiting on you — its window is closed)")
    elif was_parked:
        print("  (it was working on your answer in its own window — that window is closed)")
    _poke(cfg, "resume")
    return 0


def _warn_if_no_supervisor(cfg: Config, what: str) -> None:
    """Warn when a control command lands on a state no supervisor is reading.

    ``pause``/``resume`` mutate ``state.json`` directly; run from the wrong
    directory (the CLI resolves the project + slug from the cwd) they write a state
    file no live swarm observes, silently doing nothing — the footgun that makes
    ``swarm pause`` look broken. Surfacing it turns a confusing no-op into a
    visible warning.
    """
    if not _supervisor_running(cfg):
        print(
            f"WARNING: no swarm supervisor is running for {cfg.slug!r} "
            f"(resolved from cwd={cfg.project_dir}) — this {what} affects a state "
            "nothing is reading. Run it from a live swarm's project directory.",
            file=sys.stderr,
        )


def cmd_pause(cfg: Config, delay: str | None = None, at: str | None = None,
              cancel: bool = False) -> int:
    """Pause now, or (``--in`` / ``--at``) schedule the pause for later, or drop
    the one scheduled. There is only ever one: a new schedule replaces it."""
    if cancel:
        return cmd_pause_cancel(cfg)
    if delay or at:
        return cmd_pause_schedule(cfg, delay, at)
    with state_mod.transaction(cfg) as st:
        st.paused = True
    print("swarm paused — no new workers launch; in-flight workers finish")
    _warn_if_no_supervisor(cfg, "pause")
    return 0


def _log_pause_schedule(cfg: Config, message: str) -> None:
    log = Log(cfg.supervisor_log)
    try:
        log.line(message)
    finally:
        log.close()


def cmd_pause_schedule(cfg: Config, delay: str | None, at: str | None) -> int:
    """Record when the supervisor pauses the swarm, and wake it so its next
    wake-up counts the moment in."""
    now = time.time()
    try:
        pause_at = now + pauseat.parse_in(delay) if delay else pauseat.next_at(at or "", now)
    except ValueError as exc:
        print(f"swarm pause: {exc}", file=sys.stderr)
        return 2
    with state_mod.transaction(cfg) as st:
        prior = st.pause_at
        st.pause_at = pause_at
    replaced = f" replaces={pauseat.stamp(prior)}" if prior else ""
    _log_pause_schedule(cfg, f"PAUSE-SCHEDULED at={pauseat.stamp(pause_at)}"
                             f" in={pause_at - now:.0f}s{replaced}")
    _poke(cfg, "pause-scheduled")
    print(f"swarm pauses at {pauseat.when(pause_at, now)} — then no new workers launch;"
          " in-flight workers finish")
    if prior:
        print(f"  this replaces the pause that was scheduled for {pauseat.when(prior, now)}")
    print("  `swarm pause --cancel` drops it; `swarm resume` drops it too")
    _warn_if_no_supervisor(cfg, "pause")
    return 0


def cmd_pause_cancel(cfg: Config) -> int:
    now = time.time()
    with state_mod.transaction(cfg) as st:
        prior = st.pause_at
        st.pause_at = 0.0
        paused = st.paused
    if not prior:
        print("no pause is scheduled")
        return 0
    _log_pause_schedule(cfg, f"PAUSE-SCHEDULE-CANCELLED at={pauseat.stamp(prior)} by=cancel")
    _poke(cfg, "pause-scheduled")
    print(f"scheduled pause at {pauseat.when(prior, now)} cancelled — "
          + ("the swarm is still paused" if paused else "launching carries on"))
    return 0


def cmd_resume(cfg: Config, override_cap: bool = False) -> int:
    """Lift a pause. A usage cap's hold is not a pause: it stays, and says so,
    unless ``--override-cap`` runs through it until its window resets."""
    now = time.time()
    with state_mod.transaction(cfg) as st:
        st.paused = False
        # "Go on" also means not later: a scheduled pause would silently undo it.
        scheduled = st.pause_at
        st.pause_at = 0.0
        # A resume is "go on": it cancels a drain too, unless the stop has begun.
        drained = bool(st.drain) and not st.drain.get("stopping_at")
        was_draining = dict(st.drain) if drained else {}
        if drained:
            st.drain = {}
        hold = dict(st.usage_hold)
        if override_cap:
            for window, h in hold.items():
                # The plain key is what a supervisor older than the account tag reads.
                st.usage_override[window] = h.get("resets_at") or now
                if h.get("account"):
                    st.usage_override[f"{window}@{h['account']}"] = h.get("resets_at") or now
            st.usage_hold = {}
    if scheduled:
        _log_pause_schedule(
            cfg, f"PAUSE-SCHEDULE-CANCELLED at={pauseat.stamp(scheduled)} by=resume")
    _drop_draining_restart(cfg, was_draining, "resume")
    _poke(cfg, "resume")
    if drained:
        print("drain cancelled")
    if scheduled:
        print(f"scheduled pause at {pauseat.when(scheduled, now)} cancelled")
    if hold and not override_cap:
        for line in caps.describe_hold(hold, now):
            print(line)
        print("A usage cap is holding new workers; running workers carry on. To start "
              "new workers anyway until the reset: swarm resume --override-cap")
    elif hold:
        until = ", ".join(f"the {caps.label(w)} reset, {caps.when(h.get('resets_at'), now)}"
                          for w, h in sorted(hold.items()))
        print(f"swarm resumed — usage cap overridden: new workers start again until {until}")
    else:
        if override_cap:
            print("no usage cap is holding; nothing to override")
        print("swarm resumed — launching will fill free slots")
    _warn_if_no_supervisor(cfg, "resume")
    return 0


def _worker_windows(windows: dict[str, str]) -> list[str]:
    """Window ids of the paginated worker grid (``workers``, ``workers-2``, …).

    The master, ``wait:<phase>`` and ``resolve:<phase>`` windows are single-pane
    by construction and are never re-arranged.
    """
    return [
        wid
        for name, wid in windows.items()
        if name == "workers" or name.startswith("workers-")
    ]


def cmd_layout(cfg: Config, name: str | None) -> int:
    """Show, or re-arrange live, how the worker windows stack their slot panes.

    With no argument this prints the effective layout and the valid names. With
    one it records the choice in state (so a later park re-tidy honours it) and
    immediately re-lays out every worker window — no restart, no relaunch: the
    panes and the processes in them are untouched, only their geometry changes.
    """
    if name is None:
        st = state_mod.read(cfg)
        print(f"layout: {st.layout or cfg.tmux_layout} (config default: {cfg.tmux_layout})")
        print(f"choices: {', '.join(tmux.LAYOUTS)}")
        print(f"aliases: {', '.join(sorted(tmux.LAYOUT_ALIASES))}")
        return 0
    try:
        layout = tmux.normalize_layout(name)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    with state_mod.transaction(cfg) as st:
        st.layout = layout
        windows = _worker_windows(st.windows)
    applied = 0
    if cfg.driver == "tmux" and tmux.session_exists(cfg.session):
        for wid in windows:
            tmux.apply_layout(wid, len(tmux.list_panes(wid)), layout)
            applied += 1
    print(f"layout {layout} — re-arranged {applied} worker window(s)")
    if layout != cfg.tmux_layout:
        print(f'set `layout = "{layout}"` under [tmux] in .swarm.toml to make it the default')
    return 0


def cmd_todo(cfg: Config, as_json: bool) -> int:
    """Everything waiting on the owner that is not a question (:mod:`todo`)."""
    todos = todo_mod.collect(cfg)
    if as_json:
        return _dump(todos.to_dict())
    print(todo_mod.render(todos))
    return 0


def cmd_console(cfg: Config, new: bool) -> int:
    """Open the owner console, or move to it; ``--new`` starts a fresh conversation."""
    try:
        print(console_mod.open_words(cfg, new))
    except console_mod.ConsoleError as exc:
        print(f"swarm console: {exc}", file=sys.stderr)
        return 2
    _attach(cfg)  # from a terminal outside the session, land in it
    return 0


def cmd_guide(cfg: Config) -> int:
    """Open the owner's guide in its own tmux window, or move to the live one."""
    try:
        print(guide_mod.open_guide(cfg))
    except guide_mod.GuideError as exc:
        print(f"swarm guide: {exc}", file=sys.stderr)
        return 2
    return 0


def _operator_lines(cfg: Config, st: state_mod.State) -> list[str]:
    """The operator queue for ``swarm status``: counts, then the current job."""
    items = opqueue.load_all(cfg)
    if not items and not st.operator_phase:
        return []
    counts = {k: sum(1 for i in items if i.state == k) for k in opqueue.STATES}
    head = "operator" + ("" if cfg.operator_enabled else " (OFF — nothing drains it)")
    lines = [
        f"{head}: queued={counts[opqueue.QUEUED]} running={counts[opqueue.RUNNING]}"
        f" waiting-on-owner={counts[opqueue.WAITING]} done={counts[opqueue.DONE]}"
        f" abandoned={counts[opqueue.ABANDONED]}"
    ]
    for i in items:
        if i.phase == st.operator_phase:
            continue
        if i.state == opqueue.WAITING:
            key = state_mod.waiter_key(state_mod.OPERATOR, i.phase)
            lines.append(f"  {i.phase} [WAITING ON YOU in {state_mod.wait_window(key)}]:"
                         f" {i.question}")
        elif i.state == opqueue.QUEUED and i.run_after > time.time():
            # A job a session put back mid-work says where it stopped, not its brief.
            lines.append(f"  {i.phase} [not before {_clock(i.run_after)}] — "
                         f"{_job_brief(i.resume_note or i.note)}")
    current = next((i for i in items if i.phase == st.operator_phase), None)
    if current is not None:
        what = (
            f"WAITING ON YOU: {current.question}"
            if current.state == opqueue.WAITING
            else opqueue.standing(current)
        )
        lines.append(f"  current: {current.phase} [{what}] — {_job_brief(current.note)}")
    elif st.operator_phase:
        lines.append(f"  current: {st.operator_phase} (no queue item)")
    return lines


def _done_counts(done: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in done.values():
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _done_summary(done: dict[str, str]) -> str:
    """The done map as counts per status, naming the failures (they need you).

    These are this machine's records only — phases built elsewhere and ticked in
    the ledger are not among them — so it is labelled as such; how far along the
    ledger is, is :func:`_phase_standing`'s line.
    """
    parts = " ".join(f"{k}={v}" for k, v in _done_counts(done).items())
    failed = sorted(p for p, s in done.items() if s == statuses.FAIL)
    tail = f" failed: {' '.join(failed)}" if failed else ""
    return f"records here: {len(done)} ({parts or 'none'}){tail} — `--all` lists every phase"


def _phase_standing(cfg: Config, st) -> dict:
    """The whole ledger counted exactly as the dashboard counts it.

    :func:`tui.campaign.summarise` over the launcher's done view, so this line,
    the TUI header and the web board can never disagree about what is done.
    ``done``, ``running``, ``asking``, ``ready``, ``blocked``, ``dated`` and
    ``failed`` are disjoint and add up to ``total``.
    """
    from .tui import campaign

    path = cfg.project_dir / cfg.ledger
    graph = ledger_mod.load(path)
    ticked = ledger_mod.load_ticked(path)
    dated = ledgerw.dated(cfg)
    # At work: in a slot, or parked and working on the owner's answer.
    busy = {s.phase for s in st.busy_slots() if s.phase} | set(st.working_parked())
    # Its worker waits on the owner, in its slot or parked: the launcher will
    # not start it, so it is never ready, and it builds nothing until answered.
    asking = set(st.on_owner())
    landed = ledger_mod.with_ticked(st.done, ticked, busy | set(st.parked) | set(st.waiting))
    t = campaign.overall(campaign.summarise(graph, landed, busy, set(cfg.exclude or []), ticked,
                                            dated, asking))
    return {"done": t.built, "total": t.live_total, "held": t.held, "running": len(t.running),
            "asking": len(t.asking), "ready": len(t.ready), "blocked": t.blocked,
            "dated": t.dated, "failed": t.failed, "excluded": t.excluded,
            "dates": {p: d for p, d in sorted(dated.items(), key=lambda kv: kv[::-1])
                      if p in graph and p not in busy and p not in asking}}


def _standing_line(n: dict) -> str:
    held = (f" ({n['held']} of them done by the swarm, still open in the ledger)"
            if n["held"] else "")
    words = {"asking": "waiting on you", "dated": "waiting for a date"}
    rest = " · ".join(f"{n[k]} {words.get(k, k)}"
                      for k in ("running", "asking", "ready", "blocked", "dated", "failed")
                      if n.get(k))
    excluded = f" · {n['excluded']} yours to do, not counted" if n["excluded"] else ""
    return (f"phases: {n['done']} of {n['total']} done{held}"
            f"{' · ' + rest if rest else ''}{excluded}")


def _dates_line(dates: dict[str, str]) -> str:
    """Which rows wait for a date, soonest first: the swarm starts each that day."""
    shown = [f"{p} ({d})" for p, d in list(dates.items())[:8]]
    more = f" and {len(dates) - 8} more" if len(dates) > 8 else ""
    return f"waiting for a date: {', '.join(shown)}{more}"


def _forecast(cfg: Config, st):
    """``(forecast, "")`` — the dashboard's own, from its cache when it still
    stands — or ``(None, why)``: a status line must never fail on a forecast."""
    from .eta import engine as eta_engine

    try:
        return eta_engine.current(cfg, eta_engine.from_files(cfg, st)), ""
    except Exception as exc:  # noqa: BLE001 - status reports; it does not crash
        return None, f"{type(exc).__name__}: {exc}"


def cmd_status(cfg: Config, as_json: bool = False, show_all: bool = False) -> int:
    """The state, for a person or (``--json``) a script. The done map grows with
    the ledger — thousands of entries on a long-lived project — so it is counted
    unless ``--all`` asks for every phase.

    A ``later`` the ledger has not taken yet is recorded ``fail`` for the
    launcher's sake only (:func:`ledgerw.not_failed`): every line here reads it
    as what it is, a row waiting for its date."""
    st = state_mod.read(cfg)
    st = replace(st, done=ledgerw.not_failed(st.done, ledgerw.dated(cfg)))
    if as_json:
        data = asdict(st)
        if not show_all:
            data.pop("done")
            data["done_counts"] = _done_counts(st.done)
            data["failed"] = sorted(p for p, s in st.done.items() if s == statuses.FAIL)
        data["phases"] = _phase_standing(cfg, st)
        data["config"] = {
            "name": cfg.name, "slug": cfg.slug, "driver": cfg.driver,
            "isolation": cfg.git_isolation,
            "main_branch": cfg.git_main_branch, "layout": st.layout or cfg.tmux_layout,
            "state_dir": str(cfg.state_dir),
        }
        data["operator_jobs"] = [i.to_dict() for i in opqueue.load_all(cfg)]
        data["web"] = web_lifecycle.status_line(cfg)
        data["telegram_bot"] = tgbot.status_line(cfg)
        data["kept"] = [r.to_json() for r in keep_mod.load_all(cfg)]
        data["resources"] = resources_view.status_lines(cfg)
        data["drain_line"] = drain_mod.line(st.drain)
        data["pause_line"] = pauseat.line(st.pause_at)
        data["restart"] = restart_mod.load(cfg)
        data["restart_line"] = restart_mod.status_line(cfg, st)
        data["big_picture"] = bigpic_mod.status_text(cfg, bigpic_mod.load(cfg))
        data["owner_rows"] = [{"row": r, "blocks": n}
                              for r, n in owner_mod.current_owner_rows(cfg, st)]
        data["owner_todos"] = todo_mod.count(cfg)
        fc, why = _forecast(cfg, st)
        data["eta"] = fc.to_json() if fc is not None else {"unavailable": why}
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    lines = [
        f"name={cfg.name} slug={cfg.slug} driver={cfg.driver} finished={st.finished}"
        f" paused={st.paused}"
        f" layout={st.layout or cfg.tmux_layout}",
        f"master_alive={st.master_alive} supervisor_pid={st.supervisor_pid}",
        f"isolation={cfg.git_isolation} main={cfg.git_main_branch}"
        f" integ_blocked={st.integ_blocked} integ_queue={st.integ_queue}",
    ]
    if st.drain:
        lines.insert(1, drain_mod.line(st.drain))
    if st.pause_at:
        lines.insert(1, pauseat.line(st.pause_at))
    restarting = restart_mod.status_line(cfg, st)
    if restarting:
        lines.insert(1, restarting)
    row_models = models_mod.overrides(cfg)
    for s in st.slots:
        mark = f"BUSY {s.phase}" if s.busy else "free"
        wt = f" branch={s.branch}" if s.branch else ""
        on = f" model={row_models[s.phase]}" if s.busy and s.phase in row_models else ""
        lines.append(f"  slot {s.id} pane={s.pane_id} {mark}{wt}{on}")
    if st.waiting or st.parked:
        # A parked session the owner answered is working again, in its own window.
        working = st.working_parked()
        lines.append(f"waiting={sorted(st.waiting)} parked={st.parked}"
                     + (f" working={working}" if working else ""))
    # A live operator session holds the owner's own authority on the host. It has
    # no slot and no pane probe can find it, so these lines are the only place the
    # text UI can say one is running at all.
    lines.extend(_operator_lines(cfg, st))
    # Rows only the owner can do, that hold other rows up: nothing asks about them.
    lines.extend(f"yours to do: {r} (holds up {n})"
                 for r, n in owner_mod.current_owner_rows(cfg, st))
    lines.append(todo_mod.status_line(todo_mod.count(cfg)))
    for line in pushowed.describe(st.push_owed):
        lines.append(f"push owed: {line}")
    lines.extend(caps.summary_for(cfg, st.usage_hold))
    standing = _phase_standing(cfg, st)
    lines.append(_standing_line(standing))
    if standing["dates"]:
        lines.append(_dates_line(standing["dates"]))
    fc, why = _forecast(cfg, st)
    if fc is not None:
        from .tui import books

        lines.extend(books.status_lines(fc, time.time()))
    else:
        lines.append(f"eta: unavailable ({why})")
    lines.append(f"done={st.done}" if show_all else _done_summary(st.done))
    from . import buildstatus

    gate = buildstatus.summary_line(cfg)
    if gate:
        lines.append(gate)
    lines.append(web_lifecycle.status_line(cfg))
    lines.append(tgbot.status_line(cfg))
    lines.append(bigpic_mod.status_text(cfg, bigpic_mod.load(cfg)))
    # What `swarm keep` left running on purpose: nothing else outlives its session.
    lines.extend(f"kept: {keep_mod.line(r)}" for r in keep_mod.load_all(cfg))
    lines.extend(resources_view.status_lines(cfg))
    print("\n".join(lines))
    return 0


def cmd_resources(cfg: Config, as_json: bool = False, hours: float = 24.0,
                  days: float = 30.0) -> int:
    """What the host, the builds and the workers used: now, the last day, the
    worst builds, and whether the gate or the worker count could go up."""
    data = resources_view.collect(cfg, hours=hours, days=days)
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(resources_view.render(data))
    return 0


# -- parser ---------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarm", description=__doc__)
    p.add_argument("--config", help="path to .swarm.toml (default: ./.swarm.toml)")
    p.add_argument("--project-dir",
                   help="project directory (default: $SWARM_PROJECT in a launched session, else cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="set up + start the supervisor, then attach")
    up.add_argument(
        "--no-attach",
        action="store_true",
        help="don't attach the terminal to the swarm tmux session after bringing it up",
    )
    up.set_defaults(func=lambda cfg, a: cmd_up(cfg, attach=not a.no_attach))
    dnp = sub.add_parser(
        "down", help="stop the supervisor + tear down; --drain waits for running work first")
    dnp.add_argument("--drain", action="store_true",
                     help="launch nothing new; stop once the running work is finished")
    dnp.add_argument("--then", metavar="CMD", default="",
                     help="a shell command to run after the stop, detached from the swarm"
                          " (e.g. 'sleep 120; sudo shutdown now')")
    dnp.add_argument("--cancel", action="store_true", help="cancel a pending --drain")
    dnp.set_defaults(func=lambda cfg, a: cmd_down_verb(cfg, a.drain, a.then, a.cancel))
    sub.add_parser("_drain-down").set_defaults(func=lambda cfg, a: cmd_drain_down(cfg))
    rsp = sub.add_parser(
        "restart",
        help="load the code on disk: replace the supervisor in place (nothing else is"
             " touched), now or later; --full drains, stops and starts again",
        description="Restart the supervisor in place: the tmux session, the workers, every"
                    " session waiting on you, the operator and your console are not touched,"
                    " and the new supervisor carries on from where the old one was. With"
                    " --full: drain, `swarm down`, `swarm up` (a new tmux session).",
    )
    when = rsp.add_mutually_exclusive_group()
    when.add_argument("--in", dest="delay", metavar="DURATION",
                      help="restart this long from now: 12h, 90m, 1h30m, 2d")
    when.add_argument("--at", metavar="HH:MM", help="restart at the next HH:MM, local time")
    when.add_argument("--now", action="store_true", help="restart now (the default)")
    when.add_argument("--cancel", action="store_true",
                      help="drop a planned restart, or stop one still waiting")
    rsp.add_argument("--full", action="store_true",
                     help="drain the workers, then `swarm down` and `swarm up`; refused while"
                          " a session waits on you unless one of the next three is given")
    ask = rsp.add_mutually_exclusive_group()
    ask.add_argument("--wait-questions", dest="policy", action="store_const",
                     const=restart_mod.WAIT,
                     help="--full: also wait until every waiting session is answered")
    ask.add_argument("--keep-questions", dest="policy", action="store_const",
                     const=restart_mod.KEEP,
                     help="--full: carry the waiting sessions across the restart, alive")
    ask.add_argument("--force", dest="policy", action="store_const", const=restart_mod.FORCE,
                     help="--full: close the waiting sessions (their work is kept)")
    rsp.add_argument("--no-wait", action="store_true",
                     help="return at once instead of following the restart")
    rsp.set_defaults(policy=None, func=lambda cfg, a: cmd_restart(
        cfg, a.delay, a.at, a.cancel, a.full, a.policy, follow=not a.no_wait))
    rrp = sub.add_parser("_restart-run")
    rrp.add_argument("plan")
    rrp.add_argument("--at", type=float, default=None)
    rrp.set_defaults(func=lambda cfg, a: cmd_restart_run(cfg, a.plan, a.at))
    sub.add_parser(
        "reset", help="start a fresh run: ETA and usage count from now (nothing restarts)"
    ).set_defaults(func=lambda cfg, a: cmd_reset(cfg))
    usp = sub.add_parser(
        "usage",
        help="this run's 5-hour and weekly usage per hour, and past runs",
        description="Per-run usage: hours elapsed, average 5-hour and weekly %%/h, the "
                    "5-hour windows spanned, phases and $/h. " + usage_mod.SKEW_NOTE,
    )
    usp.add_argument("--json", action="store_true", help="machine-readable")
    usp.add_argument("-n", "--last", type=int, default=10, help="past runs to list (default 10)")
    usp.set_defaults(func=lambda cfg, a: cmd_usage(cfg, a.json, a.last))
    svp = sub.add_parser("_supervise")
    svp.add_argument("--adopt", action="store_true")
    svp.set_defaults(func=lambda cfg, a: cmd_supervise(cfg, a.adopt))
    cxp = sub.add_parser("context", help="print the read-only state snapshot (JSON)")
    cxp.add_argument("--json", action="store_true", help="accepted: the output is JSON anyway")
    cxp.set_defaults(func=lambda cfg, a: cmd_context(cfg))
    sub.add_parser("master-idle", help="signal the master finished a pass").set_defaults(
        func=lambda cfg, a: cmd_master_idle(cfg))
    sub.add_parser("bootstrap", help="ask the supervisor to spawn the init master").set_defaults(
        func=lambda cfg, a: cmd_bootstrap(cfg))
    lc = sub.add_parser("_lane-check")  # detached by the landing
    lc.add_argument("phase")
    lc.add_argument("repo", help="the repo's lane: its name, '.' for the umbrella")
    lc.set_defaults(func=lambda cfg, a: landing_mod.run_check(cfg, a.phase, a.repo))
    pd = sub.add_parser("_poke-done")
    pd.set_defaults(func=lambda cfg, a: cmd_poke_done(cfg, a.phase, a.status))
    pd.add_argument("phase")
    pd.add_argument("status")

    rp = sub.add_parser("resolved", help="signal a merge-conflict resolver finished")
    rp.add_argument("phase")
    rp.set_defaults(func=lambda cfg, a: cmd_resolved(cfg, a.phase))

    wdp = sub.add_parser(
        "widen", help="add touches to a running phase's lane, before editing outside it")
    wdp.add_argument("phase")
    wdp.add_argument("touches", nargs="+", metavar="touch")
    wdp.set_defaults(func=lambda cfg, a: cmd_widen(cfg, a.phase, a.touches))

    wp = sub.add_parser(
        "waiting", help="report this session is blocked on the owner (pings; may park it)"
    )
    wp.add_argument("phase", metavar="who",
                    help="a worker's phase, an operator job id, or `overseer`")
    wp.add_argument("note", nargs="*", default=[], help="the question, for the owner ping")
    wp.set_defaults(func=lambda cfg, a: cmd_waiting(cfg, a.phase, " ".join(a.note)))

    rsp = sub.add_parser(
        "resumed", help="report the owner answered — cancel a pending park"
    )
    rsp.add_argument("phase", metavar="who",
                     help="a worker's phase, an operator job id, or `overseer`")
    rsp.add_argument("answer", nargs="*", default=[],
                     help="the owner's answer in one line — recorded in the history")
    rsp.set_defaults(func=lambda cfg, a: cmd_resumed(cfg, a.phase, " ".join(a.answer)))

    ip = sub.add_parser("integrate", help="manually integrate swarm/<phase> into main")
    ip.add_argument("phase")
    ip.set_defaults(func=lambda cfg, a: cmd_integrate(cfg, a.phase))
    fnp = sub.add_parser("finish", help="ask the supervisor to stop now")
    fnp.add_argument("--force", action="store_true",
                     help="stop even with operator hand-offs still queued")
    fnp.set_defaults(func=lambda cfg, a: cmd_finish(cfg, a.force))
    stp = sub.add_parser("status", help="human-readable state dump")
    stp.add_argument("--json", action="store_true", help="machine-readable output")
    stp.add_argument("--all", action="store_true",
                     help="include the full done map (default: counts per status)")
    stp.set_defaults(func=lambda cfg, a: cmd_status(cfg, as_json=a.json, show_all=a.all))
    pap = sub.add_parser(
        "pause", help="stop launching new workers (in-flight finish), now or later")
    when = pap.add_mutually_exclusive_group()
    when.add_argument("--in", dest="delay", metavar="DURATION",
                      help="pause this long from now: 12h, 90m, 1h30m, 2d")
    when.add_argument("--at", metavar="HH:MM", help="pause at the next HH:MM, local time")
    when.add_argument("--cancel", action="store_true", help="drop a scheduled pause")
    pap.set_defaults(func=lambda cfg, a: cmd_pause(cfg, a.delay, a.at, a.cancel))
    rsm = sub.add_parser("resume", help="resume launching workers into free slots")
    rsm.add_argument("--override-cap", action="store_true",
                     help="also run through a usage cap's hold until its window resets")
    rsm.set_defaults(func=lambda cfg, a: cmd_resume(cfg, a.override_cap))

    bp = sub.add_parser(
        "build", help="run a build command through the concurrency gate",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Run COMMAND once it is its turn: at most [build].max_concurrent heavy builds\n"
            "run at once, swarm-wide, and the rest wait in arrival order. A build that\n"
            "usually finishes within [build].short_s may go ahead of a long one, but no\n"
            "long one is passed more than [build].overtake times.\n\n"
            "Light commands (no compile: git, ls, cargo update/metadata/fmt/tree,\n"
            "docker buildx bake --print, python scripts that start no processes...) run\n"
            "at once without queueing. Unknown commands count as heavy.\n\n"
            "Before queueing, a heavy command is checked: its program, a `cd` target, a\n"
            "-f/--file or --manifest-path, a Cargo.toml / Makefile / bake file must exist.\n\n"
            "While queued it prints (stderr) its place, who holds each slot and for how\n"
            "long, and an estimated start; then 'queued Xs, starting' and 'ran Ys, exit N'.\n"
            "The exit code is the command's (124 on --timeout).\n\n"
            "A build whose whole process tree does nothing for [build].idle_yield_s is set\n"
            "aside: it keeps running, but the next build starts beside it. It is told so\n"
            "on stderr, then and when it ends. --hold keeps the slot regardless.\n\n"
            "With [build].pair = \"distinct-repo\" two builds in one repository never run\n"
            "at once, and an image build ([build].alone), a --hold or a build outside any\n"
            "git checkout runs with no build beside it. --status says why a build waits."),
        epilog=(
            "several steps in one turn:\n"
            "  swarm build -- sh -c 'cargo clippy -- -D warnings && cargo nextest run'\n"
            "  swarm build --script gate.sh      (runs it with bash -e -o pipefail)\n"
            "stop a build that runs too long (the wait does not count):\n"
            "  swarm build --timeout 15m -- cargo nextest run\n"
            "keep the machine to one command that looks idle while it measures:\n"
            "  swarm build --hold -- ./measure.sh\n"
            "see the gate:  swarm build --status [--json]\n"
            "Time in the queue does not count toward --timeout, but it does count toward\n"
            "any timeout of whatever runs `swarm build`: killing a queued call loses its place."),
    )
    bp.add_argument("--status", action="store_true",
                    help="show holders, the queue and recent builds, then exit")
    bp.add_argument("--json", action="store_true", help="with --status: machine-readable")
    bp.add_argument("--timeout", metavar="DURATION",
                    help="stop the build this long after it STARTS (600, 90s, 10m, 1h30m)")
    bp.add_argument("--script", metavar="FILE",
                    help="run FILE with bash -e -o pipefail in one turn")
    bp.add_argument("--hold", action="store_true",
                    help="take a slot and keep it while the command runs, however idle it"
                         " looks (a measurement that sleeps): no build starts beside it")
    bp.add_argument("argv", nargs=argparse.REMAINDER, help="the build command, e.g. cargo nextest run")
    bp.set_defaults(func=lambda cfg, a: cmd_build(cfg, a.argv, status=a.status, as_json=a.json,
                                                  timeout=a.timeout, script=a.script,
                                                  hold=a.hold))

    kpp = sub.add_parser(
        "keep", help="leave one process running after your session ends (list / stop them)",
        description=(
            "Everything a session starts is ended when the session ends. `swarm keep` is "
            "the one exception: `swarm keep --name N --why \"<one plain line>\" -- <command...>` "
            "starts the command detached, records it, and lists it everywhere until "
            "`swarm keep --stop N`. Use it only when something must outlive your session."
        ),
    )
    kpp.add_argument("--name", help="a unique name (letters, digits, . _ -)")
    kpp.add_argument("--why", help=f"one plain line (≤{keep_mod.WHY_MAX} chars) saying what it is for")
    kpp.add_argument("--cwd", help="where to run it (default: here)")
    kpp.add_argument("--list", dest="listing", action="store_true", help="every kept process, alive or dead")
    kpp.add_argument("--json", action="store_true", help="with --list: JSON")
    kpp.add_argument("--stop", metavar="NAME", help="stop a kept process and forget it")
    kpp.add_argument("argv", nargs=argparse.REMAINDER, help="-- the command to keep running")
    kpp.set_defaults(func=lambda cfg, a: cmd_keep(
        cfg, a.name, a.why, a.argv, a.listing, a.stop, a.cwd, a.json))

    lp = sub.add_parser("launch", help="claim a slot and start a worker")
    lp.add_argument("phase")
    lp.set_defaults(func=lambda cfg, a: cmd_launch(cfg, a.phase))

    dp = sub.add_parser("done", help="signal phase completion")
    dp.add_argument("phase")
    dp.add_argument(
        # `needs-owner` still parses but is not advertised: a worker runs `done`
        # *after* its point of no return, so rejecting the retired spelling would
        # cost it the sentinel, the history row and the poke over a word.
        "status", nargs="?", default="ok",
        choices=list(statuses.ACCEPTED), metavar="{ok,operator,fail,blocked,later}",
    )
    dp.add_argument("note", nargs="*", default=[])
    dp.add_argument("--force", action="store_true",
                    help="replace an existing recap / re-send its ping")
    dp.add_argument("--after", default="", metavar="YYYY-MM-DD",
                    help="with `later`: the date before which the phase must not run")
    dp.set_defaults(func=lambda cfg, a: cmd_done(
        cfg, a.phase, a.status, " ".join(a.note), force=a.force, after=a.after))

    rcd = sub.add_parser(
        "record", help="(ask/operator/Overseer) record an outcome on a ledger row")
    rcd.add_argument("phase")
    rcd.add_argument("outcome", choices=list(ledgerw.RECORD_OUTCOMES))
    rcd.add_argument("note", nargs="*", default=[])
    rcd.add_argument("--after", default="", metavar="YYYY-MM-DD",
                     help="with `later`: the date before which the phase must not run")
    rcd.set_defaults(func=lambda cfg, a: cmd_record(
        cfg, a.phase, a.outcome, " ".join(a.note), a.after))

    fup = sub.add_parser("follow-up", help="file a new ledger row for work you found")
    fup.add_argument("phase", help="the phase you are building (the row it is filed from)")
    fup.add_argument("id", help="the new row's id, unique in the ledger")
    fup.add_argument("scope", nargs="*", default=[],
                     help="what the new phase must deliver; goes to its history")
    fup.add_argument("--title", required=True, help="one line: what the row is")
    fup.add_argument("--needs", default="", help="ids it waits on (comma-separated)")
    fup.add_argument("--dir", default="", help="repo dir(s) it works in (comma-separated)")
    fup.add_argument("--tag", default="", help="owner tag(s) it stops at (comma-separated)")
    fup.add_argument("--touches", default="",
                     help="what it edits, each inside --dir (comma-separated; required with lanes on)")
    fup.set_defaults(func=lambda cfg, a: cmd_follow_up(
        cfg, a.phase, a.id, a.title, a.needs, a.dir, a.tag, " ".join(a.scope), a.touches))

    rsp = sub.add_parser("reshape", help="edit an open row's needs: or touches:, through the gate")
    rsp.add_argument("by", help="who reshapes: your phase, or your role (overseer, operator)")
    rsp.add_argument("row", help="the open row to edit")
    rsp.add_argument("why", nargs="+", help="why; goes to the row's history")
    rsp.add_argument("--needs", default=None, help="replace its needs (comma-separated; '' = none)")
    rsp.add_argument("--add-needs", default="", help="needs to add (comma-separated)")
    rsp.add_argument("--drop-needs", default="", help="needs to drop (comma-separated)")
    rsp.add_argument("--touches", default=None, help="replace its touches (comma-separated)")
    rsp.set_defaults(func=lambda cfg, a: cmd_reshape(
        cfg, a.by, a.row, " ".join(a.why), a.needs, a.add_needs, a.drop_needs, a.touches))

    mdp = sub.add_parser("model", help="set the model open rows' workers run on")
    mdp.add_argument("by", help="who sets it: your phase, or your role (owner, overseer)")
    mdp.add_argument("model", nargs="?", default="",
                     help="a model name claude takes (sonnet, opus), or `default` for the"
                          " swarm's own")
    mdp.add_argument("rows", nargs="*", help="the open rows")
    mdp.add_argument("--why", default="", help="why; goes to each row's history")
    mdp.add_argument("--file", default=None,
                     help='a JSON list of {"row", "model", "why"} instead of model and rows')
    mdp.set_defaults(func=lambda cfg, a: cmd_model(cfg, a.by, a.model, a.rows, a.why, a.file))

    esp = sub.add_parser(
        "escalate", help="(worker on a row's own model) hand the phase to the swarm's model")
    esp.add_argument("phase")
    esp.add_argument("why", nargs="+", help="what you found, and why it needs the other model")
    esp.set_defaults(func=lambda cfg, a: cmd_escalate(cfg, a.phase, " ".join(a.why)))

    lsp = sub.add_parser("lesson", help="add a lesson to the project's lessons file")
    lsp.add_argument("phase")
    lsp.add_argument("text", nargs="+", help="the rule, and what taught it")
    lsp.add_argument("--title", default="", help="its heading (default: its first sentence)")
    lsp.set_defaults(func=lambda cfg, a: cmd_lesson(cfg, a.phase, " ".join(a.text), a.title))

    fp = sub.add_parser("free", help="manually free a slot (by id or phase)")
    fp.add_argument("target")
    fp.set_defaults(func=lambda cfg, a: cmd_free(cfg, a.target))

    np_ = sub.add_parser("notify", help="message the owner through the swarm's own telegram sender")
    np_.add_argument("message", nargs="?")
    np_.add_argument(
        "--attention", action="store_true",
        help="an Overseer summary that needs the owner: send it whatever triggered the pass")
    np_.add_argument(
        "--ack", action="store_true",
        help="acknowledge the pings that never reached your phone: the dashboard and"
             " doctor count only drops after this (sends nothing; the ping log is kept)")
    np_.set_defaults(func=_notify_entry)

    kp = sub.add_parser("skip", help="mark a phase done without running it")
    kp.add_argument("phase")
    kp.set_defaults(func=lambda cfg, a: cmd_skip(cfg, a.phase))

    lyp = sub.add_parser(
        "layout", help="show or change how the worker panes are arranged (live)"
    )
    lyp.add_argument(
        "name",
        nargs="?",
        help="e.g. side-by-side, top-bottom, tiled, main-vertical, auto "
        "(omit to print the current layout and every valid name)",
    )
    lyp.set_defaults(func=lambda cfg, a: cmd_layout(cfg, a.name))

    rlp = sub.add_parser("reload", help="apply a .swarm.toml edit to the running swarm")
    rlp.add_argument("--dry-run", action="store_true", help="show the diff, change nothing")
    rlp.set_defaults(func=lambda cfg, a: cmd_reload(cfg, a.dry_run))

    rtp = sub.add_parser("retry", help="put a failed phase back in play")
    rtp.add_argument("phases", nargs="*", help="phase id(s)")
    rtp.add_argument("--all-failed", action="store_true", help="every phase marked fail")
    rtp.add_argument("--cascade", action="store_true",
                     help="also reset phases that depended on it")
    rtp.add_argument("--launch", action="store_true", help="launch immediately")
    rtp.add_argument("--keep-branch", action="store_true",
                     help="do not discard swarm/<phase>")
    rtp.set_defaults(func=lambda cfg, a: cmd_retry(
        cfg, a.phases, a.all_failed, a.cascade, a.launch, a.keep_branch))

    sub.add_parser("tui", help="the always-on dashboard (window 0)").set_defaults(
        func=lambda cfg, a: cmd_tui(cfg))

    wbp = sub.add_parser("web", help="the read-only Kanban board, reached over Tailscale")
    wbp.add_argument("--host", help="address to bind (default [web] host, 0.0.0.0)")
    wbp.add_argument("--port", type=int, help="port (default [web] port, 8765; 0 = any)")
    wbp.add_argument("--pidfile", help=argparse.SUPPRESS)  # written only when `up` starts it
    wbp.set_defaults(func=lambda cfg, a: cmd_web(cfg, a.host, a.port, a.pidfile, a.config))
    tgp = sub.add_parser("telegram-bot",
                         help="answer /usage and /help from the owner's Telegram chat (foreground)")
    tgp.add_argument("--pidfile", help=argparse.SUPPRESS)
    tgp.set_defaults(func=lambda cfg, a: cmd_telegram_bot(cfg, a.pidfile))

    dcp = sub.add_parser("doctor", help="diagnose a stuck or unhealthy swarm")
    dcp.add_argument("--json", action="store_true")
    dcp.set_defaults(func=lambda cfg, a: cmd_doctor(cfg, a.json))

    rsc = sub.add_parser(
        "resources", help="host, build and worker resource use: now, history, capacity")
    rsc.add_argument("--json", action="store_true")
    rsc.add_argument("--hours", type=float, default=24.0,
                     help="the sparkline window (default 24)")
    rsc.add_argument("--days", type=float, default=30.0,
                     help="the build table and capacity window (default 30)")
    rsc.set_defaults(func=lambda cfg, a: cmd_resources(cfg, a.json, a.hours, a.days))

    whp = sub.add_parser("why", help="why is this phase not running?")
    whp.add_argument("phase")
    whp.add_argument("--json", action="store_true")
    whp.add_argument("--tree", action="store_true", help="show the dependency tree")
    whp.set_defaults(func=lambda cfg, a: cmd_why(cfg, a.phase, a.json, a.tree))

    rpp = sub.add_parser("report", help="what every phase did, with its recap")
    rpp.add_argument("--json", action="store_true")
    rpp.add_argument("--decisions", action="store_true",
                     help="only phases carrying a note or recap")
    rpp.add_argument("--phase", help="limit to one phase")
    rpp.set_defaults(
        func=lambda cfg, a: cmd_report(cfg, a.json, a.decisions, a.phase))

    gcp = sub.add_parser(
        "gc", help="reclaim disk (dry run unless --yes)",
        description="Prints a plan and deletes nothing unless --yes is given.")
    gcp.add_argument("--yes", action="store_true", help="actually delete")
    gcp.add_argument("--older-than", type=int, default=None, dest="sweep_days",
                     help="sweep build artifacts unused for N days"
                          " (default [gc].keep_days, 3)")
    gcp.add_argument("--aggressive", action="store_true",
                     help="also incremental/, release/, doc/")
    gcp.add_argument("--transcripts", action="store_true",
                     help="also orphan worker transcripts in ~/.claude/projects")
    gcp.add_argument("--branches", action="store_true",
                     help="also stale merged swarm/* branches")
    gcp.add_argument("--canonical", action="store_true",
                     help="also the project's own repos (off by default)")
    gcp.add_argument("--force", action="store_true",
                     help="proceed even if the build gate cannot be proven idle")
    gcp.add_argument("-v", "--verbose", action="store_true")
    gcp.set_defaults(func=lambda cfg, a: cmd_gc(cfg, gc_mod.GcOptions(
        yes=a.yes,
        sweep_days=cfg.gc_keep_days if a.sweep_days is None else a.sweep_days,
        aggressive=a.aggressive,
        transcripts=a.transcripts, branches=a.branches, canonical=a.canonical,
        force=a.force), a.verbose))

    rcp = sub.add_parser(
        "recap", help="summarise a phase on demand (never runs on a timer)")
    rcp.add_argument("phase")
    rcp.add_argument("--force", action="store_true", help="regenerate an existing recap")
    rcp.add_argument("--completion", action="store_true", help=argparse.SUPPRESS)
    rcp.set_defaults(func=lambda cfg, a: cmd_recap(cfg, a.phase, a.force, a.completion))

    otp = sub.add_parser(
        "operator-triage",
        help="decide when a queued operator hand-off should run (now / later)")
    otp.add_argument("phase")
    otp.set_defaults(func=lambda cfg, a: cmd_operator_triage(cfg, a.phase))

    opp = sub.add_parser(
        "operator", help="hand a phase to an operator session now")
    opp.add_argument("phase")
    opp.set_defaults(func=lambda cfg, a: cmd_operator(cfg, a.phase))

    odp = sub.add_parser(
        "operator-done", help="report this operator job is finished")
    odp.add_argument("phase", help="the operator job id")
    odp.add_argument("outcome", nargs="*", help="one line: what was done or skipped")
    odp.add_argument(
        "--attention", action="store_true",
        help="ping the owner: they must act, something is still owed, or a check failed")
    odp.add_argument(
        "--not-before", default="", metavar="WHEN",
        help="not yet: queue the job again until WHEN (6h, 3d, 2026-09-30, '2026-09-30 08:00')")
    odp.set_defaults(
        func=lambda cfg, a: cmd_operator_done(
            cfg, a.phase, " ".join(a.outcome), a.attention, a.not_before),
        tolerant=True)

    ohp = sub.add_parser(
        "operator-hold", help="this operator job's session declares long work: a longer lease")
    ohp.add_argument("phase", help="the operator job id")
    ohp.add_argument("span", metavar="HOW-LONG", help="90m, 2h, or a local 'YYYY-MM-DD HH:MM'")
    ohp.add_argument("why", nargs="+", help="one line: what the long work is")
    ohp.set_defaults(
        func=lambda cfg, a: cmd_operator_hold(cfg, a.phase, a.span, " ".join(a.why)))

    oad = sub.add_parser(
        "operator-add", help="queue an ad-hoc operator job")
    oad.add_argument("brief", nargs="+", help="the job, as the session will read it")
    oad.add_argument("--phase", help="the phase this job belongs to (default: a fresh op-<ts> id)")
    oad.add_argument(
        "--not-before", default="", metavar="WHEN",
        help="hold the job until WHEN (6h, 3d, 2026-09-30, '2026-09-30 08:00')")
    oad.set_defaults(
        func=lambda cfg, a: cmd_operator_add(cfg, " ".join(a.brief), a.phase, a.not_before))

    ovp = sub.add_parser("overseer", help="recent Overseer passes; --now asks for one")
    ovp.add_argument("--now", action="store_true", help="request a pass straight away")
    ovp.add_argument("--json", action="store_true")
    ovp.add_argument("-n", "--limit", type=int, default=10, help="passes to list (default 10)")
    ovp.set_defaults(func=lambda cfg, a: cmd_overseer(cfg, a.now, a.json, a.limit))

    ovd = sub.add_parser(
        "overseer-done", help="(Overseer) signal the pass is over, with a one-line summary")
    ovd.add_argument("summary", nargs="*", help="what the pass did, in one line")
    ovd.set_defaults(func=lambda cfg, a: cmd_overseer_done(cfg, " ".join(a.summary)))

    bpp = sub.add_parser("big-picture", help="the big-picture doc's last refresh; --now asks for one")
    bpp.add_argument("--now", action="store_true", help="request a pass straight away")
    bpp.add_argument("--json", action="store_true")
    bpp.set_defaults(func=lambda cfg, a: cmd_big_picture(cfg, a.now, a.json))

    bpd = sub.add_parser(
        "big-picture-done", help="(big-picture session) the draft is written; land it")
    bpd.add_argument("summary", nargs="*", help="what changed in the doc, in one line")
    bpd.set_defaults(func=lambda cfg, a: cmd_big_picture_done(cfg, " ".join(a.summary)),
                     tolerant=True)

    tdp = sub.add_parser("todo", help="what waits on you that is not a question")
    tdp.add_argument("--json", action="store_true")
    tdp.set_defaults(func=lambda cfg, a: cmd_todo(cfg, a.json))

    sub.add_parser(
        "guide", help="open the owner guide: a chat that walks you through your to-dos"
    ).set_defaults(func=lambda cfg, a: cmd_guide(cfg))

    cnp = sub.add_parser(
        "console", help="open the owner console (your own Claude session), or move to it")
    cnp.add_argument("--new", action="store_true",
                     help="start a fresh conversation instead of resuming the last one")
    cnp.set_defaults(func=lambda cfg, a: cmd_console(cfg, a.new))
    sub.add_parser("_console-pane").set_defaults(  # what the console window runs
        func=lambda cfg, a: console_mod.run_pane(cfg.project_dir, a.config))

    ckp = sub.add_parser("check", help="preflight config, ledger, telegram, prompts")
    ckp.add_argument("--strict", action="store_true", help="warnings are fatal")
    ckp.set_defaults(func=lambda cfg, a: cmd_check(cfg, a.strict))

    npp = sub.add_parser(
        "note",
        help="record a decision you made (silent — no telegram, no park, no slot cost)",
        description=(
            "Log a judgement call for the owner to review in a batch afterwards. "
            "This is the middle register between finishing silently and stopping "
            "the run to ask: it pings nobody. If you genuinely cannot proceed "
            "correctly without an answer, use `swarm waiting` instead."
        ),
    )
    npp.add_argument("phase")
    npp.add_argument("text", nargs="+", help="what you decided, and how to reverse it")
    npp.add_argument(
        "--kind",
        choices=list(notes_mod.KINDS),
        default="decision",
        help="decision (default), assumption, or risk",
    )
    npp.set_defaults(func=lambda cfg, a: cmd_note(cfg, a.phase, *_note_words(a.text, a.kind)))
    return p


def _load_config(explicit: str | None, project_dir: str | None) -> Config:
    """The config a command runs under: ``--project-dir``, else the project the
    session belongs to (:func:`config.session_project`), else the cwd.

    A launched session runs its commands from its mirror, a component repo
    inside it or an external checkout, and every one of them reads and writes
    the project's run state. Read from the cwd, a component repo answers with
    the defaults (lanes off, so ``swarm widen`` records nothing) and a mirror
    with the ledger branched at launch (so ``swarm follow-up`` accepts an id
    main has taken since).

    The project's file is the owner's live one and may be mid-edit. When it
    does not load, the command says so and falls back to the cwd rather than
    fail a worker's ``swarm done`` over a file the worker does not own.
    """
    project = None if project_dir else session_project()
    if project is not None:
        try:
            return load(explicit=explicit, project_dir=str(project))
        except (ValueError, OSError) as exc:
            print(f"swarm: the config of {project} does not load ({exc});"
                  " reading the current directory's instead", file=sys.stderr)
    return load(explicit=explicit, project_dir=project_dir)


def main(argv: list[str] | None = None) -> int:
    """Parse args, load config, dispatch. Returns the process exit code.

    Every subcommand attaches its handler with ``set_defaults(func=...)``, so
    adding one is a single edit in :func:`_build_parser` instead of a parser
    entry plus a matching branch a hundred lines away — a split that silently
    returned 2 whenever the two drifted apart.
    """
    parser = _build_parser()
    args, extra = parser.parse_known_args(argv)
    # A tolerant subcommand (`operator-done`) records its job whatever flags an
    # older or newer prompt passes: an unknown one is ignored, never fatal.
    if extra and not getattr(args, "tolerant", False):
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        cfg = _load_config(args.config, args.project_dir)
    except (ValueError, OSError) as exc:
        # A broken .swarm.toml must not brick `down`/`status` — the commands you
        # reach for precisely when the config is what you just broke.
        print(f"swarm: config error: {exc}", file=sys.stderr)
        return 2
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
