"""The ``swarm tui`` dashboard package.

Importing this module is cheap on purpose: ``cli.py`` imports at module scope, so
pulling Textual (and Rich, and its whole widget tree) in here would tax every
``swarm done`` a worker runs. The Textual app is imported inside :func:`main`
instead, which is also why a broken/absent Textual install degrades to one
readable line rather than breaking the entire CLI.

The pure data layer (:mod:`.data`) and the subprocess probes (:mod:`.probes`) have
no Textual dependency at all and can be imported directly — that is what
``tests/test_tui.py`` does.
"""

from __future__ import annotations

import sys


def main(cfg) -> int:
    """Entry point for ``swarm tui``. Returns a process exit code."""
    try:
        from .app import main as _run
    except ImportError as exc:
        print(
            f"swarm tui needs the `textual` package ({exc}). "
            "Install it with `uv sync` (it is a project dependency).",
            file=sys.stderr,
        )
        return 2
    return _run(cfg)


# `swarm tui` is dispatched however cli.py prefers to name it; all three spellings
# reach the same place so wiring it up can't pick the wrong one.
run = main
cmd_tui = main

__all__ = ["main", "run", "cmd_tui"]
