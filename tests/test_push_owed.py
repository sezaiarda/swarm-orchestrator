"""A failed push is owed, not a hold (real git, no tmux, no claude).

A pre-push gate refusing a push used to make the supervisor hold the
whole merge queue on it and no worker launched, while the push was
also misread as a non-ff race (fetch+merge+re-run the hook, repeatedly) and
reported as "remote unreachable?". These tests pin the replacement: the hook
refusal is classified and its reason captured, the phase integrates anyway, the
repo owes a push that is retried and cleared, and the owner hears once each way.

A push the remote turns away *after* the hook passed is a third thing: not a
refusal (the hook's ``ok`` line is not its reason) and not a non-ff. It is
pushed again as it is, and owed only if the remote keeps turning it away.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from swarm_orchestrator import doctor, gitq, pushowed
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

REASON = "node_modules/orders is 0.16.0 but package.json pins 0.17.0: install the pin"
OK_LINE = "push-gate: ok - docs only, nothing to build"
TURNED_AWAY = "! [remote rejected] master -> master"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.email", "swarm@test")
    _git(repo, "config", "user.name", "swarm")
    _git(repo, "config", "commit.gpgsign", "false")


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(origin)], check=True, capture_output=True)
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-b", "master", str(project)], check=True, capture_output=True)
    _identity(project)
    (project / "PHASE-LEDGER.md").write_text("P1\nP2\nP3\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "init")
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-u", "origin", "master")
    return project, origin


def _cfg(monkeypatch, tmp_path: Path, project: Path):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_GIT_ISOLATION", "worktree")
    monkeypatch.setenv("SWARM_GIT_MAIN", "master")
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    monkeypatch.setenv("SWARM_MASTER_CMD", "true")
    monkeypatch.setenv("SWARM_SLUG", "pushowed")
    for leak in ("SWARM_WORKER_CMD", "SWARM_READY_MARKER", "SWARM_GIT_REPOS"):
        monkeypatch.delenv(leak, raising=False)
    return load(project_dir=str(project))


def _refusing_hook(repo: Path, runs: Path) -> None:
    """A pre-push hook shaped like frontend's gate: the check's reason on stdout,
    the generic refusal on stderr, exit 1. Every run is counted in ``runs``."""
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(
        "#!/bin/sh\n"
        f"echo run >> '{runs}'\n"
        "echo '== typecheck'\n"
        f"echo '{REASON}'\n"
        "echo 'push-gate: FAILED — the push was refused.' >&2\n"
        "exit 1\n"
    )
    hook.chmod(0o755)


def _passing_hook(repo: Path, origin: Path, runs: Path, locked_runs: int) -> None:
    """A pre-push hook that passes: its ``ok`` line on stdout, exit 0. During its
    first ``locked_runs`` runs something else holds the lock on origin's master,
    as another pusher would for a moment, so the remote turns the ref away."""
    lock = origin / "refs" / "heads" / "master.lock"
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(
        "#!/bin/sh\n"
        f"echo run >> '{runs}'\n"
        f"echo '{OK_LINE}'\n"
        f"if [ \"$(wc -l < '{runs}')\" -le {locked_runs} ]; then touch '{lock}';"
        f" else rm -f '{lock}'; fi\n"
        "exit 0\n"
    )
    hook.chmod(0o755)


def _drop_hook(repo: Path) -> None:
    (repo / ".git" / "hooks" / "pre-push").unlink()


def _worker(cfg, phase: str, edits: dict[str, str], log: Log) -> None:
    wt = gitq.worktree_add(cfg, phase, log)
    for rel, content in edits.items():
        (wt / rel).write_text(content)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", f"{phase} work")


def _runs(path: Path) -> int:
    return len(path.read_text().splitlines()) if path.exists() else 0


def _sent(tmp_path: Path) -> list[str]:
    sink = tmp_path / "tg.log"
    return sink.read_text().splitlines() if sink.exists() else []


# -- Fix 2: classification ------------------------------------------------
def test_non_ff_is_recognised_only_by_its_rejected_line():
    assert gitq._non_ff(" ! [rejected]        master -> master (fetch first)\nerror: failed to push some refs to 'x'")
    assert gitq._non_ff(" ! [rejected]        master -> master (non-fast-forward)\n")
    # A local hook refusal carries the same error line but no [rejected] line.
    assert not gitq._non_ff("push-gate: FAILED\nerror: failed to push some refs to 'x'")
    assert not gitq._non_ff(" ! [remote rejected] master -> master (pre-receive hook declined)")


def test_a_hook_refusal_is_not_retried_and_carries_its_reason(monkeypatch, tmp_path):
    project, _origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    runs = tmp_path / "runs"
    _refusing_hook(project, runs)
    (project / "local.txt").write_text("L")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "local")
    log = Log(cfg.supervisor_log)
    try:
        res = gitq._push_result(project, "master", log)
    finally:
        log.close()
    assert res.status == gitq.PUSH_FAILED and res.refused
    assert REASON in res.reason and "error:" not in res.reason and "== " not in res.reason
    assert len(res.reason) <= 300
    assert _runs(runs) == 1  # the hook ran once: no fetch+merge+retry rounds
    text = cfg.supervisor_log.read_text()
    assert "PUSH-REFUSED" in text and "PUSH-RETRY" not in text


def test_a_non_ff_rejection_still_reconciles(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    (project / "local.txt").write_text("L")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "local")
    ext = tmp_path / "ext"
    subprocess.run(["git", "clone", str(origin), str(ext)], check=True, capture_output=True)
    _identity(ext)
    (ext / "ext.txt").write_text("E")
    _git(ext, "add", "-A")
    _git(ext, "commit", "-m", "external")
    _git(ext, "push", "origin", "master")
    log = Log(cfg.supervisor_log)
    try:
        res = gitq._push_result(project, "master", log)
    finally:
        log.close()
    assert res.status == gitq.MERGED
    names = _git(origin, "ls-tree", "-r", "--name-only", "master")
    assert "local.txt" in names and "ext.txt" in names
    assert "PUSH-RETRY 1" in cfg.supervisor_log.read_text()


def test_an_unreachable_remote_is_a_failure_not_a_refusal(monkeypatch, tmp_path):
    project, _origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    (project / "local.txt").write_text("L")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "local")
    _git(project, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    log = Log(cfg.supervisor_log)
    try:
        res = gitq._push_result(project, "master", log)
    finally:
        log.close()
    assert res.status == gitq.PUSH_FAILED and not res.refused
    assert "Could not read from remote repository" in res.reason


def test_a_passing_hooks_output_is_never_the_reason():
    locked = f" {TURNED_AWAY} (cannot lock ref 'refs/heads/master': is at 1111111 but expected 2222222)"
    tail = "error: failed to push some refs to 'host:team/project.git'\nhint: try again\n"
    err = f"To host:team/project.git\n{locked}\n{tail}"
    assert gitq._ref_status(err)
    assert gitq._push_reason(OK_LINE + "\n", err) == locked.strip()
    # What the remote said before its verdict is part of the reason; the hook's
    # own stderr is not.
    declined = (
        "push-gate: ok\nremote: branch is protected        \nTo host:team/project.git\n"
        f" {TURNED_AWAY} (pre-receive hook declined)\n{tail}"
    )
    assert gitq._push_reason(OK_LINE + "\n", declined) == (
        f"remote: branch is protected {TURNED_AWAY} (pre-receive hook declined)"
    )
    stale = f" ! [rejected]        master -> master (stale info)\n{tail}"
    assert gitq._push_reason(OK_LINE + "\n", stale) == "! [rejected]        master -> master (stale info)"
    # A hook that refused prints no ref-status line: its stdout is the reason.
    refusal = "push-gate: FAILED — the push was refused.\n" + tail
    assert not gitq._ref_status(refusal)
    assert gitq._push_reason(f"== typecheck\n{REASON}\n", refusal) == REASON


def _local_commit(tmp_path: Path, monkeypatch):
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    (project / "local.txt").write_text("L")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "local")
    monkeypatch.setattr(gitq, "_REMOTE_RETRY_PAUSE_S", 0.0)
    return project, origin, cfg


def test_a_push_the_remote_turns_away_is_pushed_again_as_it_is(monkeypatch, tmp_path):
    project, origin, cfg = _local_commit(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    _passing_hook(project, origin, runs, locked_runs=1)
    head = _git(project, "rev-parse", "HEAD")
    log = Log(cfg.supervisor_log)
    try:
        res = gitq._push_result(project, "master", log)
    finally:
        log.close()
    assert res.status == gitq.MERGED
    assert _runs(runs) == 2
    assert _git(origin, "rev-parse", "master") == head  # the same commit: nothing was merged
    text = cfg.supervisor_log.read_text()
    assert f"PUSH-RETRY 1 master {TURNED_AWAY}" in text
    assert "PUSH-REFUSED" not in text and OK_LINE not in text


def test_a_push_the_remote_keeps_turning_away_is_owed_and_not_refused(monkeypatch, tmp_path):
    project, origin, cfg = _local_commit(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    _passing_hook(project, origin, runs, locked_runs=99)
    state_mod.init_state(cfg)
    log = Log(cfg.supervisor_log)
    try:
        res = gitq.retry_push(cfg, project, log)
        pushowed.settle(cfg, "P1", {project: res}, log)
    finally:
        log.close()
    assert res.status == gitq.PUSH_FAILED and not res.refused
    assert res.reason.startswith(TURNED_AWAY) and OK_LINE not in res.reason
    assert _runs(runs) == 1 + gitq._REMOTE_RETRIES
    rec = state_mod.read(cfg).push_owed[str(project)]
    assert not rec["refused"] and rec["reason"] == res.reason
    text = cfg.supervisor_log.read_text()
    assert f"PUSH-FAIL master {TURNED_AWAY}" in text
    assert "PUSH-REFUSED" not in text and OK_LINE not in text


@pytest.mark.parametrize("hangs", [1, 2])
def test_the_fetch_before_an_owed_push_is_short_and_tried_twice(monkeypatch, tmp_path, hangs):
    project, origin, cfg = _local_commit(tmp_path, monkeypatch)
    real, fetches = gitq._git, []

    def hanging(repo, *args, **kw):
        if args[:1] == ("fetch",):
            fetches.append(kw.get("timeout"))
            if len(fetches) <= hangs:
                raise gitq.GitError(f"git fetch origin @ {repo}: timed out after {kw.get('timeout')}s")
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitq, "_git", hanging)
    log = Log(cfg.supervisor_log)
    try:
        res = gitq.retry_push(cfg, project, log)
    finally:
        log.close()
    assert fetches == [gitq._OWED_FETCH_TIMEOUT_S] * 2
    # Both tries together cost less than one default wait.
    assert gitq._OWED_FETCH_TIMEOUT_S * 2 < gitq._GIT_TIMEOUT_S
    assert "PUSH-FETCH-RETRY master" in cfg.supervisor_log.read_text()
    pushed = "local.txt" in _git(origin, "ls-tree", "-r", "--name-only", "master")
    if hangs == 1:
        assert res.status == gitq.MERGED and pushed
    else:
        assert res.status == gitq.PUSH_FAILED and not res.refused and not pushed
        assert "git fetch origin" in res.reason and "timed out" in res.reason


# -- Fix 1: a push failure does not hold the queue -------------------------
def _seed_slots(cfg, *phases: str) -> None:
    state_mod.init_state(cfg)
    with state_mod.transaction(cfg) as st:
        st.resize(len(phases))
        for phase in phases:
            st.claim_slot(phase)


def test_a_refused_push_is_owed_and_the_queue_keeps_moving(monkeypatch, tmp_path):
    from swarm_orchestrator.supervisor import Supervisor

    monkeypatch.setenv("SWARM_TG_PINGS", "all")  # the immediate ping, as before the grace
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    runs = tmp_path / "runs"
    log = Log(cfg.supervisor_log)
    try:
        _worker(cfg, "P1", {"p1.txt": "1"}, log)
        _worker(cfg, "P2", {"p2.txt": "2"}, log)
        _worker(cfg, "P3", {"p3.txt": "3"}, log)
    finally:
        log.close()
    _refusing_hook(project, runs)
    _seed_slots(cfg, "P1", "P2", "P3")
    sup = Supervisor(cfg)
    try:
        sup._on_done("P1", "ok")
        st = state_mod.read(cfg)
        assert st.integ_blocked is None and st.integ_queue == []
        assert st.done.get("P1") == "ok"
        assert not any(s.phase == "P1" for s in st.busy_slots())  # slot freed
        assert "p1.txt" in _git(project, "ls-tree", "-r", "--name-only", "master")
        assert "p1.txt" not in _git(origin, "ls-tree", "-r", "--name-only", "master")
        rec = st.push_owed[str(project)]
        assert rec["phase"] == "P1" and rec["refused"] and REASON in rec["reason"]
        assert _runs(runs) == 1

        # The next phase integrates straight through; still one ping.
        sup._on_done("P2", "ok")
        st = state_mod.read(cfg)
        assert st.done.get("P2") == "ok" and st.integ_blocked is None
        assert st.push_owed[str(project)]["phase"] == "P1"  # first phase kept
        assert _runs(runs) == 2  # one hook run per integration, no extra retry
        owed = [m for m in _sent(tmp_path) if "pre-push check" in m]
        assert owed == [
            f"swarm: project push refused by its pre-push check after merging P1 — "
            f"{rec['reason']}. Nothing is lost: work keeps merging on this machine and"
            " the push is retried after each merge. Fix the check, or push by hand."
        ]
        assert not any("unreachable" in m for m in _sent(tmp_path))

        # The owner fixes the check; the next integration's push carries all three.
        _drop_hook(project)
        sup._on_done("P3", "ok")
        st = state_mod.read(cfg)
        assert st.push_owed == {} and st.done.get("P3") == "ok"
        names = _git(origin, "ls-tree", "-r", "--name-only", "master")
        assert {"p1.txt", "p2.txt", "p3.txt"} <= set(names.split())
        cleared = [m for m in _sent(tmp_path) if "is pushed" in m]
        assert len(cleared) == 1 and "P1" in cleared[0]
        assert "PUSH-OWED-CLEARED project" in cfg.supervisor_log.read_text()
    finally:
        sup.log.close()


def test_an_owed_push_is_retried_and_cleared_without_an_integration(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_TG_PINGS", "all")  # the immediate ping, as before the grace
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    runs = tmp_path / "runs"
    state_mod.init_state(cfg)
    (project / "local.txt").write_text("L")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "local")
    _refusing_hook(project, runs)
    log = Log(cfg.supervisor_log)
    try:
        pushowed.settle(cfg, "P1", {project: gitq.retry_push(cfg, project, log)}, log)
        assert str(project) in state_mod.read(cfg).push_owed
        pushowed.retry(cfg, log)  # still refused: refreshed, not re-pinged
        assert str(project) in state_mod.read(cfg).push_owed
        pushowed.retry(cfg, log, min_gap=3600)  # spaced out: not even attempted
        assert _runs(runs) == 2
        assert len([m for m in _sent(tmp_path) if "pre-push check" in m]) == 1

        _drop_hook(project)
        pushowed.retry(cfg, log)
        assert state_mod.read(cfg).push_owed == {}
        assert "local.txt" in _git(origin, "ls-tree", "-r", "--name-only", "master")
        assert len([m for m in _sent(tmp_path) if "is pushed" in m]) == 1
    finally:
        log.close()


def _owe(monkeypatch, tmp_path):
    project, origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    runs = tmp_path / "runs"
    state_mod.init_state(cfg)
    (project / "local.txt").write_text("L")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "local")
    _refusing_hook(project, runs)
    return project, cfg


def _ledger_rows(tmp_path) -> list[dict]:
    path = tmp_path / "state" / "notifications.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln] if path.exists() else []


def test_a_push_owed_briefly_never_reaches_the_phone(monkeypatch, tmp_path):
    """Most debts clear on the next integration; the owner needs neither ping."""
    project, cfg = _owe(monkeypatch, tmp_path)
    log = Log(cfg.supervisor_log)
    try:
        pushowed.settle(cfg, "P1", {project: gitq.retry_push(cfg, project, log)}, log)
        pushowed.retry(cfg, log)  # inside the grace: still quiet
        _drop_hook(project)
        pushowed.retry(cfg, log)
        assert state_mod.read(cfg).push_owed == {}
    finally:
        log.close()
    assert _sent(tmp_path) == []
    rows = _ledger_rows(tmp_path)
    assert [r["kind"] for r in rows] == ["push-owed", "push-owed"]
    assert all(r["suppressed"] and not r["delivered"] for r in rows)


def test_a_push_still_owed_after_the_grace_pings_once_then_its_clearing(monkeypatch, tmp_path):
    project, cfg = _owe(monkeypatch, tmp_path)
    log = Log(cfg.supervisor_log)
    try:
        pushowed.settle(cfg, "P1", {project: gitq.retry_push(cfg, project, log)}, log)
        assert _sent(tmp_path) == []
        with state_mod.transaction(cfg) as st:
            st.push_owed[str(project)]["since"] -= cfg.telegram_push_owed_grace_s + 60
        pushowed.retry(cfg, log)
        pushowed.retry(cfg, log)  # already told: once is enough
        owed = [m for m in _sent(tmp_path) if "pre-push check" in m]
        assert len(owed) == 1 and "after merging P1 (owed 6" in owed[0]
        _drop_hook(project)
        pushowed.retry(cfg, log)
    finally:
        log.close()
    assert len([m for m in _sent(tmp_path) if "is pushed" in m]) == 1


def test_a_push_made_by_hand_clears_the_debt(monkeypatch, tmp_path):
    project, _origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    state_mod.init_state(cfg)
    (project / "local.txt").write_text("L")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "local")
    runs = tmp_path / "runs"
    _refusing_hook(project, runs)
    log = Log(cfg.supervisor_log)
    try:
        pushowed.settle(cfg, "P1", {project: gitq.retry_push(cfg, project, log)}, log)
        _git(project, "push", "--no-verify", "origin", "master")  # the owner, by hand
        pushowed.retry(cfg, log)
        assert state_mod.read(cfg).push_owed == {}
        assert _runs(runs) == 1  # nothing left to push: the hook never ran again
    finally:
        log.close()


def test_a_push_made_by_hand_clears_the_debt_at_the_next_tick(monkeypatch, tmp_path):
    """The watchdog spaces its retries, and a retry is what used to notice: for
    up to that long the debt stood, with a refusal that was no longer true."""
    project, _origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    state_mod.init_state(cfg)
    (project / "local.txt").write_text("L")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "local")
    runs = tmp_path / "runs"
    _refusing_hook(project, runs)
    log = Log(cfg.supervisor_log)
    try:
        pushowed.settle(cfg, "P1", {project: gitq.retry_push(cfg, project, log)}, log)
        pushowed.retry(cfg, log, min_gap=pushowed.TICK_RETRY_S)  # not due, and still owed
        assert str(project) in state_mod.read(cfg).push_owed and not gitq.origin_has(cfg, project)
        _git(project, "push", "--no-verify", "origin", "master")  # the owner, by hand
        pushowed.retry(cfg, log, min_gap=pushowed.TICK_RETRY_S)
        assert state_mod.read(cfg).push_owed == {}
        assert _runs(runs) == 1  # read off the clone's own refs: no push, no hook
    finally:
        log.close()
    assert "PUSH-OWED-CLEARED project" in cfg.supervisor_log.read_text()


def test_an_old_push_failed_hold_still_resolves(monkeypatch, tmp_path):
    """A running older supervisor could have left ``push_failed`` as the hold."""
    from swarm_orchestrator.supervisor import Supervisor

    project, _origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    log = Log(cfg.supervisor_log)
    try:
        _worker(cfg, "P1", {"p1.txt": "1"}, log)
    finally:
        log.close()
    _refusing_hook(project, tmp_path / "runs")
    _seed_slots(cfg, "P1")
    with state_mod.transaction(cfg) as st:
        st.integ_push("P1", "ok")
        st.integ_blocked, st.integ_blocked_kind = "P1", gitq.PUSH_FAILED
    sup = Supervisor(cfg)
    try:
        sup._on_resolved("P1")
        st = state_mod.read(cfg)
        assert st.integ_blocked is None and st.integ_queue == []
        assert st.done.get("P1") == "ok" and str(project) in st.push_owed
    finally:
        sup.log.close()


# -- surfaces + compatibility ---------------------------------------------
def test_doctor_warns_with_repo_phase_age_and_reason(monkeypatch, tmp_path):
    st = state_mod.State.fresh(1)
    assert doctor._check_push_owed(st).status == doctor.OK
    st.push_owed = {
        "/w/frontend": {"phase": "pearl-W14", "reason": REASON, "since": 0.0, "refused": True}
    }
    check = doctor._check_push_owed(st)
    assert check.status == doctor.WARN
    assert "frontend" in check.detail and "pearl-W14" in check.detail and REASON in check.detail
    assert "h)" in check.detail  # an age, in hours for a debt this old
    assert "push" in check.fix_hint


def test_state_without_push_owed_loads_and_round_trips(monkeypatch, tmp_path):
    project, _origin = _make_project(tmp_path)
    cfg = _cfg(monkeypatch, tmp_path, project)
    cfg.ensure_dirs()
    old = state_mod.State.fresh(2).to_dict()
    old.pop("push_owed")
    old["done"] = {"P0": "ok"}
    cfg.state_path.write_text(json.dumps(old))
    st = state_mod.read(cfg)
    assert st.push_owed == {} and st.done == {"P0": "ok"}
    assert state_mod.State.from_dict({**old, "push_owed": None}).push_owed == {}
    # A restart keeps the debt, as it keeps `done`.
    with state_mod.transaction(cfg) as live:
        live.push_owed = {"/w/frontend": {"phase": "P1", "reason": "r", "since": 1.0}}
    assert state_mod.init_state(cfg).push_owed == {
        "/w/frontend": {"phase": "P1", "reason": "r", "since": 1.0}
    }
