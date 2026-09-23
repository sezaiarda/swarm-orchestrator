"""The supervisor: sole FIFO reader, sole master-killer, sole finisher.

It implements EXACTLY the four-rule lifecycle below and nothing more —
no redo/coalescing, no need re-derivation, no sentinel reconcile, and no
auto-retry beyond a failed launch's bounded back-off.
It is event-driven; its timed wakes are the park deadline a ``waiting`` worker
arms (rule 4) and — only when ``[swarm].watchdog_s`` is non-zero — a periodic
liveness reconcile (see :meth:`Supervisor._watchdog_tick`):

1. ``done``       -> free the slot and launch what the ledger makes ready, in
   ledger order, straight from here (:meth:`Supervisor._fill_slots`). No LLM
   master sits between a free slot and its next worker any more: the launchable
   set is a pure function of the ledger and ``state.json``, and waiting on a
   master for it cost a delay and a model call per launch (and could hang outright).
2. ``master-idle``-> kill the master pane. The only master left is the ``init``
   bootstrap pass (command-file patch, telegram preflight); its idle is what
   releases the first launch.
3. finish when nothing is launchable, launching, ``pending``, integrating or
   owed (an operator hand-off, a push) — checked after every event that could
   have been the last (:meth:`Supervisor._finish_if_settled`).
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
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import gitq
from . import launch as launch_mod
from . import master as master_mod
from . import operator as operator_mod
from . import opqueue
from . import pushowed
from . import resolver as resolver_mod
from . import state as state_mod
from . import reload as reload_mod
from . import session as session_mod
from . import telegram, tmux
from .config import Config, load
from .logutil import Log


#: A phase whose launch *failed* (claimed a slot, could not start) is not
#: relaunched on the very next event: the cause is usually still there, and the
#: next free slot would just fail it again and ping again. It waits this long,
#: then is eligible like any other ready phase.
LAUNCH_RETRY_S = 60.0
#: After this many consecutive failed launches a phase is no longer launched
#: automatically; the owner is told once. ``swarm launch <phase>`` or ``swarm
#: resume`` puts it back. It stops holding the finish open, and the finish
#: message names it.
LAUNCH_GIVE_UP = 3


class Supervisor:
    """Long-running owner of the FIFO, ``state.json``, the launcher and the master."""

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
        # The launcher. A launch blocks for up to two READY_TIMEOUTs (worktree
        # build + claude boot + one retry), so each runs on its own thread and
        # reports back through the FIFO as `launched <phase> <outcome>`; the loop
        # never waits on one. `_launching` is the double-launch guard: a phase is
        # in it from the moment it is picked until its thread has settled the
        # state, so no second pick can race the first to `claim_slot`.
        self._launch_lock = threading.Lock()
        self._launching: set[str] = set()
        # phase -> (consecutive failed launches, time of the last one)
        self._launch_fails: dict[str, tuple[int, float]] = {}
        self._retried: set[str] = set()  # back-off expiries already acted on
        # True while the init master's bootstrap pass runs: the worker command it
        # patches has to be committed before the first worktree branches off main.
        self._bootstrapping = False

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
                self._dispatch("launch-retry", self._retry_backed_off)
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
        elif verb == "launched":
            self._on_launched(
                parts[1] if len(parts) > 1 else "?",
                parts[2] if len(parts) > 2 else launch_mod.FAILED,
            )
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
        """Run the init master's one pass, then launch.

        The init pass patches and commits the worker command file; launching
        before that commit would branch every first-batch worktree off a main
        that lacks it. So the first launch waits for its ``master-idle`` — or,
        if no master could be started, happens now. A master that hangs cannot
        hold launching for longer than one watchdog interval."""
        if self.master.is_alive():
            self.log.line("BOOTSTRAP-IGNORED master-alive")
            return
        if self._spawn_master("init"):
            self._bootstrapping = True
            return
        self.log.line("BOOTSTRAP-NO-MASTER launching without the init pass")
        self._fill_slots("bootstrap (no init master)")
        self._finish_if_settled()

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
        new_slots: list[int] = []
        if grew or shifts:
            with state_mod.transaction(self.cfg) as s2:
                if grew:
                    added, retiring = s2.resize(applied.max_workers)
                    new_slots = [
                        s.id for s in s2.slots if not s.pane_id and not s.retiring
                    ]
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
        if new_slots and self.cfg.driver == "tmux":
            self._add_slot_panes(new_slots)
        for slot_id in drop_ids:
            self.log.line(f"RELOAD-SLOT-DROPPED {slot_id}")
        for slot_id in retire_ids:
            self.log.line(f"RELOAD-SLOT-RETIRING {slot_id}")

        changed = [c.name for c in payload.changes]
        self.log.line("RELOAD " + (" ".join(changed) or "no-change"))
        self._write_config_snapshot()
        if not st.paused and not st.finished:
            self._fill_slots("config reloaded")

    def _add_slot_panes(self, slot_ids: list[int]) -> None:
        """Create and record the tmux panes of slots a live grow just added.

        A slot tmux would not give a pane is marked ``retiring`` (and, being
        free, reaped at once): ``claim_slot`` takes the *first* free slot, so a
        paneless one left in the pool would fail every launch that reached it."""
        with state_mod.transaction(self.cfg) as st:
            windows = dict(st.windows)
            layout = st.layout or self.cfg.tmux_layout
        panes, windows, failed = session_mod.add_slot_panes(
            self.cfg, windows, slot_ids, layout
        )
        with state_mod.transaction(self.cfg) as st:
            st.windows.update(windows)
            for sid, pane in panes.items():
                slot = st.slot_by_id(sid)
                if slot is not None:
                    slot.pane_id = pane
            for sid in failed:
                slot = st.slot_by_id(sid)
                if slot is not None and not slot.busy:
                    slot.retiring = True
            if failed:
                st.reap_retired()
        self.log.line(f"RELOAD-PANES added={panes} failed={failed}")
        if failed:
            self._ping(
                "reload-panes",
                f"swarm: {self.cfg.slug} reload could not create a pane for slot(s)"
                f" {failed}; running with {len(panes)} of {len(slot_ids)} new slot(s)",
                cooldown=0.0,
            )

    def _on_resume(self) -> None:
        """Fill free slots after a pause, a ``swarm free`` or a ``swarm retry``.

        Also the owner's way to hand a given-up phase back to the launcher: a
        resume is a person saying "try again", so the failure counts reset."""
        self.log.line("EVENT resume")
        with self._launch_lock:
            self._launch_fails.clear()
            self._retried.clear()
        self._fill_slots("resumed")
        self._finish_if_settled()

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
        (every sentinel said ``needs-owner``; state.json said ``ok``).

        A push failure never holds the queue: :func:`gitq.integrate` reports it
        in ``pushes`` and the phase counts as integrated (its merges are on local
        main, which is all the next worker branches from). The debt is settled
        by :mod:`pushowed` — recorded, pinged once, and retried here after every
        integration for the repos this one did not already push."""
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
            pushes: dict[Path, gitq.PushResult] = {}
            try:
                result = gitq.integrate(self.cfg, phase, self.log, pushes)
            except gitq.GitError as exc:
                # A git failure must never kill the sole FIFO reader. Hold the
                # queue for the owner instead of unwinding the loop.
                self.log.line(f"INTEGRATE-ERROR {phase} {exc}")
                pushowed.settle(self.cfg, phase, pushes, self.log)
                self._hold(phase, gitq.DIRTY, None, f"git error integrating {phase}: {exc}")
                return
            # Before the hold/advance: a repo that merged and failed to push owes
            # it even when a later repo of the same phase then conflicts.
            pushowed.settle(self.cfg, phase, pushes, self.log)
            if result == gitq.MERGED:
                self._dequeue(phase)
                self._advance_done(phase, status)
                pushowed.retry(self.cfg, self.log, skip=set(pushes))
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
        with nothing to fix). :data:`gitq.PUSH_FAILED` no longer reaches here
        from the queue (it is an owed push, :mod:`pushowed`); the branch is a
        fallback. A ``push_failed`` hold an older supervisor recorded is cleared
        by :meth:`_on_resolved` like any other, and re-integrates as owed.
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
                f"swarm: {phase} merged locally but the push failed; fix it, then"
                f" `swarm resolved {phase}` to retry"
            )
        self.log.line(f"INTEGRATE-BLOCKED {phase} {kind}")
        telegram.notify(
            self.cfg.telegram_notify,
            msg,
            kind="integrate-hold",
            phase=phase,
            source="supervisor._hold",
        )

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
                kind="integrate-hold",
                phase=phase,
                source="supervisor._on_resolved",
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

        Three kinds of deadline, all timestamps: a park deadline a ``waiting``
        worker armed, whatever the operator queue next wants looking at — a
        backed-off item becoming eligible, or a lease running out — and a failed
        launch's retry back-off running out. The queue
        belongs here for exactly the reason ``run_after`` is a timestamp rather
        than a flag: a supervisor that only wakes on input sits straight past it,
        and with ``watchdog_s = 0`` there is no other wake at all. A deadline
        already in the past clamps to 0.0 so the next wake acts on it
        immediately."""
        stamps = list(state_mod.read(self.cfg).waiting.values())
        queued = opqueue.next_deadline(self.cfg)
        if queued is not None:
            stamps.append(queued)
        # A failed launch's back-off expiring. Only future ones: an expired one
        # is acted on by the wake that reaches it (``_retry_backed_off``), and a
        # past stamp here would clamp the timeout to 0 and spin the loop.
        now = time.time()
        with self._launch_lock:
            stamps.extend(
                last + LAUNCH_RETRY_S
                for p, (n, last) in self._launch_fails.items()
                if 0 < n < LAUNCH_GIVE_UP and p not in self._retried
                and last + LAUNCH_RETRY_S > now
            )
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
        * idle, unpaused, unblocked, with a free slot and ready phases -> run the
          launcher again, past a hung init master and a launch-retry backoff
          (only a phase given up on after :data:`LAUNCH_GIVE_UP` failures stays
          out — that one is the owner's);
        * settled with nothing left to do -> finish (rule 3): a since-reaped
          worker never sends the ``done`` whose handling would have noticed;
        * ``finished`` with phases still ready -> tell the owner they were dropped;
        * a repo owing a push -> retry it, at most every
          :data:`pushowed.TICK_RETRY_S` (the owner may have fixed its check or
          pushed by hand while nothing integrated to notice).

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
        if st.push_owed:
            pushowed.retry(self.cfg, self.log, min_gap=pushowed.TICK_RETRY_S)
        if idle < self.watchdog_s:
            return  # something moved recently -- leave a live swarm alone
        ready = [
            p
            for p in master_mod.build_context(self.cfg, st)["ready"]
            if not self._given_up(p)
        ]
        if not ready:
            self._check_operator_queue()  # opportunistic: a genuinely quiet swarm
            self._finish_if_settled(st, tag="WATCHDOG-FINISH")
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
        self._touch()  # the relaunch counts as movement; don't re-fire next sweep
        self._fill_slots(f"watchdog: idle {idle:.0f}s with ready phases", force=True)

    def _finish_if_settled(
        self, st: state_mod.State | None = None, tag: str = "FINISH"
    ) -> None:
        """Finish the run (rule 3) iff there is nothing left that could move it.

        Called after every event that could have been the last one — a ``done``,
        a launch settling, the init master idling, an operator session ending, a
        resume, a watchdog sweep. It holds while anything is still owed: a busy,
        waiting or parked phase, a launch in flight, an integration queued or
        held, a push origin does not have yet, an operator hand-off, a live
        master mid-pass — or a ready phase, because the launcher owns that one
        (a phase given up on after :data:`LAUNCH_GIVE_UP` failed launches does
        not hold it; the finish message names it instead)."""
        if st is None:
            st = state_mod.read(self.cfg)
        if st.finished or st.paused or st.pending():
            return
        if st.integ_queue or st.integ_blocked is not None or self.master.is_alive():
            return
        with self._launch_lock:
            inflight = sorted(self._launching)
        if inflight:
            return
        ctx = master_mod.build_context(self.cfg, st)
        if any(not self._given_up(p) for p in ctx["ready"]):
            return
        if st.push_owed:
            self.log.line(f"{tag}-HELD push-owed={sorted(st.push_owed)}")
            return
        owed = self._operator_blocking()
        if owed:
            self.log.line(f"{tag}-HELD operator={owed}")
            return
        self.log.line(f"{tag} settled")
        self._finish_run(ctx)

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
        keeping its now-replacement ``pane_id`` — and ``_fill_slots`` fills it with
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
            kind="park",
            phase=phase,
            source="supervisor._park",
        )
        if paused:
            self.log.line("PARK-PAUSED holding — no launch")
            return
        self._fill_slots(f"{phase} parked (slot {sid} free)")

    # -- rule 1 core: free the slot, launch what is ready -----------------
    def _advance_done(self, phase: str, status: str) -> None:
        with state_mod.transaction(self.cfg) as st:
            already = phase in st.done
            was_parked = phase in st.parked
            freed = st.free_slot_for(phase)
            if already and freed is None and not was_parked:
                # Duplicate `swarm done` for an already-completed phase (its slot
                # was already freed/reused, and it isn't parked). True no-op: DON'T
                # overwrite the recorded status and don't spuriously launch.
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
            # Paused: the slot is freed but we launch nothing and hold, so
            # in-flight workers drain without advancing.
            self.log.line("DONE-PAUSED holding — no launch")
            return
        self._fill_slots(f"worker {phase} done (slot {freed_id} free)")
        self._finish_if_settled()

    # -- the launcher: ledger order, no model in the loop -----------------
    def _given_up(self, phase: str) -> bool:
        with self._launch_lock:
            return self._launch_fails.get(phase, (0, 0.0))[0] >= LAUNCH_GIVE_UP

    def _backing_off(self, phase: str, now: float) -> bool:
        """Is ``phase`` waiting out :data:`LAUNCH_RETRY_S` after a failed launch?
        Caller holds ``_launch_lock``."""
        fails, last = self._launch_fails.get(phase, (0, 0.0))
        return fails > 0 and now - last < LAUNCH_RETRY_S

    def _fill_slots(self, reason: str, *, force: bool = False) -> list[str]:
        """Launch the ledger's ready phases into the free slots; return the picks.

        The same set ``swarm context`` reports as ``launchable`` — ``ready`` in
        ledger order, capped at the free slots — minus phases already launching
        (the double-launch guard) and phases backing off after a failed launch.
        Free slots still awaiting a launch thread's claim are counted as taken.
        ``force`` (the watchdog) skips the back-off and the bootstrap hold.

        Each pick runs :func:`launch.launch_outcome` on its own thread; this
        returns at once. A racing ``swarm launch`` by hand is harmless: whichever
        claims second is refused by :meth:`state.State.claim_slot`."""
        st = state_mod.read(self.cfg)
        if st.paused or st.finished:
            return []
        if self._bootstrapping and not force:
            if self.master.is_alive():
                self.log.line(f"LAUNCH-HELD bootstrap ({reason})")
                return []
            self._bootstrapping = False  # the init master died without idling
        ctx = master_mod.build_context(self.cfg, st)
        busy = set(ctx["busy_slots"].values())
        now = time.time()
        with self._launch_lock:
            budget = len(ctx["free_slots"]) - sum(
                1 for p in self._launching if p not in busy
            )
            picks: list[str] = []
            for phase in ctx["ready"]:
                if len(picks) >= budget:
                    break
                if phase in self._launching:
                    continue
                fails = self._launch_fails.get(phase, (0, 0.0))[0]
                if fails >= LAUNCH_GIVE_UP or (not force and self._backing_off(phase, now)):
                    continue
                picks.append(phase)
            self._launching.update(picks)
        if picks:
            self.log.line(f"LAUNCH-READY {' '.join(picks)} ({reason})")
        for phase in picks:
            self._start_launch(phase)
        return picks

    def _start_launch(self, phase: str) -> None:
        """Run one launch off the loop thread (daemon: never holds up shutdown;
        an interrupted launch leaves a claimed slot that ``swarm up`` rebuilds)."""
        threading.Thread(
            target=self._launch_worker, args=(phase,), name=f"launch:{phase}", daemon=True
        ).start()

    def _launch_worker(self, phase: str) -> None:
        """Thread body: launch, settle the guard, report through the FIFO."""
        cfg = self.cfg
        try:
            outcome = launch_mod.launch_outcome(cfg, phase, self.log, quiet=True)
        except Exception as exc:  # noqa: BLE001 - a thread must report, not vanish
            self.log.line(f"LAUNCH-ERROR {phase} {exc!r}")
            outcome = launch_mod.FAILED
            with state_mod.transaction(cfg) as st:
                st.free_slot_for(phase)  # don't strand the claim nothing will run
        with self._launch_lock:
            self._launching.discard(phase)
            if outcome == launch_mod.FAILED:
                fails = self._launch_fails.get(phase, (0, 0.0))[0] + 1
                self._launch_fails[phase] = (fails, time.time())
                self._retried.discard(phase)  # a fresh back-off to wait out
            elif outcome == launch_mod.LAUNCHED:
                self._launch_fails.pop(phase, None)
        launch_mod._poke_fifo(cfg, f"launched {phase} {outcome}\n")

    def _on_launched(self, phase: str, outcome: str) -> None:
        """A launch thread settled. Tell the owner once if the phase has now
        failed often enough to be given up on, then re-check the finish — a
        launch that failed may have been the last thing the run was waiting on.
        A failure deliberately does NOT refill the slot at once: it would hand
        the same broken cause the next phase. The back-off, the next event or
        the watchdog does."""
        with self._launch_lock:
            fails = self._launch_fails.get(phase, (0, 0.0))[0]
        self.log.line(f"EVENT launched {phase} {outcome} fails={fails}")
        if outcome == launch_mod.FAILED and fails == LAUNCH_GIVE_UP:
            self._ping(
                f"launch-gave-up:{phase}",
                f"swarm: {phase} failed to launch {fails} times in a row -- no longer"
                f" launched automatically. Fix the cause, then `swarm launch {phase}`"
                " (or `swarm resume` to retry every given-up phase).",
                cooldown=0.0,
            )
        self._finish_if_settled()

    def _retry_backed_off(self) -> None:
        """Relaunch once a failed launch's back-off has run out. Runs on every
        wake; :meth:`_next_timeout` makes sure there *is* a wake at that moment
        even with the watchdog off."""
        now = time.time()
        with self._launch_lock:
            due = [
                p
                for p, (n, last) in self._launch_fails.items()
                if 0 < n < LAUNCH_GIVE_UP and last + LAUNCH_RETRY_S <= now
                and p not in self._retried
            ]
            self._retried.update(due)
        if due:
            self._fill_slots(f"retry after a failed launch: {' '.join(due)}")

    # -- rule 2 + 3: master-idle, then launch / maybe finish --------------
    def _on_master_idle(self) -> None:
        """The init master finished its pass: kill it, then launch (the first
        batch waits for this) and re-check the finish."""
        self.master.kill()
        self._bootstrapping = False
        with state_mod.transaction(self.cfg) as st:
            st.master_alive = False
            paused = st.paused
            pending = st.pending()
            integrating = bool(st.integ_queue) or st.integ_blocked is not None
        self.log.line(
            f"EVENT master-idle pending={pending} integrating={integrating} paused={paused}"
        )
        if paused:
            # Held: do not launch or finish while paused — resume decides.
            self.log.line("MASTER-IDLE paused — holding")
            return
        self._fill_slots("init master idle")
        self._finish_if_settled()

    def _finish_run(self, ctx: dict) -> None:
        """Announce a settled run, counting only what actually built."""
        leftover = [p for p in ctx["ready"] if self._given_up(p)]
        if leftover:
            self.log.line(f"FINISH-WITH-READY leftover={leftover}")
        done = ctx["done"]
        built = sum(1 for s in done.values() if s in gitq.DONE_INTEGRATE)
        skipped = sum(1 for s in done.values() if s == "skip")
        failed = sorted(
            p for p, s in done.items() if s not in gitq.DONE_INTEGRATE and s != "skip"
        )
        self._finish(
            built,
            leftover,
            skipped=skipped,
            failed=failed,
            operator=operator_mod.outstanding(self.cfg),
        )

    def _spawn_master(self, kind: str) -> bool:
        with state_mod.transaction(self.cfg) as st:
            master_pane = st.master_pane
        if not self.master.spawn(kind, master_pane):
            # No master is running; do not claim one is alive (else the next
            # `done` would inject into nothing). Owner sees spawn-master-failed.
            return False
        with state_mod.transaction(self.cfg) as st:
            st.master_alive = True
        return True

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
            # Ready phases the launcher gave up on after repeated failed
            # launches: real work nothing will retry. Say how to resume.
            msg += (
                f"; {len(leftover)} ready but unlaunched (launch kept failing): "
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
        telegram.notify(
            self.cfg.telegram_notify, msg, kind="finish", source="supervisor._finish"
        )
        self.log.line("ACTION finish")
        self._stop = True


def main(cfg: Config) -> None:
    """Entry point used by the detached ``swarm up`` supervisor process."""
    Supervisor(cfg).run()
