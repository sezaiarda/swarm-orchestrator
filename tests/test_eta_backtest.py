"""The ETA backtest still runs on the recorded history, so it can be re-run later.

``docs/research/eta-history/backtest.py`` is the evidence the engine's defaults
rest on. This runs it small (few replays, the engine alone): not to re-judge the
engine, which the full run does, but so a change to the engine that breaks the
harness fails here rather than the next time someone needs the numbers. The
calibrated engine runs too, since its split is the part most easily broken.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "docs" / "research" / "eta-history" / "backtest.py"


def test_the_backtest_scores_the_engine_and_both_baselines():
    spec = importlib.util.spec_from_file_location("eta_backtest", SCRIPT)
    backtest = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = backtest  # its dataclasses look their module up by name
    spec.loader.exec_module(backtest)
    text = backtest.run(backtest.load(), 20, backtest.VARIANTS[:2], None)
    campaign = text.split("## Target: campaign, all nine snapshots")[1].split("##")[0]
    for method in ("engine", "engine, calibrated", "rate", "tmc"):
        assert f"| {method} | 89 |" in campaign
    # The first snapshot has nothing before it to calibrate on.
    assert "k per snapshot for **engine, calibrated**, fitted on the snapshots before it: " \
           "09-19 09:00 1.00," in text
