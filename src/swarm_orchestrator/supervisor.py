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
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import caps
from . import backup as backup_mod
from . import bigpic as bigpic_mod
from . import blockedping
from . import doctor as doctor_mod
from . import drain as drain_mod
from . import gc as gc_mod
from . import gitq
from . import landing as landing_mod
from . import launch as launch_mod
from . import master as master_mod
from . import operator as operator_mod
from . import opqueue
from . import owner as owner_mod
from . import overseer as overseer_mod
from . import ovdigest
from . import ovrecord
from . import pauseat
from . import pushowed
from . import resolver as resolver_mod
from . import restart as restart_mod
from . import ledger as ledger_mod
from . import ledgerw
from . import state as state_mod
from . import reload as reload_mod
from . import runs as runs_mod
from . import session as session_mod
from . import telegram, tmux
from . import usage as usage_mod
from .config import Config, load
from . import logutil
from .resources import sampler as resources_mod
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
#: A worker whose pane dies this many times within :data:`CRASH_WINDOW_S` is not
#: started again automatically: something about the phase kills it, and each
#: restart costs a session. The owner is told once.
CRASH_LIMIT = 3
CRASH_WINDOW_S = 3600.0
#: The cheap doctor checks behind the Overseer's ``doctor`` trigger are probed at
#: most this often: they parse the ledger, and a FAIL that matters lasts minutes.
DOCTOR_PROBE_S = 600.0
#: While a hand-over waits for a safe point, and while a restart it started is
#: running, the loop wakes this often: what it waits for sends no event.
HANDOVER_POLL_S = 0.5
#: An automatic gc that found a build slot taken (or a compiler running) tries
#: again this much later — soon enough to catch the gap between two builds,
#: rarely enough that probing the gate costs nothing.
GC_RETRY_S = 600.0


class Supervisor:
    """Long-running owner of the FIFO, ``state.json``, the launcher and the master."""

    def __init__(self, cfg: Config, adopt: bool = False) -> None:
        self.cfg = cfg
        # ``swarm restart``: take over a run that is already going (its slots,
        # panes and waiting sessions, as ``state.json`` records them) instead of
        # starting one. See :meth:`_adopt`.
        self._adopting = adopt
        self._prior_cfg: Config | None = None  # what the last supervisor ran on
        # The restart this supervisor was asked to hand over to (its plan id),
        # from the ``handover`` verb until it stops; see :meth:`_handover_tick`.
        self._handover: str | None = None
        self._handover_said: list[str] | None = None
        self._handed_over = False
        # The detached ``swarm _restart-run`` a scheduled restart started.
        self._restart_proc: subprocess.Popen | None = None
        # Phases whose ``swarm done`` sentinel was found at adoption inside its
        # grace: looked at again once the grace is over (phase -> when).
        self._adopt_recheck: dict[str, float] = {}
        self.log = Log(cfg.supervisor_log, echo=bool(os.environ.get("SWARM_LOG_ECHO")),
                       max_bytes=logutil.ROTATE_BYTES)
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
        self._crashes: dict[str, list[float]] = {}  # phase -> recent reap times
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
        # Rows whose date was still ahead at the last look (:meth:`_release_dated`);
        # None until the first, which only takes the baseline.
        self._dated: set[str] | None = None
        # True while the init master's bootstrap pass runs: the worker command it
        # patches has to be committed before the first worktree branches off main.
        self._bootstrapping = False
        # The Overseer shares the master pane, and the init pass goes first: no
        # pass starts before `bootstrap` has been handled (a persisted pending
        # pass would otherwise take the pane on the very first wake and the init
        # pass would be ignored as "master alive").
        self._bootstrapped = False
        self._bootstrap_recorded_over = False  # see `_end_bootstrap`
        self.overseer = overseer_mod.Policy(cfg, self.log)
        # The periodic big-picture pass: its own window, trigger and landing.
        self.bigpic = bigpic_mod.Runner(cfg, self.log)
        # The live pass id, from the moment it is reserved (its spawn runs on a
        # thread) until it ends. Guards "one pass at a time" and holds the finish.
        self._overseer_live: str | None = None
        # True while the live pass's session is still being started on its thread.
        self._overseer_spawning = False
        self._overseer_reasons: list[overseer_mod.Reason] = []
        # Passes in a row that would not start or ran past their timeout. One is
        # not the owner's problem (its reasons wait for the next pass); a streak is.
        self._overseer_bad = 0
        self._doctor_probed = 0.0
        # Automatic gc (see `_gc_tick`). Its clock resumes from the last recorded
        # run; a run that never had one anchors at start-up, so a fresh `swarm up`
        # does not open with a full sweep while its first workers are booting —
        # the idle trigger reaches an idle swarm soon enough.
        self._gc_thread: threading.Thread | None = None
        record = gc_mod.read_record(cfg) or {}
        self._gc_last = float(record.get("ts") or time.time())
        self._gc_retry_at = 0.0
        # Idle episodes: counted up each time the slots all go quiet, so the run
        # a thread finishes is credited to the episode it *started* in, never to
        # a later one it raced.
        self._gc_idle_since: float | None = None
        self._gc_episode = 0
        self._gc_episode_done = -1
        # Usage caps (see `_usage_tick`): the last check, and the tap's samples
        # read incrementally, since `limits.jsonl` is never rotated.
        self._usage_last = 0.0
        # The login the last check ran under: a switch is checked at once.
        self._usage_login: str | None = None
        self._usage_tail = usage_mod.SampleTail(
            cfg.state_dir / usage_mod.METERS_DIR / usage_mod.LIMITS_LOG)
        # Periodic backup pushes (see `_backup_tick`), first one a full interval
        # after start-up: `swarm down` pushes too, so nothing waits on this one.
        self._backup_thread: threading.Thread | None = None
        self._backup_last = time.time()
        # The resource sampler (see resources/sampler.py): its own thread, started
        # with the loop and stopped with it. It only reads /proc and writes meters/.
        self._resources: resources_mod.Sampler | None = None

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
        if self._adopting:
            self._drop_stale_orders()
        with state_mod.transaction(self.cfg) as st:
            st.supervisor_pid = os.getpid()
            st.last_event_at = time.time()
        # Taking a run over: what the last supervisor ran on, read before the
        # snapshot below replaces it (see `_adopt_config`).
        self._prior_cfg = reload_mod.snapshot_cfg(self.cfg) if self._adopting else None
        # Record the config actually in force, so `swarm reload` has an honest
        # "before" to diff against once the file on disk has been edited.
        self._write_config_snapshot()
        self.log.line(
            f"SUPERVISOR-START pid={os.getpid()} driver={self.cfg.driver}"
            f" watchdog_s={self.watchdog_s:g}" + (" adopting" if self._adopting else "")
        )
        # The FIFO is open: a restart waiting on this lets go of its bridge now.
        restart_mod.mark_supervisor(self.cfg, self._adopting)
        if self._adopting:
            self._dispatch("adopt", self._adopt)
        else:
            # No pass survives a `swarm up`, bar one a restart carried across
            # parked on the owner (`restart.carry_in` put it back in `parked`).
            ovrecord.mark_stale(self.cfg, live=state_mod.read(self.cfg).live_passes())
            self.bigpic.recover()
        self._start_resources()
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
                # through the guard below). A due scheduled pause goes first, so
                # nothing this wake does can launch past it.
                self._dispatch("scheduled-pause", self._pause_at_tick)
                self._dispatch("restart", self._restart_tick)
                self._dispatch("park-deadlines", self._check_park_deadlines)
                self._dispatch("operator-queue", self._check_operator_queue)
                self._dispatch("watchdog", self._watchdog_tick)
                self._dispatch("launch-retry", self._retry_backed_off)
                self._dispatch("overseer", self._overseer_tick)
                if self._handover is None:
                    # Nothing new starts while handing over: each of these can
                    # open a session or a thread the exit would cut off.
                    self._dispatch("big-picture", self.bigpic.tick)
                    self._dispatch("gc", self._gc_tick)
                    self._dispatch("backup", self._backup_tick)
                self._dispatch("blocked-pings", blockedping.flush, self.cfg, self.log)
                self._dispatch("usage", self._usage_tick)
                self._dispatch("adopt-recheck", self._adopt_recheck_tick)
                self._dispatch("drain", self._drain_tick)
                self._dispatch("handover", self._handover_tick)
                if self._stop or self._fifo_fd not in ready:
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
                    # After every event too: the event may be the trigger (a
                    # `fail`, a hold), and waiting for the next wake to notice
                    # it could be waiting for nothing.
                    self._dispatch("overseer", self._overseer_tick)
                    self._dispatch("drain", self._drain_tick)
                    self._dispatch("handover", self._handover_tick)
                    if self._stop:
                        break
            if self._handed_over and buf:
                # Events read and not yet handled go back into the pipe, which
                # the restart holds open, for the next supervisor to read.
                try:
                    os.write(self._fifo_fd, buf)
                except OSError as exc:
                    self.log.line(f"HANDOVER-UNREAD-LOST {len(buf)} bytes {exc}")
        except Exception as exc:  # noqa: BLE001 - announce, then re-raise
            self.log.line(f"SUPERVISOR-CRASH {exc!r}")
            self._ping(
                "crash",
                f"swarm: {self.cfg.slug} has stopped: the swarm hit an internal error"
                f" ({exc}). No new work starts and finished work is not merged until"
                " you restart it with `swarm up`.",
                cooldown=0.0,
            )
            raise
        finally:
            # A hand-over leaves every session as it is: the next supervisor
            # adopts the pass in the master pane, the operator's lease and a
            # big-picture pass from the state they are recorded in.
            if not self._handed_over:
                self._end_sessions()
            if self._resources is not None:
                self._resources.stop()
            # A finished session's processes are ended off the loop thread; the
            # run's last `done` is often what finished it, so let those land.
            session_mod.join_reaps()
            if self._fifo_fd >= 0:
                os.close(self._fifo_fd)
            self.log.line("SUPERVISOR-STOP" + (" handover" if self._handed_over else ""))
            self.log.close()

    def _drop_stale_orders(self) -> None:
        """Take out of the pipe what was addressed to the previous supervisor.

        A restart holds the FIFO open across the gap, so everything sent
        meanwhile is still in it — which is the point for a ``done`` or a
        ``waiting``, and wrong for a ``shutdown`` or ``handover`` the last
        supervisor died before reading: obeyed here, it would stop the one
        process the restart was for. The rest goes back in, in order."""
        data = b""
        while True:
            try:
                chunk = os.read(self._fifo_fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            data += chunk
        if not data:
            return
        keep: list[bytes] = []
        for raw in data.splitlines(keepends=True):
            verb = raw.split()[0].decode("utf-8", "replace") if raw.split() else ""
            if verb in ("shutdown", "handover", "handover-cancel"):
                self.log.line(f"STALE-ORDER-DROPPED {raw.decode('utf-8', 'replace').strip()}")
            else:
                keep.append(raw)
        if keep:
            os.write(self._fifo_fd, b"".join(keep))

    def _end_sessions(self) -> None:
        """On the way out for good: end the sessions only this process owns."""
        if self._overseer_live is not None:
            ovrecord.update(
                self.cfg, self._overseer_live,
                status=ovrecord.INTERRUPTED, ended_at=time.time(),
            )
        if self.master.is_alive():
            self.master.kill()  # never orphan a master on the way out
            with state_mod.transaction(self.cfg) as st:
                st.master_alive = False
                st.overseer_pass = None
                st.overseer_deadline = 0.0
        # Same obligation, and it matters more: an operator session holds the
        # owner's full authority on the host, so leaving one typing into a
        # window nothing owns is worse than leaving a master.
        operator_mod.release(self.cfg, self.log)
        self.bigpic.shutdown()

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
                f"swarm: an internal error while handling {what!r} ({exc}). The swarm"
                " skipped that step and keeps running; nothing to do unless it repeats.",
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
        suppressed: str | None = None,
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
        telegram.notify(self.cfg.telegram_notify, msg, kind=kind, source=source,
                        suppressed=suppressed)

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
        elif verb == "lane-checked":
            # `swarm _lane-check`: the result is on disk; the poke is the wake.
            self.log.line(f"EVENT {line}")
            self._pump_integrations()
        elif verb == "resolver-escalated":
            self._on_resolver_escalated(parts[1] if len(parts) > 1 else "?")
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
        elif verb == "drain":
            self._on_drain()
        elif verb == "handover":
            self._on_handover(parts[1] if len(parts) > 1 else "?")
        elif verb == "handover-cancel":
            self._on_handover_cancel(parts[1] if len(parts) > 1 else "?")
        elif verb == "restart-scheduled":
            # `swarm restart --at/--in/--cancel`: only a wake, like a scheduled
            # pause; the plan is in restart.json and the next wake reads it.
            self.log.line("EVENT restart-scheduled")
        elif verb == "pause-scheduled":
            # `swarm pause --in/--at/--cancel`: the poke is only a wake, so the
            # next `select` timeout counts the new moment in (or drops it).
            self.log.line("EVENT pause-scheduled")
        elif verb == "reload":
            self._on_reload()
        elif verb == "operator":
            self._on_operator(parts[1] if len(parts) > 1 else "?")
        elif verb == "operator-done":
            self._on_operator_done(parts[1] if len(parts) > 1 else "?")
        elif verb == "operator-queued":
            # `swarm operator-add`: the poke is only a wake — the sweep every
            # wake runs is what dispatches, oldest first, so a new job can never
            # jump the queue by being the one that was poked.
            self.log.line(f"EVENT operator-queued {parts[1] if len(parts) > 1 else '?'}")
        elif verb == "overseer-done":
            self._end_overseer_pass(parts[1] if len(parts) > 1 else "?", ovrecord.DONE)
        elif verb == "overseer-spawned":
            self._on_overseer_spawned(
                parts[1] if len(parts) > 1 else "?",
                parts[2] if len(parts) > 2 else "failed",
            )
        elif verb == "ledger":
            # `swarm record` / a follow-up or lesson filed outside its phase.
            if self._flush_ledger({}):
                self._fill_slots("ledger updated")
        elif verb == "overseer-now":
            self.overseer.request(
                overseer_mod.MANUAL, "requested by `swarm overseer --now`", urgent=True
            )
        elif verb.startswith("big-picture-"):
            self.bigpic.handle(verb, parts[1:])
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
        self._bootstrapped = True
        # Reports queued before the last stop: their phases may have landed.
        self._flush_ledger(dict(state_mod.read(self.cfg).done))
        if self.master.is_alive():
            self.log.line("BOOTSTRAP-IGNORED master-alive")
            self._end_bootstrap()
            return
        if self._spawn_master("init"):
            self._bootstrapping = True
            return
        self.log.line("BOOTSTRAP-NO-MASTER launching without the init pass")
        self._end_bootstrap()
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

    def _on_reload(self, fill: bool = True) -> None:
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
                f"swarm: your settings change for {self.cfg.slug} was not applied: the"
                f" config file has an error ({exc}). The swarm keeps running on the old"
                " settings. Fix the file, then run `swarm reload`.",
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
        self.overseer.cfg = applied
        self.bigpic.cfg = applied
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
        # The run's averages are split at every worker-count/isolation move, so
        # a measurement at one worker is never silently blended with four.
        try:
            runs_mod.note_config(self.cfg.state_dir, applied.max_workers, applied.git_isolation)
        except OSError as exc:
            self.log.line(f"RUN-NOTE-FAILED {exc}")
        self._write_config_snapshot()
        # At once, not at the next check: a raised limit or `enabled = false`
        # should lift its hold now, and a lowered one act now.
        if self._usage_check(time.time()):
            return
        st = state_mod.read(self.cfg)
        if fill and not st.on_hold and not st.finished:
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
                f"swarm: {self.cfg.slug} could open only {len(panes)} of the"
                f" {len(slot_ids)} extra worker place(s) you asked for, so it runs with"
                " fewer workers than set. Nothing is lost.",
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

    # -- drain: stop once the running work is finished ---------------------
    def _on_drain(self) -> None:
        """``swarm down --drain`` set or updated the drain, or ``--cancel`` /
        ``swarm resume`` dropped it: look now, and refill slots if it is gone."""
        st = state_mod.read(self.cfg)
        self.log.line(f"EVENT drain {'on' if st.drain else 'off'}")
        if st.drain:
            self._drain_tick()
            return
        self._fill_slots("drain cancelled")
        self._finish_if_settled()

    def _drain_tick(self) -> None:
        """Record what a drain still waits for; once nothing, stop the swarm.

        Runs on every wake and after every event, the same moments anything it
        waits for can end. The stop itself is ``swarm _drain-down``, started
        detached: ``swarm down`` stops this process, so it cannot run in it."""
        st = state_mod.read(self.cfg)
        if not st.drain or st.drain.get("stopping_at"):
            return
        busy = {s.phase for s in st.busy_slots()}
        with self._launch_lock:
            launching = len(self._launching - busy)
        waits = drain_mod.waiting_for(
            self.cfg, st, launching=launching,
            # A pass waiting on the owner holds nothing up, like a waiting worker.
            overseer=(self._overseer_live is not None
                      and state_mod.waiter_key(state_mod.OVERSEER, self._overseer_live)
                      not in st.waiting),
            init_pass=self._bootstrapping and self.master.is_alive(),
        )
        if waits == st.drain.get("waiting") and waits:
            return
        if not waits and st.drain.get("questions") == restart_mod.KEEP:
            # A restart that carries the questions across: each waiting session
            # gets its own window now, so it can be moved out whole before the
            # tmux session is torn down.
            self._park_all_waiting(st)
        with state_mod.transaction(self.cfg) as s2:
            if not s2.drain or s2.drain.get("stopping_at"):
                return  # cancelled, or already stopping, since the read above
            s2.drain["waiting"] = waits
            if not waits:
                s2.drain["stopping_at"] = time.time()
            then = s2.drain.get("then") or ""
            restarting = bool(s2.drain.get("restart"))
        if waits:
            self.log.line(f"DRAIN-WAITING {', '.join(waits)}")
            return
        started = drain_mod.spawn_down(self.cfg)
        self.log.line(f"DRAIN-COMPLETE stopping={started} then={then!r} restart={restarting}")
        if started and restarting:
            msg = (f"swarm: {self.cfg.slug} finished the work that was running and is"
                   " restarting, as asked; you hear again only if it does not come back")
        elif started:
            msg = (f"swarm: {self.cfg.slug} finished the work that was running and is"
                   " shutting down, as you asked")
            msg += f"; afterwards it runs: {then}" if then else ""
        else:
            msg = (f"swarm: {self.cfg.slug} finished the work that was running but could"
                   " not shut itself down; run `swarm down` yourself")
        self._ping("drain", msg, cooldown=0.0, kind="drain", source="supervisor._drain_tick",
                   suppressed=(telegram.hold(self.cfg, "a restart: you hear if it fails")
                               if started and restarting else None))

    def _park_all_waiting(self, st: state_mod.State) -> None:
        """Park every session that waits on the owner and is still in its home
        pane: a worker in its slot, a job in the operator window, a pass in the
        master pane."""
        keys = list(st.waiting)
        job = st.operator_phase
        if job and drain_mod._operator_asking(self.cfg, job):
            key = state_mod.waiter_key(state_mod.OPERATOR, job)
            if key not in keys:
                keys.append(key)  # parking is off: it never armed a deadline
        for key in keys:
            self._dispatch(f"park {key}", self._park, key)

    # -- restart: fire a scheduled one, hand over, adopt --------------------
    def _restart_tick(self) -> None:
        """Start a scheduled restart once it is due, and notice one that died.

        The restart itself is ``swarm _restart-run``, detached: it replaces this
        process, and it runs the code on disk, which is the point. A helper that
        exits non-zero without settling the plan never got as far as running (the
        new code does not import, say), so this process tells the owner."""
        proc = self._restart_proc
        if proc is not None and proc.poll() is not None:
            self._restart_proc = None
            plan = restart_mod.load(self.cfg)
            if proc.returncode != 0 and restart_mod.active(plan):
                restart_mod.fail(
                    self.cfg, plan,
                    f"the restart helper exited with status {proc.returncode}"
                    f" ({self.cfg.log_dir / restart_mod.RUN_LOG} says why)", self.log)
        plan = restart_mod.load(self.cfg)
        if plan.get("stage") != restart_mod.PLANNED or plan.get("timer") != "supervisor":
            return
        now = time.time()
        due = float(plan.get("at") or 0.0)
        if plan.get("fired_at") or due > now:
            return
        if restart_mod.update(self.cfg, plan["id"], fired_at=now) is None:
            return  # replaced since the read above
        self.log.line(
            f"RESTART-DUE id={plan['id']} mode={plan.get('mode')} by={plan.get('by')!r}"
            f" due={pauseat.stamp(due)} late={now - due:.0f}s")
        self._restart_proc = restart_mod.spawn_runner(self.cfg, plan["id"])
        if self._restart_proc is None:
            restart_mod.fail(self.cfg, plan, "the restart helper could not be started", self.log)

    def _on_handover(self, plan_id: str) -> None:
        """``swarm restart``: stop at the next safe point and leave every
        session running for the supervisor that takes over."""
        plan = restart_mod.load(self.cfg)
        self.log.line(f"EVENT handover {plan_id} by={plan.get('by', '?')!r}")
        self._handover = plan_id
        self._handover_said = None
        self._handover_tick()

    def _on_handover_cancel(self, plan_id: str) -> None:
        if self._handover != plan_id:
            return
        self._handover = None
        self.log.line(f"EVENT handover-cancel {plan_id}")
        self._fill_slots("restart cancelled")
        self._finish_if_settled()

    def _handover_waits(self) -> list[str]:
        """What this process is in the middle of that its exit would cut off.

        A merge is never on the list: it runs inside one event, and this is
        only ever asked between events. Sessions are not on it either — they
        are adopted — except on the bare driver, whose master is this process's
        child and cannot be."""
        out: list[str] = []
        with self._launch_lock:
            launching = len(self._launching)
        if launching:
            out.append(f"{launching} worker{'' if launching == 1 else 's'} starting")
        elif any(t.name.startswith("launch:") and t.is_alive() for t in threading.enumerate()):
            out.append("a worker starting")  # settled, and still reporting it
        if self._overseer_live is not None and self._overseer_spawning:
            out.append("an Overseer pass starting")
        elif self.cfg.driver == "bare" and self.master.is_alive():
            out.append("an Overseer pass" if self._overseer_live else "the start-up pass")
        if self.bigpic.mem.live and not self.bigpic._spawned:
            out.append("the big-picture pass starting")
        if self._gc_running():
            out.append("a clean-up")
        if self._backup_running():
            out.append("a backup push")
        return out

    def _handover_tick(self) -> None:
        """Hand over once nothing is mid-way; until then, say what is."""
        if self._handover is None or self._stop:
            return
        waits = self._handover_waits()
        if waits:
            if waits != self._handover_said:
                self._handover_said = waits
                self.log.line(f"HANDOVER-WAITING {', '.join(waits)}")
                restart_mod.update(self.cfg, self._handover, waiting=waits)
            return
        with self._launch_lock:
            fails = {p: list(v) for p, v in self._launch_fails.items()}
        restart_mod.write_handover(self.cfg, {
            "id": self._handover,
            "launch_fails": fails,
            "crashes": self._crashes,
            "pinged": self._pinged,
            "overseer_bad": self._overseer_bad,
            "backup_last": self._backup_last,
            "bootstrapped": self._bootstrapped,
        })
        self.log.line(f"HANDOVER {self._handover} sessions left running for the next supervisor")
        self._handed_over = True
        self._stop = True

    def _adopt(self) -> None:
        """Take over a run that is already going, from ``state.json`` and the
        panes as they are. Nothing is started that already runs, and nothing
        that runs is ended.

        What the last supervisor held only in memory comes from its hand-over
        note when it left one (launch back-offs, crash counts, ping cooldowns);
        every timer that matters is a timestamp in the state already (park
        deadlines, a scheduled pause, the Overseer's deadline, usage holds).
        The rest is made good here: slots whose pane id the state lost, a pass
        in the master pane, a claim whose launch was cut off, and a ``swarm
        done`` whose poke nobody read (the sentinel is the record)."""
        cfg = self.cfg
        note = restart_mod.take_handover(cfg)
        with self._launch_lock:
            self._launch_fails = {p: (int(v[0]), float(v[1]))
                                  for p, v in (note.get("launch_fails") or {}).items()}
        self._crashes = {p: [float(t) for t in ts]
                         for p, ts in (note.get("crashes") or {}).items()}
        self._pinged = {k: float(v) for k, v in (note.get("pinged") or {}).items()}
        self._overseer_bad = int(note.get("overseer_bad") or 0)
        if note.get("backup_last"):
            self._backup_last = float(note["backup_last"])
        self._bootstrapped = True  # `swarm up` poked it for the last supervisor
        st = state_mod.read(cfg)
        self.log.line(
            f"ADOPT busy={sorted(s.phase for s in st.busy_slots() if s.phase)}"
            f" waiting={sorted(st.waiting)} parked={st.parked} queue={st.integ_queue}"
            f" blocked={st.integ_blocked} note={'yes' if note else 'no'}")
        self._adopt_panes(st)
        self._adopt_config()
        self._adopt_master(st)
        self._adopt_dead_claims()
        self._adopt_reports()
        st = state_mod.read(cfg)
        self._flush_ledger(dict(st.done))
        self._pump_integrations()  # a queue the last supervisor left standing
        self._check_operator_queue()
        if not state_mod.read(cfg).on_hold:
            self._fill_slots("supervisor restarted")
        self._finish_if_settled()

    def _adopt_config(self) -> None:
        """Move from the last supervisor's settings to the file's the way
        ``swarm reload`` does, because the run is the same run: what is safe
        mid-run is applied (a changed worker count adds or retires slots and
        their panes), and what names the run itself (state dir, tmux session,
        driver, isolation) stays as it was until a ``swarm restart --full``."""
        prior = self._prior_cfg
        if prior is None:
            return  # no snapshot: the last supervisor predates it
        self.cfg = self.master.cfg = self.overseer.cfg = self.bigpic.cfg = prior
        self.watchdog_s = max(0.0, float(getattr(prior, "watchdog_s", 300) or 0))
        self._on_reload(fill=False)
        self._write_config_snapshot()

    def _adopt_panes(self, st: state_mod.State) -> None:
        """Give back to a slot the pane tmux tags as its own when the state names
        one that is gone (a park moved the worker out and the write was lost)."""
        if self.cfg.driver != "tmux":
            return
        out = tmux.run(["list-panes", "-s", "-t", f"={self.cfg.session}", "-F",
                        f"#{{pane_id}}\t#{{{tmux.SLOT_OPT}}}\t#{{window_name}}"])
        if out.returncode != 0:
            self.log.line("ADOPT-TMUX-UNREACHABLE the panes are taken as recorded")
            return
        live: set[str] = set()
        tagged: dict[int, str] = {}
        for row in out.stdout.splitlines():
            pane, _, rest = row.partition("\t")
            tag, _, window = rest.partition("\t")
            live.add(pane)
            if tag.isdigit() and window.startswith("workers"):
                tagged[int(tag)] = pane
        fixed: list[str] = []
        with state_mod.transaction(self.cfg) as s2:
            for slot in s2.slots:
                pane = tagged.get(slot.id)
                if pane and slot.pane_id not in live and pane != slot.pane_id:
                    fixed.append(f"slot {slot.id}: {slot.pane_id} -> {pane}")
                    slot.pane_id = pane
        for change in fixed:
            self.log.line(f"ADOPT-PANE {change}")

    def _adopt_master(self, st: state_mod.State) -> None:
        """Pick up the pass in the master pane: an init pass still holds the
        first launch, an Overseer pass is the live one again. A pass the state
        names that is no longer running is settled as interrupted."""
        cfg = self.cfg
        running = False
        if cfg.driver == "tmux" and st.master_alive and st.master_pane:
            self.master.pane = st.master_pane
            running = self.master.is_alive()
            if not running:
                self.master.pane = None
        if running and st.overseer_pass:
            self._overseer_live = st.overseer_pass
            self.log.line(f"ADOPT-OVERSEER {st.overseer_pass}")
        elif running:
            self._bootstrapping = bool(st.bootstrapping)
            self.log.line(f"ADOPT-MASTER init bootstrapping={self._bootstrapping}")
        else:
            with state_mod.transaction(cfg) as s2:
                lost = s2.overseer_pass
                s2.master_alive = False
                s2.overseer_pass = None
                s2.overseer_deadline = 0.0
            if lost:
                self.log.line(f"ADOPT-OVERSEER-GONE {lost}")
        if not self._bootstrapping:
            self._end_bootstrap()
        ovrecord.mark_stale(cfg, live=state_mod.read(cfg).live_passes())
        if cfg.driver == "tmux" and self.bigpic.mem.live and self.bigpic._alive():
            self.bigpic._spawned = True  # its window is still up: carry on with it
        else:
            self.bigpic.recover()

    def _adopt_dead_claims(self) -> None:
        """Free a slot that is claimed with no worker in it: a launch the last
        supervisor's exit cut off after the claim. The pane holds its idle
        command, so the watchdog (which looks for dead panes) would never see it."""
        cfg = self.cfg
        if cfg.driver != "tmux":
            return
        st = state_mod.read(cfg)
        sentinels = gitq.sentinel_done(cfg)
        for slot in st.busy_slots():
            phase = slot.phase
            if not phase or phase in st.integrating() or phase in sentinels:
                continue
            markers = session_mod.session_markers(cfg, "worker", phase)
            if session_mod.session_processes(cfg, markers=markers):
                continue
            probe = tmux.run(["display-message", "-p", "-t", slot.pane_id or "",
                              "#{pane_dead} #{pane_current_command}"])
            dead, _, command = probe.stdout.strip().partition(" ")
            if probe.returncode == 0 and dead != "1" and command != "sleep":
                # Something runs there that is not the idle holder: not ours to judge.
                self.log.line(f"ADOPT-UNSURE {phase} slot={slot.id} runs {command!r}; left alone")
                continue
            self.log.line(f"ADOPT-DEAD-CLAIM {phase} slot={slot.id}: no worker in its pane")
            self._reap(phase)

    def _claim_times(self) -> dict[str, float]:
        """When each phase was last claimed, from the log (``CLAIM <phase>``)."""
        out: dict[str, float] = {}
        for raw in logutil.read_all(self.cfg.supervisor_log, keep=1).splitlines():
            if " CLAIM " not in raw:
                continue
            ts, msg = logutil.parse_ts(raw)
            parts = msg.split()
            if ts is not None and len(parts) >= 2 and parts[0] == "CLAIM":
                out[parts[1]] = ts
        return out

    def _unread_report(self, st: state_mod.State, phase: str, sentinels: dict[str, str],
                       claimed: dict[str, float]) -> tuple[str, float] | None:
        """``(status, when written)`` of a ``swarm done`` this run never handled
        for ``phase``, or ``None``. Only a sentinel written since the phase was
        last claimed counts: one left by an earlier attempt says nothing about
        the worker running now, and acting on a stale ``fail`` would roll back
        the branch under it."""
        status = sentinels.get(phase)
        if status is None or phase in st.done or phase in st.integrating():
            return None
        try:
            written = (self.cfg.done_dir / f"{phase}.{status}").stat().st_mtime
        except OSError:
            return None
        since = claimed.get(phase)
        if since is None or written < since - 1.0:
            self.log.line(f"ADOPT-OLD-SENTINEL {phase} {status}: not from this attempt; left")
            return None
        return status, written

    def _adopt_reports(self) -> None:
        """Act on a ``swarm done`` nobody read: the sentinel is written before
        the poke, so a phase still in flight with one has finished. One written
        within ``done_grace_s`` is looked at again once the grace has run."""
        cfg = self.cfg
        st = state_mod.read(cfg)
        sentinels = gitq.sentinel_done(cfg)
        flying = [p for p in st.claimed_phases() if p in sentinels]
        if not flying:
            return
        claimed = self._claim_times()
        now = time.time()
        for phase in flying:
            found = self._unread_report(st, phase, sentinels, claimed)
            if found is None:
                continue
            status, written = found
            due = written + cfg.done_grace_s + (2.0 if cfg.done_grace_s else 0.0)
            if due > now:
                self._adopt_recheck[phase] = due
                continue
            self.log.line(f"ADOPT-DONE {phase} {status}: its report was never read")
            self._dispatch(f"done {phase} {status}", self._on_done, phase, status)

    def _adopt_recheck_tick(self) -> None:
        if not self._adopt_recheck:
            return
        now = time.time()
        due = [p for p, at in self._adopt_recheck.items() if at <= now]
        if not due:
            return
        for phase in due:
            self._adopt_recheck.pop(phase, None)
        st = state_mod.read(self.cfg)
        sentinels = gitq.sentinel_done(self.cfg)
        claimed = self._claim_times()
        for phase in due:
            # Its own delayed poke may have arrived meanwhile: then it is done,
            # or merging, and there is nothing left to act on.
            found = self._unread_report(st, phase, sentinels, claimed) \
                if st.in_flight(phase) else None
            if found is not None:
                self.log.line(f"ADOPT-DONE {phase} {found[0]}: its report was never read")
                self._dispatch(f"done {phase} {found[0]}", self._on_done, phase, found[0])

    # -- rule 1: done -----------------------------------------------------
    def _on_done(self, phase: str, status: str) -> None:
        """Integrate first (worktree mode), then run the pure-injection advance.

        Non-worktree runs go straight to :meth:`_advance_done`. Worktree
        integrating runs (``ok`` and ``needs-owner`` — see
        :data:`gitq.DONE_INTEGRATE`) enqueue the phase and pump the merge-queue;
        ``needs-owner`` lands identically to ``ok`` here (its owner ping already
        fired worker-side). A ``fail`` drops the phase's branch(es) and advances
        (a ``later`` with a date has its work kept for that date instead,
        :func:`gitq.keep_later`) —
        UNLESS the phase is already integrating (blocked or queued after having
        reported success): a late, contradictory ``fail`` must not yank a branch
        out from under a live merge or free a parked slot, so it is ignored.

        Whatever the status, the worker's session is over: :meth:`_end_worker`
        ends it and everything it started, before the slot can be refilled.

        Ignored for a malformed id and for a phase with no worker, merge or
        record: ending a session and removing a worktree by that name would act
        on something no worker reported.
        """
        with state_mod.transaction(self.cfg) as st:
            known = (
                ledger_mod.safe_id(phase)
                and (st.in_flight(phase) or phase in st.done or phase in st.integrating())
            )
        if not known:
            self.log.line(f"DONE-REFUSED {phase!r} {status} not a phase in flight")
            return
        self._end_worker(phase)
        if self.cfg.git_isolation != "worktree":
            self._advance_done(phase, status)
            return
        if status not in gitq.DONE_INTEGRATE:
            with state_mod.transaction(self.cfg) as st:
                integrating = st.integ_blocked == phase or phase in st.integ_queue
            if integrating:
                self.log.line(f"DONE-FAIL-IGNORED {phase} integrating")
                return
            if ledgerw.later_date(self.cfg, phase):
                # It waits for a date: its work is in the tree again that day.
                gitq.keep_later(self.cfg, phase, self.log)
            else:
                gitq.discard(self.cfg, phase, self.log)  # failed build: roll back branches
            self._advance_done(phase, status)
            return
        with state_mod.transaction(self.cfg) as st:
            already = phase in st.done
            has_slot = any(s.busy and s.phase == phase for s in st.slots)
            if not (already and not has_slot):
                st.integ_push(phase, status)
        self._pump_integrations()

    def _end_worker(self, phase: str) -> None:
        """End a finished worker's session and every process it started.

        A finished worker leaves no shells lying around: when the worker is done, all of its
        shells end too. The slot's pane goes back to
        ``sleep`` now — before :meth:`_advance_done` can launch the next phase into
        it, and so the watchdog never mistakes a slot still merging for a dead
        worker — and :func:`session.reap_session` then ends whatever still carries
        the phase's markers, a ``setsid``/``nohup`` child that left the pane
        included. It waits :data:`session.REAP_GRACE_S` first, so the worker's own
        ``swarm done`` can print its result. ``swarm keep`` is the exception.
        """
        if self.cfg.driver == "tmux":
            st = state_mod.read(self.cfg)
            pane = next((s.pane_id for s in st.slots
                         if s.busy and s.phase == phase and s.pane_id), None)
            if pane is not None:
                try:
                    tmux.respawn_pane(pane, "exec sleep infinity")
                except (subprocess.CalledProcessError, OSError) as exc:
                    self.log.line(f"END-WORKER-RESPAWN-FAIL {phase} {exc}")
        session_mod.reap_session(self.cfg, "worker", phase, self.log)

    def _flush_ledger(self, finished: dict[str, str]) -> bool:
        """Apply the sessions' queued ledger reports on the target branch.

        ``finished`` is ``{phase: status}`` for the phases whose reports are
        due (see :func:`ledgerw.flush`). True when a row was ticked or added,
        which can make new work ready. Never raises: a report that cannot be
        written stays queued and the watchdog tries it again.
        """
        try:
            return ledgerw.flush(self.cfg, self.log, finished).released
        except Exception as exc:  # noqa: BLE001 - the sole FIFO reader must survive
            self.log.line(f"LEDGER-ERROR {exc!r}")
            return False

    def _release_dated(self) -> bool:
        """Keep the ``later`` rows on their dates: drop the failure record of
        each one whose row carries its date (:func:`ledgerw.release_dated`), and
        run the launcher when a date that was ahead at the last look has come.
        True when the done map or the ready set changed. Never raises."""
        try:
            released = ledgerw.release_dated(self.cfg, self.log)
            ahead = set(ledgerw.dated(self.cfg))
        except Exception as exc:  # noqa: BLE001 - the sole FIFO reader must survive
            self.log.line(f"LATER-ERROR {exc!r}")
            return False
        was, self._dated = self._dated, ahead
        come = sorted((was or set()) - ahead)
        if come:
            ready = set(master_mod.build_context(self.cfg, state_mod.read(self.cfg))["ready"])
            come = [p for p in come if p in ready]  # not a row closed meanwhile
        for phase in come:
            self.log.line(f"LATER-DUE {phase} its date has come")
        if come or any(p not in ahead for p in released):
            self._touch()
            self._fill_slots("a `later` phase's date has come")
        return bool(come or released)

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
        integration for the repos this one did not already push.

        With lanes on, a phase whose landing check runs, or whose repo another
        phase's landing holds, is skipped for this pass (lanes): the next
        one lands meanwhile, and the check's ``lane-checked`` poke pumps again.
        With lanes off nothing is ever skipped, so this is head-first as ever."""
        skipped: set[str] = set()
        while True:
            with state_mod.transaction(self.cfg) as st:
                if st.integ_blocked is not None or not st.integ_queue:
                    return
                todo = [p for p in st.integ_queue if p not in skipped]
                if not todo:
                    return
                phase = todo[0]
                status = st.integ_status.get(phase, "ok")
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
            if result in gitq.LANE_PENDING:
                skipped.add(phase)
                continue
            if result == gitq.MERGED:
                self._dequeue(phase)
                skipped.clear()  # a phase waiting on this one's landing may go now
                if status == operator_mod.INTEG_STATUS:
                    # An operator job's mirror, not a ledger phase: its work is
                    # landed, and there is nothing to record done or free.
                    # A ticked row can release its dependents.
                    self.log.line(f"OPERATOR-INTEGRATED {phase}")
                    self._fill_slots(f"operator job landed ({phase})")
                elif status == ovrecord.INTEG_STATUS:
                    # An Overseer pass's mirror: its ledger edits are on main now,
                    # so the launcher may have new work to pick up.
                    self.log.line(f"OVERSEER-INTEGRATED {phase}")
                    self._fill_slots(f"overseer edits landed ({phase})")
                else:
                    self._advance_done(phase, status)
                pushowed.retry(self.cfg, self.log, skip=set(pushes))
                continue
            # A conflict OR a dirty tree leaves an identifiable repo to clear; a
            # push failure leaves the tree clean (nothing to resolve — retry).
            if result in gitq.LANE_HOLDS:
                hit = landing_mod.blocked(self.cfg, phase)
                repo = hit[1] if hit else None  # the phase's worktree, not the checkout
            else:
                repo = (
                    gitq.blocked_repo(self.cfg, phase)
                    if result in (gitq.CONFLICT, gitq.DIRTY)
                    else None
                )
            self._hold(phase, result, repo, None)  # head stays queued, queue held
            return

    def _dequeue(self, phase: str) -> None:
        with state_mod.transaction(self.cfg) as st:
            st.integ_drop(phase)  # the head, unless lanes landed a later one

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
        held = None
        if kind in gitq.LANE_HOLDS:
            msg, held = self._hold_lane(phase, kind)
        elif kind == gitq.CONFLICT and repo is not None:
            pane = resolver_mod.spawn(self.cfg, phase, repo, self.log)
            if pane is not None:
                with state_mod.transaction(self.cfg) as st:
                    st.windows[f"resolve:{phase}"] = pane
                # The resolver is on it, and it messages the owner itself
                # (`swarm notify`) if it cannot fix the conflict.
                held = telegram.hold(self.cfg, "a resolver is on it; it tells you if it cannot fix it")
                msg = (
                    f"swarm: {phase}'s work clashes with work already merged in"
                    f" {repo.name}, so all merging is paused. A resolver is fixing it in"
                    f" tmux window resolve-{phase} and restarts merging when done; it"
                    " tells you if it cannot. Nothing to do yet"
                )
            else:
                msg = (
                    f"swarm: {phase}'s work clashes with work already merged in"
                    f" {repo.name}, so all merging is paused, and the resolver would not"
                    f" start. Fix the clash in {repo.name}, then run `swarm resolved {phase}`"
                )
        elif kind == gitq.DIRTY:
            where = f" in {repo.name}" if repo is not None else ""
            msg = detail or gitq.off_main_reason(self.cfg, repo, phase) or (
                f"swarm: {phase} is finished but cannot be merged: the checkout{where}"
                f" has uncommitted changes or stray files in the way, and all merging"
                f" waits on it. Commit, stash or move them, then run"
                f" `swarm resolved {phase}`"
            )
        else:  # PUSH_FAILED
            msg = (
                f"swarm: {phase} merged on this machine but could not be pushed; fix"
                f" the push, then run `swarm resolved {phase}` to retry"
            )
        self.log.line(f"INTEGRATE-BLOCKED {phase} {kind}")
        telegram.notify(
            self.cfg.telegram_notify,
            msg,
            kind="integrate-hold",
            phase=phase,
            source="supervisor._hold",
            suppressed=held,
        )

    def _hold_lane(self, phase: str, kind: str) -> tuple[str, str | None]:
        """Open the resolver on the phase's own worktree for a lane hold, and
        say so. The owner's checkout was never touched: it is clean on main."""
        brief = landing_mod.resolver_brief(self.cfg, phase)
        what = ("its catch-up merge conflicts with work that landed beside it"
                if kind == gitq.LANE_CONFLICT else
                "its re-test against work that landed beside it failed")
        pane = None
        if brief is not None:
            pane = resolver_mod.spawn(self.cfg, phase, brief[0], self.log, line=brief[1])
        if pane is not None:
            with state_mod.transaction(self.cfg) as st:
                st.windows[f"resolve:{phase}"] = pane
            held = telegram.hold(self.cfg, "a resolver is on it; it tells you if it cannot fix it")
            return (f"swarm: {phase} cannot land yet: {what}, so all merging is paused."
                    f" A resolver is fixing it on the phase's own copy in tmux window"
                    f" resolve-{phase} and restarts merging when done; it tells you if it"
                    " cannot. Nothing to do yet"), held
        where = f" in {brief[0]}" if brief is not None else ""
        return (f"swarm: {phase} cannot land yet: {what}, so all merging is paused, and"
                f" the resolver would not start. Fix it on the phase's own copy{where},"
                f" commit there, then run `swarm resolved {phase}`"), None

    # -- resolved: finish a blocked integration, resume the queue ---------
    def _on_resolved(self, phase: str) -> None:
        with state_mod.transaction(self.cfg) as st:
            blocked = st.integ_blocked
            repo_s = st.integ_blocked_repo
            kind = st.integ_blocked_kind
        if blocked != phase:
            self.log.line(f"RESOLVED-IGNORED expected={blocked} got={phase}")
            return
        repo = Path(repo_s) if repo_s else None
        lane_hold = kind in gitq.LANE_HOLDS
        ready = (landing_mod.resolve_ready(repo) if lane_hold
                 else gitq.resolve_ready(self.cfg, repo))
        if repo is not None and not ready:
            # Resolver / owner signalled early (still mid-merge or dirty): stay blocked.
            self.log.line(f"RESOLVED-INCOMPLETE {phase} still-blocked")
            self.overseer.resolver_escalated(phase)
            telegram.notify(
                self.cfg.telegram_notify,
                f"swarm: {phase} still cannot be merged: {repo.name} has an unfinished"
                f" merge or uncommitted changes, so merging stays paused. Finish and"
                f" commit it, then run `swarm resolved {phase}` again",
                kind="integrate-hold",
                phase=phase,
                source="supervisor._on_resolved",
            )
            return
        if lane_hold:
            landing_mod.retest(self.cfg, phase)  # merge main again, check again
        with state_mod.transaction(self.cfg) as st:
            st.integ_blocked = None
            st.integ_blocked_repo = None
            st.integ_blocked_kind = None
            win = st.windows.pop(f"resolve:{phase}", None)
        if win:
            resolver_mod.close(self.cfg, win, self.log, phase)
        self.log.line(f"RESOLVED {phase}")
        self._pump_integrations()  # re-integrate (resumes; may re-block downstream)

    def _on_resolver_escalated(self, phase: str) -> None:
        """The resolver messaged the owner instead of finishing: the hold is no
        longer being handled, so the Overseer may look at it now."""
        with state_mod.transaction(self.cfg) as st:
            blocked = st.integ_blocked
        self.log.line(f"RESOLVER-ESCALATED {phase}")
        if blocked == phase:
            self.overseer.resolver_escalated(phase)

    # -- the operator session: dispatch, and the signal it is over -------
    def _on_operator(self, phase: str) -> None:
        """Open a session for ``phase`` — the triage child's ``now`` poke, and
        ``swarm operator <phase>`` by hand.

        The loop is single-threaded, so ``_dispatch`` already serialises this
        against the queue sweep; what stops the two of them opening *two* sessions
        is the lease, which both take. Held while ``phase`` is still building or
        merging: :func:`operator.on_finished` opens it once it lands."""
        operator_mod.on_poke(self.cfg, phase, self.log)

    def _on_operator_done(self, job: str) -> None:
        """The session signalled its job is finished (or put back until later).

        The item was already settled by the CLI that sent this — durably, before
        the poke, for the same reason ``swarm done`` writes its sentinel first.
        What is left: end the session — in the operator window, or in its own
        wait window if it was parked — BEFORE its mirror is merged and removed, so
        no live process ever loses its cwd; land whatever it committed in its own
        mirror through the ordinary merge queue, open the next job, and re-check
        the finish the job was holding open."""
        released = operator_mod.release(self.cfg, self.log, job)
        key = state_mod.waiter_key(state_mod.OPERATOR, job)
        parked = self._end_parked(key) if released is None else False
        with state_mod.transaction(self.cfg) as st:
            st.clear_pending(key)  # a question it never had to wait long for
        self.log.line(f"EVENT operator-done {job} released={released} parked={parked}")
        # The session is gone, so nothing is using the job's TMPDIR any more.
        launch_mod.drop_session_tmp(self.cfg, operator_mod.mirror_name(job))
        mirror = operator_mod.integration_for(self.cfg, job)
        if mirror is not None:
            with state_mod.transaction(self.cfg) as st:
                st.integ_push(mirror, operator_mod.INTEG_STATUS)
            self._pump_integrations()
        self._check_operator_queue()  # next hand-off, if one is due
        self._finish_if_settled(state_mod.read(self.cfg))

    def _check_operator_queue(self) -> None:
        """Drain the operator queue. Runs on every wake, like the park deadlines.

        Deliberately NOT inside :meth:`_watchdog_tick`: that returns early while
        ``idle < watchdog_s``, and :meth:`_handle` refreshes the idle clock on
        every FIFO line, so a swarm that is moving never reaches the quiet point
        — and the queue would drain only once the run was already over."""
        operator_mod.sweep(self.cfg, self.log, room=self._operator_room)

    def _operator_room(self) -> bool:
        """Is there room for a ``later`` job now? A free slot nothing launchable wants.

        A ``later`` job runs whenever a slot is free: the operator is
        a side session in its own window, so it may open while other phases build
        or merge (merging takes no slot), but never into the slot a launchable
        phase needs. So: a free slot, nothing mid-launch, and no phase the launcher
        would still start (the lane picks when lanes are on, else every ready
        phase; one given up on after :data:`LAUNCH_GIVE_UP` failures does not
        count). A parked phase does not count either: it waits on the owner."""
        st = state_mod.read(self.cfg)
        if not st.free_slots():
            return False
        with self._launch_lock:
            if self._launching:
                return False
        ctx = master_mod.build_context(self.cfg, st)
        waiting = ctx["lanes"]["picked"] if ctx["lanes"].get("enabled") else ctx["ready"]
        return not any(not self._given_up(p) for p in waiting)

    def _operator_blocking(self) -> list[str]:
        """Hand-offs that must hold the finish open, checked BESIDE ``pending()``.

        Not folded into :meth:`state.State.pending`: that is a pure ``State``
        method which cannot read the filesystem queue, and gating it would make a
        non-empty queue both the trigger for opening a session and the reason the
        launch path returns early — a deadlock, not a guard."""
        return operator_mod.blocking(self.cfg)

    # -- parking: a session waiting on the owner vacates what it holds ----
    def _on_waiting(self, key: str) -> None:
        """Arm the park timer for a session that self-reported it needs the owner.

        ``key`` is a worker's phase, ``operator:<job>`` or ``overseer:<pass>``
        (:func:`state.waiter_key`). The FIFO poke already woke ``select``;
        recording the deadline re-arms the loop's timeout on the next pass.
        ``park_after == 0`` disables parking (the session just holds what it
        holds), and one already parked is in its own window for good."""
        if self.cfg.park_after <= 0:
            self.log.line(f"WAITING-IGNORED {key} parking-disabled")
            return
        with state_mod.transaction(self.cfg) as st:
            if key in st.parked:
                self.log.line(f"WAITING-IGNORED {key} already-parked")
                return
            st.waiting[key] = time.time() + self.cfg.park_after
        self.log.line(f"EVENT waiting {key} park_after={self.cfg.park_after}")

    def _on_resumed(self, key: str) -> None:
        """The owner answered before the park fired: cancel the pending park.

        Distinct from the ``resume`` (unpause) verb. A session that was ALREADY
        parked is not in ``waiting``, so this is a no-op for it — it stays in its
        own window until it finishes."""
        with state_mod.transaction(self.cfg) as st:
            cancelled = st.waiting.pop(key, None) is not None
        self.log.line(f"EVENT resumed {key} cancelled={cancelled}")

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
        st = state_mod.read(self.cfg)
        stamps = list(st.waiting.values())
        # A scheduled pause. One already due clamps to 0.0 below, so the very
        # next wake (the first one after a restart, say) sets it.
        if st.pause_at:
            stamps.append(st.pause_at)
        # A scheduled restart this supervisor fires, and sentinels found at
        # adoption inside their grace.
        plan = restart_mod.load(self.cfg)
        if plan.get("stage") == restart_mod.PLANNED and plan.get("timer") == "supervisor" \
                and not plan.get("fired_at"):
            stamps.append(float(plan.get("at") or 0.0))
        stamps.extend(self._adopt_recheck.values())
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
        # The Overseer: a pending pass's gap running out, a cadence, a wait or a
        # starvation episode crossing its threshold, a live pass's deadline.
        ov = self.overseer.next_deadline(now)
        if ov is not None:
            stamps.append(ov)
        deadline = st.overseer_deadline
        if self._overseer_live is not None and deadline > now:
            stamps.append(deadline)
        stamps.extend(t for t in self._gc_deadlines() if t > now)
        blocked_at = blockedping.deadline(self.cfg)
        if blocked_at is not None:
            stamps.append(max(now, blocked_at))
        if self.cfg.usage_enabled and self._usage_last + self.cfg.usage_check_s > now:
            stamps.append(self._usage_last + self.cfg.usage_check_s)
        if self.cfg.backup_every_s and not self._backup_running():
            stamps.append(max(now, self._backup_last + self.cfg.backup_every_s))
        bp = self.bigpic.next_deadline(now)
        if bp is not None:
            stamps.append(bp)
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
        if self._handover is not None or self._restart_proc is not None:
            # A clean-up or a backup ending is not an event: look again shortly.
            deadline = HANDOVER_POLL_S if deadline is None else min(deadline, HANDOVER_POLL_S)
        if not self.watchdog_s:
            return deadline
        cap = max(0.0, self._last_sweep + self.watchdog_s - time.time())
        return cap if deadline is None else min(deadline, cap)

    def _pause_at_tick(self) -> None:
        """Pause the swarm once its scheduled pause is due, exactly as
        ``swarm pause`` does: no new workers launch, running ones finish.

        Runs on every wake; :meth:`_next_timeout` makes sure there is one at the
        moment, and the first wake after a restart catches one that passed while
        the supervisor was down."""
        now = time.time()
        if not 0 < state_mod.read(self.cfg).pause_at <= now:
            return
        with state_mod.transaction(self.cfg) as st:
            due = st.pause_at
            if not 0 < due <= now:
                return  # cancelled or moved since the read above
            st.pause_at = 0.0
            st.paused = True
        self.log.line(f"PAUSE-SCHEDULED-FIRED due={pauseat.stamp(due)} late={now - due:.0f}s")

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

        * a busy slot whose pane is gone -> free it, keep its work for the next
          attempt, ping (never when tmux itself cannot answer);
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
        if st.landing and st.integ_queue and st.integ_blocked is None:
            # A landing check that died without a result, or a `lane-checked`
            # poke no supervisor was there to read: the queue looks again.
            self._pump_integrations()
            st = state_mod.read(self.cfg)
        # Reports the checkout could not take earlier, and `later` rows whose
        # date has come.
        self._flush_ledger(dict(st.done))
        if self._release_dated():
            st = state_mod.read(self.cfg)
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
                f"swarm: the run has ended, but {len(ready)} phase(s) are ready and never"
                f" started: {', '.join(ready[:8])}. Run `swarm up` to carry on.",
            )
            return
        if st.on_hold or st.integ_blocked is not None or not st.free_slots():
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
        held, a push origin does not have yet, an operator hand-off, a live master
        mid-pass — or a ready phase, because the launcher owns that one
        (a phase given up on after :data:`LAUNCH_GIVE_UP` failed launches does
        not hold it; the finish message names it instead)."""
        if st is None:
            st = state_mod.read(self.cfg)
        if st.finished or st.on_hold or st.pending():
            return
        if st.integ_queue or st.integ_blocked is not None or self.master.is_alive():
            return
        if self._overseer_holds_finish(st):
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
            # Settled but for the hand-offs: the run is quiet, so a `later` job
            # is due now, and nothing else may wake the loop to open it.
            self._check_operator_queue()
            return
        self.log.line(f"{tag} settled")
        self._finish_run(ctx)

    def _reap_dead_panes(self, st: state_mod.State) -> list[str]:
        """Free every busy slot whose worker pane has vanished. Returns the phases.

        A phase is only reaped after being seen dead on TWO consecutive sweeps:
        ``launch`` claims the slot before it creates the worktree and respawns the
        pane, so a single sighting inside that window would reap a worker that is
        about to start. Bare-driver runs have no panes and are skipped entirely.

        Only a real answer from tmux counts. When tmux errors, hangs or has lost
        the session, every pane would look gone: nothing is reaped, the sightings
        start over, and the next sweep asks again.
        """
        if self.cfg.driver != "tmux":
            return []
        panes = tmux.pane_states(self.cfg.session)
        if panes is None:
            self.log.line("WATCHDOG-TMUX-UNREACHABLE no answer from tmux; nothing reaped")
            self._suspect = {}
            return []
        suspect: dict[int, str] = {}
        reaped: list[str] = []
        for slot in st.busy_slots():
            if not slot.pane_id or not slot.phase or panes.get(slot.pane_id) is False:
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

    def _reap(self, phase: str) -> None:
        """Free the slot of a worker that died without reporting, keep its work
        for the next attempt (:func:`gitq.set_aside`), tell the owner. Not
        recorded in ``done``: the phase stays launchable, and its next launch
        resumes on the same branch. After :data:`CRASH_LIMIT` deaths within
        :data:`CRASH_WINDOW_S` it is given up on instead, like a phase that
        keeps failing to launch, until the owner puts it back."""
        self.log.line(f"WATCHDOG-REAP {phase} pane-dead")
        now = time.time()
        recent = [t for t in self._crashes.get(phase, []) if now - t < CRASH_WINDOW_S]
        recent.append(now)
        self._crashes[phase] = recent
        crash_looping = len(recent) >= CRASH_LIMIT
        if crash_looping:
            with self._launch_lock:
                self._launch_fails[phase] = (LAUNCH_GIVE_UP, now)
            self.log.line(f"WATCHDOG-CRASH-HOLD {phase} {len(recent)} deaths in an hour")
        with state_mod.transaction(self.cfg) as st:
            st.free_slot_for(phase)
            st.last_event_at = now
        logutil.run_ended(self.log, phase, "reaped")
        if crash_looping:
            self._ping(
                f"crash-hold:{phase}",
                f"swarm: the worker for {phase} has stopped unexpectedly {len(recent)}"
                " times in the last hour, so the swarm has stopped restarting it and the"
                " phases after it wait. Its work so far is kept. Once you have looked,"
                f" `swarm launch {phase}` starts it again from there.",
                cooldown=0.0,
            )
        else:
            self._ping(
                f"reap:{phase}",
                f"swarm: the worker for {phase} stopped without finishing. Its work so"
                " far is kept, and the phase will be started again from there. Nothing"
                " to do.",
            )
        if self.cfg.git_isolation == "worktree":
            try:
                gitq.set_aside(self.cfg, phase, self.log)
            except gitq.GitError as exc:
                self.log.line(f"WATCHDOG-SET-ASIDE-ERROR {phase} {exc}")
        launch_mod.drop_session_tmp(self.cfg, phase)  # the pane is dead
        # The pane died, but a `setsid`/`nohup` child it started may not have.
        session_mod.reap_session(self.cfg, "worker", phase, self.log)

    def _check_park_deadlines(self) -> None:
        """Park every waiting session whose deadline has fired. Runs on every
        wake; a no-op when nothing is due (so a spurious/FIFO wake is harmless)."""
        now = time.time()
        due = [
            k for k, deadline in state_mod.read(self.cfg).waiting.items() if deadline <= now
        ]
        for key in due:
            self._park(key)

    def _park(self, key: str) -> None:
        """Move a waiting session to its own window and free what it held.

        A worker frees its grid slot, an operator job the operator window (so the
        next job runs), an Overseer pass the master pane (so the next pass can
        run). Each goes on waiting for the owner, alive, in ``wait:<...>``
        (:func:`state.wait_window`), and finishes there."""
        kind, ident = state_mod.waiter(key)
        if kind == state_mod.OPERATOR:
            self._park_operator(key, ident)
        elif kind == state_mod.OVERSEER:
            self._park_overseer(key, ident)
        else:
            self._park_worker(ident)

    def _move_off(self, key: str, pane: str | None, slot: int | None, layout: str) -> str | None:
        """Break ``key``'s live pane out to its own window, leaving a fresh holding
        pane where it was; return that replacement. Split-FIRST (tmux.park_pane),
        so ``break-pane`` never sees a single-pane window. Driver-guarded: on the
        bare driver this is a no-op and parking is pure state."""
        if self.cfg.driver != "tmux" or not pane:
            return None
        name = state_mod.wait_window(key)
        wait_win, replacement = tmux.park_pane(
            tmux.window_of(pane), pane, slot, name, self.cfg.session, layout
        )
        if wait_win:
            with state_mod.transaction(self.cfg) as st:
                st.windows[name] = wait_win
        return replacement or None

    def _parked_ping(self, key: str, who: str) -> None:
        name = state_mod.wait_window(key)
        self.log.line(f"PARK {key} window={name}")
        telegram.notify(
            self.cfg.telegram_notify,
            f"swarm: {who} is still waiting for your answer, now in its own tmux window"
            f" {name} so the rest of the swarm can carry on",
            kind="park",
            phase=key,
            source="supervisor._park",
            # You were asked when it started waiting; a park only moves windows.
            suppressed=telegram.hold(self.cfg, "you were already asked; a park only moves windows"),
        )

    def _park_worker(self, phase: str) -> None:
        """Free the waiting worker's grid slot and relaunch into it: grow the
        slot's window with a replacement pane, break the waiter out, re-tidy,
        tag the replacement with the slot. ``st.park`` then frees the slot record
        — keeping its now-replacement ``pane_id`` — and ``_fill_slots`` fills it
        with the next ready phase (the parked phase is excluded from ``ready``)."""
        with state_mod.transaction(self.cfg) as st:
            slot = next((s for s in st.slots if s.busy and s.phase == phase), None)
            if slot is None:
                # No longer busy (already done / freed): just drop the dead timer.
                st.waiting.pop(phase, None)
                self.log.line(f"PARK-SKIP {phase} not-busy")
                return
            sid, old_pane = slot.id, slot.pane_id
            layout = st.layout or self.cfg.tmux_layout
        replacement = self._move_off(phase, old_pane, sid, layout)
        with state_mod.transaction(self.cfg) as st:
            if replacement:
                s = st.slot_by_id(sid)
                if s is not None:
                    s.pane_id = replacement
            st.park(phase)
            paused = st.on_hold
        self._parked_ping(phase, f"the worker on {phase}")
        if paused:
            self.log.line("PARK-PAUSED holding — no launch")
            return
        self._fill_slots(f"{phase} parked (slot {sid} free)")

    def _park_operator(self, key: str, job: str) -> None:
        """Free the operator window for the next job. The lease goes with it: the
        parked job's item stays ``waiting`` and holds the finish, and only its own
        ``operator-done`` (or a lease that finally runs out) ends it."""
        with state_mod.transaction(self.cfg) as st:
            if st.operator_phase != job:
                st.waiting.pop(key, None)
                self.log.line(f"PARK-SKIP {key} not-in-the-operator-window")
                return
            pane = st.operator_pane
        replacement = self._move_off(key, pane, None, self.cfg.tmux_layout)
        with state_mod.transaction(self.cfg) as st:
            if replacement:
                st.operator_pane = replacement
            st.release_operator()
            st.park(key)
        self._parked_ping(key, f"operator job {job}")
        self._check_operator_queue()

    def _park_overseer(self, key: str, pid: str) -> None:
        """Free the master pane: the pass stops being the live one, so a later
        pass may run; this one ends with its own ``overseer-done``."""
        if pid != self._overseer_live:
            with state_mod.transaction(self.cfg) as st:
                st.waiting.pop(key, None)
            self.log.line(f"PARK-SKIP {key} not-the-live-pass")
            return
        pane = state_mod.read(self.cfg).master_pane
        replacement = self._move_off(key, pane, None, self.cfg.tmux_layout)
        self.master.detach()
        self._overseer_live = None
        with state_mod.transaction(self.cfg) as st:
            if replacement:
                st.master_pane = replacement
            st.master_alive = False
            st.overseer_pass = None
            st.overseer_deadline = 0.0
            st.park(key)
        self.overseer.end(time.time())
        self._parked_ping(key, "the Overseer")

    def _end_parked(self, key: str) -> bool:
        """End a parked operator job or Overseer pass: close its window and every
        process its session started. False if ``key`` was not parked."""
        name = state_mod.wait_window(key)
        with state_mod.transaction(self.cfg) as st:
            if key not in st.parked:
                return False
            st.clear_pending(key)
            win = st.windows.pop(name, None)
        if win and self.cfg.driver == "tmux":
            tmux.kill_window(win)
        kind, ident = state_mod.waiter(key)
        session_mod.reap_session(self.cfg, kind, ident, self.log)
        self.log.line(f"UNPARK {key}")
        return True

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
            paused = st.on_hold
        if wait_win and self.cfg.driver == "tmux":
            tmux.kill_window(wait_win)  # close the parked worker's own window
        # The session's TMPDIR ends with its phase. Under worktree isolation the
        # merge/discard already took it with the mirror; this is the in-place
        # (`isolation = none`) path, and an idempotent no-op otherwise.
        launch_mod.drop_session_tmp(self.cfg, phase)
        self.log.line(
            f"EVENT done {phase} {status} freed_slot={freed_id} parked={was_parked}"
        )
        # The phase has landed (or been rolled back): its ledger tick, status and
        # history entry go on the target branch now, and only now.
        self._flush_ledger({phase: status})
        # A `later` whose row now carries its date waits for that date, not on
        # a failure record: nothing downstream may read it as failed.
        self._release_dated()
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
        claims second is refused by :meth:`state.State.claim_slot`.

        Every call is also a moment the ledger may have moved, so an owner-run
        row that has just started holding rows up is told to the owner here
        (once per row, :func:`owner.ping_owner_rows`), paused or not."""
        st = state_mod.read(self.cfg)
        if not st.finished:
            try:
                owner_mod.ping_owner_rows(self.cfg, st)
            except Exception as exc:  # noqa: BLE001 - a ping must never stop a launch
                self.log.line(f"OWNER-ROWS-ERROR {exc!r}")
        if st.on_hold or st.finished:
            return []
        if self._handover is not None:
            # Handing over: a launch started now would be cut off mid-way. The
            # next supervisor fills the slots the moment it has adopted the run.
            self.log.line(f"LAUNCH-HELD handover ({reason})")
            return []
        if self._bootstrapping and not force:
            if self.master.is_alive():
                self.log.line(f"LAUNCH-HELD bootstrap ({reason})")
                return []
            self._end_bootstrap()  # the init master died without idling
        ctx = master_mod.build_context(self.cfg, st)
        busy = set(ctx["busy_slots"].values())
        now = time.time()
        with self._launch_lock:
            budget = len(ctx["free_slots"]) - sum(
                1 for p in self._launching if p not in busy
            )
            picks: list[str] = []
            # With lanes on, only the lane scheduler's picks: walking `ready`
            # would launch rows it holds back, leaving the backstop to refuse them.
            lanes = ctx["lanes"]
            for phase in lanes["picked"] if lanes["enabled"] else ctx["ready"]:
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
                held = st.free_slot_for(phase) is not None  # don't strand the claim
            if held:
                logutil.run_ended(self.log, phase, "launch-failed")
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
                f"swarm: the worker for {phase} failed to start {fails} times in a row,"
                " so the swarm stopped trying and the phases after it wait. Fix the"
                f" cause, then run `swarm launch {phase}` (or `swarm resume` to retry"
                " every phase it gave up on).",
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
        batch waits for this) and re-check the finish.

        An Overseer that signals ``master-idle`` instead of ``overseer-done`` has
        still finished its pass: end it the proper way (record, mirror merge)."""
        if self._overseer_live is not None:
            self.log.line(f"OVERSEER-MASTER-IDLE {self._overseer_live}")
            self._end_overseer_pass(self._overseer_live, ovrecord.DONE)
            return
        self.master.kill()
        self._end_bootstrap()
        with state_mod.transaction(self.cfg) as st:
            st.master_alive = False
            paused = st.on_hold
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

    def _end_bootstrap(self) -> None:
        """The init pass holds launches no longer: it idled, died, or never ran.

        Recorded in state once (``State.bootstrapping``), because ``swarm doctor``
        cannot see this process: it once read the init pass's free slots
        and ready phases, fifteen seconds after ``up``, as a lost nudge."""
        self._bootstrapping = False
        if not self._bootstrap_recorded_over:
            self._bootstrap_recorded_over = True
            with state_mod.transaction(self.cfg) as st:
                st.bootstrapping = False

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

    # -- the Overseer: periodic review passes in the master pane -----------
    def _overseer_tick(self) -> None:
        """One look for the Overseer's trigger policy, then maybe a pass.

        Runs on every wake and after every event. A live pass past its deadline
        is killed first — whatever a hung session is doing, it must never hold
        the pane or the finish. Launching never waits on any of this: the
        launcher is the supervisor's own (rule 1)."""
        now = time.time()
        st = state_mod.read(self.cfg)
        live = self._overseer_live
        if live is not None and st.overseer_deadline and now >= st.overseer_deadline:
            self._overseer_timeout(live)
            st = state_mod.read(self.cfg)
        if st.finished:
            return
        if not self.cfg.overseer_enabled:
            # Nothing is watched while it is off, so nothing seen now is news
            # later: a reload that turns it on re-baselines instead of reporting
            # every phase that finished in the meantime.
            self.overseer.mem.seen_done = None
            self.overseer.mem.finished_since = 0
            return
        self.overseer.observe(
            st, now, starving=self._starving(st), doctor_fails=self._doctor_probe(st, now)
        )
        self._maybe_start_overseer(st, now)

    def _launch_view(self) -> tuple[set[str], set[str], set[str]]:
        """``(launching, given_up, backing_off)`` under the launch lock."""
        now = time.time()
        with self._launch_lock:
            launching = set(self._launching)
            given_up = {p for p, (n, _) in self._launch_fails.items() if n >= LAUNCH_GIVE_UP}
            backing = {p for p in self._launch_fails if self._backing_off(p, now)}
        return launching, given_up, backing

    def _starving(self, st: state_mod.State) -> bool:
        """Free slots, nothing the launcher can start, and backlog still open.

        The supervisor's own verdict, because only it knows what is mid-launch,
        backing off or given up. A phase given up after failed launches is
        backlog nothing will start — starvation, not progress."""
        if st.on_hold or not st.free_slots() or not self._bootstrapped or self.master.is_alive():
            return False
        launching, given_up, backing = self._launch_view()
        if launching:
            return False
        ctx = master_mod.build_context(self.cfg, st)
        if any(p not in given_up and p not in backing for p in ctx["ready"]):
            return False
        path = self.cfg.project_dir / self.cfg.ledger
        graph = ledger_mod.load(path)
        # A row waiting for its date is not work the swarm is failing to start.
        excluded = set(self.cfg.exclude) | set(ledgerw.dated(self.cfg))
        flying = ovdigest.in_flight(st)
        done = ledger_mod.with_ticked(st.done, ledger_mod.load_ticked(path), flying)
        return bool(overseer_mod.backlog(graph, done, excluded, flying))

    def _doctor_probe(self, st: state_mod.State, now: float) -> dict[str, str] | None:
        """The cheap doctor checks that mean *stuck*, at most every
        :data:`DOCTOR_PROBE_S`: ``ledger`` (a cycle or unknown dep strands every
        phase behind it) and ``run.nudge`` (free slots and ready phases, nothing
        launching — a phase given up after failed launches shows here). Never
        the full doctor, which shells out to git and du. ``None`` = not probed."""
        if now - self._doctor_probed < DOCTOR_PROBE_S:
            return None
        if not self._bootstrapped or self.master.is_alive():
            return None
        launching, _given_up, backing = self._launch_view()
        if launching:
            return None  # a launch in flight reads as a lost nudge
        self._doctor_probed = now
        ctx = master_mod.build_context(self.cfg, st)
        ready = [p for p in ctx["ready"] if p not in backing]
        checks = [
            doctor_mod._check_ledger(self.cfg, st),
            doctor_mod._check_nudge(st, ready, ctx["free_slots"]),
        ]
        return {c.name: c.detail for c in checks if c.status == doctor_mod.FAIL}

    def _maybe_start_overseer(self, st: state_mod.State, now: float) -> None:
        if not self._bootstrapped or self._overseer_live is not None or st.drain:
            return
        if self._handover is not None:
            return  # handing over: the next supervisor starts the pass
        if self.master.is_alive():
            return  # the init pass has the pane; the pending pass waits for it
        # An init master that died without idling leaves the bootstrap hold set,
        # and with a pass in the pane `_fill_slots` would read that pass as the
        # init master and hold every launch. Settle it the way `_fill_slots` does.
        self._end_bootstrap()
        if not self.overseer.due(now):
            return
        # A hold reason may have waited while the resolver cleared it.
        self.overseer.recheck(state_mod.read(self.cfg), now)
        if not self.overseer.due(now):
            return
        self._start_overseer_pass(now)

    def _start_overseer_pass(self, now: float) -> None:
        """Reserve the pass (record, state, the live id) here; build its digest,
        its mirror and its session on a thread.

        A worktree mirror (one per repo) and a claude boot take tens of
        seconds, which the loop must never spend: the thread reports back as
        ``overseer-spawned <id> ok|failed``, exactly as a launch does."""
        mem = self.overseer.mem
        since = mem.last_pass_at or mem.anchor or now
        prior = ovrecord.load_passes(self.cfg, limit=1)
        last = prior[0].to_dict() if prior else None
        reasons = self.overseer.begin(now)
        pid = ovrecord.new_id(self.cfg, now)
        mirror = ovrecord.mirror_name(pid) if self.cfg.git_isolation == "worktree" else ""
        digest = overseer_mod.overseer_dir(self.cfg) / f"digest-{pid}.md"
        ovrecord.create(self.cfg, pid, [asdict(r) for r in reasons], digest, mirror, now)
        self._overseer_live = pid
        self._overseer_spawning = True
        self._overseer_reasons = reasons
        with state_mod.transaction(self.cfg) as st:
            st.overseer_pass = pid
            st.overseer_deadline = now + self.cfg.overseer_timeout_s
        self.log.line(
            f"OVERSEER-PASS-START {pid} reasons={','.join(r.key for r in reasons) or '-'}"
        )
        launching, given_up, _ = self._launch_view()
        self._start_overseer_spawn(pid, reasons, since, launching, sorted(given_up), last)

    def _start_overseer_spawn(self, pid: str, *args) -> None:
        """Run :meth:`_overseer_spawn` off the loop thread (daemon, like a launch)."""
        threading.Thread(
            target=self._overseer_spawn, args=(pid, *args), name=f"overseer:{pid}", daemon=True
        ).start()

    def _overseer_spawn(self, pid, reasons, since, launching, given_up, last) -> None:
        """Thread body: digest, mirror, session; report through the FIFO."""
        cfg = self.cfg
        outcome = "failed"
        try:
            data = ovdigest.build(
                cfg, state_mod.read(cfg), reasons,
                since=since, launching=launching, given_up=given_up, last_pass=last,
            )
            digest = ovdigest.write(cfg, pid, data)
            cwd: Path | None = None
            if cfg.git_isolation == "worktree":
                cwd = gitq.worktree_add(cfg, ovrecord.mirror_name(pid), self.log)
                launch_mod.pretrust_dir(cwd, self.log)
            record = ovrecord.md_path(cfg, pid)
            env = {
                **launch_mod.session_env(cfg, cwd, tmp=ovrecord.mirror_name(pid),
                                         session=f"overseer:{pid}"),
                "SWARM_MASTER_KIND": master_mod.OVERSEER,
                "SWARM_OVERSEER_PASS": pid,
                "SWARM_OVERSEER_DIGEST": str(digest),
                "SWARM_OVERSEER_RECORD": str(record),
                "SWARM_PROJECT": str(cfg.project_dir),
            }
            brief = overseer_mod.overseer_dir(cfg) / f"{pid}.brief.md"
            brief.write_text(
                master_mod.overseer_brief(cfg, pid, digest, record, cwd) + "\n",
                encoding="utf-8",
            )
            line = master_mod.overseer_line(pid, brief)
            pane = state_mod.read(cfg).master_pane
            if self.master.spawn(master_mod.OVERSEER, pane, cwd=cwd, env=env, line=line):
                outcome = "ok"
        except Exception as exc:  # noqa: BLE001 - a thread must report, not vanish
            self.log.line(f"OVERSEER-SPAWN-ERROR {pid} {exc!r}")
        launch_mod._poke_fifo(cfg, f"overseer-spawned {pid} {outcome}\n")

    def _on_overseer_spawned(self, pid: str, outcome: str) -> None:
        if pid != self._overseer_live:
            self.log.line(f"OVERSEER-SPAWNED-STALE {pid} {outcome}")
            return
        self._overseer_spawning = False
        if outcome == "ok":
            with state_mod.transaction(self.cfg) as st:
                st.master_alive = True
            self.log.line(f"OVERSEER-SPAWNED {pid}")
            return
        # It never ran: put its reasons back (they wait out the gap, so a pass
        # that cannot start is not retried in a tight loop) and tell the owner.
        self._overseer_live = None
        with state_mod.transaction(self.cfg) as st:
            st.overseer_pass = None
            st.overseer_deadline = 0.0
        ovrecord.update(self.cfg, pid, status=ovrecord.FAILED, ended_at=time.time())
        self.overseer.requeue(self._overseer_reasons)
        self.overseer.end()
        if self.cfg.git_isolation == "worktree":
            try:
                gitq.discard(self.cfg, ovrecord.mirror_name(pid), self.log)
            except gitq.GitError as exc:
                self.log.line(f"OVERSEER-DISCARD-ERROR {pid} {exc}")
        self.log.line(f"OVERSEER-SPAWN-FAILED {pid}")
        self._ping(
            "overseer-spawn",
            "swarm: the Overseer (the session that looks after the run) would not"
            f" start ({pid}). Workers carry on; what it was due to look at waits for"
            " its next pass. If this keeps happening, check the overseer window.",
            kind="overseer",
            source="supervisor._on_overseer_spawned",
            suppressed=self._overseer_streak(),
        )
        self._finish_if_settled()

    def _overseer_timeout(self, pid: str) -> None:
        minutes = self.cfg.overseer_timeout_s // 60
        self.log.line(f"OVERSEER-TIMEOUT {pid} after {self.cfg.overseer_timeout_s}s")
        self._ping(
            f"overseer-timeout:{pid}",
            f"swarm: an Overseer pass ({pid}) ran past its {minutes}-minute limit and"
            " was stopped; what it had finished is kept. Workers carry on; nothing to"
            " do unless this keeps happening.",
            cooldown=0.0,
            kind="overseer",
            source="supervisor._overseer_timeout",
            suppressed=self._overseer_streak(),
        )
        self._end_overseer_pass(pid, ovrecord.TIMEOUT)

    def _end_overseer_pass(self, pid: str, status: str) -> None:
        """End a pass — the live one, or one parked on the owner in its own
        window: kill its session BEFORE its mirror is merged and removed (no
        live process may lose its cwd), settle the record, land the mirror
        through the ordinary queue, and let the launcher and the finish look
        again — the pass may have changed the ledger or retried a phase."""
        now = time.time()
        if pid == self._overseer_live:
            self._overseer_live = None
            self.master.kill()
            session_mod.reap_session(self.cfg, "overseer", pid, self.log)  # and what it started
            with state_mod.transaction(self.cfg) as st:
                st.master_alive = False
                st.overseer_pass = None
                st.overseer_deadline = 0.0
                st.clear_pending(state_mod.waiter_key(state_mod.OVERSEER, pid))
            self.overseer.end(now)
        elif not self._end_parked(state_mod.waiter_key(state_mod.OVERSEER, pid)):
            self.log.line(f"OVERSEER-DONE-IGNORED expected={self._overseer_live} got={pid}")
            return
        launch_mod.drop_session_tmp(self.cfg, ovrecord.mirror_name(pid))  # session gone
        rec = ovrecord.update(self.cfg, pid, status=status, ended_at=now)
        if status == ovrecord.DONE:
            self._overseer_bad = 0
        self.log.line(f"OVERSEER-PASS-END {pid} {rec.status if rec else status}")
        if self.cfg.git_isolation == "worktree":
            name = ovrecord.mirror_name(pid)
            if gitq.branch_exists(self.cfg.project_dir, f"swarm/{name}"):
                with state_mod.transaction(self.cfg) as st:
                    st.integ_push(name, ovrecord.INTEG_STATUS)
                self._pump_integrations()
        self._fill_slots(f"overseer pass {pid} over")
        self._finish_if_settled()

    def _overseer_streak(self) -> str | None:
        """Count one more bad pass; the hold reason unless it makes a streak.

        One pass that would not start or ran long costs nothing the next pass does
        not pick up. :data:`telegram.STREAK` in a row is a broken Overseer, and the owner
        hears about it (and again at every further one)."""
        self._overseer_bad += 1
        if self._overseer_bad % telegram.STREAK == 0:
            return None
        return telegram.hold(
            self.cfg, f"bad pass {self._overseer_bad} in a row; you hear at {telegram.STREAK}")

    def _overseer_holds_finish(self, st: state_mod.State) -> bool:
        """A live pass, or one owed, holds the finish.

        The event that settles a run may be the very one that should trigger a
        pass (the last phase failing, the Nth finishing), and a run that finishes
        first never gets it. So look once more here — without the starvation
        verdict, which a settled run with excluded rows left over would satisfy
        forever."""
        if self._overseer_live is not None:
            return True
        if not self.cfg.overseer_enabled or not self._bootstrapped:
            return False
        self.overseer.observe(st, starving=None, doctor_fails=None)
        pending = self.overseer.pending
        if pending:
            self.log.line(f"FINISH-HELD overseer pending={[r.key for r in pending]}")
            return True
        return False

    # -- automatic gc ---------------------------------------------------------
    def _gc_running(self) -> bool:
        return self._gc_thread is not None and self._gc_thread.is_alive()

    def _gc_tick(self) -> None:
        """Start a background gc when one is due; never blocks the loop.

        Due at most once per ``[gc].every_s``, and once per idle episode (no busy
        slot) longer than ``[gc].idle_s``. :func:`gc.auto` waits its turn in the
        build queue, holding no slot, and refuses to run while any build is
        alive or a compiler is running under a tree it would touch; that comes
        back as ``busy`` and is retried after :data:`GC_RETRY_S`."""
        cfg = self.cfg
        if not cfg.gc_auto:
            return
        # Idle tracking first, even while a gc runs: a busy spell during one must
        # still end its episode, or the next idle stretch would go unserved.
        now = time.time()
        if state_mod.read(cfg).busy_slots():
            self._gc_idle_since = None
        elif self._gc_idle_since is None:
            self._gc_idle_since = now
            self._gc_episode += 1
        if self._gc_running():
            return
        reason = self._gc_due(now)
        if reason is None or now < self._gc_retry_at:
            return
        episode = self._gc_episode if self._gc_idle_since is not None else None
        self._gc_thread = threading.Thread(
            target=self._gc_run, args=(reason, episode), name="gc-auto", daemon=True
        )
        self._gc_thread.start()

    def _gc_due(self, now: float) -> str | None:
        cfg = self.cfg
        if cfg.gc_every_s and now - self._gc_last >= cfg.gc_every_s:
            return "interval"
        if (
            cfg.gc_idle_s
            and self._gc_idle_since is not None
            and self._gc_episode_done != self._gc_episode
            and now - self._gc_idle_since >= cfg.gc_idle_s
        ):
            return "idle"
        return None

    def _gc_deadlines(self) -> list[float]:
        """When the gc scheduler next wants a wake (the caller drops past ones)."""
        cfg = self.cfg
        if not cfg.gc_auto or self._gc_running():
            return []
        out = [self._gc_retry_at]
        if cfg.gc_every_s:
            out.append(self._gc_last + cfg.gc_every_s)
        if cfg.gc_idle_s and self._gc_idle_since is not None and (
            self._gc_episode_done != self._gc_episode
        ):
            out.append(self._gc_idle_since + cfg.gc_idle_s)
        return out

    def _gc_run(self, reason: str, episode: int | None) -> None:
        """Thread body: one automatic gc, logged and recorded."""
        try:
            result = gc_mod.auto(self.cfg, self.log)
        except Exception as exc:  # noqa: BLE001 - a thread must report, not vanish
            result = gc_mod.AutoResult(gc_mod.AUTO_FAILED, detail=repr(exc))
        now = time.time()
        if result.outcome == gc_mod.AUTO_BUSY:
            self._gc_retry_at = now + GC_RETRY_S
            self.log.line(f"GC-AUTO-SKIP {reason} busy: {result.detail}")
            return
        # A failure waits out the same interval as a success: retrying a broken
        # gc every few minutes would walk every large build target for nothing.
        self._gc_last = now
        if episode is not None:
            self._gc_episode_done = episode
        gc_mod.write_record(self.cfg, result, reason, now)
        if result.outcome == gc_mod.AUTO_FAILED:
            self.log.line(f"GC-AUTO-FAILED {reason} {result.detail}")
            return
        kinds = " ".join(
            f"{k}={gc_mod.human(v)}" for k, v in sorted(result.by_kind.items()) if v
        )
        self.log.line(
            f"GC-AUTO {reason} freed={result.freed} ({gc_mod.human(result.freed)})"
            f" {kinds or '-'} errors={result.errors}"
        )

    # -- usage caps (see caps.py) -------------------------------------------
    def _usage_tick(self) -> None:
        """Check usage against the caps, at most once per ``[usage].check_s``.

        Runs with the caps off too, so that turning them off lifts a hold. A
        switch of the logged-in account checks at once: a hold on the old
        account should not wait out the interval once the new one reads under
        its limit."""
        now = time.time()
        login = usage_mod.login_account()
        if now - self._usage_last >= self.cfg.usage_check_s or (
                login is not None and login != self._usage_login):
            self._usage_check(now)

    def _usage_check(self, now: float) -> bool:
        """Read the newest usage, apply the rules, and act on what changed.
        True when a ``down`` rule has begun stopping the swarm.

        The tap's samples come first. Only when they hold no fresh reading does
        this ask the usage endpoint, at most once per :data:`caps.API_MIN_GAP_S`
        across restarts — or at once after an account switch, whose account has
        nothing fresh yet; a failed call is logged and the last reading stands.
        Only the samples of the account in use count (:func:`usage.active_account`).
        """
        self._usage_last = now
        cfg = self.cfg
        rules = cfg.usage_rules if cfg.usage_enabled else []
        login = usage_mod.login_account()
        switched = None not in (login, self._usage_login) and login != self._usage_login
        self._usage_login = login
        self._usage_tail.poll()
        account = usage_mod.active_account(self._usage_tail.samples)
        samples = usage_mod.of_account(self._usage_tail.samples, account)
        if cfg.usage_enabled and caps.needs_api(samples, now, cfg.usage_stale_s):
            if switched or now - state_mod.read(cfg).usage_api_at >= caps.API_MIN_GAP_S:
                with state_mod.transaction(cfg) as st:
                    st.usage_api_at = now
                row, note = caps.fetch_api(cfg.state_dir, now)
                self.log.line(f"USAGE-API {note}")
                if row is not None:
                    caps.record_api(cfg.state_dir, row)
                    self._usage_tail.poll()
                    samples = usage_mod.of_account(self._usage_tail.samples, account)
        reads = caps.readings(samples, now, cfg.usage_stale_s)
        with state_mod.transaction(cfg) as st:
            out = caps.evaluate(rules, reads, st.usage_hold, st.usage_fired,
                                st.usage_override, now, account)
            st.usage_hold, st.usage_fired, st.usage_override = out.hold, out.fired, out.override
            on_hold = st.on_hold
        figures = " ".join(f"{w}={r.pct:g}%" for w, r in sorted(reads.items())) or "unknown"
        self.log.line(f"USAGE-CHECK {figures} account={account or '?'} "
                      f"held={','.join(sorted(out.hold)) or '-'}")
        for window in out.held:
            h = out.hold[window]
            self.log.line(f"USAGE-HOLD {window} {h['pct']:g}% limit={h['at']:g}%")
            if out.down is None:  # the stop's own ping says it all
                self._usage_ping(caps.pause_ping(window, h, now))
        for window in out.lifted:
            self.log.line(f"USAGE-LIFT {window} window reset")
            self._usage_ping(caps.lift_ping(window, reads.get(window)))
        for window in out.switched:
            self.log.line(f"USAGE-LIFT {window} account switched to {account}")
            self._usage_ping(caps.switch_ping(window, reads.get(window), account))
        for window in out.released:
            self.log.line(f"USAGE-RELEASE {window} no rule holds it now")
        if out.down is not None:
            d = out.down
            self.log.line(f"USAGE-DOWN {d['window']} {d['pct']:g}% limit={d['at']:g}%")
            self._usage_ping(caps.down_ping(d, now, bool(out.hold)))
            self._usage_down()
            return True
        if (out.lifted or out.switched or out.released) and not on_hold:
            self._fill_slots("usage cap lifted")
        return False

    def _usage_ping(self, msg: str) -> None:
        self._ping("usage-cap", msg, cooldown=0.0, kind="usage-cap",
                   source="supervisor._usage_check")

    def _usage_down(self) -> None:
        """Stop the swarm the way ``swarm down`` does, by running it.

        Detached, because ``swarm down`` asks this process to exit and waits
        for it; the swarm then stays down until the owner runs ``swarm up``."""
        cfg = self.cfg
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        with (cfg.log_dir / "usage-down.log").open("a", encoding="utf-8") as out:
            subprocess.Popen(
                [sys.executable, "-m", "swarm_orchestrator", "--project-dir",
                 str(cfg.project_dir), "down"],
                cwd=str(cfg.project_dir), stdin=subprocess.DEVNULL, stdout=out,
                stderr=subprocess.STDOUT, start_new_session=True,
            )

    # -- backup pushes --------------------------------------------------------
    def _backup_running(self) -> bool:
        return self._backup_thread is not None and self._backup_thread.is_alive()

    def _backup_tick(self) -> None:
        """Start a backup pass every ``[backup].every_s``, on a thread: it is
        network-bound and must never hold up the loop."""
        every = self.cfg.backup_every_s
        now = time.time()
        if not every or self._backup_running() or now - self._backup_last < every:
            return
        self._backup_last = now
        self._backup_thread = threading.Thread(
            target=self._backup_run, name="backup", daemon=True
        )
        self._backup_thread.start()

    def _backup_run(self) -> None:
        try:
            backup_mod.run(self.cfg, self.log)
        except Exception as exc:  # noqa: BLE001 - a thread must report, not vanish
            self.log.line(f"BACKUP-ERROR {exc!r}")

    # -- resource sampler -----------------------------------------------------
    def _start_resources(self) -> None:
        """Start the sampler thread. A sampler that cannot start is logged and
        left out: measuring must never stop the swarm."""
        try:
            self._resources = resources_mod.Sampler(
                lambda: self.cfg, notify=self._resources_ping, log=self.log.line)
            self._resources.start()
        except Exception as exc:  # noqa: BLE001 - optional instrument, never fatal
            self._resources = None
            self.log.line(f"RESOURCES-ERROR not started: {exc!r}")

    def _resources_ping(self, key: str, msg: str) -> None:
        """An idle build holder: the sampler rate-limits per build itself."""
        self._ping(key, msg, cooldown=0.0, kind="idle-build",
                   source="resources.sampler")

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
                f"; {len(leftover)} ready but unlaunched (their worker kept failing to"
                f" start): {', '.join(leftover)}. Run `swarm launch <phase>` to carry on"
            )
        if operator:
            # Only reachable past the blocking check, so every one of these is an
            # item that burned its attempt cap: real work nothing will retry. They
            # are the notes the whole feature exists to stop losing — name them.
            msg += (
                f"; {len(operator)} follow-up job(s) the operator never got done"
                f" (undrained): {', '.join(operator[:8])}. Run `swarm operator <phase>`"
                " to try again, or do them yourself"
            )
        telegram.notify(
            self.cfg.telegram_notify,
            msg,
            kind="finish",
            source="supervisor._finish",
        )
        self.log.line("ACTION finish")
        self._stop = True


def main(cfg: Config, adopt: bool = False) -> None:
    """Entry point of the detached supervisor process: ``swarm up`` starts a
    run, ``swarm restart`` (``adopt``) takes over the one already going."""
    Supervisor(cfg, adopt=adopt).run()
