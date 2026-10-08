"""The stall check: a busy worker silent for ``[worker].stall_check_s`` gets a
model's look, and one found stuck is told what is wrong.

Hermetic: transcripts are fake files under a temp ``CLAUDE_CONFIG_DIR``, the
model is the ``SWARM_STALL_CMD`` seam, and tmux is stubbed at the functions the
check calls (pane pid, capture, ``send_submit``).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from swarm_orchestrator import gc as gc_mod
from swarm_orchestrator import stallcheck, tmux
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.supervisor import Supervisor

H = 3600.0
STUCK = {"stuck": True, "why": "teammate w15 idle, its nextest run died",
         "message": "teammate w15 is idle and its run ended with exit 144;\nre-run it in the foreground"}
OK = {"stuck": False, "why": "cargo nextest is running", "message": ""}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for leak in ("SWARM_RECAP_CMD", "ANTHROPIC_API_KEY", "SWARM_STALL_CMD"):
        monkeypatch.delenv(leak, raising=False)
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_SLUG", "stall")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "PHASE-LEDGER.md").write_text("- [ ] `P0` · needs:—\n", encoding="utf-8")
    cfg = load(project_dir=str(tmp_path))
    cfg.stall_check_s = int(H)
    state_mod.init_state(cfg)
    wt = tmp_path / "wt" / "P0"
    wt.mkdir(parents=True)
    with state_mod.transaction(cfg) as st:
        slot = st.slots[0]
        slot.busy, slot.phase, slot.pane_id, slot.worktree = True, "P0", "%7", str(wt)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(tmux, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    monkeypatch.setattr(tmux, "capture", lambda pane: "❯ \n  waiting for w15's report\n")
    monkeypatch.setattr(tmux, "send_submit", lambda pane, text, *a, **k: sent.append((pane, text)) or True)
    monkeypatch.setattr(stallcheck.buildstatus, "snapshot", lambda cfg, n_recent=10: {})
    monkeypatch.setattr(stallcheck.buildstatus, "render", lambda snap: "build gate: idle")
    return {"cfg": cfg, "tmp": tmp_path, "wt": wt, "sent": sent, "mp": monkeypatch}


def _transcripts(env, silent_s: float) -> Path:
    """A lead transcript with one teammate and a background output, all last
    written ``silent_s`` ago."""
    pdir = env["tmp"] / "claude" / "projects" / gc_mod.transcript_name(env["wt"])
    sid = "cf3a0aee"
    lead = pdir / f"{sid}.jsonl"
    subs = pdir / sid / "subagents"
    subs.mkdir(parents=True)
    entries = [
        {"type": "user", "timestamp": "2026-10-08T00:48:54Z",
         "message": {"role": "user", "content": "<teammate-message teammate_id=\"w15\">"
                     "The nextest run is still running. I'll pick up the results.</teammate-message>"}},
        {"type": "assistant", "timestamp": "2026-10-08T00:48:56Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "SECRET-THOUGHT"},
             {"type": "text", "text": "The builder's test run is still going. I'll wait."}]}},
        {"type": "queue-operation", "operation": "enqueue"},
    ]
    lead.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    sub = subs / "agent-a1.jsonl"
    sub.write_text(json.dumps({"type": "assistant", "timestamp": "2026-10-08T00:48:50Z",
                               "message": {"role": "assistant", "content": [
                                   {"type": "tool_use", "name": "Bash",
                                    "input": {"command": "cargo nextest run",
                                              "run_in_background": True}}]}}) + "\n")
    (subs / "agent-a1.meta.json").write_text(json.dumps(
        {"name": "w15", "agentType": "w15", "taskKind": "in_process_teammate"}))
    tasks = (env["cfg"].tmp_dir / "P0" / "claude-1000" / gc_mod.transcript_name(env["wt"])
             / sid / "tasks")
    tasks.mkdir(parents=True)
    out = tasks / "b1zw8zczs.output"
    out.write_text("")
    stamp = time.time() - silent_s
    for path in (lead, sub, out):
        os.utime(path, (stamp, stamp))
    return lead


def _seam(env, *answers: object) -> Path:
    """The model: answers in turn (a dict as JSON, a str as is); every prompt kept."""
    tmp = env["tmp"]
    for i, ans in enumerate(answers):
        (tmp / f"answer{i}").write_text(json.dumps(ans) if isinstance(ans, dict) else ans)
    script = tmp / "model.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"n=$(ls {tmp} | grep -c '^prompt')\n"
        f"cat > {tmp}/prompt$n\n"
        f"cat {tmp}/answer$n\n")
    script.chmod(0o755)
    env["mp"].setenv("SWARM_STALL_CMD", str(script))
    return tmp


def _prompts(env) -> list[str]:
    return [p.read_text() for p in sorted(env["tmp"].glob("prompt*"))]


def _run(sup: Supervisor, now: float | None = None) -> None:
    sup._stall_sweep(state_mod.read(sup.cfg), time.time() if now is None else now)
    for thread in list(sup._stall_threads.values()):
        thread.join(30)


def _log(cfg) -> str:
    return cfg.supervisor_log.read_text(encoding="utf-8")


def _tg(env) -> list[str]:
    path = env["tmp"] / "tg.log"
    return path.read_text().splitlines() if path.is_file() else []


@pytest.fixture
def sup(env):
    s = Supervisor(env["cfg"])
    yield s
    s.log.close()


def test_silence_below_the_threshold_runs_no_check(env, sup):
    _transcripts(env, silent_s=H - 120)
    _seam(env, STUCK)
    _run(sup)
    assert _prompts(env) == [] and env["sent"] == []
    assert "STALL-CHECK" not in _log(env["cfg"])


def test_a_stuck_worker_gets_one_line_in_its_pane(env, sup):
    _transcripts(env, silent_s=H + 60)
    _seam(env, STUCK)
    _run(sup)
    [prompt] = _prompts(env)
    # The material: the lead's words, the teammate by name, the dead run's output
    # file, the pane and the gate; never its thinking or queue noise.
    assert "I'll wait." in prompt and "SECRET-THOUGHT" not in prompt
    assert "name=w15" in prompt and "cargo nextest run" in prompt
    assert "b1zw8zczs.output: 0 bytes" in prompt
    assert "waiting for w15's report" in prompt and "build gate: idle" in prompt
    assert "has written nothing for 1h01m" in prompt
    [(pane, line)] = env["sent"]
    assert pane == "%7"
    assert line.startswith("Swarm stall check: teammate w15 is idle")
    assert "\n" not in line and "re-run it in the foreground" in line
    log = _log(env["cfg"])
    assert "STALL-CHECK P0 silent=" in log and "verdict=stuck why=teammate w15 idle" in log
    assert "STALL-NUDGE P0" in log
    assert _tg(env) == []  # one stuck verdict is the worker's, not the owner's


def test_at_most_one_check_per_window(env, sup):
    _transcripts(env, silent_s=H + 60)
    _seam(env, OK, OK)
    now = time.time()
    _run(sup, now)
    _run(sup, now + 300)  # the next sweep: still silent, but checked just now
    assert len(_prompts(env)) == 1
    _run(sup, now + H + 1)
    assert len(_prompts(env)) == 2


def test_an_ok_verdict_sends_nothing(env, sup):
    _transcripts(env, silent_s=H + 60)
    _seam(env, OK)
    _run(sup)
    assert env["sent"] == []
    assert "verdict=ok why=cargo nextest is running" in _log(env["cfg"])
    assert "STALL-NUDGE" not in _log(env["cfg"])


@pytest.mark.parametrize("answer", ["I think it is stuck", '{"why": "no field"}',
                                   '{"stuck": true, "why": "x", "message": ""}'])
def test_garbage_is_no_verdict(env, sup, answer):
    _transcripts(env, silent_s=H + 60)
    _seam(env, answer)
    _run(sup)
    assert env["sent"] == []
    assert "verdict=none" in _log(env["cfg"])


def test_a_failed_model_call_is_no_verdict(env, sup):
    _transcripts(env, silent_s=H + 60)
    env["mp"].setenv("SWARM_STALL_CMD", "exit 3")
    _run(sup)
    assert env["sent"] == []
    assert "verdict=none why=seam-exit-3" in _log(env["cfg"])


@pytest.mark.parametrize("hold", ["waiting", "parked", "frozen", "capped"])
def test_skipped_while_waiting_on_the_owner_frozen_or_capped(env, sup, hold):
    _transcripts(env, silent_s=H + 60)
    _seam(env, STUCK)
    with state_mod.transaction(env["cfg"]) as st:
        if hold == "waiting":
            st.waiting["P0"] = time.time() + 120
        elif hold == "parked":
            st.parked.append("P0")
        elif hold == "frozen":
            st.frozen = {"stage": "frozen", "since": time.time()}
        else:
            st.usage_hold = {"five_hour": {"pct": 95}}
    _run(sup)
    assert _prompts(env) == [] and env["sent"] == []


def test_two_stuck_checks_in_a_row_ask_the_owner(env, sup):
    _transcripts(env, silent_s=H + 60)
    _seam(env, STUCK, STUCK)
    now = time.time()
    _run(sup, now)
    assert _tg(env) == []
    _run(sup, now + H + 1)
    assert len(env["sent"]) == 2
    [ask] = _tg(env)
    assert "Look at worker P0" in ask and "stuck on two checks in a row" in ask
    assert "STALL-NUDGE P0 streak=2" in _log(env["cfg"])


def test_an_ok_verdict_between_two_stuck_ones_asks_nobody(env, sup):
    _transcripts(env, silent_s=H + 60)
    _seam(env, STUCK, OK, STUCK)
    now = time.time()
    for k in range(3):
        _run(sup, now + k * (H + 1))
    assert len(env["sent"]) == 2 and _tg(env) == []


def test_zero_disables_it(env, sup):
    _transcripts(env, silent_s=10 * H)
    _seam(env, STUCK)
    sup.cfg.stall_check_s = 0
    _run(sup)
    assert _prompts(env) == [] and env["sent"] == []


def test_no_transcript_means_no_check(env, sup):
    _seam(env, STUCK)
    _run(sup)
    assert _prompts(env) == []


def test_a_recent_subagent_write_counts_as_activity(env, sup):
    lead = _transcripts(env, silent_s=2 * H)
    sub = lead.parent / lead.stem / "subagents" / "agent-a1.jsonl"
    os.utime(sub, None)  # a teammate that is still working
    _seam(env, STUCK)
    _run(sup)
    assert _prompts(env) == []


def test_the_watchdog_sweep_starts_it(env, sup):
    _transcripts(env, silent_s=H + 60)
    _seam(env, STUCK)
    sup.watchdog_s = 300
    sup._watchdog_tick()
    for thread in list(sup._stall_threads.values()):
        thread.join(30)
    assert len(env["sent"]) == 1


def test_parse_takes_json_inside_noise():
    v = stallcheck.parse('```json\n{"stuck": false, "why": "running", "message": ""}\n```')
    assert v.stuck is False and v.why == "running"
    assert stallcheck.parse(None).stuck is None


def test_the_nudge_is_one_line_and_bounded():
    line = stallcheck.nudge_line("a\nb " + "x" * 2000)
    assert "\n" not in line and line.startswith(stallcheck.PREFIX + "a b ")
    assert len(line) <= stallcheck.NUDGE_CHARS
