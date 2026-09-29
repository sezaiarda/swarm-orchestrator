"""The dashboard's poll reads the big-picture memory into the headline.

``poll()`` once reset ``big_picture`` to empty on every tick, so the headline's
"big picture 2.0h ago" never showed on a live dashboard even though the
rendering test (which sets the field by hand) passed.
"""

from __future__ import annotations

import time

import pytest

from swarm_orchestrator import bigpic
from swarm_orchestrator.config import load
from swarm_orchestrator.tui.dash import Dash


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\nP1 needs:P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "bigpicpoll")
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    c = load(project_dir=str(project))
    c.ensure_dirs()
    return c


def test_poll_carries_the_last_pass_into_the_headline(cfg):
    dash = Dash(cfg)
    dash.poll()
    assert dash.big_picture == ""
    bigpic.save(cfg, bigpic.Memory(last_end=time.time() - 2 * 3600, last_status=bigpic.LANDED))
    dash.poll()
    assert dash.big_picture.startswith("big picture 2")
    dash.poll()
    assert dash.big_picture.startswith("big picture 2")  # a later tick keeps it
