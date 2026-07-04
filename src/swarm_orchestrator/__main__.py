"""Enable ``python -m swarm_orchestrator ...`` (used by fake scripts in tests)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
