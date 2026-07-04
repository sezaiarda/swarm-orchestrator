"""``swarm`` command-line entry point.

Thin dispatch over the library. Commands that only signal the supervisor poke
the FIFO (best-effort, never hang); commands that own an action (launch, done,
free, skip, status) act directly under the state lock.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from . import launch as launch_mod
from . import session as session_mod
from . import state as state_mod
from . import supervisor as sup_mod
from . import telegram
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


# -- commands -------------------------------------------------------------
def cmd_up(cfg: Config) -> int:
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
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
    return 0


def cmd_supervise(cfg: Config) -> int:
    sup_mod.main(cfg)
    return 0


def cmd_down(cfg: Config) -> int:
    st = state_mod.read(cfg)
    _poke(cfg, "shutdown")
    if st.supervisor_pid:
        _wait_pid_gone(st.supervisor_pid)
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


def cmd_context(cfg: Config) -> int:
    st = state_mod.read(cfg)
    print(json.dumps(build_context(cfg, st)))
    return 0


def cmd_master_idle(cfg: Config) -> int:
    _poke(cfg, "master-idle")
    return 0


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
    ]
    for s in st.slots:
        mark = f"BUSY {s.phase}" if s.busy else "free"
        lines.append(f"  slot {s.id} pane={s.pane_id} {mark}")
    lines.append(f"done={st.done}")
    print("\n".join(lines))
    return 0


# -- parser ---------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="swarm", description=__doc__)
    p.add_argument("--config", help="path to .swarm.toml (default: ./.swarm.toml)")
    p.add_argument("--project-dir", help="project directory (default: cwd)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("up", help="set up + start the supervisor")
    sub.add_parser("down", help="stop the supervisor + tear down")
    sub.add_parser("_supervise", help=argparse.SUPPRESS)
    sub.add_parser("context", help="print the read-only state snapshot (JSON)")
    sub.add_parser("master-idle", help="signal the master finished a pass")
    sub.add_parser("bootstrap", help="ask the supervisor to spawn the init master")
    sub.add_parser("finish", help="ask the supervisor to stop now")
    sub.add_parser("status", help="human-readable state dump")
    sub.add_parser("pause", help="stop launching new workers (in-flight finish)")
    sub.add_parser("resume", help="resume launching workers into free slots")

    lp = sub.add_parser("launch", help="claim a slot and start a worker")
    lp.add_argument("phase")

    dp = sub.add_parser("done", help="signal phase completion")
    dp.add_argument("phase")
    dp.add_argument("status", nargs="?", default="ok", choices=["ok", "fail"])
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
        return cmd_up(cfg)
    if cmd == "down":
        return cmd_down(cfg)
    if cmd == "_supervise":
        return cmd_supervise(cfg)
    if cmd == "launch":
        return cmd_launch(cfg, args.phase)
    if cmd == "done":
        return cmd_done(cfg, args.phase, args.status, " ".join(args.note))
    if cmd == "context":
        return cmd_context(cfg)
    if cmd == "master-idle":
        return cmd_master_idle(cfg)
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
