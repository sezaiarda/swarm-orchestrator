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

from . import buildsem
from . import gitq
from . import launch as launch_mod
from . import session as session_mod
from . import state as state_mod
from . import supervisor as sup_mod
from . import telegram, tmux
from .config import Config, load
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
    are the record of which phases actually finished. We first rehydrate ``done``
    from them (so a restart doesn't rebuild completed phases), then reconcile —
    a finished phase's (possibly incomplete) integration is completed, while an
    *interrupted* phase (a leftover branch with no ``ok`` sentinel) is discarded
    and left NOT done so the master rebuilds it. A git failure here degrades to a
    warning instead of aborting ``swarm up``.
    """
    log = Log(cfg.supervisor_log)
    try:
        seed = gitq.sentinel_done(cfg)
        st = state_mod.read(cfg)
        try:
            integrated = gitq.reconcile_orphans(cfg, dict(st.done), log)
        except gitq.GitError as exc:
            print(f"reconcile skipped (git error): {exc}", file=sys.stderr)
            log.line(f"RECONCILE-ERROR {exc}")
            integrated = []
        if seed or integrated:
            with state_mod.transaction(cfg) as s:
                for phase, status in seed.items():
                    s.mark_done(phase, status)
                for phase in integrated:
                    s.mark_done(phase, "ok")
        if integrated:
            print(f"reconciled orphan branches: {', '.join(integrated)}")
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
    log = Log(cfg.supervisor_log)
    ok = launch_mod.launch(cfg, phase, log)
    log.close()
    return 0 if ok else 1


def cmd_done(cfg: Config, phase: str, status: str, note: str) -> int:
    launch_mod.done(cfg, phase, status, note)
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


def cmd_finish(cfg: Config) -> int:
    _poke(cfg, "shutdown")
    return 0


def cmd_free(cfg: Config, target: str) -> int:
    with state_mod.transaction(cfg) as st:
        if target.isdigit():
            slot = st.slot_by_id(int(target))
            if slot:
                slot.busy = False
                slot.phase = None
                slot.worktree = None
                slot.branch = None
        else:
            st.free_slot_for(target)
    print(f"freed {target}")
    return 0


def cmd_skip(cfg: Config, phase: str) -> int:
    with state_mod.transaction(cfg) as st:
        st.mark_done(phase, "skip")
    print(f"skipped {phase}")
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


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _unmerged_paths(repo: str) -> list[str]:
    """Paths left unmerged in ``repo``. Empty if git cannot answer — best-effort."""
    try:
        r = subprocess.run(
            ["git", "-C", repo, "diff", "--name-only", "--diff-filter=U"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def _pane_alive(pane: str | None) -> bool:
    return bool(pane) and bool(tmux.list_panes(str(pane)))


def cmd_why(cfg: Config) -> int:
    """Explain why the swarm is — or is not — making progress, and how to clear it.

    Pure observability: reads state, never mutates it, never acts. Every finding
    carries the command that clears it, because the pure-injection design has no
    auto-retry *by decision* — which makes the owner the recovery path, and a
    recovery path you cannot see is not one. ``swarm status`` answers "what is the
    state"; this answers "why is nothing happening", which is the question actually
    asked when a run goes quiet.
    """
    st = state_mod.read(cfg)
    ctx = build_context(cfg, st)
    out: list[str] = [f"swarm why — {cfg.slug}"]
    stalls = 0
    # Set by any non-stall line that already explains the situation, so the
    # "nothing wrong" summary below cannot contradict it (printing both
    # "DECIDING, 1 free slot" and "IDLE, nothing ready" is worse than either).
    noted = False

    def finding(title: str, *body: str) -> None:
        nonlocal stalls
        stalls += 1
        out.append("")
        out.append(title)
        out.extend(f"    {b}" for b in body)

    # -- is anything driving the run at all? --------------------------------
    if st.finished:
        finding(
            "FINISHED  this run is over; nothing further will launch",
            f"{len(st.done)} phase(s) recorded done",
            "fix: swarm up   (starts a fresh run)",
        )
    elif not _pid_alive(st.supervisor_pid):
        finding(
            "STALLED   no supervisor is alive — nothing launches, nothing integrates",
            f"state records supervisor_pid={st.supervisor_pid}, which is not running",
            "fix: swarm up",
        )
    if st.paused:
        finding(
            "PAUSED    launching is disabled; in-flight phases still finish",
            "fix: swarm resume",
        )

    # -- an integration hold stops the ENTIRE queue, not just its own phase --
    if st.integ_blocked:
        phase = st.integ_blocked
        repo = st.integ_blocked_repo or "?"
        kind = st.integ_blocked_kind or "?"
        body = [
            f"repo    : {repo.rstrip('/').split('/')[-1]}   ({kind})",
            f"path    : {repo}",
        ]
        if kind == gitq.CONFLICT:
            files = _unmerged_paths(repo)
            if files:
                shown = ", ".join(files[:8]) + (" …" if len(files) > 8 else "")
                body.append(f"unmerged: {shown}")
            pane = st.windows.get(f"resolve:{phase}")
            if pane is None:
                body.append("resolver: none open — resolve it yourself")
            elif _pane_alive(pane):
                body.append(f"resolver: pane {pane} still open (it may be idle or interrupted)")
            else:
                body.append(f"resolver: pane {pane} is GONE — nothing is resolving this")
        body.append(f"fix     : resolve + commit in that repo, then `swarm resolved {phase}`")
        finding(
            f"BLOCKED   integrating {phase} — this holds the whole integration queue",
            *body,
        )
    elif st.integ_queue:
        out.append("")
        out.append(f"integrating: {', '.join(st.integ_queue)} (in progress)")
        noted = True

    # -- workers that are off-grid waiting on the owner ---------------------
    # Still on the grid with a park timer armed: definitely unanswered.
    for phase in sorted(st.waiting):
        finding(
            f"WAITING   {phase} asked you something and is holding its slot",
            "answer it in its pane; it then finishes normally with `swarm done`",
        )
    # Parked means OFF-GRID, not unanswered. `swarm resumed` cancels the park timer,
    # but a phase that was already parked stays in this list until it reports done —
    # so reporting it as "waiting on YOU" sends the owner to answer a question they
    # may have answered an hour ago, while the worker is busy building.
    for phase in sorted(p for p in st.parked if p not in st.waiting):
        out.append("")
        out.append(f"PARKED    {phase} is off-grid in its own tmux window")
        out.append("          it asked you something earlier. If you have answered, it is")
        out.append("          building there and will report `swarm done` on its own.")
        noted = True

    # -- the accepted pure-injection race: ready but nothing launched -------
    busy = ctx["busy_slots"]
    free = ctx["free_slots"]
    ready = ctx["ready"]
    if free and ready and not st.paused and not st.integ_blocked:
        if ctx["master_alive"]:
            # NOT a stall: a live master is mid-decision. Spawning + claiming takes
            # ~30 s, and calling that a lost nudge sends the owner to `swarm launch`
            # for a phase the master is about to claim — the manual launch is then
            # refused by the atomic slot claim, which is correct but looks broken.
            # A diagnostic that cries wolf during normal operation is worse than none.
            out.append("")
            out.append(
                f"DECIDING  master is choosing what to put in {len(free)} free slot(s)"
                " — a claim normally lands within ~30s"
            )
            noted = True
        else:
            finding(
                f"IDLE      {len(free)} free slot(s) and {len(ready)} ready phase(s), none launched",
                "no master is alive to claim them — a nudge was probably lost",
                "(accepted consequence of pure injection — there is no auto-retry by design)",
                f"ready: {', '.join(ready[:6])}" + (" …" if len(ready) > 6 else ""),
                f"fix: swarm launch {ready[0]}",
            )

    if ctx["ledger_issues"]:
        finding(
            "LEDGER    structural problems — phases may never become ready",
            *[str(i) for i in ctx["ledger_issues"][:6]],
        )

    # -- nothing wrong: say what it is waiting ON, not just "fine" ----------
    if stalls == 0:
        if busy:
            work = ", ".join(f"{p} (slot {sid})" for sid, p in sorted(busy.items()))
            out.append("")
            out.append(f"WORKING   {work}")
            if free and not ready:
                out.append(
                    f"          {len(free)} slot(s) idle because every remaining phase"
                    " depends on one still building — expected in a serial wave train"
                )
        elif not noted:
            # Only when nothing is busy AND nothing above already explained the
            # situation — otherwise this contradicts the line right above it.
            out.append("")
            out.append("IDLE      nothing busy, nothing ready, nothing blocked")
            out.append("          every remaining phase is excluded or already done")

    print("\n".join(out))
    return 0


def cmd_status(cfg: Config) -> int:
    st = state_mod.read(cfg)
    lines: list[str] = []
    # Lead with anything that has stopped the run. `integ_blocked` used to appear
    # mid-way through line 3, where a held queue reads exactly like a healthy one —
    # the state was reported and still not seen. Attention-worthy facts go first.
    if st.integ_blocked:
        lines.append(
            f"!! BLOCKED integrating {st.integ_blocked}"
            f" ({st.integ_blocked_kind} in"
            f" {(st.integ_blocked_repo or '?').rstrip('/').split('/')[-1]})"
            f" — the whole queue is held; `swarm why` for detail"
        )
    if not st.finished and not _pid_alive(st.supervisor_pid):
        lines.append("!! NO SUPERVISOR — nothing launches or integrates; `swarm up`")
    if st.paused:
        lines.append("!! PAUSED — no new workers launch; `swarm resume`")
    lines += [
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
    sub.add_parser("down", help="stop the supervisor + tear down")
    sub.add_parser("_supervise", help=argparse.SUPPRESS)
    sub.add_parser("context", help="print the read-only state snapshot (JSON)")
    sub.add_parser("master-idle", help="signal the master finished a pass")
    sub.add_parser("bootstrap", help="ask the supervisor to spawn the init master")
    pd = sub.add_parser("_poke-done", help=argparse.SUPPRESS)
    pd.add_argument("phase")
    pd.add_argument("status")

    rp = sub.add_parser("resolved", help="signal a merge-conflict resolver finished")
    rp.add_argument("phase")

    wp = sub.add_parser(
        "waiting", help="report this worker is blocked on the owner (may park its slot)"
    )
    wp.add_argument("phase")
    wp.add_argument("note", nargs="*", default=[], help="the question, for the owner ping")

    rsp = sub.add_parser(
        "resumed", help="report the owner answered — cancel a pending park"
    )
    rsp.add_argument("phase")

    ip = sub.add_parser("integrate", help="manually integrate swarm/<phase> into main")
    ip.add_argument("phase")
    sub.add_parser("finish", help="ask the supervisor to stop now")
    sub.add_parser("status", help="human-readable state dump")
    sub.add_parser("why", help="explain why the swarm is/isn't progressing, and how to clear it")
    sub.add_parser("pause", help="stop launching new workers (in-flight finish)")
    sub.add_parser("resume", help="resume launching workers into free slots")

    bp = sub.add_parser("build", help="run a build command through the concurrency gate")
    bp.add_argument("argv", nargs=argparse.REMAINDER, help="the build command, e.g. cargo nextest run")

    lp = sub.add_parser("launch", help="claim a slot and start a worker")
    lp.add_argument("phase")

    dp = sub.add_parser("done", help="signal phase completion")
    dp.add_argument("phase")
    dp.add_argument(
        "status", nargs="?", default="ok", choices=["ok", "needs-owner", "fail"]
    )
    dp.add_argument("note", nargs="*", default=[])

    fp = sub.add_parser("free", help="manually free a slot (by id or phase)")
    fp.add_argument("target")

    kp = sub.add_parser("skip", help="mark a phase done without running it")
    kp.add_argument("phase")

    lyp = sub.add_parser(
        "layout", help="show or change how the worker panes are arranged (live)"
    )
    lyp.add_argument(
        "name",
        nargs="?",
        help="e.g. side-by-side, top-bottom, tiled, main-vertical, auto "
        "(omit to print the current layout and every valid name)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Parse args, load config, dispatch. Returns the process exit code."""
    args = _build_parser().parse_args(argv)
    cfg = load(explicit=args.config, project_dir=args.project_dir)
    cmd = args.command
    if cmd == "up":
        return cmd_up(cfg, attach=not args.no_attach)
    if cmd == "down":
        return cmd_down(cfg)
    if cmd == "_supervise":
        return cmd_supervise(cfg)
    if cmd == "build":
        return cmd_build(cfg, args.argv)
    if cmd == "launch":
        return cmd_launch(cfg, args.phase)
    if cmd == "done":
        return cmd_done(cfg, args.phase, args.status, " ".join(args.note))
    if cmd == "context":
        return cmd_context(cfg)
    if cmd == "master-idle":
        return cmd_master_idle(cfg)
    if cmd == "resolved":
        return cmd_resolved(cfg, args.phase)
    if cmd == "waiting":
        return cmd_waiting(cfg, args.phase, " ".join(args.note))
    if cmd == "resumed":
        return cmd_resumed(cfg, args.phase)
    if cmd == "integrate":
        return cmd_integrate(cfg, args.phase)
    if cmd == "bootstrap":
        return cmd_bootstrap(cfg)
    if cmd == "_poke-done":
        return cmd_poke_done(cfg, args.phase, args.status)
    if cmd == "finish":
        return cmd_finish(cfg)
    if cmd == "free":
        return cmd_free(cfg, args.target)
    if cmd == "skip":
        return cmd_skip(cfg, args.phase)
    if cmd == "layout":
        return cmd_layout(cfg, args.name)
    if cmd == "status":
        return cmd_status(cfg)
    if cmd == "why":
        return cmd_why(cfg)
    if cmd == "pause":
        return cmd_pause(cfg)
    if cmd == "resume":
        return cmd_resume(cfg)
    return 2


if __name__ == "__main__":
    sys.exit(main())
