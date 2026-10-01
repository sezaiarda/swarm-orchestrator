"""gc and the build gate, with real processes: gc takes its turn through the
build queue and never sits on one slot while it waits for another.

Each build is the fake ``cargo`` of :mod:`test_build_queue`; gc is the real
:func:`gc.auto` on a thread (the supervisor's) or in a process of its own.
"""

from __future__ import annotations

import threading
import time

import pytest
from test_build_queue import Gate, _finish

from swarm_orchestrator import gc


@pytest.fixture
def gate(tmp_path):
    made: list[Gate] = []

    def make(**kw) -> Gate:
        made.append(Gate(tmp_path, **kw))
        return made[-1]

    yield make
    for g in made:
        g.cleanup()


def _cfg(g: Gate, wait_s: float, hold_s: float):
    cfg = g.cfg()
    cfg.gc_wait_s, cfg.gc_hold_s = wait_s, hold_s
    return cfg


def _auto(cfg) -> tuple[threading.Thread, dict]:
    """The interval fires: one automatic gc, on a thread as in the supervisor."""
    out: dict = {}

    def run() -> None:
        out["result"] = gc.auto(cfg)
        out["at"] = time.time()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, out


def _long_build_on_slot_1(g: Gate, dur: float):
    first = g.start("first", dur=1.2)
    g.wait_event("start", "P-first")
    long = g.start("long", dur=dur)
    assert g.wait_event("start", "P-long")["slot"] == 1
    first.wait(timeout=20)  # slot 0 is free again
    return long


# -- the bug: gc sat on slot 0 while it waited for slot 1 ----------------------
@pytest.mark.xfail(strict=True, reason="gc takes slot 0 and keeps it while it waits for slot 1")
def test_a_gc_waiting_for_a_long_build_leaves_the_free_slot_to_builds(gate):
    """Two slots, a long build on slot 1, and the gc interval fires. gc cannot
    run until that build ends; until then slot 0 belongs to the builds."""
    g = gate(max_concurrent=2)
    long = _long_build_on_slot_1(g, dur=7.0)
    th, out = _auto(_cfg(g, wait_s=3, hold_s=1))
    time.sleep(0.6)  # gc is waiting for the gate
    b = g.start("b", dur=0.2)
    b.wait(timeout=20)
    start = g.wait_event("start", "P-b")
    th.join(20)
    assert start["slot"] == 0 and start["wait_s"] < 1.0  # not behind gc's whole wait
    assert start["ts"] < out["at"] - 1.0  # it ran while gc was still waiting
    assert out["result"].outcome == gc.AUTO_BUSY
    _finish([long])
