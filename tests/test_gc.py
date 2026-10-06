"""``swarm gc``: it actually prunes now, on its own, and never during a build.

Every planner once skipped a symlinked ``cache/target/<repo>`` entry,
and in a mirrored workspace *every* entry is a symlink into the canonical repo's own
``target/`` — so ``swarm gc`` reclaimed nothing while those targets kept
growing. These tests pin the repaired behaviour on a fake tree: one sweep per
real target however many links share it, incremental state always dropped,
orphan mirrors and stale session temp dirs pruned, a dry run that touches
nothing, and the supervisor's automatic run that skips a held build slot.

Nothing here runs a real ``cargo-sweep``: a stub on disk records its argv and
the target its shim project links to.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import threading
import time
from pathlib import Path

import pytest
from conftest import machine_toml

from swarm_orchestrator import gc as gc_mod
from swarm_orchestrator import gitq, launch, ovrecord, resolver
from swarm_orchestrator import operator as operator_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator.config import load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.supervisor import Supervisor
from swarm_orchestrator.tui import dash as dash_mod
from swarm_orchestrator.tui import probes
from swarm_orchestrator.tui.data import Meter


# -- fixtures ---------------------------------------------------------------
@pytest.fixture
def cfg(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    monkeypatch.setenv("SWARM_SLUG", "gctest")
    monkeypatch.setenv("SWARM_DRIVER", "bare")
    machine_toml(build={"max_concurrent": 1})
    for leak in ("SWARM_GIT_ISOLATION", "SWARM_WORKER_CMD", "SWARM_READY_MARKER"):
        monkeypatch.delenv(leak, raising=False)
    c = load(project_dir=str(project))
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


def _fill(path: Path, size: int = 4096) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _target(project: Path, repo: str) -> Path:
    """A canonical repo with source and a real ``target/`` holding build output."""
    _fill(project / repo / "src" / "lib.rs", 100)
    t = project / repo / "target"
    _fill(t / "debug" / "deps" / "libdep.rlib")
    _fill(t / "debug" / ".fingerprint" / "dep" / "hash")
    _fill(t / "debug" / "incremental" / "crate-1" / "s-abc" / "query-cache.bin")
    _fill(t / "release" / "incremental" / "crate-2" / "dep-graph.bin")
    return t


@pytest.fixture
def tree(cfg):
    """``cache/target`` shaped like a real build cache: symlinks into canonical targets,
    two of them sharing one real dir, plus links GC must refuse to follow."""
    project = cfg.project_dir
    a, b = _target(project, "alpha"), _target(project, "beta")
    cache = cfg.build_cache_dir
    cache.mkdir(parents=True)
    (cache / "alpha").symlink_to(a)
    (cache / "alpha-old").symlink_to(a)  # a second link to the same real target
    (cache / "beta").symlink_to(b)
    (cache / "dangling").symlink_to(project / "gone" / "target")
    (cache / "evil").symlink_to(project / "alpha" / "src")  # not a target dir
    return {"alpha": a, "beta": b}


@pytest.fixture
def sweep_stub(tmp_path, monkeypatch):
    """A ``cargo-sweep`` that records each call and the real target it was aimed at."""
    calls = tmp_path / "sweep-calls.jsonl"
    stub = tmp_path / "bin" / "cargo-sweep"
    stub.parent.mkdir()
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "shim = sys.argv[-1]\n"
        f"with open({str(calls)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps({'argv': sys.argv[1:],"
        " 'target': os.path.realpath(os.path.join(shim, 'target'))}) + '\\n')\n"
        "print('Would clean: 1.00 MiB' if '--dry-run' in sys.argv else 'Cleaned 1.00 MiB')\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(gc_mod, "_cargo_sweep", lambda: str(stub))

    def read() -> list[dict]:
        if not calls.exists():
            return []
        return [json.loads(line) for line in calls.read_text().splitlines()]

    return read


def _kinds(plan) -> dict[str, list]:
    out: dict[str, list] = {}
    for t in plan.targets:
        out.setdefault(t.kind, []).append(t)
    return out


# -- symlinked target caches ---------------------------------------------------
def test_symlinked_caches_resolve_and_a_shared_target_is_swept_once(cfg, tree, sweep_stub):
    plan = gc_mod.plan_gc(cfg, gc_mod.GcOptions(sweep_days=3))

    sweeps = _kinds(plan)["cargo-sweep"]
    assert sorted(Path(t.path) for t in sweeps) == sorted(tree.values())
    assert {t.label for t in sweeps} == {"cache/target/alpha+alpha-old", "cache/target/beta"}
    calls = sweep_stub()
    assert sorted(c["target"] for c in calls) == sorted(str(p) for p in tree.values())
    assert all(c["argv"][:3] == ["sweep", "--time", "3"] and "--dry-run" in c["argv"] for c in calls)
    # Neither the dangling link nor the link at a source dir becomes a root.
    assert set(gc_mod.cache_roots(cfg)) == set(tree.values())


def test_apply_sweeps_each_real_target_once_and_drops_incremental(cfg, tree, sweep_stub):
    plan = gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True, sweep_days=3, estimate=False))
    assert sweep_stub() == []  # no dry-run estimate when estimate=False
    incr = _kinds(plan)["incremental"]
    assert len(incr) == 4  # debug + release, for each of two real targets

    gc_mod.apply(plan)

    real = [c for c in sweep_stub() if "--dry-run" not in c["argv"]]
    assert sorted(c["target"] for c in real) == sorted(str(p) for p in tree.values())
    for t in tree.values():
        assert not (t / "debug" / "incremental").exists()
        assert not (t / "release" / "incremental").exists()
        assert (t / "debug" / "deps" / "libdep.rlib").exists()  # the stub sweeps nothing
        assert (t / "debug" / ".fingerprint").is_dir()
    assert (cfg.project_dir / "alpha" / "src" / "lib.rs").exists()  # source never touched
    assert all(t.error is None for t in plan.targets), [t.error for t in plan.targets]
    assert sum(t.reclaimed or 0 for t in incr) > 0


def test_nothing_outside_a_linked_target_is_reachable_without_canonical(cfg, tree):
    """The resolution is the only way into the project; a hand-edited plan
    pointing at source is refused at apply time."""
    evil = gc_mod.Target(
        kind="incremental", label="x", op="rmtree", detail="",
        path=str(cfg.project_dir / "alpha" / "src"),
    )
    whole = gc_mod.Target(kind="incremental", label="y", op="rmtree", detail="",
                          path=str(tree["alpha"]))
    plan = gc_mod.GcPlan(cfg=cfg, opts=gc_mod.GcOptions(yes=True), targets=[evil, whole])
    gc_mod.apply(plan)
    assert evil.error and "canonical" in evil.error
    assert whole.error and "canonical" in whole.error  # a root itself is never rmtree'd
    assert (cfg.project_dir / "alpha" / "src" / "lib.rs").exists()
    assert tree["alpha"].is_dir()


def test_a_dry_run_changes_nothing(cfg, tree, sweep_stub):
    (cfg.tmp_dir / "gone-phase").mkdir(parents=True)
    _fill(cfg.tmp_dir / "gone-phase" / "scratch")
    (cfg.wt_dir / "gone-phase").mkdir(parents=True)

    plan = gc_mod.plan_gc(cfg, gc_mod.GcOptions(sweep_days=3))
    assert plan.total_bytes > 0
    with pytest.raises(gc_mod.GcRefused):
        gc_mod.apply(plan)

    for t in tree.values():
        assert (t / "debug" / "incremental").is_dir()
    assert (cfg.tmp_dir / "gone-phase" / "scratch").exists()
    assert (cfg.wt_dir / "gone-phase").is_dir()
    assert all("--dry-run" in c["argv"] for c in sweep_stub())
    assert "DRY RUN" in gc_mod.render(plan)


# -- superseded cargo units ------------------------------------------------------
_OLD = 3 * 3600  # older than the grace window, far younger than keep_days


def _unit(
    profile: Path,
    pkg: str,
    h: str,
    *,
    by: Path,
    local: bool = False,
    stem: str | None = None,
    kind: str = "lib",
    features: tuple = (),
    deps: tuple[str, ...] = (),
    age: float = _OLD,
) -> None:
    """One cargo unit the way cargo lays it out: a fingerprint dir whose hash
    file holds the unit's fingerprint (little-endian hex, as cargo writes it), a
    JSON naming its dependencies by fingerprint, an output and a ``.d`` whose
    first line names the target it was built through (``by``)."""
    stem = stem or pkg
    fp = profile / ".fingerprint" / f"{pkg}-{h}"
    fp.mkdir(parents=True)
    (fp / f"{kind}-{stem}").write_text(int(h, 16).to_bytes(8, "little").hex())
    (fp / f"{kind}-{stem}.json").write_text(json.dumps({
        "features": json.dumps(list(features)), "profile": 1, "target": 2,
        "compile_kind": 0, "rustflags": [],
        "deps": [[0, d, False, int(d.rsplit("-", 1)[1], 16)] for d in deps],
    }))
    out = profile / "deps" / (f"lib{stem}-{h}.rlib" if kind == "lib" else f"{stem}-{h}")
    _fill(out)
    src = f"tests/{stem}.rs" if local else f"/home/u/.cargo/registry/src/{pkg}/lib.rs"
    dinfo = profile / "deps" / f"{stem}-{h}.d"
    dinfo.write_text(f"{by}/debug/deps/{out.name}: {src}\n")
    stamp = time.time() - age
    for p in (fp, *fp.iterdir(), out, dinfo):
        os.utime(p, (stamp, stamp))


@pytest.fixture
def cargo_cache(cfg):
    """A real (unlinked) ``cache/target/<repo>/debug`` holding two finished
    phases' worth of units, ``P-old`` and ``P-new``."""
    profile = cfg.build_cache_dir / gitq._slug(cfg.project_dir) / "debug"
    wt = {p: cfg.wt_dir / p / "webhooks" / "target" for p in ("P-old", "P-new")}
    # The contract crate, repinned between the two finished phases.
    _unit(profile, "bundle", "a1" * 8, by=wt["P-old"], age=_OLD + 60)
    _unit(profile, "bundle", "a2" * 8, by=wt["P-new"])
    # A third-party crate every phase reuses: one copy, built long ago.
    _unit(profile, "serde", "5e" * 8, by=wt["P-old"], age=_OLD + 120)
    # The repo's own crate and a test binary, once per phase.
    for phase, h, contract in (("P-old", "01", "a1"), ("P-new", "02", "a2")):
        dep = (f"bundle-{contract * 8}", f"serde-{'5e' * 8}")
        _unit(profile, "webhooks", h * 8, by=wt[phase], local=True, deps=dep)
        _unit(profile, "webhooks", h * 4 + "bb" * 4, by=wt[phase], local=True,
              stem="watch_mark", kind="test-integration-test",
              deps=(*dep, f"webhooks-{h * 8}"))
    # A fingerprint-less output (an interrupted build) and a stray non-unit file.
    _fill(profile / "deps" / f"libghost-{'9f' * 8}.rlib")
    old = time.time() - _OLD
    os.utime(profile / "deps" / f"libghost-{'9f' * 8}.rlib", (old, old))
    _fill(profile / "deps" / "README")
    return profile


def _alive(profile: Path) -> set[str]:
    return {p.name for p in (profile / ".fingerprint").iterdir()}


def test_superseded_units_go_and_the_newest_of_each_kind_stays(cfg, cargo_cache):
    plan = gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True))
    (row,) = _kinds(plan)["superseded"]
    assert row.label == f"cache/target/{gitq._slug(cfg.project_dir)}/debug"
    assert row.estimate > 0

    gc_mod.apply(plan)

    assert row.error is None and row.reclaimed > 0
    # The newest contract and the one serde survive; the repinned-away contract
    # goes, and so does every unit the finished phases built of the repo's own
    # crate — a fresh worktree rebuilds those whatever the cache holds.
    assert _alive(cargo_cache) == {f"bundle-{'a2' * 8}", f"serde-{'5e' * 8}"}
    deps = {p.name for p in (cargo_cache / "deps").iterdir()}
    assert f"libbundle-{'a2' * 8}.rlib" in deps and f"libserde-{'5e' * 8}.rlib" in deps
    assert not any("a1" * 8 in n or "01" * 8 in n or "0101" in n for n in deps)
    assert f"libghost-{'9f' * 8}.rlib" not in deps  # outputs without a fingerprint
    assert "README" in deps  # not a unit: never touched


def test_units_of_an_in_flight_phase_and_their_dependencies_stay(cfg, cargo_cache):
    wt = cfg.wt_dir / "P-live" / "webhooks" / "target"
    # The live phase built against the OLD contract (it has not repinned yet),
    # an hour ago: neither recent nor the newest of its kind, yet it must stay.
    _unit(cargo_cache, "webhooks", "03" * 8, by=wt, local=True,
          deps=(f"bundle-{'a1' * 8}", f"serde-{'5e' * 8}"))
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P-live")

    gc_mod.apply(gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True)))

    alive = _alive(cargo_cache)
    assert f"webhooks-{'03' * 8}" in alive
    assert f"bundle-{'a1' * 8}" in alive  # kept because the live unit links it
    assert (cargo_cache / "deps" / f"libbundle-{'a1' * 8}.rlib").exists()
    assert f"webhooks-{'01' * 8}" not in alive and f"webhooks-{'02' * 8}" not in alive


def test_a_recently_touched_unit_is_never_superseded(cfg, cargo_cache):
    # Written a minute ago by a build that has not been recorded anywhere yet.
    _unit(cargo_cache, "bundle", "a3" * 8, by=cfg.wt_dir / "P-gone" / "target", age=60)
    _unit(cargo_cache, "webhooks", "04" * 8, by=cfg.wt_dir / "P-gone" / "target",
          local=True, age=60)
    now_dead = gc_mod.superseded(cfg, cargo_cache, set())
    names = {p.name for p in now_dead}
    assert f"bundle-{'a3' * 8}" not in names and f"webhooks-{'04' * 8}" not in names
    # a3 is now the newest contract, so a2 has become the superseded one.
    assert f"bundle-{'a2' * 8}" in names


def test_a_supersede_target_outside_a_cache_profile_is_refused(cfg, cargo_cache):
    evil = gc_mod.Target(kind="superseded", label="x", op="supersede", detail="",
                         path=str(cfg.project_dir))
    plan = gc_mod.GcPlan(cfg=cfg, opts=gc_mod.GcOptions(yes=True), targets=[evil])
    gc_mod.apply(plan)
    assert evil.error and "profile" in evil.error


# -- session temp dirs ---------------------------------------------------------
def test_stale_tmp_goes_and_a_live_sessions_tmp_stays(cfg):
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P-live")
        st.claim_operator("job1", time.time() + 600, time.time())
        st.overseer_pass = "OVS1"
    for name in ("P-live", "P-dead", "op-job1", ovrecord.mirror_name("OVS1")):
        _fill(cfg.tmp_dir / name / "f")

    plan = gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True))
    stale = [t.label for t in _kinds(plan).get("stale-tmp", [])]
    assert stale == ["tmp/P-dead"]
    gc_mod.apply(plan)
    assert not (cfg.tmp_dir / "P-dead").exists()
    for name in ("P-live", "op-job1", ovrecord.mirror_name("OVS1")):
        assert (cfg.tmp_dir / name / "f").exists()


def test_a_stray_file_in_tmp_goes_like_a_folder_and_a_symlink_is_left(cfg):
    """A session can leave a plain file beside its TMPDIR. It is unlinked, not
    handed to `rmtree`, which raises on a file and so failed on every run."""
    _fill(cfg.tmp_dir / "P-dead" / "f")
    _fill(cfg.tmp_dir / "pointer", 94)
    kept = cfg.project_dir / "kept"
    _fill(kept / "f")
    (cfg.tmp_dir / "link").symlink_to(kept)

    plan = gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True))
    stale = _kinds(plan).get("stale-tmp", [])
    assert [t.label for t in stale] == ["tmp/P-dead", "tmp/pointer"]
    gc_mod.apply(plan)
    assert [t.error for t in plan.targets if t.error] == []
    assert not (cfg.tmp_dir / "P-dead").exists()
    assert not (cfg.tmp_dir / "pointer").exists()
    assert all(t.reclaimed == t.before for t in stale)
    assert (cfg.tmp_dir / "link").is_symlink() and (kept / "f").exists()


def test_a_tmp_dir_that_went_live_after_planning_is_skipped(cfg):
    _fill(cfg.tmp_dir / "P1" / "f")
    plan = gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True))
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P1")  # launched between plan and apply
    gc_mod.apply(plan)
    assert (cfg.tmp_dir / "P1" / "f").exists()
    assert "live" in (_kinds(plan)["stale-tmp"][0].error or "")


def test_tmp_leftovers_in_slash_tmp_are_reported_never_removed(tmp_path):
    root = tmp_path / "fake-tmp"
    target = root / "s14t"
    _fill(target / "CACHEDIR.TAG", 10)
    _fill(target / "debug" / "big", 8192)
    _fill(root / "someone-elses" / "big", 8192)
    found = gc_mod.tmp_offenders(root, min_bytes=4096)
    assert [p.name for p, _, _ in found] == ["s14t"]
    assert target.exists()


# -- orphan mirrors (real git) ----------------------------------------------
@pytest.fixture
def gitcfg(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    from test_worktree import _cfg, _make_project

    project, _origin = _make_project(tmp_path)
    monkeypatch.setenv("SWARM_CLAUDE_CONFIG", str(tmp_path / "claude.json"))
    c = _cfg(monkeypatch, tmp_path, project, driver="bare")
    c.ensure_dirs()
    state_mod.init_state(c)
    return c


def test_orphan_mirrors_are_pruned_the_way_reconcile_would(gitcfg):
    cfg = gitcfg
    log = Log(cfg.supervisor_log)
    for phase in ("P-live", "P-dead", "P-finished"):
        gitq.worktree_add(cfg, phase, log)
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P-live")
    (cfg.done_dir / "P-finished.ok").write_text("")  # finished, never integrated

    plan = gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True))
    assert [t.label for t in _kinds(plan).get("orphan-mirror", [])] == ["wt/P-dead"]
    assert any("P-finished" in p and "swarm up" in p for p in plan.protected)

    gc_mod.apply(plan, log)
    log.close()
    assert not (cfg.wt_dir / "P-dead").exists()
    assert not gitq.branch_exists(cfg.project_dir, "swarm/P-dead")
    assert (cfg.wt_dir / "P-live").is_dir() and (cfg.wt_dir / "P-finished").is_dir()
    assert gitq.branch_exists(cfg.project_dir, "swarm/P-finished")


def test_an_interrupted_phase_holding_work_is_left_for_its_next_launch(gitcfg):
    from test_worktree import _git

    cfg = gitcfg
    ledger = cfg.project_dir / cfg.ledger
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("L1\nL2 needs:L1\n")
    log = Log(cfg.supervisor_log)
    wt = gitq.worktree_add(cfg, "L2", log)
    (wt / "half.txt").write_text("half")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-m", "half done")
    plan = gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True))
    log.close()
    assert "orphan-mirror" not in _kinds(plan)
    assert any("L2" in p and "resumes" in p for p in plan.protected)


def test_old_attic_refs_go_and_recent_ones_stay(gitcfg):
    from test_worktree import _git, _out

    cfg = gitcfg
    project = cfg.project_dir
    head = _out(project, "rev-parse", "HEAD").strip()
    old = time.strftime(gitq.ATTIC_STAMP, time.gmtime(time.time() - 31 * 86400))
    new = time.strftime(gitq.ATTIC_STAMP, time.gmtime(time.time() - 29 * 86400))
    _git(project, "update-ref", f"refs/swarm-attic/P1/{old}", head)
    _git(project, "update-ref", f"refs/swarm-attic/P1/{new}", head)
    plan = gc_mod.plan_gc(cfg, gc_mod.GcOptions(yes=True))
    assert [t.extra["ref"] for t in _kinds(plan)["attic"]] == [f"refs/swarm-attic/P1/{old}"]
    gc_mod.apply(plan)
    assert [r for r, _ in gitq.attic_refs(project)] == [f"refs/swarm-attic/P1/{new}"]


# -- TMPDIR per session kind ---------------------------------------------------
def _tmp_of(env: dict, cfg, name: str) -> Path:
    want = str(cfg.tmp_dir / name)
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == want
    assert Path(want).is_dir()
    return Path(want)


def test_every_session_kind_gets_its_own_tmpdir_on_disk(cfg):
    _tmp_of(launch._worker_env(cfg, "P1"), cfg, "P1")
    _tmp_of(operator_mod._operator_env(cfg, "job1"), cfg, "op-job1")
    _tmp_of(resolver._resolver_env(cfg, "P1"), cfg, "P1")  # shares the phase's
    ovs = ovrecord.mirror_name("OVS1")
    _tmp_of(launch.session_env(cfg, None, tmp=ovs), cfg, ovs)
    assert "TMPDIR" not in launch.session_env(cfg)  # only when a session is named
    assert launch.tmp_env(cfg, "../escape") == {}


def test_the_overseer_spawn_hands_its_session_a_tmpdir(cfg, monkeypatch):
    sup = Supervisor(cfg)
    seen = []
    monkeypatch.setattr(sup.master, "spawn", lambda kind, pane=None, **kw: seen.append(kw) or True)
    monkeypatch.setattr(launch, "_poke_fifo", lambda *a, **k: True)
    sup._overseer_spawn("OVS9", [], 0.0, [], [], None)
    sup.log.close()
    _tmp_of(seen[0]["env"], cfg, ovrecord.mirror_name("OVS9"))


def test_tmpdirs_go_when_their_session_ends(cfg, monkeypatch):
    sup = Supervisor(cfg)
    monkeypatch.setattr(sup.master, "kill", lambda: None)
    # A worker: its phase finishing (in place) drops it.
    with state_mod.transaction(cfg) as st:
        st.claim_slot("P1")
    launch._worker_env(cfg, "P1")
    sup._advance_done("P1", "ok")
    assert not (cfg.tmp_dir / "P1").exists()
    # A merge/discard drops it with the mirror.
    launch._worker_env(cfg, "P2")
    gitq._rmtree_mirror(cfg, "P2")
    assert not (cfg.tmp_dir / "P2").exists()
    # The operator: its job ending.
    operator_mod._operator_env(cfg, "job1")
    sup._on_operator_done("job1")
    assert not (cfg.tmp_dir / "op-job1").exists()
    # The Overseer: its pass ending.
    ovs = ovrecord.mirror_name("OVS1")
    launch.session_env(cfg, None, tmp=ovs)
    sup._overseer_live = "OVS1"
    monkeypatch.setattr(ovrecord, "update", lambda *a, **k: None)
    sup._end_overseer_pass("OVS1", ovrecord.DONE)
    sup.log.close()
    assert not (cfg.tmp_dir / ovs).exists()


# -- the automatic run ---------------------------------------------------------
def _auto_sup(cfg, monkeypatch, results: list):
    monkeypatch.setattr(cfg, "gc_auto", True)
    sup = Supervisor(cfg)
    calls = []

    def fake(c, log=None):
        calls.append(time.time())
        return results.pop(0) if results else gc_mod.AutoResult(gc_mod.AUTO_DONE, freed=10)

    monkeypatch.setattr(gc_mod, "auto", fake)
    return sup, calls


def _tick(sup) -> None:
    sup._gc_tick()
    if sup._gc_thread is not None:
        sup._gc_thread.join(5)


def _hold_seat0(cfg) -> int:
    """A build alive on the gate, as the gate knows one: its seat is locked."""
    cfg.buildsem_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.buildsem_dir / "seat0", os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_auto_gc_skips_a_held_build_slot_and_retries_later(cfg, monkeypatch):
    """The real `auto`: a build holding the only slot past `[gc].wait_s` means
    `busy`, and nothing is planned or deleted while it runs."""
    monkeypatch.setattr(cfg, "gc_wait_s", 1)
    _fill(cfg.tmp_dir / "P-dead" / "f")
    fd = _hold_seat0(cfg)
    try:
        t0 = time.monotonic()
        result = gc_mod.auto(cfg)
        assert time.monotonic() - t0 < 5  # waits `[gc].wait_s`, no longer
    finally:
        os.close(fd)
    assert result.outcome == gc_mod.AUTO_BUSY
    assert (cfg.tmp_dir / "P-dead" / "f").exists()

    done = gc_mod.auto(cfg)  # slot free again
    assert done.outcome == gc_mod.AUTO_DONE and done.by_kind.get("stale-tmp", 0) > 0
    assert not (cfg.tmp_dir / "P-dead").exists()


def test_auto_gc_waits_for_the_slot_and_runs_between_two_builds(cfg, monkeypatch):
    """A slot freed during the wait is taken — ahead of a builder polling for it —
    so a gc on a swarm that is never idle still runs."""
    monkeypatch.setattr(cfg, "gc_wait_s", 30)
    _fill(cfg.tmp_dir / "P-dead" / "f")
    fd = _hold_seat0(cfg)
    threading.Timer(0.5, os.close, args=(fd,)).start()  # the build ends mid-wait
    t0 = time.monotonic()
    result = gc_mod.auto(cfg)
    assert 0.4 < time.monotonic() - t0 < 10
    assert result.outcome == gc_mod.AUTO_DONE and result.by_kind.get("stale-tmp", 0) > 0
    assert not (cfg.tmp_dir / "P-dead").exists()


def test_auto_gc_names_every_failing_target_in_the_log_and_the_record(cfg, monkeypatch):
    """`errors=N` must be readable without a second run: each failing target is
    a log line of its own, and the record's detail carries all of them."""
    for name in ("P-a", "P-b"):
        _fill(cfg.tmp_dir / name / "f")

    def refuse(c, target, log=None, live=None):
        raise OSError(f"cannot remove {target.label}")

    monkeypatch.setattr(gc_mod, "_execute", refuse)
    log = Log(cfg.supervisor_log)
    result = gc_mod.auto(cfg, log)
    log.close()

    assert result.outcome == gc_mod.AUTO_DONE and result.errors == 2
    lines = [ln for ln in cfg.supervisor_log.read_text().splitlines() if "GC-AUTO-ERROR" in ln]
    assert [ln.split("GC-AUTO-ERROR ", 1)[1] for ln in lines] == [
        "tmp/P-a: cannot remove tmp/P-a",
        "tmp/P-b: cannot remove tmp/P-b",
    ]
    gc_mod.write_record(cfg, result, "interval", time.time())
    rec = gc_mod.read_record(cfg)
    assert rec["errors"] == 2
    assert rec["detail"] == "tmp/P-a: cannot remove tmp/P-a; tmp/P-b: cannot remove tmp/P-b"


def test_the_supervisor_backs_off_after_busy_and_runs_on_the_interval(cfg, monkeypatch):
    busy = gc_mod.AutoResult(gc_mod.AUTO_BUSY, detail="build slot 0 busy")
    sup, calls = _auto_sup(cfg, monkeypatch, [busy])
    monkeypatch.setattr(cfg, "gc_idle_s", 0)
    sup._gc_last = time.time() - cfg.gc_every_s - 1  # overdue

    _tick(sup)
    assert len(calls) == 1 and sup._gc_retry_at > time.time()
    _tick(sup)
    assert len(calls) == 1  # backing off, not hammering the gate

    sup._gc_retry_at = 0.0
    _tick(sup)
    assert len(calls) == 2
    _tick(sup)
    assert len(calls) == 2  # ran: not due again for [gc].every_s
    rec = gc_mod.read_record(cfg)
    assert rec["outcome"] == gc_mod.AUTO_DONE and rec["reason"] == "interval"
    log = cfg.supervisor_log.read_text()
    sup.log.close()
    assert "GC-AUTO-SKIP interval busy" in log and "GC-AUTO interval freed=10" in log


def test_the_supervisor_runs_once_per_idle_episode(cfg, monkeypatch):
    sup, calls = _auto_sup(cfg, monkeypatch, [])
    monkeypatch.setattr(cfg, "gc_every_s", 0)

    _tick(sup)  # idle starts now
    assert calls == []
    sup._gc_idle_since -= cfg.gc_idle_s + 1
    _tick(sup)
    assert len(calls) == 1
    sup._gc_idle_since -= 10 * cfg.gc_idle_s
    _tick(sup)
    assert len(calls) == 1  # same episode: once

    with state_mod.transaction(cfg) as st:
        st.claim_slot("P1")
    _tick(sup)  # busy: the episode is over
    with state_mod.transaction(cfg) as st:
        st.free_slot_for("P1")
    _tick(sup)  # a new idle episode starts
    sup._gc_idle_since -= cfg.gc_idle_s + 1
    _tick(sup)
    sup.log.close()
    assert len(calls) == 2


def test_auto_off_never_starts_a_gc(cfg, monkeypatch):
    sup, calls = _auto_sup(cfg, monkeypatch, [])
    monkeypatch.setattr(cfg, "gc_auto", False)
    sup._gc_last = 0.0
    _tick(sup)
    sup.log.close()
    assert calls == [] and sup._gc_thread is None


# -- the dashboard probe -------------------------------------------------------
def test_context_comes_from_the_meters_and_git_runs_every_30s(cfg, monkeypatch):
    d = dash_mod.Dash(cfg)
    slot = type("S", (), {"busy": True, "pane_id": "%1", "phase": "P1", "worktree": "/wt/P1"})()
    d.snapshot = type("Snap", (), {"slots": [slot]})()
    monkeypatch.setattr(probes, "agents", lambda: [])
    monkeypatch.setattr(probes, "panes", lambda: {})
    monkeypatch.setattr(probes, "capture", lambda pane: "... 900k/1.0M ...")
    monkeypatch.setattr(probes, "parse_context", lambda text: pytest.fail("pane scraped for context"))
    git = []
    monkeypatch.setattr(probes, "repo_stat", lambda wt, main: git.append(wt) or probes.RepoStat(1, 0))
    d.meters = {"P1": Meter(phase="P1", ts=0.0, context_tokens=100_000, context_window=1_000_000)}

    d.probe(now=1000.0)
    d.probe(now=1010.0)
    d.probe(now=1029.0)
    assert git == ["/wt/P1"]
    d.probe(now=1031.0)
    assert git == ["/wt/P1", "/wt/P1"]
    assert d.tails == {"%1": "... 900k/1.0M ..."}  # the activity preview still has it
    assert d.context_pct("P1") == pytest.approx(10.0)


def test_swarm_gc_yes_reports_a_refusal_at_delete_time(cfg, monkeypatch, capsys):
    """The delete step re-checks its blockers; a refusal there (a build started
    after the plan was made) is a clean message and exit 1, not a traceback."""
    from swarm_orchestrator import cli

    def refuse(plan, log=None):
        raise gc_mod.GcRefused("a build is running")

    monkeypatch.setattr(gc_mod, "apply", refuse)
    assert cli.cmd_gc(cfg, gc_mod.GcOptions(yes=True), verbose=False) == 1
    assert "swarm gc refused: a build is running" in capsys.readouterr().err


def test_remote_attic_deletes_wait_until_the_build_gate_is_released(gitcfg, monkeypatch):
    """Each is a network call; no build should queue behind a dead remote."""
    from contextlib import contextmanager

    from swarm_orchestrator import backup
    from test_worktree import _git, _out

    project = gitcfg.project_dir
    head = _out(project, "rev-parse", "HEAD").strip()
    old = time.strftime(gitq.ATTIC_STAMP, time.gmtime(time.time() - 31 * 86400))
    _git(project, "update-ref", f"refs/swarm-attic/P1/{old}", head)
    held = {"gate": False}
    calls = []

    @contextmanager
    def gate(cfg, opts):
        held["gate"] = True
        try:
            yield
        finally:
            held["gate"] = False

    monkeypatch.setattr(gc_mod, "build_gate", gate)
    monkeypatch.setattr(backup, "drop_attic",
                        lambda repo, ref, log: calls.append((ref, held["gate"])))
    gc_mod.apply(gc_mod.plan_gc(gitcfg, gc_mod.GcOptions(yes=True)))
    assert calls == [(f"refs/swarm-attic/P1/{old}", False)]
