"""Tests for the disk tab's measuring layer.

Only the pure half is tested: :func:`~swarm_orchestrator.tui.disk.scan` and the
two formatters. That is where the bugs that matter live — a walk that follows a
worktree's ``target`` symlink counts the shared cargo cache twice and doubles the
reported size, and a walk that bills a hardlinked file per link does the
same thing more quietly.

The other property under test is that nothing raises. This runs on a worker
thread against a tree the swarm is actively rewriting, so a missing state dir, a
worktree discarded mid-walk or a directory the owner cannot read must all degrade
to a number, never to an exception that takes the tab down.
"""

from __future__ import annotations

import os

import pytest

from swarm_orchestrator import config
from swarm_orchestrator.tui import disk

MB = 1024 * 1024


def write(path, megabytes: float) -> None:
    """A file that actually occupies blocks (sizes are measured as allocation)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * int(megabytes * MB))


def sizes(report) -> dict[str, int]:
    return {e.label: e.size for e in report.entries}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A real :class:`Config` whose state dir is a temp tree we can fill."""
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("SWARM_GIT_REPOS", raising=False)
    return config.load(project_dir=str(project))


@pytest.fixture
def populated(cfg):
    """The layout a run leaves behind: a mirror, a shared cache, sentinels."""
    write(cfg.build_cache_dir / "pricing" / "debug" / "blob", 6)
    write(cfg.wt_dir / "P1" / "src" / "main.rs", 3)
    write(cfg.done_dir / "P1.ok", 1)
    write(cfg.state_path, 0.25)
    write(cfg.supervisor_log, 0.25)
    return cfg


# -- formatting -----------------------------------------------------------
@pytest.mark.parametrize(
    "value,text",
    [
        (None, "—"),
        (0, "0B"),
        (512, "512B"),
        (12 * 1024, "12K"),
        (840 * 1024**2, "840M"),
        (int(12.4 * 1024**3), "12.4G"),
        (694 * 1024**3, "694G"),
        (1024**4, "1.0T"),
        # A hair under a gigabyte must not render as "1024M".
        (1024**3 - 1, "1.0G"),
    ],
)
def test_fmt_bytes_reads_like_a_size_column(value, text):
    assert disk.fmt_bytes(value) == text


# -- ranking --------------------------------------------------------------
def test_entries_rank_largest_first(populated):
    report = disk.scan(populated)
    labels = [e.label for e in report.entries]
    assert labels[:3] == ["build cache", "worktree P1", "done/"]
    assert report.largest.label == "build cache"
    assert report.total == sum(e.size for e in report.entries)
    assert not report.partial


def test_every_category_explains_itself(populated):
    """The tab's whole point is "what uses what", so no row may be unlabelled."""
    report = disk.scan(populated)
    for entry in report.entries:
        assert entry.what, entry.label
        assert entry.path


def test_volume_is_measured(populated):
    report = disk.scan(populated)
    assert report.volume.total > 0
    assert report.volume.free > 0
    assert 0 <= report.volume.pct_used <= 100


# -- de-duplication -------------------------------------------------------
def test_hardlinks_are_billed_once(cfg):
    blob = cfg.wt_dir / "P1" / "blob"
    write(blob, 4)
    os.link(blob, blob.parent / "blob-2")
    os.link(blob, blob.parent / "blob-3")

    got = sizes(disk.scan(cfg))["worktree P1"]
    assert 4 * MB <= got < 5 * MB


def test_symlinks_are_not_followed(cfg):
    """A worktree's ``target`` points into the shared cache, and there is 8 MiB
    of unrelated tree outside the state dir. Neither may be billed to the
    worktree: the first would double-count the cache, the second is not ours."""
    write(cfg.build_cache_dir / "pricing" / "blob", 6)
    write(cfg.state_dir.parent / "outside" / "blob", 8)
    wt = cfg.wt_dir / "P1"
    write(wt / "src" / "main.rs", 1)
    (wt / "target").symlink_to(cfg.build_cache_dir / "pricing")
    (wt / "escape").symlink_to(cfg.state_dir.parent / "outside")

    report = disk.scan(cfg)
    got = sizes(report)
    assert got["worktree P1"] < 2 * MB
    assert 6 * MB <= got["build cache"] < 7 * MB
    assert report.total < 9 * MB


def test_state_rest_excludes_what_was_already_counted(populated):
    """The cache and the worktrees live *inside* the state dir. "state · rest"
    is what remains once they, and every named part, have been billed."""
    write(populated.state_dir / "turns" / "T1.json", 2)

    got = sizes(disk.scan(populated))
    assert 2 * MB <= got["state · rest"] < 3 * MB
    assert got["state · rest"] < got["build cache"]


# -- degradation ----------------------------------------------------------
def test_missing_paths_degrade_to_zero(cfg):
    """Nothing has been created: every category reads 0, nothing raises."""
    report = disk.scan(cfg)
    got = sizes(report)
    for label in ("build cache", "state.json", "done/", "recaps/", "supervisor log"):
        assert got[label] == 0
    assert not any(e.label.startswith("worktree ") for e in report.entries)
    assert report.volume.total > 0


def test_unreadable_directory_is_a_floor_not_an_error(cfg):
    write(cfg.wt_dir / "P1" / "locked" / "blob", 1)
    (cfg.wt_dir / "P1" / "locked").chmod(0o000)
    try:
        report = disk.scan(cfg)
    finally:
        (cfg.wt_dir / "P1" / "locked").chmod(0o755)
    entry = next(e for e in report.entries if e.label == "worktree P1")
    # Root can read it anyway; the point is only that the scan survived it.
    assert entry.partial or entry.size >= MB


def test_deadline_bounds_the_walk(populated):
    report = disk.scan(populated, deadline_s=0.0)
    assert report.partial
    assert report.volume.total > 0


# -- the home line --------------------------------------------------------
def test_summary_names_the_biggest_consumer(populated):
    line = disk.summary(disk.scan(populated))
    assert "used by this swarm" in line
    assert "free" in line
    assert "biggest build cache" in line
    assert "\n" not in line


def test_summary_fits_the_width_it_is_given(populated):
    report = disk.scan(populated)
    for width in (80, 44, 30, 12):
        assert len(disk.summary(report, width=width)) <= width


def test_summary_without_a_scan_says_so(cfg):
    """Home must never trigger a walk, so with no report it offers the one fact
    a single stat call can prove — and refuses to guess at the rest."""
    line = disk.summary(None, cfg)
    assert "free" in line
    assert "not measured" in line
    assert disk.summary(None) == "disk not measured"
