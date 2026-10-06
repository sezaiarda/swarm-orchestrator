"""The owner's to-do list (``swarm todo``) and the guide that walks it (``swarm guide``).

Everything that waits on the owner and is not a question, from four records: the
owner's ledger rows, operator jobs that need his hands, to-dos a finish sent him,
and the Overseer's "Left for the owner". Questions stay in the needs-you flow.
The guide window's lifecycle runs on a fully isolated tmux server (its own
``TMUX_TMPDIR``, killed after — never the live swarm).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time

import pytest

from swarm_orchestrator import guide, opqueue, todo, tmux
from swarm_orchestrator import notes as notes_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load

LEDGER = """# Ledger

- [x] `P0` · dir:`.` · needs:— · **the base**
- [ ] `OWN` · owner-run · needs:`P0` · **try the new page by hand.**
- [ ] `A` · dir:`.` · needs:`OWN` · **build on it**
- [x] `JOB` · dir:`.` · needs:`P0` · **the shared job**
- [ ] `EVE` · owner-run · needs:`JOB` · **try the job's output by hand.**
- [ ] `STAND` · dir:`.` · needs:`P0` · **a standing target: keep the step fast.**
- [ ] `NIGHT` · dir:`.` · needs:`P0` · **what one night of usage actually costs.**
- [ ] `LATER` · owner-run · needs:`A` · **a later step.**
- [ ] `OPT` · dir:`.` · owner-optional · needs:`P0` · **the same step, optional.**
- [x] `DONEOWN` · owner-run · needs:`P0` · **picked already.**
- [ ] `QROW` · owner-run · needs:`P0` · **the owner picks an option.**
- [ ] `JUDGE` · dir:`.` · needs:`P0` · **the owner judges the result.**
- [x] `F9` · dir:`.` · needs:`P0` · **a fix that needs a deploy**
- [x] `F10` · dir:`.` · needs:`P0` · **a fix already deployed**
"""
EXCLUDE = ["OWN", "EVE", "STAND", "NIGHT", "LATER", "OPT", "DONEOWN", "QROW", "JUDGE"]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "LEDGER.md").write_text(LEDGER, encoding="utf-8")
    (project / ".swarm.toml").write_text(
        '[swarm]\ndriver = "bare"\nmax_workers = 1\n[tasks]\nledger = "LEDGER.md"\n'
        f"exclude = {json.dumps(EXCLUDE)}\n[operator]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    for leak in ("SWARM_DRIVER", "SWARM_OPERATOR", "SWARM_OPERATOR_JOB", "SWARM_GIT_ISOLATION"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


def job(cfg, phase, note, state=opqueue.QUEUED, queued_at=100.0, **kw):
    item = opqueue.Item(phase=phase, status="operator", note=note, queued_at=queued_at,
                        state=state, **kw)
    cfg.operator_dir.mkdir(parents=True, exist_ok=True)
    opqueue.item_path(cfg, phase).write_text(json.dumps(item.to_dict()), encoding="utf-8")


def ping(cfg, kind, phase, text, ts=100.0, **more):
    with (cfg.state_dir / "notifications.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "kind": kind, "phase": phase, "text": text,
                             "delivered": True, **more}) + "\n")


def overseer(cfg, name, left):
    d = cfg.state_dir / "overseer"
    d.mkdir(exist_ok=True)
    (d / f"{name}.md").write_text(f"# Overseer pass {name}\n\n## Saw\n- F9 is fine.\n\n"
                                  f"## Left for the owner\n{left}\n\n## Summary\nok\n",
                                  encoding="utf-8")


def ids(todos):
    return [t.id for t in todos.items]


def by_id(todos):
    return {t.id: t for t in todos.items}


# -- ledger rows ------------------------------------------------------------------
def test_ready_owner_rows_are_todos_and_the_rest_is_explained(cfg):
    got = todo.collect(cfg)
    assert set(ids(got)) == {"OWN", "EVE", "QROW", "JUDGE"}
    assert {u["id"]: u["waits_for"] for u in got.upcoming} == {"LATER": ["A"]}
    assert {x["id"] for x in got.left_out} == {"STAND", "NIGHT", "OPT"}
    own = by_id(got)["OWN"]
    assert own.kind == todo.OWNER_ROW and own.releases == ["A"] and own.rows_behind == 1
    assert own.close[0].startswith("swarm record OWN done")
    assert f"{cfg.project_dir / cfg.ledger}:4" in own.paths


def test_questions_and_rows_in_flight_are_not_todos(cfg):
    with state_mod.transaction(cfg) as st:
        st.waiting["QROW"] = time.time() + 60
        st.parked.append("JUDGE")
    job(cfg, "ASK", "have the owner pick a colour", state=opqueue.WAITING, question="red?")
    ping(cfg, "waiting", "QROW", "which option?")
    got = todo.collect(cfg)
    assert "QROW" not in ids(got) and "JUDGE" not in ids(got) and "ASK" not in ids(got)


def test_the_last_attempt_rides_along_with_a_row(cfg):
    (cfg.done_dir / "OWN.fail").write_text("OWN fail TO CLOSE: the owner checks the page",
                                           encoding="utf-8")
    with state_mod.transaction(cfg) as st:
        st.done["OWN"] = "fail"
    own = by_id(todo.collect(cfg))["OWN"]
    assert "the owner checks the page" in own.spec
    assert str(cfg.done_dir / "OWN.fail") in own.paths


# -- operator jobs ------------------------------------------------------------------
def test_a_queued_job_that_needs_the_owner_is_folded_into_the_row_that_needs_it(cfg):
    job(cfg, "JOB", "The deploy is live. LEFT: someone must have the owner run exit (b).")
    got = by_id(todo.collect(cfg))
    assert "JOB" not in got
    eve = got["EVE"]
    assert eve.jobs == ["JOB"] and "JOB" in eve.releases
    assert "operator job JOB" in eve.sources and "exit (b)" in eve.spec
    assert any(c.startswith("swarm operator-done JOB") for c in eve.close)


def test_owner_jobs_stand_alone_and_machine_work_is_left_to_the_operator(cfg):
    job(cfg, "F8", "Built. LEFT: Owner must verify the page on a device.")
    job(cfg, "F7", "Deploy at the next quiet hour.")
    job(cfg, "F6", "have the owner check it", state=opqueue.RUNNING)
    job(cfg, "F5", "the owner's option B is live; deploy it", triage={"why": "deploy"})
    got = by_id(todo.collect(cfg))
    assert "F7" not in got and "F6" not in got and "F5" not in got
    assert got["F8"].kind == todo.DEVICE_CHECK
    assert got["F8"].title == "Owner must verify the page on a device."


def test_a_job_the_queue_gave_up_on_is_yours_until_it_is_handed_on(cfg):
    job(cfg, "F4", "deploy it", state=opqueue.ABANDONED, queued_at=100.0)
    job(cfg, "F3", "deploy it", state=opqueue.ABANDONED, queued_at=100.0)
    job(cfg, "F3-op2", "deploy it again", queued_at=200.0)
    got = by_id(todo.collect(cfg))
    assert got["F4"].kind == todo.GIVEN_UP and "F3" not in got


def test_a_job_that_finished_with_an_ask_is_yours_until_it_is_handed_on(cfg):
    """The phone showed the ask; the outcome it could not carry is here."""
    job(cfg, "F2", "roll it", state=opqueue.DONE, done_at=100.0, attention=True,
        outcome="api NOT rolled: the script refuses this host. Tried three times.",
        ask="Roll the api by hand: the roll script refuses this host.")
    job(cfg, "F1", "roll it", state=opqueue.DONE, done_at=100.0, outcome="rolled; green")
    got = by_id(todo.collect(cfg))
    assert "F1" not in got  # nothing was asked of the owner
    assert got["F2"].kind == todo.OPERATOR_ASK
    assert got["F2"].title == "Roll the api by hand: the roll script refuses this host."
    assert got["F2"].spec.startswith("api NOT rolled: the script refuses this host.")
    notes_mod.add(cfg, "F2", "owner did it: rolled by hand", "decision")
    assert "F2" not in by_id(todo.collect(cfg))


# -- to-dos a finish sent -------------------------------------------------------------
def test_a_worker_todo_stays_until_a_later_job_or_note_handles_it(cfg):
    # The to-do is the recap kept beside the ask, not the ask that points at it.
    ping(cfg, "operator-todo", "F9", "[x] Asks you: Do the follow-up that F9 left behind.",
         ts=100.0, detail="built the page. LEFT: deploy it.")
    ping(cfg, "operator-todo", "F10", "[x] Asks you: Do the follow-up that F10 left behind.",
         ts=100.0, detail="deploy it.")
    notes_mod.add(cfg, "F10", "owner did it: deployed", "decision")
    got = by_id(todo.collect(cfg))
    assert got["F9"].kind == todo.WORKER_TODO and got["F9"].title == "deploy it."
    assert "F10" not in got
    job(cfg, "F9-op2", "deploy F9", queued_at=time.time())
    assert "F9" not in ids(todo.collect(cfg))


def test_a_retired_needs_owner_finish_is_a_todo(cfg):
    (cfg.done_dir / "F9.needs-owner").write_text("F9 needs-owner LEFT: plug in the disk.",
                                                 encoding="utf-8")
    with state_mod.transaction(cfg) as st:
        st.done["F9"] = "needs-owner"
    got = by_id(todo.collect(cfg))
    assert got["F9"].title == "plug in the disk."


# -- the Overseer ---------------------------------------------------------------------
def test_the_latest_overseer_note_dedups_promotes_and_adds_what_is_new(cfg):
    job(cfg, "JOB", "LEFT: have the owner run exit (b).")
    overseer(cfg, "20260101T000000Z", "- Renew the old thing.")
    overseer(cfg, "20260929T151454Z",
             "- Nothing new. Still owed: the JOB device checks; run the batch so NIGHT can\n"
             "  measure after one night.\n- Decide whether to renew the license.\n- Nothing.")
    got = todo.collect(cfg)
    items = by_id(got)
    assert any("Overseer" in s for s in items["EVE"].sources)
    assert items["NIGHT"].kind == todo.OWNER_ROW and items["NIGHT"].needs_time
    assert "NIGHT" not in {x["id"] for x in got.left_out}
    extra = [t for t in got.items if t.kind == todo.OVERSEER]
    assert [t.title for t in extra] == ["Decide whether to renew the license."]
    assert extra[0].paths[0].endswith("20260929T151454Z.md")


def test_left_for_owner_reads_only_its_section():
    text = "## Did\n- x\n## Left for the owner\n- one\n  more\n- two\n## Summary\n- s\n"
    assert todo.left_for_owner(text) == ["one more", "two"]


# -- order ------------------------------------------------------------------------------
def test_order_is_what_it_unblocks_then_what_needs_time(cfg):
    job(cfg, "JOB", "LEFT: have the owner run exit (b).")
    overseer(cfg, "20260929T151454Z", "- run the batch so NIGHT can measure after a night.")
    assert ids(todo.collect(cfg))[:3] == ["OWN", "NIGHT", "EVE"]


# -- the commands -------------------------------------------------------------------------
def test_swarm_todo_json_and_text(cfg, capsys):
    job(cfg, "JOB", "LEFT: have the owner run exit (b).")
    assert cli_main(["--project-dir", str(cfg.project_dir), "todo", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 4 and data["items"][0]["id"] == "OWN"
    first = data["items"][0]
    for key in ("id", "title", "kind", "releases", "sources", "spec", "paths", "close"):
        assert key in first
    assert cli_main(["--project-dir", str(cfg.project_dir), "todo"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("owner to-dos: 4 — swarm guide (or g in the TUI)")
    assert "left out" in out and "STAND" in out


def test_swarm_status_has_the_todo_line(cfg, capsys):
    assert cli_main(["--project-dir", str(cfg.project_dir), "status"]) == 0
    assert "owner to-dos: 4 — swarm guide (or g in the TUI)" in capsys.readouterr().out


def test_guide_on_the_bare_driver_says_so(cfg, capsys):
    assert cli_main(["--project-dir", str(cfg.project_dir), "guide"]) == 2
    assert "needs the tmux driver" in capsys.readouterr().err


# -- the guide window, on an isolated tmux server -------------------------------------------
@pytest.fixture
def tmux_cfg(cfg, monkeypatch):
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")
    sockdir = tempfile.mkdtemp(prefix="guideprobe-")
    monkeypatch.setenv("TMUX_TMPDIR", sockdir)
    monkeypatch.delenv("TMUX", raising=False)
    cfg.driver = "tmux"
    cfg.session = "guideprobe"
    monkeypatch.setattr(guide, "command", lambda c: "exec sleep 30")
    try:
        yield cfg
    finally:
        tmux.run(["kill-server"])
        shutil.rmtree(sockdir, ignore_errors=True)


def guide_windows(cfg) -> list[str]:
    out = tmux.run(["list-windows", "-t", f"={cfg.session}", "-F", "#{window_name}"])
    return [w for w in out.stdout.split() if w == guide.WINDOW]


def test_guide_needs_the_swarms_session(tmux_cfg):
    with pytest.raises(guide.GuideError, match="not running"):
        guide.open_window(tmux_cfg)
    tmux.new_session(tmux_cfg.session)
    tmux.mark_owner(tmux_cfg.session, "/some/other/state")
    with pytest.raises(guide.GuideError, match="not this swarm's"):
        guide.open_window(tmux_cfg)


def test_guide_opens_once_focuses_after_and_starts_fresh_when_it_ends(tmux_cfg, monkeypatch):
    cfg = tmux_cfg
    tmux.new_session(cfg.session)
    tmux.mark_owner(cfg.session, str(cfg.state_dir))
    what, win = guide.open_window(cfg)
    assert what == guide.OPENED and guide_windows(cfg) == ["guide"]
    current = tmux.run(["display-message", "-p", "-t", f"={cfg.session}:", "#{window_id}"])
    assert current.stdout.strip() == win  # the client is moved there
    again, win2 = guide.open_window(cfg)
    assert again == guide.FOCUSED and win2 == win and guide_windows(cfg) == ["guide"]
    # The session ends: the window goes with it, and the next press starts afresh.
    pane = tmux.list_panes(win)[0]
    tmux.run(["respawn-pane", "-k", "-t", pane, "true"])
    deadline = time.monotonic() + 5
    while guide_windows(cfg) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert guide_windows(cfg) == []
    third, _ = guide.open_window(cfg)
    assert third == guide.OPENED and guide_windows(cfg) == ["guide"]
    # open_guide on a live window never types a second first line.
    sent = []
    monkeypatch.setattr(guide, "deliver", lambda c, w: sent.append(w) or True)
    assert "already open" in guide.open_guide(cfg) and sent == []


def test_guide_launch_is_a_worker_built_claude_in_the_project(cfg):
    cfg.operator_model = "opus"
    cmd = guide.command(cfg)
    assert cmd.startswith(f"cd {cfg.project_dir}") and "claude --model opus -n 'swarm · guide'" in cmd
    env = guide.env(cfg)
    assert env["SWARM_STATE_DIR"] == str(cfg.state_dir) and env["SWARM_SESSION_ID"].startswith(
        "guide:")
    line = guide.pane_line(cfg)
    assert "owner_guide.md" in line and "todo --json" in line and "\n" not in line


def test_the_guide_prompt_exists_and_lints_clean():
    from swarm_orchestrator import cli, promptlint, resolver

    path = resolver.prompt_path(guide.PROMPT)
    findings = promptlint.lint(path.read_text(encoding="utf-8"),
                               known_commands=cli._known_commands())
    assert findings == []
