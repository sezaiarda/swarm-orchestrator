"""The dashboard's data holder: everything on disk, re-read only when it changed.

Split out of ``app.py`` so every tab can consume it without importing the app,
and without the import cycle that would create. The presentation layer around it
was rebuilt; this part was already right and is unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import opqueue
from .. import runs as runs_mod
from .. import usage as usage_mod
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
    load_graph,
    run_started_at,
    spark,
    utilisation,
)


#: Per-slot ``git rev-list`` + ``git status`` run at most this often. The probe
#: ticks every 10 s, and on WSL two git walks per busy worktree per tick was a
#: steady CPU and filesystem-lock cost for a commit count that moves per minutes.
REPO_PROBE_S = 30.0

#: The run's usage summary is re-derived at least this often (its $/h moves with
#: every render of every worker), and at once when a limit sample or the run moves.
USAGE_EVERY_S = 60.0


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
        self.sentinels: dict = {}
        self.recaps: dict = {}
        self.notes: dict = {}
        self.operator: list[opqueue.Item] = []
        self.history: list[PhaseRun] = []
        self.graph: dict = {}
        self.agents: list = []
        self.panes: dict = {}
        self.tails: dict[str, str] = {}  # pane_id -> last visible lines
        self.repos: dict[str, probes.RepoStat] = {}  # phase -> commits/dirty
        self.meters: dict[str, Meter] = {}  # phase -> its worker's status-line figures
        self.limits: Limits | None = None
        # The open run (``None`` = a state dir from before runs), the phase runs
        # its ETA is made from, and whether those had to be borrowed from history.
        self.run: dict | None = None
        self.eta_runs: list[PhaseRun] = []
        self.eta_from_history = False
        #: The open run's usage summary (legacy period when no run is open).
        self.usage: dict | None = None
        #: Closed runs' stored summaries, newest first — the runs tab.
        self.past_runs: list[dict] = []
        self._samples = usage_mod.SampleTail(cfg.state_dir / METERS_DIR / LIMITS_LOG)
        self._all_meters: dict[str, Meter] = {}
        self._usage_at = 0.0
        self._mtimes: dict[str, float] = {}
        self._graph_mtime: float | None = None
        self._repos_at = 0.0  # when the git half of the probe last ran

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
            self._all_meters = load_meters(self.meters_dir)
            changed.add("meters")
        if changed & {"meters", "run"}:
            # Only phases active in this run feed the live views; a busy one is
            # kept even if it has not rendered since the epoch.
            active = {s.phase for s in self.snapshot.slots if s.busy and s.phase}
            active |= {b.phase for b in self.snapshot.blockers if b.phase}
            self.meters = live_meters(self._all_meters, self.epoch, active)
            self.limits = load_limits(self.meters, self.meters_dir / LIMITS_LOG)
        if self._changed("ledger", Path(self.cfg.project_dir) / self.cfg.ledger):
            self.graph = load_graph(self.cfg)
            changed.add("ledger")
        if changed & {"state", "log", "notifications", "done", "recaps", "ledger",
                      "notes", "operator", "run"}:
            self._rebuild()
        grew = self._samples.poll()
        now = time.time()
        if grew or changed & {"run", "log"} or now - self._usage_at >= USAGE_EVERY_S:
            self._usage_at = now
            self.usage = self._run_usage(now)
            changed.add("usage")
        return changed

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

    def _rebuild(self) -> None:
        events = self.tail.events
        self.snapshot = build_snapshot(
            self.cfg,
            read_state(self.cfg),
            graph=self.graph,
            launch_times=launch_times(events),
            questions=question_index(self.notifications, self.sentinels),
            started_at=run_started_at(events),
            operator=self.operator,
        )
        self.history = build_history(
            events, self.sentinels, self.recaps, self.cfg.done_dir, self.notes
        )
        self.eta_runs, self.eta_from_history = eta_sample(self.history, self.epoch)

    def probe(self, now: float | None = None) -> None:
        """Refresh the live probes. Runs on a worker thread — never on the UI.

        ``capture-pane`` stays: the pane's last lines are the activity preview.
        Context no longer comes from scraping it — the meters tap writes the
        exact figure (:meth:`context_pct`). The git half runs every
        :data:`REPO_PROBE_S`, or at once for a busy phase it has not seen yet.
        """
        now = time.time() if now is None else now
        self.agents = probes.agents()
        self.panes = probes.panes()
        main = getattr(self.cfg, "git_main_branch", "master")
        busy = [s for s in self.snapshot.slots if s.busy and s.pane_id]
        self.tails = {s.pane_id: probes.capture(s.pane_id) for s in busy}
        wanted = {s.phase: s.worktree for s in busy if s.phase and s.worktree}
        if now - self._repos_at >= REPO_PROBE_S or set(wanted) - set(self.repos):
            self._repos_at = now
            self.repos = {p: probes.repo_stat(wt, main) for p, wt in wanted.items()}
        else:
            self.repos = {p: r for p, r in self.repos.items() if p in wanted}

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


# -- modal screens --------------------------------------------------------
