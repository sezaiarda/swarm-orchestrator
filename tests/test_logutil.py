"""The supervisor log rotates by size, and its history readers read every file."""

from __future__ import annotations

from swarm_orchestrator import logutil


def test_the_log_rotates_at_max_bytes_and_keeps_three_old_files(tmp_path):
    path = tmp_path / "logs" / "supervisor.log"
    log = logutil.Log(path, max_bytes=200)
    try:
        for i in range(40):
            log.line(f"EVENT line {i:02d} " + "x" * 40)
    finally:
        log.close()
    olds = logutil.generations(path)
    assert [p.name for p in olds] == ["supervisor.log.3", "supervisor.log.2", "supervisor.log.1"]
    assert all(p.stat().st_size < 400 for p in [*olds, path])
    lines = logutil.read_all(path).splitlines()
    numbers = [int(ln.split("EVENT line ")[1][:2]) for ln in lines]
    assert numbers == sorted(numbers) and numbers[-1] == 39  # oldest first, newest last


def test_a_log_without_max_bytes_never_rotates(tmp_path):
    path = tmp_path / "supervisor.log"
    log = logutil.Log(path)
    try:
        for i in range(50):
            log.line("EVENT " + "x" * 100)
    finally:
        log.close()
    assert logutil.generations(path) == []


def test_read_all_can_take_only_the_newest_rotated_file(tmp_path):
    path = tmp_path / "supervisor.log"
    for text in ("a\n", "b\n", "c\n"):
        path.write_text(text, encoding="utf-8")
        logutil.rotate(path)
    path.write_text("d\n", encoding="utf-8")
    assert logutil.read_all(path) == "a\nb\nc\nd\n"
    assert logutil.read_all(path, keep=1) == "c\nd\n"
    assert logutil.read_all(path, current=False) == "a\nb\nc\n"


def test_report_and_usage_read_events_from_rotated_files(tmp_path, monkeypatch):
    """`swarm report`'s timings and the run summaries read the whole history."""
    from swarm_orchestrator import report, usage
    from swarm_orchestrator.config import load

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    cfg = load(project_dir=str(project))
    cfg.ensure_dirs()
    log = logutil.Log(cfg.supervisor_log)
    log.line("SUPERVISOR-START pid=1 driver=bare")
    log.line("CLAIM P0 slot=0")
    log.line("LAUNCH P0 slot=0")
    log.close()
    logutil.rotate(cfg.supervisor_log)
    log = logutil.Log(cfg.supervisor_log)
    log.line("EVENT done P0 ok freed_slot=0 parked=False")
    log.close()

    runs, _ = report._read_log(cfg)
    assert runs["P0"][0].claimed is not None and runs["P0"][0].done_at is not None
    kinds = [e.kind for e in usage.Sources(cfg).events]
    assert "supervisor-start" in kinds and "done" in kinds
