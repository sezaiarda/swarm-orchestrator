"""The dashboard's visual language: tokens, and the widgets built from them.

Every panel in the first dashboard returned a ``str`` that got dumped into a
``Static``. That is why it read as a wall of text — there was no structure to
see, only paragraphs, and nothing on screen carried meaning by its shape or its
colour. Reading it required parsing it.

This module fixes that at the root by giving the whole app one small vocabulary:

* :class:`Panel` — a titled, bordered region. Panels are how the eye finds
  anything; a screen of them can be scanned instead of read.
* :class:`Field` — one label/value pair, right-aligned value, optional colour.
  The unit the dense panels are built from.
* :class:`Meter` — a labelled bar that colours itself by threshold.
* :class:`Chip` — a small status token (``● running``) whose colour IS the
  message, so a glance suffices.

The palette is semantic, not decorative. A colour means a state (`ok`, `warn`,
`bad`, `info`, `muted`) and nothing else, so the same green always means the same
thing on every tab. Anything that wants a colour asks for a state.
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Static

# -- semantic tokens -------------------------------------------------------
OK = "ok"
WARN = "warn"
BAD = "bad"
INFO = "info"
MUTED = "muted"
ACCENT = "accent"

COLOR = {
    OK: "#3fb950",
    WARN: "#d29922",
    BAD: "#f85149",
    INFO: "#58a6ff",
    MUTED: "#8b949e",
    ACCENT: "#bc8cff",
}

# Status word -> token. One place, so `ok` is never green here and blue there.
STATE = {
    "ok": OK,
    "done": OK,
    "delivered": OK,
    "running": INFO,
    "busy": INFO,
    "launching": INFO,
    "ready": ACCENT,
    "idle": MUTED,
    "free": MUTED,
    "skip": MUTED,
    "blocked": MUTED,
    "pending": MUTED,
    "retiring": MUTED,
    "waiting": WARN,
    "parked": WARN,
    "needs-owner": WARN,
    "held": WARN,
    "warn": WARN,
    "abandoned": BAD,
    "operator-abandoned": BAD,
    # A question is not a failure.
    "operator-ask": WARN,
    "fail": BAD,
    "failed": BAD,
    "gone": BAD,
    "dead": BAD,
    "down": BAD,
}


def token(status: str | None) -> str:
    """Semantic token for a status word. Unknown words read as muted."""
    return STATE.get((status or "").strip().lower(), MUTED)


def paint(text: str, state: str) -> str:
    """Wrap ``text`` in the colour for ``state`` (Rich markup)."""
    return f"[{COLOR.get(state, COLOR[MUTED])}]{text}[/]"


def dot(status: str | None, label: str | None = None) -> str:
    """``● label`` coloured by status — the smallest unit of meaning here."""
    state = token(status)
    body = label if label is not None else (status or "?")
    return f"[{COLOR[state]}]●[/] {body}"


def bar(done: float, total: float, width: int = 20) -> str:
    """A solid/□ meter. Deliberately not a Unicode gradient: a gradient reads as
    texture at a glance, and this has to be legible from across a room."""
    if total <= 0:
        return "─" * width
    filled = max(0, min(width, round(width * done / total)))
    return "█" * filled + "░" * (width - filled)


def meter_state(pct: float, *, warn: float = 70.0, bad: float = 90.0,
                invert: bool = False) -> str:
    """Threshold colour for a percentage.

    ``invert`` for meters where *low* is the problem (a progress bar at 5% is
    not an emergency; a context window at 95% is).
    """
    if invert:
        return OK if pct >= warn else (WARN if pct >= bad else BAD)
    return BAD if pct >= bad else (WARN if pct >= warn else OK)


# -- widgets ---------------------------------------------------------------
class Panel(Vertical):
    """A titled, bordered region.

    The border title is the whole point: it is what lets someone find the thing
    they came for without reading the screen top to bottom.
    """

    DEFAULT_CSS = """
    Panel {
        border: round #30363d;
        border-title-color: #8b949e;
        border-title-style: bold;
        padding: 0 1;
        height: auto;
    }
    Panel.-ok { border: round #3fb950; }
    Panel.-warn { border: round #d29922; }
    Panel.-bad { border: round #f85149; }
    Panel.-attention { border: thick #d29922; border-title-color: #d29922; }
    """

    def __init__(self, title: str = "", *, state: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        if state:
            self.add_class(f"-{state}")

    def on_mount(self) -> None:
        self.border_title = self._title

    def set_title(self, title: str, subtitle: str = "") -> None:
        self.border_title = title
        if subtitle:
            self.border_subtitle = subtitle


class Body(Static):
    """Panel content. A Static, but one that only ever receives pre-composed
    markup from a panel that owns its own layout — never a paragraph of prose."""

    DEFAULT_CSS = "Body { height: auto; }"


def field(label: str, value: str, *, state: str | None = None,
          width: int = 16) -> str:
    """One ``label   value`` line, value coloured by state."""
    painted = paint(value, state) if state else value
    return f"[#8b949e]{label:<{width}}[/]{painted}"


def rows(*lines: str) -> str:
    """Join panel lines, dropping the empties so a missing datum leaves no hole."""
    return "\n".join(x for x in lines if x)
