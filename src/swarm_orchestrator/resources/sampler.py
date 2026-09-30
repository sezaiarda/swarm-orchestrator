"""The sampler: one light thread the supervisor owns.

Every :data:`FAST_S` it makes a cheap check — has the gate's event log grown,
does anything hold a build slot (``/proc/locks``)? — and takes a full sample
when a build is running, or every :data:`SLOW_S` when none is. A full sample
reads the host (:mod:`.host`), scans the process table once (:mod:`.ptree`),
measures each build's tree (:mod:`.builds`) and each swarm session's
processes, and appends one row to ``meters/resources.jsonl``. One second while
building because a peak sampled every few seconds reads low (a 2 s sampler
missed a quarter of a 4 s ramp); fifteen idle because nothing is moving.

Rarer work rides the same loop: the disk headroom every minute, directory
sizes every ten on a thread of their own (a ``du`` of a large build cache must
never hold up a sample), compaction every hour. The thread's own CPU time is
measured (``time.thread_time``), plus the ``du`` children's, and published in
``resources-now.json`` so the overhead is a number, not a promise.

It never raises into the supervisor: a failed sample is logged (rate-limited)
and the next one tried. It never signals anything either: an idle holder is
reported, not killed.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from .. import gc as gc_mod
from ..config import Config
from . import builds as builds_mod
from . import disk, host, ptree, store

FAST_S = 1.0
SLOW_S = 15.0
#: While sampling every second, the snapshot file is rewritten at most this often.
NOW_EVERY_S = 2.0
#: An idle holder is re-announced this often while it stays idle.
IDLE_REPING_S = 3600.0
_ERROR_LOG_S = 600.0


class Sampler:
    """Samples the host, the builds and the swarm's sessions (see module doc)."""

    def __init__(
        self,
        cfg_get: Callable[[], Config],
        notify: Callable[[str, str], None] | None = None,
        log: Callable[[str], None] | None = None,
        proc_root: Path = ptree.PROC,
        wsl: bool | None = None,
    ) -> None:
        self.cfg_get = cfg_get
        cfg = cfg_get()
        self.state_dir = cfg.state_dir
        self.notify = notify
        self.log = log or (lambda _m: None)
        self.proc = proc_root
        self.host = host.HostReader(proc_root)
        self.attr = ptree.Attributor(cfg.state_dir, proc_root)
        self.book = builds_mod.BuildBook(
            cfg.buildsem_dir, cfg.env_marker, proc_root,
            done_ids=store.recent_build_ids(cfg.state_dir))
        self.headroom = disk.Headroom(cfg.state_dir, cfg.resources_vhdx, wsl=wsl)
        self.boot = ptree.boot_time(proc_root)
        self.static = host.static(proc_root)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = time.time()
        self._last_sample = 0.0
        self._last_now = 0.0
        self._last_headroom = 0.0
        self._last_dirs = 0.0
        self._last_compact = time.time()
        self._disk: dict | None = None
        self._dirs: dict | None = None
        self._dirs_prev: dict | None = None
        self._dirs_thread: threading.Thread | None = None
        self._dirs_result: dict | None = None
        self._own: dict[int, tuple[int, int]] = {}  # pid -> (start, own cpu ticks)
        self._idle_pinged: dict[str, float] = {}
        self._err_at = 0.0
        self._wrote_host = False
        # the overhead ledger
        self.cpu_s = 0.0
        self.du_cpu_s = 0.0
        self.bytes_written = 0
        self.samples = 0
        self.last_row: dict | None = None
        self.last_workers: list[dict] = []
        self.last_infra: dict | None = None

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="resources", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def run(self) -> None:
        while not self._stop.is_set():
            t0 = time.thread_time()
            try:
                if self.cfg_get().resources_enabled:
                    self.step(time.time())
            except Exception as exc:  # noqa: BLE001 - never take the supervisor down
                now = time.time()
                if now - self._err_at >= _ERROR_LOG_S:
                    self._err_at = now
                    self.log(f"RESOURCES-ERROR {exc!r}")
            self.cpu_s += time.thread_time() - t0
            self._stop.wait(FAST_S)

    # -- one step ---------------------------------------------------------------
    def step(self, now: float) -> dict | None:
        """The cheap check, and a full sample when one is due. Returns the row."""
        if not self._wrote_host:
            self.bytes_written += store.append(
                store.meters_dir(self.state_dir) / store.FULL,
                {"ts": round(now, 3), "k": "host", **self.static})
            self._wrote_host = True
        changed = self.book.poll(now)
        self._collect_dirs(now)
        due = changed or self.book.busy() or now - self._last_sample >= SLOW_S
        row = self.sample(now) if due else None
        if now - self._last_compact >= store.COMPACT_EVERY_S:
            self._last_compact = now
            done = store.compact(self.state_dir, now)
            if done["aged"] or done["dropped"] or done["dropped_builds"]:
                self.log(f"RESOURCES-COMPACT {done}")
        if now - self._last_dirs >= disk.DIRS_EVERY_S and not self._dirs_running():
            self._last_dirs = now
            self._start_dirs()
        return row

    def sample(self, now: float) -> dict:
        cfg = self.cfg_get()
        idle_s = float(cfg.resources_idle_s)
        table = ptree.scan(self.proc)
        kids = ptree.children(table)
        labels = self.attr.label(table)
        row: dict = {"ts": round(now, 3), "k": "s"}
        row.update(self.host.sample(now))
        if now - self._last_headroom >= disk.HEADROOM_EVERY_S:
            self._last_headroom = now
            self._disk = self.headroom.read(now)
            if self._disk:
                row["headroom_gb"] = self._disk["headroom_gb"]
        self.book.measure(now, table, kids, row, idle_s)
        in_builds: set[int] = set()
        for b in self.book.active.values():
            in_builds |= b.tree
        groups = self._sessions(table, labels, in_builds, now)
        row["nb"] = len(self.book.active)
        if self.book.active:
            row["b"] = {bid: [round(b.cores, 2), round(b.anon_mb, 1)]
                        for bid, b in self.book.active.items()}
        workers = {k: v for k, v in groups.items() if k != ptree.INFRA}
        if workers:
            row["w"] = {k: [v["cores"], v["anon_mb"]] for k, v in workers.items()}
        if ptree.INFRA in groups:
            infra = groups[ptree.INFRA]
            row["x"] = [infra["cores"], infra["anon_mb"]]
        if self.book.busy():
            row["fast"] = 1
        path = store.meters_dir(self.state_dir) / store.FULL
        self.bytes_written += store.append(path, row)
        self._finish_builds()
        self._idle_holders(now, idle_s, cfg)
        self._last_sample = now
        self.samples += 1
        self.last_row = row
        self.last_workers = [{"label": k, **v} for k, v in sorted(workers.items())]
        self.last_infra = groups.get(ptree.INFRA)
        if not self.book.busy() or now - self._last_now >= NOW_EVERY_S:
            self._last_now = now
            store.write_now(self.state_dir, self.snapshot(now, idle_s))
        return row

    def _sessions(self, table: dict[int, ptree.Proc], labels: dict[int, str],
                  exclude: set[int], now: float) -> dict[str, dict]:
        """Per swarm session: cores (own CPU of its live processes since the last
        sample), anon and total RSS. Build trees are left out — they are counted
        as builds — and a session's CPU is its processes' own time, not their
        reaped children's, or a finished build would land on the worker whose
        shell reaped it."""
        by: dict[str, set[int]] = {}
        for pid, lab in labels.items():
            if pid not in exclude:
                by.setdefault(lab, set()).add(pid)
        dt = now - self._last_sample if self._last_sample else 0.0
        own: dict[int, tuple[int, int]] = {}
        out: dict[str, dict] = {}
        for lab, pids in by.items():
            ticks = 0
            for pid in pids:
                p = table[pid]
                own[pid] = (p.start, p.own)
                prev = self._own.get(pid)
                if prev is not None and prev[0] == p.start:
                    ticks += max(0, p.own - prev[1])
                elif self._last_sample and self.boot and (
                        self.boot + p.start / ptree.TICK) >= self._last_sample:
                    ticks += p.own  # started since the last sample: all of it is new
            use = ptree.usage(table, pids, self.proc)
            cores = ticks / ptree.TICK / dt if dt > 0 else 0.0
            out[lab] = {"cores": round(cores, 2), "anon_mb": round(use.anon_mb, 1),
                        "rss_mb": round(use.rss_mb, 1), "procs": use.procs}
        self._own = own
        return out

    def _finish_builds(self) -> None:
        path = store.meters_dir(self.state_dir) / store.BUILDS
        for b in self.book.take_finished():
            self.bytes_written += store.append(path, b.summary())
            self._idle_pinged.pop(b.id, None)

    def _idle_holders(self, now: float, idle_s: float, cfg: Config) -> None:
        for b in self.book.active.values():
            if not b.idle_for(now, idle_s):
                continue
            b.idle_flagged = True
            last = self._idle_pinged.get(b.id)
            if last is not None and now - last < IDLE_REPING_S:
                continue
            self._idle_pinged[b.id] = now
            msg = idle_message(b, now, idle_s)
            self.log(f"RESOURCES-IDLE-HOLDER {b.id} pid={b.pid} phase={b.phase}")
            if self.notify is not None:
                self.notify(f"idle-build:{b.id}", msg)

    # -- directory sizes (own thread) ------------------------------------------
    def _dirs_running(self) -> bool:
        return self._dirs_thread is not None and self._dirs_thread.is_alive()

    def _start_dirs(self) -> None:
        cfg = self.cfg_get()
        self._dirs_thread = threading.Thread(
            target=self._measure_dirs, args=(cfg,), name="resources-du", daemon=True)
        self._dirs_thread.start()

    def _measure_dirs(self, cfg: Config) -> None:
        t0 = time.time()
        row: dict = {"k": "dirs"}
        cpu = 0.0
        partial: list[str] = []
        for key, path in (("state_gb", cfg.state_dir), ("wt_gb", cfg.wt_dir)):
            size, c = disk.du(path)
            cpu += c
            row[key] = None if size is None else round(size / 2**30, 2)
            if size is None:
                partial.append(key)
        caches: dict[str, float | None] = {}
        try:
            roots = gc_mod.cache_roots(cfg)
        except OSError:
            roots = {}
        for real, names in roots.items():
            size, c = disk.du(real)
            cpu += c
            caches["+".join(names)] = None if size is None else round(size / 2**30, 2)
            if size is None:
                partial.append("+".join(names))
        row["cache"] = caches
        known = [v for v in caches.values() if v is not None]
        row["cache_gb"] = round(sum(known), 2) if known else (0.0 if not caches else None)
        row["partial"] = partial
        row["du_s"] = round(time.time() - t0, 1)
        row["du_cpu_s"] = round(cpu, 2)
        row["ts"] = round(time.time(), 3)
        self._dirs_result = row

    def _collect_dirs(self, now: float) -> None:
        row, self._dirs_result = self._dirs_result, None
        if row is None:
            return
        self.du_cpu_s += row.get("du_cpu_s") or 0.0
        prev = self._dirs
        for key in ("state_gb", "wt_gb", "cache_gb"):
            row[key.replace("_gb", "_growth_gb_h")] = disk.growth_gb_per_h(prev, row, key)
        self._dirs_prev, self._dirs = prev, row
        self.bytes_written += store.append(store.meters_dir(self.state_dir) / store.FULL, row)

    # -- the snapshot -------------------------------------------------------------
    def overhead(self, now: float) -> dict:
        wall = max(1.0, now - self.started)
        return {
            "since": round(self.started, 3), "samples": self.samples,
            "cpu_s": round(self.cpu_s, 2), "du_cpu_s": round(self.du_cpu_s, 2),
            "pct_core": round(100.0 * (self.cpu_s + self.du_cpu_s) / wall, 3),
            "bytes_written": self.bytes_written,
            "bytes_per_day": int(self.bytes_written * 86400 / wall),
        }

    def snapshot(self, now: float, idle_s: float) -> dict:
        builds = [b.now_row(now, idle_s) for b in self.book.active.values()]
        return {
            "ts": round(now, 3), "pid": os.getpid(), "started": round(self.started, 3),
            "source": self.book.source, "static": self.static,
            "host": self.last_row, "disk": self._disk, "dirs": self._dirs,
            "builds": builds, "queued": len(self.book.queued),
            "workers": self.last_workers, "infra": self.last_infra,
            "idle_holders": [b for b in builds if b["idle"]],
            "idle_s": idle_s, "sampler": self.overhead(now),
            "files": store.sizes(self.state_dir),
        }


def idle_message(b: builds_mod.Build, now: float, idle_s: float) -> str:
    what = (b.argv or "a build").split()
    short = " ".join(what[:4]) + (" …" if len(what) > 4 else "")
    return (
        f"swarm: a build has held a build slot for {int((now - b.started) // 60)} min"
        f" and used under 1% of a core for the last {int(idle_s // 60)} min"
        f" ({short}, phase {b.phase or '?'}, pid {b.pid}). Other builds queue behind it."
        " Nothing was stopped; `swarm resources` shows it."
    )
