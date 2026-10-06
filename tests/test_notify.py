"""Tests for the one sender: how a message is sent, held back and logged, and
what a finishing worker says.

Two failure classes cost the most here, and they pull in opposite directions:

* **the silent drop** — ``notify.sh`` failed on every send and every caller threw
  the exit code away, so a run that never reached the owner looked identical to
  one that did. Hence the log: every message, delivered or not, is one JSON
  line with its error, and a send never raises into the caller.
* **the firehose** — a message that goes out when it should not trains the owner
  to ignore the channel. Only an ask and the Overseer's summary reach the phone,
  each named for its swarm and short enough for a notification; everything
  else is held back in the log. A session's own words are refused when too
  long, never cut.

Everything runs against the ``SWARM_TG_SINK`` file or a throwaway ``notify.sh``;
nothing touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import machine_toml

from swarm_orchestrator import launch, owner, statuses, telegram
from swarm_orchestrator.config import load


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "state"
    monkeypatch.setenv("SWARM_STATE_DIR", str(path))
    return path


@pytest.fixture
def sink(tmp_path: Path, state_dir: Path, monkeypatch) -> Path:
    path = tmp_path / "tg.log"
    monkeypatch.setenv("SWARM_TG_SINK", str(path))
    return path


@pytest.fixture
def cfg(tmp_path: Path, sink: Path, monkeypatch):
    """A bare-driver Config whose telegrams land in the sink."""
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\nP1 needs:P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_SLUG", "notifytest")
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    for leak in ("SWARM_MASTER_CMD", "SWARM_WORKER_CMD", "SWARM_SESSION", "SWARM_LAYOUT",
                 "SWARM_GIT_ISOLATION"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    return c


def sent(sink: Path) -> list[str]:
    return sink.read_text(encoding="utf-8").splitlines() if sink.is_file() else []


def ledger(state_dir: Path) -> list[dict]:
    path = state_dir / telegram.LEDGER_NAME
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def script(tmp_path: Path, body: str) -> Path:
    """An executable stand-in for ``notify.sh``."""
    path = tmp_path / "bin" / "notify.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


# -- the send ---------------------------------------------------------------
# -- the two kinds ------------------------------------------------------------
def test_an_ask_names_the_swarm_once_and_leads_with_asks_you(cfg, sink):
    result = telegram.ask(cfg, "Approve the price page: the launch waits on it.",
                          kind="waiting", phase="P3", source="cli.waiting")
    assert result.delivered is True
    assert sent(sink) == ["[project] Asks you: Approve the price page: the launch waits on it."]
    assert sent(sink)[0].count("[project]") == 1
    [row] = ledger(cfg.state_dir)
    assert (row["class"], row["kind"], row["phase"], row["source"]) == (
        "ask", "waiting", "P3", "cli.waiting")
    assert row["delivered"] is True and row["error"] is None
    assert row["text"] == sent(sink)[0]
    assert row["ask"] == "Approve the price page: the launch waits on it."
    assert isinstance(row["ts"], float)


def test_the_prefix_is_the_swarms_own_name(cfg, sink, monkeypatch):
    monkeypatch.setattr(cfg, "name", "glasheim")
    telegram.ask(cfg, "Run `swarm up`.")
    telegram.summary(cfg, "Two phases landed. Nothing waits on you.")
    assert sent(sink) == [
        "[glasheim] Asks you: Run `swarm up`.",
        "[glasheim] Overseer: Two phases landed. Nothing waits on you.",
    ]
    assert [r["class"] for r in ledger(cfg.state_dir)] == ["ask", "summary"]


def test_a_summary_leads_with_overseer_and_is_filed_as_one(cfg, sink):
    telegram.summary(cfg, "Since 10:00 three phases landed; two are building. Nothing waits on you.")
    assert sent(sink) == [
        "[project] Overseer: Since 10:00 three phases landed; two are building."
        " Nothing waits on you."]
    [row] = ledger(cfg.state_dir)
    assert (row["class"], row["kind"], row["delivered"]) == ("summary", "summary", True)


@pytest.mark.parametrize("send", [telegram.ask, telegram.summary])
def test_no_message_the_swarm_starts_is_longer_than_a_notification(cfg, sink, send):
    """The swarm's own wording is built to fit; one that still runs over is cut
    rather than lost, and never goes out long."""
    send(cfg, "word " * 200)
    [line] = sent(sink)
    assert len(line) == telegram.PHONE_MAX == 280
    assert line.endswith("…")


def test_the_room_is_what_the_prefix_leaves_of_280(cfg, monkeypatch):
    assert telegram.room(cfg) == 280 - len("[project] Asks you: ")
    assert telegram.room(cfg, telegram.SUMMARY_LEAD) == 280 - len("[project] Overseer: ")
    monkeypatch.setattr(cfg, "name", "a-much-longer-swarm-name")
    assert telegram.room(cfg) == 280 - len("[a-much-longer-swarm-name] Asks you: ")


def test_a_sessions_ask_that_fits_comes_back_on_one_line(cfg):
    assert telegram.short(cfg, "  which\n schema?  ") == "which schema?"
    assert telegram.short(cfg, "x" * telegram.room(cfg)) == "x" * telegram.room(cfg)


def test_a_sessions_ask_that_is_too_long_is_refused_with_the_limit(cfg):
    """Never cut: half a recap explains nothing, and the caller can rewrite it."""
    limit = telegram.room(cfg)
    with pytest.raises(telegram.TooLong) as err:
        telegram.short(cfg, "x" * (limit + 1))
    said = str(err.value)
    assert f"is {limit + 1} characters and at most {limit} fit" in said
    assert "Rewrite it, do not cut it" in said and "what you need from them, then why" in said


def test_a_summary_that_is_too_long_is_refused_with_its_own_limit(cfg):
    limit = telegram.room(cfg, telegram.SUMMARY_LEAD)
    with pytest.raises(telegram.TooLong) as err:
        telegram.short(cfg, "y" * (limit + 5), telegram.SUMMARY_LEAD)
    assert f"the summary is {limit + 5} characters and at most {limit} fit" in str(err.value)
    assert "what landed and what is running" in str(err.value)


@pytest.mark.parametrize("empty", ["", "   ", "\n"])
def test_an_empty_ask_is_refused(cfg, empty):
    with pytest.raises(telegram.TooLong, match="the ask is empty"):
        telegram.short(cfg, empty)


def test_fitted_cuts_only_the_fragment_the_swarm_put_there(cfg):
    text = telegram.fitted(cfg, "Fix P1 (", "t" * 500, "), then run `swarm retry P1`.")
    assert len(text) == telegram.room(cfg)
    assert text.startswith("Fix P1 (ttt") and text.endswith("…), then run `swarm retry P1`.")
    assert telegram.fitted(cfg, "Fix P1 (", "short", ").") == "Fix P1 (short)."


def test_names_lists_a_few_and_counts_the_rest():
    assert telegram.names(["a", "b"]) == "a, b"
    assert telegram.names(["a", "b", "c", "d", "e"]) == "a, b, c and 2 more"


# -- held back ----------------------------------------------------------------
def test_a_folded_message_is_logged_never_sent_and_never_a_drop(cfg, sink):
    """Held back on purpose: in the alerts, off the phone, and not read as a failure."""
    from swarm_orchestrator.tui.data import parse_notification

    result = telegram.fold(cfg, "operator job J is done — rolled the api", kind="operator-done",
                           phase="J", source="cli.operator-done")
    assert result.delivered is False and sent(sink) == []
    [row] = ledger(cfg.state_dir)
    assert row["class"] == "folded" and row["delivered"] is False and row["error"] is None
    assert row["suppressed"] == telegram.FOLD_REASON
    assert row["text"] == "operator job J is done — rolled the api"  # no prefix: it went nowhere
    note = parse_notification(json.dumps(row))
    assert note.suppressed == telegram.FOLD_REASON and note.dropped is False
    assert parse_notification(json.dumps({**row, "suppressed": None})).dropped is True
    assert telegram.open_drops(ledger(cfg.state_dir), 0.0) == []


def test_a_logged_message_keeps_why_it_is_no_news(cfg, sink):
    telegram.log(cfg, "the worker on P1 moved to its own window", why="the owner was asked",
                 kind="park", phase="P1")
    telegram.log(cfg, "nothing special")
    assert sent(sink) == []
    first, second = ledger(cfg.state_dir)
    assert (first["class"], first["suppressed"]) == ("logged", "the owner was asked")
    assert second["suppressed"] == telegram.LOG_REASON


def test_only_what_was_folded_since_the_last_summary_is_the_next_ones(cfg, sink):
    telegram.fold(cfg, "old news", kind="push-owed")
    telegram.summary(cfg, "All quiet. Nothing waits on you.")
    cut = telegram.last_summary_at(cfg.state_dir)
    assert cut == ledger(cfg.state_dir)[-1]["ts"]
    telegram.fold(cfg, "P2 FAILED — tests red", kind="worker-done", phase="P2")
    telegram.log(cfg, "a park", kind="park", phase="P3")
    telegram.ask(cfg, "Answer P4.", kind="waiting", phase="P4")
    assert [r["text"] for r in telegram.folded_since(cfg.state_dir, cut)] == ["P2 FAILED — tests red"]
    assert telegram.last_summary_at(cfg.state_dir) == cut  # an ask is not a summary


def test_recorded_finds_what_was_said_about_a_phase_sent_or_not(cfg, sink):
    assert not telegram.recorded(cfg.state_dir, ("lane-unprepared",), "P1")
    telegram.fold(cfg, "P1 cannot land yet", kind="lane-unprepared", phase="P1")
    assert telegram.recorded(cfg.state_dir, ("lane-unprepared",), "P1")
    assert not telegram.recorded(cfg.state_dir, ("lane-unprepared",), "P2")
    assert not telegram.recorded(cfg.state_dir, ("spawn-fail",), "P1")


# -- the wire -------------------------------------------------------------------
def test_an_unwritable_sink_fails_loudly_in_the_log(cfg, tmp_path, monkeypatch):
    blocked = tmp_path / "sink-is-a-dir"
    blocked.mkdir()
    monkeypatch.setenv("SWARM_TG_SINK", str(blocked))
    result = telegram.ask(cfg, "hello", kind="waiting", phase="P1")
    assert result.delivered is False and result.error
    [row] = ledger(cfg.state_dir)
    assert row["delivered"] is False and row["error"] == result.error
    assert telegram.open_drops(ledger(cfg.state_dir), 0.0) == [row]


def test_script_success(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    out = tmp_path / "got.txt"
    machine_toml(telegram={"notify": str(script(
        tmp_path, f'printf "%s|%s" "$1" "$SWARM_TG_ENV" > {out}\n'))})
    monkeypatch.setenv("SWARM_TG_ENV", "/a/shells/own.env")  # not the machine's bot
    assert telegram.ask(cfg, "ping").delivered is True
    # The script is told the machine's env file: beside it, by default.
    assert out.read_text() == f"[project] Asks you: ping|{tmp_path / '.env'}"
    assert ledger(cfg.state_dir)[0]["delivered"] is True


def test_script_failure_keeps_the_api_error(cfg, tmp_path, monkeypatch):
    """The redacted API error used to be captured and discarded."""
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    sh = script(tmp_path, 'echo "400 can\'t parse entities" >&2\nexit 1\n')
    machine_toml(telegram={"notify": str(sh)})
    result = telegram.ask(cfg, "ping")
    assert result.delivered is False
    assert result.error == "400 can't parse entities"
    assert ledger(cfg.state_dir)[0]["error"] == "400 can't parse entities"


@pytest.mark.parametrize(
    ("body", "error"),
    [("echo from-stdout\nexit 2\n", "from-stdout"), ("exit 3\n", "exit 3")],
)
def test_script_failure_with_no_stderr_still_says_something(cfg, tmp_path, monkeypatch,
                                                            body, error):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    machine_toml(telegram={"notify": str(script(tmp_path, body))})
    assert telegram.ask(cfg, "ping").error == error


def test_a_missing_script_never_raises(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    machine_toml(telegram={"notify": str(tmp_path / "nope.sh")})
    result = telegram.summary(cfg, "ping")
    assert result.delivered is False and result.error
    assert ledger(cfg.state_dir)[0]["delivered"] is False


def test_a_long_script_error_is_capped(cfg, tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    machine_toml(telegram={"notify": str(script(tmp_path, 'printf "%0600d" 0 >&2\nexit 1\n'))})
    assert len(telegram.ask(cfg, "ping").error) == 400


def test_an_unwritable_log_never_fails_the_send(cfg, tmp_path, sink, monkeypatch):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    monkeypatch.setattr(cfg, "state_dir", not_a_dir)
    assert telegram.ask(cfg, "hi").delivered is True
    assert sent(sink) == ["[project] Asks you: hi"]


def test_every_message_is_one_whole_line_of_the_log(cfg, sink):
    for i in range(5):
        telegram.ask(cfg, f"line {i}\nwith a newline", kind="other")
    assert [r["ask"] for r in ledger(cfg.state_dir)] == [f"line {i} with a newline" for i in range(5)]
    assert len(sent(sink)) == 5  # one line each on the phone too


def test_a_reply_keeps_its_lines_and_is_as_long_as_its_answer(cfg, sink):
    """An answer to a command the owner typed is not a message a swarm starts:
    it is about every swarm, so it names none and goes into no swarm's log."""
    answer = "weekly 48%\n5-hour 11%\n" + "detail " * 100
    assert telegram.reply(answer).delivered
    assert sink.read_text(encoding="utf-8") == answer + "\n"
    assert ledger(cfg.state_dir) == []
    assert len(telegram.reply("z" * 10_000).text) == telegram.MAX_REPLY_CHARS


# -- check ------------------------------------------------------------------
def test_check_passes_under_the_sink(sink):
    machine_toml(telegram={"notify": "/does/not/exist"})
    assert telegram.check() == (True, "sink")


def test_with_no_machine_file_the_bot_is_the_bundled_script_and_the_env_beside_it():
    got = telegram.bot()
    assert got.script == telegram.BUNDLED and got.script.name == "notify.sh"
    assert got.env == telegram.BUNDLED.resolve().parent.parent / ".env"


def test_check_reads_the_env_file_the_sender_reads(tmp_path, monkeypatch):
    """Beside the repo root (``bin/..``), not the owner's own Claude bot, and
    not a file a shell named for itself."""
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    elsewhere = tmp_path / "elsewhere.env"
    elsewhere.write_text("TELEGRAM_BOT_TOKEN=a\nTELEGRAM_CHAT_ID=1\n")
    monkeypatch.setenv("SWARM_TG_ENV", str(elsewhere))
    sh = script(tmp_path, "exit 0\n")
    machine_toml(telegram={"notify": str(sh)})
    ok, detail = telegram.check()
    assert not ok and "telegram env missing" in detail and str(tmp_path / ".env") in detail

    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc\n")
    ok, detail = telegram.check()
    assert not ok and "TELEGRAM_CHAT_ID" in detail

    env.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=42\n")
    assert telegram.check() == (True, "ok")


def test_the_machine_file_names_the_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    env = tmp_path / "bot.env"
    env.write_text("TELEGRAM_BOT_TOKEN=a\nTELEGRAM_CHAT_ID=1\n")
    machine_toml(telegram={"notify": str(script(tmp_path, "exit 0\n")), "env": str(env)})
    assert telegram.bot().env == env
    assert telegram.check() == (True, "ok")


def test_check_rejects_a_script_it_cannot_run(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_TG_SINK", raising=False)
    sh = script(tmp_path, "exit 0\n")
    sh.chmod(0o644)
    machine_toml(telegram={"notify": str(sh)})
    ok, detail = telegram.check()
    assert not ok and "not executable" in detail


def test_a_project_file_that_still_names_the_sender_is_refused(tmp_path):
    """The bot is the machine's. Read from a project, the swarm would send from
    one bot while the machine's listener polls another."""
    (tmp_path / ".swarm.toml").write_text('[telegram]\ncommands = true\nnotify = "/x.sh"\n')
    with pytest.raises(ValueError) as exc:
        load(project_dir=str(tmp_path))
    said = str(exc.value)
    assert "[telegram].notify" in said and str(tmp_path / ".swarm.toml") in said
    assert "machine setting" in said and "one Telegram bot" in said
    (tmp_path / ".swarm.toml").write_text("[telegram]\ncommands = false\n")
    assert load(project_dir=str(tmp_path)).telegram_commands is False


# -- a finishing worker -------------------------------------------------------
def test_the_log_keeps_a_failure_with_its_collapsed_recap():
    assert launch._fail_note("P1", "  build\n  broke ") == (
        "P1 FAILED — build broke. The phases that depend on it wait.")
    assert launch._fail_note("P1", "   ") == "P1 FAILED. The phases that depend on it wait."


@pytest.mark.parametrize(
    ("status", "why"),
    [("ok", "is a silent success"), ("operator", "hands off to a session")],
)
def test_finishes_that_say_nothing_say_why(status, why):
    plan, detail = launch._ping_decision("P1", status, "did it", None, "written", False)
    assert plan == "skipped" and why in detail


def test_a_failure_always_speaks_however_thin_the_note():
    """Silence here would turn a bad recap into an invisible dead run."""
    assert launch._ping_decision("P1", "fail", "x", None, "written", False) == ("send", "")


def test_a_repeated_identical_failure_is_deduped():
    plan, detail = launch._ping_decision("P1", "fail", "tests  red", "tests red", "written", False)
    assert plan == "deduped" and "identical recap" in detail


def test_a_refused_thinner_failure_is_deduped():
    plan, _ = launch._ping_decision("P1", "fail", "x", "a much fuller recap", "refused", False)
    assert plan == "deduped"


def test_force_always_speaks():
    assert launch._ping_decision("P1", "fail", "same", "same", "written", True)[0] == "send"


def test_only_operator_routes_and_only_with_a_real_brief():
    assert launch._route_decision("P1", "fail", "x" * 200)[0] == "skipped"
    thin = launch._route_decision("P1", "operator", "done")
    assert thin[0] == "skipped" and "too thin" in thin[1]
    rich = "rotate the deploy key on the staging box, then re-run the smoke suite"
    assert launch._route_decision("P1", "operator", rich) == ("dispatch", "")


# -- `swarm done` / `swarm waiting` end to end --------------------------------
def test_a_fail_nothing_will_retry_asks_once_in_the_swarms_own_words(cfg, sink):
    """This cfg has no Overseer (the suite's default), so nothing retries it."""
    result = launch.done(cfg, "P1", "fail", "cargo test red on the parser " * 20)
    assert result.ping == "sent"
    assert sent(sink) == [
        "[project] Asks you: Fix what stopped P1, then run `swarm retry P1`: it failed; with"
        " the Overseer off nothing retries it. `swarm report` has its recap."]
    [row] = ledger(cfg.state_dir)
    assert (row["class"], row["kind"], row["phase"], row["source"]) == (
        "ask", "worker-done", "P1", "launch.done")
    # The recap is never cut into the ask; it is kept whole beside it.
    assert row["detail"] == ("cargo test red on the parser " * 20).strip()
    assert "owner asked on telegram" in result.render()


def test_the_fail_ask_says_what_waits_on_the_phase_and_names_it(cfg, sink):
    ledger_file = cfg.project_dir / cfg.ledger
    ledger_file.write_text("P0\nP1 needs:P0\nP2 needs:P1\nP3 needs:P2\n", encoding="utf-8")
    launch.done(cfg, "P1", "fail", "boom")
    assert "it failed, and 2 phases wait on it;" in sent(sink)[0]
    assert len(sent(sink)[0]) <= telegram.PHONE_MAX


def test_done_fail_run_twice_rings_the_phone_once(cfg, sink):
    """A phase that ran `swarm done` several times used to telegram once per run."""
    launch.done(cfg, "P1", "fail", "cargo test red")
    again = launch.done(cfg, "P1", "fail", "cargo test red")
    assert again.ping == "deduped"
    assert len(sent(sink)) == 1
    assert "no telegram: identical recap" in again.render()


def test_done_ok_is_silent(cfg, sink):
    result = launch.done(cfg, "P0", "ok", "wrote the parser")
    assert result.ping == "skipped"
    assert sent(sink) == [] and ledger(cfg.state_dir) == []


def test_done_operator_hands_off_instead_of_asking(cfg, sink, monkeypatch):
    from swarm_orchestrator import opqueue

    cfg.operator_enabled = True
    spawned = []
    monkeypatch.setattr(launch, "_detach_triage", lambda c, p: spawned.append(p) or True)
    note = "rotate the deploy key on the staging box, then re-run the smoke suite"
    result = launch.done(cfg, "P1", "operator", note)
    assert (result.ping, result.route) == ("skipped", "dispatch")
    assert sent(sink) == [] and ledger(cfg.state_dir) == []
    assert [i.phase for i in opqueue.load_all(cfg)] == ["P1"]
    assert spawned == ["P1"]


def test_a_follow_up_with_the_operator_off_asks_and_keeps_the_recap(cfg, sink, monkeypatch):
    """`needs-owner` OWED a ping when it was live; today it is `operator`. With
    the operator off (this cfg's default) only the owner will do it: an ask in
    the swarm's own words, the recap whole beside it for `swarm todo`."""
    monkeypatch.setattr(launch, "_detach_triage", lambda c, p: True)
    assert statuses.NEEDS_OWNER in statuses.OWED_PING
    result = launch.done(cfg, "P1", "needs-owner", "check the auth change")
    assert result.status == "operator" and result.ping == "skipped"
    assert result.route == "owner"
    assert sent(sink) == [
        "[project] Asks you: Do the follow-up that P1 left behind: the operator is switched"
        " off, so nobody else will. `swarm todo` shows what is left to do."]
    [row] = ledger(cfg.state_dir)
    assert (row["kind"], row["detail"]) == ("operator-todo", "check the auth change")


def test_a_dropped_fail_ask_is_reported_by_done(cfg, tmp_path, monkeypatch):
    blocked = tmp_path / "sink-dir"
    blocked.mkdir()
    monkeypatch.setenv("SWARM_TG_SINK", str(blocked))
    result = launch.done(cfg, "P1", "fail", "boom")
    assert result.ping == "failed" and result.ping_detail
    assert "telegram FAILED" in result.render()
    assert ledger(cfg.state_dir)[0]["delivered"] is False


def test_waiting_sends_the_sessions_ask_as_written(cfg, sink):
    owner.waiting(cfg, "P1", "  Pick the schema\n for orders: the migration waits on it. ")
    assert sent(sink) == [
        "[project] Asks you: Pick the schema for orders: the migration waits on it."]
    [row] = ledger(cfg.state_dir)
    assert (row["class"], row["kind"], row["phase"], row["source"]) == (
        "ask", "waiting", "P1", "cli.waiting")


def test_waiting_with_an_ask_too_long_records_nothing_and_tells_nobody(cfg, sink, monkeypatch):
    from swarm_orchestrator import state as state_mod

    poked = []
    monkeypatch.setattr(launch, "_poke_fifo", lambda c, line: poked.append(line) or True)
    with pytest.raises(telegram.TooLong):
        owner.waiting(cfg, "P1", "which schema? " * 40)
    assert sent(sink) == [] and ledger(cfg.state_dir) == [] and poked == []
    assert state_mod.read(cfg).waiting == {}


def test_call_site_kinds_are_declared():
    """The dashboard renders from KINDS; a kind nobody declared is a blank label."""
    import re
    import swarm_orchestrator

    src = Path(swarm_orchestrator.__file__).parent
    used = set()
    for name in ("supervisor", "launch", "owner", "opqueue", "pushowed", "landing", "master",
                 "restart", "blockedping", "cli", "telegram"):
        text = (src / f"{name}.py").read_text(encoding="utf-8")
        used |= set(re.findall(r'(?:\bkind=|said = \{"kind": )"([a-z-]+)"', text))
    assert {"waiting", "session-ask", "summary", "finish", "worker-done"} <= used
    assert used <= set(telegram.KINDS), used - set(telegram.KINDS)


def test_the_run_ends_with_one_last_summary(swarm):
    swarm.env["FAKE_WORKER_SLEEP"] = "2"  # a full run, as test_lifecycle drives it
    swarm.up()
    assert swarm.wait(lambda: "ACTION finish" in swarm.log_text(), timeout=40), swarm.log_text()
    rows = ledger(swarm.state_dir)
    [finish] = [r for r in rows if r.get("kind") == "finish"]
    assert finish["delivered"] is True and finish["class"] == "summary"
    name = swarm.project.name
    assert finish["text"] == (f"[{name}] Overseer: The run has finished: 5 phase(s) landed."
                              " Nothing waits on you.")
    assert [r for r in rows if r.get("delivered")] == [finish]  # and nothing else was sent


def test_a_resolver_asking_the_owner_tells_the_supervisor_it_gave_up(
    sink: Path, tmp_path: Path, monkeypatch
):
    from swarm_orchestrator import cli

    project = tmp_path / "proj"
    project.mkdir()
    cfg = load(project_dir=str(project))
    poked: list[str] = []
    monkeypatch.setattr(launch, "_poke_fifo", lambda cfg, line: poked.append(line) or True)
    monkeypatch.setenv("SWARM_SESSION_ID", "resolver:P1")
    assert cli.cmd_notify(cfg, "Merge P1's ledger rows by hand: I cannot tell which wins.") == 0
    assert poked == ["resolver-escalated P1\n"]
    assert sent(sink) == [
        "[proj] Asks you: Merge P1's ledger rows by hand: I cannot tell which wins."]
    monkeypatch.setenv("SWARM_SESSION_ID", "overseer:x")
    cli.cmd_notify(cfg, "hello")
    assert poked == ["resolver-escalated P1\n"]


def test_notify_refuses_an_ask_too_long_and_says_the_limit(cfg, sink, capsys):
    from swarm_orchestrator import cli

    assert cli.cmd_notify(cfg, "the build broke because " * 30) == 2
    err = capsys.readouterr().err
    assert "nothing was sent" in err and f"at most {telegram.room(cfg)} fit" in err
    assert "Rewrite it, do not cut it" in err
    assert sent(sink) == [] and ledger(cfg.state_dir) == []


# -- a burst of `blocked` finishes is one ask -----------------------------------
def test_blocked_phases_of_one_burst_are_one_ask_that_names_them(cfg, sink):
    from swarm_orchestrator import blockedping
    from swarm_orchestrator.logutil import Log

    wall = "The live box refuses this host's ssh key. Deploy needs it."
    for phase in ("P1", "P2", "P3"):
        result = launch.done(cfg, phase, "blocked", f"{phase}: {wall}")
        assert result.ping == "held"
    launch.done(cfg, "P4", "blocked", "the vendor API is down")
    assert sent(sink) == []
    assert [(r["class"], r["suppressed"]) for r in ledger(cfg.state_dir)] == [
        ("logged", blockedping.HELD)] * 4

    log = Log(cfg.supervisor_log)
    start = blockedping.deadline(cfg) - blockedping.GATHER_S
    assert not blockedping.flush(cfg, log, start + blockedping.GATHER_S - 1)
    assert blockedping.flush(cfg, log, start + blockedping.GATHER_S)
    assert not blockedping.flush(cfg, log, start + 2 * blockedping.GATHER_S)  # nothing left
    log.close()
    assert sent(sink) == [
        "[project] Asks you: Clear what blocks 4 phases (P1, P2, P3 and 1 more), then `swarm"
        " retry` each: they stopped on something outside their own work that the swarm"
        " cannot fix. `swarm report` has their recaps."]
    # The reasons, each once, are kept beside the ask.
    assert ledger(cfg.state_dir)[-1]["detail"] == (
        "The live box refuses this host's ssh key. (P1, P2, P3); the vendor API is down (P4)")
    assert blockedping.deadline(cfg) is None


def test_one_blocked_phase_is_asked_about_by_name(cfg, sink):
    from swarm_orchestrator import blockedping
    from swarm_orchestrator.logutil import Log

    launch.done(cfg, "P1", "blocked", "the box refuses the key")
    log = Log(cfg.supervisor_log)
    assert blockedping.flush(cfg, log, blockedping.deadline(cfg))
    log.close()
    assert sent(sink) == [
        "[project] Asks you: Clear what blocks P1, then run `swarm retry P1`: it stopped on"
        " something outside its own work that the swarm cannot fix. `swarm report` has"
        " its recap."]
