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

from . import buildsem
from . import notes as notes_mod
from . import opqueue
from . import tui as tui_mod
from . import doctor as doctor_mod
from . import gc as gc_mod
from . import promptlint
from . import recap as recap_mod
from . import reload as reload_mod
from . import report as report_mod
from . import why as why_mod
from . import gitq
from . import ledger as ledger_mod
from . import launch as launch_mod
from . import session as session_mod
from . import state as state_mod
from . import statuses
from . import supervisor as sup_mod
from . import telegram, tmux
from .config import Config, load
from . import logutil
from .logutil import Log
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
        rebuilt = opqueue.reconcile(cfg, log)
        if rebuilt:
            print(f"operator queue: {', '.join(rebuilt)}")
        seed = gitq.sentinel_done(cfg)
        st = state_mod.read(cfg)
        try:
            result = gitq.reconcile(cfg, dict(st.done), log)
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
        if result.held:
            first = result.held[0]
            with state_mod.transaction(cfg) as s:
                s.integ_blocked = first.phase
                s.integ_blocked_kind = first.kind
                s.integ_blocked_repo = str(first.repo) if first.repo else None
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
    state_mod.init_state(cfg)
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
    if attach:
        _attach(cfg)  # interactive: hand the terminal to the swarm window
    return 0


def cmd_supervise(cfg: Config) -> int:
    sup_mod.main(cfg)
    return 0


def cmd_down(cfg: Config) -> int:
    st = state_mod.read(cfg)
    _poke(cfg, "shutdown")
    pid = st.supervisor_pid
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
    if cfg.driver == "tmux":
        session_mod.teardown(cfg)
    print("swarm down")
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
    """
    result = launch_mod.done(cfg, phase, status, note, force=force)
    print(result.render())
    # Exit non-zero only when a ping the owner was owed did not go out. A missing
    # supervisor is NOT a failure: the sentinel is durable and `swarm up`
    # reconciles from it, which is precisely what the last line of render() says.
    return 1 if result.ping == "failed" else 0


def _dump(obj) -> int:
    print(json.dumps(obj, indent=2, default=str))
    return 0


def cmd_doctor(cfg: Config, as_json: bool) -> int:
    """Answer "what is wrong with my swarm right now?".

    The supervisor is deliberately watchdog-free, and the failure modes that
    matter most produce no log line at all -- a held integration, a dead
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
        plan = gc_mod.apply(plan)
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
        # spawner of the master. A `later` item drains from the queue sweep.
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
    heard = _poke(cfg, f"operator {phase}")
    print(f"operator {phase}: {item.note or '(no brief)'}")
    print(f"  supervisor: {'poked' if heard else 'NOT RUNNING — nothing will open'}")
    return 0


def cmd_operator_done(cfg: Config, phase: str) -> int:
    """The session signals its hand-off is carried out."""
    if opqueue.complete(cfg, phase) is None:
        print(f"swarm operator-done: no live hand-off for {phase}", file=sys.stderr)
        return 1
    # The item is settled whatever happens next; only the session's own lease
    # rides on the poke, and a lost one holds it until it expires. Say so.
    heard = _poke(cfg, f"operator-done {phase}")
    print(f"operator-done {phase}")
    print(f"  supervisor: {'poked' if heard else 'not running — lease clears on expiry'}")
    return 0


def cmd_operator_ask(cfg: Config, phase: str, question: str) -> int:
    """Escalate an ambiguous brief to the owner instead of guessing at it.

    The one telegram in the whole operator flow. The session runs after its
    worker is gone and cannot ask what the recap meant, so a guess here is a
    guess made with the owner's authority on the host — this is the cheaper
    branch by a wide margin.
    """
    if not question.strip():
        print("swarm operator-ask: empty question", file=sys.stderr)
        return 2
    if opqueue.escalate(cfg, phase, question) is None:
        print(f"swarm operator-ask: no live hand-off for {phase}", file=sys.stderr)
        return 1
    _poke(cfg, f"operator-done {phase}")  # the session is over either way
    print(f"operator-ask {phase}: the owner has the question; stop here")
    return 0


def cmd_check(cfg: Config, strict: bool) -> int:
    """Preflight: config, ledger, telegram, prompts -- without a live supervisor."""
    bad = False
    ok, detail = telegram.check(cfg.telegram_notify)
    print(f"telegram: {'ok' if ok else 'FAIL'} — {detail}")
    bad = bad or not ok
    try:
        graph = ledger_mod.load(Path(cfg.ledger))
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
            bad = bad or any(f.severity == "contradicted" for f in findings)
    if not bad:
        print("all checks passed")
    return 1 if (bad and strict) else (1 if bad else 0)


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
    for name in ("init_master.md", "step_master.md", "resolver.md", "operator.md"):
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
                graph = ledger_mod.load(Path(cfg.ledger))
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
        print("run `swarm launch <phase>`, or `swarm nudge` once a supervisor is up")
    return 0


def cmd_tui(cfg: Config) -> int:
    """Run the always-on dashboard (tmux window 0).

    The import at module scope is deliberately cheap -- ``tui/__init__`` pulls in
    nothing but ``sys`` and defers Textual to ``main()`` -- because this module is
    also what a worker loads for the ``swarm done`` inside its bash tool call.
    """
    return tui_mod.main(cfg)


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


def cmd_resumed(cfg: Config, phase: str) -> int:
    """Signal the owner answered — cancel a pending park (distinct from `resume`)."""
    _poke(cfg, f"resumed {phase}")
    print(f"resumed {phase}")
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


def cmd_skip(cfg: Config, phase: str) -> int:
    """Mark a phase done without running it — and let go of everything it held.

    This used to call ``mark_done`` alone, which is only correct for a phase that
    was never started. Skipping a *parked* or *waiting* phase left it in those
    maps, and ``pending()`` is ``any_busy() or parked or waiting`` — so the run
    could never finish, with no CLI able to clear it. Skipping a *busy* phase left
    its slot claimed forever, permanently losing capacity.
    """
    with state_mod.transaction(cfg) as st:
        was_parked = st.clear_phase(phase, "skip")
        wait_win = st.windows.pop(f"wait:{phase}", None)
    if was_parked and wait_win and cfg.driver == "tmux":
        tmux.kill_window(wait_win)
    print(f"skipped {phase}")
    if was_parked:
        print("  (it was parked waiting on you — its window is closed)")
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


def cmd_status(cfg: Config, as_json: bool = False, show_all: bool = False) -> int:
    st = state_mod.read(cfg)
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
    # no slot and no pane probe can find it, so this line is the only place the
    # text UI can say one is running at all.
    owed = opqueue.pending(cfg)
    if st.operator_phase or owed:
        lines.append(
            f"operator={st.operator_phase} queued="
            f"{[f'{i.phase}:{i.state}' for i in owed]}"
        )
    lines.append(f"done={st.done}")
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
    rsp.set_defaults(func=lambda cfg, a: cmd_resumed(cfg, a.phase))

    ip = sub.add_parser("integrate", help="manually integrate swarm/<phase> into main")
    ip.add_argument("phase")
    ip.set_defaults(func=lambda cfg, a: cmd_integrate(cfg, a.phase))
    fnp = sub.add_parser("finish", help="ask the supervisor to stop now")
    fnp.add_argument("--force", action="store_true",
                     help="stop even with operator hand-offs still queued")
    fnp.set_defaults(func=lambda cfg, a: cmd_finish(cfg, a.force))
    stp = sub.add_parser("status", help="human-readable state dump")
    stp.add_argument("--json", action="store_true", help="machine-readable output")
    stp.add_argument("--all", action="store_true", help="include the full done map")
    stp.set_defaults(func=lambda cfg, a: cmd_status(cfg, as_json=a.json, show_all=a.all))
    sub.add_parser("pause", help="stop launching new workers (in-flight finish)").set_defaults(
        func=lambda cfg, a: cmd_pause(cfg))
    sub.add_parser("resume", help="resume launching workers into free slots").set_defaults(
        func=lambda cfg, a: cmd_resume(cfg))

    bp = sub.add_parser("build", help="run a build command through the concurrency gate")
    bp.add_argument("argv", nargs=argparse.REMAINDER, help="the build command, e.g. cargo nextest run")
    bp.set_defaults(func=lambda cfg, a: cmd_build(cfg, a.argv))

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
    gcp.add_argument("--older-than", type=int, default=1, dest="sweep_days",
                     help="only sweep build artifacts older than N days (default 1)")
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
        yes=a.yes, sweep_days=a.sweep_days, aggressive=a.aggressive,
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
        "operator-done", help="report this operator hand-off is carried out")
    odp.add_argument("phase")
    odp.set_defaults(func=lambda cfg, a: cmd_operator_done(cfg, a.phase))

    oap = sub.add_parser(
        "operator-ask",
        help="ask the owner instead of guessing at an ambiguous hand-off")
    oap.add_argument("phase")
    oap.add_argument("question", nargs="+", help="what you need to know")
    oap.set_defaults(
        func=lambda cfg, a: cmd_operator_ask(cfg, a.phase, " ".join(a.question)))

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
    npp.set_defaults(
        func=lambda cfg, a: cmd_note(cfg, a.phase, " ".join(a.text), a.kind)
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Parse args, load config, dispatch. Returns the process exit code.

    Every subcommand attaches its handler with ``set_defaults(func=...)``, so
    adding one is a single edit in :func:`_build_parser` instead of a parser
    entry plus a matching branch a hundred lines away — a split that silently
    returned 2 whenever the two drifted apart.
    """
    args = _build_parser().parse_args(argv)
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
