"""The status vocabulary: one definition, and the distinctions inside it.

Every set in :mod:`swarm_orchestrator.statuses` answers a different question, and
the failure mode of getting one wrong is silence — a status missing from
``INTEGRATES`` discards the branch of a phase that succeeded, one missing from
``ALL`` makes ``swarm up`` read a finished phase as interrupted. These pin the
memberships that carry a consequence, plus the two sets that look identical and
must never be merged (``PINGS`` / ``OWED_PING``).
"""

from __future__ import annotations

from swarm_orchestrator import cli, gitq, statuses
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load

LEDGER = "- [ ] `P0` · needs:—\n- [ ] `P1` · needs:`P0`\n"


def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_SLUG", "statuses")
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text(LEDGER, encoding="utf-8")
    return load(project_dir=str(tmp_path))


# -- the retired spelling stays readable forever --------------------------
def test_needs_owner_is_readable_but_not_advertised():
    """Retiring a status removes it from what we *write*, never from what we read.

    Live sentinels on disk say `needs-owner` and always will, so every membership
    test a read path makes has to keep answering yes.
    """
    assert statuses.NEEDS_OWNER not in statuses.WRITABLE
    assert statuses.NEEDS_OWNER in statuses.ALL
    assert statuses.NEEDS_OWNER in statuses.INTEGRATES
    assert statuses.NEEDS_OWNER in statuses.SATISFIES_DEPS
    assert statuses.NEEDS_OWNER in statuses.OWED_PING
    assert statuses.NEEDS_OWNER in statuses.ACCEPTED  # still honoured as input


def test_canonical_maps_the_retired_spelling_forward():
    assert statuses.canonical(statuses.NEEDS_OWNER) == statuses.OPERATOR
    assert statuses.canonical(statuses.OK) == statuses.OK


# -- operator lands like ok, and routes instead of pinging ----------------
def test_operator_integrates_and_routes():
    assert statuses.OPERATOR in statuses.ALL
    assert statuses.OPERATOR in statuses.INTEGRATES
    assert statuses.OPERATOR in statuses.SATISFIES_DEPS
    assert statuses.OPERATOR in statuses.ROUTES
    assert statuses.OPERATOR not in statuses.PINGS  # it opens a session instead


def test_pings_and_owed_ping_are_not_the_same_set():
    """Future tense vs past tense — merging them rewrites history either way.

    `PINGS` asks "will finishing like this telegram the owner?"; `OWED_PING` asks
    it of sentinels already written, where `needs-owner` finishes genuinely did
    owe one. One set would either re-flag those forever as `owner-never-pinged`
    or erase that they were owed at all.
    """
    assert statuses.PINGS != statuses.OWED_PING
    assert statuses.NEEDS_OWNER in statuses.OWED_PING
    assert statuses.NEEDS_OWNER not in statuses.PINGS


def test_precedence_puts_integrating_statuses_first():
    """`recap.sentinel` walks this order and the first file that opens wins, so a
    completed build must outrank a `fail`/`skip` left by an earlier attempt."""
    order = list(statuses.PRECEDENCE)
    assert set(statuses.INTEGRATES) <= set(order)
    last_built = max(order.index(s) for s in statuses.INTEGRATES)
    assert last_built < order.index(statuses.FAIL)
    assert last_built < order.index(statuses.SKIP)
    assert set(order) == set(statuses.ALL)


def test_satisfies_deps_is_integrates_plus_skip():
    """A `fail` had its branch discarded; a `skip` is the owner releasing it."""
    assert statuses.SATISFIES_DEPS == statuses.INTEGRATES | {statuses.SKIP}
    assert statuses.FAIL not in statuses.SATISFIES_DEPS


# -- the reconcile seed must survive the merge ----------------------------
def test_reconciled_phase_keeps_its_sentinel_status(tmp_path, monkeypatch):
    """`swarm up` recorded every reconciled phase as `ok`.

    It read the sentinels into `seed` and then threw the status away, hardcoding
    `"ok"` for anything it integrated — the same bug `supervisor._pump_integrations`
    was fixed for. A phase the worker finished `needs-owner` came back from a
    restart as a clean success, so nothing ever told the owner it was waiting.
    """
    cfg = _cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(gitq, "sentinel_done", lambda c: {"P0": "needs-owner"})
    monkeypatch.setattr(
        gitq, "reconcile", lambda c, d, l, **kw: gitq.ReconcileResult(integrated=["P0"])
    )

    cli._reconcile_orphans(cfg)

    assert state_mod.read(cfg).done["P0"] == "needs-owner"
