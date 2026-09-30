"""Usage caps: readings, the rules, the endpoint fallback, and what the swarm does.

What is load-bearing here:

* a ``pause`` rule holds new launches with its own record, so lifting it never
  undoes a pause the owner made, and ``swarm resume`` alone never overrides it;
* a hold lifts only once its window has reset *and* a fresh reading is under the
  limit — a lagging or stale reading can neither lift nor create one;
* a ``down`` rule runs ``swarm down`` once per window, with one ping;
* the usage endpoint is asked only when the tap has nothing fresh, at most once
  per half hour, and its token is never sent once expired and never logged.

The endpoint is always mocked; nothing here reaches the network.
"""

from __future__ import annotations

import io
import os
import json
import time
import urllib.error

import pytest

from swarm_orchestrator import caps
from swarm_orchestrator import launch as launch_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import supervisor as sup_mod
from swarm_orchestrator import usage as usage_mod
from swarm_orchestrator import why as why_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load
from swarm_orchestrator.doctor import WARN, _check_usage
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor
from test_deps import _bare_cfg

NOW = 1_790_000_000.0
WEEK_RESET = NOW + 3 * 86400
FIVE_RESET = NOW + 2 * 3600
RULES = [
    {"window": "week", "at": 60, "action": "pause"},
    {"window": "week", "at": 70, "action": "down"},
    {"window": "five_hour", "at": 90, "action": "pause"},
]
LEDGER = "- [ ] `P0` · needs:—\n- [ ] `P1` · needs:—\n"


def _r(pct, resets=WEEK_RESET, ts=NOW):
    return caps.Reading(pct=pct, resets_at=resets, ts=ts)


def _eval(reads, hold=None, fired=None, override=None, rules=RULES, now=NOW):
    return caps.evaluate(rules, reads, hold or {}, fired or {}, override or {}, now)


# -- the rules ---------------------------------------------------------------------
def test_a_crossing_holds_once_and_says_so_once():
    out = _eval({"week": _r(61)})
    assert out.held == ["week"] and out.down is None
    assert out.hold["week"] == {"at": 60, "pct": 61, "resets_at": WEEK_RESET, "since": NOW}
    again = _eval({"week": _r(63, ts=NOW + 600)}, hold=out.hold, now=NOW + 600)
    assert again.held == [] and again.hold["week"]["pct"] == 63
    assert again.hold["week"]["since"] == NOW


def test_a_lagging_or_stale_reading_never_lifts_a_hold():
    hold = _eval({"week": _r(61)}).hold
    for reads in ({"week": _r(44)}, {}):  # a lagging session; nothing fresh at all
        out = _eval(reads, hold=hold, now=NOW + 600)
        assert out.hold == hold and not out.lifted and not out.released


def test_a_stale_reading_never_creates_a_hold():
    assert _eval({}).hold == {}


def test_the_hold_lifts_after_the_reset_on_a_fresh_reading_under_the_limit():
    hold = _eval({"week": _r(61)}).hold
    after = WEEK_RESET + 60
    # Reset, but still over the limit in the new window: held on.
    assert _eval({"week": _r(65, WEEK_RESET + 7 * 86400, after)}, hold=hold, now=after).hold
    out = _eval({"week": _r(2, WEEK_RESET + 7 * 86400, after)}, hold=hold, now=after)
    assert out.hold == {} and out.lifted == ["week"]


def test_a_raised_limit_or_caps_turned_off_release_the_hold():
    hold = _eval({"week": _r(61)}).hold
    raised = [{"window": "week", "at": 80, "action": "pause"}]
    assert _eval({}, hold=hold, rules=raised).released == ["week"]
    assert _eval({"week": _r(61)}, hold=hold, rules=[]).released == ["week"]


def test_windows_are_independent():
    out = _eval({"week": _r(40), "five_hour": _r(91, FIVE_RESET)})
    assert list(out.hold) == ["five_hour"]
    out = _eval({"week": _r(40), "five_hour": _r(3, FIVE_RESET + 5 * 3600, FIVE_RESET + 60)},
                hold=out.hold, now=FIVE_RESET + 60)
    assert out.hold == {} and out.lifted == ["five_hour"]


def test_an_override_runs_through_its_window_and_ends_at_the_reset():
    override = {"week": WEEK_RESET}
    assert _eval({"week": _r(65)}, override=override).hold == {}
    after = WEEK_RESET + 60
    out = _eval({"week": _r(61, WEEK_RESET + 7 * 86400, after)}, override=override, now=after)
    assert out.override == {} and out.held == ["week"]


def test_down_acts_once_per_window():
    out = _eval({"week": _r(71)})
    assert out.down == {"window": "week", "at": 70, "pct": 71, "resets_at": WEEK_RESET}
    assert out.fired == {"week:70": WEEK_RESET}
    assert _eval({"week": _r(75)}, hold=out.hold, fired=out.fired).down is None
    after = WEEK_RESET + 60
    nxt = _eval({"week": _r(72, WEEK_RESET + 7 * 86400, after)}, fired=out.fired, now=after)
    assert nxt.down is not None


def _sample(ts, week=None, five=None, week_reset=WEEK_RESET, five_reset=FIVE_RESET, account=None):
    return usage_mod.Sample(ts=ts, week_pct=week, week_resets_at=week_reset,
                            five_pct=five, five_resets_at=five_reset, account=account)


# -- accounts ----------------------------------------------------------------------
A, B = "acct-a", "acct-b"


def _on(account, reads, **kw):
    kw.setdefault("now", NOW)
    return caps.evaluate(kw.pop("rules", RULES), reads, kw.pop("hold", {}), kw.pop("fired", {}),
                         kw.pop("override", {}), kw.pop("now"), account)


def test_a_hold_lifts_when_another_account_reads_under_the_limit():
    hold = _on(A, {"week": _r(91)}).hold
    assert hold["week"]["account"] == A
    # The new account's week resets *before* the held one: under the old rule
    # that read as a lagging reading of the held window, and held until 03:00.
    for resets in (NOW + 3600, WEEK_RESET + 6 * 86400):
        out = _on(B, {"week": _r(2, resets, NOW + 60)}, hold=hold, now=NOW + 60)
        assert out.hold == {} and out.switched == ["week"] and not out.lifted


def test_the_same_accounts_lagging_reading_never_lifts_its_hold():
    hold = _on(A, {"week": _r(91)}).hold
    for account, reads in ((A, {"week": _r(44)}), (B, {})):  # lagging; nothing fresh on B
        out = _on(account, reads, hold=hold, now=NOW + 600)
        assert out.hold == hold and not (out.lifted or out.switched or out.released)


def test_a_switch_to_an_account_over_the_limit_holds_it_anew():
    hold = _on(A, {"week": _r(91)}).hold
    out = _on(B, {"week": _r(95, NOW + 3600, NOW + 60)}, hold=hold, now=NOW + 60)
    assert out.held == ["week"]
    assert out.hold["week"] == {"at": 60, "pct": 95, "resets_at": NOW + 3600,
                                "since": NOW + 60, "account": B}


def test_down_crossings_and_overrides_are_per_account():
    first = _on(A, {"week": _r(71)})
    assert first.down is not None and first.fired == {"week:70@acct-a": WEEK_RESET}
    other = _on(B, {"week": _r(72, NOW + 3600)}, fired=first.fired)
    assert other.down is not None
    back = _on(A, {"week": _r(73)}, fired=other.fired)  # A's window again: acted already
    assert back.down is None
    # An override written before the tag (plain key) still counts for its own window.
    assert _on(A, {"week": _r(71)}, fired={"week:70": WEEK_RESET}).down is None
    override = {"week": WEEK_RESET, "week@acct-a": WEEK_RESET}
    assert _on(A, {"week": _r(65)}, override=override).hold == {}
    assert _on(B, {"week": _r(65, NOW + 3600)}, override=override).held == ["week"]


def test_a_hold_from_before_the_tag_is_adopted_by_its_own_window():
    legacy = {"week": {"at": 60, "pct": 61, "resets_at": WEEK_RESET, "since": NOW}}
    assert _on(A, {"week": _r(62)}, hold=legacy).hold["week"]["account"] == A
    out = _on(A, {"week": _r(44)}, hold=legacy)  # lagging, but its own window
    assert out.hold["week"] == legacy["week"] | {"account": A}
    # Without an account on either side it is the old rule, untouched.
    assert _on(None, {"week": _r(44)}, hold=legacy).hold == legacy


def test_readings_never_blend_accounts():
    samples = [_sample(NOW - 60, week=91, five=50, account=A),
               _sample(NOW - 30, week=2, five=1, week_reset=NOW + 3600, five_reset=NOW + 1800,
                       account=B)]
    assert caps.readings(usage_mod.of_account(samples, B), NOW, 1800)["week"].pct == 2
    assert caps.readings(usage_mod.of_account(samples, A), NOW, 1800)["week"].pct == 91
    line = caps.reading_line(caps.readings(usage_mod.of_account(samples, B), NOW, 1800), NOW, B)
    assert line.startswith("Usage now: weekly 2%, 5-hour 1% (as of") and "account acct-b" in line


# -- readings ----------------------------------------------------------------------


def test_a_reading_is_the_highest_fresh_figure_of_the_newest_window():
    samples = [_sample(NOW - 3000, week=80, five=10),  # stale
               _sample(NOW - 60, week=47, five=12),
               _sample(NOW - 30, week=44, five=11)]  # a lagging session
    reads = caps.readings(samples, NOW, 1800)
    assert reads["week"] == caps.Reading(47, WEEK_RESET, NOW - 30)
    assert reads["five_hour"].pct == 12
    assert not caps.needs_api(samples, NOW, 1800)


def test_a_window_that_reset_since_its_reading_has_no_reading():
    samples = [_sample(NOW - 60, week=47, five=95, five_reset=NOW - 10)]
    assert list(caps.readings(samples, NOW, 1800)) == ["week"]
    assert caps.needs_api(samples, NOW, 1800)
    assert caps.needs_api([], NOW, 1800)


# -- the endpoint --------------------------------------------------------------------
@pytest.fixture
def creds(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "sk-ant-oat-SECRET", "refreshToken": "sk-ant-ort-SECRET",
        "expiresAt": int((NOW + 3600) * 1000)}}))
    return path


class FakeOpener:
    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if isinstance(self.answer, Exception):
            raise self.answer
        return io.BytesIO(json.dumps(self.answer).encode())


def test_the_endpoint_answer_becomes_an_ordinary_sample(tmp_path, creds, monkeypatch):
    opener = FakeOpener({"five_hour": {"utilization": 11.0, "resets_at": "2026-09-27T00:20:00+00:00"},
                         "seven_day": {"utilization": 48.0, "resets_at": "2026-09-30T08:00:00+00:00"}})
    monkeypatch.setattr(caps, "_urlopen", opener)
    before = creds.read_text()
    row, note = caps.fetch_api(tmp_path, NOW, credentials=creds)
    assert note == "ok" and row["src"] == "api" and row["ts"] == NOW
    assert row["week_pct"] == 48.0 and row["five_pct"] == 11.0
    assert row["week_resets_at"] == 1790755200.0
    req = opener.requests[0]
    assert req.full_url == caps.API_URL
    assert req.get_header("Anthropic-beta") == "oauth-2025-04-20"
    assert req.get_header("Authorization") == "Bearer sk-ant-oat-SECRET"
    assert creds.read_text() == before  # never refreshed, never written
    caps.record_api(tmp_path, row)
    assert usage_mod.load_samples(tmp_path / "meters" / "limits.jsonl")[0].week_pct == 48.0


@pytest.mark.parametrize("answer, expect", [
    (urllib.error.HTTPError(caps.API_URL, 401, "Unauthorized", {}, None), "HTTP 401"),
    (urllib.error.URLError("timed out"), "did not answer"),
    ({"something": "else"}, "unexpected shape"),
])
def test_a_failed_call_says_why_without_the_token(tmp_path, creds, monkeypatch, answer, expect):
    monkeypatch.setattr(caps, "_urlopen", FakeOpener(answer))
    row, note = caps.fetch_api(tmp_path, NOW, credentials=creds)
    assert row is None and expect in note and "SECRET" not in note


def test_an_expired_or_missing_login_is_never_sent(tmp_path, creds, monkeypatch):
    opener = FakeOpener({})
    monkeypatch.setattr(caps, "_urlopen", opener)
    row, note = caps.fetch_api(tmp_path, NOW + 7200, credentials=creds)
    assert row is None and "expired" in note
    row, note = caps.fetch_api(tmp_path, NOW, credentials=tmp_path / "missing.json")
    assert row is None and note == "no Claude login found"
    assert opener.requests == []


# -- the supervisor ------------------------------------------------------------------
@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_USAGE", "1")
    c = _bare_cfg(tmp_path, monkeypatch, LEDGER)
    state_mod.init_state(c)
    return c


@pytest.fixture
def api(monkeypatch):
    """The endpoint, mocked: records calls, answers with ``api.answer``."""
    class Api:
        answer: tuple = (None, "the usage endpoint answered HTTP 500")
        calls: list = []

    def fetch(state_dir, now, credentials=None):
        Api.calls.append(now)
        return Api.answer

    monkeypatch.setattr(caps, "fetch_api", fetch)
    return Api


@pytest.fixture
def sup(cfg, api, monkeypatch):
    s = Supervisor(cfg)
    s.downs = []
    monkeypatch.setattr(s, "_usage_down", lambda: s.downs.append(time.time()))
    yield s
    s.log.close()


def _tap(cfg, week=None, five=None, ago=60.0, week_reset=None, five_reset=None, account=None):
    now = time.time()
    row = usage_mod.sample_row(
        now - ago, None,
        None if five is None else {"pct": five, "resets_at": five_reset or now + 7200},
        None if week is None else {"pct": week, "resets_at": week_reset or now + 3 * 86400},
        account)
    path = cfg.state_dir / "meters" / "limits.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def _tg(tmp_path) -> list[str]:
    path = tmp_path / "tg.log"
    return path.read_text().splitlines() if path.is_file() else []


def test_a_usage_hold_stops_launches_and_pings_once(sup, cfg, tmp_path, api):
    _tap(cfg, week=61, five=20)
    sup._usage_tick()
    st = state_mod.read(cfg)
    assert st.usage_hold["week"]["pct"] == 61 and not st.paused
    assert sup._fill_slots("test") == []
    log = Log(cfg.supervisor_log)
    assert launch_mod.launch_outcome(cfg, "P0", log) == launch_mod.DENIED
    log.close()
    assert "LAUNCH-DENIED P0 usage-cap" in cfg.supervisor_log.read_text()
    pings = _tg(tmp_path)
    assert len(pings) == 1 and pings[0].startswith("Swarm paused: weekly usage reached 61%")
    sup._usage_check(time.time())  # the next check: still held, no new ping
    assert len(_tg(tmp_path)) == 1
    assert api.calls == []  # the tap was fresh


def test_the_lift_never_undoes_the_owners_pause(sup, cfg, tmp_path):
    reset = time.time() + 3600
    _tap(cfg, week=61, five=20, week_reset=reset)
    sup._usage_check(time.time())
    with state_mod.transaction(cfg) as st:
        st.paused = True
        st.usage_hold["week"]["resets_at"] = time.time() - 60  # the window has reset
    _tap(cfg, week=1, five=20, ago=0, week_reset=time.time() + 7 * 86400)
    sup._usage_check(time.time())
    st = state_mod.read(cfg)
    assert st.usage_hold == {} and st.paused
    assert _tg(tmp_path)[-1].startswith("Swarm resumed: the weekly usage window reset")
    assert getattr(sup, "stub_launches", []) == []


def test_a_lift_launches_again(sup, cfg):
    _tap(cfg, week=61, five=20)
    sup._usage_check(time.time())
    with state_mod.transaction(cfg) as st:
        st.usage_hold["week"]["resets_at"] = time.time() - 60
    _tap(cfg, week=1, five=20, ago=0, week_reset=time.time() + 7 * 86400)
    sup._usage_check(time.time())
    assert sup.stub_launches == ["P0", "P1"]


def test_down_runs_once_with_one_ping(sup, cfg, tmp_path):
    _tap(cfg, week=71, five=20)
    sup._usage_check(time.time())
    assert len(sup.downs) == 1
    pings = _tg(tmp_path)
    assert len(pings) == 1
    assert pings[0].startswith("Swarm stopped: weekly usage reached 71% (your limit 70%).")
    assert "Start it again with swarm up when you want." in pings[0]
    sup._usage_check(time.time())
    assert len(sup.downs) == 1 and len(_tg(tmp_path)) == 1
    # A restart keeps the record: the same crossing does not stop it again.
    state_mod.init_state(cfg)
    fresh = Supervisor(cfg)
    fresh.downs = []
    fresh._usage_down = lambda: fresh.downs.append(1)
    fresh._usage_check(time.time())
    fresh.log.close()
    assert fresh.downs == [] and state_mod.read(cfg).usage_hold


def test_down_is_the_swarm_down_command(cfg, monkeypatch):
    seen = []
    monkeypatch.setattr(sup_mod.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)))
    s = Supervisor(cfg)
    s._usage_down()
    s.log.close()
    argv, kw = seen[0]
    assert argv[1:] == ["-m", "swarm_orchestrator", "--project-dir", str(cfg.project_dir), "down"]
    assert kw["start_new_session"] is True


def test_the_endpoint_is_asked_only_when_the_tap_is_stale_and_at_most_half_hourly(sup, cfg, api):
    _tap(cfg, week=40, five=10, ago=3600)  # an hour old
    now = time.time()
    api.answer = (usage_mod.sample_row(now, None, {"pct": 12, "resets_at": now + 7200},
                                       {"pct": 61, "resets_at": now + 86400}) | {"src": "api"},
                  "ok")
    sup._usage_check(now)
    assert len(api.calls) == 1
    assert state_mod.read(cfg).usage_hold["week"]["pct"] == 61  # the API reading counted
    rows = (cfg.state_dir / "meters" / "limits.jsonl").read_text().splitlines()
    assert json.loads(rows[-1])["src"] == "api"
    assert "USAGE-API ok" in cfg.supervisor_log.read_text()
    api.answer = (None, "the usage endpoint answered HTTP 500")
    sup._usage_check(now + 1900)  # the API reading is now stale too, and 30 min passed
    sup._usage_check(now + 2000)  # but not twice in half an hour
    assert len(api.calls) == 2
    assert state_mod.read(cfg).usage_hold  # a failed call keeps the last hold


def test_a_failed_first_call_creates_no_hold(sup, cfg, api):
    sup._usage_check(time.time())
    assert api.calls and state_mod.read(cfg).usage_hold == {}


def test_turning_caps_off_by_reload_lifts_the_hold(sup, cfg, monkeypatch):
    _tap(cfg, week=61, five=20)
    sup._usage_check(time.time())
    monkeypatch.setenv("SWARM_USAGE", "0")
    sup._on_reload()
    assert state_mod.read(cfg).usage_hold == {}
    assert "USAGE-RELEASE week" in cfg.supervisor_log.read_text()
    assert sup.stub_launches[:2] == ["P0", "P1"]


def _login(tmp_path, monkeypatch, uuid):
    path = tmp_path / f"claude-{uuid[-1]}.json"
    path.write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}))
    monkeypatch.setenv("SWARM_CLAUDE_JSON", str(path))
    return usage_mod.account_key(uuid)


def test_a_login_switch_lifts_the_hold_at_once(sup, cfg, tmp_path, api, monkeypatch):
    """The owner's case: held at 90% weekly on one account, then ``/login`` to
    another whose week is at 0% and resets later — or sooner."""
    a = _login(tmp_path, monkeypatch, "00000000-0000-0000-0000-00000000000a")
    _tap(cfg, week=91, five=20, week_reset=time.time() + 3 * 3600, account=a)
    sup._usage_tick()
    assert state_mod.read(cfg).usage_hold["week"]["account"] == a
    b = _login(tmp_path, monkeypatch, "00000000-0000-0000-0000-00000000000b")
    now = time.time()
    with state_mod.transaction(cfg) as st:
        st.usage_api_at = now  # the endpoint was just asked: a switch asks again anyway
    api.answer = (usage_mod.sample_row(now, None, {"pct": 0, "resets_at": now + 7200},
                                       {"pct": 1, "resets_at": now + 3600}, b) | {"src": "api"},
                  "ok")
    sup._usage_tick()  # long before `check_s`: the switch is checked at once
    assert len(api.calls) == 1
    assert state_mod.read(cfg).usage_hold == {}
    assert f"USAGE-LIFT week account switched to {b}" in cfg.supervisor_log.read_text()
    assert _tg(tmp_path)[-1].startswith(
        f"Swarm resumed: the weekly cap held another account; Claude is now logged in as "
        f"account {b}, whose weekly usage is 1%.")
    assert sup.stub_launches[:2] == ["P0", "P1"]
    sup._usage_tick()  # same login, inside the interval: no further check
    assert len(api.calls) == 1


def test_a_lagging_reading_of_the_held_account_keeps_the_hold(sup, cfg, tmp_path, monkeypatch):
    a = _login(tmp_path, monkeypatch, "00000000-0000-0000-0000-00000000000a")
    _tap(cfg, week=91, five=20, account=a)
    sup._usage_check(time.time())
    _tap(cfg, week=40, five=20, ago=0, account=a)  # a session whose status line lags
    _tap(cfg, week=5, five=5, ago=0, week_reset=time.time() + 3600)  # untagged: anyone's
    sup._usage_check(time.time())
    assert state_mod.read(cfg).usage_hold["week"]["pct"] == 91


def test_the_check_is_scheduled_not_polled(sup, cfg):
    sup._usage_tick()
    left = sup._next_timeout()
    assert left is not None and cfg.usage_check_s - 5 < left <= cfg.usage_check_s


# -- what the owner sees -------------------------------------------------------------
def _held(cfg, pct=61):
    with state_mod.transaction(cfg) as st:
        st.usage_hold = {"week": {"at": 60, "pct": pct, "resets_at": time.time() + 86400,
                                  "since": time.time()}}


def test_resume_explains_the_hold_and_keeps_it(cfg, capsys):
    _held(cfg)
    with state_mod.transaction(cfg) as st:
        st.paused = True
    assert cli_main(["--project-dir", str(cfg.project_dir), "resume"]) == 0
    out = capsys.readouterr().out
    assert "Paused by usage cap: weekly 61% (limit 60%). Resumes automatically after the reset" in out
    assert "swarm resume --override-cap" in out
    st = state_mod.read(cfg)
    assert not st.paused and st.usage_hold


def test_override_runs_until_the_reset(cfg, capsys):
    _held(cfg)
    reset = state_mod.read(cfg).usage_hold["week"]["resets_at"]
    assert cli_main(["--project-dir", str(cfg.project_dir), "resume", "--override-cap"]) == 0
    assert "usage cap overridden: new workers start again until the weekly reset" in (
        capsys.readouterr().out)
    st = state_mod.read(cfg)
    assert st.usage_hold == {} and st.usage_override == {"week": reset}
    # A hold on a known account is overridden for that account; the plain key
    # stays for a supervisor older than the tag.
    with state_mod.transaction(cfg) as st:
        st.usage_override = {}
        st.usage_hold = {"week": {"at": 60, "pct": 61, "resets_at": reset,
                                  "since": time.time(), "account": "acct-a"}}
    assert cli_main(["--project-dir", str(cfg.project_dir), "resume", "--override-cap"]) == 0
    assert state_mod.read(cfg).usage_override == {"week": reset, "week@acct-a": reset}


def test_status_why_and_doctor_say_it_in_plain_english(cfg, capsys):
    _held(cfg)
    assert cli_main(["--project-dir", str(cfg.project_dir), "status"]) == 0
    out = capsys.readouterr().out
    assert "Paused by usage cap: weekly 61% (limit 60%)." in out
    assert "Usage now: unknown (no recent reading)." in out
    assert why_mod.explain(cfg, "P0").detail.startswith(
        "ready — but paused by usage cap: weekly 61% (limit 60%).")
    check = _check_usage(cfg, state_mod.read(cfg))
    assert check.status == WARN and "Paused by usage cap" in check.detail
    with state_mod.transaction(cfg) as st:
        st.usage_hold = {}
    _tap(cfg, week=48, five=11)
    detail = _check_usage(cfg, state_mod.read(cfg)).detail
    assert "Usage now: weekly 48%, 5-hour 11%" in detail
    assert "The swarm pauses at weekly 60% and 5-hour 90%, and stops at weekly 70%." in detail


def test_rules_are_validated_at_load(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / ".swarm.toml").write_text(
        '[usage]\nrules = [{ window = "month", at = 60, action = "pause" }]\n')
    with pytest.raises(ValueError, match=r"\[usage\].rules\[0\].window"):
        load(project_dir=str(tmp_path))
    (tmp_path / ".swarm.toml").write_text(
        '[usage]\nrules = [{ window = "week", at = 50, action = "pause" }]\n')
    assert load(project_dir=str(tmp_path)).usage_rules == [
        {"window": "week", "at": 50, "action": "pause"}]


# -- end to end ----------------------------------------------------------------------
def _seed(swarm, week: float) -> None:
    now = time.time()
    meters = swarm.state_dir / "meters"
    meters.mkdir(parents=True, exist_ok=True)
    row = usage_mod.sample_row(now, None, {"pct": 20, "resets_at": now + 7200},
                               {"pct": week, "resets_at": now + 3 * 86400})
    with (meters / "limits.jsonl").open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def test_up_over_a_cap_launches_nothing_and_a_down_crossing_stops_the_swarm(swarm):
    swarm.env["SWARM_USAGE"] = "1"
    swarm.state_dir.mkdir(parents=True, exist_ok=True)
    # The tap is fresh, and the endpoint counts as just asked: nothing here may
    # reach the owner's real login, and `swarm up` carries this over.
    (swarm.state_dir / "state.json").write_text(json.dumps({"usage_api_at": time.time()}))
    _seed(swarm, 61)
    swarm.up()
    assert swarm.wait(lambda: "USAGE-HOLD week 61% limit=60%" in swarm.log_text()), swarm.log_text()
    assert swarm.wait(lambda: "MASTER-IDLE" in swarm.log_text() or "master-idle" in swarm.log_text())
    time.sleep(1.0)
    assert swarm.busy_count() == 0 and "LAUNCH-READY" not in swarm.log_text()
    status = swarm.cli("status").stdout
    assert "Paused by usage cap: weekly 61% (limit 60%)." in status

    pid = swarm.state()["supervisor_pid"]
    _seed(swarm, 71)
    with open(swarm.state_dir / "control.fifo", "w") as fifo:
        fifo.write("reload\n")  # checks the caps at once, as a reload does
    assert swarm.wait(lambda: "USAGE-DOWN week 71% limit=70%" in swarm.log_text()), swarm.log_text()

    def gone():
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False

    assert swarm.wait(gone, timeout=40), swarm.log_text()
    assert swarm.wait(lambda: "swarm down" in (swarm.state_dir / "logs" / "usage-down.log").read_text())
    stops = [line for line in swarm.tg_lines() if line.startswith("Swarm stopped:")]
    assert len(stops) == 1 and "weekly usage reached 71% (your limit 70%)" in stops[0]
    assert state_mod.State.from_dict(swarm.state()).usage_fired
