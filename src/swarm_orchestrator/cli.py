"""``swarm`` command-line entry point.

Thin dispatch over the library. Commands that only signal the supervisor poke
the FIFO (best-effort, never hang); commands that own an action (launch, done,
free, skip, status) act directly under the state lock.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time

from dataclasses import asdict, fields
from pathlib import Path

from . import ask as ask_mod
from . import buildsem
from . import notes as notes_mod
from . import operator as operator_mod
from . import opqueue
from . import overseer as overseer_mod
from . import ovrecord
from . import tui as tui_mod
from . import doctor as doctor_mod
from . import gc as gc_mod
from . import promptlint
from . import procs
from . import pushowed
from . import recap as recap_mod
from . import reload as reload_mod
from . import report as report_mod
from . import why as why_mod
from . import gitq
from . import keep as keep_mod
from . import ledger as ledger_mod
from . import launch as launch_mod
from . import session as session_mod
from . import state as state_mod
from . import statuses
from . import supervisor as sup_mod
from . import telegram, tgbot, tmux
from . import usage as usage_mod
from .web import lifecycle as web_lifecycle
from .config import Config, load
from . import logutil
from .logutil import Log
from .procs import SESSION_ENV
from . import master as master_mod
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
            result = gitq.reconcile(cfg, dict(st.done), log, operator=_mirror_plan(cfg))
        except gitq.GitError as exc:
            print(f"reconcile skipped (git error): {exc}", file=sys.stderr)
            log.line(f"RECONCILE-ERROR {exc}")
            result = gitq.ReconcileResult()
        held_phases = {h.phase for h in result.held}
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
        if result.integrated:
            print(f"reconciled orphan branches: {', '.join(result.integrated)}")
        if result.operator_integrated:
            print(f"landed operator/overseer mirrors: {', '.join(result.operator_integrated)}")
        for phase, pushes in result.pushes.items():
            pushowed.settle(cfg, phase, pushes, log)  # a failed push is owed, not held
        if result.held:
            first = result.held[0]
            plan = operator_mod.mirror_plan(cfg)
            passes = ovrecord.mirror_plan(cfg)
            asks = ask_mod.mirror_plan(cfg)
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
                    elif h.phase in asks:
                        s.integ_push(h.phase, ask_mod.INTEG_STATUS)
                    else:
                        s.integ_push(h.phase, seed.get(h.phase, "ok"))
            names = ", ".join(f"{h.phase} ({h.kind})" for h in result.held)
            print(f"integration HELD: {names}", file=sys.stderr)
            print("  these phases are NOT marked done — their branches never merged.")
            print("  resolve, then `swarm resolved <phase>`; `swarm doctor` for detail.")
            log.line(f"RECONCILE-HELD-BOOT {names}")
            telegram.notify(
                cfg.telegram_notify,
                f"swarm: {cfg.slug} started with integration held — {names}",
                kind="integrate-hold",
                phase=first.phase,
                source="cli._reconcile_orphans",
                state_dir=cfg.state_dir,
            )
    finally:
        log.close()


def _mirror_plan(cfg: Config) -> dict[str, str]:
    """Every ``swarm/*`` branch that has no sentinel by design and must not be
    discarded as an interrupted phase: operator jobs', Overseer passes' and asks'."""
    return {**operator_mod.mirror_plan(cfg), **ovrecord.mirror_plan(cfg),
            **ask_mod.mirror_plan(cfg)}


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


def _report_web_board(cfg: Config) -> None:
    """After ``up`` starts the board (tmux window or detached process), say
    whether it actually came up — port-taken and crash both used to go silent:
    the pane died, ``swarm up`` printed the URLs anyway, and nothing but a
    since-corrected ``status``/``doctor`` connect check ever disagreed."""
    state, detail = web_lifecycle.wait_probe(cfg)
    if state == web_lifecycle.OURS:
        print(f"web board: {' '.join(web_lifecycle.urls(cfg))}")
        return
    if state == web_lifecycle.TAKEN:
        who = f" ({detail})" if detail else ""
        reason = f"port :{cfg.web_port} is held by another program{who}"
    else:
        reason = f"nothing answered on :{cfg.web_port} — check <state>/logs/web.log"
    hint = "set [web].port in .swarm.toml to a free port and restart"
    print(f"web board: FAILED to start — {reason}", file=sys.stderr)
    print(f"  fix: {hint}", file=sys.stderr)
    telegram.notify(
        cfg.telegram_notify,
        f"swarm: {cfg.slug} — the web board did not start ({reason}); {hint}",
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
    state_mod.init_state(cfg)
    _start_run(cfg, "up")
    # Every up, whatever the isolation: it is what re-queues a hand-off whose
    # sentinel outlived its item — and the swarm also runs with isolation "none".
    log = Log(cfg.supervisor_log)
    try:
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
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarm_orchestrator", "_supervise"],
        cwd=str(cfg.project_dir),
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
    if cfg.web_enabled:
        # Under tmux the board already runs in its own window (session.setup);
        # the headless driver has no session, so it gets its own process.
        if cfg.driver != "tmux":
            web_lifecycle.start_detached(cfg)
        _report_web_board(cfg)
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


def cmd_supervise(cfg: Config) -> int:
    sup_mod.main(cfg)
    return 0


def _our_supervisor(cfg: Config, pid: int) -> bool:
    """Whether ``pid`` is this project's supervisor and not a process that has
    since been given the same number. The pid recorded in state outlives the
    supervisor, and ``down`` escalates to SIGKILL."""
    args = procs.cmdline(pid)
    if "_supervise" not in args or not any("swarm" in a for a in args):
        return False
    return procs.cwd(pid) == cfg.project_dir.resolve()


def cmd_down(cfg: Config) -> int:
    st = state_mod.read(cfg)
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
    closed = usage_mod.close_run(cfg, "down")
    print("swarm down" + (f" — {_run_line(closed)}" if closed else ""))
    if ended:
        print(f"  ended {ended} session process(es)")
    if left:
        print(f"swarm down: {len(left)} process(es) survived SIGKILL: "
              f"{' '.join(map(str, left))}", file=sys.stderr)
        return 1
    return 0


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
        return _dump({"current": cur, "runs": past,
                      "now": {w: usage_mod.latest(src.samples, w, now) for w in ("five", "week")},
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


def cmd_done(cfg: Config, phase: str, status: str, note: str, force: bool = False) -> int:
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
    result = launch_mod.done(cfg, phase, status, note, force=force)
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
            plan = gc_mod.apply(plan)
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
        # merged. A `later` item drains from the queue sweep once the run is quiet.
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
    if operator_mod.deferred(item):
        # Asking by hand is the owner saying it cannot keep: a `later` triage
        # must not hold it past its merge.
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
    question: str = "",
) -> int:
    """The session signals its job is finished, with a one-line outcome.

    The outcome is recorded on the item, in the notification ledger and in the
    next Overseer digest. Routine outcomes arrive folded into the Overseer's
    summary, unless ``[operator].notify`` says otherwise.

    One that needs the owner (``--attention``, or ``--ask "<question>"``, which
    implies it) opens an ask (:func:`operator.ask_owner`): the owner answers in
    that window, and its one ping carries the question and where to answer it.
    The outcome's own "needs you" line used to be the whole of it, sent as the
    session ended, so an owner could go to answer one phase in an
    operator pane already running a different phase and found no question anywhere.
    """
    question = " ".join((question or "").split())
    attention = attention or bool(question)
    item = opqueue.complete(cfg, phase, outcome, attention)
    if item is None:
        print(f"swarm operator-done: no live operator job {phase}", file=sys.stderr)
        return 1
    tail = f" — {item.outcome}" if item.outcome else " (no outcome given)"
    asked, failed = None, ""
    if attention:
        try:
            asked = operator_mod.ask_owner(cfg, item, question)
        except ask_mod.AskError as exc:
            failed = str(exc)
    mode = cfg.operator_notify
    if mode == "attention" and telegram.sends_all(cfg):
        mode = "all"  # `[telegram].pings = "all"` restores every ping, this one too
    if asked is not None:
        # One ping, not two: the ask's, sent when its window opens, which says
        # what is asked and where. This line saying "needs you" beside it would
        # point at a session that has already ended.
        hold = f"its question goes out when window {asked.window} opens"
        head = f"swarm: operator job {phase} done, question opened in {asked.window}"
    elif attention:
        hold = _operator_done_hold(mode, attention)
        head = (f"swarm: operator job {phase} needs you, but no question window could"
                f" open ({failed}); its session has ended")
    else:
        hold = _operator_done_hold(mode, attention)
        head = f"swarm: operator job {phase} done"
    telegram.notify(
        cfg.telegram_notify,
        f"{head}{tail}",
        kind="operator-done",
        phase=phase,
        source="cli.operator-done",
        state_dir=cfg.state_dir,
        suppressed=hold,
    )
    # The ask is poked before the job's end: the end respawns this very pane,
    # and a poke that never leaves would leave the question unopened until the
    # next `swarm up`. The supervisor holds it until the job's work has landed.
    ask_heard = _poke(cfg, f"ask-open {asked.name}") if asked is not None else False
    # The item is settled whatever happens next; only the session's own lease
    # (and, under worktree isolation, the merge of its mirror) rides on the
    # poke, and a lost one holds the lease until it expires. Say so.
    heard = _poke(cfg, f"operator-done {phase}")
    print(f"operator-done {phase}")
    if asked is not None:
        print(f"  owner: asked in window {asked.window} (`swarm ask --list`):"
              f" {asked.question}")
        print(f"  question: {'opens once this job has landed, then pings the owner once' if ask_heard else 'opens at the next swarm up'}")
    else:
        print(f"  owner: {'pinged' if hold is None else 'not pinged — ' + hold}")
    print(f"  supervisor: {'poked' if heard else 'not running — lease clears on expiry'}")
    return 0


def _job_brief(note: str, limit: int = 160) -> str:
    """A job's brief cut to what fits beside a question on a phone screen."""
    return note if len(note) <= limit else note[: limit - 1].rstrip() + "…"


def cmd_operator_ask(cfg: Config, phase: str, question: str) -> int:
    """The session hit a genuine decision: ping the owner and wait for them.

    The session stays alive and asks in its own pane (AskUserQuestion), exactly
    as a worker does after ``swarm waiting``; this call is the ping that gets the
    owner to that pane, plus the lease that keeps the session alive until they
    arrive (:data:`opqueue.WAIT_LEASE_S`, on the item and in ``state.json``). It
    used to end the session instead, so every answer arrived to nobody.
    """
    if not question.strip():
        print("swarm operator-ask: empty question", file=sys.stderr)
        return 2
    item, fresh = opqueue.wait_on_owner(cfg, phase, question)
    if item is None:
        print(f"swarm operator-ask: no running operator job {phase}", file=sys.stderr)
        return 1
    operator_mod.hold_lease(cfg, phase, item.lease_until)
    if fresh:
        telegram.notify(
            cfg.telegram_notify,
            launch_mod.cost_line(None, "operator session held", item.asked_at or time.time())
            + f"\nswarm: operator job {phase} is waiting on you"
            f" — {launch_mod.ping_question(item.question)}"
            f" (the job: {_job_brief(item.note or 'no brief')})",
            kind="operator-ask",
            phase=phase,
            source="cli.operator-ask",
            state_dir=cfg.state_dir,
        )
    told = "has been pinged" if fresh else "already has this question"
    print(f"operator-ask {phase}: the owner {told}")
    print("  now ask it with AskUserQuestion in this pane and wait for the answer;")
    print(f'  then run: swarm operator-resumed {phase} "<the answer>"')
    return 0


def cmd_operator_resumed(cfg: Config, phase: str, answer: str = "") -> int:
    """The owner answered: record it and put the session back on a normal lease."""
    item = opqueue.resume(cfg, phase, answer)
    if item is None:
        print(f"swarm operator-resumed: {phase} is not waiting on the owner", file=sys.stderr)
        return 1
    operator_mod.hold_lease(cfg, phase, item.lease_until)
    if notes_mod.owner_answer(cfg, opqueue.owning_phase(phase), item.answer, item.question):
        print("  recorded as an owner decision (`swarm report --decisions`)")
    print(f"operator-resumed {phase}: carry on")
    return 0


def cmd_operator_add(cfg: Config, brief: str, phase: str | None = None) -> int:
    """Queue an ad-hoc operator job — the Overseer's (and the owner's) way in.

    It joins the same queue as a phase's hand-off and is dispatched by the same
    sweep, oldest first; the poke only wakes the supervisor to look.
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
    item = opqueue.add_adhoc(cfg, brief, phase)
    if item is None:
        print("swarm operator-add: could not queue the job", file=sys.stderr)
        return 1
    heard = _poke(cfg, f"operator-queued {item.phase}")
    print(f"queued operator job {item.phase}: {item.note}")
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
    passes = ovrecord.load_passes(cfg, limit=limit, live=st.overseer_pass)
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
    if st.overseer_pass != pid:
        print(f"swarm overseer-done: pass {pid} is no longer live (timed out?) — recorded only",
              file=sys.stderr)
        return 1
    heard = _poke(cfg, f"overseer-done {pid}")
    print(f"overseer-done {pid}")
    print(f"  supervisor: {'poked' if heard else 'not running — the pass ends on the next swarm up'}")
    return 0


def cmd_overseer_ask(cfg: Config, question: str) -> int:
    """The Overseer hit an owner-level call: ping the owner and wait for them.

    Like ``swarm waiting`` for a worker: the session stays alive and asks in its
    own pane (AskUserQuestion); this is the ping that brings the owner there, and
    the deadline stretch that stops the timeout killing a pass that is only
    waiting on a person."""
    question = " ".join(question.split())
    if not question:
        print("swarm overseer-ask: empty question", file=sys.stderr)
        return 2
    pid, st = _live_pass(cfg)
    rec = ovrecord.load_json(cfg, pid) if pid else None
    if rec is None or st.overseer_pass != pid:
        print("swarm overseer-ask: no Overseer pass is running", file=sys.stderr)
        return 1
    fresh = rec.question != question or bool(rec.answer)
    ovrecord.update(cfg, pid, question=question, asked_at=time.time(), answer="")
    with state_mod.transaction(cfg) as s:
        if s.overseer_pass == pid:
            s.overseer_deadline = time.time() + opqueue.WAIT_LEASE_S
    if fresh:
        telegram.notify(
            cfg.telegram_notify,
            launch_mod.cost_line(None, "Overseer pass held", time.time())
            + f"\nswarm: the Overseer is waiting on you — {launch_mod.ping_question(question)}",
            kind="overseer-ask",
            source="cli.overseer-ask",
            state_dir=cfg.state_dir,
        )
    print(f"overseer-ask {pid}: the owner {'has been pinged' if fresh else 'already has this question'}")
    print("  now ask it with AskUserQuestion in this pane and wait for the answer;")
    print('  then run: swarm overseer-resumed "<the answer>"')
    return 0


def cmd_overseer_resumed(cfg: Config, answer: str) -> int:
    """The owner answered: record it and put the pass back on its normal timeout."""
    pid, st = _live_pass(cfg)
    rec = ovrecord.load_json(cfg, pid) if pid else None
    if rec is None or st.overseer_pass != pid or not rec.question:
        print("swarm overseer-resumed: the Overseer is not waiting on the owner", file=sys.stderr)
        return 1
    ovrecord.update(cfg, pid, answer=" ".join(answer.split()) or "(answered)")
    with state_mod.transaction(cfg) as s:
        if s.overseer_pass == pid:
            s.overseer_deadline = time.time() + cfg.overseer_timeout_s
    if notes_mod.owner_answer(cfg, notes_mod.OVERSEER, answer, rec.question):
        print("  recorded as an owner decision (`swarm report --decisions`)")
    print(f"overseer-resumed {pid}: carry on")
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
        graph = ledger_mod.load(cfg.project_dir / cfg.ledger)
        issues = ledger_mod.validate(graph)
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
    for name in ("init_master.md", "resolver.md", "operator.md", "overseer.md", "ask.md"):
        q = shipped / name
        if q.is_file():
            out.append((f"prompts/{name}", q))
    return out


def _snapshot_cfg(cfg: Config) -> Config | None:
    """The config the running supervisor is actually using, or None.

    Written by the supervisor at startup and after every reload. It matters
    because ``load()`` layers SWARM_* env overrides from *that* process's
    environment: a CLI re-reading the file would compute the wrong "before" for
    every overridden field, and once the file has been edited it cannot see the
    old values at all.
    """
    path = cfg.state_dir / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    init_fields = {f.name for f in fields(Config) if f.init}
    kwargs = {}
    for name in init_fields:
        if name not in raw:
            return None
        val = raw[name]
        kwargs[name] = Path(val) if name in ("project_dir",) and isinstance(val, str) else val
    try:
        return Config(**kwargs)
    except (TypeError, ValueError):
        return None


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


def cmd_build(cfg: Config, argv: list[str]) -> int:
    """Run a heavy build command through the swarm-wide concurrency gate.

    ``swarm build cargo nextest run`` etc. On success this ``exec``s the command
    (never returns); the returned code only covers the error paths.
    """
    return buildsem.run(cfg, argv)


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


def cmd_ask(cfg: Config, name: str | None, rows: str | None, why: str | None,
            brief: str, listing: bool = False, reopen: str | None = None,
            as_json: bool = False) -> int:
    """Open a window where the owner answers review questions — or list them.

    The record is written first (``<state>/ask/<name>.json``), then the
    supervisor is poked to open the window; ``swarm up`` opens an unanswered one
    again from that record. It takes no slot and never times out.
    """
    if listing or (not name and not reopen):
        asks = ask_mod.load_all(cfg)
        shown = [a for a in asks if a.is_open] + [a for a in asks if not a.is_open][:ask_mod.RECENT]
        if as_json:
            return _dump([a.to_dict() for a in shown])
        print("\n".join(ask_mod.line(a) for a in shown) if shown else "no asks")
        return 0
    if reopen:
        ask = ask_mod.load(cfg, reopen)
        if ask is None or not ask.is_open:
            print(f"swarm ask: {reopen} is not an open ask", file=sys.stderr)
            return 1
        if ask_mod.session_alive(cfg, ask):
            print(f"swarm ask: {reopen} is open in window {ask.window} (`{ask.attach()}`)")
            return 0
        name = reopen
        reopened = True
    else:
        try:
            ask, reopened = ask_mod.create(cfg, name, ask_mod.parse_rows(rows or ""),
                                           why or "", brief)
        except ask_mod.AskError as exc:
            print(f"swarm ask: {exc}", file=sys.stderr)
            return 2
        graph = ledger_mod.load(cfg.project_dir / cfg.ledger)
        unknown = [r for r in ask.rows if graph and r not in graph]
        if unknown:
            print(f"  warning: not in the ledger: {', '.join(unknown)}", file=sys.stderr)
    heard = _poke(cfg, f"ask-open {name}")
    print(f"ask {name} {'opens again' if reopened else 'recorded'}: {ask_mod.rows_text(ask.rows)}")
    print(f"  why: {ask.why}")
    print(f"  window: {ask.window} in the swarm's tmux session")
    print(f"  supervisor: {'poked — the window opens now and the owner is pinged once' if heard else 'NOT RUNNING — it opens at the next swarm up'}")
    return 0


def cmd_ask_done(cfg: Config, name: str, outcome: str = "", stop_keeps: list[str] | None = None,
                 attention: bool = False) -> int:
    """The ask session is finished: record the outcome, stop the review's kept
    processes, then poke the supervisor to close the window and land the mirror.

    The outcome follows the operator's quiet policy (``[operator].notify``): it
    pings only with ``--attention``, and otherwise reaches the owner in the
    Overseer's summary.
    """
    stop_keeps = [k for k in (stop_keeps or []) if k]
    ask = ask_mod.complete(cfg, name, outcome, attention, stop_keeps)
    if ask is None:
        print(f"swarm ask-done: no open ask {name}", file=sys.stderr)
        return 1
    job = ask_mod.opened_by_operator(ask)
    # An operator job's question: the answer is the owner's decision on the
    # job's phase, in the run's history beside what the job itself reported.
    if job and notes_mod.owner_answer(cfg, opqueue.owning_phase(job), ask.outcome,
                                      ask.question or ask.why):
        print("  recorded as an owner decision (`swarm report --decisions`)")
    for keep in stop_keeps:
        rec = keep_mod.stop(cfg, keep)
        if rec is None:
            print(f"  keep {keep}: nothing kept under that name")
        elif rec.alive:
            print(f"  keep {keep}: pid {rec.pid} survived SIGKILL", file=sys.stderr)
        else:
            print(f"  keep {keep}: stopped (pid {rec.pid})")
    mode = cfg.operator_notify
    if mode == "attention" and telegram.sends_all(cfg):
        mode = "all"
    hold = _operator_done_hold(mode, attention)
    tail = f" — {ask.outcome}" if ask.outcome else " (no outcome given)"
    telegram.notify(
        cfg.telegram_notify,
        f"swarm: ask {name} ({ask_mod.rows_text(ask.rows)})"
        f" {'needs you' if attention else 'answered'}{tail}",
        kind="ask-done",
        phase=name,
        source="cli.ask-done",
        state_dir=cfg.state_dir,
        suppressed=hold,
    )
    print(f"ask-done {name}")
    print(f"  owner: {'pinged' if hold is None else 'not pinged — ' + hold}")
    sys.stdout.flush()
    # Last: the supervisor closes this very window.
    heard = _poke(cfg, f"ask-done {name}")
    print(f"  supervisor: {'poked — the window closes' if heard else 'not running — the next swarm up lands the mirror'}")
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


def cmd_waiting(cfg: Config, phase: str, note: str) -> int:
    """Self-report that this worker is blocked on the owner (pings + may park)."""
    launch_mod.waiting(cfg, phase, note)
    print(f"waiting {phase}")
    return 0


def cmd_resumed(cfg: Config, phase: str, answer: str = "") -> int:
    """Signal the owner answered — cancel a pending park (distinct from `resume`).

    ``answer`` is the owner's answer in one line. It is recorded as an
    ``owner_decision`` note before the poke, so the history keeps it whether or
    not a supervisor is listening; without it the owner's calls were the one kind
    of decision the history never held.
    """
    if answer.strip():
        notes_mod.owner_answer(cfg, phase, answer, doctor_mod.waiting_question(cfg, phase))
    _poke(cfg, f"resumed {phase}")
    print(f"resumed {phase}")
    if answer.strip():
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
    asks = ask_mod.open_asks(cfg)
    if asks and not force:
        # Each waits on the owner in its own window; a stopped supervisor would
        # never close it, merge its picks or open it again.
        print(f"{len(asks)} ask(s) still wait on you:", file=sys.stderr)
        for ask in asks:
            print(f"  {ask_mod.line(ask)}", file=sys.stderr)
        print("answer them in their windows, or `swarm finish --force`", file=sys.stderr)
        return 1
    _poke(cfg, "shutdown")
    return 0


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
                if phase:
                    was_parked = st.clear_phase(phase)
                    wait_win = st.windows.pop(f"wait:{phase}", None)
        else:
            freed = st.free_slot_for(target) is not None or target in st.done
            was_parked = st.clear_phase(target)
            wait_win = st.windows.pop(f"wait:{target}", None)
            freed = freed or was_parked
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


def cmd_notify(cfg: Config, message: str, attention: bool = False) -> int:
    """Send ``message`` to the owner through the swarm's own sender.

    The master prompts say "telegram the owner" and, until this existed, gave the
    master no swarm-side way to do it - so an LLM master reached for whatever it
    had, and run events then arrived from another sender and never reached
    ``notifications.jsonl``. This is the
    one door: the configured ``[telegram] notify`` script, logged like every other
    swarm ping. Best-effort, like all of them: a failed send is exit 1, never an
    exception.

    Sent from an Overseer pass, it is the pass's summary to the owner, and it
    ends with the usage block (:func:`usage.brief_for`): where the 5-hour and
    weekly limits stand and what this run uses per hour.
    """
    kind = "master-note"
    held = None
    if os.environ.get("SWARM_MASTER_KIND") == master_mod.OVERSEER:
        kind = "overseer-digest"
        held = _summary_hold(cfg, attention)
        if not held:
            message = telegram.with_footer(message, usage_mod.brief_for(cfg))
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
        was_parked = st.clear_phase(phase, "skip")
        wait_win = st.windows.pop(f"wait:{phase}", None)
    if was_parked and wait_win and cfg.driver == "tmux":
        tmux.kill_window(wait_win)
    launch_mod._write_sentinel(cfg, phase, "skip", "skipped with `swarm skip`")
    print(f"skipped {phase}")
    if was_parked:
        print("  (it was parked waiting on you — its window is closed)")
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


def cmd_pause(cfg: Config) -> int:
    with state_mod.transaction(cfg) as st:
        st.paused = True
    print("swarm paused — no new workers launch; in-flight workers finish")
    _warn_if_no_supervisor(cfg, "pause")
    return 0


def cmd_resume(cfg: Config) -> int:
    with state_mod.transaction(cfg) as st:
        st.paused = False
    _poke(cfg, "resume")
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
    current = next((i for i in items if i.phase == st.operator_phase), None)
    if current is not None:
        what = (
            f"WAITING ON YOU: {current.question}"
            if current.state == opqueue.WAITING
            else current.state
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
    """The done map as counts per status, naming the failures (they need you)."""
    parts = " ".join(f"{k}={v}" for k, v in _done_counts(done).items())
    failed = sorted(p for p, s in done.items() if s == statuses.FAIL)
    tail = f" failed: {' '.join(failed)}" if failed else ""
    return f"done: {len(done)} ({parts or 'none'}){tail} — `--all` lists every phase"


def cmd_status(cfg: Config, as_json: bool = False, show_all: bool = False) -> int:
    """The state, for a person or (``--json``) a script. The done map grows with
    the ledger — thousands of entries on a long-lived project — so it is counted
    unless ``--all`` asks for every phase."""
    st = state_mod.read(cfg)
    if as_json:
        data = asdict(st)
        if not show_all:
            data.pop("done")
            data["done_counts"] = _done_counts(st.done)
            data["failed"] = sorted(p for p, s in st.done.items() if s == statuses.FAIL)
        data["config"] = {
            "slug": cfg.slug, "driver": cfg.driver, "isolation": cfg.git_isolation,
            "main_branch": cfg.git_main_branch, "layout": st.layout or cfg.tmux_layout,
            "state_dir": str(cfg.state_dir),
        }
        data["operator_jobs"] = [i.to_dict() for i in opqueue.load_all(cfg)]
        data["web"] = web_lifecycle.status_line(cfg)
        data["telegram_bot"] = tgbot.status_line(cfg)
        data["kept"] = [r.to_json() for r in keep_mod.load_all(cfg)]
        data["asks"] = [a.to_dict() for a in ask_mod.open_asks(cfg)]
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    lines = [
        f"slug={cfg.slug} driver={cfg.driver} finished={st.finished} paused={st.paused}"
        f" layout={st.layout or cfg.tmux_layout}",
        f"master_alive={st.master_alive} supervisor_pid={st.supervisor_pid}",
        f"isolation={cfg.git_isolation} main={cfg.git_main_branch}"
        f" integ_blocked={st.integ_blocked} integ_queue={st.integ_queue}",
    ]
    for s in st.slots:
        mark = f"BUSY {s.phase}" if s.busy else "free"
        wt = f" branch={s.branch}" if s.branch else ""
        lines.append(f"  slot {s.id} pane={s.pane_id} {mark}{wt}")
    if st.waiting or st.parked:
        lines.append(f"waiting={sorted(st.waiting)} parked={st.parked}")
    # A live operator session holds the owner's own authority on the host. It has
    # no slot and no pane probe can find it, so these lines are the only place the
    # text UI can say one is running at all.
    lines.extend(_operator_lines(cfg, st))
    # Asks wait on the owner in their own windows, outside the slot grid.
    lines.extend(f"ask: {ask_mod.line(a)}" for a in ask_mod.open_asks(cfg))
    for line in pushowed.describe(st.push_owed):
        lines.append(f"push owed: {line}")
    lines.append(f"done={st.done}" if show_all else _done_summary(st.done))
    lines.append(web_lifecycle.status_line(cfg))
    lines.append(tgbot.status_line(cfg))
    # What `swarm keep` left running on purpose: nothing else outlives its session.
    lines.extend(f"kept: {keep_mod.line(r)}" for r in keep_mod.load_all(cfg))
    print("\n".join(lines))
    return 0


# -- parser ---------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarm", description=__doc__)
    p.add_argument("--config", help="path to .swarm.toml (default: ./.swarm.toml)")
    p.add_argument("--project-dir", help="project directory (default: cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="set up + start the supervisor, then attach")
    up.add_argument(
        "--no-attach",
        action="store_true",
        help="don't attach the terminal to the swarm tmux session after bringing it up",
    )
    up.set_defaults(func=lambda cfg, a: cmd_up(cfg, attach=not a.no_attach))
    sub.add_parser("down", help="stop the supervisor + tear down").set_defaults(
        func=lambda cfg, a: cmd_down(cfg))
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
    sub.add_parser("_supervise").set_defaults(func=lambda cfg, a: cmd_supervise(cfg))
    sub.add_parser("context", help="print the read-only state snapshot (JSON)").set_defaults(
        func=lambda cfg, a: cmd_context(cfg))
    sub.add_parser("master-idle", help="signal the master finished a pass").set_defaults(
        func=lambda cfg, a: cmd_master_idle(cfg))
    sub.add_parser("bootstrap", help="ask the supervisor to spawn the init master").set_defaults(
        func=lambda cfg, a: cmd_bootstrap(cfg))
    pd = sub.add_parser("_poke-done")
    pd.set_defaults(func=lambda cfg, a: cmd_poke_done(cfg, a.phase, a.status))
    pd.add_argument("phase")
    pd.add_argument("status")

    rp = sub.add_parser("resolved", help="signal a merge-conflict resolver finished")
    rp.add_argument("phase")
    rp.set_defaults(func=lambda cfg, a: cmd_resolved(cfg, a.phase))

    wp = sub.add_parser(
        "waiting", help="report this worker is blocked on the owner (may park its slot)"
    )
    wp.add_argument("phase")
    wp.add_argument("note", nargs="*", default=[], help="the question, for the owner ping")
    wp.set_defaults(func=lambda cfg, a: cmd_waiting(cfg, a.phase, " ".join(a.note)))

    rsp = sub.add_parser(
        "resumed", help="report the owner answered — cancel a pending park"
    )
    rsp.add_argument("phase")
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
    sub.add_parser("pause", help="stop launching new workers (in-flight finish)").set_defaults(
        func=lambda cfg, a: cmd_pause(cfg))
    sub.add_parser("resume", help="resume launching workers into free slots").set_defaults(
        func=lambda cfg, a: cmd_resume(cfg))

    bp = sub.add_parser("build", help="run a build command through the concurrency gate")
    bp.add_argument("argv", nargs=argparse.REMAINDER, help="the build command, e.g. cargo nextest run")
    bp.set_defaults(func=lambda cfg, a: cmd_build(cfg, a.argv))

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

    akp = sub.add_parser(
        "ask", help="open a window where the owner answers review questions (--list shows them)",
        description=(
            "`swarm ask --name N --rows R1,R2 --why \"<one line>\" \"<brief>\"` opens an ask "
            "session in tmux window ask:N: it shows the owner what to look at, asks with "
            "AskUserQuestion, records the picks in the rows and ends with `swarm ask-done N`. "
            "It takes no worker slot and never times out."
        ),
    )
    akp.add_argument("--name", help="a unique name (letters, digits, _ -)")
    akp.add_argument("--rows", help="the ledger row(s) the answer settles, comma-separated")
    akp.add_argument("--why", help=f"one plain line (≤{ask_mod.WHY_MAX} chars): what the owner decides")
    akp.add_argument("--list", dest="listing", action="store_true", help="open and recent asks")
    akp.add_argument("--json", action="store_true", help="with --list: JSON")
    akp.add_argument("--reopen", metavar="NAME", help="open an open ask's window again (same brief)")
    akp.add_argument("brief", nargs="*", help="what the owner looks at and where (URLs, files, a kept server)")
    akp.set_defaults(func=lambda cfg, a: cmd_ask(
        cfg, a.name, a.rows, a.why, " ".join(a.brief), a.listing, a.reopen, a.json))

    adp = sub.add_parser("ask-done", help="(ask session) the owner's answers are recorded")
    adp.add_argument("name")
    adp.add_argument("outcome", nargs="*", help="one line: what the owner picked")
    adp.add_argument("--stop-keep", action="append", default=[], metavar="KEEP",
                     help="also stop this kept process (it only existed for the review); repeatable")
    adp.add_argument("--attention", action="store_true",
                     help="ping the owner: they still have something to do")
    adp.set_defaults(func=lambda cfg, a: cmd_ask_done(
        cfg, a.name, " ".join(a.outcome), a.stop_keep, a.attention), tolerant=True)

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
        choices=list(statuses.ACCEPTED), metavar="{ok,operator,fail}",
    )
    dp.add_argument("note", nargs="*", default=[])
    dp.add_argument("--force", action="store_true",
                    help="replace an existing recap / re-send its ping")
    dp.set_defaults(func=lambda cfg, a: cmd_done(
        cfg, a.phase, a.status, " ".join(a.note), force=a.force))

    fp = sub.add_parser("free", help="manually free a slot (by id or phase)")
    fp.add_argument("target")
    fp.set_defaults(func=lambda cfg, a: cmd_free(cfg, a.target))

    np_ = sub.add_parser("notify", help="message the owner through the swarm's own telegram sender")
    np_.add_argument("message")
    np_.add_argument(
        "--attention", action="store_true",
        help="an Overseer summary that needs the owner: send it whatever triggered the pass")
    np_.set_defaults(func=lambda cfg, a: cmd_notify(cfg, a.message, a.attention))

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

    wbp = sub.add_parser("web", help="the read-only Kanban board, served on the LAN")
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
        help="the owner must act, something is still owed, or a check failed:"
             " opens an ask window for them")
    odp.add_argument(
        "--ask", dest="question", default="", metavar="QUESTION",
        help="a decision only the owner can make: opens an ask window that puts it to them")
    odp.set_defaults(
        func=lambda cfg, a: cmd_operator_done(
            cfg, a.phase, " ".join(a.outcome), a.attention, a.question),
        tolerant=True)

    oap = sub.add_parser(
        "operator-ask",
        help="ping the owner with a genuine decision; the session waits for the answer")
    oap.add_argument("phase", help="the operator job id")
    oap.add_argument("question", nargs="+", help="what you need to know")
    oap.set_defaults(
        func=lambda cfg, a: cmd_operator_ask(cfg, a.phase, " ".join(a.question)))

    orp = sub.add_parser(
        "operator-resumed",
        help="the owner answered an operator question; the session carries on")
    orp.add_argument("phase", help="the operator job id")
    orp.add_argument("answer", nargs="*", help="the owner's answer, as given")
    orp.set_defaults(
        func=lambda cfg, a: cmd_operator_resumed(cfg, a.phase, " ".join(a.answer)))

    oad = sub.add_parser(
        "operator-add", help="queue an ad-hoc operator job")
    oad.add_argument("brief", nargs="+", help="the job, as the session will read it")
    oad.add_argument("--phase", help="the phase this job belongs to (default: a fresh op-<ts> id)")
    oad.set_defaults(
        func=lambda cfg, a: cmd_operator_add(cfg, " ".join(a.brief), a.phase))

    ovp = sub.add_parser("overseer", help="recent Overseer passes; --now asks for one")
    ovp.add_argument("--now", action="store_true", help="request a pass straight away")
    ovp.add_argument("--json", action="store_true")
    ovp.add_argument("-n", "--limit", type=int, default=10, help="passes to list (default 10)")
    ovp.set_defaults(func=lambda cfg, a: cmd_overseer(cfg, a.now, a.json, a.limit))

    ovd = sub.add_parser(
        "overseer-done", help="(Overseer) signal the pass is over, with a one-line summary")
    ovd.add_argument("summary", nargs="*", help="what the pass did, in one line")
    ovd.set_defaults(func=lambda cfg, a: cmd_overseer_done(cfg, " ".join(a.summary)))

    ova = sub.add_parser(
        "overseer-ask", help="(Overseer) an owner-level call: ping the owner and wait")
    ova.add_argument("question", nargs="+")
    ova.set_defaults(func=lambda cfg, a: cmd_overseer_ask(cfg, " ".join(a.question)))

    ovr = sub.add_parser(
        "overseer-resumed", help="(Overseer) the owner answered; back to the normal timeout")
    ovr.add_argument("answer", nargs="*")
    ovr.set_defaults(func=lambda cfg, a: cmd_overseer_resumed(cfg, " ".join(a.answer)))

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
        cfg = load(explicit=args.config, project_dir=args.project_dir)
    except (ValueError, OSError) as exc:
        # A broken .swarm.toml must not brick `down`/`status` — the commands you
        # reach for precisely when the config is what you just broke.
        print(f"swarm: config error: {exc}", file=sys.stderr)
        return 2
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
