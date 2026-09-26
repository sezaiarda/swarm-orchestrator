"""``[telegram].pings = "necessary"``: the owner's phone rings only when they are needed.

Pings can be limited to the necessary ones.
Every rule here has two halves, and both are tested: the
message the owner does not need is logged (``notifications.jsonl``, marked
``suppressed``) instead of sent, and the one they do need still goes out.
``pings = "all"`` restores every ping.

The ``operator-done`` and push-owed rules are tested beside their own code
(``test_opsession.py``, ``test_push_owed.py``); the park in ``test_park.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from swarm_orchestrator import gitq, launch, ovrecord, telegram
from swarm_orchestrator import master as master_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import supervisor as sup_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\nP1 needs:P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_OVERSEER", "1")
    for leak in ("SWARM_TG_PINGS", "SWARM_GIT_ISOLATION", "SWARM_MASTER_KIND",
                 "SWARM_OVERSEER_PASS", "SWARM_MASTER_CMD"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


def sent(cfg) -> str:
    sink = Path(str(cfg.state_dir)).parent / "tg.log"
    return sink.read_text(encoding="utf-8") if sink.exists() else ""


def ledger(cfg) -> list[dict]:
    path = cfg.state_dir / telegram.LEDGER_NAME
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def test_the_default_is_necessary_and_a_bad_value_falls_back_to_it(cfg, monkeypatch):
    assert cfg.telegram_pings == "necessary"
    monkeypatch.setenv("SWARM_TG_PINGS", "loud")
    assert load(project_dir=str(cfg.project_dir)).telegram_pings == "necessary"
    monkeypatch.setenv("SWARM_TG_PINGS", "all")
    assert load(project_dir=str(cfg.project_dir)).telegram_pings == "all"


# -- worker-done: the Overseer retries a first fail -------------------------------
def test_a_first_fail_is_the_overseers_and_a_fail_after_its_retry_pings(cfg):
    first = launch.done(cfg, "P1", "fail", "cargo test red on the parser")
    assert first.ping == "held" and sent(cfg) == ""
    assert "no telegram (logged)" in first.render()
    # A fuller re-run inside the same attempt is still the first failure.
    again = launch.done(cfg, "P1", "fail", "cargo test red on the parser: the lexer panics")
    assert again.ping == "held" and sent(cfg) == ""
    assert [r["suppressed"] for r in ledger(cfg)] and all(not r["delivered"] for r in ledger(cfg))

    (cfg.done_dir / "P1.fail").unlink()  # what `swarm retry` does
    second = launch.done(cfg, "P1", "fail", "still red after the retry")
    assert second.ping == "sent"
    assert sent(cfg) == "swarm: P1 FAILED — still red after the retry\n"


def test_a_fail_pings_at_once_without_an_overseer_or_under_all(cfg):
    cfg.overseer_enabled = False
    assert launch.done(cfg, "P1", "fail", "red").ping == "sent"
    cfg.overseer_enabled = True
    cfg.telegram_pings = "all"
    assert launch.done(cfg, "P2", "fail", "red too").ping == "sent"
    assert sent(cfg).count("FAILED") == 2


# -- a failed start: once per phase ------------------------------------------------
def test_a_failed_start_pings_once_per_phase(cfg):
    assert launch._launch_fail_hold(cfg, "P1") is None
    telegram.notify(cfg.telegram_notify, "swarm: worker P1 failed to start", kind="spawn-fail",
                    phase="P1", state_dir=cfg.state_dir)
    assert launch._launch_fail_hold(cfg, "P1")  # told once already
    assert launch._launch_fail_hold(cfg, "P2") is None  # another phase is news
    cfg.telegram_pings = "all"
    assert launch._launch_fail_hold(cfg, "P1") is None


# -- routine once, a problem as a streak --------------------------------------------
def test_a_master_that_will_not_start_pings_on_the_third_failure_in_a_row(cfg):
    log = Log(cfg.supervisor_log)
    try:
        m = master_mod.Master(cfg, log)
        assert m._timeout_hold() and m._timeout_hold()
        assert m._timeout_hold() is None  # three in a row
        assert m._timeout_hold()  # the count goes on; the next ping is at six
    finally:
        log.close()


# -- merge holds -----------------------------------------------------------------
@pytest.mark.parametrize("pane, pinged", [("%9", False), (None, True)])
def test_a_conflict_pings_only_when_no_resolver_is_on_it(cfg, monkeypatch, pane, pinged):
    monkeypatch.setattr(sup_mod.resolver_mod, "spawn", lambda *a: pane)
    sup = sup_mod.Supervisor(cfg)
    try:
        sup._hold("P1", gitq.CONFLICT, cfg.project_dir, None)
    finally:
        sup.log.close()
    assert ("merge conflict integrating P1" in sent(cfg)) is pinged
    [row] = ledger(cfg)
    assert row["kind"] == "integrate-hold" and bool(row.get("suppressed")) is not pinged


def test_a_dirty_tree_always_pings(cfg):
    sup = sup_mod.Supervisor(cfg)
    try:
        sup._hold("P1", gitq.DIRTY, cfg.project_dir, None)
    finally:
        sup.log.close()
    assert "uncommitted changes" in sent(cfg)


# -- the Overseer's summary ---------------------------------------------------------
def _pass(cfg, monkeypatch, key: str) -> None:
    pid = "20260926T100000Z"
    ovrecord.create(cfg, pid, [{"key": key, "text": key}], None)
    with state_mod.transaction(cfg) as st:
        st.overseer_pass = pid
    monkeypatch.setenv("SWARM_OVERSEER_PASS", pid)
    monkeypatch.setenv("SWARM_MASTER_KIND", "overseer")


def _notify(cfg, *args: str) -> int:
    return cli_main(["--project-dir", str(cfg.project_dir), "notify", *args])


@pytest.mark.parametrize("key", ["finished", "manual"])
def test_a_cadence_or_requested_pass_sends_its_summary_as_written(cfg, monkeypatch, key):
    _pass(cfg, monkeypatch, key)
    assert _notify(cfg, "10 done, all green") == 0
    assert sent(cfg) == "10 done, all green\n"  # usage only when the owner asks


@pytest.mark.parametrize("key", ["every", "starve", "hold:P1", "doctor:x", "owner:P1"])
def test_any_other_pass_records_its_summary_and_sends_nothing(cfg, monkeypatch, capsys, key):
    _pass(cfg, monkeypatch, key)
    assert _notify(cfg, "nothing new") == 0
    assert sent(cfg) == ""
    [row] = ledger(cfg)
    assert row["kind"] == "overseer-digest" and row["suppressed"] and row["text"] == "nothing new"
    assert "recorded, not sent" in capsys.readouterr().out


def test_attention_sends_the_summary_from_any_pass(cfg, monkeypatch):
    _pass(cfg, monkeypatch, "every")
    assert _notify(cfg, "P3 failed twice; needs you", "--attention") == 0
    assert "P3 failed twice; needs you" in sent(cfg)


def test_pings_all_sends_every_summary(cfg, monkeypatch):
    monkeypatch.setenv("SWARM_TG_PINGS", "all")
    _pass(cfg, monkeypatch, "every")
    assert _notify(cfg, "hourly") == 0
    assert "hourly" in sent(cfg)


def test_a_note_from_any_other_session_always_sends(cfg, monkeypatch):
    """The resolver's "I cannot fix this conflict" is `swarm notify` too."""
    monkeypatch.setenv("SWARM_MASTER_KIND", "resolver")
    assert _notify(cfg, "cannot resolve P1 in frontend") == 0
    assert sent(cfg) == "cannot resolve P1 in frontend\n"


def test_the_digest_tells_the_overseer_whether_its_summary_goes_out(cfg):
    from swarm_orchestrator import ovdigest
    from swarm_orchestrator.overseer import Reason

    st = state_mod.read(cfg)
    quiet = ovdigest.render(ovdigest.build(cfg, st, [Reason("every", "no pass for 3h")], since=0.0))
    loud = ovdigest.render(ovdigest.build(cfg, st, [Reason("finished", "10 finished")], since=0.0))
    assert "summary is recorded, not sent" in quiet and "`--attention`" in quiet
    assert "goes to the owner's phone" in loud
