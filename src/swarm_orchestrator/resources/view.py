"""``swarm resources``, the ``swarm status`` line and the doctor checks.

Everything here reads files the sampler wrote; nothing samples. The text view
has four parts — now, the last day as sparklines, the worst builds, capacity —
and ``--json`` returns the same data for scripts.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..config import Config
from . import capacity, host, store

#: The snapshot is rewritten at least every :data:`sampler.SLOW_S`; older than
#: this and the sampler is not running (or is stuck).
STALE_S = 120.0
SPARK = "▁▂▃▄▅▆▇█"
BUCKETS = 48


def fresh_now(state_dir: Path, now: float | None = None) -> tuple[dict | None, float | None]:
    """``(snapshot, age_s)``; the snapshot is ``None`` when there is none."""
    snap = store.read_now(state_dir)
    if snap is None:
        return None, None
    now = time.time() if now is None else now
    return snap, now - (snap.get("ts") or 0)


def _gb(mb: float | None) -> str:
    return "?" if mb is None else f"{mb / 1024:.1f}G"


def _f(v, fmt: str = "{:.1f}", none: str = "?") -> str:
    return none if v is None else fmt.format(v)


def _ago(s: float | None) -> str:
    if s is None:
        return "never"
    if s < 90:
        return f"{s:.0f}s ago"
    if s < 5400:
        return f"{s / 60:.0f}m ago"
    return f"{s / 3600:.1f}h ago"


# -- status and doctor ----------------------------------------------------------
def status_lines(cfg: Config, now: float | None = None) -> list[str]:
    """``swarm status``: one line, plus one per idle holder. The builds running
    and queued are the build gate's line (``build gate: …``), not repeated here."""
    snap, age = fresh_now(cfg.state_dir, now)
    if snap is None:
        return ["resources: no samples yet" + ("" if cfg.resources_enabled else " (off)")]
    if age is not None and age > STALE_S:
        return [f"resources: last sample {_ago(age)} (sampler not running)"]
    h = snap.get("host") or {}
    line = (f"resources: cpu {_f(h.get('cpu'), '{:.0f}')}% · MemAvailable"
            f" {_gb(h.get('avail_mb'))}")
    d = snap.get("disk") or {}
    if d.get("headroom_gb") is not None:
        line += f" · disk headroom {d['headroom_gb']:.0f}G"
    out = [line]
    for b in snap.get("idle_holders") or []:
        out.append(f"IDLE BUILD HOLDER: pid {b['pid']} ({b.get('phase') or '?'}) has held"
                   f" slot {b.get('slot')} for {b['age_s'] / 60:.0f} min at under 1% of a"
                   f" core: {b.get('argv', '')[:80]}")
    return out


def doctor_checks(cfg: Config, supervisor_alive: bool, now: float | None = None) -> list[tuple[str, str, str, str | None]]:
    """``(name, status, detail, fix)`` rows for ``swarm doctor``: the sampler's
    health, its files' sizes, and idle build holders."""
    rows: list[tuple[str, str, str, str | None]] = []
    snap, age = fresh_now(cfg.state_dir, now)
    sizes = store.sizes(cfg.state_dir)
    caps = {store.FULL: store.MAX_FULL_BYTES, store.AGG: store.MAX_AGG_BYTES,
            store.BUILDS: store.MAX_BUILDS_BYTES}
    size_text = ", ".join(f"{k} {v / 2**20:.1f}M" for k, v in sizes.items() if v)
    if not cfg.resources_enabled:
        rows.append(("resources.sampler", "ok", "off ([resources] enabled = false)", None))
    elif not supervisor_alive:
        rows.append(("resources.sampler", "ok",
                     f"the supervisor is down; last sample {_ago(age)}", None))
    elif snap is None or age is None or age > STALE_S:
        rows.append(("resources.sampler", "warn",
                     f"the supervisor is up but the last sample was {_ago(age)}",
                     f"grep RESOURCES {cfg.supervisor_log}"))
    else:
        o = snap.get("sampler") or {}
        rows.append(("resources.sampler", "ok",
                     f"sampling ({snap.get('source')} build source), last {_ago(age)};"
                     f" cost {o.get('pct_core', 0):.2f}% of one core,"
                     f" ~{(o.get('bytes_per_day') or 0) / 2**20:.0f}M/day written", None))
    over = [f"{k} {sizes[k] / 2**20:.0f}M > {cap / 2**20:.0f}M"
            for k, cap in caps.items() if sizes.get(k, 0) > cap * 1.5]
    rows.append(("resources.files", "warn" if over else "ok",
                 ("over their bound: " + "; ".join(over)) if over
                 else (size_text or "no files yet"),
                 "compaction runs hourly in the supervisor; check its log" if over else None))
    live = snap is not None and age is not None and age <= STALE_S
    idle = (snap.get("idle_holders") or []) if live else []
    if idle:
        detail = "; ".join(
            f"pid {b['pid']} ({b.get('phase') or '?'}, slot {b.get('slot')}) held"
            f" {b['age_s'] / 60:.0f} min at <1% of a core: {b.get('argv', '')[:60]}"
            for b in idle)
        rows.append(("resources.idle-build", "warn", detail,
                     "look at the build (it may wait on input or a lock); nothing was killed"))
    else:
        rows.append(("resources.idle-build", "ok", "no build holds a slot idle", None))
    return rows


# -- swarm resources ------------------------------------------------------------
def spark(vals: list[float | None]) -> str:
    known = [v for v in vals if v is not None]
    if not known:
        return " " * len(vals)
    lo, hi = min(known), max(known)
    span = hi - lo
    out = []
    for v in vals:
        if v is None:
            out.append(" ")
        elif span <= 0:
            out.append(SPARK[0])
        else:
            out.append(SPARK[min(len(SPARK) - 1, int((v - lo) / span * (len(SPARK) - 1) + 0.5))])
    return "".join(out)


#: The day's sparklines: label, how to read a row, how to fold a bucket, unit.
SERIES = (
    ("cpu %", lambda r: store.val(r, "cpu", "max"), max, "%"),
    ("MemAvailable", lambda r: store.val(r, "avail_mb", "min"), min, "MiB"),
    ("anon", lambda r: store.val(r, "anon_mb", "max"), max, "MiB"),
    ("page cache", lambda r: store.val(r, "cache_mb", "avg"), max, "MiB"),
    ("swap used", lambda r: store.val(r, "swap_mb", "max"), max, "MiB"),
    ("psi mem some", lambda r: store.psi_val(r, "mem"), max, "%"),
    ("psi io full", lambda r: store.psi_val(r, "iof"), max, "%"),
    ("disk write", lambda r: store.val(r, "wr_mbs", "max"), max, "MB/s"),
    ("builds", lambda r: store.val(r, "nb", "max"), max, ""),
)


def day_series(rows: list[dict], now: float, hours: float) -> list[dict]:
    start = now - hours * 3600
    width = hours * 3600 / BUCKETS
    out = []
    for label, read, fold, unit in SERIES:
        buckets: list[list[float]] = [[] for _ in range(BUCKETS)]
        allv: list[float] = []
        for r in rows:
            v = read(r)
            if v is None:
                continue
            i = int(((r.get("ts") or 0) - start) // width)
            if 0 <= i < BUCKETS:
                buckets[i].append(v)
                allv.append(v)
        vals = [fold(b) if b else None for b in buckets]
        out.append({"label": label, "unit": unit, "values": vals,
                    "min": min(allv) if allv else None,
                    "avg": sum(allv) / len(allv) if allv else None,
                    "max": max(allv) if allv else None})
    return out


def collect(cfg: Config, hours: float = 24.0, days: float = 30.0, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    snap, age = fresh_now(cfg.state_dir, now)
    day = store.history(cfg.state_dir, now - hours * 3600)
    month = store.history(cfg.state_dir, now - days * 86400)
    build_rows = store.builds(cfg.state_dir, now - days * 86400)
    static = (snap or {}).get("static") or host.static()
    cap = capacity.analyse(build_rows, month, static, cfg.build_max_concurrent,
                           cfg.build_jobs, cfg.max_workers)
    worst = sorted(build_rows, key=lambda r: -(r.get("peak_anon_mb") or 0))[:10]
    return {
        "now": snap, "age_s": age, "stale": snap is None or (age or 0) > STALE_S,
        "series": day_series(day, now, hours), "hours": hours,
        "builds": {"count": len(build_rows), "worst": worst},
        "dirs": store.dirs_rows(cfg.state_dir, now - hours * 3600)[-1:],
        "capacity": cap, "files": store.sizes(cfg.state_dir),
    }


def render(data: dict) -> str:
    lines: list[str] = []
    snap = data["now"]
    lines.append("NOW")
    if snap is None:
        lines.append("  no samples yet: the supervisor's sampler writes them while the swarm is up")
    else:
        if data["stale"]:
            lines.append(f"  (stale: last sample {_ago(data['age_s'])}; the sampler is not running)")
        lines.extend(_now_lines(snap))
    lines.append("")
    lines.append(f"LAST {data['hours']:g} H ({BUCKETS} buckets of"
                 f" {data['hours'] * 60 / BUCKETS:.0f} min; each bucket its worst value)")
    for s in data["series"]:
        if s["max"] is None:
            lines.append(f"  {s['label']:<13} no data")
            continue
        lines.append(f"  {s['label']:<13} {spark(s['values'])}  min {_num(s['min'])}"
                     f" avg {_num(s['avg'])} max {_num(s['max'])} {s['unit']}")
    lines.append("")
    lines.extend(_builds_lines(data["builds"]))
    lines.append("")
    lines.extend(_capacity_lines(data["capacity"]))
    return "\n".join(lines)


def _num(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v:.0f}" if abs(v) >= 100 else f"{v:.1f}"


def _now_lines(snap: dict) -> list[str]:
    st = snap.get("static") or {}
    h = snap.get("host") or {}
    psi = h.get("psi") or {}
    ncpu = st.get("ncpu") or 1
    cpu = h.get("cpu")
    out = [
        f"  host   cpu {_f(cpu, '{:.0f}')}% ({_f(None if cpu is None else cpu * ncpu / 100)}"
        f" of {ncpu} cores) · load {_f(h.get('load'), '{:.2f}')}"
        f" · psi cpu {_f(psi.get('cpu'))}% mem {_f(psi.get('mem'))}/{_f(psi.get('memf'))}%"
        f" io {_f(psi.get('io'))}/{_f(psi.get('iof'))}% (some/full)",
        f"  memory MemAvailable {_gb(h.get('avail_mb'))} of {_gb(st.get('mem_total_mb'))}"
        f" · anon {_gb(h.get('anon_mb'))} · page cache {_gb(h.get('cache_mb'))}"
        f" · swap used {_gb(h.get('swap_mb'))} of {_gb(st.get('swap_total_mb'))}",
    ]
    d = snap.get("disk") or {}
    disk = f"  disk   read {_f(h.get('rd_mbs'))} MB/s · write {_f(h.get('wr_mbs'))} MB/s"
    if d.get("wsl"):
        disk += (f" · headroom {d['headroom_gb']:.0f}G (vhdx slack {d['slack_gb']:.0f}G"
                 f" + Windows drive free {d['host_free_gb']:.0f}G)")
    elif d:
        disk += f" · free {d['headroom_gb']:.0f}G"
    out.append(disk)
    dirs = snap.get("dirs")
    if dirs:
        def one(key: str, name: str) -> str:
            g = dirs.get(f"{key}_growth_gb_h")
            grow = f" ({g:+.2f}G/h)" if g is not None else ""
            return f"{name} {_f(dirs.get(key + '_gb'))}G{grow}"
        out.append(f"  dirs   {one('state', 'state dir')} · {one('wt', 'worktrees')}"
                   f" · {one('cache', 'build caches')} · measured"
                   f" {_ago((snap.get('ts') or 0) - dirs.get('ts', 0))} in {dirs.get('du_s')}s")
    builds = snap.get("builds") or []
    out.append(f"  builds {len(builds)} running, {snap.get('queued', 0)} queued"
               f" (source: {snap.get('source')})")
    for b in builds:
        flag = "  IDLE" if b.get("idle") else ""
        out.append(f"    slot {b.get('slot')} pid {b['pid']} {b.get('phase') or '?'}"
                   f" {b['age_s'] / 60:.1f} min · {b['cores']:.2f} cores · anon"
                   f" {_gb(b['anon_mb'])} (peak {_gb(b['peak_anon_mb'])}) · {b['procs']} procs"
                   f"{flag}  {b.get('argv', '')[:50]}")
    workers = snap.get("workers") or []
    out.append(f"  sessions {len(workers)}")
    for w in workers:
        out.append(f"    {w['label']:<28} {w['cores']:.2f} cores · anon {_gb(w['anon_mb'])}"
                   f" · rss {_gb(w['rss_mb'])} · {w['procs']} procs")
    x = snap.get("infra")
    if x:
        out.append(f"    {'swarm itself':<28} {x['cores']:.2f} cores · anon {_gb(x['anon_mb'])}")
    c = snap.get("console")
    if c:
        out.append(f"    {'owner console':<28} {c['cores']:.2f} cores · anon {_gb(c['anon_mb'])}"
                   " (yours, not the swarm's)")
    o = snap.get("sampler") or {}
    out.append(f"  sampler cost {o.get('pct_core', 0):.2f}% of one core ({o.get('cpu_s', 0):.1f}s"
               f" + du {o.get('du_cpu_s', 0):.1f}s CPU over {o.get('samples', 0)} samples),"
               f" ~{(o.get('bytes_per_day') or 0) / 2**20:.1f} MiB/day written")
    return out


def _builds_lines(b: dict) -> list[str]:
    out = [f"BUILDS ({b['count']} finished in the window; worst peaks first)"]
    if not b["worst"]:
        out.append("  none recorded yet")
        return out
    out.append(f"  {'ended':<11} {'phase':<12} {'run':>6} {'cpu s':>7} {'avg':>5} {'peak':>5}"
               f" {'anon pk':>8} {'min avail':>9} {'psi m/io':>9} {'exit':>4}  command")
    for r in b["worst"]:
        psi = r.get("psi_max") or {}
        ended = time.strftime("%m-%d %H:%M", time.localtime(r.get("ended") or 0))
        out.append(
            f"  {ended:<11} {str(r.get('phase') or '?')[:12]:<12} {_dur(r.get('run_s')):>6}"
            f" {_num(r.get('cpu_s')):>7} {_num(r.get('avg_cores')):>5} {_num(r.get('peak_cores')):>5}"
            f" {_gb(r.get('peak_anon_mb')):>8} {_gb(r.get('min_avail_mb')):>9}"
            f" {_num(psi.get('memf')) + '/' + _num(psi.get('iof')):>9} {str(r.get('exit')):>4}"
            f"  {str(r.get('argv') or '')[:40]}")
    return out


def _dur(s: float | None) -> str:
    if s is None:
        return "?"
    return f"{s:.0f}s" if s < 120 else f"{s / 60:.0f}m"


def _capacity_lines(c: dict) -> list[str]:
    b, s, h = c["builds"], c["sessions"], c["host"]
    cfg = c["config"]
    out = [
        "CAPACITY (p95 of history; memory is anon, page cache left out)",
        f"  host: {h.get('ncpu')} cores, {_gb(h.get('mem_total_mb'))} RAM,"
        f" {_gb(h.get('swap_total_mb'))} swap · config: max_concurrent={cfg['max_concurrent']}"
        f" jobs={cfg['jobs']} max_workers={cfg['max_workers']}",
        f"  a heavy build ({b['n']} measured): peak anon p50 {_gb(b['peak_anon_mb']['p50'])}"
        f" p95 {_gb(b['peak_anon_mb']['p95'])} max {_gb(b['peak_anon_mb']['max'])};"
        f" avg cores p95 {_num(b['avg_cores']['p95'])}, peak cores p95 {_num(b['peak_cores']['p95'])};"
        f" run p50 {_dur(b['run_s']['p50'])}; lowest MemAvailable seen {_gb(b['min_avail_mb'])}",
        f"  a worker ({s['worker_hours']} h of samples, its builds excluded): anon p95"
        f" {_gb(s['worker_anon_mb']['p95'])}, cores p95 {_num(s['worker_cores']['p95'])}",
        f"  everything else on the host: anon p95 {_gb(s['other_anon_mb']['p95'])}",
        f"  a scenario fits when anon needed <= {c['mem_budget']:.0%} of RAM and cores needed"
        " <= cores; build memory for more jobs scales linearly (an upper bound)",
    ]
    for sc in c["scenarios"]:
        mem = "?" if sc["fits_mem"] is None else ("fits" if sc["fits_mem"] else "DOES NOT FIT")
        cpu = "fits" if sc["fits_cpu"] else "OVER"
        thin = "  [data too thin]" if sc["thin"] else ""
        out.append(f"  {sc['name']:<22} memory {mem}, cpu {cpu}{thin}")
        out.append(f"      {sc['math']}")
    for note in c["notes"]:
        out.append(f"  note: {note}")
    return out
