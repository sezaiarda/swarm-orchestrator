"""`swarm reshape`: the one edit a session makes to an existing row.

It edits an open row's ``needs:`` or ``touches:``, lands at once like
``swarm record``, and goes through the ledger gate: a refusal leaves the ledger
byte for byte as it was and says why in the filer's history; a success notes who
changed what, and why, in the row's own history.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import cli, ledgerw
from swarm_orchestrator import ledger as ledger_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log

LEDGER = """# Ledger

## Campaign A
- [x] `a-W1` · dir:`alpha` · needs:— · **the first wave**
- [ ] `a-W2` · dir:`alpha` · needs:`a-W1` · touches:`alpha/src/**` · **the second wave** · **TAG:`v1`**
- [ ] `a-W3` · dir:`alpha` · needs:`a-W2` `b-W1` · **the third wave**

## Campaign B
- [ ] `b-W1` · dir:`beta` · needs:`a-W1` · **beta starts**
- [ ] `b-W2` · dir:`beta` · needs:— *(ordering only)* · **owner-run** · **beta by hand**
"""
KNOWN = frozenset({"alpha", "beta", ".", "@live-box"})


def _line(text: str, phase: str) -> str:
    return next(ln for ln in text.splitlines() if ln[6:].startswith(f"`{phase}`"))


# -- the pure edit -------------------------------------------------------------
def test_drop_needs_edits_only_that_row_and_says_what_changed():
    out, said = ledgerw.reshape_row(LEDGER, "a-W3", drop=["b-W1"], known=KNOWN)
    assert said == "needs −b-W1"
    assert _line(out, "a-W3") == "- [ ] `a-W3` · dir:`alpha` · needs:`a-W2` · **the third wave**"
    changed = [(a, b) for a, b in zip(LEDGER.splitlines(), out.splitlines()) if a != b]
    assert len(changed) == 1 and len(out.splitlines()) == len(LEDGER.splitlines())
    assert ledger_mod.parse(out)["a-W3"] == {"a-W2"}


def test_needs_replace_add_and_drop_compose_and_keep_a_note_in_the_field():
    out, said = ledgerw.reshape_row(LEDGER, "a-W3", needs=["b-W1"], add=["a-W1"], known=KNOWN)
    assert said == "needs −a-W2 +a-W1"
    assert ledger_mod.parse(out)["a-W3"] == {"a-W1", "b-W1"}
    out, _ = ledgerw.reshape_row(LEDGER, "b-W2", add=["b-W1"], known=KNOWN)
    assert "needs:`b-W1` *(ordering only)*" in _line(out, "b-W2")
    back, said = ledgerw.reshape_row(out, "b-W2", drop=["b-W1"], known=KNOWN)
    assert back == LEDGER and said == "needs −b-W1"
    empty, _ = ledgerw.reshape_row(LEDGER, "a-W3", needs=[], known=KNOWN)
    assert "· needs:— ·" in _line(empty, "a-W3")


def test_touches_replaces_the_field_or_adds_it_after_needs():
    out, said = ledgerw.reshape_row(LEDGER, "a-W2", touches=["alpha/src/x.rs", "@live-box"],
                                    known=KNOWN)
    assert said == "touches → `alpha/src/x.rs` `@live-box`"
    assert _line(out, "a-W2") == ("- [ ] `a-W2` · dir:`alpha` · needs:`a-W1` · touches:`alpha/src/x.rs`"
                                  " `@live-box` · **the second wave** · **TAG:`v1`**")
    out, _ = ledgerw.reshape_row(LEDGER, "b-W1", touches=["beta/**"], known=KNOWN)
    assert _line(out, "b-W1") == "- [ ] `b-W1` · dir:`beta` · needs:`a-W1` · touches:`beta/**` · **beta starts**"
    assert ledger_mod.lanes(out, KNOWN)["b-W1"]


@pytest.mark.parametrize("phase, kw, why", [
    ("a-W1", {"add": ["b-W1"]}, "is ticked"),
    ("zz-W1", {"add": ["a-W1"]}, "no ledger row zz-W1"),
    ("a-W3", {"add": ["zz-W9"]}, "names no ledger row: zz-W9"),
    ("a-W3", {"drop": ["a-W1"]}, "does not need a-W1"),
    ("a-W3", {"add": ["a-W3"]}, "cannot need itself"),
    ("b-W1", {"add": ["a-W3"]}, "cycle"),
    ("a-W3", {"add": ["a-W2"]}, "changes nothing"),
    ("a-W2", {"touches": ["alpha/src/**"]}, "changes nothing"),
    ("a-W2", {"touches": ["beta/x"]}, "outside its dir"),
    ("a-W2", {"touches": []}, "has no touch"),
    ("a-W2", {"touches": ["alpha/**/x"]}, "may only be the last segment"),
])
def test_a_reshape_is_refused(phase, kw, why):
    with pytest.raises(ledgerw.ReportError, match=why):
        ledgerw.reshape_row(LEDGER, phase, known=KNOWN, **kw)


def test_an_excluded_row_is_refused():
    with pytest.raises(ledgerw.ReportError, match="excluded"):
        ledgerw.reshape_row(LEDGER, "b-W2", add=["b-W1"], excluded=["b-W2"], known=KNOWN)


# -- through the writer, on git -------------------------------------------------
def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


def _project(tmp_path: Path, monkeypatch, gate: str = "") -> tuple:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True)
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", "-b", "master", str(project)], check=True)
    for k, v in (("user.email", "swarm@test"), ("user.name", "swarm"), ("commit.gpgsign", "false")):
        _git(project, "config", k, v)
    (project / "docs").mkdir()
    (project / "docs" / "PHASE-LEDGER.md").write_text(LEDGER)
    (project / ".swarm.toml").write_text(
        f'[tasks]\nledger = "docs/PHASE-LEDGER.md"\nledger_gate = "{gate}"\nexclude = ["b-W2"]\n'
        '[lanes]\nexternal = { alpha = "~/alpha", beta = "~/beta" }\nresources = ["live-box"]\n')
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "init")
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-q", "-u", "origin", "master")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_SLUG", "reshape")
    monkeypatch.delenv("SWARM_SESSION_ID", raising=False)
    return load(project_dir=str(project)), project, origin


def _flush(cfg) -> ledgerw.Applied:
    log = Log(cfg.supervisor_log)
    try:
        return ledgerw.flush(cfg, log, {})
    finally:
        log.close()


pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@pytestmark_git
def test_reshape_through_the_cli_lands_at_once_with_a_note(tmp_path, monkeypatch, capsys):
    cfg, project, origin = _project(tmp_path, monkeypatch)
    args = ["--project-dir", str(project)]
    assert cli.main(args + ["reshape", "a-W2", "a-W3", "--drop-needs", "b-W1",
                            "--touches", "alpha/src/**", "b-W1 is ordering only"]) == 0
    assert "reshape a-W3: queued" in capsys.readouterr().out
    got = _flush(cfg)
    assert got.touched == ["reshape a-W3"] and got.released
    text = (project / "docs" / "PHASE-LEDGER.md").read_text()
    assert _line(text, "a-W3") == ("- [ ] `a-W3` · dir:`alpha` · needs:`a-W2` · touches:`alpha/src/**`"
                                   " · **the third wave**")
    hist = ledgerw.history_text(project, cfg.history_dir, "a-W3")
    assert ("reshaped by a-W2: needs −b-W1; touches → `alpha/src/**`; why: b-W1 is ordering only"
            in hist)
    assert "· reshape · by a-W2" in hist
    assert _git(project, "status", "--porcelain") == ""
    assert "ledger: reshape a-W3" in _git(origin, "log", "-1", "--format=%s", "master")


@pytestmark_git
def test_the_cli_refuses_at_once_what_the_writer_would(tmp_path, monkeypatch, capsys):
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    args = ["--project-dir", str(project)]
    for extra, why in ((["a-W1", "--add-needs", "b-W1"], "is ticked"),
                       (["b-W2", "--add-needs", "b-W1"], "excluded"),
                       (["a-W3", "--add-needs", "nope-W1"], "names no ledger row"),
                       (["b-W1", "--add-needs", "a-W3"], "cycle"),
                       (["a-W2", "--touches", "beta/x"], "outside its dir")):
        assert cli.main(args + ["reshape", "overseer", *extra, "why"]) == 2
        assert why in capsys.readouterr().err
    assert ledgerw.pending(cfg) == {}


@pytestmark_git
def test_a_failing_gate_leaves_the_ledger_identical_and_records_the_refusal(tmp_path, monkeypatch):
    cfg, project, _origin = _project(tmp_path, monkeypatch, gate="false")
    before = (project / "docs" / "PHASE-LEDGER.md").read_bytes()
    ledgerw.file_reshape(cfg, "a-W2", "a-W3", "try it", drop=["b-W1"])
    got = _flush(cfg)
    assert got.refused and got.refused[0].startswith("reshape a-W3:")
    assert (project / "docs" / "PHASE-LEDGER.md").read_bytes() == before
    assert "reshape of `a-W3` refused" in ledgerw.history_text(project, cfg.history_dir, "a-W2")
    assert "reshaped by" not in ledgerw.history_text(project, cfg.history_dir, "a-W3")
    assert "LEDGER-REFUSED reshape a-W3" in cfg.supervisor_log.read_text()


@pytestmark_git
def test_a_reshape_the_ledger_outgrew_is_refused_by_the_writer(tmp_path, monkeypatch):
    """Checked when filed, checked again when applied: the row was ticked in between."""
    cfg, project, _origin = _project(tmp_path, monkeypatch)
    ledgerw.file_reshape(cfg, "overseer", "a-W3", "loosen", drop=["b-W1"])
    led = project / "docs" / "PHASE-LEDGER.md"
    led.write_text(ledgerw.set_state(led.read_text(), "a-W3", tick=True))
    _git(project, "commit", "-qam", "a-W3 done")
    before = led.read_bytes()
    got = _flush(cfg)
    assert got.refused and "is ticked" in got.refused[0]
    assert led.read_bytes() == before
    # "overseer" is not a row: the refusal goes to the reshaped row's history.
    assert "reshape of `a-W3` refused" in ledgerw.history_text(project, cfg.history_dir, "a-W3")
