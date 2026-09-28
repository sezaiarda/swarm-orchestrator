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
`bad`, `info`, `muted`, and the quieter `ready`/`blocked`/`skip`/`idle`, plus
`you` for anything waiting on the owner and one hue each for the operator and
the Overseer) and nothing else, so the same green always means the same thing on
every tab. Anything that wants a colour asks for a state; anything that wants a
shape asks :func:`glyph`, so colour and shape can never disagree.
"""

from __future__ import annotations

from textual.containers import Vertical
from textual.widgets import Static

# -- surfaces ---------------------------------------------------------------
#: The same four greys the web board's CSS uses (``--bg``/``--sf``/``--sf2``/
#: ``--bd``), so a glance at the phone and a glance at tmux read as one product.
#: A card is one step lighter than the page it sits on: the eye finds a region by
#: its surface before it reads a single border.
BG = "#0d1117"
SURFACE = "#161b22"
RAISED = "#1c2330"
BORDER = "#30363d"
TEXT = "#e6edf3"

# -- semantic tokens -------------------------------------------------------
OK = "ok"
WARN = "warn"
BAD = "bad"
INFO = "info"
MUTED = "muted"
ACCENT = "accent"
#: States that used to share MUTED's grey. Seven different situations in one
#: colour meant the colour said nothing, so each gets its own quiet hue: still
#: dim enough to sit behind ok/fail/running, distinct enough to tell apart.
READY = "ready"
BLOCKED = "blocked"
SKIP = "skip"
IDLE = "idle"
#: Anything waiting on the owner. Warmer than WARN on purpose: a question is the
#: one thing on screen that needs a person, not a thing that went wrong.
YOU = "you"
OPERATOR = "operator"
OVERSEER = "overseer"
#: Not states: body text and the hairline rule under a section heading.
BRIGHT = "bright"
SOFT = "soft"
RULE = "rule"

COLOR = {
    OK: "#3fb950",
    WARN: "#d29922",
    BAD: "#f85149",
    INFO: "#58a6ff",
    MUTED: "#8b949e",
    ACCENT: "#bc8cff",
    READY: "#56d4dd",
    BLOCKED: "#a8906a",
    SKIP: "#6b7d91",
    IDLE: "#6e7681",
    YOU: "#f0883e",
    OPERATOR: "#db61a2",
    OVERSEER: "#bc8cff",
    BRIGHT: TEXT,
    SOFT: "#c9d1d9",
    RULE: BORDER,
}

# Status word -> token. One place, so `ok` is never green here and blue there.
STATE = {
    "ok": OK,
    "done": OK,
    "delivered": OK,
    "running": INFO,
    "busy": INFO,
    "launching": INFO,
    "building": INFO,
    "ready": READY,
    "idle": IDLE,
    "free": IDLE,
    "pending": IDLE,
    "retiring": IDLE,
    "skip": SKIP,
    "skipped": SKIP,
    "blocked": BLOCKED,
    "waiting": YOU,
    "parked": YOU,
    "needs-owner": YOU,
    "needs_you": YOU,
    # A question is not a failure.
    "operator-ask": YOU,
    "owner-row": YOU,
    "held": WARN,
    "warn": WARN,
    "operator": OPERATOR,
    "queued": OPERATOR,
    "overseer": OVERSEER,
    "abandoned": BAD,
    "operator-abandoned": BAD,
    "fail": BAD,
    "failed": BAD,
    # History: a claim that ended without `swarm done`, and a phase the ledger
    # ticked with no report here (words from :data:`tui.data.RUN_WORDS`).
    "worker gone": WARN,
    "done elsewhere": OK,
    "gone": BAD,
    "dead": BAD,
    "down": BAD,
}

#: One glyph per token, so a state reads the same by shape as by colour — which
#: matters on a terminal whose palette mangles the hues, and to anyone who cannot
#: tell amber from orange. All single-cell: a wide glyph shifts every column after it.
GLYPH = {
    OK: "✓",
    BAD: "✗",
    INFO: "●",
    READY: "○",
    BLOCKED: "◌",
    SKIP: "–",
    IDLE: "·",
    YOU: "◆",
    WARN: "▲",
    OPERATOR: "◇",
    OVERSEER: "◎",
    ACCENT: "●",
    MUTED: "·",
}


def token(status: str | None) -> str:
    """Semantic token for a status word. Unknown words read as muted."""
    return STATE.get((status or "").strip().lower(), MUTED)


def paint(text: str, state: str) -> str:
    """Wrap ``text`` in the colour for ``state`` (Rich markup)."""
    return f"[{COLOR.get(state, COLOR[MUTED])}]{text}[/]"


def glyph(status: str | None) -> str:
    """The status's shape, coloured — ``✓`` green, ``◆`` orange, ``◌`` amber-grey."""
    state = token(status)
    return f"[{COLOR[state]}]{GLYPH.get(state, '·')}[/]"


def section(title: str, count: int | None = None, state: str | None = None,
            width: int = 44) -> str:
    """A detail pane's section heading: a blank line, then ``── recap ─────``.

    Bold title on a muted rule rather than a coloured word on its own: a colour
    alone reads as one more status, a rule reads as "a new part starts here". The
    blank line is part of the heading because :func:`rows` drops empty lines,
    which had every section butting against the one above it.
    """
    label = f"{title} ({count})" if count is not None else title
    colour = COLOR.get(state or BRIGHT, TEXT)
    rule = "─" * max(2, width - len(label) - 4)
    return f"\n[{COLOR[RULE]}]──[/] [bold][{colour}]{label}[/][/] [{COLOR[RULE]}]{rule}[/]"


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
        background: #161b22;
        border: round #30363d;
        border-title-color: #e6edf3;
        border-title-style: bold;
        border-subtitle-color: #8b949e;
        padding: 0 1;
        height: auto;
    }
    Panel.-ok { border: round #3fb950; }
    Panel.-warn { border: round #d29922; }
    Panel.-bad { border: round #f85149; }
    Panel.-you { border: round #f0883e; border-title-color: #f0883e; }
    Panel.-attention { border: thick #d29922; border-title-color: #d29922; }
    """

    def __init__(self, title: str = "", *, state: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._title = title
        if state:
            self.add_class(f"-{state}")

    def on_mount(self) -> None:
        self.border_title = self._title

    def set_title(self, title: str, subtitle: str | None = None) -> None:
        """Set the border title/subtitle — only when they changed.

        The setters refresh unconditionally, and home re-titles its cards on
        every tick; an unguarded write there is a repaint a tick for nothing.
        ``subtitle=None`` leaves the subtitle alone; ``""`` clears it.
        """
        if getattr(self, "_set_title", None) != title:
            self._set_title = title
            self.border_title = title
        if subtitle is not None and getattr(self, "_set_subtitle", None) != subtitle:
            self._set_subtitle = subtitle
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
