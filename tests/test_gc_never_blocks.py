"""Worktree cleanup is housekeeping and must never fail an integration.

Regression guard: a phase merged into a repo and pushed, then
`git worktree remove --force` on that worktree timed out (a checkout with a very
large dependency tree is slow to delete on a busy box). If `_gc` raises, the
integrator classifies it as a DIRTY hold, and a *successful* merge is reported
to the owner as a blocked integration holding the entire queue.

`check=False` looked like it covered this and did not: a timeout raises a
`GitError` regardless of `check`, because the process never produced a return
code to ignore. The lesson is narrow and worth keeping — "best effort" has to
handle the call not *finishing*, not merely the call failing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm_orchestrator import gitq


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def line(self, text: str) -> None:
        self.lines.append(text)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from swarm_orchestrator import config as config_mod

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "gctest")
    monkeypatch.setenv("SWARM_DRIVER", "none")
    return config_mod.load(project_dir=str(project))


@pytest.fixture
def repo(cfg):
    """A component repo INSIDE the project — `_wt_for` resolves relative to it."""
    r = cfg.project_dir / "frontend"
    r.mkdir(parents=True, exist_ok=True)
    return r


def test_gc_swallows_a_removal_timeout(cfg, repo, monkeypatch):
    """A timed-out `worktree remove` must not propagate — it merely logs."""
    wt = gitq._wt_for(cfg, repo, "P1")
    wt.mkdir(parents=True, exist_ok=True)

    def _boom(repo, *args, **kw):
        if args[:2] == ("worktree", "remove"):
            raise gitq.GitError("git worktree remove: timed out after 900.0s")
        return None

    monkeypatch.setattr(gitq, "_git", _boom)
    monkeypatch.setattr(gitq, "_branch_exists", lambda *a, **k: False)

    log = _Log()
    gitq._gc(cfg, repo, "P1", log)  # must not raise

    assert any("WORKTREE-GC-FAILED" in ln for ln in log.lines), log.lines


def test_gc_swallows_a_prune_timeout(cfg, repo, monkeypatch):
    """The prune step is equally best-effort."""

    def _boom(repo, *args, **kw):
        if args[:2] == ("worktree", "prune"):
            raise gitq.GitError("git worktree prune: timed out after 900.0s")
        return None

    monkeypatch.setattr(gitq, "_git", _boom)
    monkeypatch.setattr(gitq, "_branch_exists", lambda *a, **k: False)

    log = _Log()
    gitq._gc(cfg, repo, "P1", log)
    assert any("WORKTREE-GC-FAILED" in ln for ln in log.lines), log.lines


def test_gc_uses_a_generous_timeout_for_removal(cfg, repo, monkeypatch):
    """Removal is an IO-bound tree walk; the ordinary git timeout is too short."""
    wt = gitq._wt_for(cfg, repo, "P1")
    wt.mkdir(parents=True, exist_ok=True)
    seen: dict[str, float] = {}

    def _spy(repo, *args, **kw):
        if args[:2] == ("worktree", "remove"):
            seen["remove"] = kw.get("timeout", gitq._GIT_TIMEOUT_S)
        return None

    monkeypatch.setattr(gitq, "_git", _spy)
    monkeypatch.setattr(gitq, "_branch_exists", lambda *a, **k: False)

    gitq._gc(cfg, repo, "P1", _Log())
    assert seen["remove"] > gitq._GIT_TIMEOUT_S, seen


def test_gc_is_silent_when_nothing_goes_wrong(cfg, repo, monkeypatch):
    monkeypatch.setattr(gitq, "_git", lambda repo, *a, **k: None)
    monkeypatch.setattr(gitq, "_branch_exists", lambda *a, **k: False)
    log = _Log()
    gitq._gc(cfg, repo, "P1", log)
    assert not any("GC-FAILED" in ln for ln in log.lines), log.lines
