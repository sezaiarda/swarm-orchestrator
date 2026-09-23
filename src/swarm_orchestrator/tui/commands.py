"""The command centre: every ``swarm`` subcommand, runnable from the cockpit.

The list is not written down anywhere in this file. It is read off
``cli._build_parser()`` every time the tab is built, the same way
``cli._known_commands`` feeds the prompt linter, so a subcommand added to the CLI
appears here without anyone remembering to add it — and one that is removed
cannot linger as a button that answers ``invalid choice``. A hand-kept menu is a
second copy of the CLI, and second copies drift.

Running a command means running *that* CLI: ``python -m swarm_orchestrator``
from the interpreter drawing this tab, pointed at the dashboard's project. Not
whatever ``swarm`` is first on PATH — that can be a different install from the
one whose parser produced the list, and then the cockpit offers commands its own
binary rejects. It goes through the real process boundary, rather than calling
``cmd_*`` in-process, for the reason ``probes.swarm`` gives: the flock, the FIFO
poke and every printed warning are the CLI's behaviour, and a second
implementation of them would drift silently.

Three things keep this from being a foot-gun bolted to a dashboard:

* **It never blocks the UI.** The subprocess runs on a worker thread and its
  output is streamed into the log line by line; ``ctrl+x`` stops it. A ``gc`` or
  a ``build`` that takes minutes costs this tab its log panel, not the cockpit.
* **Destructive commands confirm first.** :data:`DESTRUCTIVE` is the one list of
  what cannot be taken back, each with the reason shown in the prompt — a
  confirm that only says "are you sure?" teaches the reflexive ``y``.
* **What ran is kept.** Recent runs, their exit status and how long they took
  stay on screen, so "did that ``retry`` actually work" is answered by looking,
  and ``ctrl+r`` repeats the last one.
"""

from __future__ import annotations

import argparse
import os
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

from rich.markup import escape
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label, RichLog, Static

from .. import cli
from .data import fmt_ago, fmt_duration
from .tables import cell, clip, set_border, set_text
from .theme import (
    ACCENT,
    BAD,
    COLOR,
    INFO,
    MUTED,
    OK,
    WARN,
    Body,
    Panel,
    field,
    paint,
    token,
)

#: Not offered even though the parser has them. ``tui`` is this program: a second
#: cockpit started on a pipe instead of a terminal has nowhere to draw. The
#: ``_``-prefixed commands are the supervisor's own plumbing and are skipped by
#: the same rule ``cli._known_commands`` uses.
HIDDEN = frozenset({"tui"})

#: Positionals that name a phase, and so are prefilled from what the app has
#: selected. ``free`` takes "a slot id or a phase" — a phase is the common case.
PHASE_ARGS = ("phase", "phases", "target")

#: Everything that throws work away or cannot be taken back, read off what each
#: ``cmd_*`` in ``cli.py`` actually does: ``(flag, why)``. ``flag`` is None when
#: the command is always destructive, or the one flag that makes it so — ``gc``
#: is a dry run until ``--yes``, and confirming a dry run would only teach the
#: owner to press ``y`` without reading. Deliberately absent: ``pause`` /
#: ``resume`` / ``layout`` / ``reload`` (undone by running the opposite), and
#: ``launch`` / ``operator`` (they start work; they do not end any).
DESTRUCTIVE: dict[str, tuple[str | None, str]] = {
    "down": (None, "stops the supervisor and tears down the tmux session — every live worker dies"),
    "finish": (None, "stops the supervisor; --force also ends a live operator session"),
    "free": (None, "releases the slot whatever its worker is doing — that work is orphaned"),
    "skip": (None, "marks the phase done without running it and closes its waiting window"),
    "done": (None, "writes the phase's sentinel — the run treats it as finished for good"),
    "retry": (None, "deletes the phase's sentinels and discards swarm/<phase> unless --keep-branch"),
    "integrate": (None, "merges swarm/<phase> into main"),
    "operator-done": (None, "settles the job and ends its operator session"),
    "gc": ("--yes", "deletes files from disk"),
    "reset": (None, "closes the open run and starts a fresh one — ETA and usage count from "
                    "now; the closed run keeps its summary under `swarm usage`"),
}

#: How many runs are kept, and how many the "recent" panel shows.
HISTORY_KEEP = 50
HISTORY_SHOWN = 8

#: Lines of output the log keeps; ``gc -v`` alone can print thousands.
OUTPUT_KEEP = 2000

#: How long ``ctrl+x`` waits after SIGTERM before it stops asking.
STOP_GRACE_S = 3.0


# -- discovery -------------------------------------------------------------
@dataclass(frozen=True)
class Arg:
    """One argument of a subcommand, as argparse describes it."""

    name: str
    help: str
    positional: bool
    required: bool


@dataclass(frozen=True)
class CommandSpec:
    """One subcommand: what it is called, what it does, what it takes."""

    name: str
    help: str
    usage: str
    args: tuple[Arg, ...] = ()
    phase_arg: str | None = None

    @property
    def destructive(self) -> bool:
        """Whether *any* spelling of this command needs a confirm (``!`` in the list)."""
        return self.name in DESTRUCTIVE


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))


def _describe(action: argparse.Action) -> Arg | None:
    if isinstance(action, argparse._HelpAction) or action.help == argparse.SUPPRESS:
        return None
    positional = not action.option_strings
    name = action.dest if positional else ", ".join(action.option_strings)
    if positional and action.nargs in ("*", "+", argparse.REMAINDER):
        name += "…"
    required = positional and action.nargs not in ("?", "*", argparse.REMAINDER)
    return Arg(name=name, help=action.help or "", positional=positional, required=required)


def discover(parser: argparse.ArgumentParser | None = None) -> list[CommandSpec]:
    """Every public subcommand of ``parser`` (the real CLI's by default), in its order.

    Parser order rather than alphabetical: the CLI declares its commands roughly
    in the order of a run's life (``up`` before ``down``), which is a better
    reading order than the alphabet, and ``/`` is how anything is found.
    """
    parser = parser if parser is not None else cli._build_parser()
    sub = _subparsers(parser)
    # A subcommand's one-line help lives on a pseudo-action beside the parser,
    # not on the parser itself; only commands registered with help= have one.
    helps = {a.dest: a.help or "" for a in sub._choices_actions}
    out: list[CommandSpec] = []
    for name, child in sub.choices.items():
        if name.startswith("_") or name in HIDDEN:
            continue
        args = tuple(a for a in (_describe(x) for x in child._actions) if a is not None)
        phase_arg = next(
            (x.dest for x in child._actions if not x.option_strings and x.dest in PHASE_ARGS),
            None,
        )
        usage = " ".join(child.format_usage().split()).replace(" [-h]", "")
        out.append(
            CommandSpec(
                name=name,
                help=helps.get(name) or (child.description or "").split("\n")[0],
                usage=usage.removeprefix("usage: "),
                args=args,
                phase_arg=phase_arg,
            )
        )
    return out


def matches(spec: CommandSpec, needle: str) -> bool:
    """`/` search: every word of the needle appears in the name or the help."""
    hay = f"{spec.name} {spec.help}".lower()
    return all(word in hay for word in (needle or "").lower().split())


def destructive_reason(name: str, args) -> str | None:
    """Why ``swarm <name> <args>`` needs a confirm, or None when it does not."""
    entry = DESTRUCTIVE.get(name)
    if entry is None:
        return None
    flag, why = entry
    if flag is not None and flag not in args:
        return None
    return why


def prefill(spec: CommandSpec, phase: str | None) -> str:
    """The argument line a freshly picked command starts with."""
    return shlex.quote(phase) if spec.phase_arg and phase else ""


def split_args(text: str) -> list[str]:
    """The argument line as argv. Raises ValueError on an unclosed quote."""
    return shlex.split(text or "")


def phase_in(spec: CommandSpec | None, text: str) -> str | None:
    """The phase an argument line names, if its command takes one.

    This is what the tab reports as its selection, so picking ``why`` after
    ``retry P7`` carries ``P7`` across. A bare number given to ``free`` is a slot
    id, not a phase.
    """
    if spec is None or not spec.phase_arg:
        return None
    try:
        words = split_args(text)
    except ValueError:
        return None
    first = next((w for w in words if not w.startswith("-")), None)
    return None if first is None or first.isdigit() else first


def command_line(name: str, args) -> str:
    """How a run reads to a person: the line they would have typed."""
    return " ".join(["swarm", name, *(shlex.quote(a) for a in args)])


def build_argv(name: str, args, project_dir) -> list[str]:
    """The subprocess argv: this interpreter's CLI, scoped to the dashboard's project."""
    return [
        sys.executable, "-m", "swarm_orchestrator",
        "--project-dir", str(project_dir),
        name, *args,
    ]


# -- history ---------------------------------------------------------------
@dataclass
class Run:
    """One command the cockpit ran."""

    name: str
    args: tuple[str, ...]
    started_at: float
    ended_at: float | None = None
    code: int | None = None
    stopped: bool = False
    error: str = ""

    @property
    def status(self) -> str:
        if self.ended_at is None:
            return "running"
        if self.stopped:
            return "stopped"
        return "ok" if self.code == 0 else "failed"

    @property
    def line(self) -> str:
        return command_line(self.name, self.args)

    def took(self, now: float | None = None) -> float:
        end = self.ended_at if self.ended_at is not None else (now or time.time())
        return max(0.0, end - self.started_at)


def fmt_took(seconds: float) -> str:
    """Command durations are mostly sub-second; ``0s`` for all of them says nothing."""
    return f"{seconds:.1f}s" if seconds < 10 else fmt_duration(seconds)


def remember(history: list[Run], run: Run, keep: int = HISTORY_KEEP) -> list[Run]:
    """Append ``run``, dropping the oldest beyond ``keep``."""
    history.append(run)
    del history[:-keep]
    return history


def history_lines(history: list[Run], now: float | None = None,
                  shown: int = HISTORY_SHOWN) -> list[str]:
    """The "recent" panel: newest first, one run per line."""
    now = now if now is not None else time.time()
    runs = sorted(history, key=lambda r: r.started_at, reverse=True)[:shown]
    lines = []
    for run in runs:
        status = run.status
        state = token(status) if status != "stopped" else WARN
        code = "" if run.code is None or status == "ok" else f" {run.code}"
        lines.append(
            f"{paint('●', state)} {paint(f'{status}{code}'.ljust(10), state)} "
            f"{escape(clip(run.line, 40))}  "
            f"[{COLOR[MUTED]}]{fmt_took(run.took(now))} · {fmt_ago(run.started_at, now)}[/]"
        )
    return lines


def spec_detail(spec: CommandSpec, phase: str | None = None) -> str:
    """The highlighted command in full: what it does, what it takes, what it risks."""
    lines = [f"[b]swarm {escape(spec.name)}[/b]", escape(spec.help or "— no description —")]
    entry = DESTRUCTIVE.get(spec.name)
    if entry is not None:
        flag, why = entry
        when = f"with {flag}: " if flag else ""
        lines.append(paint(f"! {when}{escape(why)} — confirms first", WARN))
    lines.append("")
    lines.append(paint(escape(spec.usage), MUTED))
    for arg in spec.args:
        label = arg.name if arg.required or not arg.positional else f"{arg.name}?"
        lines.append(field(escape(clip(label, 20)), escape(clip(arg.help, 60)), width=22))
    if spec.phase_arg:
        lines.append("")
        hint = f"enter prefills {phase}" if phase else "takes a phase — nothing selected to prefill"
        lines.append(paint(escape(hint), MUTED))
    return "\n".join(lines)


def _signal(proc: subprocess.Popen, sig: int) -> None:
    """Signal the command's whole process group.

    The group, not the pid: ``swarm build`` execs its build, and a ``gc`` or
    ``integrate`` shells out to git. Signalling only the direct child would leave
    those running with their output pipe closed.
    """
    try:
        os.killpg(proc.pid, sig)
    except (OSError, AttributeError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


# -- the confirm -----------------------------------------------------------
class ConfirmRun(ModalScreen[bool]):
    """``y`` runs it, ``n`` or ``esc`` does not. Nothing else answers."""

    BINDINGS = [
        Binding("y", "answer(True)", "go"),
        Binding("n,escape", "answer(False)", "stop"),
    ]
    DEFAULT_CSS = f"""
    ConfirmRun {{ align: center middle; }}
    ConfirmRun > Vertical {{
        width: 72; height: auto;
        border: thick {COLOR[WARN]}; padding: 1 2; background: #0d1117;
    }}
    """

    def __init__(self, line: str, reason: str) -> None:
        super().__init__()
        self.line = line
        self.reason = reason

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(paint("[b]this cannot be taken back[/b]", WARN))
            yield Static(f"\n$ {escape(self.line)}\n\n{escape(self.reason)}\n")
            yield Static(
                f"[{COLOR[OK]}]y[/] run it    [{COLOR[BAD]}]n[/] / [{COLOR[BAD]}]esc[/] don't"
            )

    def action_answer(self, go: bool) -> None:
        self.dismiss(go)


# -- the tab ---------------------------------------------------------------
COMMAND_COLUMNS: tuple[tuple[str, int], ...] = (
    ("", 1),
    ("command", 16),
    ("what it does", 58),
)


class Commands(Vertical):
    """The command centre tab: pick a ``swarm`` subcommand, give it arguments, run it."""

    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("slash", "filter", "filter", show=False),
        Binding("escape", "escape", "clear filter", show=False),
        # Priority, because the argument line is an Input and Input claims
        # ctrl+x for "cut": without it the one key that stops a runaway command
        # would silently do nothing while the cursor sat in that box.
        Binding("f5", "run", "run", priority=True),
        Binding("ctrl+r", "rerun", "re-run last", priority=True),
        Binding("ctrl+x", "stop", "stop", priority=True),
    ]

    DEFAULT_CSS = """
    Commands { height: 1fr; }
    Commands > .tab-head { height: 1; padding: 0 1; }
    Commands > #cmd-filter.-off { display: none; }
    Commands > #cmd-top { height: 1fr; min-height: 8; }
    Commands #cmd-table { width: 1fr; }
    Commands #cmd-side { width: 45%; }
    Commands #cmd-detail-panel { height: 1fr; overflow-y: auto; }
    Commands #cmd-history-panel { height: auto; max-height: 12; }
    Commands > #cmd-argline { height: 3; }
    Commands #cmd-prompt { width: auto; padding: 1 1 0 1; }
    Commands #cmd-args { width: 1fr; }
    Commands > #cmd-output-panel { height: 1fr; min-height: 6; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # `_cmd_`-prefixed for the reason Doctor spells out: Textual owns names
        # like `_running`, and shadowing one fails far from the cause.
        self._cmd_specs: list[CommandSpec] = []
        self._cmd_error = ""
        try:
            self._cmd_specs = discover()
        except Exception as exc:  # noqa: BLE001 - a broken parser costs this tab only
            self._cmd_error = f"{type(exc).__name__}: {exc}"
        self._cmd_by_name = {s.name: s for s in self._cmd_specs}
        self._cmd_rows: list[CommandSpec] = []
        self._cmd_filter = ""
        self._cmd_picked: CommandSpec | None = None
        self._cmd_history: list[Run] = []
        self._cmd_run: Run | None = None
        self._cmd_proc: subprocess.Popen | None = None
        self._cmd_ticker = None
        self._dash = None

    # -- composition ------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Body("", classes="tab-head")
        yield Input(placeholder="filter commands — esc clears", id="cmd-filter",
                    classes="-off", disabled=True)
        with Horizontal(id="cmd-top"):
            table = DataTable(cursor_type="row", zebra_stripes=True, id="cmd-table")
            for label, width in COMMAND_COLUMNS:
                table.add_column(label, width=width)
            yield table
            with Vertical(id="cmd-side"):
                with Panel("command", id="cmd-detail-panel"):
                    yield Body("", id="cmd-detail")
                with Panel("recent", id="cmd-history-panel"):
                    yield Body("", id="cmd-history")
        with Horizontal(id="cmd-argline"):
            yield Label("$ swarm", id="cmd-prompt")
            yield Input(placeholder="pick a command with enter", id="cmd-args")
        with Panel("output", id="cmd-output-panel"):
            yield RichLog(id="cmd-output", max_lines=OUTPUT_KEEP, wrap=True, markup=False)

    def on_mount(self) -> None:
        self._fill()
        self._paint()
        self.update_detail()

    def on_show(self) -> None:
        # `c` is "take me to the command centre", so arrive ready to type: with
        # focus left on the tab strip, `/`, enter and f5 would all go nowhere.
        focused = self.app.focused
        if focused is None or self not in focused.ancestors:
            self.table.focus()

    def on_unmount(self) -> None:
        # The reader thread is blocked on the child's stdout, and the app cannot
        # exit until it returns: ending the child is what lets `q` actually quit.
        proc = self._cmd_proc
        if proc is not None and proc.poll() is None:
            _signal(proc, signal.SIGTERM)

    @property
    def table(self) -> DataTable:
        return self.query_one("#cmd-table", DataTable)

    @property
    def args_text(self) -> str:
        try:
            return self.query_one("#cmd-args", Input).value
        except Exception:  # noqa: BLE001 - not mounted
            return ""

    # -- the contract -----------------------------------------------------
    def update(self, dash) -> None:
        """Repaint the head and the history — never the list, which only a filter moves."""
        self._dash = dash
        try:
            self._paint()
        except Exception as exc:  # noqa: BLE001 - this tab must not take the app down
            self._say(f"this panel failed to render: {exc}", BAD)

    def selected_phase(self) -> str | None:
        """The phase on the argument line, so the app's selection follows it here."""
        return phase_in(self._cmd_picked, self.args_text)

    # -- rendering --------------------------------------------------------
    @property
    def selected(self) -> CommandSpec | None:
        try:
            index = self.table.cursor_row
        except Exception:  # noqa: BLE001 - not mounted
            return None
        return self._cmd_rows[index] if 0 <= index < len(self._cmd_rows) else None

    def _fill(self) -> None:
        """Rebuild the list for the current filter, keeping the cursor on its command."""
        table = self.table
        previous = self.selected
        self._cmd_rows = [s for s in self._cmd_specs if matches(s, self._cmd_filter)]
        table.clear()
        for spec in self._cmd_rows:
            table.add_row(
                paint("!", WARN) if spec.destructive else "",
                cell(spec.name, 16, WARN if spec.destructive else None),
                cell(spec.help or "—", 58, None if spec.help else MUTED),
                key=spec.name,
            )
        if self._cmd_rows:
            names = [s.name for s in self._cmd_rows]
            table.move_cursor(row=names.index(previous.name) if previous and previous.name in names else 0)
        self.update_detail()

    def _paint(self) -> None:
        now = time.time()
        total = len(self._cmd_specs)
        head = [
            f"{len(self._cmd_rows)} of {total} commands" if self._cmd_filter else f"{total} commands",
            paint("! confirms first", WARN),
        ]
        run = self._cmd_run
        if run is not None and run.ended_at is None:
            head.append(paint(f"running {escape(run.line)} · {fmt_took(run.took(now))}", INFO))
        if self._cmd_error:
            head.append(paint(f"parser unavailable: {escape(self._cmd_error)}", BAD))
        suffix = f"  ·  [{COLOR[WARN]}]/{escape(self._cmd_filter)}[/]" if self._cmd_filter else ""
        set_text(self.query_one(".tab-head", Body), "  ·  ".join(head) + suffix)

        lines = history_lines(self._cmd_history, now)
        set_text(
            self.query_one("#cmd-history", Body),
            "\n".join(lines) or paint("nothing run yet — enter picks, f5 runs", MUTED),
        )
        failed = sum(1 for r in self._cmd_history if r.status == "failed")
        set_border(
            self.query_one("#cmd-history-panel", Panel),
            title=f"recent — {len(self._cmd_history)} run(s)" if self._cmd_history else "recent",
            subtitle=f"{failed} failed" if failed else "",
        )

    def update_detail(self) -> None:
        try:
            body = self.query_one("#cmd-detail", Body)
        except Exception:  # noqa: BLE001 - not mounted yet
            return
        spec = self.selected
        if spec is None:
            text = paint("nothing matches — esc clears the filter", MUTED) if self._cmd_filter else ""
        else:
            text = spec_detail(spec, self._phase_hint() if spec.phase_arg else None)
        set_text(body, text)

    def _phase_hint(self) -> str | None:
        try:
            return self.app.selected_phase()
        except Exception:  # noqa: BLE001 - a bare harness app has no notion of one
            return None

    def _say(self, text: str, state: str = MUTED) -> None:
        """One line into the output log, in the tab's own voice."""
        self._write(Text.from_markup(paint(escape(text), state)))

    def _write(self, line: Text | str) -> None:
        try:
            self.query_one("#cmd-output", RichLog).write(line)
        except Exception:  # noqa: BLE001 - not mounted
            pass

    # -- keys -------------------------------------------------------------
    def on_data_table_row_highlighted(self, _event) -> None:
        self.update_detail()

    def on_data_table_row_selected(self, event) -> None:
        event.stop()
        spec = self.selected
        if spec is not None:
            self.pick(spec)

    def pick(self, spec: CommandSpec) -> None:
        """Put ``spec`` on the argument line, prefilled with the selected phase."""
        phase = self._phase_hint()
        self._cmd_picked = spec
        self.query_one("#cmd-prompt", Label).update(f"$ swarm {escape(spec.name)}")
        box = self.query_one("#cmd-args", Input)
        box.value = prefill(spec, phase)
        box.placeholder = spec.usage.removeprefix(f"swarm {spec.name}").strip() or "no arguments"
        box.focus()

    def action_cursor_down(self) -> None:
        self.table.action_cursor_down()

    def action_cursor_up(self) -> None:
        self.table.action_cursor_up()

    def action_filter(self) -> None:
        box = self.query_one("#cmd-filter", Input)
        box.disabled = False
        box.remove_class("-off")
        box.focus()

    def set_filter(self, needle: str) -> None:
        self._cmd_filter = needle or ""
        self._fill()
        self._paint()

    def action_escape(self) -> None:
        box = self.query_one("#cmd-filter", Input)
        if self._cmd_filter or not box.disabled:
            box.value = ""
            self.set_filter("")
            # Disabled as well as hidden: a hidden Input can still be handed
            # focus, and then every key meant for the list types into nothing.
            box.disabled = True
            box.add_class("-off")
        self.table.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "cmd-filter":
            event.stop()
            self.set_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "cmd-filter":
            self.table.focus()
        else:
            self.action_run()

    # -- running ----------------------------------------------------------
    def action_run(self) -> None:
        """Run what is on the argument line — or the highlighted command, picked first."""
        if self._cmd_picked is None:
            spec = self.selected
            if spec is None:
                return
            self.pick(spec)
        try:
            args = split_args(self.args_text)
        except ValueError as exc:
            self._say(f"cannot parse the arguments: {exc}", BAD)
            return
        self.request(self._cmd_picked, args)

    def action_rerun(self) -> None:
        if not self._cmd_history:
            self._say("nothing has run yet")
            return
        last = max(self._cmd_history, key=lambda r: r.started_at)
        spec = self._cmd_by_name.get(last.name)
        if spec is None:
            return
        self._cmd_picked = spec
        self.query_one("#cmd-prompt", Label).update(f"$ swarm {escape(spec.name)}")
        self.query_one("#cmd-args", Input).value = shlex.join(last.args)
        self.request(spec, list(last.args))

    def request(self, spec: CommandSpec, args: list[str]) -> None:
        """Run ``spec`` with ``args`` — after a confirm, if it is destructive."""
        if self._cmd_run is not None and self._cmd_run.ended_at is None:
            self._say(f"{self._cmd_run.line} is still running — ctrl+x stops it", WARN)
            return
        reason = destructive_reason(spec.name, args)
        if reason is None:
            self._start(spec, args)
            return

        def answered(go: bool | None) -> None:
            if go:
                self._start(spec, args)
            else:
                self._say(f"not run: {command_line(spec.name, args)}")

        self.app.push_screen(ConfirmRun(command_line(spec.name, args), reason), answered)

    def _start(self, spec: CommandSpec, args: list[str]) -> None:
        cfg = getattr(self._dash, "cfg", None) or getattr(self.app, "cfg", None)
        if cfg is None:
            self._say("no project loaded — nothing to run against", BAD)
            return
        run = Run(name=spec.name, args=tuple(args), started_at=time.time())
        self._cmd_run = run
        remember(self._cmd_history, run)
        self._write(Text(""))
        self._write(Text.from_markup(paint(f"$ {escape(run.line)}", ACCENT)))
        self.query_one("#cmd-output-panel", Panel).set_title("output", "running")
        if self._cmd_ticker is None:
            # The app only repaints on disk changes; a quiet `gc` would otherwise
            # show a frozen clock and look hung.
            self._cmd_ticker = self.set_interval(1.0, self._paint)
        self._paint()
        self._spawn(run, build_argv(spec.name, args, cfg.project_dir), str(cfg.project_dir))

    @work(thread=True, group="command")
    def _spawn(self, run: Run, argv: list[str], cwd: str) -> None:
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                start_new_session=True,  # its own group, so ctrl+x reaches its children
            )
        except OSError as exc:
            self.app.call_from_thread(self._finish, run, None, f"could not start: {exc}")
            return
        self._cmd_proc = proc
        for line in proc.stdout:
            self.app.call_from_thread(self._write, Text(line.rstrip("\n")))
        self.app.call_from_thread(self._finish, run, proc.wait(), "")

    def _finish(self, run: Run, code: int | None, error: str) -> None:
        run.ended_at, run.code, run.error = time.time(), code, error
        self._cmd_proc = None
        if self._cmd_ticker is not None:
            self._cmd_ticker.stop()
            self._cmd_ticker = None
        status = run.status
        state = WARN if status == "stopped" else token(status)
        tail = error or (f"exit {code}" if status != "stopped" else f"stopped (exit {code})")
        self._write(Text.from_markup(paint(escape(f"{tail} · {fmt_took(run.took())}"), state)))
        self.query_one("#cmd-output-panel", Panel).set_title("output", f"{run.name} · {status}")
        self._paint()

    def action_stop(self) -> None:
        proc, run = self._cmd_proc, self._cmd_run
        if proc is None or run is None or proc.poll() is not None:
            self._say("nothing is running")
            return
        run.stopped = True
        self._say(f"stopping {run.line}…", WARN)
        _signal(proc, signal.SIGTERM)

        def insist() -> None:
            if proc.poll() is None:
                _signal(proc, signal.SIGKILL)

        self.set_timer(STOP_GRACE_S, insist)
