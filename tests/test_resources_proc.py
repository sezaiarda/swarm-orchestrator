"""resources: reading the host and process trees from /proc (committed fixtures)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from swarm_orchestrator.resources import builds, disk, host, ptree

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "proc"
MB = 1024 * 1024


@pytest.fixture
def proc(tmp_path) -> Path:
    root = tmp_path / "proc"
    shutil.copytree(FIXTURE, root)
    return root


def set_env(root: Path, pid: int, env: dict[str, str]) -> None:
    (root / str(pid) / "environ").write_bytes(
        b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()))


# -- host -----------------------------------------------------------------------
def test_cpu_busy_leaves_out_idle_and_iowait():
    busy, total = host.parse_cpu((FIXTURE / "stat").read_text())
    assert total == 1000 + 50 + 400 + 8000 + 200 + 10 + 20 + 5
    assert busy == total - 8000 - 200


def test_memory_splits_anon_from_page_cache():
    mem = host.memory(host.parse_meminfo((FIXTURE / "meminfo").read_text()))
    assert mem["avail_mb"] == round(23980032 / 1024, 1)
    assert mem["anon_mb"] == round(2441216 / 1024, 1)
    # page cache = Buffers + Cached - Shmem (shmem is not reclaimable cache)
    assert mem["cache_mb"] == round((204800 + 23000000 - 774144) / 1024, 1)
    assert mem["swap_mb"] == round((8388608 - 4829184) / 1024, 1)


def test_unitless_meminfo_fields_are_not_scaled():
    assert host.parse_meminfo("HugePages_Total:       3\n") == {"HugePages_Total": 3}


def test_diskstats_counts_whole_disks_only():
    rd, wr = host.parse_diskstats((FIXTURE / "diskstats").read_text())
    # sda + nvme0n1; partitions, dm, loop and ram would count bytes twice or are not storage
    assert rd == (20480 + 2048) * 512
    assert wr == (40960 + 4096) * 512


def test_pressure_reads_the_monotonic_totals():
    assert host.parse_pressure((FIXTURE / "pressure" / "io").read_text()) == {
        "some": 3000000, "full": 2500000}


def test_host_reader_turns_counters_into_rates(proc):
    reader = host.HostReader(proc)
    first = reader.sample(100.0)
    assert first["cpu"] is None and first["psi"] is None  # no made-up zero
    assert first["load"] == 1.43 and first["avail_mb"] > 0

    stat = (proc / "stat").read_text().replace(
        "cpu  1000 50 400 8000 200", "cpu  1600 50 400 8400 200")
    (proc / "stat").write_text(stat)
    (proc / "pressure" / "memory").write_text(
        "some avg10=0 avg60=0 avg300=0 total=2500000\nfull avg10=0 avg60=0 avg300=0 total=1600000\n")
    (proc / "pressure" / "io").write_text(
        "some avg10=0 avg60=0 avg300=0 total=4000000\nfull avg10=0 avg60=0 avg300=0 total=3000000\n")
    disks = (proc / "diskstats").read_text().replace(
        "sda 1000 10 20480 500 2000 30 40960", "sda 1000 10 22528 500 2000 30 45056")
    (proc / "diskstats").write_text(disks)

    row = reader.sample(102.0)
    assert row["cpu"] == 60.0  # 600 busy of 1000 jiffies
    assert row["psi"]["mem"] == 25.0  # 0.5 s stalled in 2 s
    assert row["psi"]["memf"] == 5.0
    assert row["psi"]["io"] == 50.0 and row["psi"]["iof"] == 25.0
    assert row["psi"]["cpu"] == 0.0
    assert row["rd_mbs"] == round(2048 * 512 / MB / 2, 2)
    assert row["wr_mbs"] == round(4096 * 512 / MB / 2, 2)


def test_missing_pressure_files_give_no_pressure(proc):
    shutil.rmtree(proc / "pressure")
    reader = host.HostReader(proc)
    reader.sample(1.0)
    assert reader.sample(2.0)["psi"] is None


# -- process trees ----------------------------------------------------------------
def test_stat_parsing_survives_parens_and_spaces_in_the_name():
    p = ptree.parse_stat(4243, (FIXTURE / "4243" / "stat").read_text())
    assert (p.ppid, p.state, p.start, p.cpu, p.own) == (4242, "R", 5100, 400, 400)


def test_a_trees_cpu_includes_reaped_children_and_zombies(proc):
    table = ptree.scan(proc)
    assert set(table) == {77, 4242, 4243, 4244}
    tree = ptree.tree(ptree.children(table), {4242})
    assert tree == {4242, 4243, 4244}
    use = ptree.usage(table, tree, proc)
    # cargo 150+50 own + 700+100 reaped children, rustc 400, zombie 50
    assert use.cpu_s == pytest.approx((1000 + 400 + 50) / ptree.TICK)
    assert use.anon_mb == pytest.approx(((2000 - 500) + (30000 - 1000)) * ptree.PAGE / MB)
    assert use.rss_mb == pytest.approx((2000 + 30000) * ptree.PAGE / MB)
    assert (use.rd_bytes, use.wr_bytes) == (1048576 + 4096, 2097152 + 8192)
    assert use.procs == 3


def test_sessions_are_labelled_from_the_environment_and_inherited(proc, tmp_path):
    state = tmp_path / "state"
    set_env(proc, 4242, {"SWARM_STATE_DIR": str(state), "SWARM_SESSION_ID": "worker:P1"})
    set_env(proc, 77, {"SWARM_STATE_DIR": "/some/other/run", "SWARM_SESSION_ID": "worker:X"})
    labels = ptree.Attributor(state, proc).label(ptree.scan(proc))
    # the children have no readable environment: they inherit their parent's label
    assert labels == {4242: "worker:P1", 4243: "worker:P1", 4244: "worker:P1"}


def test_the_swarms_own_processes_are_infra_and_do_not_lend_a_label(proc, tmp_path):
    state = tmp_path / "state"
    set_env(proc, 4242, {"SWARM_STATE_DIR": str(state)})
    labels = ptree.Attributor(state, proc).label(ptree.scan(proc))
    assert labels == {4242: ptree.INFRA}


def test_a_reused_pid_is_labelled_afresh(proc, tmp_path):
    state = tmp_path / "state"
    set_env(proc, 77, {"SWARM_STATE_DIR": str(state), "SWARM_SESSION_ID": "worker:A"})
    attr = ptree.Attributor(state, proc)
    assert attr.label(ptree.scan(proc))[77] == "worker:A"
    set_env(proc, 77, {"SWARM_STATE_DIR": str(state), "SWARM_SESSION_ID": "worker:B"})
    assert attr.label(ptree.scan(proc))[77] == "worker:A"  # same process: remembered
    stat = (proc / "77" / "stat").read_text().replace(" 0 1 0 100 ", " 0 1 0 900 ")
    (proc / "77" / "stat").write_text(stat)
    assert attr.label(ptree.scan(proc))[77] == "worker:B"  # new start time: a new process


# -- the flock fallback -----------------------------------------------------------
def test_flock_holders_match_the_slot_files_device_and_inode():
    slots = {(8, 0x30, 546909): 0, (8, 0x30, 556457): 1}
    holders = builds.flock_holders((FIXTURE / "locks").read_text(), slots)
    # slot 1's line is a waiter (->) and a POSIX lock: nobody holds it
    assert holders == {0: 4242}


def test_slot_files_are_keyed_by_device_and_inode(tmp_path):
    sem = tmp_path / "buildsem"
    sem.mkdir()
    (sem / "slot0").touch()
    (sem / "slot1").touch()
    (sem / "events.jsonl").touch()
    st = (sem / "slot1").stat()
    keys = builds.slot_files(sem)
    assert sorted(keys.values()) == [0, 1]
    assert keys[(os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)] == 1


# -- disk ---------------------------------------------------------------------------
def test_wsl_headroom_is_vhdx_slack_plus_the_windows_drive():
    gb = 1024 ** 3
    h = disk.wsl_headroom(176 * gb, 118 * gb, 19 * gb)
    assert (h["slack_gb"], h["host_free_gb"], h["headroom_gb"]) == (58.0, 19.0, 77.0)
    # a vhdx smaller than what is used inside (it was compacted) has no slack, not negative
    assert disk.wsl_headroom(100 * gb, 118 * gb, 19 * gb)["headroom_gb"] == 19.0


def test_the_largest_vhdx_is_the_distro(tmp_path):
    for name, size in (("a", 10), ("b", 300), ("c", 20)):
        d = tmp_path / name
        d.mkdir()
        (d / "ext4.vhdx").write_bytes(b"\0" * size)
    found = disk.find_vhdx(globs=(str(tmp_path / "*" / "ext4.vhdx"),))
    assert found == tmp_path / "b" / "ext4.vhdx"
    assert disk.find_vhdx(str(tmp_path / "a" / "ext4.vhdx")) == tmp_path / "a" / "ext4.vhdx"
    assert disk.find_vhdx(str(tmp_path / "nope.vhdx")) is None


def test_wsl_is_detected_from_the_kernel_version():
    assert disk.is_wsl("Linux version 6.6.87.2-microsoft-standard-WSL2")
    assert not disk.is_wsl("Linux version 6.8.0-45-generic")


def test_outside_wsl_headroom_is_plain_free_space(tmp_path):
    h = disk.Headroom(tmp_path, wsl=False).read()
    assert h["wsl"] is False and h["headroom_gb"] >= 0


def test_du_measures_a_tree_and_reports_its_cpu(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f").write_bytes(b"x" * 100_000)
    size, cpu = disk.du(tmp_path / "d")
    assert size is not None and size >= 100_000
    assert cpu >= 0.0
    assert disk.du(tmp_path / "missing") == (0, 0.0)


def test_growth_needs_two_measurements_a_minute_apart():
    assert disk.growth_gb_per_h(None, {"ts": 0, "x": 1}, "x") is None
    assert disk.growth_gb_per_h({"ts": 0, "x": 1.0}, {"ts": 30, "x": 2.0}, "x") is None
    assert disk.growth_gb_per_h({"ts": 0, "x": 1.0}, {"ts": 1800, "x": 2.0}, "x") == 2.0
