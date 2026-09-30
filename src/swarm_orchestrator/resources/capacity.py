"""Could the gate or the worker count go up? The arithmetic, from history.

The question is always the same shape: at the observed 95th percentile, what
would N concurrent builds and M workers need, against what this host has? So:

    memory needed = other + M x worker + N x build        (anon MiB, p95 each)
    cores needed  =         M x worker + N x build        (cores, p95 each)

where *build* is a heavy build's peak anon memory (its whole process tree) and
its average cores, *worker* is one swarm session's own processes (its builds
excluded; they are the *build* term), and *other* is everything on the host that
is neither (the owner's own programs, the swarm's supervisor and dashboards).
A scenario **fits** when memory needed stays under :data:`MEM_BUDGET` of RAM —
the rest is left to page cache (build IO lives in it) and the kernel — and cores
needed stay under the core count. Raising ``[build].jobs`` is estimated by
scaling a build's memory linearly with its jobs: an upper bound, since linking
does not parallelise, and said so.

Nothing here is a verdict when the data is thin: with fewer than
:data:`MIN_BUILDS` measured builds or :data:`MIN_WORKER_H` hours of worker
samples every scenario is marked ``thin`` and the text says so plainly.
"""

from __future__ import annotations

import math

from . import store

MEM_BUDGET = 0.85
MIN_BUILDS = 5
MIN_WORKER_H = 1.0
#: Pressure above these during builds means the host was already stalling.
PSI_MEM_FULL_HOT = 5.0
PSI_IO_FULL_HOT = 20.0


def pct(vals: list[float], q: float) -> float | None:
    """Nearest-rank percentile (``q`` in 0..100); ``None`` for no data."""
    vals = sorted(v for v in vals if isinstance(v, (int, float)))
    if not vals:
        return None
    k = max(0, min(len(vals) - 1, math.ceil(q / 100 * len(vals)) - 1))
    return vals[k]


def _is_worker(label: str) -> bool:
    return label.startswith("worker:")


def build_stats(rows: list[dict]) -> dict:
    measured = [r for r in rows if (r.get("samples") or 0) > 0 and r.get("cls", "heavy") == "heavy"]
    def col(key: str) -> list[float]:
        return [r[key] for r in measured if isinstance(r.get(key), (int, float))]

    jobs = [r["jobs"] for r in measured if isinstance(r.get("jobs"), int) and r["jobs"] > 0]
    return {
        "n": len(measured),
        "peak_anon_mb": {"p50": pct(col("peak_anon_mb"), 50), "p95": pct(col("peak_anon_mb"), 95),
                         "max": max(col("peak_anon_mb"), default=None)},
        "avg_cores": {"p50": pct(col("avg_cores"), 50), "p95": pct(col("avg_cores"), 95)},
        "peak_cores": {"p95": pct(col("peak_cores"), 95), "max": max(col("peak_cores"), default=None)},
        "run_s": {"p50": pct(col("run_s"), 50), "p95": pct(col("run_s"), 95)},
        "min_avail_mb": min(col("min_avail_mb"), default=None),
        "psi_mem_full_max": max((r.get("psi_max", {}).get("memf", 0) for r in measured), default=None),
        "psi_io_full_p95": pct([r.get("psi_max", {}).get("iof", 0) for r in measured], 95),
        "jobs": pct(jobs, 50),
    }


def wpct(pairs: list[tuple[float, float]], q: float) -> float | None:
    """Weighted percentile of ``(value, weight)`` pairs: a time-weighted one when
    the weight is the seconds a sample stands for."""
    pairs = sorted(p for p in pairs if p[1] > 0)
    total = sum(w for _, w in pairs)
    if not total:
        return None
    acc = 0.0
    for value, weight in pairs:
        acc += weight
        if acc >= q / 100 * total:
            return value
    return pairs[-1][0]


def session_stats(rows: list[dict]) -> dict:
    """Per worker (one session's own processes), and everything that is not the
    swarm, as time-weighted percentiles: a sample weighs the seconds since the
    one before it (at most a minute), a minute row a minute."""
    anon: list[tuple[float, float]] = []
    cores: list[tuple[float, float]] = []
    other: list[tuple[float, float]] = []
    seconds = 0.0
    prev_ts = None
    for r in rows:
        ts = r.get("ts") or 0.0
        if r.get("k") == "m":
            weight = 60.0
        else:
            weight = 1.0 if prev_ts is None else max(0.0, min(ts - prev_ts, 60.0))
        prev_ts = ts
        w = r.get("w") or {}
        mine = [v for k, v in w.items() if _is_worker(k)]
        for c, a in mine:
            anon.append((a, weight))
            cores.append((c, weight))
        if mine:
            seconds += weight
        host_anon = store.val(r, "anon_mb", "max")
        if host_anon is None:
            continue
        swarm = sum(v[1] for v in w.values()) + sum(v[1] for v in (r.get("b") or {}).values())
        if isinstance(r.get("x"), list):
            swarm += r["x"][1]
        other.append((max(0.0, host_anon - swarm), weight))
    return {
        "worker_hours": round(seconds / 3600, 2),
        "worker_anon_mb": {"p50": wpct(anon, 50), "p95": wpct(anon, 95),
                           "max": max((a for a, _ in anon), default=None)},
        "worker_cores": {"p50": wpct(cores, 50), "p95": wpct(cores, 95)},
        "other_anon_mb": {"p50": wpct(other, 50), "p95": wpct(other, 95)},
    }


def scenario(name: str, builds: int, workers: int, jobs: int | None, b: dict, s: dict,
             host: dict, jobs_seen: float | None) -> dict:
    build_mb = b["peak_anon_mb"]["p95"] or 0.0
    build_cores = b["avg_cores"]["p95"] or 0.0
    if jobs and jobs_seen:
        build_mb = build_mb * jobs / jobs_seen
        build_cores = min(float(jobs), build_cores * jobs / jobs_seen)
    worker_mb = s["worker_anon_mb"]["p95"] or 0.0
    worker_cores = s["worker_cores"]["p95"] or 0.0
    other_mb = s["other_anon_mb"]["p95"] or 0.0
    mem_total = host.get("mem_total_mb") or 0.0
    ncpu = host.get("ncpu") or 1
    need_mb = other_mb + workers * worker_mb + builds * build_mb
    need_cores = workers * worker_cores + builds * build_cores
    budget = MEM_BUDGET * mem_total
    thin = b["n"] < MIN_BUILDS or s["worker_hours"] < MIN_WORKER_H
    return {
        "name": name, "builds": builds, "workers": workers, "jobs": jobs,
        "need_mb": round(need_mb), "budget_mb": round(budget), "mem_total_mb": mem_total,
        "need_cores": round(need_cores, 1), "ncpu": ncpu,
        "fits_mem": need_mb <= budget if mem_total else None,
        "fits_cpu": need_cores <= ncpu,
        "math": (f"{other_mb:.0f} other + {workers} x {worker_mb:.0f} worker"
                 f" + {builds} x {build_mb:.0f} build = {need_mb:.0f} MiB"
                 f" vs {MEM_BUDGET:.0%} of {mem_total:.0f} = {budget:.0f} MiB;"
                 f" cores {workers} x {worker_cores:.2f} + {builds} x {build_cores:.2f}"
                 f" = {need_cores:.1f} of {ncpu}"),
        "thin": thin,
    }


def analyse(build_rows: list[dict], history: list[dict], host: dict, max_concurrent: int,
            jobs: int, max_workers: int) -> dict:
    b = build_stats(build_rows)
    s = session_stats(history)
    jobs_seen = b["jobs"] or (jobs or None)
    cur_b = max(1, max_concurrent)
    scenarios = [
        scenario("now", cur_b, max_workers, None, b, s, host, jobs_seen),
        scenario("2 builds", max(2, cur_b), max_workers, None, b, s, host, jobs_seen),
        scenario("8 workers", cur_b, max(8, max_workers), None, b, s, host, jobs_seen),
        scenario("2 builds + 8 workers", max(2, cur_b), max(8, max_workers), None, b, s,
                 host, jobs_seen),
    ]
    if jobs_seen:
        scenarios.append(scenario(f"jobs {int(jobs_seen) * 2}", cur_b, max_workers,
                                  int(jobs_seen) * 2, b, s, host, jobs_seen))
    notes: list[str] = []
    if b["n"] < MIN_BUILDS:
        notes.append(f"too thin: {b['n']} measured build(s); want at least {MIN_BUILDS}"
                     " before trusting a build figure")
    if s["worker_hours"] < MIN_WORKER_H:
        notes.append(f"too thin: {s['worker_hours']} h of worker samples; want at least"
                     f" {MIN_WORKER_H:g} h")
    if (b["psi_mem_full_max"] or 0) >= PSI_MEM_FULL_HOT:
        notes.append(f"memory pressure already reached {b['psi_mem_full_max']:.1f}% (full)"
                     " during a build: every task stalled on memory for that share of a second")
    if (b["psi_io_full_p95"] or 0) >= PSI_IO_FULL_HOT:
        notes.append(f"IO pressure during builds is high (p95 of per-build max:"
                     f" {b['psi_io_full_p95']:.1f}% full): a second concurrent build would"
                     " queue on the disk, not the CPU")
    return {"builds": b, "sessions": s, "host": host, "scenarios": scenarios, "notes": notes,
            "config": {"max_concurrent": max_concurrent, "jobs": jobs, "max_workers": max_workers},
            "mem_budget": MEM_BUDGET}
