"""The dashboard's data holder: everything on disk, re-read only when it changed.

Split out of ``app.py`` so every tab can consume it without importing the app,
and without the import cycle that would create. The presentation layer around it
was rebuilt; this part was already right and is unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import opqueue
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
    fmt_ago,
    fmt_clock,
    fmt_duration,
    fmt_stamp,
    integration_holds,
    launch_times,
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
        self.contexts: dict[str, float] = {}  # pane_id -> context percent
        self.tails: dict[str, str] = {}  # pane_id -> last visible lines
        self.repos: dict[str, probes.RepoStat] = {}  # phase -> commits/dirty
        self.meters: dict[str, Meter] = {}  # phase -> its worker's status-line figures
        self.limits: Limits | None = None
        self._mtimes: dict[str, float] = {}
        self._graph_mtime: float | None = None

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
        # The tap replaces its file atomically, so every write moves the dir.
        if self._changed("meters", self.meters_dir):
            self.meters = load_meters(self.meters_dir)
            self.limits = load_limits(self.meters, self.meters_dir / LIMITS_LOG)
            changed.add("meters")
        if self._changed("ledger", Path(self.cfg.project_dir) / self.cfg.ledger):
            self.graph = load_graph(self.cfg)
            changed.add("ledger")
        if changed & {"state", "log", "notifications", "done", "recaps", "ledger",
                      "notes", "operator"}:
            self._rebuild()
        return changed

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

    def probe(self) -> None:
        """Refresh the live probes. Runs on a worker thread — never on the UI."""
        self.agents = probes.agents()
        self.panes = probes.panes()
        main = getattr(self.cfg, "git_main_branch", "master")
        contexts: dict[str, float] = {}
        tails: dict[str, str] = {}
        repos: dict[str, probes.RepoStat] = {}
        for slot in self.snapshot.slots:
            if not slot.busy or not slot.pane_id:
                continue
            text = probes.capture(slot.pane_id)
            tails[slot.pane_id] = text
            parsed = probes.parse_context(text)
            if parsed:
                contexts[slot.pane_id] = parsed[2]
            if slot.phase and slot.worktree:
                repos[slot.phase] = probes.repo_stat(slot.worktree, main)
        self.contexts, self.tails, self.repos = contexts, tails, repos

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
                    self.contexts.get(slot.pane_id or ""),
                    pane,
                )
            )
        return rows


# -- modal screens --------------------------------------------------------
