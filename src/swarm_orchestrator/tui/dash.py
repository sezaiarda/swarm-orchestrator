"""The dashboard's data holder: everything on disk, re-read only when it changed.

Split out of ``app.py`` so every tab can consume it without importing the app,
and without the import cycle that would create. The presentation layer around it
was rebuilt; this part was already right and is unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import bigpic, opqueue, ovrecord
from .. import pace as pace_mod
from .. import runs as runs_mod
from .. import state as state_mod
from .. import telegram
from .. import todo as todo_mod
from .. import usage as usage_mod
from ..eta import engine as eta_mod
from ..meters import LIMITS_LOG, METERS_DIR
from . import probes
from .data import (
    Blocker,
    Limits,
    LogTail,
    Meter,
    PhaseRun,
    Snapshot,
    bar,
    build_history,
    build_snapshot,
    completion_density,
    completions_series,
    eta_sample,
    finish_times,
    fmt_ago,
    fmt_clock,
    fmt_duration,
    fmt_stamp,
    integration_holds,
    launch_times,
    live_meters,
    load_attempts,
    load_all_notes,
    load_limits,
    load_meters,
    load_notes,
    load_notifications,
    load_recaps,
    load_sentinels,
    occupancy_series,
    phase_durations,
    question_index,
    read_state,
    load_deferred,
    load_graph,
    load_kept,
    load_ticked,
    run_started_at,
    spark,
    utilisation,
)


#: Per-slot ``git rev-list`` + ``git status`` run at most this often. The probe
#: ticks every 10 s, and on WSL two git walks per busy worktree per tick was a
#: steady CPU and filesystem-lock cost for a commit count that moves per minutes.
REPO_PROBE_S = 30.0
#: How often the owner's to-do list (:mod:`todo`) is re-read, on the probe thread.
TODO_PROBE_S = 30.0
#: ``claude agents --json`` runs at most this often, or when the busy phases
#: change: each call starts a Claude Code CLI (and, under WSL, two ``reg.exe``),
#: the costliest thing the dashboard ran, five times a minute.
AGENTS_PROBE_S = 60.0

#: The run's usage summary is re-derived at least this often (its $/h moves with
#: every render of every worker), and at once when a limit sample or the run moves.
USAGE_EVERY_S = 60.0

#: A kept process can die without touching its record, so the records are
#: re-read this often even when ``<state>/keep/`` has not moved.
KEEP_RECHECK_S = 10.0

#: Overseer pass records kept for the home feed and the needs-you strip (the web
#: board shows the same dozen).
PASSES = 12


class Dash:
    """Everything on disk, re-read only when it changed.

    Holds one :class:`~swarm_orchestrator.tui.data.LogTail` (byte-offset
    incremental) plus an mtime per cheap source. :meth:`poll` returns the set of
    sources that actually changed, and the UI uses that to decide what to
    repaint — the whole reason an idle dashboard costs nothing.
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.tail = LogTail(cfg.supervisor_log)
        self.snapshot = Snapshot()
        self.notifications: list = []
        #: When the owner last acknowledged the dropped pings (0 = never).
        self.pings_acked_at = 0.0
        self.sentinels: dict = {}
        self.recaps: dict = {}
        self.notes: dict = {}
        self.operator: list[opqueue.Item] = []
        self.history: list[PhaseRun] = []
        self.graph: dict = {}
        self.ticked: set[str] = set()
        self.agents: list = []
        self.panes: dict = {}
        self.tails: dict[str, str] = {}  # pane_id -> last visible lines
        self.repos: dict[str, probes.RepoStat] = {}  # phase -> commits/dirty
        self.meters: dict[str, Meter] = {}  # phase -> its worker's status-line figures
        self.limits: Limits | None = None
        # The open run (``None`` = a state dir from before runs) and the phase
        # runs a running phase's own ETA is made from.
        self.run: dict | None = None
        self.eta_runs: list[PhaseRun] = []
        #: The project's git history of ticks and worker counts (:mod:`pace`).
        self.ledger_history = pace_mod.History()
        #: Every done phase -> when it finished, wherever it was built; the
        #: chart reads this, never this machine's log alone.
        self.finished: dict[str, float] = {}
        self.bulk: set[str] = set()
        #: Open rows whose ``after:`` date is still ahead, and the UTC day it was read.
        self.deferred: dict[str, str] = {}
        self._deferred_day = ""
        #: The forecast worker (:mod:`swarm_orchestrator.eta`); views read
        #: :attr:`forecast`, never wait on it.
        self.eta = eta_mod.shared_engine(cfg)
        self._eta_seen = 0
        self._eta_asked = 0
        self._state: dict | None = None
        #: The open run's usage summary (legacy period when no run is open).
        self.usage: dict | None = None
        #: Each usage window's points per busy worker-hour, as the forecast burns
        #: them (:func:`eta.engine.burn_of`) — the usage box's projection.
        self.burn: dict[str, float] = {}
        #: Closed runs' stored summaries, newest first — the runs tab.
        self.past_runs: list[dict] = []
        #: Recent Overseer passes, newest first — the feed and the needs-you strip.
        self.passes: list = []
        #: Campaign name -> its one-line "what it is", from the ledger headings.
        self.campaign_what: dict[str, str] = {}
        #: ``swarm keep`` records, alive or dead, by name — the shells tab.
        self.kept: list = []
        self._kept_at = 0.0
        #: The big-picture pass in a few words, for the headline.
        self.big_picture = ""
        self._bigpic = bigpic.Memory()
        self._live_pass: tuple[str, ...] = ()
        self._passes_live: object = ()
        self._samples = usage_mod.SampleTail(cfg.state_dir / METERS_DIR / LIMITS_LOG)
        self._all_meters: dict[str, Meter] = {}
        self._meter_files: dict = {}
        self._usage_at = 0.0
        self._mtimes: dict[str, float] = {}
        self._graph_mtime: float | None = None
        self._repos_at = 0.0  # when the git half of the probe last ran
        self._agents_at = 0.0
        self._agents_for: frozenset = frozenset()
        #: The owner's to-dos (not questions): the ``g`` hint and the alerts box.
        self.todos: list = []
        self._todos_at = 0.0

    @property
    def notifications_path(self) -> Path:
        return self.cfg.state_dir / "notifications.jsonl"

    @property
    def recaps_dir(self) -> Path:
        return self.cfg.state_dir / "recaps"

    @property
    def notes_dir(self) -> Path:
        return self.cfg.state_dir / "notes"

    @property
    def operator_dir(self) -> Path:
        return self.cfg.operator_dir

    @property
    def meters_dir(self) -> Path:
        return self.cfg.state_dir / METERS_DIR

    @property
    def config_path(self) -> Path:
        return Path(self.cfg.project_dir) / ".swarm.toml"

    def _changed(self, key: str, path: Path) -> bool:
        """Whether ``path``'s mtime+size moved since the last check.

        Size is part of the key because a log appended to twice inside one
        filesystem mtime granularity would otherwise look unchanged.
        """
        try:
            stat = path.stat()
            stamp = stat.st_mtime + stat.st_size
        except OSError:
            stamp = -1.0
        if self._mtimes.get(key) == stamp:
            return False
        self._mtimes[key] = stamp
        return True

    def poll(self) -> set[str]:
        """Re-read whatever moved. Returns the changed source names."""
        changed: set[str] = set()
        if self._changed("state", self.cfg.state_path):
            changed.add("state")
        if self.tail.poll():
            changed.add("log")
        if self._changed("notifications", self.notifications_path):
            self.notifications = load_notifications(self.notifications_path)
            changed.add("notifications")
        if self._changed("pings-ack", self.cfg.state_dir / telegram.ACK_NAME):
            self.pings_acked_at = telegram.acked_at(self.cfg.state_dir)
            changed.add("notifications")
        if self._changed("done", self.cfg.done_dir):
            self.sentinels = load_sentinels(self.cfg.done_dir)
            changed.add("done")
        if self._changed("recaps", self.recaps_dir):
            self.recaps = load_recaps(self.recaps_dir)
            changed.add("recaps")
        if self._changed("notes", self.notes_dir):
            self.notes = load_all_notes(self.notes_dir)
            changed.add("notes")
        if self._changed("operator", self.operator_dir):
            self.operator = opqueue.load_all(self.cfg)
            changed.add("operator")
        history_dir = runs_mod.history_dir(self.cfg.state_dir)
        if self._changed("run", history_dir / runs_mod.CURRENT):
            self.run = runs_mod.current(self.cfg.state_dir)
            self.past_runs = usage_mod.past_summaries(self.cfg.state_dir, 50)
            changed.add("run")
        # The tap replaces its file atomically, so every write moves the dir.
        if self._changed("meters", self.meters_dir):
            self._all_meters = load_meters(self.meters_dir, self._meter_files)
            changed.add("meters")
        if changed & {"meters", "run"}:
            # Only phases active in this run feed the live views; a busy one is
            # kept even if it has not rendered since the epoch.
            active = {s.phase for s in self.snapshot.slots if s.busy and s.phase}
            active |= {b.phase for b in self.snapshot.blockers if b.phase}
            self.meters = live_meters(self._all_meters, self.epoch, active)
            self.limits = load_limits(self.meters, self.meters_dir / LIMITS_LOG)
        ledger_moved = self._changed("ledger", Path(self.cfg.project_dir) / self.cfg.ledger)
        if ledger_moved:
            self.graph = load_graph(self.cfg)
            self.ticked = load_ticked(self.cfg)
            self.campaign_what = campaign_lines(self.cfg)
            changed.add("ledger")
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if ledger_moved or today != self._deferred_day:
            self._deferred_day = today
            self.deferred = load_deferred(self.cfg)
            changed.add("ledger")
        # A tick or a worker-count change lands as a commit; git is asked only
        # when one of the two files moved, and answers from a cache keyed on HEAD.
        if self._changed("config", self.config_path) | ledger_moved:
            self.ledger_history = pace_mod.load(self.cfg)
            changed.add("ledger")
        if changed & {"state", "log", "notifications", "done", "recaps", "ledger",
                      "notes", "operator", "run"}:
            self._rebuild()
        if self._overseer_moved():
            self.passes = ovrecord.load_passes(self.cfg, limit=PASSES, live=self._live_pass)
            self._passes_live = self._live_pass
            changed.add("overseer")
        now = time.time()
        if self._poll_kept(now):
            changed.add("keep")
        if self._changed("bigpic", bigpic.memory_path(self.cfg)):
            self._bigpic = bigpic.load(self.cfg)
        text = bigpic.short_text(self._bigpic, now)
        if text != self.big_picture:
            self.big_picture = text
            changed.add("bigpic")
        grew = self._samples.poll()
        if grew or changed & {"run", "log"} or now - self._usage_at >= USAGE_EVERY_S:
            self._usage_at = now
            self.usage = self._run_usage(now)
            try:
                self.burn = eta_mod.burn_of(self.tail.events, int(self.cfg.max_workers or 1),
                                            self._samples.samples, now)
            except Exception:  # noqa: BLE001 - a projection must never take the dash down
                self.burn = {}
            changed.add("usage")
        self._ask_eta(now)
        if self.eta.version != self._eta_seen:
            self._eta_seen = self.eta.version
            changed.add("eta")
        return changed

    @property
    def samples(self) -> list:
        """``limits.jsonl`` as :class:`usage.Sample` rows, oldest first."""
        return self._samples.samples

    @property
    def usage_state(self) -> tuple[dict, dict]:
        """``(usage_hold, usage_override)`` from the state: which windows a cap
        holds right now, and which the owner chose to run through."""
        st = self._state if isinstance(self._state, dict) else {}
        hold, over = st.get("usage_hold"), st.get("usage_override")
        return (hold if isinstance(hold, dict) else {}, over if isinstance(over, dict) else {})

    @property
    def forecast(self):
        """The latest :class:`~swarm_orchestrator.eta.forecast.Forecast`, or
        ``None`` before the first one has been made."""
        return self.eta.result

    def _ask_eta(self, now: float) -> None:
        """Hand the forecast worker what moved, when a forecast is due
        (:class:`eta.engine.Gate`); it gathers and simulates on its own thread,
        so this is a few tuple comparisons on the render path."""
        state = self._state
        if not isinstance(state, dict) or not self.eta.drives(self):
            return
        busy = any(s.busy for s in self.snapshot.slots)
        every = eta_mod.RECOMPUTE_S if busy else eta_mod.IDLE_RECOMPUTE_S
        real, soft = self._eta_inputs(state, now)
        if not self.eta.gate.due(real, soft, now, every):
            return
        self._eta_asked += 1
        stamp = (id(self), self._eta_asked)
        cfg, events, history = self.cfg, self.tail.events, self.history
        ledger_history, samples = self.ledger_history, self._samples.samples

        def make():
            return eta_mod.gather(cfg, state_mod.State.from_dict(state), events=events,
                                  history=history, ledger_history=ledger_history,
                                  usage=samples, now=time.time())

        self.eta.request(stamp, make)

    def _eta_inputs(self, state: dict, now: float) -> tuple[tuple, tuple]:
        """``(real, soft)`` for :meth:`eta.engine.Gate.due`: what a forecast is
        made from, as this dash already holds it, split by how soon a change
        must show."""
        done = state.get("done") if isinstance(state.get("done"), dict) else {}
        busy = frozenset(s.phase for s in self.snapshot.slots if s.busy and s.phase)
        real = (self._mtimes.get("ledger"), frozenset(done.items()), busy)
        usage = tuple(eta_mod.usage_key(usage_mod.latest(self._samples.samples, w, now))
                      for w in ("week", "five"))

        def names(key: str) -> tuple:
            got = state.get(key)
            return tuple(sorted(map(str, got))) if isinstance(got, (dict, list)) else ()

        soft = (names("parked"), names("waiting"), names("integ_queue"),
                str(state.get("integ_blocked") or ""), bool(state.get("paused")),
                bool(state.get("drain")), state.get("pause_at"), names("usage_hold"),
                names("usage_override"), self._mtimes.get("config"), self._deferred_day,
                usage)
        return real, soft

    def _poll_kept(self, now: float) -> bool:
        """Re-read the kept records when their dir moved, or a death may have gone unseen.

        Returns whether the set of names or any of their liveness changed.
        """
        moved = self._changed("keep", self.cfg.state_dir / "keep")
        if not moved and now - self._kept_at < KEEP_RECHECK_S:
            return False
        self._kept_at = now
        before = [(r.name, r.alive) for r in self.kept]
        self.kept = load_kept(self.cfg)
        return moved or before != [(r.name, r.alive) for r in self.kept]

    @property
    def epoch(self) -> float | None:
        """The open run's epoch, or ``None`` before runs were recorded."""
        return float(self.run["epoch_ts"]) if self.run else None

    def _run_usage(self, now: float) -> dict | None:
        """The open run summarised to now; before runs, since the last supervisor start."""
        rec = self.run
        if rec is None:
            began = run_started_at(self.tail.events)
            if began is None:
                return None
            rec = {"run_id": None, "epoch_ts": began}
        try:
            return usage_mod.summarize(
                rec, now, samples=self._samples.samples, events=self.tail.events,
                sessions=usage_mod.load_sessions(self.meters_dir))
        except Exception:  # noqa: BLE001 - a usage figure must never take the dash down
            return None

    def _overseer_moved(self) -> bool:
        """Whether the Overseer's records moved, or which pass is live did.

        A pass rewrites its JSON by atomic replace (which moves the directory)
        but its session edits the ``.md`` mirror in place (which does not), so
        the newest mirror is stat'ed too — one listdir, and only while the
        directory exists at all.
        """
        odir = ovrecord.overseer_dir(self.cfg)
        moved = self._changed("overseer", odir)
        if odir.is_dir():
            newest = max(odir.glob("*.md"), default=None)
            if newest is not None:
                moved |= self._changed("overseer-md", newest)
        return moved or self._live_pass != self._passes_live

    def _rebuild(self) -> None:
        events = self.tail.events
        state = read_state(self.cfg)
        self._state = state if isinstance(state, dict) else None
        st = state if isinstance(state, dict) else {}
        # A pass parked on the owner is alive too, in a window of its own.
        parked = {ident for kind, ident in map(state_mod.waiter, st.get("parked") or [])
                  if kind == state_mod.OVERSEER}
        live = {st["overseer_pass"]} if st.get("overseer_pass") else set()
        self._live_pass = tuple(sorted(parked | live))
        self.snapshot = build_snapshot(
            self.cfg,
            state,
            graph=self.graph,
            launch_times=launch_times(events),
            questions=question_index(self.notifications, self.sentinels),
            started_at=run_started_at(events),
            operator=self.operator,
            ticked=self.ticked,
            deferred=self.deferred,
        )
        self.history = build_history(
            events, self.sentinels, self.recaps, self.cfg.done_dir, self.notes,
            state=state if isinstance(state, dict) else None, ticked=self.ticked,
        )
        self.eta_runs, _ = eta_sample(self.history, self.epoch)
        self.finished, self.bulk = finish_times(
            self.snapshot.landed, self.ticked, self.ledger_history, self.history)

    def probe(self, now: float | None = None) -> None:
        """Refresh the live probes. Runs on a worker thread — never on the UI.

        ``capture-pane`` stays: the pane's last lines are the activity preview.
        Context no longer comes from scraping it — the meters tap writes the
        exact figure (:meth:`context_pct`). The git half runs every
        :data:`REPO_PROBE_S`, or at once for a busy phase it has not seen yet.
        """
        now = time.time() if now is None else now
        busy = [s for s in self.snapshot.slots if s.busy and s.pane_id]
        running = frozenset(s.phase for s in busy)
        if now - self._agents_at >= AGENTS_PROBE_S or running != self._agents_for:
            self._agents_at, self._agents_for = now, running
            self.agents = probes.agents()
        self.panes = probes.panes()
        main = getattr(self.cfg, "git_main_branch", "master")
        self.tails = {s.pane_id: probes.capture(s.pane_id) for s in busy}
        wanted = {s.phase: s.worktree for s in busy if s.phase and s.worktree}
        if now - self._repos_at >= REPO_PROBE_S or set(wanted) - set(self.repos):
            self._repos_at = now
            self.repos = {p: probes.repo_stat(wt, main) for p, wt in wanted.items()}
        else:
            self.repos = {p: r for p, r in self.repos.items() if p in wanted}
        if now - self._todos_at >= TODO_PROBE_S:
            self._todos_at = now
            try:
                self.todos = todo_mod.collect(self.cfg).items
            except Exception:  # noqa: BLE001 - a count must never take the probe down
                pass

    def context_pct(self, phase: str | None) -> float | None:
        """A worker's context use in percent, from its meters file."""
        m = self.meters.get(phase or "")
        if m is None or not m.context_tokens or not m.context_window:
            return None
        return min(100.0, 100.0 * m.context_tokens / m.context_window)

    def slot_rows(self) -> list[tuple]:
        """Slots joined with live probe data: ``(slot, live_status, waiting_for, ctx)``."""
        rows = []
        for slot in self.snapshot.slots:
            agent = probes.match_agent(self.agents, slot.phase, slot.worktree)
            pane = self.panes.get(slot.pane_id or "")
            if agent is not None:
                status = agent.status
            elif slot.busy and pane is not None and not pane.alive:
                # state says busy, but nothing is running in the pane — the exact
                # shape of a stall that otherwise looks perfectly healthy.
                status = "gone"
            elif slot.busy:
                status = "unknown"
            else:
                status = "idle"
            rows.append(
                (
                    slot,
                    status,
                    agent.waiting_for if agent else "",
                    self.context_pct(slot.phase),
                    pane,
                )
            )
        return rows


def campaign_lines(cfg) -> dict[str, str]:
    """``campaign -> what it is`` from the ledger headings, the web board's way.

    Borrowed from :mod:`~swarm_orchestrator.web.campaigns` rather than written
    twice, so the TUI's headline and the board's campaign card say the same
    sentence. Runs only when the ledger moves (~20 ms on a 5,000-line ledger).
    """
    try:
        from ..web import campaigns, rows

        path = Path(cfg.project_dir) / cfg.ledger
        parsed, _ = rows.parse(path.read_text(encoding="utf-8", errors="replace"))
        adr = campaigns.adr_titles(path.parent / "adr") or campaigns.adr_titles(
            Path(cfg.project_dir) / "docs" / "adr")
        return {name: meta.what for name, meta in campaigns.describe(parsed, adr).items()
                if meta.what}
    except Exception:  # noqa: BLE001 - a missing sentence is not worth a dead dash
        return {}


# -- modal screens --------------------------------------------------------
