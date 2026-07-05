"""The supervisor: sole FIFO reader, sole master-killer, sole finisher.

It implements EXACTLY the four-rule pure-injection lifecycle and nothing more —
no redo/coalescing, no need re-derivation, no sentinel reconcile, no safety
timer, no pane-died watchdog, no auto-retry:

1. ``done``       -> free the slot; spawn a master if none is alive, else inject.
2. ``master-idle``-> kill the master pane.
3. after a kill   -> finish when no slot is busy AND nothing is integrating.
4. a parked worker keeps its slot busy, so finish cannot fire while it is parked.

Being the single FIFO reader gives total event ordering. Integration (worktree
mode) runs inline on this loop but can never crash it: every git failure is a
:class:`gitq.GitError` that holds the merge-queue for a human instead of
unwinding the loop, and the loop's teardown runs in a ``finally`` so the master
is never orphaned. The only remaining races are in keystroke delivery (accepted
by the owner; never papered over here).
"""

from __future__ import annotations

import os
import select
import signal
from pathlib import Path

from . import gitq
from . import master as master_mod
from . import resolver as resolver_mod
from . import state as state_mod
from . import telegram
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
        """Open the FIFO and process events until stopped.

        Teardown (kill the master, close the FIFO) runs in a ``finally`` so that
        even an unexpected exception escaping a handler can never leave a live
        master pane orphaned or the FIFO open — the sole supervisor always exits
        clean.
        """
        self._open_fifo()
        self._install_signals()
        with state_mod.transaction(self.cfg) as st:
            st.supervisor_pid = os.getpid()
        self.log.line(f"SUPERVISOR-START pid={os.getpid()} driver={self.cfg.driver}")
        buf = b""
        try:
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
        finally:
            if self.master.is_alive():
                self.master.kill()  # never orphan a master on the way out
                with state_mod.transaction(self.cfg) as st:
                    st.master_alive = False
            if self._fifo_fd >= 0:
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
        elif verb == "resolved":
            self._on_resolved(parts[1] if len(parts) > 1 else "?")
        elif verb == "bootstrap":
            self._on_bootstrap()
        elif verb == "resume":
            self._on_resume()
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
        else:
            # A live-but-idle master launched nothing while paused; re-drive it
            # (symmetric with the `done` inject path) so it fills free slots now
            # instead of idling straight into a premature finish.
            self.master.inject(
                "resumed -- run `swarm context` and launch what is ready"
            )

    # -- rule 1: done -----------------------------------------------------
    def _on_done(self, phase: str, status: str) -> None:
        """Integrate first (worktree mode), then run the pure-injection advance.

        Non-worktree runs go straight to :meth:`_advance_done`. Worktree
        integrating runs (``ok`` and ``needs-owner`` — see
        :data:`gitq.DONE_INTEGRATE`) enqueue the phase and pump the merge-queue;
        ``needs-owner`` lands identically to ``ok`` here (its owner ping already
        fired worker-side). A ``fail`` drops the phase's branch(es) and advances —
        UNLESS the phase is already integrating (blocked or queued after having
        reported success): a late, contradictory ``fail`` must not yank a branch
        out from under a live merge or free a parked slot, so it is ignored.
        """
        if self.cfg.git_isolation != "worktree":
            self._advance_done(phase, status)
            return
        if status not in gitq.DONE_INTEGRATE:
            with state_mod.transaction(self.cfg) as st:
                integrating = st.integ_blocked == phase or phase in st.integ_queue
            if integrating:
                self.log.line(f"DONE-FAIL-IGNORED {phase} integrating")
                return
            gitq.discard(self.cfg, phase, self.log)  # failed build: roll back branches
            self._advance_done(phase, status)
            return
        with state_mod.transaction(self.cfg) as st:
            already = phase in st.done
            has_slot = any(s.busy and s.phase == phase for s in st.slots)
            if not (already and not has_slot) and phase not in st.integ_queue:
                st.integ_queue.append(phase)
        self._pump_integrations()

    def _pump_integrations(self) -> None:
        """Drain ``integ_queue`` head-first while nothing is blocked."""
        while True:
            with state_mod.transaction(self.cfg) as st:
                if st.integ_blocked is not None or not st.integ_queue:
                    return
                phase = st.integ_queue[0]
                already = phase in st.done
                has_slot = any(s.busy and s.phase == phase for s in st.slots)
            if already and not has_slot:
                self._dequeue(phase)
                self._advance_done(phase, "ok")  # duplicate -> DONE-DUPLICATE no-op
                continue
            try:
                result = gitq.integrate(self.cfg, phase, self.log)
            except gitq.GitError as exc:
                # A git failure must never kill the sole FIFO reader. Hold the
                # queue for the owner instead of unwinding the loop.
                self.log.line(f"INTEGRATE-ERROR {phase} {exc}")
                self._hold(phase, gitq.DIRTY, None, f"git error integrating {phase}: {exc}")
                return
            if result == gitq.MERGED:
                self._dequeue(phase)
                self._advance_done(phase, "ok")
                continue
            # A conflict OR a dirty tree leaves an identifiable repo to clear; a
            # push failure leaves the tree clean (nothing to resolve — retry).
            repo = (
                gitq.blocked_repo(self.cfg, phase)
                if result in (gitq.CONFLICT, gitq.DIRTY)
                else None
            )
            self._hold(phase, result, repo, None)  # head stays queued, queue held
            return

    def _dequeue(self, phase: str) -> None:
        with state_mod.transaction(self.cfg) as st:
            if st.integ_queue and st.integ_queue[0] == phase:
                st.integ_queue.pop(0)

    def _hold(
        self, phase: str, kind: str, repo: Path | None, detail: str | None
    ) -> None:
        """Park the head phase and HOLD the queue until it is resolved/retried.

        Only a genuine :data:`gitq.CONFLICT` (a repo left mid-merge) spawns a
        claude resolver pane. A :data:`gitq.DIRTY` tree or a :data:`gitq.PUSH_FAILED`
        has nothing to *resolve* — the owner cleans the tree / restores
        connectivity and re-runs ``swarm resolved <phase>`` to retry — so no
        resolver is opened for those (avoiding a resolver staring at a clean tree
        with nothing to fix).
        """
        with state_mod.transaction(self.cfg) as st:
            st.integ_blocked = phase
            st.integ_blocked_kind = kind
            st.integ_blocked_repo = str(repo) if repo is not None else None
        if kind == gitq.CONFLICT and repo is not None:
            pane = resolver_mod.spawn(self.cfg, phase, repo, self.log)
            if pane is not None:
                with state_mod.transaction(self.cfg) as st:
                    st.windows[f"resolve:{phase}"] = pane
            msg = (
                f"swarm: merge conflict integrating {phase} in {repo.name}; resolver"
                f" pane open -- run `swarm resolved {phase}` once fixed"
            )
        elif kind == gitq.DIRTY:
            where = f" in {repo.name}" if repo is not None else ""
            msg = detail or (
                f"swarm: {phase} held -- the working tree{where} has uncommitted"
                f" changes; commit or stash them, then `swarm resolved {phase}`"
            )
        else:  # PUSH_FAILED
            msg = (
                f"swarm: {phase} merged locally but the push failed (remote"
                f" unreachable?); fix it, then `swarm resolved {phase}` to retry"
            )
        self.log.line(f"INTEGRATE-BLOCKED {phase} {kind}")
        telegram.notify(self.cfg.telegram_notify, msg)

    # -- resolved: finish a blocked integration, resume the queue ---------
    def _on_resolved(self, phase: str) -> None:
        with state_mod.transaction(self.cfg) as st:
            blocked = st.integ_blocked
            repo_s = st.integ_blocked_repo
        if blocked != phase:
            self.log.line(f"RESOLVED-IGNORED expected={blocked} got={phase}")
            return
        repo = Path(repo_s) if repo_s else None
        if repo is not None and not gitq.resolve_ready(self.cfg, repo):
            # Resolver / owner signalled early (still mid-merge or dirty): stay blocked.
            self.log.line(f"RESOLVED-INCOMPLETE {phase} still-blocked")
            telegram.notify(
                self.cfg.telegram_notify,
                f"swarm: {phase} not finished yet ({repo.name} still has an unfinished"
                f" merge / dirty tree) -- resolve + commit, then re-run `swarm resolved {phase}`",
            )
            return
        with state_mod.transaction(self.cfg) as st:
            st.integ_blocked = None
            st.integ_blocked_repo = None
            st.integ_blocked_kind = None
            win = st.windows.pop(f"resolve:{phase}", None)
        if win:
            resolver_mod.close(self.cfg, win, self.log)
        self.log.line(f"RESOLVED {phase}")
        self._pump_integrations()  # re-integrate (resumes; may re-block downstream)

    # -- rule 1 core (pure-injection): free slot + spawn/inject -----------
    def _advance_done(self, phase: str, status: str) -> None:
        with state_mod.transaction(self.cfg) as st:
            already = phase in st.done
            freed = st.free_slot_for(phase)
            if already and freed is None:
                # Duplicate `swarm done` for an already-completed phase (its slot
                # was already freed/reused). True no-op: DON'T overwrite the
                # recorded status and don't spuriously spawn/inject.
                self.log.line(f"DONE-DUPLICATE {phase} ignored")
                return
            st.mark_done(phase, status)
            freed_id = freed.id if freed else None
            paused = st.paused
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
            integrating = bool(st.integ_queue) or st.integ_blocked is not None
            paused = st.paused
            ctx = master_mod.build_context(self.cfg, st)
        self.log.line(
            f"EVENT master-idle busy={busy} integrating={integrating}"
            f" paused={paused} ready={ctx['ready']}"
        )
        if paused:
            # Held: do not finish while paused — resume decides what happens next.
            self.log.line("MASTER-IDLE paused — holding")
            return
        if busy or integrating:
            # A running/parked worker or a pending/blocked integration keeps the
            # supervisor alive: it must stay in select() so `resolved`/`done` can
            # still complete the merge-queue rather than finishing mid-flight.
            return
        if ctx["ready"]:
            # Accepted lost-injection race: nothing is running or integrating yet
            # a phase is ready. We finish anyway (no backstop); the owner is told
            # (log + telegram) and can `swarm launch` it manually.
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


def main(cfg: Config) -> None:
    """Entry point used by the detached ``swarm up`` supervisor process."""
    Supervisor(cfg).run()
