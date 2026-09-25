"""Recap generation against synthetic turn/sentinel material.

No model is ever called here: the ``SWARM_RECAP_CMD`` seam stands in for Haiku
exactly the way ``SWARM_MASTER_CMD`` stands in for the LLM master elsewhere in
the suite, so the tests assert on *which* path was taken and what it produced
rather than on a model's prose. The cases that matter are the ones where a real
campaign degrades: no turns file, a truncated jsonl line, a model that exits
non-zero, no ``claude`` on PATH.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from swarm_orchestrator import recap
from swarm_orchestrator.config import load

# A note over the 40-word short-circuit threshold, so the model path is taken.
LONG_NOTE = " ".join(f"word{i}" for i in range(60))


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "PHASE-LEDGER.md").write_text("P0\nP1 needs:P0\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_SLUG", "recaptest")
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    for leak in ("SWARM_RECAP_CMD", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                 "SWARM_MASTER_CMD", "SWARM_WORKER_CMD", "SWARM_SESSION", "SWARM_LAYOUT"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    return c


def write_sentinel(cfg, phase: str, status: str, note: str) -> None:
    cfg.done_dir.mkdir(parents=True, exist_ok=True)
    (cfg.done_dir / f"{phase}.{status}").write_text(
        f"{phase} {status} {note}\n", encoding="utf-8"
    )


def write_turns(cfg, phase: str, texts: list[str]) -> None:
    path = recap.turns_path(cfg, phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for i, text in enumerate(texts):
            fh.write(json.dumps({"ts": 1000.0 + i, "session_id": "s1", "text": text}) + "\n")


def seam(monkeypatch, tmp_path: Path, reply: str = "Model wrote this.", rc: int = 0):
    """Point SWARM_RECAP_CMD at a script that records that it ran."""
    marker = tmp_path / "model-was-called"
    script = tmp_path / "fake-model.sh"
    script.write_text(
        f"#!/bin/sh\ncat > /dev/null\ntouch {marker}\nprintf '%s' {reply!r}\nexit {rc}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("SWARM_RECAP_CMD", str(script))
    return marker


# -- the no-model paths ---------------------------------------------------
def test_short_completion_note_is_the_recap_and_costs_no_model_call(cfg, monkeypatch, tmp_path):
    """A worker's own one-liner already IS the recap; paraphrasing it can only
    lose information, so the model is never asked."""
    marker = seam(monkeypatch, tmp_path)
    write_sentinel(cfg, "P0", "ok", "Ledger parser landed and the whole suite is green.")
    write_turns(cfg, "P0", ["a much longer turn " * 40])

    rec = recap.summarize(cfg, "P0")

    assert rec.summary == "Ledger parser landed and the whole suite is green."
    assert rec.status == "ok"
    assert rec.reason is None
    assert not marker.exists(), "the model was called for text that was already short"


def test_short_final_turn_serves_when_there_is_no_sentinel(cfg, monkeypatch, tmp_path):
    marker = seam(monkeypatch, tmp_path)
    write_turns(cfg, "P0", ["First turn.", "Wired the resolver and pushed it."])

    rec = recap.summarize(cfg, "P0")

    assert rec.summary == "Wired the resolver and pushed it."
    assert rec.status is None
    assert not marker.exists()


def test_markdown_in_the_captured_text_is_flattened(cfg, monkeypatch, tmp_path):
    """A dashboard cell wants a sentence, and workers write markdown."""
    seam(monkeypatch, tmp_path)
    write_turns(cfg, "P0", ["## Done\n\n- Landed `parse()`\n- **Green** suite"])

    rec = recap.summarize(cfg, "P0")

    assert rec.summary == "Done Landed parse() Green suite"


# -- the model path -------------------------------------------------------
def test_long_material_goes_to_the_model(cfg, monkeypatch, tmp_path):
    marker = seam(monkeypatch, tmp_path, reply="Shipped the merge queue; one deviation to review.")
    write_sentinel(cfg, "P1", "needs-owner", LONG_NOTE)

    rec = recap.summarize(cfg, "P1")

    assert marker.exists()
    assert rec.summary == "Shipped the merge queue; one deviation to review."
    assert rec.status == "needs-owner"
    assert LONG_NOTE in rec.raw


def test_only_the_last_turns_reach_the_model(cfg, monkeypatch, tmp_path):
    """Earlier turns are mid-flight noise, and every one is tokens spent on text
    the recap will not mention."""
    seam(monkeypatch, tmp_path)
    write_turns(cfg, "P0", [f"turn-{i} " + "filler " * 30 for i in range(12)])

    rec = recap.summarize(cfg, "P0")

    assert "turn-11" in rec.raw
    assert "turn-4" in rec.raw
    assert "turn-3" not in rec.raw, "window should be the last 8 turns"


def test_material_is_budgeted_from_the_back(cfg, monkeypatch, tmp_path):
    """When the turns overflow the char budget the OLDEST are dropped: the last
    thing a worker said is the thing the recap is about."""
    seam(monkeypatch, tmp_path)
    write_turns(cfg, "P0", ["OLD " + "x" * 9000, "NEW " + "y" * 9000])

    rec = recap.summarize(cfg, "P0")

    assert "NEW" in rec.raw
    assert "OLD" not in rec.raw
    assert len(rec.raw) <= 12_000


def test_a_runaway_summary_is_clamped(cfg, monkeypatch, tmp_path):
    """A model that ignores the word limit must not be able to blow up a
    one-line UI."""
    seam(monkeypatch, tmp_path, reply=" ".join(f"w{i}" for i in range(200)))
    write_sentinel(cfg, "P0", "ok", LONG_NOTE)

    rec = recap.summarize(cfg, "P0")

    assert len(rec.summary.split()) <= 61  # 60 words + the ellipsis token
    assert rec.summary.endswith("...")


# -- degradation ----------------------------------------------------------
def test_nothing_captured_yields_a_reason_not_an_exception(cfg):
    rec = recap.summarize(cfg, "ghost")

    assert rec.summary is None
    assert rec.reason == "no-turns-and-no-sentinel"
    assert recap.recap_path(cfg, "ghost").is_file(), "a blank cell must still be explainable"


def test_a_failing_model_degrades_to_a_reason(cfg, monkeypatch, tmp_path):
    seam(monkeypatch, tmp_path, rc=3)
    write_sentinel(cfg, "P0", "ok", LONG_NOTE)

    rec = recap.summarize(cfg, "P0")

    assert rec.summary is None
    assert rec.reason == "seam-exit-3"
    assert rec.raw, "the material is kept so the owner can still read it"


def test_no_claude_binary_degrades(cfg, monkeypatch):
    monkeypatch.setattr(recap.shutil, "which", lambda name: None)
    write_sentinel(cfg, "P0", "ok", LONG_NOTE)

    rec = recap.summarize(cfg, "P0")

    assert rec.summary is None
    assert rec.reason == "no-claude-binary"


def test_a_torn_jsonl_line_is_skipped_not_fatal(cfg, monkeypatch, tmp_path):
    """The hook appends under no lock, so a half-written final line is possible."""
    seam(monkeypatch, tmp_path)
    path = recap.turns_path(cfg, "P0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"ts": 1.0, "text": "Good turn."}) + "\n" + '{"ts": 2.0, "te',
        encoding="utf-8",
    )

    assert [t["text"] for t in recap.read_turns(cfg, "P0")] == ["Good turn."]
    assert recap.summarize(cfg, "P0").summary == "Good turn."


def test_missing_turns_file_reads_as_empty(cfg):
    assert recap.read_turns(cfg, "never-ran") == []


# -- sentinel reading -----------------------------------------------------
def test_a_completed_build_outranks_a_leftover_fail(cfg):
    """Same precedence `gitq.sentinel_done` uses: an ok/needs-owner sentinel from
    a later attempt wins over a fail left by an earlier one."""
    write_sentinel(cfg, "P0", "fail", "first attempt died")
    write_sentinel(cfg, "P0", "ok", "second attempt landed it")

    assert recap.sentinel(cfg, "P0") == ("ok", "second attempt landed it")


def test_a_noteless_sentinel_still_yields_its_status(cfg):
    (cfg.done_dir / "P0.ok").write_text("P0 ok\n", encoding="utf-8")

    assert recap.sentinel(cfg, "P0") == ("ok", "")


# -- persistence + policy -------------------------------------------------
def test_completion_reuses_a_good_recap_but_on_demand_regenerates(cfg, monkeypatch, tmp_path):
    """Only two moments generate a recap, and a repeat of the first must not bill
    a second model call for a line that already exists."""
    marker = seam(monkeypatch, tmp_path, reply="First answer.")
    write_sentinel(cfg, "P0", "ok", LONG_NOTE)
    assert recap.summarize(cfg, "P0").summary == "First answer."

    marker.unlink()
    seam(monkeypatch, tmp_path, reply="Second answer.")
    assert recap.summarize(cfg, "P0").summary == "First answer."
    assert not marker.exists(), "completion re-ran the model over an existing recap"

    on_demand = recap.summarize(cfg, "P0", on_demand=True)
    assert on_demand.summary == "Second answer."
    assert on_demand.source == "on-demand"


def test_force_replaces_an_existing_recap_on_the_completion_path(cfg, monkeypatch, tmp_path):
    seam(monkeypatch, tmp_path, reply="First answer.")
    write_sentinel(cfg, "P0", "ok", LONG_NOTE)
    recap.summarize(cfg, "P0")

    seam(monkeypatch, tmp_path, reply="Replaced.")
    rec = recap.summarize(cfg, "P0", force=True)

    assert rec.summary == "Replaced."
    assert rec.source == "completion"


def test_a_failed_recap_is_retried_rather_than_reused(cfg, monkeypatch, tmp_path):
    """A stored ``summary=None`` is a blank cell waiting to be filled, not a
    result — so the next completion pass tries again."""
    seam(monkeypatch, tmp_path, rc=1)
    write_sentinel(cfg, "P0", "ok", LONG_NOTE)
    assert recap.summarize(cfg, "P0").summary is None

    seam(monkeypatch, tmp_path, reply="Recovered.")
    assert recap.summarize(cfg, "P0").summary == "Recovered."


def test_load_round_trips_and_load_all_is_newest_first(cfg, monkeypatch, tmp_path):
    seam(monkeypatch, tmp_path)
    write_sentinel(cfg, "P0", "ok", "First phase landed.")
    write_sentinel(cfg, "P1", "needs-owner", "Second phase needs a look.")
    recap.summarize(cfg, "P0")
    recap.summarize(cfg, "P1")

    p0 = recap.load(cfg, "P0")
    assert p0.summary == "First phase landed."
    assert p0.status == "ok"
    assert p0.to_dict()["source"] == "completion"

    history = recap.load_all(cfg)
    assert [r.phase for r in history] == ["P1", "P0"]


def test_load_never_raises_on_missing_or_corrupt_files(cfg):
    assert recap.load(cfg, "nope") is None

    recap.recaps_dir(cfg).mkdir(parents=True, exist_ok=True)
    recap.recap_path(cfg, "broken").write_text("{not json", encoding="utf-8")
    assert recap.load(cfg, "broken") is None
    assert recap.load_all(cfg) == []


def test_a_recap_written_by_another_version_still_loads(cfg):
    """Unknown keys drop, missing ones default — the dashboard must survive a
    file written by an older or newer build."""
    recap.recaps_dir(cfg).mkdir(parents=True, exist_ok=True)
    recap.recap_path(cfg, "P9").write_text(
        json.dumps({"phase": "P9", "summary": "ok", "future_field": 1}), encoding="utf-8"
    )

    rec = recap.load(cfg, "P9")
    assert rec.phase == "P9" and rec.summary == "ok" and rec.ts == 0.0


def test_the_stored_file_is_json_serialisable(cfg, monkeypatch, tmp_path):
    seam(monkeypatch, tmp_path)
    write_sentinel(cfg, "P0", "ok", "Landed it.")
    rec = recap.summarize(cfg, "P0")

    on_disk = json.loads(recap.recap_path(cfg, "P0").read_text(encoding="utf-8"))
    assert on_disk == rec.to_dict()
    assert set(on_disk) == {"phase", "status", "summary", "raw", "ts", "source", "reason"}


# -- the hook script ------------------------------------------------------
def test_stop_hook_appends_the_turn_text(cfg, tmp_path):
    """The hook is the whole input side of this module: if it does not write,
    every recap below it is a blank cell."""
    import subprocess

    hook = Path(__file__).resolve().parent.parent / "scripts" / "stop-hook.py"
    payload = json.dumps(
        {"session_id": "sess-1", "hook_event_name": "Stop",
         "last_assistant_message": "  Landed the gate.  "}
    )
    env = {"SWARM_PHASE": "P0", "SWARM_STATE_DIR": str(cfg.state_dir), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run([str(hook)], input=payload, env=env, capture_output=True, text=True)

    assert proc.returncode == 0
    assert proc.stdout == "", "the hook must stay out of the worker's transcript"
    assert [t["text"] for t in recap.read_turns(cfg, "P0")] == ["Landed the gate."]


def test_stop_hook_never_fails_a_turn(cfg, tmp_path):
    """Garbage in, exit 0. A hook that errors 90 minutes into a phase costs far
    more than a missing recap line."""
    import subprocess

    hook = Path(__file__).resolve().parent.parent / "scripts" / "stop-hook.py"
    base = {"SWARM_STATE_DIR": str(cfg.state_dir), "PATH": "/usr/bin:/bin"}
    for payload, env in (
        ("not json at all", {**base, "SWARM_PHASE": "P0"}),
        ("", {**base, "SWARM_PHASE": "P0"}),
        ('{"last_assistant_message": "x"}', base),  # no SWARM_PHASE: not a worker
        ('{"hook_event_name": "Stop"}', {**base, "SWARM_PHASE": "P0"}),  # no message
    ):
        proc = subprocess.run([str(hook)], input=payload, env=env, capture_output=True, text=True)
        assert proc.returncode == 0, payload

    assert recap.read_turns(cfg, "P0") == []


def test_stop_hook_truncates_a_giant_final_message(cfg):
    import subprocess

    hook = Path(__file__).resolve().parent.parent / "scripts" / "stop-hook.py"
    payload = json.dumps({"session_id": "s", "last_assistant_message": "z" * 20_000})
    env = {"SWARM_PHASE": "P0", "SWARM_STATE_DIR": str(cfg.state_dir), "PATH": "/usr/bin:/bin"}
    subprocess.run([str(hook)], input=payload, env=env, capture_output=True, text=True)

    text = recap.read_turns(cfg, "P0")[0]["text"]
    assert len(text) == 8000 and text.endswith("...[truncated]")


def test_hook_script_is_executable():
    hook = Path(__file__).resolve().parent.parent / "scripts" / "stop-hook.py"
    assert hook.is_file()
    assert shutil.which(str(hook)) is not None, "the hook must be directly executable"


def test_a_recap_uses_claude_p_even_with_an_api_key(cfg, monkeypatch, tmp_path):
    """A subscription login has no API key: a recap asks for the `haiku` alias, which
    is not an API model id, so it goes through `claude -p` whatever the env holds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    calls = []

    def api(*a, **k):
        calls.append("api")
        return None, "no"

    def cli(*a, **k):
        calls.append("cli")
        return "Built it.", None

    monkeypatch.setattr(recap, "_api_summary", api)
    monkeypatch.setattr(recap, "_cli_summary", cli)
    write_sentinel(cfg, "P0", "ok", LONG_NOTE)
    assert recap.summarize(cfg, "P0").summary == "Built it."
    assert calls == ["cli"]
