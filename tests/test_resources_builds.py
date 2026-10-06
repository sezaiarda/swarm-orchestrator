"""resources: attributing resource use to gated builds and sessions (a fake /proc)."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from swarm_orchestrator import doctor
from swarm_orchestrator.config import load
from swarm_orchestrator.resources import builds, capacity, ptree, sampler, store, view

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "proc"
BTIME = 1790000000.0
T0 = BTIME + 1000.0  # "now" at the start of each test


class FakeProc:
    """A writable /proc: the host files from the fixture, processes made up."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        for name in ("stat", "meminfo", "loadavg", "diskstats"):
            shutil.copy(FIXTURE / name, root / name)
        shutil.copytree(FIXTURE / "pressure", root / "pressure")
        (root / "locks").write_text("")

    def add(self, pid: int, ppid: int = 1, started: float = T0 - 5, own: int = 0,
            reaped: int = 0, anon_pages: int = 1000, env: dict | None = None,
            state: str = "S", comm: str = "proc", argv: str = "", io=(0, 0)) -> None:
        d = self.root / str(pid)
        d.mkdir(exist_ok=True)
        ticks = int((started - BTIME) * ptree.TICK)
        fields = [state, ppid, pid, 1, 0, -1, 0, 0, 0, 0, 0, own, 0, reaped, 0, 20, 0, 1, 0,
                  ticks, 0, 0]
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(map(str, fields)) + "\n")
        (d / "statm").write_text(f"9000 {anon_pages + 100} 100 1 0 1 0\n")
        (d / "io").write_text(f"read_bytes: {io[0]}\nwrite_bytes: {io[1]}\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(argv.replace(" ", "\0").encode() + b"\0")
        if env is not None:
            (d / "environ").write_bytes(
                b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()))

    def cpu(self, pid: int, own: int, reaped: int = 0) -> None:
        path = self.root / str(pid) / "stat"
        head, rest = path.read_text().split(") ", 1)
        f = rest.split()
        f[11], f[13] = str(own), str(reaped)
        path.write_text(head + ") " + " ".join(f) + "\n")

    def remove(self, pid: int) -> None:
        shutil.rmtree(self.root / str(pid), ignore_errors=True)

    def avail(self, kb: int) -> None:
        text = (self.root / "meminfo").read_text()
        lines = [f"MemAvailable:   {kb} kB" if line.startswith("MemAvailable") else line
                 for line in text.splitlines()]
        (self.root / "meminfo").write_text("\n".join(lines) + "\n")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_RESOURCES", "1")
    monkeypatch.delenv("SWARM_RESOURCES_IDLE", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    cfg.buildsem_dir.mkdir(parents=True, exist_ok=True)
    fake = FakeProc(tmp_path / "proc")
    pings: list[tuple[str, str]] = []
    return cfg, fake, pings


def make_sampler(cfg, fake, pings) -> sampler.Sampler:
    s = sampler.Sampler(lambda: cfg, notify=lambda k, m: pings.append((k, m)),
                        proc_root=fake.root, wsl=False)
    s._last_dirs = float("inf")  # no du thread in these tests
    s._last_compact = float("inf")
    return s


def event(cfg, **ev) -> None:
    store.append(cfg.buildsem_dir / builds.EVENTS, ev)


def summaries(cfg) -> list[dict]:
    return store.builds(cfg.state_dir)


def ours(cfg) -> dict:
    """The swarm fields the gate writes on an event of this swarm's build."""
    return {"swarm": cfg.state_dir.name, "swarm_name": cfg.name}


#: The same for a build of another swarm on the machine.
THEIRS = {"swarm": "glasheim-1a2b", "swarm_name": "glasheim"}


def worker_env(cfg, phase="P1") -> dict:
    return {"SWARM_STATE_DIR": str(cfg.state_dir), "SWARM_SESSION_ID": f"worker:{phase}",
            "SWARM_PHASE": phase, "CARGO_BUILD_JOBS": "6"}


# -- the event log ----------------------------------------------------------------
def test_a_build_from_start_to_end_is_summarised(env):
    cfg, fake, pings = env
    fake.add(500, comm="claude", env=worker_env(cfg))
    fake.add(900, ppid=500, started=T0 - 1, comm="cargo", argv="cargo build", env=worker_env(cfg))
    event(cfg, ts=T0 - 2, event="queued", id="b1", phase="P1", pid=900, cls="heavy")
    event(cfg, ts=T0 - 1, event="start", id="b1", phase="P1", pid=900, slot=0, cls="heavy",
          argv="cargo build", cwd="/w/p1", wait_s=1.0)
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    assert s.book.busy() and s.book.active["b1"].jobs == 6
    # the build fans out: a compiler child, then the compiler is reaped into cargo
    fake.add(901, ppid=900, started=T0 + 0.5, own=150, anon_pages=50_000, comm="rustc",
             io=(4096, 1 << 20))
    fake.avail(1_000_000)
    s.step(T0 + 1)
    fake.remove(901)
    fake.cpu(900, own=20, reaped=150)
    s.step(T0 + 2)
    event(cfg, ts=T0 + 2.5, event="end", id="b1", phase="P1", pid=900, run_s=3.5, exit=0)
    fake.remove(900)
    s.step(T0 + 3)

    [row] = summaries(cfg)
    assert (row["id"], row["phase"], row["slot"], row["exit"], row["ended_by"]) == (
        "b1", "P1", 0, 0, "end")
    assert row["run_s"] == 3.5 and row["wait_s"] == 1.0 and row["jobs"] == 6
    assert row["cpu_s"] == pytest.approx(1.7)  # 150 + 20 ticks, never counted twice
    assert row["avg_cores"] == pytest.approx(1.7 / 3.5, abs=0.01)
    assert row["peak_anon_mb"] >= 50_000 * ptree.PAGE / 2**20
    assert row["min_avail_mb"] == round(1_000_000 / 1024, 1)
    assert row["wr_mb"] == 1.0
    assert not s.book.busy()


def test_a_start_whose_process_died_without_an_end_is_ended(env):
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1, comm="cargo")
    event(cfg, ts=T0 - 1, event="start", id="k1", pid=900, slot=0, cls="heavy", argv="cargo t")
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    fake.remove(900)  # SIGKILLed: no "end" ever comes
    s.step(T0 + 1)
    assert summaries(cfg) == [] and s.book.busy()  # the gate's end may still be on its way
    s.step(T0 + 1 + builds.END_GRACE_S)
    [row] = summaries(cfg)
    assert row["ended_by"] == "gone" and row["exit"] is None and row["samples"] == 1
    assert row["ended"] == T0  # when it was last seen, not when the wait ran out


def test_the_exit_code_survives_an_end_written_after_the_process_is_gone(env):
    """The gate writes ``end`` a moment after the build's process exits. A sample
    landing in between used to retire the build as "gone" and lose its exit code
    (``swarm resources`` showed ``None`` for a build that exited 100)."""
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1, comm="cargo")
    event(cfg, ts=T0 - 1, event="start", id="x1", pid=900, slot=0, cls="heavy", argv="cargo t")
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    fake.remove(900)  # the process is gone...
    s.step(T0 + 1)  # ...and this sample sees that before the gate has logged the end
    assert summaries(cfg) == []
    event(cfg, ts=T0 + 1.02, event="end", id="x1", pid=900, slot=0, cls="heavy",
          run_s=2.02, exit=100)
    s.step(T0 + 2)
    [row] = summaries(cfg)
    assert (row["ended_by"], row["exit"], row["run_s"]) == ("end", 100, 2.02)
    text = view.render(view.collect(cfg, now=T0 + 3))
    assert " 100  cargo t" in text and "None" not in text


def test_a_reused_pid_is_not_mistaken_for_the_build(env):
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1)  # started long after the event below
    event(cfg, ts=T0 - 600, event="start", id="old", pid=900, slot=0, cls="heavy")
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    s.step(T0 + builds.END_GRACE_S)
    [row] = summaries(cfg)
    assert row["ended_by"] == "gone" and row["samples"] == 0


def test_light_builds_and_the_queue(env):
    cfg, fake, pings = env
    fake.add(900)
    event(cfg, ts=T0 - 3, event="queued", id="q1", pid=901, cls="heavy")
    event(cfg, ts=T0 - 3, event="queued", id="q2", pid=902, cls="heavy")
    event(cfg, ts=T0 - 3, event="queued", id="q3", pid=903, cls="heavy")
    event(cfg, ts=T0 - 2, event="bypass", id="q2", pid=902, cls="light")
    event(cfg, ts=T0 - 2, event="left", id="q3", pid=903, cls="heavy", wait_s=1.0)
    event(cfg, ts=T0 - 1, event="start", id="l1", pid=900, cls="light")
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    assert not s.book.busy()
    assert list(s.book.queued) == ["q1"]  # the one that gave up waiting is gone too
    assert store.read_now(cfg.state_dir)["queued"] == 1


def test_a_gc_holding_the_gate_is_not_a_build(env):
    """gc logs its turn at the gate like a build's, with ``cls`` ``gc``. It is
    neither measured as a build nor counted as one waiting."""
    cfg, fake, pings = env
    fake.add(500, comm="swarm", own=900)  # the supervisor: gc runs inside it
    gc = {"pid": 500, "cls": "gc", "argv": "swarm gc", "slot": None, "phase": None}
    event(cfg, ts=T0 - 9, event="queued", id="g1", **gc)
    event(cfg, ts=T0 - 3, event="start", id="g1", wait_s=6.0, **gc)
    event(cfg, ts=T0 - 2, event="queued", id="g2", **gc)  # a second one, still waiting
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    assert not s.book.busy() and not s.book.queued
    now = store.read_now(cfg.state_dir)
    assert (now["builds"], now["queued"], now["idle_holders"]) == ([], 0, [])
    event(cfg, ts=T0 + 0.5, event="end", id="g1", run_s=3.5, exit=0, **gc)
    event(cfg, ts=T0 + 0.6, event="left", id="g2", wait_s=2.6, **gc)
    s.step(T0 + 1)
    assert summaries(cfg) == [] and not s.book.queued and not pings


def test_without_an_event_log_a_slot_gc_holds_is_not_a_build(env):
    cfg, fake, pings = env
    keys = []
    for name in ("slot0", "slot1", "gc"):
        path = cfg.buildsem_dir / name
        path.touch()
        st = path.stat()
        keys.append(f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}")
    fake.add(700, started=T0 - 30, comm="swarm", argv="swarm supervise")
    held = [f"{n}: FLOCK  ADVISORY  WRITE 700 {key} 0 EOF\n" for n, key in enumerate(keys, 1)]
    (fake.root / "locks").write_text("".join(held))  # gc: every slot, and its own record
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    assert s.book.source == "flock" and not s.book.busy()
    (fake.root / "locks").write_text(held[0])  # a slot held with gc's record free: a build
    s.step(T0 + 1)
    assert [b.slot for b in s.book.active.values()] == [0]


def test_a_restarted_sampler_does_not_summarise_a_build_twice(env):
    cfg, fake, pings = env
    event(cfg, ts=T0 - 9, event="start", id="b1", pid=900, cls="heavy")
    event(cfg, ts=T0 - 8, event="end", id="b1", pid=900, run_s=1.0, exit=0)
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    s.step(T0 + 1)
    assert [r["id"] for r in summaries(cfg)] == ["b1"]
    again = make_sampler(cfg, fake, pings)
    again.step(T0 + 2)
    again.step(T0 + 3)
    assert [r["id"] for r in summaries(cfg)] == ["b1"]


def test_the_event_tail_keeps_a_partial_line_for_later(tmp_path):
    path = tmp_path / "events.jsonl"
    tail = builds.EventTail(path)
    path.write_text('{"event":"start","id":"a"}\n{"event":"end",')
    assert [e["id"] for e in tail.poll()] == ["a"]
    with path.open("a") as fh:
        fh.write('"id":"a"}\nnot json\n')
    assert [e["event"] for e in tail.poll()] == ["end"]
    path.write_text('{"event":"queued","id":"b"}\n')  # rotated: smaller than the offset
    assert [e["id"] for e in tail.poll()] == ["b"]


# -- the flock fallback -------------------------------------------------------------
def test_without_an_event_log_the_slot_holder_is_the_build(env):
    cfg, fake, pings = env
    slot = cfg.buildsem_dir / "slot0"
    slot.touch()
    st = slot.stat()
    fake.add(700, started=T0 - 30, comm="cargo", argv="cargo nextest run",
             env=worker_env(cfg, "P7"))
    lock = f"1: FLOCK  ADVISORY  WRITE 700 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n"
    (fake.root / "locks").write_text(lock)
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    assert s.book.source == "flock"
    [b] = s.book.active.values()
    assert (b.pid, b.slot, b.phase, b.argv, b.partial) == (700, 0, "P7", "cargo nextest run", True)
    # a lock names no swarm: the build is nobody's by name, and counts as this swarm's
    assert (b.swarm, b.swarm_name, b.mine) == (None, None, True)
    assert b.started == pytest.approx(T0 - 30, abs=0.1)  # already running: from its start
    fake.cpu(700, own=300)
    s.step(T0 + 1)
    (fake.root / "locks").write_text("")  # the slot is released
    s.step(T0 + 2)
    [row] = summaries(cfg)
    assert row["source"] == "flock" and row["ended_by"] == "released"
    assert row["cpu_s"] == pytest.approx(3.0)


def test_a_holder_seen_starting_counts_from_first_sight(env):
    cfg, fake, pings = env
    slot = cfg.buildsem_dir / "slot1"
    slot.touch()
    st = slot.stat()
    s = make_sampler(cfg, fake, pings)
    s.step(T0)  # the first look: nothing held
    fake.add(701, started=T0 + 0.5)
    (fake.root / "locks").write_text(
        f"1: FLOCK ADVISORY WRITE 701 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n")
    s.step(T0 + 1)
    [b] = s.book.active.values()
    assert b.started == T0 + 1 and not b.partial and b.slot == 1


# -- whose build it is ----------------------------------------------------------------
def test_a_neighbours_build_is_measured_and_marked_not_mine(env):
    """The gate is the machine's: its log holds every swarm's builds. All are
    measured, each says whose it is, and the views name the other swarm."""
    cfg, fake, pings = env
    fake.add(500, anon_pages=10_000, comm="claude", env=worker_env(cfg))
    fake.add(900, ppid=500, started=T0 - 1, anon_pages=20_000, comm="cargo")
    fake.add(950, started=T0 - 1, anon_pages=40_000, comm="cargo")  # another swarm's session
    base = {"cls": "heavy", "cwd": "/w"}
    event(cfg, ts=T0 - 1, event="start", id="own", phase="P1", pid=900, slot=0,
          argv="cargo build", **base, **ours(cfg))
    event(cfg, ts=T0 - 1, event="start", id="far", phase="W7", pid=950, slot=1,
          argv="cargo test", **base, **THEIRS)
    event(cfg, ts=T0 - 1, event="queued", id="q-own", pid=901, **base, **ours(cfg))
    event(cfg, ts=T0 - 1, event="queued", id="q-far", pid=951, **base, **THEIRS)
    event(cfg, ts=T0 - 1, event="queued", id="q-far2", pid=952, **base, **THEIRS)
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    fake.cpu(950, own=300)
    row = s.step(T0 + 1)

    assert row["nb"] == 2 and set(row["b"]) == {"own", "far"} and row["bo"] == ["far"]
    assert row["b"]["far"] == [pytest.approx(3.0), round(40_000 * ptree.PAGE / 2**20, 1)]
    assert set(row["w"]) == {"worker:P1"}  # the sessions are this swarm's alone
    # "everything else" holds no swarm's build: the neighbour's is taken out with ours
    spent = sum(v[1] for v in row["b"].values()) + row["w"]["worker:P1"][1]
    other = capacity.session_stats([row])["other_anon_mb"]["p95"]
    assert other == pytest.approx(row["anon_mb"] - spent)
    assert other <= row["anon_mb"] - row["b"]["far"][1]
    [minute] = store.aggregate([row])
    assert minute["bo"] == ["far"] and set(minute["b"]) == {"own", "far"}

    snap = store.read_now(cfg.state_dir)
    by = {b["id"]: b for b in snap["builds"]}
    assert (by["own"]["swarm"], by["own"]["swarm_name"], by["own"]["mine"]) == (
        cfg.state_dir.name, cfg.name, True)
    assert (by["far"]["swarm"], by["far"]["swarm_name"], by["far"]["mine"]) == (
        "glasheim-1a2b", "glasheim", False)
    assert (snap["queued"], snap["queued_mine"]) == (3, 1)
    text = view.render(view.collect(cfg, now=T0 + 2))
    assert "builds 2 running on the machine's gate (1 of other swarms), 3 queued (1 this swarm's)" in text
    assert "slot 1 pid 950 [glasheim] W7" in text and "slot 0 pid 900 P1" in text

    for bid, pid in (("own", 900), ("far", 950)):
        event(cfg, ts=T0 + 1.5, event="end", id=bid, pid=pid, run_s=2.5, exit=0)
        fake.remove(pid)
    s.step(T0 + 2)
    rows = {r["id"]: r for r in summaries(cfg)}
    assert (rows["own"]["swarm"], rows["own"]["swarm_name"], rows["own"]["mine"]) == (
        cfg.state_dir.name, cfg.name, True)
    assert (rows["far"]["swarm"], rows["far"]["swarm_name"], rows["far"]["mine"]) == (
        "glasheim-1a2b", "glasheim", False)
    assert rows["far"]["samples"] == 2 and rows["far"]["cpu_s"] == pytest.approx(3.0)
    done = view.collect(cfg, now=T0 + 3)
    assert done["builds"]["others"] == 1 and done["capacity"]["builds"]["others"] == 1
    text = view.render(done)
    assert "BUILDS (2 finished in the window on the machine's gate, 1 of other swarms" in text
    listed = [line for line in text.splitlines() if line.endswith("cargo test")]
    assert "[glasheim] W7" in listed[0]
    assert "2 measured on the machine's gate, 1 of them other swarms'" in text
    assert any("1 of the 2 measured build(s) were other swarms'" in n
               for n in done["capacity"]["notes"])


def test_an_event_that_names_no_swarm_counts_as_this_swarms(env):
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1)
    event(cfg, ts=T0 - 1, event="start", id="b", pid=900, slot=0, cls="heavy",
          swarm=None, swarm_name=None)
    s = make_sampler(cfg, fake, pings)
    row = s.step(T0)
    [b] = s.book.active.values()
    assert (b.swarm, b.swarm_name, b.mine) == (None, None, True) and "bo" not in row


def test_only_this_swarms_idle_build_is_reported(env):
    """A neighbour's idle build is its own supervisor's to report: this one
    measures it and shows it idle, and neither pings nor warns about it."""
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1, own=100, argv="cargo test")
    fake.add(950, started=T0 - 1, own=100, argv="bash wait.sh")
    event(cfg, ts=T0 - 1, event="start", id="own", phase="P1", pid=900, slot=0, cls="heavy",
          argv="cargo test", **ours(cfg))
    event(cfg, ts=T0 - 1, event="start", id="far", phase="W7", pid=950, slot=1, cls="heavy",
          argv="bash wait.sh", **THEIRS)
    s = make_sampler(cfg, fake, pings)
    for t in (0, 300, 610):
        s.step(T0 + t)
    assert [k for k, _ in pings] == ["idle-build:own"]
    snap = store.read_now(cfg.state_dir)
    assert [b["id"] for b in snap["idle_holders"]] == ["own"]
    assert {b["id"]: (b["idle"], b["mine"]) for b in snap["builds"]} == {
        "own": (True, True), "far": (True, False)}
    lines = [x for x in view.status_lines(cfg, now=T0 + 611) if x.startswith("IDLE BUILD")]
    assert len(lines) == 1 and "pid 900" in lines[0]
    rows = {r[0]: r for r in view.doctor_checks(cfg, supervisor_alive=True, now=T0 + 611)}
    assert "pid 900" in rows["resources.idle-build"][2]
    assert "pid 950" not in rows["resources.idle-build"][2]
    s.step(T0 + 620 + sampler.IDLE_REPING_S)
    assert [k for k, _ in pings] == ["idle-build:own"] * 2


def test_a_neighbours_idle_build_alone_warns_nobody_here(env):
    cfg, fake, pings = env
    fake.add(950, started=T0 - 1, own=100, argv="bash wait.sh")
    event(cfg, ts=T0 - 1, event="start", id="far", phase="W7", pid=950, slot=0, cls="heavy",
          argv="bash wait.sh", **THEIRS)
    s = make_sampler(cfg, fake, pings)
    for t in (0, 300, 610):
        s.step(T0 + t)
    assert pings == [] and store.read_now(cfg.state_dir)["idle_holders"] == []
    assert not any(x.startswith("IDLE BUILD") for x in view.status_lines(cfg, now=T0 + 611))
    rows = {r[0]: r for r in view.doctor_checks(cfg, supervisor_alive=True, now=T0 + 611)}
    assert rows["resources.idle-build"][1] == doctor.OK
    assert s.book.active["far"].idle_flagged  # the measurement still says it sat idle


# -- idle holders -------------------------------------------------------------------
def test_an_idle_holder_is_reported_once_an_hour_and_never_killed(env):
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1, own=100, comm="cargo", argv="cargo test --workspace",
             env=worker_env(cfg, "P3"))
    event(cfg, ts=T0 - 1, event="start", id="i1", pid=900, slot=0, cls="heavy",
          argv="cargo test --workspace")
    s = make_sampler(cfg, fake, pings)
    for t in range(0, 600, 15):
        s.step(T0 + t)
    assert pings == []  # not yet watched idle for ten minutes
    s.step(T0 + 620)
    assert len(pings) == 1 and pings[0][0] == "idle-build:i1"
    assert "P3" in pings[0][1] and "Nothing was stopped" in pings[0][1]
    s.step(T0 + 700)
    assert len(pings) == 1  # rate-limited
    snap = store.read_now(cfg.state_dir)
    assert [b["id"] for b in snap["idle_holders"]] == ["i1"]
    assert (fake.root / "900").exists()  # nothing was signalled
    s.step(T0 + 620 + sampler.IDLE_REPING_S)
    assert len(pings) == 2


def _holder_record(cfg, slot: int, **rec) -> None:
    (cfg.buildsem_dir / f"slot{slot}").write_text(json.dumps({"v": 1, "ended": None, **rec}))


def test_an_idle_holder_is_confirmed_by_the_gates_own_record(env):
    """The gate's holder record decides: another build's id there means the
    sampler missed an end, so no warning; a matching one lends its words."""
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1, own=100, argv="sleep 9999")
    fake.add(901, started=T0 - 1, own=100, argv="sleep 9999")
    event(cfg, ts=T0 - 1, event="start", id="stale", pid=900, slot=0, cls="heavy",
          argv="old cmd")
    event(cfg, ts=T0 - 1, event="start", id="mine", pid=901, slot=1, cls="heavy",
          argv="cmd")
    _holder_record(cfg, 0, id="newer", phase="P9", argv="cargo build")
    _holder_record(cfg, 1, id="mine", phase="P2", argv="cargo test --workspace")
    s = make_sampler(cfg, fake, pings)
    for t in (0, 300, 610):
        s.step(T0 + t)
    assert [k for k, _ in pings] == ["idle-build:mine"]
    assert "P2" in pings[0][1] and "cargo test" in pings[0][1]
    assert "swarm build --status" in pings[0][1]
    snap = store.read_now(cfg.state_dir)
    assert [b["id"] for b in snap["idle_holders"]] == ["mine"]
    lines = view.status_lines(cfg, now=T0 + 611)
    assert sum(line.startswith("IDLE BUILD HOLDER") for line in lines) == 1


def test_a_busy_build_is_not_idle(env):
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1)
    event(cfg, ts=T0 - 1, event="start", id="b", pid=900, slot=0, cls="heavy")
    s = make_sampler(cfg, fake, pings)
    for i, t in enumerate(range(0, 700, 10)):
        fake.cpu(900, own=i * 20)  # 2 cores
        s.step(T0 + t)
    assert pings == [] and not store.read_now(cfg.state_dir)["idle_holders"]


def test_idle_needs_the_whole_window_watched():
    b = builds.Build(id="x", pid=1, started=0.0, source="events")
    use = ptree.Usage()
    b.observe(500.0, use, None, True, 600.0)  # sampler started late: first sight at 500 s
    b.observe(700.0, use, None, True, 600.0)
    assert not b.idle_for(700.0, 600.0)
    b.observe(1101.0, use, None, True, 600.0)
    assert b.idle_for(1101.0, 600.0)


def test_status_and_doctor_surface_an_idle_holder(env, monkeypatch):
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1, argv="cargo build")
    event(cfg, ts=T0 - 1, event="start", id="i1", pid=900, slot=0, cls="heavy", argv="cargo build")
    s = make_sampler(cfg, fake, pings)
    for t in (0, 300, 610):
        s.step(T0 + t)
    lines = view.status_lines(cfg, now=T0 + 611)
    assert any(line.startswith("IDLE BUILD HOLDER: pid 900") for line in lines)
    rows = {r[0]: r for r in view.doctor_checks(cfg, supervisor_alive=True, now=T0 + 611)}
    assert rows["resources.idle-build"][1] == doctor.WARN
    assert rows["resources.sampler"][1] == doctor.OK
    stale = {r[0]: r for r in view.doctor_checks(cfg, supervisor_alive=True, now=T0 + 3600)}
    assert stale["resources.sampler"][1] == doctor.WARN
    assert stale["resources.idle-build"][1] == doctor.OK  # a stale snapshot proves nothing


def _seat_record(cfg, seat: int, **rec) -> None:
    (cfg.buildsem_dir / f"seat{seat}").write_text(
        json.dumps({"v": 1, "ended": None, "seat": seat, **rec}))


def test_a_yielded_build_is_summarised_and_its_warning_says_the_slot_was_released(env):
    """The gate set the idle holder aside (``yield`` in its log). The warning
    still comes, but it says the slot was released; the summary row carries how
    long the build was set aside, across a wake-up, and the table shows it."""
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1, own=100, argv="bash wait.sh", env=worker_env(cfg, "P4"))
    fake.add(901, started=T0 + 200, own=100, argv="cargo test", env=worker_env(cfg, "P5"))
    base = {"cls": "heavy", "slot": 0}
    event(cfg, ts=T0 - 1, event="start", id="idle", pid=900, argv="bash wait.sh", **base)
    _seat_record(cfg, 0, id="idle", slot=0, phase="P4", argv="bash wait.sh")
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    event(cfg, ts=T0 + 150, event="yield", id="idle", pid=900, idle_s=150.0, **base)
    event(cfg, ts=T0 + 200, event="start", id="beside", pid=901, argv="cargo test", **base)
    # the slot file now names the build that started beside it; its seat still names it
    _holder_record(cfg, 0, id="beside", phase="P5", argv="cargo test", seat=1)
    _seat_record(cfg, 1, id="beside", slot=0, phase="P5", argv="cargo test")
    for t in (210, 400, 620):
        fake.cpu(901, own=100 + t * 100)
        s.step(T0 + t)
    assert [k for k, _ in pings] == ["idle-build:idle"]
    assert "released its slot 7 min ago" in pings[0][1] and "start beside it" in pings[0][1]
    assert "queue behind it" not in pings[0][1] and "Nothing was stopped" in pings[0][1]
    snap = store.read_now(cfg.state_dir)
    [row] = snap["idle_holders"]
    assert row["id"] == "idle" and row["yielded"] and row["yielded_s"] == 470.0
    [line] = [x for x in view.status_lines(cfg, now=T0 + 621) if x.startswith("IDLE BUILD")]
    assert "slot released: builds start beside it" in line
    rows = {r[0]: r for r in view.doctor_checks(cfg, supervisor_alive=True, now=T0 + 621)}
    assert rows["resources.idle-build"][1] == doctor.WARN
    assert "slot released" in rows["resources.idle-build"][2]
    assert "YIELDED 8m (slot released)" in view.render(view.collect(cfg, now=T0 + 621))

    # it wakes for a while, is set aside again, and ends while set aside
    event(cfg, ts=T0 + 700, event="unyield", id="idle", pid=900, idle_s=550.0, **base)
    event(cfg, ts=T0 + 900, event="yield", id="idle", pid=900, idle_s=150.0, **base)
    event(cfg, ts=T0 + 1000, event="end", id="idle", pid=900, run_s=1001.0, exit=0, **base)
    event(cfg, ts=T0 + 1000, event="end", id="beside", pid=901, run_s=800.0, exit=0, **base)
    fake.remove(900)
    fake.remove(901)
    s.step(T0 + 1001)
    by = {r["id"]: r for r in summaries(cfg)}
    assert by["idle"]["yielded_s"] == 650.0  # 150..700 and 900..1000
    assert by["beside"]["yielded_s"] == 0.0
    text = view.render(view.collect(cfg, now=T0 + 1002))
    assert "yielded: 1 build(s) sat idle and were set aside by the gate, 11m in all" in text
    head = next(line for line in text.splitlines() if "anon pk" in line)
    listed = [line for line in text.splitlines() if line.endswith("bash wait.sh")]
    assert head.index("yielded") <= listed[0].index("11m") < head.index("cpu s")


def test_the_warning_for_a_holder_that_never_yields_says_why(env):
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1, own=100, argv="docker build .")
    event(cfg, ts=T0 - 1, event="start", id="d1", pid=900, slot=0, cls="heavy",
          argv="docker build .")
    _seat_record(cfg, 0, id="d1", slot=0, phase="P6", argv="docker build .",
                 noyield="docker hands its work to a daemon")
    s = make_sampler(cfg, fake, pings)
    for t in (0, 300, 610):
        s.step(T0 + t)
    assert "it never yields its slot (docker hands its work to a daemon)" in pings[0][1]
    [line] = [x for x in view.status_lines(cfg, now=T0 + 611) if x.startswith("IDLE BUILD")]
    assert "slot kept (docker hands its work to a daemon)" in line


def test_a_seat_that_went_to_another_build_means_the_end_was_missed(env):
    cfg, fake, pings = env
    fake.add(900, started=T0 - 1, own=100, argv="sleep 9999")
    event(cfg, ts=T0 - 1, event="start", id="old", pid=900, slot=0, cls="heavy", argv="x")
    _seat_record(cfg, 0, id="newer", slot=0, phase="P9", argv="cargo build")
    _holder_record(cfg, 0, id="newer", phase="P9", argv="cargo build", seat=0)
    s = make_sampler(cfg, fake, pings)
    for t in (0, 300, 610):
        s.step(T0 + t)
    assert pings == [] and not store.read_now(cfg.state_dir)["idle_holders"]


# -- sessions -------------------------------------------------------------------------
def test_a_worker_is_measured_without_its_builds(env):
    cfg, fake, pings = env
    fake.add(500, own=0, anon_pages=10_000, comm="claude", env=worker_env(cfg))
    fake.add(900, ppid=500, started=T0 - 1, anon_pages=90_000, comm="cargo")
    fake.add(600, env={"SWARM_STATE_DIR": str(cfg.state_dir)}, comm="swarm")
    event(cfg, ts=T0 - 1, event="start", id="b", pid=900, cls="heavy")
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    fake.cpu(500, own=50)  # half a core over the next second
    fake.cpu(900, own=400)
    s.step(T0 + 1)
    row = s.last_row
    cores, anon = row["w"]["worker:P1"]
    assert cores == pytest.approx(0.5)
    assert anon == round(10_000 * ptree.PAGE / 2**20, 1)  # the build's 90k pages are the build's
    assert row["b"]["b"][0] == pytest.approx(4.0)
    assert row["x"][1] > 0 and row["nb"] == 1 and row["fast"] == 1


def test_the_owner_console_is_neither_a_worker_nor_overhead(env):
    """The console carries the run's state dir but no session id; its marker files
    it apart, so per-worker figures and the swarm's overhead leave it out."""
    cfg, fake, pings = env
    console_env = {"SWARM_STATE_DIR": str(cfg.state_dir), ptree.CONSOLE_ENV: "1"}
    fake.add(700, anon_pages=20_000, comm="claude", env=console_env)
    fake.add(701, ppid=700, anon_pages=5_000, comm="bash", env=console_env)
    fake.add(600, anon_pages=1_000, env={"SWARM_STATE_DIR": str(cfg.state_dir)}, comm="swarm")
    fake.add(500, anon_pages=10_000, comm="claude", env=worker_env(cfg))
    s = make_sampler(cfg, fake, pings)
    row = s.step(T0)
    assert set(row["w"]) == {"worker:P1"}
    assert row["x"][1] == round(1_000 * ptree.PAGE / 2**20, 1)
    assert row["o"][1] == round(25_000 * ptree.PAGE / 2**20, 1)
    snap = store.read_now(cfg.state_dir)
    assert [w["label"] for w in snap["workers"]] == ["worker:P1"]
    assert snap["console"]["procs"] == 2
    assert "owner console" in view.render(view.collect(cfg, now=T0 + 1))
    [minute] = store.aggregate([row])
    assert minute["o"] == row["o"]


def test_idle_sampling_is_slow_and_building_is_fast(env):
    cfg, fake, pings = env
    s = make_sampler(cfg, fake, pings)
    assert s.step(T0) is not None
    assert s.step(T0 + 1) is None  # nothing building: next sample at SLOW_S
    assert s.step(T0 + sampler.SLOW_S) is not None
    fake.add(900, started=T0 + 15)
    event(cfg, ts=T0 + 15.5, event="start", id="b", pid=900, cls="heavy")
    assert s.step(T0 + 16) is not None
    assert s.step(T0 + 17) is not None


def test_a_failing_sample_never_escapes_the_thread(env, monkeypatch):
    cfg, fake, pings = env
    logged: list[str] = []
    s = sampler.Sampler(lambda: cfg, log=logged.append, proc_root=fake.root, wsl=False)
    monkeypatch.setattr(s, "step", lambda now: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sampler, "FAST_S", 0.01)
    s.start()
    import time
    time.sleep(0.1)
    s.stop()
    assert logged and logged[0].startswith("RESOURCES-ERROR")
    assert len(logged) == 1  # rate-limited


def test_the_snapshot_states_the_samplers_own_cost(env):
    cfg, fake, pings = env
    s = make_sampler(cfg, fake, pings)
    s.step(T0)
    snap = json.loads(store.now_path(cfg.state_dir).read_text())
    o = snap["sampler"]
    assert o["samples"] == 1 and o["bytes_written"] > 0 and "pct_core" in o
    assert snap["static"]["mem_total_mb"] == round(28734976 / 1024, 1)


# -- the supervisor owns it -------------------------------------------------------------
def test_a_running_swarm_samples_and_reports_it(swarm):
    swarm.env["SWARM_RESOURCES"] = "1"
    swarm.up()
    now = store.now_path(swarm.state_dir)
    assert swarm.wait(now.is_file, timeout=20)
    snap = json.loads(now.read_text())
    assert snap["pid"] == swarm.state()["supervisor_pid"]
    assert snap["host"]["avail_mb"] > 0
    out = swarm.cli("status").stdout
    assert "resources: cpu" in out
    # the demo run may already have finished: the check is there either way
    doc = swarm.cli("doctor", check=False).stdout
    assert "resources.sampler" in doc and "resources.idle-build" in doc


def test_an_idle_holder_ping_goes_through_the_supervisors_notify_path(tmp_path, monkeypatch):
    from swarm_orchestrator.supervisor import Supervisor

    sink = tmp_path / "tg.log"
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(sink))
    project = tmp_path / "project"
    project.mkdir()
    sup = Supervisor(load(project_dir=str(project)))
    sup._resources_note("idle-build:b1", "a build has held a build slot for 11 min")
    assert not sink.exists()  # a report, held for the Overseer's summary: nobody is asked
    rows = [json.loads(line) for line in
            (tmp_path / "state" / "notifications.jsonl").read_text().splitlines()]
    assert (rows[-1]["kind"], rows[-1]["class"]) == ("idle-build", "folded")
    assert "held a build slot" in rows[-1]["text"]
