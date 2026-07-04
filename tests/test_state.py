"""Unit tests: slot claim/flock, dependency resolver, FIFO line format."""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from swarm_orchestrator import ledger
from swarm_orchestrator.state import State


# -- dependency resolver --------------------------------------------------
def test_parse_and_ready_gating():
    graph = ledger.parse(
        "P0\nP1 needs:P0\nP2 needs:P0\nP4 needs:P1,P2 trailing note\n# comment\n"
    )
    assert graph == {"P0": set(), "P1": {"P0"}, "P2": {"P0"}, "P4": {"P1", "P2"}}
    # nothing done -> only the root is ready
    assert ledger.ready(graph, {}, set(), set()) == ["P0"]
    # P0 done -> P1,P2 ready; P4 still gated on P2
    assert ledger.ready(graph, {"P0": "ok"}, set(), set()) == ["P1", "P2"]
    # P1,P2 done -> P4 ready
    assert ledger.ready(graph, {"P0": "ok", "P1": "ok", "P2": "ok"}, set(), set()) == [
        "P4"
    ]


def test_ready_excludes_busy_and_excluded():
    graph = ledger.parse("P0\nP1 needs:P0\nP2 needs:P0\n")
    done = {"P0": "ok"}
    # P1 busy, P2 excluded -> neither ready
    assert ledger.ready(graph, done, {"P1"}, {"P2"}) == []


def test_prose_ledger_yields_no_false_phases():
    # A real markdown ledger line should not be misread as a phase declaration.
    graph = ledger.parse("## Phase 0 — the walking skeleton (done)\n- bullet\n")
    assert graph == {}


# -- slot accounting (in-memory) ------------------------------------------
def test_claim_and_free_slots():
    st = State.fresh(4)
    claimed = [st.claim_slot(f"P{i}") for i in range(4)]
    assert [s.id for s in claimed] == [0, 1, 2, 3]
    assert st.claim_slot("P4") is None  # no free slot
    assert st.any_busy()
    freed = st.free_slot_for("P2")
    assert freed is not None and freed.id == 2
    assert not freed.busy and freed.phase is None
    # freed slot is reused first
    assert st.claim_slot("P5").id == 2


# -- flock check-and-set under real concurrency ---------------------------
def test_concurrent_launch_never_double_claims(swarm):
    """8 concurrent `swarm launch` against 4 slots -> exactly 4 claim."""
    swarm.env["FAKE_WORKER_PARK"] = "1"  # parked workers: hold slot, never done

    results: list[int] = []
    lock = threading.Lock()

    def launch(phase: str) -> None:
        proc = swarm.cli("launch", phase, check=False, timeout=20)
        with lock:
            results.append(proc.returncode)

    threads = [threading.Thread(target=launch, args=(f"P{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert swarm.busy_count() == 4  # never 5+, never a crash
    assert results.count(0) == 4  # exactly four succeeded
    assert results.count(1) == 4  # four denied (no free slot)


# -- FIFO poke line format (the counterpart of the supervisor parser) -----
def test_done_writes_sentinel_and_fifo_line(swarm):
    swarm.state_dir.mkdir(parents=True, exist_ok=True)
    fifo = swarm.state_dir / "control.fifo"
    os.mkfifo(fifo)
    # Hold the FIFO open O_RDWR (as the supervisor does) so the poke lands.
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        swarm.cli("done", "P1", "ok", "some note", timeout=10)
        # sentinel written atomically
        assert (swarm.state_dir / "done" / "P1.ok").is_file()
        line = os.read(fd, 4096).decode()
        assert line == "done P1 ok\n"
    finally:
        os.close(fd)
