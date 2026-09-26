"""An operator job that ends needing the owner ends in a real, answerable question.

An operator job that finishes with
``swarm operator-done ... --attention`` may embed a call only the owner can
make ("closing the row needs a nightly read or a call on whether skipped items
count"). A Telegram line saying "operator job api-W5 needs you" is not enough:
the operator pane is reused for the next job, so the owner, going there to
answer, would be told nothing was needed, and ``swarm ask --list`` would say "no
asks": there would be no question anywhere.

Now ``--attention`` (and ``--ask "<question>"``, which implies it) opens an ask:
a window of its own, held until the job's work lands, whose one ping says what
is asked and where. Its answer is the owner's decision on the job's phase and
reaches the Overseer's digest.

Bare driver throughout; ``SWARM_ASK_CMD`` stands in for the ask session, so no
test can start a model.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarm_orchestrator import ask as ask_mod
from swarm_orchestrator import cli as cli_mod
from swarm_orchestrator import notes as notes_mod
from swarm_orchestrator import operator as operator_mod
from swarm_orchestrator import opqueue, ovdigest, procs
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor

REPO = Path(__file__).resolve().parent.parent
JOB = "api-W5"
ASK = "op-api-W5"
NOTE = "run the api-W5 closing check on the staging box and close or annotate the row"
OUTCOME = ("closing check INCONCLUSIVE; closing the row needs a nightly"
           " read or a call on whether skipped items count")
QUESTION = ("api-W5 passed 9 of 10 checks and the last needs a nightly run."
            " Count it as passed (the row closes), or wait for tomorrow's"
            " nightly run (the row stays open a day)?")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".swarm.toml").write_text(
        '[operator]\nenabled = true\n[swarm]\ndriver = "bare"\n', encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_BIN", "true")
    monkeypatch.setenv("SWARM_ASK_CMD", "exec sleep 300")
    for leak in ("SWARM_TRIAGE_CMD", "SWARM_RECAP_CMD", "SWARM_OPERATOR", "SWARM_DRIVER",
                 "SWARM_PHASE", procs.SESSION_ENV, "SWARM_GIT_ISOLATION",
                 "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(leak, raising=False)
    monkeypatch.setattr(session_mod, "REAP_GRACE_S", 0.0)
    monkeypatch.setattr(session_mod, "END_WAIT_S", 0.5)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    yield c
    rec = ask_mod.load(c, ASK)
    pids = session_mod.session_processes(c, markers=session_mod.session_markers(c, "ask", ASK))
    for pid in pids | ({rec.pid} if rec is not None and rec.pid else set()):
        try:
            os.kill(pid, 9)
        except OSError:
            pass


@pytest.fixture
def log(cfg):
    lg = Log(cfg.supervisor_log)
    yield lg
    lg.close()


def _running(cfg, log) -> opqueue.Item:
    assert opqueue.add(cfg, JOB, status="operator", note=NOTE) is not None
    assert operator_mod.dispatch(cfg, JOB, log) is True
    return opqueue.load(cfg, JOB)


def cli(cfg, *args: str):
    return subprocess.run(
        [sys.executable, "-m", "swarm_orchestrator", "--project-dir", str(cfg.project_dir), *args],
        cwd=str(cfg.project_dir), capture_output=True, text=True, timeout=60)


def tg_lines(cfg) -> list[str]:
    sink = Path(os.environ["SWARM_TG_SINK"])
    return [ln for ln in sink.read_text(encoding="utf-8").splitlines() if ln.strip()] \
        if sink.is_file() else []


def _wait(pred, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


# -- operator-done opens the question ----------------------------------------------
def test_attention_opens_an_ask_the_owner_can_answer(cfg, log):
    """The failure: `--attention` with the decision inside the outcome line."""
    _running(cfg, log)

    result = cli(cfg, "operator-done", JOB, OUTCOME, "--attention")

    assert result.returncode == 0, result.stderr
    ask = ask_mod.load(cfg, ASK)
    assert ask is not None and ask.is_open
    assert ask.rows == [JOB] and ask.by == f"operator:{JOB}"
    assert len(ask.why) <= ask_mod.WHY_MAX
    assert OUTCOME in ask.question and OUTCOME in ask.brief and NOTE in ask.brief
    assert "operator-add" in ask.brief  # the answer is handed on to something that acts
    listed = cli(cfg, "ask", "--list").stdout
    assert "no asks" not in listed and ASK in listed and "waiting on you" in listed
    # The outcome's own line is held back: the ask's ping is the one that says
    # where to answer, and "needs you" beside it would point at an ended session.
    assert not any("needs you" in ln for ln in tg_lines(cfg))
    assert f"ask:{ASK}" in result.stdout


def test_ask_puts_the_operators_own_question_and_implies_attention(cfg, log):
    _running(cfg, log)

    result = cli(cfg, "operator-done", JOB, OUTCOME, "--ask", QUESTION)

    assert result.returncode == 0, result.stderr
    item = opqueue.load(cfg, JOB)
    assert item.state == opqueue.DONE and item.attention is True and item.outcome == OUTCOME
    ask = ask_mod.load(cfg, ASK)
    assert ask.question == QUESTION and QUESTION in ask.brief
    assert ask.why == QUESTION[: ask_mod.WHY_MAX - 1].rstrip() + "…"


def test_the_ping_states_the_question_and_where_to_answer_it(cfg, log):
    _running(cfg, log)
    assert cli(cfg, "operator-done", JOB, OUTCOME, "--ask", QUESTION).returncode == 0

    assert ask_mod.open_session(cfg, ASK, log)

    [ping] = [ln for ln in tg_lines(cfg) if ASK in ln]
    sent = "\n".join(tg_lines(cfg))
    assert f"answer in tmux window ask:{ASK}" in ping and f"tmux attach -t {cfg.session}" in ping
    assert QUESTION in sent
    assert "needs you" not in sent


def test_a_routine_outcome_opens_no_ask(cfg, log):
    _running(cfg, log)
    assert cli(cfg, "operator-done", JOB, "rolled; healthz green").returncode == 0
    assert ask_mod.load_all(cfg) == []


def test_a_second_job_of_the_same_phase_gets_its_own_ask(cfg, log):
    _running(cfg, log)
    assert cli(cfg, "operator-done", JOB, OUTCOME, "--attention").returncode == 0
    extra = opqueue.add_adhoc(cfg, "the nightly read", JOB)
    operator_mod.release(cfg, log)
    assert operator_mod.dispatch(cfg, extra.phase, log) is True
    assert cli(cfg, "operator-done", extra.phase, "still short", "--ask", "Wait another night?").returncode == 0
    second = ask_mod.load(cfg, operator_mod.ask_name(extra.phase))
    assert second is not None and second.name != ASK and second.rows == [JOB]
    assert ask_mod.load(cfg, ASK).is_open


# -- the supervisor opens it once the job's work has landed -------------------------
def test_the_supervisor_holds_the_ask_until_the_job_ends_then_opens_it(cfg, log):
    _running(cfg, log)
    assert cli(cfg, "operator-done", JOB, OUTCOME, "--ask", QUESTION).returncode == 0
    sup = Supervisor(cfg)

    sup._on_ask_open(ASK, "asked")  # the CLI's poke arrives before the job's end

    assert f"ASK-HELD {ASK} until operator job {JOB} lands" in cfg.supervisor_log.read_text()
    assert not ask_mod.load(cfg, ASK).window_at

    sup._on_operator_done(JOB)  # in place: nothing to merge, so it opens now

    assert _wait(lambda: f"ASK-OPEN {ASK} ok (operator job {JOB} landed)"
                 in cfg.supervisor_log.read_text())
    assert ask_mod.session_alive(cfg, ask_mod.load(cfg, ASK))
    assert any(f"ask:{ASK}" in ln for ln in tg_lines(cfg))


def test_an_operator_ask_waits_while_its_mirror_merges(cfg, log):
    _running(cfg, log)
    assert cli(cfg, "operator-done", JOB, OUTCOME, "--attention").returncode == 0
    operator_mod.release(cfg, log)
    mirror = operator_mod.mirror_name(JOB)
    with state_mod.transaction(cfg) as st:
        st.integ_push(mirror, operator_mod.INTEG_STATUS)
    assert operator_mod.landing(cfg, state_mod.read(cfg), JOB)
    Supervisor(cfg)._on_ask_open(ASK, "asked")
    assert not ask_mod.load(cfg, ASK).window_at
    with state_mod.transaction(cfg) as st:
        st.integ_pop(mirror)
    assert not operator_mod.landing(cfg, state_mod.read(cfg), JOB)


# -- the answer reaches something that acts on it -----------------------------------
def test_the_answer_is_the_owners_decision_and_reaches_the_overseer(cfg, log, capsys):
    _running(cfg, log)
    assert cli(cfg, "operator-done", JOB, OUTCOME, "--ask", QUESTION).returncode == 0
    answer = "count it as passed; row ticked"

    assert cli_mod.cmd_ask_done(cfg, ASK, answer) == 0

    assert "owner decision" in capsys.readouterr().out
    [note] = [n for n in notes_mod.load_all(cfg).get(JOB, []) if n.kind == notes_mod.OWNER_DECISION]
    assert answer in note.text and "passed 9 of 10 checks" in note.text
    data = ovdigest.build(cfg, state_mod.read(cfg), [], since=0.0)
    [row] = data["answered"]
    assert row["name"] == ASK and row["outcome"] == answer and row["by"] == f"operator:{JOB}"
    text = ovdigest.render(data)
    assert "## Asks the owner answered since the start (1)" in text and answer in text


# -- the prompts and the brief -------------------------------------------------------
def test_the_operator_is_told_to_put_decisions_as_questions():
    flat = " ".join((REPO / "prompts" / "operator.md").read_text(encoding="utf-8").split())
    assert '--ask "<the question>"' in flat
    assert "never inside an outcome line" in flat
    item = opqueue.Item(phase=JOB, status="operator", note=NOTE)
    assert "--ask" in operator_mod.brief(load(project_dir=str(REPO)), item)


def test_the_ask_prompt_hands_operator_answers_on():
    flat = " ".join((REPO / "prompts" / "ask.md").read_text(encoding="utf-8").split())
    assert "opened by an operator job" in flat and "swarm operator-add" in flat
    flat = " ".join((REPO / "prompts" / "overseer.md").read_text(encoding="utf-8").split())
    assert "asks the owner answered" in flat
