"""Lanes end-to-end: a real bare-driver supervisor, lanes on, two slots.

Two rows in one repo (lane ``.``) whose touches are disjoint run together; the
same two rows with overlapping touches run one after the other. The supervisor
is the separate process the ``swarm`` fixture starts, driven by the fake master
and worker scripts, so this proves the scheduler, the launch path and the lane
snapshot in state.json agree, not just :func:`lanes.pick` on its own.
"""

from __future__ import annotations

import time

import pytest

DISJOINT = ("./src/a.txt", "./src/b.txt")
OVERLAP = ("./src/**", "./src/b.txt")


def _setup(swarm, touch_a: str, touch_b: str) -> None:
    (swarm.project / "ledger.md").write_text(
        "# Lanes e2e ledger\n\n"
        f"- [ ] `A` · needs:— · touches:`{touch_a}` · **first**\n"
        f"- [ ] `B` · needs:— · touches:`{touch_b}` · **second**\n",
        encoding="utf-8",
    )
    toml = swarm.project / ".swarm.toml"
    text = (
        toml.read_text(encoding="utf-8")
        .replace("max_workers = 4", "max_workers = 2")
        .replace('ledger = "ledger.txt"', 'ledger = "ledger.md"')
    )
    toml.write_text(text + "\n[lanes]\nenabled = true\n", encoding="utf-8")
    swarm.env["FAKE_WORKER_SLEEP"] = "2"


def _run_until_finished(swarm, timeout: float = 40) -> list[tuple[list[str], dict]]:
    """Poll state.json until the run finishes; every distinct (busy, lanes) seen."""
    seen: list[tuple[list[str], dict]] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = swarm.state()
        if st:
            sample = (sorted(s["phase"] for s in st["slots"] if s["busy"]),
                      dict(st.get("lanes") or {}))
            if not seen or seen[-1] != sample:
                seen.append(sample)
            if st.get("finished") and "ACTION finish" in swarm.log_text():
                return seen
        time.sleep(0.02)
    pytest.fail(f"run did not finish; samples={seen}\n{swarm.log_text()}")


def _index(log: str, needle: str) -> int:
    idx = log.find(needle)
    assert idx >= 0, f"no {needle!r} in:\n{log}"
    return idx


def _launched(log: str, phase: str) -> int:
    return _index(log, f"LAUNCH {phase} slot=")


def _done(log: str, phase: str) -> int:
    return _index(log, f"EVENT done {phase} ok ")


def test_disjoint_touches_in_one_repo_run_together(swarm):
    _setup(swarm, *DISJOINT)
    swarm.up()

    seen = _run_until_finished(swarm)
    log = swarm.log_text()
    assert any(busy == ["A", "B"] for busy, _ in seen), (seen, log)
    # Both held their snapshotted lane at the same moment.
    assert any(lanes == {"A": ["./src/a.txt"], "B": ["./src/b.txt"]}
               for _, lanes in seen), (seen, log)
    # Both launched before either reported done.
    assert max(_launched(log, "A"), _launched(log, "B")) < min(_done(log, "A"), _done(log, "B"))
    assert "LAUNCH-DENIED" not in log, log
    st = swarm.state()
    assert set(st["done"]) >= {"A", "B"}
    assert not st.get("lanes"), st.get("lanes")  # released on done


def test_overlapping_touches_in_one_repo_run_in_turn(swarm):
    _setup(swarm, *OVERLAP)
    swarm.up()

    seen = _run_until_finished(swarm)
    log = swarm.log_text()
    assert all(len(busy) <= 1 for busy, _ in seen), (seen, log)
    assert all(len(lanes) <= 1 for _, lanes in seen), (seen, log)
    assert any(busy == ["A"] for busy, _ in seen), (seen, log)
    assert any(busy == ["B"] for busy, _ in seen), (seen, log)
    # Ledger order: A (the wider lane) first, B only once A is done.
    assert _launched(log, "A") < _done(log, "A") < _launched(log, "B") < _done(log, "B"), log
    assert "LAUNCH-DENIED" not in log, log
    st = swarm.state()
    assert set(st["done"]) >= {"A", "B"}
    assert not st.get("lanes"), st.get("lanes")
