"""The swarm cockpit.

Rebuilt. The first version composed every panel as a ``str`` and pushed it into a
``Static``, which is why it read as an undifferentiated wall of text: nothing on
screen carried meaning by its shape or its colour, so the whole thing had to be
*read* rather than *seen*. It also put every fact on one tab, so switching away
lost all situational awareness, and it showed the config as raw TOML — which is
not a control panel, it is an editor with extra steps.

What replaced it:

* a :class:`~swarm_orchestrator.tui.shell.StatusBar` that never leaves the
  screen, carrying the four facts that decide whether to look closer — alive,
  capacity, campaign progress, and whether anything is waiting on the owner;
* tabs of real widgets — bordered panels, data tables, meters — built from one
  small vocabulary in :mod:`~swarm_orchestrator.tui.theme`, so a colour means
  the same thing everywhere;
* settings as a form of typed fields rather than a text buffer;
* a command centre that discovers every ``swarm`` subcommand from the argument
  parser, so the CLI and the cockpit cannot drift apart.

Idle cost stays near zero: cheap sources are polled by mtime, the log is tailed
by byte offset, subprocess probes run on worker threads, and a repaint only
happens when something changed or a clock is genuinely ticking. Only the tab you
are looking at repaints — painting all eight cost a settings re-state and a
command-history re-sort twice a second to draw pixels nobody could see.
"""

from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Label, Static, TabbedContent, TabPane

from .dash import Dash
from .shell import StatusBar
from .theme import BAD, COLOR, MUTED, OK

TICK_S = 2.0
PROBE_S = 10.0

#: Below this many columns the needs-you drawer floats over the tab instead of
#: docking beside it: 44 columns taken out of 100 leaves a tab too narrow to read.
DRAWER_OVERLAY_COLS = 110


def _missing(name: str, err: Exception):
    """A tab that failed to import becomes an explanatory panel, not a crash.

    This is a cockpit: one broken module must cost its own tab and nothing else.
    """

    class Missing(Vertical):
        def update(self, dash) -> None:  # noqa: D102 - matches the tab contract
            pass

        def selected_phase(self):  # noqa: D102
            return None

        def compose(self) -> ComposeResult:
            yield Static(
                f"[{COLOR[BAD]}]{name} unavailable[/]\n\n"
                f"[{COLOR[MUTED]}]{type(err).__name__}: {err}[/]"
            )

    return Missing


try:
    from .home import Home
except Exception as exc:  # noqa: BLE001
    Home = _missing("home", exc)
try:
    from .tables import History, Notifications, Runs, Workers
except Exception as exc:  # noqa: BLE001
    Workers = _missing("workers", exc)
    History = _missing("history", exc)
    Notifications = _missing("notifications", exc)
    Runs = _missing("runs", exc)
try:
    from .disk import Disk
except Exception as exc:  # noqa: BLE001
    Disk = _missing("disk", exc)
try:
    from .configform import Settings
except Exception as exc:  # noqa: BLE001
    Settings = _missing("settings", exc)
try:
    from .commands import Commands
except Exception as exc:  # noqa: BLE001
    Commands = _missing("commands", exc)
try:
    from .doctor import Doctor
except Exception as exc:  # noqa: BLE001
    Doctor = _missing("doctor", exc)
try:
    from .drawer import Drawer
except Exception as exc:  # noqa: BLE001
    Drawer = None
    _DRAWER_ERR = exc


class HelpScreen(ModalScreen[None]):
    """Every key, on one screen. Keyboard-first only works if the keys are findable.

    Including the key that gets you out. This screen used to say nothing about
    how to leave and then warned that ``q`` quits the dashboard, so the one exit
    a reader could see was the one they had just been told not to take. A modal
    that traps the person who opened it to learn the keys is the worst place in
    the app to be coy.
    """

    BINDINGS = [Binding("escape,q,question_mark", "dismiss", "close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical {
        width: 74; height: auto; max-height: 90%;
        border: round #58a6ff; padding: 1 2; background: #0d1117;
    }
    """

    HELP = f"""[b]tabs[/b]
  [{COLOR[OK]}]1[/] home      [{COLOR[OK]}]2[/] workers   [{COLOR[OK]}]3[/] history   [{COLOR[OK]}]4[/] alerts
  [{COLOR[OK]}]5[/] disk      [{COLOR[OK]}]6[/] settings  [{COLOR[OK]}]7[/] commands  [{COLOR[OK]}]8[/] doctor
  [{COLOR[OK]}]9[/] runs
  [{COLOR[MUTED]}]tab / shift+tab cycle[/]

[b]anywhere[/b]
  [{COLOR[OK]}]n[/] needs-you drawer   [{COLOR[OK]}]c[/] command centre   [{COLOR[OK]}]d[/] run the doctor
  [{COLOR[OK]}]j k[/] / arrows move    [{COLOR[OK]}]enter[/] or click opens what is selected
  [{COLOR[OK]}]R[/] reset the run: ETA and usage count from now (asks first)

[b]on their own tab[/b]
  [{COLOR[OK]}]/[/] commands: filter   [{COLOR[OK]}]esc[/] clear it
  [{COLOR[OK]}]F[/] alerts: all / failed / delivered
  [{COLOR[OK]}]r[/] disk: rescan   [{COLOR[MUTED]}](it never scans on a timer)[/]

[b]settings[/b]
  [{COLOR[OK]}]enter[/] or [{COLOR[OK]}]e[/] edit   [{COLOR[OK]}]ctrl+s[/] apply   [{COLOR[OK]}]r[/] revert   [{COLOR[OK]}]d[/] dry-run

[b]commands[/b]
  [{COLOR[OK]}]enter[/] pick   [{COLOR[OK]}]f5[/] run   [{COLOR[OK]}]ctrl+r[/] re-run last   [{COLOR[OK]}]ctrl+x[/] stop

[{COLOR[MUTED]}]destructive commands confirm first — y to go, n / esc to stop[/]
[{COLOR[MUTED]}]q quits the dashboard; the swarm keeps running[/]"""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[b]keys[/b]   [{COLOR[MUTED]}]esc or ? closes this[/]")
            yield Static(self.HELP)


class DetailScreen(ModalScreen[None]):
    """A record in full, over whatever tab opened it — an Overseer pass, a job.

    Modal rather than another tab: it answers "what was that?" about one row and
    should get out of the way the moment it has, with the tab still where it was.
    """

    BINDINGS = [Binding("escape,q,enter", "dismiss", "close")]
    DEFAULT_CSS = """
    DetailScreen { align: center middle; background: #0d1117 60%; }
    DetailScreen > Vertical {
        width: 96; max-width: 96%; height: auto; max-height: 90%;
        border: round #30363d; border-title-color: #e6edf3; padding: 0 1;
        background: #161b22;
    }
    DetailScreen VerticalScroll { height: auto; max-height: 100%; }
    """

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        with Vertical() as box:
            box.border_title = self._title
            box.border_subtitle = "esc closes"
            with VerticalScroll():
                yield Static(self._body)


class SwarmApp(App):
    """The dashboard application."""

    TITLE = "swarm"
    CSS = """
    Screen { background: #0d1117; }
    TabbedContent { height: 1fr; }
    Tabs { background: #0d1117; }
    Tabs Tab { color: #8b949e; }
    Tabs Tab.-active { color: #e6edf3; text-style: bold; }
    Footer { background: #161b22; }
    #body { height: 1fr; layers: default overlay; }
    #body > TabbedContent { width: 1fr; }
    .hidden { display: none; }
    """

    BINDINGS = [
        Binding("1", "tab('home')", "home"),
        Binding("2", "tab('workers')", "workers"),
        Binding("3", "tab('history')", "history"),
        Binding("4", "tab('alerts')", "alerts"),
        Binding("5", "tab('disk')", "disk"),
        Binding("6", "tab('settings')", "settings"),
        Binding("7", "tab('commands')", "commands"),
        Binding("8", "tab('doctor')", "doctor"),
        Binding("9", "tab('runs')", "runs"),
        Binding("R", "reset_run", "reset run", show=False),
        Binding("c", "tab('commands')", "commands", show=False),
        Binding("d", "doctor", "doctor", show=False),
        Binding("n", "toggle_drawer", "needs you"),
        Binding("r", "rescan", "rescan", show=False),
        Binding("question_mark", "help", "help"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, cfg) -> None:
        super().__init__()
        self.cfg = cfg
        self.dash = Dash(cfg)
        self._busy_last = False

    def compose(self) -> ComposeResult:
        yield StatusBar(id="statusbar")
        # The drawer docks inside the body rather than the screen, so opening it
        # insets the tabs and leaves the status bar and footer whole.
        with Horizontal(id="body"):
            with TabbedContent(initial="home", id="tabs"):
                with TabPane("1 home", id="home"):
                    yield Home(id="tab-home")
                with TabPane("2 workers", id="workers"):
                    yield Workers(id="tab-workers")
                with TabPane("3 history", id="history"):
                    yield History(id="tab-history")
                with TabPane("4 alerts", id="alerts"):
                    yield Notifications(id="tab-alerts")
                with TabPane("5 disk", id="disk"):
                    yield Disk(id="tab-disk")
                with TabPane("6 settings", id="settings"):
                    yield Settings(id="tab-settings")
                with TabPane("7 commands", id="commands"):
                    yield Commands(id="tab-commands")
                with TabPane("8 doctor", id="doctor"):
                    yield Doctor(id="tab-doctor")
                with TabPane("9 runs", id="runs"):
                    yield Runs(id="tab-runs")
            if Drawer is not None:
                yield Drawer(id="drawer")
        yield Footer()

    def on_mount(self) -> None:
        self._fit_drawer(self.size.width)
        self.refresh_all()
        self.set_interval(TICK_S, self._tick)
        self.set_interval(PROBE_S, self._probe)
        self._probe()

    # -- refresh ----------------------------------------------------------
    def _tick(self) -> None:
        """Poll cheap sources; repaint only when something moved.

        Elapsed and context are the only reasons to redraw with nothing changed
        on disk, and only while something is running — so an idle, paused or
        finished swarm repaints nothing at all.
        """
        changed = self.dash.poll()
        busy = any(s.busy for s in self.dash.snapshot.slots)
        if changed or busy or busy != self._busy_last:
            self.refresh_all()
        self._busy_last = busy

    @work(thread=True, exclusive=True, group="probe")
    def _probe(self) -> None:
        try:
            self.dash.probe()
        except Exception:  # noqa: BLE001 - a probe must never take the app down
            return
        self.call_from_thread(self.refresh_all)

    def refresh_all(self) -> None:
        """Repaint the status bar and the one tab that is actually on screen.

        Every tab used to repaint on every tick, which cost a 32-row settings
        re-state and a command-history re-sort twice a second to draw things
        nobody could see. Tabs render from ``Dash`` rather than accumulating,
        so a hidden one loses nothing by being skipped — it is repainted on the
        way in by ``on_tabbed_content_tab_activated``.
        """
        try:
            self.query_one(StatusBar).update_from(self.dash)
        except Exception as exc:  # noqa: BLE001
            self.log(f"statusbar: {exc}")
        # The drawer is not a tab, and it has to run every tick even while shut:
        # deciding whether a blocker is new enough to toast is the whole point.
        if Drawer is not None:
            self._repaint(self.query_one(Drawer))
        self._repaint(self.active_tab)

    def _repaint(self, node) -> None:
        update = getattr(node, "update", None)
        if update is None:
            return
        try:
            update(self.dash)
        except Exception as exc:  # noqa: BLE001
            self.log(f"panel {type(node).__name__}: {exc}")

    def on_tabbed_content_tab_activated(self, _event) -> None:
        # The tab being switched to has been missing ticks; catch it up before
        # it is seen, or the first refresh after a switch shows stale numbers.
        self.call_after_refresh(lambda: self._repaint(self.active_tab))

    def on_resize(self, event) -> None:
        self._fit_drawer(event.size.width)

    def _fit_drawer(self, width: int) -> None:
        if Drawer is None:
            return
        try:
            self.query_one(Drawer).set_class(0 < width < DRAWER_OVERLAY_COLS, "-overlay")
        except Exception:  # noqa: BLE001 - not composed yet
            pass

    # -- helpers ----------------------------------------------------------
    @property
    def active_tab(self):
        pane = self.query_one("#tabs", TabbedContent).active_pane
        return pane.children[0] if pane and pane.children else None

    def selected_phase(self) -> str | None:
        """Whatever the active tab has selected, else the first thing blocking."""
        tab = self.active_tab
        got = getattr(tab, "selected_phase", None)
        if got:
            try:
                phase = got()
                if phase:
                    return phase
            except Exception:  # noqa: BLE001
                pass
        snap = self.dash.snapshot
        if snap.blockers:
            return snap.blockers[0].phase
        for slot in snap.slots:
            if slot.busy and slot.phase:
                return slot.phase
        return None

    # -- actions ----------------------------------------------------------
    def action_tab(self, name: str) -> None:
        self.query_one("#tabs", TabbedContent).active = name

    def check_action(self, action: str, parameters):
        # `r` belongs to settings (revert) and to disk (rescan). Only claim it
        # for disk, and only while disk is the tab in front of you.
        if action == "rescan":
            return isinstance(self.active_tab, Disk)
        return True

    def action_rescan(self) -> None:
        node = self.active_tab
        rescan = getattr(node, "rescan", None)
        if rescan is not None:
            rescan()

    def action_toggle_drawer(self) -> None:
        if Drawer is not None:
            self.query_one(Drawer).toggle()

    # -- drilling in ------------------------------------------------------
    def open_phase(self, phase: str, slot: int | None) -> None:
        """A row was clicked or entered. Show the fullest view of that phase.

        A phase held by a live slot has a worker worth looking at; anything
        else only exists as history. This is what the row cursors were always
        for — until now they selected things nothing could act on.
        """
        tab, node = ("workers", "#tab-workers") if slot is not None else ("history", "#tab-history")
        self.action_tab(tab)
        # Paint it now: a tab that was never shown has no rows yet, and the
        # cursor cannot land on a row that is not there.
        self._repaint(self.query_one(node))
        focus = getattr(self.query_one(node), "focus_row", None)
        if focus is not None:
            self.call_after_refresh(lambda: focus(phase))

    def on_open_phase(self, event) -> None:
        event.stop()
        self.open_phase(event.phase, event.slot)

    def on_home_open_phase(self, event) -> None:
        event.stop()
        self.open_phase(event.phase, event.slot)

    def on_home_open_text(self, event) -> None:
        event.stop()
        self.push_screen(DetailScreen(event.title, event.body))

    def on_open_detail(self, event) -> None:
        """``enter`` on a table row: the tab's detail, full height, readable."""
        event.stop()
        self.push_screen(DetailScreen(event.title, event.body))

    def action_doctor(self) -> None:
        """Run the health checks and show them, from whichever tab you were on."""
        self.action_tab("doctor")
        node = self.query_one("#tab-doctor")
        run = getattr(node, "run_checks", None)
        if run is not None:
            run()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_reset_run(self) -> None:
        """``R``: close the open run and start a fresh one, after the usual confirm."""
        from .commands import ConfirmRun, destructive_reason

        def answered(go: bool | None) -> None:
            if not go:
                self.notify("run not reset")
                return
            try:
                rec = reset_run(self.cfg)
            except Exception as exc:  # noqa: BLE001 - say it, never crash the cockpit
                self.notify(f"reset failed: {exc}", severity="error")
                return
            self.notify(f"run {rec['run_id']} started — ETA and usage count from now")
            self.dash.poll()
            self.refresh_all()

        self.push_screen(ConfirmRun("swarm reset", destructive_reason("reset", [])), answered)


def reset_run(cfg) -> dict:
    """What ``R`` does — ``swarm reset``, in-process (it is a few file writes)."""
    from ..cli import open_run

    return open_run(cfg, "reset")[0]


def main(cfg) -> int:
    """Run the dashboard. Returns a process exit code."""
    try:
        SwarmApp(cfg).run()
    except Exception as exc:  # noqa: BLE001
        import sys

        print(f"swarm tui: {exc}", file=sys.stderr)
        return 2
    return 0
