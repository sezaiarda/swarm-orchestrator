"""``swarm freeze`` and ``swarm thaw`` against each other and against what can
cut them short.

* **one at a time** — the two exclude each other from first step to last, so a
  run always ends as the verb that came last asked, its record and its groups
  in agreement;
* **nothing strands** — a group is named in the record before it is told to
  freeze, a thaw says it has begun before it wakes anything, and a record is
  dropped whatever becomes of the bookkeeping around it;
* **could not tell is not thawed** — a state that cannot be read changes
  nothing, for the supervisor and for ``swarm thaw``.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from swarm_orchestrator import cli
from swarm_orchestrator import freezer
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.supervisor import FROZEN_POLL_S, Supervisor

from test_freeze import _record, _spans, cfg, stand_in  # noqa: F401 - two are fixtures

WORKERS = ("/run/worker-P0", "/run/worker-P1", "/run/worker-P2")


@pytest.fixture
def three(cfg, fake_cgroups, stand_in):  # noqa: F811
    """Three workers, each in its group."""
    for phase in ("P0", "P1", "P2"):
        stand_in(f"worker:{phase}")
    fake_cgroups.settle()


def _until(pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _in_thread(fn) -> tuple[threading.Thread, dict]:
    out: dict = {}

    def body() -> None:
        try:
            out["rc"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            out["raised"] = exc

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread, out


def _agree(cfg, fake_cgroups, frozen: bool) -> dict:  # noqa: F811
    """The record and the groups say the same thing; returns the record."""
    record = state_mod.read(cfg).frozen
    told = {path: fake_cgroups.told(path) for path in WORKERS}
    if frozen:
        assert record["stage"] == freezer.FROZEN, record
        assert sorted(g["path"] for g in record["cgroups"]) == sorted(WORKERS)
        assert set(told.values()) == {"1"}, told
    else:
        assert record == {} and set(told.values()) == {"0"}, (record, told)
    return record


# -- one at a time ----------------------------------------------------------------
def test_a_freeze_that_arrives_during_a_thaw_waits_then_freezes_from_scratch(
        cfg, fake_cgroups, three):  # noqa: F811
    assert cli.cmd_freeze(cfg) == 0
    first = state_mod.read(cfg).frozen["since"]

    thaw, thawed = _in_thread(lambda: cli.cmd_thaw(cfg, gap_s=0.4))
    # One group is awake and two are still to come: the thaw is half way.
    assert _until(lambda: fake_cgroups.told(WORKERS[0]) == "0")
    assert fake_cgroups.told(WORKERS[2]) == "1"
    assert state_mod.read(cfg).frozen["stage"] == freezer.THAWING  # said before the first wake

    assert cli.cmd_freeze(cfg) == 0  # waits for the thaw, then freezes

    thaw.join(10)
    assert thawed == {"rc": 0}
    record = _agree(cfg, fake_cgroups, frozen=True)  # the freeze came last
    assert record["since"] > first and "shifted" not in record and "until" not in record
    assert [s["since"] for s in _spans(cfg)] == [first]  # the first freeze ended, whole
    log = cfg.supervisor_log.read_text()
    assert log.index("THAW groups=3") < log.rindex("FREEZE-START groups=3")
    assert cli.cmd_thaw(cfg, gap_s=0) == 0
    _agree(cfg, fake_cgroups, frozen=False)


def test_a_thaw_that_arrives_during_a_freeze_waits_then_wakes_what_it_froze(
        cfg, fake_cgroups, three, monkeypatch):  # noqa: F811
    real, under_way = cli._poke, threading.Event()

    def slow(cfg_, line):
        if line == "freeze":  # between the record and the groups: the freeze is half way
            under_way.set()
            time.sleep(0.6)
        return real(cfg_, line)

    monkeypatch.setattr(cli, "_poke", slow)
    freeze, froze = _in_thread(lambda: cli.cmd_freeze(cfg))
    assert under_way.wait(10)
    assert state_mod.read(cfg).frozen["stage"] == freezer.FREEZING

    assert cli.cmd_thaw(cfg, gap_s=0) == 0  # waits for the freeze, then thaws

    freeze.join(10)
    assert froze == {"rc": 0}  # the freeze was made whole, not found half undone
    _agree(cfg, fake_cgroups, frozen=False)  # the thaw came last
    assert len(_spans(cfg)) == 1
    log = cfg.supervisor_log.read_text()
    assert log.index("FROZEN groups=3") < log.index("THAW groups=3")
    assert "FREEZE-ROLLBACK" not in log


def test_a_verb_that_never_gets_its_turn_changes_nothing_and_says_so(
        cfg, fake_cgroups, three, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(freezer, "TURN_S", 0.3)
    with freezer.turn(cfg) as other:  # the other verb, still running
        assert other
        assert cli.cmd_freeze(cfg) == 1
        assert "still running after 0.3s; nothing was changed" in capsys.readouterr().err
        _agree(cfg, fake_cgroups, frozen=False)

    assert cli.cmd_freeze(cfg) == 0
    before = cfg.state_path.read_text()
    with freezer.turn(cfg):
        assert cli.cmd_thaw(cfg, gap_s=0) == 1
        assert "swarm thaw: another `swarm freeze` or `swarm thaw`" in capsys.readouterr().err
    assert cfg.state_path.read_text() == before
    _agree(cfg, fake_cgroups, frozen=True)


def test_a_freeze_after_a_thaw_that_was_cut_short_finishes_it_first(
        cfg, fake_cgroups, three, monkeypatch, capsys):  # noqa: F811
    assert cli.cmd_freeze(cfg) == 0
    first = state_mod.read(cfg).frozen["since"]
    real = freezer.thaw

    def cut(groups, cg=None, gap_s=0.0, **kw):
        real(groups[:1], cg)
        raise KeyboardInterrupt  # killed with one group awake and two frozen

    monkeypatch.setattr(freezer, "thaw", cut)
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_thaw(cfg, gap_s=0)
    monkeypatch.setattr(freezer, "thaw", real)
    assert state_mod.read(cfg).frozen["stage"] == freezer.THAWING
    assert [fake_cgroups.told(p) for p in WORKERS] == ["0", "1", "1"]
    capsys.readouterr()
    # Half thawed is said as that, not as frozen.
    assert cli.main(["--project-dir", str(cfg.project_dir), "launch", "P0"]) == 1
    assert "swarm launch: the swarm is being thawed" in capsys.readouterr().err

    assert cli.cmd_freeze(cfg) == 0

    record = _agree(cfg, fake_cgroups, frozen=True)
    assert record["since"] > first
    assert [s["since"] for s in _spans(cfg)] == [first]


# -- nothing strands --------------------------------------------------------------
def test_no_group_is_told_to_freeze_before_the_record_names_it(
        cfg, fake_cgroups, stand_in, monkeypatch):  # noqa: F811
    stand_in("worker:P0")
    fake_cgroups.settle()
    real_poke, real_set, real_settled = cli._poke, freezer.Cgroups.set, freezer.Cgroups.settled

    def late(cfg_, line):
        if line == "freeze":  # a session turns up after the first look at the run
            stand_in("worker:P1")
            fake_cgroups.settle()
        return real_poke(cfg_, line)

    told: list[tuple[str, bool]] = []

    def watching(self, path, frozen):
        if frozen:
            named = {g["path"] for g in freezer.peek(cfg).get("cgroups") or []}
            told.append((path, path in named))
        return real_set(self, path, frozen)

    def cut(self, path):
        raise KeyboardInterrupt  # interrupted while it waits for the groups to settle

    monkeypatch.setattr(cli, "_poke", late)
    monkeypatch.setattr(freezer.Cgroups, "set", watching)
    monkeypatch.setattr(freezer.Cgroups, "settled", cut)

    with pytest.raises(KeyboardInterrupt):
        cli.cmd_freeze(cfg)

    assert told == [("/run/worker-P0", True), ("/run/worker-P1", True)]
    assert fake_cgroups.told("/run/worker-P0") == fake_cgroups.told("/run/worker-P1") == "1"
    assert state_mod.read(cfg).frozen["stage"] == freezer.FREEZING
    monkeypatch.setattr(freezer.Cgroups, "settled", real_settled)

    assert cli.cmd_thaw(cfg, gap_s=0) == 0  # and so the thaw undoes all of it

    assert fake_cgroups.told("/run/worker-P0") == fake_cgroups.told("/run/worker-P1") == "0"
    assert state_mod.read(cfg).frozen == {}


def test_a_freeze_killed_while_the_groups_settle_can_be_thawed(
        cfg, fake_cgroups, stand_in):  # noqa: F811
    stand_in("worker:P0")
    stand_in("worker:P1")
    fake_cgroups.settle()
    fake_cgroups.stuck.update({"/run/worker-P0", "/run/worker-P1"})  # told, never settled
    proc = subprocess.Popen(
        [sys.executable, "-m", "swarm_orchestrator", "--project-dir", str(cfg.project_dir),
         "freeze"], env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert _until(lambda: fake_cgroups.told("/run/worker-P0") == "1"
                      and fake_cgroups.told("/run/worker-P1") == "1", timeout=30)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(15) == -signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
    record = state_mod.read(cfg).frozen
    assert record["stage"] == freezer.FREEZING
    assert sorted(g["path"] for g in record["cgroups"]) == ["/run/worker-P0", "/run/worker-P1"]

    assert cli.cmd_thaw(cfg, gap_s=0) == 0

    assert fake_cgroups.told("/run/worker-P0") == fake_cgroups.told("/run/worker-P1") == "0"
    assert state_mod.read(cfg).frozen == {}


def test_a_span_or_a_log_that_cannot_be_written_does_not_keep_the_record(
        cfg, fake_cgroups, three, monkeypatch):  # noqa: F811
    assert cli.cmd_freeze(cfg) == 0

    def full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(freezer, "close_span", full)

    assert cli.cmd_thaw(cfg, gap_s=0) == 0

    _agree(cfg, fake_cgroups, frozen=False)  # the run carries on
    assert _spans(cfg) == []
    assert "THAW-SPAN-FAILED" in cfg.supervisor_log.read_text()

    monkeypatch.undo()
    assert cli.cmd_freeze(cfg) == 0
    monkeypatch.setattr(freezer, "close_span", full)
    monkeypatch.setattr(cli, "Log", full)  # and the log cannot be opened either

    assert cli.cmd_thaw(cfg, gap_s=0) == 0

    _agree(cfg, fake_cgroups, frozen=False)


def test_a_thaw_goes_through_with_the_state_lock_in_a_frozen_hand(
        cfg, fake_cgroups, three, monkeypatch):  # noqa: F811
    assert cli.cmd_freeze(cfg) == 0
    monkeypatch.setattr(freezer, "MARK_S", 0.3)
    holding = threading.Event()

    def frozen_session() -> None:
        """Holds the state lock for as long as its group is frozen."""
        with cfg.lock_path.open("w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            holding.set()
            _until(lambda: fake_cgroups.told(WORKERS[0]) == "0", timeout=20)
            fcntl.flock(fh, fcntl.LOCK_UN)

    session = threading.Thread(target=frozen_session, daemon=True)
    session.start()
    assert holding.wait(5)

    thaw, thawed = _in_thread(lambda: cli.cmd_thaw(cfg, gap_s=0))
    thaw.join(15)

    assert not thaw.is_alive() and thawed == {"rc": 0}
    session.join(5)
    _agree(cfg, fake_cgroups, frozen=False)
    assert "THAW-UNMARKED" in cfg.supervisor_log.read_text()


# -- could not tell is not thawed -------------------------------------------------
def test_look_tells_no_record_from_a_state_that_cannot_be_read(cfg):  # noqa: F811
    assert freezer.look(cfg) == {}
    _record(cfg)
    assert freezer.look(cfg)["stage"] == freezer.FROZEN
    cfg.state_path.write_text("{not json", encoding="utf-8")
    assert freezer.look(cfg) is None and freezer.peek(cfg) == {}
    cfg.state_path.write_text("[]", encoding="utf-8")
    assert freezer.look(cfg) is None
    cfg.state_path.unlink()
    assert freezer.look(cfg) == {}  # no state at all: no run, nothing frozen


def test_an_unreadable_state_while_frozen_changes_nothing(
        cfg, fake_cgroups, monkeypatch, capsys):  # noqa: F811
    fake_cgroups.group("/run/worker-P0").joinpath("cgroup.freeze").write_text("1\n")
    _record(cfg, {"path": "/run/worker-P0", "kind": "worker", "id": "P0"})
    sup = Supervisor(cfg)
    restarted: list[bool] = []
    monkeypatch.setattr(sup, "_start_resources", lambda: restarted.append(True))
    sup._freeze_tick()
    sup._deferred.append("done P0 ok")
    since = sup._frozen_since
    good = cfg.state_path.read_text(encoding="utf-8")
    try:
        cfg.state_path.write_text("{not json", encoding="utf-8")

        sup._freeze_tick()  # the look that fails
        sup._handle("thaw 60.000 1.000")  # and a thaw it cannot check

        assert sup._frozen_since == since and sup._deferred == ["done P0 ok"]
        assert sup._select_timeout() == FROZEN_POLL_S and not restarted
        assert "THAWED" not in cfg.supervisor_log.read_text()
        # `swarm thaw` does not take "could not tell" for "nothing is frozen".
        assert cli.cmd_thaw(cfg, gap_s=0) == 1
        io = capsys.readouterr()
        assert "could not be read" in io.err and "nothing is frozen" not in io.out
        assert fake_cgroups.told("/run/worker-P0") == "1"

        cfg.state_path.write_text(good, encoding="utf-8")
        sup._freeze_tick()
        assert sup._frozen_since == since  # readable again, and still frozen
    finally:
        sup.log.close()


def test_a_supervisor_that_is_not_frozen_stays_so_through_an_unreadable_state(
        cfg, fake_cgroups):  # noqa: F811
    sup = Supervisor(cfg)
    try:
        cfg.state_path.write_text("{not json", encoding="utf-8")
        sup._freeze_tick()
        assert sup._frozen_since is None
    finally:
        sup.log.close()


# -- one freeze after another ---------------------------------------------------------
def test_a_thaw_read_after_the_next_freeze_began_still_moves_the_clocks_once(
        cfg, fake_cgroups, monkeypatch):  # noqa: F811
    now = time.time()
    first, second = now - 3600, now - 1  # an hour frozen, thawed, frozen again at once
    sup = Supervisor(cfg)
    monkeypatch.setattr(sup, "_start_resources", lambda: None)
    sup._last_sweep = first - 30
    _record(cfg, since=first)
    try:
        sup._freeze_tick()
        _record(cfg, since=second)  # the next freeze's record is there already

        sup._handle(f"thaw 3599.000 {first:.3f}")

        assert sup._frozen_since == second  # still standing still, for the second
        assert sup._last_sweep == pytest.approx(first - 30 + 3599)
        log = cfg.supervisor_log.read_text()
        assert "THAW-REFROZEN frozen=3599s" in log and "THAWED" not in log

        sup._handle(f"thaw 3599.000 {first:.3f}")  # said twice: moved once
        assert sup._last_sweep == pytest.approx(first - 30 + 3599)

        with state_mod.transaction(cfg) as st:
            st.frozen = {}
        sup._handle(f"thaw 1.000 {second:.3f}")
        assert sup._frozen_since is None
        assert sup._last_sweep == pytest.approx(first - 30 + 3600)
        assert "THAWED frozen=1s" in cfg.supervisor_log.read_text()
    finally:
        sup.log.close()
