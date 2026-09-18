"""``python -m swarm_orchestrator.tui`` — run the dashboard standalone.

Useful before ``swarm tui`` exists in the CLI, and for driving the dashboard
against another project's state dir without touching that project's session.
"""

from __future__ import annotations

import sys

from ..config import load
from . import main

if __name__ == "__main__":
    project_dir = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(main(load(project_dir=project_dir)))
