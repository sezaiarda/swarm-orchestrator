"""A resolver that never got its brief must not be left holding the merge queue.

What happened: a lane check went red, the queue was held and a resolver window
opened. Its brief was typed as one long line; claude folded it into ``[Pasted
text #1]``, the Enter was swallowed, and the session sat at an empty transcript.
``spawn`` still returned the window, so the supervisor recorded "a resolver is on
it", kept the owner's message back, and waited for a ``swarm resolved`` that an
idle session never runs. The Overseer's ten-minute hold trigger was the only
thing that noticed.

Pinned here: the brief goes to a file and the pane gets one short line (the
cure the operator's and the Overseer's sessions already have); a session that
did not take its line is closed and one more is opened; and when that fails too
``spawn`` says so, which is what tells the owner and the Overseer at once.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from swarm_orchestrator import config as config_mod
from swarm_orchestrator import gitq
from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import resolver as resolver_mod
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import supervisor as sup_mod
from swarm_orchestrator import tmux

#: Longer than the ~800 characters past which claude folds typed input, as a
#: lane-mode brief is on a real project (it names five long paths).
LANE_BRIEF = "Read the prompt and follow it exactly, in its lane mode. " + "path " * 300


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def line(self, text: str) -> None:
        self.lines.append(text)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "repo").mkdir(parents=True)
    (project / "docs").mkdir()
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\nP1 needs:P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_SLUG", "resolverdelivery")
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_OVERSEER", "1")
    for leak in ("SWARM_RESOLVER_CMD", "SWARM_GIT_ISOLATION"):
        monkeypatch.delenv(leak, raising=False)
    cfg = config_mod.load(project_dir=str(project))
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    return cfg


@pytest.fixture
def pane(cfg, monkeypatch):
    """A tmux that opens numbered windows and records what the resolver does to
    them. ``results`` is what each send into a pane proves, in order (the last
    one repeats); ``ready`` likewise for the boot wait. Nothing reaches a real
    tmux server or a real process."""
    seen = SimpleNamespace(opened=[], killed=[], typed=[], reaped=[],
                           results=[tmux.DELIVERED], ready=[True])

    def new_window(*_a, **_k) -> str:
        seen.opened.append(f"@{len(seen.opened) + 1}")
        return seen.opened[-1]

    def take(queue: list):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def send(pane_id: str, line: str, *_a, **_k) -> str:
        seen.typed.append((pane_id, line))
        return take(seen.results)

    cfg.driver = "tmux"
    monkeypatch.setattr(tmux, "new_window", new_window)
    monkeypatch.setattr(tmux, "list_panes", lambda win: [win.replace("@", "%")])
    monkeypatch.setattr(tmux, "respawn_pane", lambda *a, **k: None)
    monkeypatch.setattr(tmux, "kill_window", seen.killed.append)
    monkeypatch.setattr(tmux, "send_submit_ex", send)
    monkeypatch.setattr(tmux, "send_submit", lambda *a, **k: send(*a, **k) in tmux.SUBMIT_OK)
    monkeypatch.setattr(launch_mod, "pretrust_dir", lambda *a, **k: None)
    monkeypatch.setattr(launch_mod, "await_ready", lambda *a, **k: take(seen.ready))
    monkeypatch.setattr(session_mod, "reap_session",
                        lambda _cfg, kind, ident, *a, **k: seen.reaped.append(f"{kind}:{ident}"))
    return seen


def _spawn(cfg, log=None, line=None):
    return resolver_mod.spawn(cfg, "P1", cfg.project_dir / "repo", log or _Log(), line=line)


# -- the brief ------------------------------------------------------------------
def test_a_long_brief_goes_to_a_file_and_the_pane_gets_one_short_line(cfg, pane):
    assert _spawn(cfg, line=LANE_BRIEF) == "@1"

    [(pane_id, typed)] = pane.typed
    assert pane_id == "%1"
    assert len(typed) < 400  # far under the length claude folds
    brief = resolver_mod.brief_path(cfg, "P1")
    assert str(brief) in typed
    assert brief.read_text(encoding="utf-8").strip() == LANE_BRIEF.strip()


def test_the_default_brief_names_the_repo_the_branch_and_the_way_out(cfg, pane):
    assert _spawn(cfg) == "@1"

    brief = resolver_mod.brief_path(cfg, "P1").read_text(encoding="utf-8")
    assert str(cfg.project_dir / "repo") in brief
    assert "swarm/P1" in brief and "`swarm resolved P1`" in brief
    assert "swarm resolved" not in pane.typed[0][1]  # the line only points at it


def test_closing_the_resolver_removes_its_brief(cfg, pane):
    win = _spawn(cfg)
    resolver_mod.close(cfg, win, _Log(), "P1")

    assert not resolver_mod.brief_path(cfg, "P1").exists()
    assert pane.killed == ["@1"] and pane.reaped == ["resolver:P1"]


# -- a session that did not take its line ----------------------------------------
@pytest.mark.parametrize("lost", [tmux.UNCONFIRMED, tmux.NOT_DELIVERED])
def test_a_resolver_that_lost_its_line_is_closed_and_another_is_opened(cfg, pane, lost):
    pane.results = [lost, tmux.DELIVERED]
    log = _Log()

    assert _spawn(cfg, log) == "@2"

    assert pane.killed == ["@1"]  # the idle one is gone, not left beside its successor
    # A fresh session each time: nothing is typed twice into one box.
    assert [p for p, _ in pane.typed] == ["%1", "%2"]
    assert f"RESOLVER-SUBMIT-LOST P1 {lost}" in log.lines
    assert "RESOLVER-RETRY P1 attempt=2" in log.lines
    assert log.lines[-1] == "RESOLVER-SPAWN P1 repo=repo win=@2"


def test_a_line_lost_twice_fails_the_spawn(cfg, pane):
    pane.results = [tmux.UNCONFIRMED]
    log = _Log()

    assert _spawn(cfg, log) is None

    assert pane.opened == ["@1", "@2"] and pane.killed == ["@1", "@2"]
    assert "RESOLVER-SPAWN-FAIL P1 brief-not-delivered" in log.lines
    assert not any(ln.startswith("RESOLVER-SPAWN P1") for ln in log.lines)


def test_a_resolver_that_never_becomes_ready_is_not_reported_as_started(cfg, pane):
    pane.ready = [False]
    log = _Log()

    assert _spawn(cfg, log) is None

    assert pane.typed == [] and pane.killed == ["@1", "@2"]
    assert log.lines.count("RESOLVER-READY-TIMEOUT P1") == 2


def test_a_slow_first_boot_is_given_a_second_session(cfg, pane):
    pane.ready = [False, True]

    assert _spawn(cfg) == "@2"
    assert pane.killed == ["@1"]


def test_a_custom_resolver_cmd_is_started_once_and_handed_nothing(cfg, pane):
    cfg.resolver_cmd = "my-resolver"

    assert _spawn(cfg) == "@1"
    assert pane.typed == [] and pane.killed == []


# -- the reaper of the last session must not meet the next one -------------------
def test_the_next_resolver_waits_for_the_last_ones_reaper(cfg, pane, monkeypatch):
    """A resolver's processes are found by ``resolver:<phase>``, a moment after
    its window closes. The next session for the same phase carries the same
    marker, so started before that look it would be ended by it."""
    looked = threading.Event()

    def reap(_cfg, kind, ident, *_a, **_k) -> threading.Thread:
        thread = threading.Thread(target=lambda: (time.sleep(0.2), looked.set()))
        thread.start()
        return thread

    started: list[bool] = []
    monkeypatch.setattr(session_mod, "reap_session", reap)
    monkeypatch.setattr(tmux, "respawn_pane", lambda *a, **k: started.append(looked.is_set()))

    resolver_mod.close(cfg, "@0", _Log(), "P1")
    assert _spawn(cfg) == "@1"

    assert started == [True]


# -- what the supervisor does with a resolver that would not start ----------------
def _sent(cfg) -> str:
    sink = Path(str(cfg.state_dir)).parent / "tg.log"
    return sink.read_text(encoding="utf-8") if sink.exists() else ""


@pytest.mark.parametrize("results, started", [
    ([tmux.DELIVERED], True),
    ([tmux.UNCONFIRMED, tmux.DELIVERED], True),
    ([tmux.UNCONFIRMED], False),
])
def test_a_hold_whose_resolver_never_took_its_brief_is_escalated_at_once(
    cfg, pane, results, started
):
    pane.results = results
    cfg.driver = "bare"
    sup = sup_mod.Supervisor(cfg)
    cfg.driver = "tmux"
    try:
        sup._hold("P1", gitq.CONFLICT, cfg.project_dir / "repo", None)
        st = state_mod.read(cfg)
        # No window on record is what lets the Overseer look now, not after
        # `hold_wait_s` of silence.
        assert ("resolve:P1" in st.windows) is started
        assert sup.overseer._hold_trigger(st, time.time()) == (
            "" if started else "no resolver is on it")
    finally:
        sup.log.close()
    assert st.integ_blocked == "P1"
    assert ("the resolver would not start" in _sent(cfg)) is not started
