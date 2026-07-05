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


def cmd_pause(cfg: Config) -> int:
    with state_mod.transaction(cfg) as st:
        st.paused = True
    print("swarm paused — no new workers launch; in-flight workers finish")
    return 0


def cmd_resume(cfg: Config) -> int:
    with state_mod.transaction(cfg) as st:
        st.paused = False
    _poke(cfg, "resume")
    print("swarm resumed — launching will fill free slots")
    return 0


def cmd_status(cfg: Config) -> int:
    st = state_mod.read(cfg)
    lines = [
        f"slug={cfg.slug} driver={cfg.driver} finished={st.finished} paused={st.paused}",
        f"master_alive={st.master_alive} supervisor_pid={st.supervisor_pid}",
        f"isolation={cfg.git_isolation} main={cfg.git_main_branch}"
        f" integ_blocked={st.integ_blocked} integ_queue={st.integ_queue}",
    ]
    for s in st.slots:
        mark = f"BUSY {s.phase}" if s.busy else "free"
        wt = f" branch={s.branch}" if s.branch else ""
        lines.append(f"  slot {s.id} pane={s.pane_id} {mark}{wt}")
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

    rp = sub.add_parser("resolved", help="signal a merge-conflict resolver finished")
    rp.add_argument("phase")

    ip = sub.add_parser("integrate", help="manually integrate swarm/<phase> into main")
    ip.add_argument("phase")
    sub.add_parser("finish", help="ask the supervisor to stop now")
    sub.add_parser("status", help="human-readable state dump")
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
    if cmd == "integrate":
        return cmd_integrate(cfg, args.phase)
    if cmd == "bootstrap":
        return cmd_bootstrap(cfg)
    if cmd == "finish":
        return cmd_finish(cfg)
    if cmd == "free":
        return cmd_free(cfg, args.target)
    if cmd == "skip":
        return cmd_skip(cfg, args.phase)
    if cmd == "status":
        return cmd_status(cfg)
    if cmd == "pause":
        return cmd_pause(cfg)
    if cmd == "resume":
        return cmd_resume(cfg)
    return 2


if __name__ == "__main__":
    sys.exit(main())
