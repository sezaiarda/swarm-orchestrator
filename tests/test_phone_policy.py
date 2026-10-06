"""What reaches the owner's phone: an ask, the Overseer's summary, and nothing else.

The owner reads one or two sentences in a notification. So the swarm sends two
kinds of message (``telegram.ask``, ``telegram.summary``) and holds everything
else back in ``notifications.jsonl``, folded for the next summary or logged
only. Every rule here has two halves, and both are tested: what the owner does
not need is recorded and not sent, and what stops on them still asks.

The sender itself is tested in ``test_notify.py``; the operator's outcome,
owed pushes, parks and usage caps beside their own code (``test_opsession.py``,
``test_push_owed.py``, ``test_park.py``, ``test_caps.py``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from swarm_orchestrator import gitq, launch, ovdigest, ovrecord, telegram
from swarm_orchestrator import master as master_mod
from swarm_orchestrator import overseer as ov
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import supervisor as sup_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / ".swarm.toml").touch()
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\nP1 needs:P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_OVERSEER", "1")
    monkeypatch.setenv("SWARM_WATCHDOG", "0")
    for leak in ("SWARM_GIT_ISOLATION", "SWARM_MASTER_KIND", "SWARM_OVERSEER_PASS",
                 "SWARM_MASTER_CMD", "SWARM_NAME"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


@pytest.fixture
def sup(cfg, monkeypatch):
    s = sup_mod.Supervisor(cfg)
    s._bootstrapped = True
    monkeypatch.setattr(s.master, "is_alive", lambda: False)
    monkeypatch.setattr(s.master, "kill", lambda: None)
    monkeypatch.setattr(s, "_start_overseer_spawn", lambda *a: None)
    s._doctor_probed = s._box_probed = time.time()
    yield s
    s.log.close()


def sent(cfg) -> list[str]:
    sink = Path(str(cfg.state_dir)).parent / "tg.log"
    return sink.read_text(encoding="utf-8").splitlines() if sink.exists() else []


def ledger(cfg) -> list[dict]:
    path = cfg.state_dir / telegram.LEDGER_NAME
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def test_the_knobs_that_tuned_the_volume_are_gone(cfg):
    for gone in ("telegram_pings", "telegram_push_owed_grace_s", "operator_notify",
                 "overseer_every_finished"):
        assert not hasattr(cfg, gone)
    assert cfg.overseer_every_s == 14400


# -- a failed phase: the Overseer retries a first fail --------------------------
def test_a_first_fail_is_the_overseers_and_a_fail_after_its_retry_asks(cfg):
    first = launch.done(cfg, "P1", "fail", "cargo test red on the parser")
    assert first.ping == "held" and sent(cfg) == []
    assert "no telegram (logged)" in first.render()
    # A fuller re-run inside the same attempt is still the first failure.
    again = launch.done(cfg, "P1", "fail", "cargo test red on the parser: the lexer panics")
    assert again.ping == "held" and sent(cfg) == []
    assert [r["class"] for r in ledger(cfg)] == ["folded", "folded"]
    assert "P1 FAILED — cargo test red on the parser" in ledger(cfg)[0]["text"]

    (cfg.done_dir / "P1.fail").unlink()  # what `swarm retry` does
    second = launch.done(cfg, "P1", "fail", "still red after the retry")
    assert second.ping == "sent"
    assert sent(cfg) == [
        "[project] Asks you: Fix what stopped P1, then run `swarm retry P1`: it failed again"
        " after a retry. `swarm report` has its recap."]


def test_a_fail_asks_at_once_without_an_overseer(cfg):
    cfg.overseer_enabled = False
    assert launch.done(cfg, "P1", "fail", "red").ping == "sent"
    [line] = sent(cfg)
    assert line.startswith("[project] Asks you: Fix what stopped P1, then run `swarm retry P1`")
    assert "with the Overseer off nothing retries it" in line


def test_the_fail_ask_names_the_row_by_its_title_and_always_fits(cfg):
    cfg.overseer_enabled = False
    title = "Rework the checkout flow so a card is charged exactly once " * 3
    (cfg.project_dir / cfg.ledger).write_text(
        f"- [ ] `P0` · **{title.strip()}** and the detail of the row\n", encoding="utf-8")
    assert launch.row_title(cfg, "P0").startswith("Rework the checkout flow")
    launch.done(cfg, "P0", "fail", "red")
    [line] = sent(cfg)
    assert len(line) == telegram.PHONE_MAX
    assert line.startswith("[project] Asks you: Fix what stopped P0 (Rework the checkout flow")
    assert line.endswith("…), then run `swarm retry P0`: it failed; with the Overseer off nothing"
                         " retries it. `swarm report` has its recap.")


# -- a failed start, a session that would not come up: the log only ----------------
def test_a_failed_start_is_logged_and_asks_nobody(cfg, monkeypatch):
    monkeypatch.setattr(launch, "_launch_bare", lambda *a: False)
    log = Log(cfg.supervisor_log)
    try:
        assert launch.launch_outcome(cfg, "P0", log, quiet=True) == launch.FAILED
    finally:
        log.close()
    assert sent(cfg) == []
    [row] = ledger(cfg)
    assert (row["class"], row["kind"], row["phase"]) == ("logged", "spawn-fail", "P0")
    assert row["suppressed"] == "the swarm tries again; an ask follows if it gives up"


def test_a_launch_given_up_asks(sup, cfg):
    sup._ping_gave_up("P0", 3)
    assert sent(cfg) == [
        "[project] Asks you: Fix why P0 will not start, then run `swarm launch P0`: its worker"
        " failed to start 3 times in a row, so the swarm stopped trying and the phases after"
        " it wait."]


def test_a_session_that_will_not_come_up_is_logged(cfg):
    log = Log(cfg.supervisor_log)
    try:
        master_mod.Master(cfg, log)._log_failed("the Overseer session would not start.")
    finally:
        log.close()
    assert sent(cfg) == []
    assert [(r["class"], r["kind"]) for r in ledger(cfg)] == [("logged", "master-timeout")]


def test_three_bad_overseer_passes_in_a_row_ask_and_the_single_ones_do_not(sup, cfg):
    sup._overseer_bad_pass("a pass ran long", "t")
    sup._overseer_bad_pass("a pass would not start", "t")
    assert sent(cfg) == [] and [r["class"] for r in ledger(cfg)] == ["logged", "logged"]
    sup._overseer_bad_pass("a pass ran long", "t")
    assert sent(cfg) == [
        "[project] Asks you: Check the overseer window: the Overseer has failed 3 times in a"
        " row (it would not start, or ran past its time limit), so nothing is reviewing the"
        " run. Workers carry on."]
    sup._overseer_bad_pass("again", "t")
    assert len(sent(cfg)) == 1  # the next ask is at six


# -- merge holds -----------------------------------------------------------------
@pytest.mark.parametrize("pane, asked", [("%9", False), (None, True)])
def test_a_conflict_asks_only_when_no_resolver_is_on_it(sup, cfg, monkeypatch, pane, asked):
    monkeypatch.setattr(sup_mod.resolver_mod, "spawn", lambda *a: pane)
    sup._hold("P1", gitq.CONFLICT, cfg.project_dir, None)
    [row] = ledger(cfg)
    assert row["kind"] == "integrate-hold"
    if asked:
        assert sent(cfg) == [
            "[project] Asks you: Fix the merge clash in project, then run `swarm resolved P1`:"
            " P1's work clashes with work already merged, the resolver would not start, and"
            " all merging waits on it."]
    else:
        assert sent(cfg) == []
        assert (row["class"], row["suppressed"]) == ("logged", sup_mod.RESOLVER_ON_IT)
        assert "A resolver is fixing it" in row["text"]


def test_a_dirty_tree_asks_for_what_to_do_first(sup, cfg):
    sup._hold("P1", gitq.DIRTY, cfg.project_dir, None)
    assert sent(cfg) == [
        "[project] Asks you: Commit, stash or move the uncommitted changes in project, then"
        " run `swarm resolved P1`: P1 is finished but cannot be merged over them, and all"
        " merging waits on it."]


def test_a_git_error_asks_with_gits_own_words_cut_to_fit(sup, cfg):
    sup._hold("P1", gitq.DIRTY, None, "fatal: " + "refusing to merge unrelated histories " * 20)
    [line] = sent(cfg)
    assert len(line) == telegram.PHONE_MAX
    assert line.startswith("[project] Asks you: Look at the merge of P1, then run `swarm resolved"
                           " P1`: git stopped it (fatal: refusing")
    assert line.endswith("…), and all merging waits on it.")


def test_a_resolved_that_came_too_early_is_for_the_summary(sup, cfg, monkeypatch):
    with state_mod.transaction(cfg) as st:
        st.integ_blocked, st.integ_blocked_kind = "P1", gitq.CONFLICT
        st.integ_blocked_repo = str(cfg.project_dir)
    monkeypatch.setattr(gitq, "resolve_ready", lambda *_a: False)
    monkeypatch.setattr(gitq, "unfinished", lambda _r: "a merge in progress")
    sup._on_resolved("P1")
    assert sent(cfg) == []
    [row] = ledger(cfg)
    assert row["class"] == "folded" and "still cannot be merged" in row["text"]


# -- the supervisor's own trouble ---------------------------------------------------
def test_a_handler_error_is_folded_and_the_overseer_gets_a_pass(sup, cfg):
    def boom():
        raise RuntimeError("no such pane")

    assert sup._dispatch("done P1 ok", boom) is False
    assert sent(cfg) == []
    [row] = ledger(cfg)
    assert row["class"] == "folded" and "no such pane" in row["text"]
    assert [r.key for r in sup.overseer.pending] == ["error:done"]


def test_a_worker_that_died_is_for_the_summary_and_a_crash_loop_asks(sup, cfg):
    sup._fold("reap:P1", "the worker for P1 stopped without finishing.", phase="P1")
    assert sent(cfg) == [] and ledger(cfg)[0]["class"] == "folded"
    sup._ask("crash-hold:P1", "Look at P1, then run `swarm launch P1`.", cooldown=0.0, phase="P1")
    assert sent(cfg) == ["[project] Asks you: Look at P1, then run `swarm launch P1`."]


def test_one_condition_asks_once_per_cooldown(sup, cfg):
    sup.watchdog_s = 300
    for _ in range(3):
        sup._ask("finished-with-ready", "Run `swarm up` to carry on.")
    assert len(sent(cfg)) == 1


# -- the Overseer's summary: on its own pass, short, once ---------------------------
def _pass(cfg, monkeypatch, key: str) -> str:
    pid = "20260926T100000Z"
    ovrecord.create(cfg, pid, [{"key": key, "text": key}], None)
    with state_mod.transaction(cfg) as st:
        st.overseer_pass = pid
    monkeypatch.setenv("SWARM_OVERSEER_PASS", pid)
    monkeypatch.setenv("SWARM_MASTER_KIND", "overseer")
    return pid


def _swarm(cfg, *args: str) -> int:
    return cli_main(["--project-dir", str(cfg.project_dir), *args])


SUMMARY = "Since noon the checkout flow landed and six phases are building. Nothing waits on you."


def test_the_summary_pass_sends_the_summary_as_written(cfg, monkeypatch):
    pid = _pass(cfg, monkeypatch, "summary")
    assert _swarm(cfg, "overseer-summary", SUMMARY) == 0
    assert sent(cfg) == [f"[project] Overseer: {SUMMARY}"]
    [row] = ledger(cfg)
    assert (row["class"], row["kind"], row["delivered"]) == ("summary", "summary", True)
    assert ovrecord.load_json(cfg, pid).owner_summary == SUMMARY


def test_a_pass_sends_its_summary_once(cfg, monkeypatch, capsys):
    _pass(cfg, monkeypatch, "summary")
    assert _swarm(cfg, "overseer-summary", SUMMARY) == 0
    assert _swarm(cfg, "overseer-summary", "And another thing.") == 1
    assert "already sent its summary" in capsys.readouterr().err
    assert len(sent(cfg)) == 1


@pytest.mark.parametrize("key", ["manual", "starve", "hold:P1", "doctor:x", "owner:P1", "box",
                                 "fail:P1"])
def test_any_other_pass_records_a_summary_and_sends_nothing(cfg, monkeypatch, capsys, key):
    """On the time cadence only: never for a failure, a hold, or a finished phase."""
    pid = _pass(cfg, monkeypatch, key)
    assert _swarm(cfg, "overseer-summary", "nothing new") == 0
    assert sent(cfg) == []
    [row] = ledger(cfg)
    assert (row["class"], row["kind"], row["text"]) == ("logged", "summary", "nothing new")
    out = capsys.readouterr().out
    assert "recorded, not sent" in out and "swarm notify" in out
    assert ovrecord.load_json(cfg, pid).owner_summary == ""


def test_a_summary_too_long_is_refused_with_the_limit_and_nothing_goes_out(
        cfg, monkeypatch, capsys):
    pid = _pass(cfg, monkeypatch, "summary")
    assert _swarm(cfg, "overseer-summary", "P1 landed. " * 40) == 2
    err = capsys.readouterr().err
    limit = telegram.room(cfg, telegram.SUMMARY_LEAD)
    assert "nothing was sent" in err and f"at most {limit} fit" in err
    assert "Rewrite it, do not cut it" in err
    assert sent(cfg) == [] and ledger(cfg) == []
    assert ovrecord.load_json(cfg, pid).owner_summary == ""
    assert _swarm(cfg, "overseer-summary", SUMMARY) == 0  # the rewrite goes out
    assert len(sent(cfg)) == 1


def test_a_summary_outside_a_pass_is_refused(cfg, capsys):
    assert _swarm(cfg, "overseer-summary", SUMMARY) == 1
    assert "no Overseer pass is running" in capsys.readouterr().err
    assert sent(cfg) == []


def test_the_overseer_raises_what_needs_the_owner_as_an_ask(cfg, monkeypatch):
    """Not a free-form message: `swarm notify` from a pass is an ask like any other."""
    _pass(cfg, monkeypatch, "fail:P3")
    ask = "Renew the staging certificate: the deploy fails on it and three rows wait."
    assert _swarm(cfg, "notify", ask) == 0
    assert sent(cfg) == [f"[project] Asks you: {ask}"]
    assert (ledger(cfg)[0]["class"], ledger(cfg)[0]["kind"]) == ("ask", "session-ask")


def test_notify_takes_no_attention_flag_any_more(cfg, monkeypatch):
    _pass(cfg, monkeypatch, "starve")
    with pytest.raises(SystemExit):
        _swarm(cfg, "notify", "P3 failed twice; needs you", "--attention")
    assert sent(cfg) == []


def test_the_digest_tells_the_pass_whether_the_summary_is_its_to_write(cfg):
    st = state_mod.read(cfg)
    other = ovdigest.render(ovdigest.build(
        cfg, st, [ov.Reason("starve", "free slots for 10m")], since=0.0))
    due = ovdigest.render(ovdigest.build(
        cfg, st, [ov.Reason("summary", "the owner's summary is due")], since=0.0))
    limit = telegram.room(cfg, telegram.SUMMARY_LEAD)
    assert "this is the summary pass" in due
    assert f'`swarm overseer-summary "<text>"` (two short sentences, at most {limit} characters)' in due
    assert "no summary is due on this pass" in other and "`swarm notify`" in other


def test_the_digest_lists_what_was_held_back_since_the_last_summary(cfg):
    telegram.fold(cfg, "an old pause", kind="usage-cap")
    telegram.summary(cfg, "All quiet. Nothing waits on you.")
    telegram.fold(cfg, "P7 FAILED — the lexer panics", kind="worker-done", phase="P7")
    telegram.log(cfg, "a park", kind="park", phase="P8")
    data = ovdigest.build(cfg, state_mod.read(cfg), [ov.Reason("summary", "due")], since=0.0)
    assert data["held_back"]["count"] == 1
    assert [h["text"] for h in data["held_back"]["rows"]] == ["P7 FAILED — the lexer panics"]
    text = ovdigest.render(data)
    assert "## Held back since the last summary (1)" in text
    assert "- [worker-done P7] P7 FAILED — the lexer panics" in text
    assert "an old pause" not in text and "a park" not in text


# -- the summary goes out on the clock whatever becomes of the session ---------------
def _summaries(cfg) -> list[dict]:
    return [r for r in ledger(cfg) if r["class"] == "summary"]


def test_a_summary_pass_that_ends_without_one_gets_the_swarms_own(sup, cfg, monkeypatch):
    with state_mod.transaction(cfg) as st:
        st.mark_done("P0", "ok")
    (cfg.done_dir / "P0.ok").write_text("P0 ok built\n")
    sup.overseer.mem.anchor = time.time() - cfg.overseer_every_s - 1
    sup._overseer_tick()
    pid = sup._overseer_live
    assert pid and [r.key for r in sup._overseer_reasons] == ["summary"]
    assert sent(cfg) == []  # the pass has the summary to write
    sup._on_overseer_spawned(pid, "ok")
    sup._end_overseer_pass(pid, ovrecord.DONE)
    [row] = _summaries(cfg)
    assert row["text"].startswith("[project] Overseer: Since ")
    assert row["text"].endswith(": 1 phase landed; 0 building now. Nothing waits on you.")
    assert ovrecord.load_json(cfg, pid).owner_summary
    assert "SUMMARY-OWN supervisor._summary_owed" in cfg.supervisor_log.read_text()


def _building(cfg, phase: str = "P0") -> None:
    """Something to report: a worker is on ``phase``."""
    with state_mod.transaction(cfg) as st:
        st.claim_slot(phase)


def test_a_summary_pass_that_wrote_its_own_gets_no_second_one(sup, cfg, monkeypatch):
    _building(cfg)
    sup.overseer.mem.anchor = time.time() - cfg.overseer_every_s - 1
    sup._overseer_tick()
    pid = sup._overseer_live
    sup._on_overseer_spawned(pid, "ok")
    monkeypatch.setenv("SWARM_OVERSEER_PASS", pid)
    assert _swarm(cfg, "overseer-summary", SUMMARY) == 0
    sup._end_overseer_pass(pid, ovrecord.DONE)
    assert [r["text"] for r in _summaries(cfg)] == [f"[project] Overseer: {SUMMARY}"]


def test_a_summary_pass_that_never_started_still_sends_on_time(sup, cfg):
    _building(cfg)
    sup.overseer.mem.anchor = time.time() - cfg.overseer_every_s - 1
    sup._overseer_tick()
    pid = sup._overseer_live
    sup._on_overseer_spawned(pid, "failed")
    assert len(_summaries(cfg)) == 1
    assert [r.key for r in sup.overseer.pending] == []  # not owed a second time
    sup._overseer_tick()
    assert len(_summaries(cfg)) == 1


def test_a_pass_for_anything_else_ends_without_a_summary(sup, cfg):
    sup.overseer.request(ov.MANUAL, "by hand", urgent=True)
    sup._overseer_tick()
    pid = sup._overseer_live
    sup._on_overseer_spawned(pid, "ok")
    sup._end_overseer_pass(pid, ovrecord.DONE)
    assert _summaries(cfg) == [] and sent(cfg) == []


def test_no_number_of_finished_phases_sends_a_summary(sup, cfg):
    sup._overseer_tick()  # the baseline
    with state_mod.transaction(cfg) as st:
        for i in range(12):
            st.mark_done(f"X{i}", "ok")
    sup._overseer_tick()
    assert sup._overseer_live is None and sup.overseer.pending == []
    assert ledger(cfg) == []


def test_with_the_overseer_off_the_swarm_writes_the_summary_on_the_clock(sup, cfg):
    cfg.overseer_enabled = False
    sup._overseer_tick()
    assert ledger(cfg) == []  # the clock has only just started
    with state_mod.transaction(cfg) as st:
        st.mark_done("P0", "fail")
        st.waiting["P1"] = time.time() + 60
    (cfg.done_dir / "P0.fail").write_text("P0 fail red\n")
    sup.overseer.mem.anchor = time.time() - cfg.overseer_every_s - 1
    sup._overseer_tick()
    [row] = _summaries(cfg)
    assert row["text"].endswith(
        ": 0 phases landed, 1 failed; 0 building now. Waiting on you: 1 question.")
    sup._overseer_tick()
    assert len(_summaries(cfg)) == 1  # the clock started again
    assert sup.overseer.next_deadline() == pytest.approx(
        sup.overseer.mem.last_summary_at + cfg.overseer_every_s)


@pytest.mark.parametrize("overseer", [True, False])
def test_a_swarm_that_stood_still_sends_no_summary_and_the_clock_starts_again(
        sup, cfg, overseer):
    """Paused, idle or waiting on an answer for four hours: a summary would only
    repeat the last one, and what waits on the owner was asked when it began to."""
    cfg.overseer_enabled = overseer
    with state_mod.transaction(cfg) as st:
        st.paused = True
        st.waiting["P1"] = time.time() + 60
    sup.overseer.mem.anchor = time.time() - cfg.overseer_every_s - 1
    sup._overseer_tick()
    assert ledger(cfg) == [] and sup._overseer_live is None and sup.overseer.pending == []
    assert "SUMMARY-SKIPPED" in cfg.supervisor_log.read_text()
    assert not sup.overseer.summary_due(time.time())  # four hours until the next look
    # The next tick finds something held back since: there is a summary again.
    telegram.fold(cfg, "the worker for P0 stopped without finishing", phase="P0")
    sup.overseer.mem.last_summary_at = time.time() - cfg.overseer_every_s - 1
    sup._overseer_tick()
    if overseer:
        assert [r.key for r in sup._overseer_reasons] == ["summary"]
    else:
        assert len(_summaries(cfg)) == 1


def test_what_counts_as_something_to_report(cfg):
    st = state_mod.read(cfg)
    assert ovdigest.nothing_to_report(cfg, st, 0.0)
    telegram.log(cfg, "a park", kind="park")
    telegram.ask(cfg, "Answer P1.", kind="waiting", phase="P1")
    assert ovdigest.nothing_to_report(cfg, state_mod.read(cfg), 0.0)  # neither is news
    telegram.fold(cfg, "a push is owed", kind="push-owed")
    assert not ovdigest.nothing_to_report(cfg, state_mod.read(cfg), 0.0)
    assert ovdigest.nothing_to_report(cfg, state_mod.read(cfg), time.time() + 1)
    _building(cfg)
    assert not ovdigest.nothing_to_report(cfg, state_mod.read(cfg), time.time() + 1)


def test_the_swarms_own_summary_says_what_waits_and_why_nothing_starts(cfg):
    with state_mod.transaction(cfg) as st:
        st.integ_blocked, st.integ_blocked_kind = "P1", gitq.DIRTY
        st.paused = True
    text = ovdigest.own_summary(cfg, state_mod.read(cfg), 0.0)
    assert text == ("So far: 0 phases landed; 0 building now, and the swarm is paused."
                    " Waiting on you: a held merge (P1).")
    assert len(text) <= telegram.room(cfg, telegram.SUMMARY_LEAD)


# -- the end of the run: one last summary --------------------------------------------
def test_the_finish_is_a_summary_of_what_landed_and_what_is_left(sup, cfg):
    sup._finish(7, leftover=["P8"], skipped=1, failed=["P3", "P4", "P5"], operator=["op-1"])
    [line] = sent(cfg)
    assert line == (
        "[project] Overseer: The run has finished: 7 phase(s) landed, 1 skipped, 3 failed"
        " (P3, P4 and 1 more). Left for you: `swarm retry` the failed; `swarm launch` the 1"
        " that never started; `swarm operator` the 1 follow-up job(s) not done.")
    assert len(line) <= telegram.PHONE_MAX
    assert (ledger(cfg)[0]["class"], ledger(cfg)[0]["kind"]) == ("summary", "finish")
    sup._finish(7)  # announced exactly once
    assert len(sent(cfg)) == 1


def test_a_clean_finish_says_nothing_waits(sup, cfg):
    sup._finish(5)
    assert sent(cfg) == [
        "[project] Overseer: The run has finished: 5 phase(s) landed. Nothing waits on you."]


# -- the swarm's own wording fits, whole, with a long name and long ids ---------------
LONG = "frontend-checkout-F26"  # a phase id as long as real ledgers have them


def test_the_swarms_own_asks_arrive_whole_with_a_long_swarm_name_and_long_ids(
        sup, cfg, monkeypatch):
    """Built to fit: what to do and why are never the part that is cut off. Only
    a fragment the swarm quotes (a title, an error, a list) is ever shortened."""
    from swarm_orchestrator import blockedping, caps, opqueue, owner, restart

    monkeypatch.setattr(cfg, "name", "glasheim-production")
    monkeypatch.setattr(sup_mod.resolver_mod, "spawn", lambda *a, **k: None)
    monkeypatch.setattr(sup_mod.landing_mod, "resolver_brief", lambda *a: None)
    cfg.overseer_enabled = False
    (cfg.project_dir / cfg.ledger).write_text(
        f"{LONG}\nP1 needs:{LONG}\nP2 needs:P1\nOWN-1 needs:P9\nOWN-2 needs:P9\n", encoding="utf-8")
    now = time.time()

    sup._hold(LONG, gitq.CONFLICT, cfg.project_dir, None)
    sup._hold(LONG, gitq.DIRTY, cfg.project_dir, None)
    sup._hold(LONG, gitq.PUSH_FAILED, cfg.project_dir, None)
    sup._hold(LONG, gitq.LANE_CONFLICT, cfg.project_dir, None)
    sup._hold(LONG, gitq.LANE_RED, cfg.project_dir, None)
    sup._ping_gave_up(LONG, 3)
    sup._overseer_bad = 2
    sup._overseer_bad_pass("ran long", "t")
    telegram.ask(cfg, caps.down_ask(
        {"window": "five_hour", "pct": 100, "at": 90, "resets_at": now + 4 * 3600}, now, True))
    launch.done(cfg, LONG, "fail", "red")
    launch.done(cfg, "P2", "operator", "deploy the checkout page to staging and check /health")
    cfg.operator_enabled = True
    opqueue.add(cfg, LONG, status="operator", note="deploy it and check it")
    opqueue.abandon(cfg, LONG, "three crashes")
    log = Log(cfg.supervisor_log)
    try:
        blockedping.gather(cfg, LONG, "the box refuses the key")
        blockedping.flush(cfg, log, blockedping.deadline(cfg))
        restart.fail(cfg, {"id": "r1", "by": "the Overseer"}, "no answer", log, left=restart.DOWN)
        restart.fail(cfg, {"id": "r2", "by": "the Overseer"}, "no answer", log,
                     left=restart.UNSUPERVISED)
    finally:
        log.close()
    monkeypatch.setattr(owner, "current_owner_rows", lambda *_a: [("OWN-1", 12), ("OWN-2", 3)])
    owner.ping_owner_rows(cfg, state_mod.read(cfg))
    sup._finish(120, leftover=[LONG, "P1", "P2"], skipped=14, failed=[LONG, "P8", "P9"],
                operator=[LONG, "op-2"])

    lines = sent(cfg)
    assert len(lines) == 16, lines
    for line in lines:
        assert line.startswith(("[glasheim-production] Asks you: ",
                                "[glasheim-production] Overseer: ")), line
        assert line.count("[glasheim-production]") == 1
        assert len(line) <= telegram.PHONE_MAX, (len(line), line)
        assert not line.endswith("…"), line  # the reason at the end arrived whole
    assert all(r["class"] in ("ask", "summary") for r in ledger(cfg) if r["delivered"])
