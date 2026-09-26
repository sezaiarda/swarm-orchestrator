"""Pure/hermetic coverage for the paginated worker layout.

``plan_worker_windows`` is pure math (no tmux). ``split_layout`` shells out, so we
stub :func:`swarm_orchestrator.tmux.run` to record the issued commands and fake
``list-panes`` geometry — proving both the ``auto`` per-count preset (1 full / 2
even-horizontal / 3-4 tiled) and a pinned ``[tmux].layout`` without needing a real
tmux server.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm_orchestrator import config as config_mod
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


@pytest.mark.parametrize(
    "total, per_window, expected",
    [
        (5, 2, [("workers", 2), ("workers-2", 2), ("workers-3", 1)]),
        (3, 2, [("workers", 2), ("workers-2", 1)]),
        (2, 2, [("workers", 2)]),
        (3, 1, [("workers", 1), ("workers-2", 1), ("workers-3", 1)]),
        (5, 4, [("workers", 4), ("workers-2", 1)]),
        (2, 0, [("workers", 1), ("workers-2", 1)]),  # never below one per window
    ],
)
def test_plan_worker_windows_per_window(total, per_window, expected):
    assert plan_worker_windows(total, per_window) == expected


def test_config_panes_per_window_default_file_env_and_floor(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("SWARM_PANES_PER_WINDOW", raising=False)
    assert config_mod.load(project_dir=str(tmp_path)).tmux_panes_per_window == PANES_PER_WINDOW
    (tmp_path / ".swarm.toml").write_text("[tmux]\npanes_per_window = 2\n", encoding="utf-8")
    assert config_mod.load(project_dir=str(tmp_path)).tmux_panes_per_window == 2
    monkeypatch.setenv("SWARM_PANES_PER_WINDOW", "3")
    assert config_mod.load(project_dir=str(tmp_path)).tmux_panes_per_window == 3
    monkeypatch.setenv("SWARM_PANES_PER_WINDOW", "0")
    assert config_mod.load(project_dir=str(tmp_path)).tmux_panes_per_window == 1


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not available")
def test_three_workers_at_two_per_window_live_tmux(tmp_path, monkeypatch):
    """The multi-repo setting, on a real but fully isolated tmux server (own
    TMUX_TMPDIR): 3 workers at 2 per window is `workers` (slots 0, 1) and
    `workers-2` (slot 2); parking slot 2's worker keeps `workers-2` at one pane
    with a replacement tagged 2; growing to 5 fills `workers-2` then opens
    `workers-3`. The default server, and any live swarm on it, is never touched."""
    from swarm_orchestrator import session as session_mod
    from swarm_orchestrator import state as state_mod
    from swarm_orchestrator.supervisor import Supervisor

    sockdir = tempfile.mkdtemp(prefix="ppwprobe-")
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    for leak in ("SWARM_DRIVER", "SWARM_PANES_PER_WINDOW", "SWARM_SESSION", "SWARM_LAYOUT",
                 "SWARM_GIT_ISOLATION", "SWARM_MASTER_CMD"):
        monkeypatch.delenv(leak, raising=False)
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    project = tmp_path / "project"
    project.mkdir()
    (project / ".swarm.toml").write_text(
        '[swarm]\nmax_workers = 3\ndriver = "tmux"\n'
        '[tmux]\nsession = "ppwprobe"\npanes_per_window = 2\n'
        "[web]\nenabled = false\n[tui]\nautostart = false\n",
        encoding="utf-8",
    )
    cfg = config_mod.load(project_dir=str(project))
    cfg.ensure_dirs()
    state_mod.init_state(cfg)

    def slots(win: str) -> list[str]:
        return [s for _, s in tmux.list_panes_with_slot(win)]

    try:
        windows = session_mod.setup(cfg)
        workers = [n for n in windows if n.startswith("workers")]
        assert workers == ["workers", "workers-2"]
        assert slots(windows["workers"]) == ["0", "1"]
        assert slots(windows["workers-2"]) == ["2"]
        st = state_mod.read(cfg)
        assert [tmux.window_of(s.pane_id) for s in st.slots] == [
            windows["workers"], windows["workers"], windows["workers-2"]]

        # Park the worker in slot 2: its window keeps one pane, the replacement.
        with state_mod.transaction(cfg) as s:
            s.claim_slot("P2")  # slots 0 and 1 first
            s.claim_slot("P3")
            s.claim_slot("P4")
            s.waiting["P4"] = 0.0
            s.paused = True  # the park must not go on to launch anything
        old = state_mod.read(cfg).slots[2].pane_id
        sup = Supervisor(cfg)
        try:
            sup._park("P4")
        finally:
            sup.log.close()
        st = state_mod.read(cfg)
        assert "P4" in st.parked
        assert st.slots[2].pane_id != old
        assert tmux.list_panes(windows["workers-2"]) == [st.slots[2].pane_id]
        assert slots(windows["workers-2"]) == ["2"]
        assert slots(windows["workers"]) == ["0", "1"]
        assert tmux.list_panes(st.windows["wait:P4"]) == [old]

        # A live grow to five follows the same page size.
        panes, grown, failed = session_mod.add_slot_panes(cfg, st.windows, [3, 4], cfg.tmux_layout)
        assert failed == []
        assert slots(grown["workers-2"]) == ["2", "3"]
        assert slots(grown["workers-3"]) == ["4"]
    finally:
        tmux.run(["kill-server"])
        shutil.rmtree(sockdir, ignore_errors=True)


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


# -- configurable layout --------------------------------------------------
@pytest.mark.parametrize(
    "name, expected",
    [
        ("auto", "auto"),
        ("even-vertical", "even-vertical"),
        ("EVEN-HORIZONTAL", "even-horizontal"),
        (" top-bottom ", "even-vertical"),
        ("side-by-side", "even-horizontal"),
        ("stacked", "even-vertical"),
        ("grid", "tiled"),
        ("", "auto"),
    ],
)
def test_normalize_layout(name, expected):
    assert tmux.normalize_layout(name) == expected


def test_normalize_layout_rejects_unknown():
    with pytest.raises(ValueError, match="unknown tmux layout"):
        tmux.normalize_layout("diagonal")


@pytest.mark.parametrize(
    "count, layout, expected",
    [
        (1, "auto", None),
        (1, "even-vertical", None),  # a lone pane already fills its window
        (2, "auto", "even-horizontal"),
        (3, "auto", "tiled"),
        (2, "even-vertical", "even-vertical"),
        (4, "even-vertical", "even-vertical"),
        (2, "main-horizontal", "main-horizontal"),
    ],
)
def test_preset_for(count, layout, expected):
    assert tmux.preset_for(count, layout) == expected


@pytest.mark.parametrize(
    "layout, flag",
    [
        ("even-vertical", "-v"),
        ("main-horizontal", "-v"),
        ("even-horizontal", "-h"),
        ("main-vertical", "-h"),
        ("tiled", None),
    ],
)
def test_split_layout_pinned(monkeypatch, layout, flag):
    """A pinned layout drives BOTH the split direction and the locked preset."""
    fake = _FakeTmux()
    monkeypatch.setattr(tmux, "run", fake.run)

    panes = tmux.split_layout("@1", 2, layout)

    assert len(panes) == 2
    expected = ["split-window", *([flag] if flag else []), "-t", "@1", "sleep infinity"]
    assert _splits(fake.calls) == [expected]
    assert [c[-1] for c in fake.calls if c[0] == "select-layout"] == [layout]


def test_apply_layout_pinned(monkeypatch):
    fake = _FakeTmux()
    monkeypatch.setattr(tmux, "run", fake.run)

    tmux.apply_layout("@1", 2, "even-vertical")
    tmux.apply_layout("@1", 1, "even-vertical")  # single pane -> untouched

    assert _splits(fake.calls) == []
    assert [c[-1] for c in fake.calls if c[0] == "select-layout"] == ["even-vertical"]


def _load_with_layout(tmp_path: Path, body: str):
    (tmp_path / ".swarm.toml").write_text(body, encoding="utf-8")
    return config_mod.load(project_dir=str(tmp_path))


def test_config_layout_default_is_auto(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_LAYOUT", raising=False)
    assert _load_with_layout(tmp_path, "[swarm]\nmax_workers = 2\n").tmux_layout == "auto"


def test_config_layout_alias_is_normalized(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_LAYOUT", raising=False)
    cfg = _load_with_layout(tmp_path, '[tmux]\nlayout = "top-bottom"\n')
    assert cfg.tmux_layout == "even-vertical"


def test_config_layout_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_LAYOUT", "tiled")
    cfg = _load_with_layout(tmp_path, '[tmux]\nlayout = "even-vertical"\n')
    assert cfg.tmux_layout == "tiled"


def test_config_layout_typo_fails_loudly(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_LAYOUT", raising=False)
    with pytest.raises(ValueError, match="unknown tmux layout"):
        _load_with_layout(tmp_path, '[tmux]\nlayout = "top_bottom"\n')
