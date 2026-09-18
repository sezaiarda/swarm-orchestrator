"""The health tab: `swarm doctor`, rendered.

The supervisor is deliberately watchdog-light, and the failure modes that cost
the most produce **no log line at all** — a held integration that pings once and
then goes quiet, a recorded pid that still reads alive after the process is gone,
a busy slot whose worker never received its prompt. `swarm doctor` checks for
exactly these, and this tab is its face: it shells out to
the real command rather than reimplementing the checks, so the cockpit and the
CLI can never disagree about whether the swarm is healthy — and a check added to
`doctor.py` shows up here for free.

It runs on a worker thread and only when asked. Health checks probe tmux, git and
the filesystem; doing that on every tick would make an always-open dashboard
expensive for no reason, and a stale answer with a timestamp is more honest than
a fresh one that costs something to produce.
"""

from __future__ import annotations

import json
import subprocess
import time

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from .data import fmt_ago
from .theme import BAD, COLOR, MUTED, OK, WARN, Body, Panel, paint

_ICON = {"ok": "✓", "warn": "!", "fail": "✗"}
_TOKEN = {"ok": OK, "warn": WARN, "fail": BAD}


class Doctor(Vertical):
    """Runs `swarm doctor --json` on demand and renders the checks."""

    DEFAULT_CSS = """
    Doctor { height: 1fr; }
    Doctor Panel { height: 1fr; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # All four are `_doc_`-prefixed deliberately. Textual's MessagePump owns
        # `self._running` and Widget owns `self._render`; shadowing either makes
        # the framework call or read OUR version, which fails in ways that look
        # nothing like the cause -- a widget that renders `None`, or a message
        # pump that believes it is already started so the worker's result never
        # lands. Both cost a debugging cycle here. Prefix and move on.
        self._doc_checks: list[dict] = []
        self._doc_ran_at: float | None = None
        self._doc_error: str = ""
        self._doc_busy = False

    def compose(self) -> ComposeResult:
        with Panel("health", id="doctor-panel"):
            yield Body(id="doctor-body")

    def on_mount(self) -> None:
        self._repaint()

    # The app calls this every refresh; it must not trigger a subprocess.
    def update(self, dash) -> None:  # noqa: D102 - tab contract
        self._repaint()

    def selected_phase(self) -> str | None:  # noqa: D102 - tab contract
        return None

    def run_checks(self) -> None:
        """Ask for a fresh answer. Bound to `d` app-wide."""
        if self._doc_busy:
            return
        self._doc_busy = True
        self._repaint()
        self._spawn()

    @work(thread=True, exclusive=True, group="doctor")
    def _spawn(self) -> None:
        cfg = self.app.cfg
        try:
            proc = subprocess.run(
                ["swarm", "doctor", "--json"],
                cwd=str(cfg.project_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            raw = (proc.stdout or "").strip()
            if not raw:
                checks, err = [], (proc.stderr or "").strip()[:400] or "doctor produced no output"
            else:
                data = json.loads(raw)
                # doctor's shape has moved before; accept the plausible ones
                # rather than showing nothing over a key name.
                if isinstance(data, dict):
                    data = data.get("checks", [])
                checks, err = [c for c in data if isinstance(c, dict)], ""
        except subprocess.TimeoutExpired:
            checks, err = [], "doctor timed out after 120s"
        except FileNotFoundError:
            checks, err = [], "`swarm` is not on PATH"
        except (ValueError, OSError) as exc:
            checks, err = [], f"{type(exc).__name__}: {exc}"
        self.app.call_from_thread(self._finish, checks, err)

    def _finish(self, checks: list[dict], err: str) -> None:
        self._doc_checks, self._doc_error, self._doc_busy = checks, err, False
        self._doc_ran_at = time.time()
        self._repaint()

    def _repaint(self) -> None:
        """Redraw from cached results.

        NOT named ``_render``: that is Textual's own ``Widget._render``, and
        shadowing it makes the framework call this instead, get ``None`` where it
        expects a visual, and take the whole tab down with
        ``'NoneType' object has no attribute 'render_strips'``.
        """
        try:
            body = self.query_one("#doctor-body", Body)
            panel = self.query_one("#doctor-panel", Panel)
        except Exception:  # noqa: BLE001 - not mounted yet
            return

        if self._doc_busy:
            body.update(paint("  running swarm doctor…", MUTED))
            panel.set_title("health", "working")
            return
        if self._doc_ran_at is None:
            body.update(
                paint("  press ", MUTED) + paint("d", OK)
                + paint(" to run swarm doctor", MUTED)
                + "\n\n"
                + paint(
                    "  checks the things that produce no log line: a held merge queue,\n"
                    "  a stale supervisor pid, a busy slot whose worker never got its\n"
                    "  prompt, a telegram that silently failed to send.",
                    MUTED,
                )
            )
            panel.set_title("health", "not run")
            return
        if self._doc_error:
            body.update(paint(f"  {self._doc_error}", BAD))
            panel.set_title("health", "failed")
            return

        counts = {"ok": 0, "warn": 0, "fail": 0}
        lines: list[str] = []
        for chk in self._doc_checks:
            status = str(chk.get("status", "ok")).lower()
            counts[status] = counts.get(status, 0) + 1
            tok = _TOKEN.get(status, MUTED)
            icon = _ICON.get(status, "·")
            name = str(chk.get("name", "?"))
            lines.append(
                f"[{COLOR[tok]}]{icon}[/] [b]{name:<22}[/b] {chk.get('detail', '')}"
            )
            hint = chk.get("fix_hint")
            if hint and status != "ok":
                lines.append(f"  {' ' * 22} [{COLOR[MUTED]}]→ {hint}[/]")
        body.update("\n".join(lines) or paint("  no checks returned", MUTED))

        worst = BAD if counts.get("fail") else (WARN if counts.get("warn") else OK)
        panel.remove_class("-ok", "-warn", "-bad")
        panel.add_class(f"-{'bad' if worst is BAD else 'warn' if worst is WARN else 'ok'}")
        panel.set_title(
            "health",
            f"{counts.get('fail', 0)} failing · {counts.get('warn', 0)} warning "
            f"· ran {fmt_ago(self._doc_ran_at)}",
        )
