"""Pure/hermetic coverage for the paginated worker layout.

``plan_worker_windows`` is pure math (no tmux). ``split_layout`` shells out, so we
stub :func:`swarm_orchestrator.tmux.run` to record the issued commands and fake
``list-panes`` geometry — proving the per-count preset (1 full / 2 even-horizontal
/ 3-4 tiled) without needing a real tmux server.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from swarm_orchestrator import tmux
from swarm_orchestrator.session import PANES_PER_WINDOW, plan_worker_windows


@pytest.mark.parametrize(
    "total, expected",
    [
        (1, [("workers", 1)]),
        (2, [("workers", 2)]),
        (4, [("workers", 4)]),
        (5, [("workers", 4), ("workers-2", 1)]),
        (6, [("workers", 4), ("workers-2", 2)]),
        (8, [("workers", 4), ("workers-2", 4)]),
    ],
)
def test_plan_worker_windows(total, expected):
    assert plan_worker_windows(total) == expected


def test_plan_worker_windows_sums_to_total():
    for total in range(1, 13):
        plan = plan_worker_windows(total)
        assert sum(size for _, size in plan) == total
        assert all(1 <= size <= PANES_PER_WINDOW for _, size in plan)


def test_plan_worker_windows_zero():
    assert plan_worker_windows(0) == []


class _FakeTmux:
    """Records tmux invocations; fakes list-panes with N tidy geometric rows."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.panes = 1  # a fresh window opens with one pane

    def run(self, args, check=False):
        args = list(args)
        self.calls.append(args)
        if args[0] == "split-window":
            self.panes += 1
            return SimpleNamespace(stdout="", returncode=0)
        if args[0] == "list-panes":
            rows = "\n".join(f"0\t{i * 10}\t%{i}" for i in range(self.panes))
            return SimpleNamespace(stdout=rows + "\n", returncode=0)
        return SimpleNamespace(stdout="", returncode=0)


def _splits(calls):
    return [c for c in calls if c and c[0] == "split-window"]


@pytest.mark.parametrize(
    "count, n_splits, preset",
    [
        (1, 0, None),
        (2, 1, "even-horizontal"),
        (3, 2, "tiled"),
        (4, 3, "tiled"),
    ],
)
def test_split_layout_preset_per_count(monkeypatch, count, n_splits, preset):
    fake = _FakeTmux()
    monkeypatch.setattr(tmux, "run", fake.run)

    panes = tmux.split_layout("@1", count)

    assert len(panes) == count
    splits = _splits(fake.calls)
    assert len(splits) == n_splits
    layouts = [c[-1] for c in fake.calls if c and c[0] == "select-layout"]
    if preset is None:
        assert layouts == []
    else:
        assert layouts == [preset]
    # count==2 must be a horizontal (LEFT|RIGHT) split, never a stacked one
    if count == 2:
        assert splits == [["split-window", "-h", "-t", "@1", "sleep infinity"]]
