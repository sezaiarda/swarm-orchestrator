"""The supervisor: sole FIFO reader, sole master-killer, sole finisher.

It implements EXACTLY the four-rule pure-injection lifecycle and nothing more —
no redo/coalescing, no need re-derivation, no sentinel reconcile, no auto-retry.
It is event-driven; its timed wakes are the park deadline a ``waiting`` worker
arms (rule 4) and — only when ``[swarm].watchdog_s`` is non-zero — a periodic
liveness reconcile (see :meth:`Supervisor._watchdog_tick`):

1. ``done``       -> free the slot; spawn a master if none is alive, else inject.
2. ``master-idle``-> kill the master pane.
3. after a kill   -> finish when nothing is ``pending``, nothing is integrating and
   no operator hand-off is still owed.
4. a worker that is still busy, ``waiting`` on the owner, or ``parked`` in its own
   window keeps the swarm ``pending`` — so finish cannot fire until every phase has
   run ``swarm done``. Parking frees the grid slot (via the deadline-driven
   ``select`` timeout) but not the obligation, so ``pending()`` — not ``any_busy`` —
   gates the finish.

Being the single FIFO reader gives total event ordering, and being the only
reader also makes it a single point of failure: an exception escaping a handler
used to unwind the ``while`` loop straight into the ``finally``, which logged
``SUPERVISOR-STOP`` — byte-identical to a clean shutdown. Every dispatch is
therefore wrapped (:meth:`Supervisor._dispatch`): a handler that raises is logged,
telegrammed and *stepped over*, and only a failure of the loop machinery itself
is fatal — and even that announces itself before the ``finally`` runs. Git
failures were already contained: each is a :class:`gitq.GitError` that holds the
merge-queue for a human instead of unwinding the loop. The remaining races are in
keystroke delivery (accepted by the owner; never papered over here).
"""

from __future__ import annotations

import json
import os
import select
import signal
import time
from dataclasses import asdict
from pathlib import Path

from . import gitq
from . import master as master_mod
from . import operator as operator_mod
from . import opqueue
from . import resolver as resolver_mod
from . import state as state_mod
from . import reload as reload_mod
from . import telegram, tmux
from .config import Config, load
from .logutil import Log


class Supervisor:
    """Long-running owner of the FIFO, ``state.json`` and the master lifecycle."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = Log(cfg.supervisor_log, echo=bool(os.environ.get("SWARM_LOG_ECHO")))
        self.master = master_mod.Master(cfg, self.log)
        self._stop = False
        self._fifo_fd = -1
        # Opt-in liveness watchdog. 0 disables it, restoring the purely
        # event-driven loop byte-for-byte. Read defensively so the supervisor
        # works against a config that predates the field.
        self.watchdog_s = max(0.0, float(getattr(cfg, "watchdog_s", 300) or 0))
        self._last_sweep = 0.0
        # (slot id -> phase) seen dead on the PREVIOUS sweep. A slot is only
        # reaped after two consecutive sightings, so the claim-then-respawn
        # window in `launch` can never be mistaken for a dead worker.
        self._suspect: dict[int, str] = {}
        self._pinged: dict[str, float] = {}  # ping key -> last send (cooldown)

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

        Nothing an event handler does may end the run: every dispatch goes through
        :meth:`_dispatch`, which logs + telegrams a raising handler and carries on.
        A failure of the loop machinery itself IS fatal, but announces itself
        (``SUPERVISOR-CRASH`` + telegram) before unwinding, so a dead supervisor
        can never again look like a clean shutdown.

        Teardown (kill the master, close the FIFO) runs in a ``finally`` so that
        even a fatal exception can never leave a live master pane orphaned or the
        FIFO open — the sole supervisor always exits clean.
        """
        self._open_fifo()
        self._install_signals()
        with state_mod.transaction(self.cfg) as st:
            st.supervisor_pid = os.getpid()
            st.last_event_at = time.time()
        # Record the config actually in force, so `swarm reload` has an honest
        # "before" to diff against once the file on disk has been edited.
        self._write_config_snapshot()
        self.log.line(
            f"SUPERVISOR-START pid={os.getpid()} driver={self.cfg.driver}"
            f" watchdog_s={self.watchdog_s:g}"
        )
        buf = b""
        try:
            while not self._stop:
                try:
                    ready, _, _ = select.select(
                        [self._fifo_fd], [], [], self._select_timeout()
                    )
                except InterruptedError:
                    continue
                # Fire on EVERY wake — a FIFO event, a park-deadline timeout or a
                # watchdog tick (the latter two leave `ready` empty and fall
                # through the guard below).
                self._dispatch("park-deadlines", self._check_park_deadlines)
                self._dispatch("operator-queue", self._check_operator_queue)
                self._dispatch("watchdog", self._watchdog_tick)
                if self._fifo_fd not in ready:
                    continue
                try:
                    chunk = os.read(self._fifo_fd, 65536)
                except BlockingIOError:
                    continue  # spurious select wake; nothing to read yet
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    self._dispatch(line, self._handle, line)
                    if self._stop:
                        break
        except Exception as exc:  # noqa: BLE001 - announce, then re-raise
            self.log.line(f"SUPERVISOR-CRASH {exc!r}")
            self._ping(
                "crash",
                f"swarm: supervisor CRASHED ({exc!r}) -- the run is over; nothing"
                " will be launched, integrated or finished. Run `swarm up` to restart.",
                cooldown=0.0,
            )
            raise
        finally:
            if self.master.is_alive():
                self.master.kill()  # never orphan a master on the way out
                with state_mod.transaction(self.cfg) as st:
                    st.master_alive = False
            # Same obligation, and it matters more: an operator session holds the
            # owner's full authority on the host, so leaving one typing into a
            # window nothing owns is worse than leaving a master.
            operator_mod.release(self.cfg, self.log)
            if self._fifo_fd >= 0:
                os.close(self._fifo_fd)
            self.log.line("SUPERVISOR-STOP")
            self.log.close()

    # -- failure containment ----------------------------------------------
    def _dispatch(self, what: str, fn, *args) -> bool:
        """Run one loop step, absorbing anything it raises. True == it completed.

        The sole FIFO reader must outlive its own handlers. ``gitq`` failures were
        already contained, but nothing caught a ``CalledProcessError`` from tmux —
        and ``send_literal`` / ``send_enter`` / ``set_slot`` / ``respawn_pane`` /
        ``split_one`` / ``break_pane`` all run ``check=True``, so a single vanished
        pane (``tmux send-keys -t %999`` exits 1) killed the loop. The exception
        escaped into the ``finally``, which wrote ``SUPERVISOR-STOP`` and looked
        exactly like a clean exit: no ``_finish``, no telegram, ``finished`` left
        false, and every later ``done`` poke dropped on ENXIO. Stepping over the
        failure keeps the swarm alive and tells the owner which event broke."""
        try:
            fn(*args)
            return True
        except Exception as exc:  # noqa: BLE001 - a handler must never end the run
            self.log.line(f"HANDLER-ERROR {what!r} {exc!r}")
            self._ping(
                f"handler:{what.split(' ')[0]}",
                f"swarm: supervisor error on {what!r}: {exc}",
            )
            return False

    def _ping(
        self,
        key: str,
        msg: str,
        cooldown: float | None = None,
        *,
        kind: str = "other",
        source: str = "",
    ) -> None:
        """Telegram ``msg``, at most once per ``cooldown`` seconds for this ``key``.

        Every ping the supervisor sends outside the normal lifecycle reports a
        *condition*, not an event — a dead pane, a stalled queue, a handler that
        keeps raising. Without a per-key cooldown a condition that reasserts on
        every wake would ping-storm the owner's phone.

        ``kind``/``source`` are ledger metadata, forwarded to
        :func:`telegram.notify`. They are parameters because a caller was already
        passing them: :meth:`_on_reload` handed them to *this* method, which had
        no such arguments, so every failed reload raised ``TypeError`` inside
        :meth:`_dispatch` and reported ``HANDLER-ERROR`` — a config parse error
        told the owner the supervisor had broken instead."""
        gap = self.watchdog_s * 4 if cooldown is None else cooldown
        now = time.time()
        if gap and now - self._pinged.get(key, 0.0) < gap:
            return
        self._pinged[key] = now
        telegram.notify(self.cfg.telegram_notify, msg, kind=kind, source=source)

    # -- event dispatch ---------------------------------------------------
    def _handle(self, line: str) -> None:
        if not line:
            return
        with state_mod.transaction(self.cfg) as st:
            st.last_event_at = time.time()
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
        elif verb == "waiting":
            self._on_waiting(parts[1] if len(parts) > 1 else "?")
        elif verb == "resumed":
            self._on_resumed(parts[1] if len(parts) > 1 else "?")
        elif verb == "bootstrap":
            self._on_bootstrap()
        elif verb == "resume":
            self._on_resume()
        elif verb == "reload":
            self._on_reload()
        elif verb == "operator":
            self._on_operator(parts[1] if len(parts) > 1 else "?")
        elif verb == "operator-done":
            self._on_operator_done(parts[1] if len(parts) > 1 else "?")
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
    def _write_config_snapshot(self) -> None:
        """Record the config this supervisor is actually running on.

        A reload has to diff against the values in force *now*, and only this
        process knows them: it loaded the file at `swarm up`, and `load()` layers
        SWARM_* env overrides from this process's own environment on top. A CLI
        re-reading the file would compute the wrong "before" for every overridden
        field, and could not see them at all once the file had been edited.
        """
        try:
            snap = {
                k: (str(v) if isinstance(v, Path) else v)
                for k, v in asdict(self.cfg).items()
            }
            path = self.cfg.state_dir / "config.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, TypeError) as exc:
            self.log.line(f"CONFIG-SNAPSHOT-FAILED {exc}")

    def _on_reload(self) -> None:
        """Re-read .swarm.toml and apply what can safely change mid-run.

        Parse-validate-swap, never half-apply: a broken file leaves the running
        config untouched and says so. RESTART-class fields are reverted to their
        live values by ``reload.hold_over`` before anything is adopted, so a
        reload can never move the state dir, rename the tmux session or fork the
        run in two.
        """
        try:
            new_cfg = load(
                explicit=str(self.cfg.config_file) if getattr(self.cfg, "config_file", None) else None,
                project_dir=str(self.cfg.project_dir),
            )
        except (ValueError, OSError) as exc:
            self.log.line(f"RELOAD-ERROR {exc}")
            self._ping(
                "reload-error",
                f"swarm: {self.cfg.slug} config reload FAILED, still running the old "
                f"config — {exc}",
                kind="other",
                source="supervisor._on_reload",
            )
            return
        st = state_mod.read(self.cfg)
        facts = reload_mod.Facts.from_state(st)
        payload = reload_mod.plan(self.cfg, new_cfg, facts)
        applied = payload.cfg  # held-over: RESTART fields reverted
        if applied.state_dir != self.cfg.state_dir:
            self.log.line("RELOAD-ABORT state-dir-would-move")
            return

        # These take the VALUES, not the configs.
        prior_workers = self.cfg.max_workers
        drop_ids, retire_ids = reload_mod.retire_plan(
            self.cfg.max_workers, applied.max_workers, facts
        )
        shifts = reload_mod.park_shift(
            self.cfg.park_after, applied.park_after, facts
        )

        self.cfg = applied
        self.master.cfg = applied
        # watchdog_s is cached on the instance (read per select, not per event),
        # so swapping self.cfg alone would leave the old sweep interval running.
        self.watchdog_s = max(0.0, float(getattr(applied, "watchdog_s", 300) or 0))

        # resize() is driven by the VALUE changing, not by retire_plan returning
        # work: growing the pool yields no drops and no retirements, so gating on
        # those meant `max_workers 2 -> 4` reloaded cleanly and added no slots.
        grew = applied.max_workers != prior_workers
        if grew or shifts:
            with state_mod.transaction(self.cfg) as s2:
                if grew:
                    added, retiring = s2.resize(applied.max_workers)
                    # resize only MARKS the surplus; reaping drops the ones that
                    # are free right now. A retiring slot that is still BUSY keeps
                    # its record and its pane and drains normally -- deleting it
                    # would make its `swarm done` land on a slot that no longer
                    # exists, degrade to a DONE-DUPLICATE no-op, and strand the
                    # phase with its worktree leaked.
                    reaped = s2.reap_retired()
                    self.log.line(
                        f"RELOAD-RESIZE {prior_workers}->{applied.max_workers} "
                        f"added={added} retiring={retiring} reaped={reaped}"
                    )
                for phase, deadline in shifts.items():
                    if phase in s2.waiting:
                        s2.waiting[phase] = deadline
        for slot_id in drop_ids:
            self.log.line(f"RELOAD-SLOT-DROPPED {slot_id}")
        for slot_id in retire_ids:
            self.log.line(f"RELOAD-SLOT-RETIRING {slot_id}")

        changed = [c.name for c in payload.changes]
        self.log.line("RELOAD " + (" ".join(changed) or "no-change"))
        self._write_config_snapshot()
        if not st.paused and not st.finished:
            self._relaunch(None, "config reloaded")

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
            if not (already and not has_slot):
                st.integ_push(phase, status)
        self._pump_integrations()

    def _pump_integrations(self) -> None:
        """Drain ``integ_queue`` head-first while nothing is blocked.

        The status the worker reported travels with the phase and is what gets
        recorded in ``done``. Hardcoding ``"ok"`` here silently rewrote every
        ``needs-owner`` as a clean success the moment it merged — so ``swarm
        status`` could never tell the owner which phases were waiting on them
        (every sentinel said ``needs-owner``; state.json said ``ok``)."""
        while True:
            with state_mod.transaction(self.cfg) as st:
                if st.integ_blocked is not None or not st.integ_queue:
                    return
                phase, status = st.integ_head()
                already = phase in st.done
                has_slot = any(s.busy and s.phase == phase for s in st.slots)
            if already and not has_slot:
                self._dequeue(phase)
                self._advance_done(phase, status)  # duplicate -> DONE-DUPLICATE no-op
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
                self._advance_done(phase, status)
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
            st.integ_pop(phase)

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

    # -- the operator session: dispatch, and the signal it is over -------
    def _on_operator(self, phase: str) -> None:
        """Open a session for ``phase`` — the triage child's ``now`` poke, and
        ``swarm operator <phase>`` by hand.

        The loop is single-threaded, so ``_dispatch`` already serialises this
        against the queue sweep; what stops the two of them opening *two* sessions
        is the lease, which both take."""
        operator_mod.dispatch(self.cfg, phase, self.log, reason="poked")

    def _on_operator_done(self, phase: str) -> None:
        """The session signalled it is finished (carried out, or asked the owner).

        The item was already settled by the CLI that sent this — durably, before
        the poke, for the same reason ``swarm done`` writes its sentinel first.
        All that is left is the session itself: drop the lease, idle the pane, and
        re-check the finish the outstanding hand-off was holding open."""
        released = operator_mod.release(self.cfg, self.log)
        self.log.line(f"EVENT operator-done {phase} released={released}")
        operator_mod.sweep(self.cfg, self.log)  # next hand-off, if one is due
        self._finish_if_settled(state_mod.read(self.cfg))

    def _check_operator_queue(self) -> None:
        """Drain the operator queue. Runs on every wake, like the park deadlines.

        Deliberately NOT inside :meth:`_watchdog_tick`: that returns early while
        ``idle < watchdog_s``, and :meth:`_handle` refreshes the idle clock on
        every FIFO line, so a swarm that is moving never reaches the quiet point
        — and the queue would drain only once the run was already over."""
        operator_mod.sweep(self.cfg, self.log)

    def _operator_blocking(self) -> list[str]:
        """Hand-offs that must hold the finish open, checked BESIDE ``pending()``.

        Not folded into :meth:`state.State.pending`: that is a pure ``State``
        method which cannot read the filesystem queue, and gating it would make a
        non-empty queue both the trigger for opening a session and the reason the
        launch path returns early — a deadlock, not a guard."""
        return operator_mod.blocking(self.cfg)

    # -- parking: a worker waiting on the owner vacates its slot after a delay --
    def _on_waiting(self, phase: str) -> None:
        """Arm the park timer for a worker that self-reported it needs the owner.

        The FIFO poke already woke ``select``; recording the deadline re-arms the
        loop's timeout on the next pass. ``park_after == 0`` disables parking (the
        waiting worker just holds its slot, unchanged from before)."""
        if self.cfg.park_after <= 0:
            self.log.line(f"WAITING-IGNORED {phase} parking-disabled")
            return
        with state_mod.transaction(self.cfg) as st:
            st.waiting[phase] = time.time() + self.cfg.park_after
        self.log.line(f"EVENT waiting {phase} park_after={self.cfg.park_after}")

    def _on_resumed(self, phase: str) -> None:
        """The owner answered before the park fired: cancel the pending park.

        Distinct from the ``resume`` (unpause) verb. A phase that was ALREADY
        parked is not in ``waiting``, so this is a no-op for it — it stays in its
        own window until ``swarm done``."""
        with state_mod.transaction(self.cfg) as st:
            cancelled = st.waiting.pop(phase, None) is not None
        self.log.line(f"EVENT resumed {phase} cancelled={cancelled}")

    def _next_timeout(self) -> float | None:
        """Seconds until the earliest scheduled deadline, or ``None`` when nothing
        is scheduled (``select`` then blocks indefinitely — no busy-poll).

        Two kinds of deadline, both timestamps: a park deadline a ``waiting``
        worker armed, and whatever the operator queue next wants looking at — a
        backed-off item becoming eligible, or a lease running out. The queue
        belongs here for exactly the reason ``run_after`` is a timestamp rather
        than a flag: a supervisor that only wakes on input sits straight past it,
        and with ``watchdog_s = 0`` there is no other wake at all. A deadline
        already in the past clamps to 0.0 so the next wake acts on it
        immediately."""
        stamps = list(state_mod.read(self.cfg).waiting.values())
        queued = opqueue.next_deadline(self.cfg)
        if queued is not None:
            stamps.append(queued)
        if not stamps:
            return None
        return max(0.0, min(stamps) - time.time())

    def _select_timeout(self) -> float | None:
        """How long ``select`` may block: the next park deadline, capped by the
        watchdog interval.

        Deliberately distinct from :meth:`_next_timeout`, which answers a different
        question — *when is the next scheduled deadline* — and still returns
        ``None`` when nothing is scheduled. The cap is not a deadline; it is the
        ceiling that makes a stall detectable at all. Without it ``select`` blocks
        forever whenever nothing is ``waiting``, so with no pane-died watchdog NO
        timer could fire in steady state: four workers killed by an OOM left four
        slots busy forever, ``pending()`` true forever, ``_finish`` unreachable, and
        the owner un-notified (a silent outage of unbounded length). With
        ``watchdog_s = 0`` this returns ``_next_timeout`` unchanged, so the purely
        event-driven loop is preserved byte-for-byte."""
        deadline = self._next_timeout()
        if not self.watchdog_s:
            return deadline
        cap = max(0.0, self._last_sweep + self.watchdog_s - time.time())
        return cap if deadline is None else min(deadline, cap)

    def _touch(self) -> None:
        """Record that something moved (used to measure a stall)."""
        with state_mod.transaction(self.cfg) as st:
            st.last_event_at = time.time()

    # -- watchdog: the one poll, opt-in ------------------------------------
    def _watchdog_tick(self) -> None:
        """One idempotent liveness reconcile, at most once per ``watchdog_s``.

        The lifecycle is driven purely by worker input, which is exactly why it
        cannot detect the absence of input: a worker killed out-of-band (host OOM,
        a closed pane, a crashed claude) never sends ``done``, so its slot stays
        busy and the run hangs silently forever. This sweep is the only poll in the
        supervisor and it only ever asserts what the event path would have done:

        * a busy slot whose pane is gone -> free it, roll back its branches, ping;
        * idle, unpaused, unblocked, with a free slot and ready phases -> re-nudge
          the master (the accepted lost-injection race, now recoverable);
        * settled with nothing left to do -> finish (rule 3), because the
          ``master-idle`` that would normally notice was already refused while a
          since-reaped worker still held its slot, and none will arrive again;
        * ``finished`` with phases still ready -> tell the owner they were dropped.

        Everything is gated on ``idle >= watchdog_s`` (bar the pane check, which is
        confirmed across two sweeps instead), so a swarm that is making progress is
        never touched. ``watchdog_s = 0`` disables the whole thing.
        """
        if not self.watchdog_s:
            return
        now = time.time()
        if now - self._last_sweep < self.watchdog_s:
            return  # not due yet; a FIFO wake must not re-sweep
        self._last_sweep = now
        st = state_mod.read(self.cfg)
        idle = now - st.last_event_at if st.last_event_at else 0.0
        self.log.line(
            f"WATCHDOG idle={idle:.0f}s busy={sorted(s.phase for s in st.busy_slots() if s.phase)}"
            f" queue={st.integ_queue} blocked={st.integ_blocked} paused={st.paused}"
        )
        if self._reap_dead_panes(st):
            st = state_mod.read(self.cfg)  # slots changed under us
        if idle < self.watchdog_s:
            return  # something moved recently -- leave a live swarm alone
        ready = master_mod.build_context(self.cfg, st)["ready"]
        if not ready:
            self._check_operator_queue()  # opportunistic: a genuinely quiet swarm
            self._finish_if_settled(st)
            return
        if st.finished:
            self._ping(
                "finished-with-ready",
                f"swarm: run finished but {len(ready)} phase(s) never launched: "
                f"{', '.join(ready[:8])} -- run `swarm up` to resume",
            )
            return
        if st.paused or st.integ_blocked is not None or not st.free_slots():
            return
        self.log.line(f"WATCHDOG-RELAUNCH idle={idle:.0f}s ready={ready}")
        self._touch()  # the nudge counts as movement; don't re-fire next sweep
        self._relaunch(None, f"watchdog: idle {idle:.0f}s with ready phases")

    def _finish_if_settled(self, st: state_mod.State) -> None:
        """Apply rule 3 when no ``master-idle`` can ever arrive to apply it.

        The finish check runs only on ``master-idle``, and that event is
        single-shot: the last one was refused because a worker still held a slot,
        and once that worker is reaped (it never sent ``done``) nothing re-triggers
        it. The run is then complete but never *finishes* — no telegram, ``finished``
        left false. Same guards as :meth:`_on_master_idle`, so this can only fire on
        a run that is genuinely settled."""
        if st.finished or st.paused or st.pending():
            return
        if st.integ_queue or st.integ_blocked is not None or self.master.is_alive():
            return
        owed = self._operator_blocking()
        if owed:
            self.log.line(f"WATCHDOG-FINISH-HELD operator={owed}")
            return
        self.log.line("WATCHDOG-FINISH settled")
        self._on_master_idle()

    def _reap_dead_panes(self, st: state_mod.State) -> list[str]:
        """Free every busy slot whose worker pane has vanished. Returns the phases.

        A phase is only reaped after being seen dead on TWO consecutive sweeps:
        ``launch`` claims the slot before it creates the worktree and respawns the
        pane, so a single sighting inside that window would reap a worker that is
        about to start. Bare-driver runs have no panes and are skipped entirely.
        """
        if self.cfg.driver != "tmux":
            return []
        suspect: dict[int, str] = {}
        reaped: list[str] = []
        for slot in st.busy_slots():
            if not slot.pane_id or not slot.phase or self._pane_alive(slot.pane_id):
                continue
            if self._suspect.get(slot.id) != slot.phase:
                suspect[slot.id] = slot.phase  # first sighting; confirm next sweep
                self.log.line(
                    f"WATCHDOG-SUSPECT {slot.phase} slot={slot.id} pane={slot.pane_id}"
                )
                continue
            reaped.append(slot.phase)
        self._suspect = suspect
        for phase in reaped:
            self._reap(phase)
        return reaped

    def _pane_alive(self, pane_id: str) -> bool:
        """False when tmux no longer knows ``pane_id``, or the pane's process
        exited and it is only being held open by ``remain-on-exit``."""
        out = tmux.run(["display-message", "-p", "-t", pane_id, "#{pane_dead}"])
        if out.returncode != 0:
            return False  # no such pane
        return out.stdout.strip() != "1"

    def _reap(self, phase: str) -> None:
        """Undo a worker that died without reporting: free its slot, roll back its
        branches, tell the owner. Deliberately NOT recorded in ``done`` — the phase
        was never attempted to completion, so it stays relaunchable."""
        self.log.line(f"WATCHDOG-REAP {phase} pane-dead")
        with state_mod.transaction(self.cfg) as st:
            st.free_slot_for(phase)
            st.last_event_at = time.time()
        self._ping(
            f"reap:{phase}",
            f"swarm: worker {phase} died without 'swarm done' -- slot freed and its"
            f" work rolled back; `swarm launch {phase}` to retry",
        )
        if self.cfg.git_isolation == "worktree":
            try:
                gitq.discard(self.cfg, phase, self.log)
            except gitq.GitError as exc:
                self.log.line(f"WATCHDOG-DISCARD-ERROR {phase} {exc}")

    def _check_park_deadlines(self) -> None:
        """Park every waiting phase whose deadline has fired. Runs on every wake;
        a no-op when nothing is due (so a spurious/FIFO wake is harmless)."""
        now = time.time()
        due = [
            p for p, deadline in state_mod.read(self.cfg).waiting.items() if deadline <= now
        ]
        for phase in due:
            self._park(phase)

    def _park(self, phase: str) -> None:
        """Move a waiting worker to its own window, free its grid slot, relaunch.

        Split-FIRST pane mechanic (driver-guarded, so the bare hermetic path is
        PURE STATE): grow the slot's window with a fresh replacement pane BEFORE
        breaking the live waiter out to ``wait:<phase>`` (so ``break-pane`` never
        sees a single-pane window and renames in place), re-tidy the survivors, and
        tag the replacement with the slot. Then ``st.park`` frees the slot record —
        keeping its now-replacement ``pane_id`` — and ``_relaunch`` fills it with
        the next ready phase (the parked phase is excluded from ``ready``)."""
        with state_mod.transaction(self.cfg) as st:
            slot = next((s for s in st.slots if s.busy and s.phase == phase), None)
            if slot is None:
                # No longer busy (already done / freed): just drop the dead timer.
                st.waiting.pop(phase, None)
                self.log.line(f"PARK-SKIP {phase} not-busy")
                return
            sid, old_pane = slot.id, slot.pane_id
            layout = st.layout or self.cfg.tmux_layout
        wait_win: str | None = None
        replacement: str | None = None
        if self.cfg.driver == "tmux" and old_pane:
            wait_win, replacement = tmux.park_pane(
                tmux.window_of(old_pane),
                old_pane,
                sid,
                f"wait:{phase}",
                self.cfg.session,
                layout,
            )
        with state_mod.transaction(self.cfg) as st:
            if wait_win:
                st.windows[f"wait:{phase}"] = wait_win
            if replacement:
                s = st.slot_by_id(sid)
                if s is not None:
                    s.pane_id = replacement
            st.park(phase)
            paused = st.paused
        self.log.line(f"PARK {phase} slot={sid}")
        telegram.notify(
            self.cfg.telegram_notify,
            f"swarm: {phase} moved to its own window (still waiting on you)",
        )
        if paused:
            self.log.line("PARK-PAUSED holding — no launch")
            return
        self._relaunch(sid, f"{phase} parked")

    # -- rule 1 core (pure-injection): free slot + spawn/inject -----------
    def _advance_done(self, phase: str, status: str) -> None:
        with state_mod.transaction(self.cfg) as st:
            already = phase in st.done
            was_parked = phase in st.parked
            freed = st.free_slot_for(phase)
            if already and freed is None and not was_parked:
                # Duplicate `swarm done` for an already-completed phase (its slot
                # was already freed/reused, and it isn't parked). True no-op: DON'T
                # overwrite the recorded status and don't spuriously spawn/inject.
                self.log.line(f"DONE-DUPLICATE {phase} ignored")
                return
            st.mark_done(phase, status)
            # An owner-answered parked worker finishes here: drop it from the
            # waiting/parked tracking (its dependents were only gated because it was
            # not yet in `done`) and reclaim its off-grid window below.
            st.clear_pending(phase)
            wait_win = st.windows.pop(f"wait:{phase}", None) if was_parked else None
            freed_id = freed.id if freed else None
            paused = st.paused
        if wait_win and self.cfg.driver == "tmux":
            tmux.kill_window(wait_win)  # close the parked worker's own window
        self.log.line(
            f"EVENT done {phase} {status} freed_slot={freed_id} parked={was_parked}"
        )
        # After `mark_done`, never from `launch.done`: `swarm done` has to return
        # to its worker immediately, and under worktree isolation this point is
        # the first at which the work the session is briefed about is actually in
        # main. A session opened any earlier acts on a phantom.
        operator_mod.on_finished(self.cfg, phase, self.log)
        if paused:
            # Paused: the slot is freed but we launch nothing and hold — no
            # master spawn/inject, so in-flight workers drain without advancing.
            self.log.line("DONE-PAUSED holding — no launch")
            return
        self._relaunch(freed_id, f"worker {phase} done")

    def _relaunch(self, freed_id: int | None, reason: str) -> None:
        """Fill a just-freed slot: spawn a master if none is alive, else nudge the
        live one to launch what is ready. Shared by :meth:`_advance_done` (a worker
        finished) and :meth:`_park` (a waiting worker vacated its slot)."""
        if not self.master.is_alive():
            self._spawn_master("step")
        else:
            where = f", slot {freed_id} free" if freed_id is not None else ""
            self.master.inject(
                f"{reason}{where} -- run `swarm context` and launch what is ready"
            )

    # -- rule 2 + 3: master-idle, then maybe finish -----------------------
    def _on_master_idle(self) -> None:
        self.master.kill()
        with state_mod.transaction(self.cfg) as st:
            st.master_alive = False
            pending = st.pending()
            integrating = bool(st.integ_queue) or st.integ_blocked is not None
            paused = st.paused
            ctx = master_mod.build_context(self.cfg, st)
        owed = self._operator_blocking()
        self.log.line(
            f"EVENT master-idle pending={pending} integrating={integrating}"
            f" paused={paused} operator={owed} ready={ctx['ready']}"
        )
        if paused:
            # Held: do not finish while paused — resume decides what happens next.
            self.log.line("MASTER-IDLE paused — holding")
            return
        if pending or integrating or owed:
            # A busy/waiting/parked worker, a pending/blocked integration or an
            # undrained operator hand-off keeps the supervisor alive: it must stay
            # in select() so `resolved`/`done`/`operator-done` (and a firing park
            # deadline) can still complete rather than finishing mid-flight.
            return
        if ctx["ready"]:
            # Accepted lost-injection race: nothing is running or integrating yet
            # a phase is ready. We finish anyway (no backstop); the owner is told
            # (log + telegram) and can `swarm launch` it manually.
            self.log.line(f"FINISH-WITH-READY leftover={ctx['ready']}")
        done = ctx["done"]
        built = sum(1 for s in done.values() if s in gitq.DONE_INTEGRATE)
        skipped = sum(1 for s in done.values() if s == "skip")
        failed = sorted(
            p for p, s in done.items() if s not in gitq.DONE_INTEGRATE and s != "skip"
        )
        self._finish(
            built,
            ctx["ready"],
            skipped=skipped,
            failed=failed,
            operator=operator_mod.outstanding(self.cfg),
        )

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
    def _finish(
        self,
        done_count: int,
        leftover: list[str] | None = None,
        skipped: int = 0,
        failed: list[str] | None = None,
        operator: list[str] | None = None,
    ) -> None:
        """Announce the run exactly once.

        ``done_count`` counts phases that actually BUILT (``ok``/``needs-owner``).
        It used to be ``len(done)``, which counts the whole map — so a run that
        skipped phases and failed some reported the whole map as "phase(s) done" though
        only some had built. Skips and failures are real outcomes, but they are not "done", so
        they are reported separately instead of inflating one number."""
        with state_mod.transaction(self.cfg) as st:
            if st.finished:
                return
            st.finished = True
        msg = f"swarm finished: {done_count} phase(s) done"
        if skipped:
            msg += f", {skipped} skipped"
        if failed:
            msg += f", {len(failed)} failed: {', '.join(failed[:8])}"
        if leftover:
            # Accepted-race surfacing: a nudge was lost, so these ready phases
            # were never launched. Tell the owner how to resume (no auto-retry).
            msg += (
                f"; {len(leftover)} ready but unlaunched (lost nudge): "
                f"{', '.join(leftover)} -- run `swarm launch <phase>` to resume"
            )
        if operator:
            # Only reachable past the blocking check, so every one of these is an
            # item that burned its attempt cap: real work nothing will retry. They
            # are the notes the whole feature exists to stop losing — name them.
            msg += (
                f"; {len(operator)} operator hand-off(s) undrained: "
                f"{', '.join(operator[:8])} -- run `swarm operator <phase>`"
            )
        telegram.notify(self.cfg.telegram_notify, msg)
        self.log.line("ACTION finish")
        self._stop = True


def main(cfg: Config) -> None:
    """Entry point used by the detached ``swarm up`` supervisor process."""
    Supervisor(cfg).run()
