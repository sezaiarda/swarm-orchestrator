"""The supervisor: sole FIFO reader, sole master-killer, sole finisher.

It implements EXACTLY the four-rule pure-injection lifecycle and nothing more —
no redo/coalescing, no need re-derivation, no sentinel reconcile, no safety
timer, no pane-died watchdog, no auto-retry:

1. ``done``       -> free the slot; spawn a master if none is alive, else inject.
2. ``master-idle``-> kill the master pane.
3. after a kill   -> finish when no slot is busy (implies no ready phase remains).
4. a parked worker keeps its slot busy, so finish cannot fire while it is parked.

Being the single FIFO reader gives total event ordering, so the only races are
in keystroke delivery (accepted by the owner; never papered over here).
"""

from __future__ import annotations

import os
import select
import signal

from . import master as master_mod
from . import state as state_mod
from . import telegram, tmux
from .config import Config
from .logutil import Log


class Supervisor:
    """Long-running owner of the FIFO, ``state.json`` and the master lifecycle."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = Log(cfg.supervisor_log, echo=bool(os.environ.get("SWARM_LOG_ECHO")))
        self.master = master_mod.Master(cfg, self.log)
        self._stop = False
        self._fifo_fd = -1

    # -- setup / teardown -------------------------------------------------
    def _open_fifo(self) -> None:
        self.cfg.ensure_dirs()
        if not self.cfg.fifo_path.exists():
            os.mkfifo(self.cfg.fifo_path)
        # O_RDWR so the reader never sees EOF even with no writer attached.
        self._fifo_fd = os.open(self.cfg.fifo_path, os.O_RDWR | os.O_NONBLOCK)

    def _install_signals(self) -> None:
        def handler(signum, _frame):
            # Async-signal-safe wake: poke our own FIFO fd, break select().
            try:
                os.write(self._fifo_fd, b"shutdown\n")
            except OSError:
                self._stop = True

        try:
            signal.signal(signal.SIGTERM, handler)
            signal.signal(signal.SIGINT, handler)
        except ValueError:
            # Not on the main thread (in-process test harness); rely on the
            # explicit `shutdown` FIFO poke to stop the loop instead.
            pass

    # -- main loop --------------------------------------------------------
    def run(self) -> None:
        """Open the FIFO and process events until stopped."""
        self._open_fifo()
        self._install_signals()
        with state_mod.transaction(self.cfg) as st:
            st.supervisor_pid = os.getpid()
        self.log.line(f"SUPERVISOR-START pid={os.getpid()} driver={self.cfg.driver}")
        buf = b""
        while not self._stop:
            try:
                ready, _, _ = select.select([self._fifo_fd], [], [])
            except InterruptedError:
                continue
            if self._fifo_fd not in ready:
                continue
            try:
                chunk = os.read(self._fifo_fd, 65536)
            except BlockingIOError:
                continue  # spurious select wake; nothing to read yet
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._handle(raw.decode("utf-8", "replace").strip())
                if self._stop:
                    break
            if not self._stop:
                self._reconcile()
        if self.master.is_alive():
            self.master.kill()  # never orphan a master on the way out
            with state_mod.transaction(self.cfg) as st:
                st.master_alive = False
        os.close(self._fifo_fd)
        self.log.line("SUPERVISOR-STOP")
        self.log.close()

    # -- event dispatch ---------------------------------------------------
    def _handle(self, line: str) -> None:
        if not line:
            return
        parts = line.split()
        verb = parts[0]
        if verb == "done":
            phase = parts[1] if len(parts) > 1 else "?"
            status = parts[2] if len(parts) > 2 else "ok"
            self._on_done(phase, status)
        elif verb == "master-idle":
            self._on_master_idle()
        elif verb == "bootstrap":
            self._on_bootstrap()
        elif verb == "resume":
            self._on_resume()
        elif verb == "reconcile":
            self._reconcile()
        elif verb == "shutdown":
            self._stop = True
        else:
            self.log.line(f"UNKNOWN {line}")

    # -- rule 0 (bootstrap): first master, once ---------------------------
    def _on_bootstrap(self) -> None:
        if self.master.is_alive():
            self.log.line("BOOTSTRAP-IGNORED master-alive")
            return
        self._spawn_master("init")

    # -- resume: fill free slots after a pause ----------------------------
    def _on_resume(self) -> None:
        self.log.line("EVENT resume")
        if not self.master.is_alive():
            self._spawn_master("step")

    # -- rule 1: done -----------------------------------------------------
    def _on_done(self, phase: str, status: str) -> None:
        with state_mod.transaction(self.cfg) as st:
            st.mark_done(phase, status)
            freed = st.free_slot_for(phase)
            paused = st.paused
        freed_id = freed.id if freed else None
        self.log.line(f"EVENT done {phase} {status} freed_slot={freed_id}")
        if paused:
            # Paused: the slot is freed but we launch nothing and hold — no
            # master spawn/inject, so in-flight workers drain without advancing.
            self.log.line("DONE-PAUSED holding — no launch")
            return
        if not self.master.is_alive():
            self._spawn_master("step")
        else:
            self.master.inject(
                f"worker {phase} done, slot {freed_id} free"
                " -- run `swarm context` and launch what is ready"
            )

    # -- rule 2 + 3: master-idle, then maybe finish -----------------------
    def _on_master_idle(self) -> None:
        self.master.kill()
        with state_mod.transaction(self.cfg) as st:
            st.master_alive = False
            busy = st.any_busy()
            paused = st.paused
            ctx = master_mod.build_context(self.cfg, st)
        self.log.line(f"EVENT master-idle busy={busy} paused={paused} ready={ctx['ready']}")
        if paused:
            # Held: do not finish while paused — resume decides what happens next.
            self.log.line("MASTER-IDLE paused — holding")
            return
        if not busy:
            if ctx["ready"]:
                # Accepted lost-injection race: nothing is running yet a phase is
                # ready. We finish anyway (no backstop); the owner is told (log +
                # telegram) and can `swarm launch` it manually.
                self.log.line(f"FINISH-WITH-READY leftover={ctx['ready']}")
            self._finish(len(ctx["done"]), ctx["ready"])

    def _spawn_master(self, kind: str) -> None:
        with state_mod.transaction(self.cfg) as st:
            master_pane = st.master_pane
        if not self.master.spawn(kind, master_pane):
            # No master is running; do not claim one is alive (else the next
            # `done` would inject into nothing). Owner sees spawn-master-failed.
            return
        with state_mod.transaction(self.cfg) as st:
            st.master_alive = True

    # -- finish -----------------------------------------------------------
    def _finish(self, done_count: int, leftover: list[str] | None = None) -> None:
        with state_mod.transaction(self.cfg) as st:
            if st.finished:
                return
            st.finished = True
        self._reap_teammates()
        msg = f"swarm finished: {done_count} phase(s) done"
        if leftover:
            # Accepted-race surfacing: a nudge was lost, so these ready phases
            # were never launched. Tell the owner how to resume (no auto-retry).
            msg += (
                f"; {len(leftover)} ready but unlaunched (lost nudge): "
                f"{', '.join(leftover)} -- run `swarm launch <phase>` to resume"
            )
        telegram.notify(self.cfg.telegram_notify, msg)
        self.log.line("ACTION finish")
        self._stop = True

    # -- teammate hygiene (tmux only) -------------------------------------
    def _reconcile(self) -> None:
        if self.cfg.driver != "tmux":
            return
        with state_mod.transaction(self.cfg) as st:
            workers = st.windows.get("workers")
            teammates = st.windows.get("teammates")
        if not workers or not teammates:
            return
        moved = tmux.reconcile_teammates(workers, teammates)
        if moved:
            self.log.line(f"RECONCILE moved={moved}")

    def _reap_teammates(self) -> None:
        if self.cfg.driver != "tmux":
            return
        with state_mod.transaction(self.cfg) as st:
            teammates = st.windows.get("teammates")
        if not teammates:
            return
        for pane in tmux.list_panes(teammates):
            tmux.kill_pane(pane)


def main(cfg: Config) -> None:
    """Entry point used by the detached ``swarm up`` supervisor process."""
    Supervisor(cfg).run()
