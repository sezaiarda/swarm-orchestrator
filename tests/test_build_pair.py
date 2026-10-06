"""The build gate's pairing rules (``[build].pair = "distinct-repo"``), with
real processes: two builds in different repos run together; two in one repo
never do, whichever worktree, mirror or clone of it they stand in; a build that
must run alone (an image build, ``--hold``, an unknown repo) waits for the gate
to empty and keeps it empty; a waiter the rules allow passes one they hold
back, but each waiter only ``overtake`` times and an alone one at the head
never; a script that turns out to build an image is alone from that moment;
SIGKILL of holders and waiters; idle yield beside the rules; ``pair = "any"``
and ``max_concurrent = 1`` as they were; and what the log and ``--status`` say.

Each build is a fake ``cargo`` (or ``docker``) on PATH that logs ``start <tag>``
and ``end <tag>`` around a sleep, so overlap is read back from one file.
"""

from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_build_idle import CARGO
from test_build_queue import KEYS, Gate, _descendants, _finish, _max_overlap

from swarm_orchestrator import buildclass, buildpair, buildsem, buildstatus, machine

ALONE = machine.Settings().build_alone


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "swarm@test")
    _git(path, "config", "user.name", "swarm-test")
    (path / "Cargo.toml").write_text("[package]\n")
    _git(path, "add", "Cargo.toml")
    _git(path, "commit", "-m", "init")
    return path


class PairGate(Gate):
    """A project that is a git repo with component repos ``a``, ``b`` and ``c``
    inside it, and a fake ``docker`` beside the fake ``cargo``."""

    def __init__(self, tmp: Path, max_concurrent: int = 2, overtake: int = 2,
                 pair: str = "distinct-repo", yield_s: int = 0, **side):
        super().__init__(tmp, max_concurrent=max_concurrent, overtake=overtake, **side)
        (self.proj / ".gitignore").write_text("/a\n/b\n/c\n")
        _repo(self.proj)
        self.a, self.b, self.c = (_repo(self.proj / n) for n in "abc")
        body = (self.bin / "cargo").read_text()
        (self.bin / "docker").write_text(body)
        (self.bin / "docker").chmod(0o755)
        if yield_s:  # a cargo that follows $PLAN (sleep:3 burn:2), for idle yield
            (self.bin / "cargo").write_text(CARGO.format(python=sys.executable,
                                                         log=str(self.log)))
        self.env.update(SWARM_PROJECT=str(self.proj))
        self.machine(pair=pair, idle_yield_s=yield_s)

    def at(self, where: Path, tag: str, *cmd: str, dur: float = 0.2, extra=(),
           env: dict | None = None) -> subprocess.Popen:
        args = [sys.executable, "-m", "swarm_orchestrator", "build", *extra,
                *(cmd or ("cargo", "build"))]
        e = dict(self.env, TAG=tag, DUR=str(dur), SWARM_PHASE=f"P-{tag}", **(env or {}))
        p = subprocess.Popen(args, cwd=where, env=e, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def started(self, tag: str) -> bool:
        return f"start {tag}" in self.lines()

    def ts(self, kind: str, tag: str) -> float:
        return self.wait_event(kind, f"P-{tag}", timeout=40)["ts"]

    def status(self) -> tuple[dict, str]:
        snap = buildstatus.snapshot(self.cfg())
        return snap, buildstatus.render(snap)


@pytest.fixture
def gate(tmp_path):
    made: list[PairGate] = []

    def make(**kw) -> PairGate:
        made.append(PairGate(tmp_path, **kw))
        return made[-1]

    yield make
    for g in made:
        g.cleanup()


def _overlap(lines: list[str], tags: set[str]) -> int:
    """The most builds among ``tags`` that ran at one time."""
    return _max_overlap([x for x in lines if x.split()[1] in tags])


# -- which repo ---------------------------------------------------------------
def test_a_repo_is_one_repo_from_every_worktree_mirror_and_clone(gate, tmp_path):
    g = gate()
    tool = _repo(tmp_path / "tool")
    (g.proj / ".swarm.toml").write_text(f'[lanes]\nexternal = {{ "tool" = "{tool}" }}\n')
    _git(tool, "worktree", "add", str(tmp_path / "tool-lane"), "-b", "lane/y")
    cfg = g.cfg()
    assert buildpair.repo_of(cfg, tool) == "tool" == buildpair.repo_of(cfg, tmp_path / "tool-lane")
    wt = cfg.wt_dir
    wt.mkdir(parents=True)
    _git(g.proj, "worktree", "add", str(wt / "P1"), "-b", "swarm/P1")  # a phase's mirror:
    _git(g.a, "worktree", "add", str(wt / "P1" / "a"), "-b", "swarm/P1")  # nested worktrees
    _git(g.a, "worktree", "add", str(tmp_path / "elsewhere"), "-b", "lane/x")
    subprocess.run(["git", "clone", str(g.a), str(wt / "P2" / "a")], check=True,
                   capture_output=True)  # a mirror made by cloning
    (g.a / "src").mkdir()
    outside = _repo(tmp_path / "outside")
    plain = tmp_path / "plain"
    plain.mkdir()
    for path in (g.a, g.a / "src", wt / "P1" / "a", tmp_path / "elsewhere", wt / "P2" / "a",
                 g.a / "not" / "there" / "yet"):
        assert buildpair.repo_of(cfg, path) == "a", path
    assert buildpair.repo_of(cfg, g.b) == "b"
    assert buildpair.repo_of(cfg, g.proj) == "." == buildpair.repo_of(cfg, wt / "P1")
    assert buildpair.repo_of(cfg, outside) == str(outside.resolve())
    assert buildpair.repo_of(cfg, plain) is None
    # the repos a command names count too: the project root building inside `a`
    verdict = buildclass.classify(["cargo", "test", "--manifest-path", "a/Cargo.toml"],
                                  str(g.proj))
    assert buildpair.repos(cfg, g.proj, verdict) == [str(g.proj), str(g.a)]
    script = buildclass.classify(["sh", "-c", "cd b && cargo build"], str(g.proj))
    assert buildpair.repos(cfg, g.proj, script) == [str(g.proj), str(g.b)]
    assert buildpair.repos(cfg, plain, verdict) is None
    # The rules know a repo by its place on the machine, which a mirror shares
    # with the checkout it stands for; the swarm's name is only what is shown.
    for path in (g.a, wt / "P1" / "a", tmp_path / "elsewhere", wt / "P2" / "a"):
        assert buildpair.place_of(cfg, path) == g.a, path
    assert buildpair.place_of(cfg, wt / "P1") == g.proj
    assert buildpair.name(cfg, g.a) == "a" and buildpair.name(cfg, g.proj) == "."


def test_two_builds_in_different_repos_run_together(gate):
    g = gate()
    procs = [g.at(g.a, "a", dur=1.5), g.at(g.b, "b", dur=1.5)]
    _finish(procs)
    assert all(p.returncode == 0 for p in procs)
    assert _max_overlap(g.lines()) == 2
    starts = {e["phase"]: e for e in g.events() if e["event"] == "start"}
    assert (starts["P-a"]["repo"], starts["P-b"]["repo"]) == ("a", "b")
    assert starts["P-a"]["alone"] is False and starts["P-a"]["why"] is None
    assert {starts["P-a"]["slot"], starts["P-b"]["slot"]} == {0, 1}
    assert all(set(e) == KEYS for e in g.events())
    assert "passed" not in [e["event"] for e in g.events()]


def test_two_builds_in_one_repo_never_overlap_wherever_they_stand(gate, tmp_path):
    """Three checkouts of repo ``a`` (the repo, a worktree in a phase's mirror, a
    worktree elsewhere) are one repo; a build in ``b`` runs beside them."""
    g = gate()
    wt = g.cfg().wt_dir
    wt.mkdir(parents=True)
    _git(g.proj, "worktree", "add", str(wt / "P1"), "-b", "swarm/P1")
    _git(g.a, "worktree", "add", str(wt / "P1" / "a"), "-b", "swarm/P1")
    _git(g.a, "worktree", "add", str(tmp_path / "elsewhere"), "-b", "lane/x")
    first = g.at(g.a, "a1", dur=3.0)
    g.wait_event("start", "P-a1")
    second = g.at(wt / "P1" / "a", "a2", dur=1.0)
    g.wait_event("queued", "P-a2")
    third = g.at(tmp_path / "elsewhere", "a3", dur=1.0)
    g.wait_event("queued", "P-a3")
    other = g.at(g.b, "b", dur=7.0)  # a different repo: it runs beside all three
    g.wait_event("start", "P-b")
    snap, text = g.status()
    assert not g.started("a2") and not g.started("a3")
    assert snap["pair"] == "distinct-repo" and snap["slots"][0]["repo"] == "a"
    held = {q["phase"]: q["blocked"] for q in snap["queue"]}
    assert held == {"P-a2": "same repo as slot 0 (a)", "P-a3": "same repo as slot 0 (a)"}
    assert "repo a" in text and "held back: same repo as slot 0 (a)" in text
    assert "pairing: no two builds in one repo" in text
    _finish([first, second, third, other])
    lines = g.lines()
    assert _overlap(lines, {"a1", "a2", "a3"}) == 1 and _max_overlap(lines) == 2
    assert [x for x in lines if x.startswith("start a")] == ["start a1", "start a2", "start a3"]
    assert g.ts("end", "b") > g.ts("start", "a3")  # b ran beside each of them
    assert "; held back: same repo as slot 0 (a)" in second.stderr.read()
    assert {e["repo"] for e in g.events() if e["phase"] in ("P-a1", "P-a2", "P-a3")} == {"a"}


def test_the_project_root_building_inside_a_component_is_in_that_repo(gate):
    g = gate()
    inside = g.at(g.a, "in", dur=2.5)
    g.wait_event("start", "P-in")
    root = g.at(g.proj, "root", "cargo", "test", "--manifest-path", "a/Cargo.toml", dur=0.2)
    g.wait_event("queued", "P-root")
    time.sleep(0.8)
    assert not g.started("root")
    _finish([inside, root])
    assert g.lines() == ["start in", "end in", "start root", "end root"]
    assert g.wait_event("start", "P-root")["repo"] == "."  # named after where it stands


# -- builds that run alone ----------------------------------------------------
def test_an_image_build_waits_for_the_gate_to_empty_and_keeps_it_empty(gate):
    g = gate()
    first = g.at(g.a, "a", dur=3.0)
    g.wait_event("start", "P-a")
    image = g.at(g.b, "img", "docker", "build", ".", dur=1.5)
    g.wait_event("queued", "P-img")
    late = g.at(g.c, "c", dur=0.2)  # could pair with `a`, but the alone build is first
    g.wait_event("queued", "P-c")
    time.sleep(0.6)
    snap, text = g.status()
    held = {q["phase"]: q["blocked"] for q in snap["queue"]}
    assert held["P-img"] == "waits to run alone (`docker build` is in [build].alone)"
    assert held["P-c"] is None and not g.started("img") and not g.started("c")
    assert [q["phase"] for q in snap["queue"]] == ["P-img", "P-c"]
    g.wait_event("start", "P-img")
    time.sleep(0.5)
    snap, text = g.status()
    assert snap["queue"][0]["blocked"] == "slot 0 runs alone (`docker build` is in [build].alone)"
    assert "runs alone (`docker build` is in [build].alone)" in text
    _finish([first, image, late])
    assert g.lines() == ["start a", "end a", "start img", "end img", "start c", "end c"]
    start = g.wait_event("start", "P-img")
    assert (start["alone"], start["why"], start["repo"]) == (True, "`docker build` is in [build].alone",
                                                             "b")
    assert g.wait_event("queued", "P-img")["alone"] is True
    assert "alone (`docker build` is in [build].alone)" in image.stderr.read()


def test_an_alone_build_is_not_starved_by_a_stream_of_pairs(gate):
    """While ``a`` runs, builds in other repos keep arriving, each one free to
    pair with ``a``. The image build queued before them goes first all the same."""
    g = gate(overtake=5)
    (g.b / "x.hcl").write_text("")
    first = g.at(g.a, "a", dur=4.0)
    g.wait_event("start", "P-a")
    image = g.at(g.b, "img", "docker", "buildx", "bake", "-f", "x.hcl", dur=0.5)
    g.wait_event("queued", "P-img")
    stream = []
    for n in range(6):
        stream.append(g.at(g.c if n % 2 else g.b, f"s{n}", dur=0.1))
        g.wait_event("queued", f"P-s{n}")
    _finish([first, image, *stream])
    lines = g.lines()
    assert lines[:4] == ["start a", "end a", "start img", "end img"]
    assert g.ts("start", "img") >= g.ts("end", "a") - 0.05
    assert min(g.ts("start", f"s{n}") for n in range(6)) >= g.ts("end", "img") - 0.05
    assert "passed" not in [e["event"] for e in g.events()]  # nobody went ahead of it


def test_nothing_starts_beside_a_running_image_build(gate):
    g = gate()
    (g.a / "c.yml").write_text("")
    image = g.at(g.a, "img", "sh", "-c", "docker compose -f c.yml build", dur=2.0)
    g.wait_event("start", "P-img")
    other = g.at(g.b, "b", dur=0.2)
    g.wait_event("queued", "P-b")
    time.sleep(0.7)
    assert not g.started("b")
    _finish([image, other])
    assert g.lines() == ["start img", "end img", "start b", "end b"]
    assert g.wait_event("start", "P-img")["why"] == "`docker compose` is in [build].alone"


def test_a_build_in_no_repo_runs_alone(gate, tmp_path):
    g = gate()
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "Cargo.toml").write_text("[package]\n")
    first = g.at(g.a, "a", dur=2.5)
    g.wait_event("start", "P-a")
    lost = g.at(plain, "x", dur=1.2)
    g.wait_event("queued", "P-x")
    time.sleep(0.5)
    assert not g.started("x")  # it waits for the gate to empty...
    g.wait_event("start", "P-x")
    other = g.at(g.b, "b", dur=0.2)
    _finish([first, lost, other])
    assert g.lines() == ["start a", "end a", "start x", "end x", "start b", "end b"]  # ...and
    start = g.wait_event("start", "P-x")  # nothing started beside it
    assert (start["repo"], start["alone"]) == (None, True) and "repo is unknown" in start["why"]


def test_hold_runs_alone(gate):
    g = gate()
    held = g.at(g.a, "h", "sleep", "2.5", extra=("--hold",))
    start = g.wait_event("start", "P-h")
    other = g.at(g.b, "b", dur=0.2)
    g.wait_event("queued", "P-b")
    time.sleep(0.7)
    assert not g.started("b")
    _finish([held, other])
    assert g.ts("start", "b") >= g.ts("end", "h") - 0.05
    assert (start["hold"], start["alone"], start["why"]) == (True, True, "started with --hold")


def test_a_script_that_turns_out_to_build_an_image_is_alone_from_then_on(gate):
    """Nothing in the command's text says docker. One second into the run a
    docker client appears in its process tree: from then on no build starts
    beside it, also after that client is gone. The command is told."""
    g = gate()
    script = g.a / "gate.sh"
    script.write_text('sleep 1\n"$TOOL" build .\nsleep 2.5\n')
    opaque = g.at(g.a, "img", "bash", str(script), dur=3.2, env={"TOOL": "docker"})
    start = g.wait_event("start", "P-img")
    assert (start["alone"], start["why"]) == (False, None)  # not known when it started
    found = g.wait_event("alone", "P-img")
    assert found["id"] == start["id"] and found["pid"] == start["pid"]
    assert found["why"] == "`docker build` seen running in it"
    assert 0.9 < found["ts"] - start["ts"] < 4.3 and set(found) == KEYS
    while "end img" not in g.lines():  # the docker client is gone; the script sleeps on
        time.sleep(0.05)
    other = g.at(g.b, "b", dur=0.2)
    g.wait_event("queued", "P-b")
    time.sleep(0.8)
    snap, _ = g.status()
    assert not g.started("b") and opaque.poll() is None
    assert snap["queue"][0]["blocked"] == f"slot 0 runs alone ({found['why']})"
    assert snap["slots"][0]["alone"] == found["why"]
    _finish([opaque, other])
    assert g.ts("start", "b") >= g.ts("end", "img") - 0.05
    assert len([e for e in g.events() if e["event"] == "alone"]) == 1
    assert "this build runs alone from now on (`docker build` seen running in it)" in opaque.stderr.read()
    marks = json.loads((g.sem / "pair.json").read_text())["alone"]
    assert marks[start["id"]]["why"] == found["why"]


def test_whoever_would_start_beside_a_build_looks_at_its_processes_first(gate):
    """The holder's own ``swarm build`` is not what the rule rests on: with it
    stopped before its first look, the waiter's own look finds the docker
    client."""
    g = gate()
    script = g.a / "gate.sh"
    script.write_text('"$TOOL" build .\n')
    opaque = g.at(g.a, "img", "bash", str(script), dur=2.0, env={"TOOL": "docker"})
    g.wait_event("start", "P-img")
    opaque.send_signal(signal.SIGSTOP)  # its build runs on; its own gate looks at nothing
    try:
        other = g.at(g.b, "b", dur=0.2)
        found = g.wait_event("alone", "P-img")
        deadline = time.time() + 20
        while "end img" not in g.lines() and time.time() < deadline:
            time.sleep(0.05)
    finally:
        opaque.send_signal(signal.SIGCONT)
    _finish([opaque, other])
    assert g.lines() == ["start img", "end img", "start b", "end b"]
    assert found["why"] == "`docker build` seen running in it"
    assert "no other build starts beside it" not in opaque.stderr.read()


def test_reading_which_commands_run_alone(tmp_path):
    (tmp_path / "compose.yml").write_text("")
    (tmp_path / "bake.sh").write_text("set -e\nfor s in a b; do docker buildx bake $s; done\n")
    (tmp_path / "opaque.sh").write_text('tag=$(date +%F)\ndocker buildx bake --set "*.tags=$tag"\n')
    (tmp_path / "plain.sh").write_text("tag=$(date +%F)\ncargo build # not docker\n")
    (tmp_path / "run.py").write_text("import subprocess\nsubprocess.run(['docker', 'build'])\n")
    (tmp_path / "Makefile").write_text("image:\n\tdocker build .\n")
    table = [
        (["docker", "build", "."], "`docker build` is in [build].alone"),
        (["timeout", "900", "env", "X=1", "docker", "buildx", "bake"],
         "`docker buildx bake` is in [build].alone"),
        (["flock", "/tmp/l", "docker", "compose", "build"], "`docker compose build` is in [build].alone"),
        (["docker", "compose", "-f", "compose.yml", "up", "-d"], "`docker compose` is in [build].alone"),
        (["podman", "build", "."], "`podman build` is in [build].alone"),
        (["bash", "-e", "-o", "pipefail", "bake.sh"], "`docker buildx bake` is in [build].alone"),
        (["bash", "opaque.sh"], "it names `docker` in code that could not be read"),
        (["python3", "run.py"], "it names `docker` in code that could not be read"),
        (["sh", "-c", "eval \"$CMD\"; nerdctl build ."],
         "it names `nerdctl` in code that could not be read"),
        (["sh", "-c", "cargo build && buildah bud ."], "`buildah bud` is in [build].alone"),
        (["docker", "ps"], None),
        (["docker", "buildx", "bake", "--print"], None),
        (["sh", "-c", "docker compose ps && cargo build"], None),
        (["sh", "-c", "docker compose down; cargo nextest run"], None),
        (["bash", "plain.sh"], None),
        (["cargo", "build"], None),
        (["make", "image"], None),  # not read: found when the docker client runs
        (["./deploy/tool", "build"], None),
    ]
    for argv, want in table:
        got = buildclass.classify(argv, str(tmp_path), alone=ALONE)
        assert got.alone == want, argv
        assert buildclass.classify(argv, str(tmp_path)).alone is None, argv  # the rules off
    mine = ["scripts/release.sh", "cargo build --release"]
    assert buildclass.classify(["bash", "-c", "cargo build --release -p x"], str(tmp_path),
                               alone=mine).alone == "`cargo build` is in [build].alone"
    assert buildclass.classify(["cargo", "build"], str(tmp_path), alone=mine).alone is None
    assert buildclass.classify(["docker", "build", "."], str(tmp_path), alone=mine).alone is None
    # the processes of a running build, by their command lines
    plugin = "/usr/libexec/docker/cli-plugins/docker-compose"
    for argv, want in [(["/usr/bin/docker", "buildx", "bake", "-f", "/x/y.hcl"], True),
                       ([plugin, "compose", "build"], True), ([plugin, "compose", "ps"], False),
                       (["docker", "ps"], False), (["docker-compose", "build"], True),
                       (["cargo", "build"], False), (["sleep", "5"], False), ([], False)]:
        assert bool(buildclass.proc_alone(argv, str(tmp_path), ALONE)) is want, argv


# -- the queue under the rules ------------------------------------------------
def test_a_compatible_waiter_passes_a_blocked_one_but_only_overtake_times(gate):
    g = gate(overtake=2)
    first = g.at(g.a, "a1", dur=4.0)
    start_a1 = g.wait_event("start", "P-a1")
    blocked = g.at(g.a, "a2", dur=0.2)
    q = g.wait_event("queued", "P-a2")
    one = g.at(g.b, "b1", dur=0.2)
    g.wait_event("end", "P-b1")
    two = g.at(g.c, "c1", dur=0.2)
    g.wait_event("end", "P-c1")
    three = g.at(g.b, "b2", dur=0.2)  # a2 has been passed twice: nothing more goes ahead
    g.wait_event("queued", "P-b2")
    time.sleep(1.0)
    snap, _ = g.status()
    assert not g.started("b2") and not g.started("a2") and first.poll() is None
    assert [(x["phase"], x["passed"], x["blocked"]) for x in snap["queue"]] == [
        ("P-a2", 2, "same repo as slot 0 (a)"), ("P-b2", 0, None)]
    _finish([first, blocked, one, two, three])
    starts = [x.split()[1] for x in g.lines() if x.startswith("start")]
    assert starts[:4] == ["a1", "b1", "c1", "a2"] and "b2" in starts
    assert g.ts("start", "a2") >= g.ts("end", "a1") - 0.05
    assert g.ts("start", "b2") >= g.ts("start", "a2") - 0.05
    passed = [e for e in g.events() if e["event"] == "passed"]
    by = [g.wait_event("start", p)["id"] for p in ("P-b1", "P-c1")]
    assert [(e["id"], e["by"], e["why"], e["repo"]) for e in passed] == [
        (q["id"], by[0], "same repo as slot 0 (a)", "a"),
        (q["id"], by[1], "same repo as slot 0 (a)", "a")]
    assert all(set(e) == KEYS and e["pid"] == q["pid"] and e["slot"] is None for e in passed)
    assert start_a1["slot"] == 0


def test_overtake_zero_keeps_plain_fifo_under_the_rules(gate):
    g = gate(overtake=0)
    first = g.at(g.a, "a1", dur=1.5)
    g.wait_event("start", "P-a1")
    blocked = g.at(g.a, "a2", dur=1.5)
    g.wait_event("queued", "P-a2")
    free = g.at(g.b, "b", dur=0.2)
    g.wait_event("queued", "P-b")
    time.sleep(0.7)
    assert not g.started("b")  # it could run beside a1, but it may not pass a2
    _finish([first, blocked, free])
    starts = [x.split()[1] for x in g.lines() if x.startswith("start")]
    assert starts == ["a1", "a2", "b"]
    assert _max_overlap(g.lines()) == 2  # b then ran beside a2


def _ticket(seq: int, repo: str | None, short: bool = False, alone: bool = False) -> dict:
    return {"id": f"t{seq}", "seq": seq, "pred_s": 5 if short else 900, "repo": repo,
            "repos": [repo] if repo else None, "alone": "it runs alone" if alone else None}


def test_select_steps_over_blocked_waiters_within_the_budget():
    q = [_ticket(1, "a"), _ticket(2, "a"), _ticket(3, "b"), _ticket(4, "c")]
    holders = [{"id": "h", "slot": 0, "repos": ["a"], "alone": None}]
    blocked = buildpair.blocked_all(q, holders, {})
    assert blocked == {"t1": "same repo as slot 0 (a)", "t2": "same repo as slot 0 (a)"}
    assert buildsem.select(q, {}, 2, 60, blocked)["id"] == "t3"
    assert buildsem.select(q, {"t1": 1}, 2, 60, blocked)["id"] == "t3"
    assert buildsem.select(q, {"t1": 2}, 2, 60, blocked)["id"] == "t1"  # its budget is spent
    assert buildsem.select(q, {"t2": 2}, 2, 60, blocked)["id"] == "t1"  # nobody past t2
    assert buildsem.select(q, {}, 0, 60, blocked)["id"] == "t1"  # overtake 0: FIFO
    assert buildsem.select(q, {}, 2, 60, {})["id"] == "t1"  # the holder gone: the oldest
    assert buildsem.select(q, {}, 2, 60)["id"] == "t1"  # the rules off
    # an alone waiter: stepped over while it is not the oldest, never once it is
    q = [_ticket(1, "a"), _ticket(2, "b", alone=True), _ticket(3, "c", short=True)]
    blocked = buildpair.blocked_all(q, holders, {})
    assert blocked["t2"] == "waits to run alone (it runs alone)"
    assert buildsem.select(q, {}, 2, 60, blocked)["id"] == "t3"
    assert buildsem.select(q[1:], {}, 2, 60, blocked)["id"] == "t2"
    assert buildsem.select(q[1:], {}, 2, 60, {})["id"] == "t2"  # free to start: not passed
    # beside a holder that runs alone nobody starts, and nobody is passed
    alone = [{"id": "h", "slot": 1, "repos": ["z"], "alone": "`docker build` is in [build].alone"}]
    blocked = buildpair.blocked_all(q, alone, {})
    assert set(blocked) == {"t1", "t2", "t3"}
    assert blocked["t3"] == "slot 1 runs alone (`docker build` is in [build].alone)"
    assert buildsem.select(q, {}, 2, 60, blocked)["id"] == "t1"
    # a holder found alone while running, one that says nothing of its repo, a --hold
    for holder, why in [({"id": "h", "slot": 0, "repos": ["z"]}, "seen"),
                        ({"id": "h", "slot": 0}, buildpair.OLD_HOLDER),
                        ({"slot": 0, "repos": ["z"]}, buildpair.OLD_HOLDER),
                        ({"id": "h", "slot": 0, "repos": None}, buildpair.UNKNOWN_REPO),
                        ({"id": "h", "slot": 0, "repos": ["z"], "hold": True}, buildpair.HOLD)]:
        assert buildpair.blocked(q[0], [holder], {"h": "seen"} if why == "seen" else {}) == \
            f"slot 0 runs alone ({why})"
    # a build whose command ended, with a leftover process on the seat, is not a build
    assert buildpair.counted([{"id": "h", "over": True}, {"id": "i"}]) == [{"id": "i"}]
    # a waiter from an older swarm build says nothing of its repo: it runs alone
    old = buildpair.waiters([{"id": "o", "seq": 9}])
    assert buildpair.blocked(old[0], holders, {}) == f"waits to run alone ({buildpair.UNKNOWN_REPO})"


def test_no_waiter_is_passed_more_than_overtake_times_and_the_rules_always_hold():
    """A model of the gate: random arrivals in a few repos, some alone, two
    slots. Whatever the order, no two running builds share a repo, an alone
    build runs with nothing beside it, and nobody is passed more than
    ``overtake`` times or waits for ever."""
    rng = random.Random(11)
    for trial in range(300):
        k = rng.randint(0, 3)
        cap = rng.choice((1, 2, 3))
        waiting, counts, passed, running, seq = [], {}, {}, [], 0
        started_alone_at_head = True
        for step in range(400):
            if step < 60 and rng.random() < 0.6:
                seq += 1
                waiting.append(_ticket(seq, rng.choice("abcd"), short=rng.random() < 0.3,
                                       alone=rng.random() < 0.15))
            running = [r for r in running if rng.random() > 0.4]  # some builds end
            while len(running) < cap and waiting:
                holders = [{"id": r["id"], "slot": n, "repos": r["repos"], "alone": r["alone"]}
                           for n, r in enumerate(running)]
                blocked = buildpair.blocked_all(waiting, holders, {})
                chosen = buildsem.select(waiting, counts, k, 60, blocked)
                if chosen["id"] in blocked:
                    break  # the turn is with a waiter the rules hold back: nobody starts
                head = waiting[0]
                if head["alone"] and chosen is not head:
                    started_alone_at_head = False
                for w in waiting:
                    if w["seq"] < chosen["seq"]:
                        counts[w["id"]] = counts.get(w["id"], 0) + 1
                        passed[w["id"]] = passed.get(w["id"], 0) + 1
                waiting.remove(chosen)
                running.append(chosen)
                repos = [r["repo"] for r in running]
                assert len(set(repos)) == len(repos), (trial, repos)
                assert len(running) == 1 or not any(r["alone"] for r in running), trial
        assert all(n <= k for n in passed.values()), (trial, k, passed)
        assert started_alone_at_head, trial
        assert not waiting, (trial, [w["id"] for w in waiting])  # everyone got a turn


# -- crashes ------------------------------------------------------------------
def test_a_killed_holder_and_a_killed_waiter_leave_the_rules_working(gate):
    g = gate()
    holder = g.at(g.a, "a1", dur=30)
    build_pid = g.wait_event("start", "P-a1")["pid"]
    dead = g.at(g.a, "dead", dur=0.2)
    g.wait_event("queued", "P-dead")
    waiter = g.at(g.a, "a2", dur=1.5)
    g.wait_event("queued", "P-a2")
    time.sleep(0.5)
    assert not g.started("dead") and not g.started("a2")
    dead.send_signal(signal.SIGKILL)
    for pid in [holder.pid, build_pid, *_descendants(build_pid)]:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    g.wait_event("start", "P-a2")  # the repo is free: a2 starts, the dead waiter is skipped
    same = g.at(g.a, "a3", dur=0.2)
    other = g.at(g.b, "b", dur=0.2)
    _finish([waiter, same, other])
    lines = g.lines()
    assert "start dead" not in lines and _overlap(lines, {"a2", "a3"}) == 1
    assert g.ts("start", "a3") >= g.ts("end", "a2") - 0.05  # the rule still holds...
    assert g.ts("start", "b") < g.ts("end", "a2")  # ...and so does the pairing
    end = g.wait_event("end", "P-a1")  # written by the gate, with the repo it held
    assert (end["exit"], end["repo"]) == (None, "a")
    assert list((g.sem / "queue").iterdir()) == []


def test_a_build_whose_gate_was_killed_keeps_its_repo_until_its_processes_are_gone(gate):
    """SIGKILL of ``swarm build`` alone: the build is told to stop, but the
    ``sleep`` it started runs on with the seat. Until that is gone, a second
    slot is free and the repo is not."""
    g = gate()
    holder = g.at(g.a, "a1", dur=2.5)
    start = g.wait_event("start", "P-a1")
    same = g.at(g.a, "a2", dur=0.2)
    g.wait_event("queued", "P-a2")
    os.kill(holder.pid, signal.SIGKILL)
    other = g.at(g.b, "b", dur=0.2)
    _finish([same, other])
    assert g.ts("start", "a2") - start["ts"] > 2.3  # not before the leftover ended
    assert g.ts("end", "b") < g.ts("start", "a2")  # another repo did not have to wait
    assert "end a1" not in g.lines()


def test_a_killed_image_build_frees_the_gate(gate):
    g = gate()
    image = g.at(g.a, "img", "docker", "build", ".", dur=30)
    build_pid = g.wait_event("start", "P-img")["pid"]
    waiter = g.at(g.b, "b", dur=0.2)
    g.wait_event("queued", "P-b")
    time.sleep(0.5)
    assert not g.started("b")
    for pid in [image.pid, build_pid, *_descendants(build_pid)]:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    waiter.wait(timeout=15)
    assert waiter.returncode == 0 and g.started("b")


# -- idle yield beside the rules ----------------------------------------------
def test_a_holder_set_aside_as_idle_still_holds_its_repo(gate):
    """Idle yield frees a slot, not a repo: with one slot and an idle holder in
    ``a``, a build in ``b`` starts beside it, a build in ``a`` and an image
    build wait until it has ended."""
    g = gate(max_concurrent=1, yield_s=2)
    idle = g.at(g.a, "h", env={"PLAN": "sleep:7"})
    g.wait_event("start", "P-h")
    same = g.at(g.a, "a2", env={"PLAN": "sleep:0.1"})
    g.wait_event("queued", "P-a2")
    other = g.at(g.b, "b", env={"PLAN": "burn:1"})
    g.wait_event("yield", "P-h")
    g.wait_event("start", "P-b")  # passes a2: the rules hold a2 back, not b
    image = g.at(g.c, "img", "docker", "build", ".", dur=0.2)
    g.wait_event("end", "P-b")
    time.sleep(0.6)
    snap, text = g.status()
    assert idle.poll() is None and not g.started("a2") and not g.started("img")
    assert snap["queue"][0]["blocked"] == "same repo as slot 0 (a)"
    assert "yielded: P-h" in text and "still holds its repo" in text
    _finish([idle, same, other, image])
    assert g.ts("start", "a2") >= g.ts("end", "h") - 0.05
    assert g.ts("start", "img") >= g.ts("end", "h") - 0.05
    assert g.ts("start", "b") < g.ts("end", "h")
    assert [e["by"] for e in g.events() if e["event"] == "passed"] == [
        g.wait_event("start", "P-b")["id"]]


# -- the rules off, and one slot ----------------------------------------------
def test_pair_any_is_the_gate_as_it_was(gate):
    g = gate(pair="any")
    procs = [g.at(g.a, "a1", dur=1.2), g.at(g.a, "a2", dur=1.2)]
    g.wait_event("start", "P-a1")
    g.wait_event("start", "P-a2")
    image = g.at(g.b, "img", "docker", "build", ".", dur=1.2)
    held = g.at(g.c, "h", "sleep", "1.0", extra=("--hold",))
    snap, text = g.status()
    _finish([*procs, image, held])
    lines = g.lines()
    assert _overlap(lines, {"a1", "a2"}) == 2  # one repo, side by side
    assert g.ts("start", "h") < g.ts("end", "img")  # a --hold and an image build, too
    starts = [e for e in g.events() if e["event"] == "start"]
    assert all(e["alone"] is False and e["why"] is None for e in starts)
    assert {e["repo"] for e in starts} == {"a", "b", "c"}  # the repo is logged all the same
    assert snap["pair"] == "any" and all(q["blocked"] is None for q in snap["queue"])
    assert "pairing" not in text and "repo" not in text
    assert not (g.sem / "pair.json").exists()
    assert g.cfg().build_pair == "any" and buildpair.enabled(g.cfg()) is False


def test_one_slot_is_first_come_first_served_under_the_rules_too(gate):
    g = gate(max_concurrent=1, overtake=0)
    procs = [g.at(g.a, "0", dur=1.0)]
    g.wait_event("start", "P-0")
    order = [("1", g.a), ("2", g.b), ("3", g.a), ("4", g.c), ("5", g.b)]
    for tag, where in order:
        procs.append(g.at(where, tag, dur=0.1))
        g.wait_event("queued", f"P-{tag}")
    _finish(procs)
    lines = g.lines()
    assert [x.split()[1] for x in lines if x.startswith("start")] == ["0", "1", "2", "3", "4", "5"]
    assert _max_overlap(lines) == 1
    assert "passed" not in [e["event"] for e in g.events()]


def test_a_mistyped_pair_value_stops_the_build_and_names_the_key(gate):
    """Read as ``any`` it would quietly drop rules the owner believes are in
    force; the machine file refuses it, and so does every command."""
    g = gate()
    assert g.cfg().build_pair == "distinct-repo" and buildpair.enabled(g.cfg())
    g.machine(pair="distnct")
    with pytest.raises(machine.SettingsError, match=r"\[build\].pair must be one of"):
        g.cfg()
    p = g.at(g.a, "a")
    _, err = p.communicate(timeout=20)
    assert p.returncode == 2 and "[build].pair must be one of any, distinct-repo" in err
    assert g.lines() == []  # nothing ran
    g.machine(pair="any", alone=["scripts/release.sh", "docker"])
    assert g.cfg().build_pair == "any" and not buildpair.enabled(g.cfg())
    assert g.cfg().build_alone == ["scripts/release.sh", "docker"]


def test_with_no_alone_patterns_an_image_build_pairs_like_any_other(gate):
    g = gate()
    g.machine(alone=[])
    procs = [g.at(g.a, "img", "docker", "build", ".", dur=1.2), g.at(g.b, "b", dur=1.2)]
    _finish(procs)
    assert _max_overlap(g.lines()) == 2
    assert g.wait_event("start", "P-img")["alone"] is False


# -- the swarm's own builds ---------------------------------------------------
def test_a_slot_the_swarm_holds_itself_follows_the_rules(gate):
    """``buildsem.slot`` (the landing's lane check) queues like any build: it
    carries its repo, and a build in that repo waits for it."""
    g = gate()
    with buildsem.slot(g.cfg(), "cargo test", g.a, "P-check") as held:
        same = g.at(g.a, "a", dur=0.2)
        other = g.at(g.b, "b", dur=0.2)
        g.wait_event("queued", "P-a")
        other.wait(timeout=20)
        assert not g.started("a") and g.started("b")
        held.exit = 0
    same.wait(timeout=20)
    start = g.wait_event("start", "P-check")
    assert (start["repo"], start["alone"]) == ("a", False)
    assert g.ts("start", "a") >= g.ts("end", "check") - 0.05
