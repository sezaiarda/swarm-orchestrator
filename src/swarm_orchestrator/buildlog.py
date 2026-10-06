"""The build gate's event log, holder records and run-time history.

``<machine dir>/buildsem/events.jsonl`` is append-only, one JSON object per
line, and is a contract other tools read. It is the machine's: every swarm's
builds are in it, each line naming its swarm. Every line has exactly these
keys::

    {"ts": <unix float>,
     "swarm": "<the swarm's slug>" or null, "swarm_name": "<its name>" or null,
     "event": "queued"|"start"|"end"|"bypass"|"preflight_fail"|"yield"|"unyield"
              |"passed"|"alone"|"left",
     "id": "<one per swarm build call>", "phase": <$SWARM_PHASE or null>,
     "pid": <int>, "slot": <int or null>, "cls": "heavy"|"light"|"gc",
     "argv": "<command, at most 300 chars>", "cwd": "<path>",
     "wait_s": <float on start and left, else null>,
     "run_s": <float on end, yield and unyield, else null>,
     "exit": <int or null on end, else null>,
     "idle_s": <float on yield and unyield, else null>,
     "hold": <true|false on start, else null>,
     "repo": "<the build's repo>" or null,
     "alone": <true|false on queued and start, else null>,
     "why": "<a few words>" or null, "by": "<build id>" or null}

``swarm`` is the slug of the swarm the build belongs to (its state dir's name,
unique on the machine: ``swarm ls``) and ``swarm_name`` what its owner calls it,
on every event of that build, whoever wrote the line: a ``yield`` is written by
whichever waiter measured the holder, often another swarm's. Both are null only
for a holder whose record could not be read.

- ``queued``: a heavy command joined the queue. ``pid`` is the waiting
  ``swarm build`` process (the build does not exist yet).
- ``start``: it got slot ``slot``; ``pid`` is the build process itself (its whole
  process tree holds the slot), ``wait_s`` how long it queued. ``hold`` is true
  for a build started with ``swarm build --hold``: it keeps its slot however
  idle it looks, and never gets a ``yield``.
- ``bypass``: a light command (or any command while the gate is off) started
  without queueing; ``pid`` is the command's process, ``slot`` null.
- ``end``: the command finished; ``run_s`` since its start, ``exit`` its exit code
  (a signal N is ``128+N``; ``124`` is ``--timeout``). ``exit`` is **null** for a
  synthetic end the gate writes when it finds a holder that died without
  logging one (``swarm build`` itself was SIGKILLed); then ``run_s`` is measured
  up to when that was noticed.
- ``preflight_fail``: the command was refused before queueing (missing program,
  directory or file); nothing ran.
- ``yield``: a running build was set aside as idle (:mod:`buildidle`): it keeps
  running but no longer counts against ``max_concurrent``. Same ``id``, ``pid``
  and ``slot`` as its ``start``; ``idle_s`` is how long its whole process tree
  had been quiet, ``run_s`` how long it had run. Written by the waiter that
  measured it.
- ``unyield``: a set-aside build is working again and counts again; ``idle_s``
  is how long it was set aside, ``why`` what the measurement saw. A build that
  ends while set aside gets no ``unyield``: its ``end`` closes the stretch.

- ``passed``: under ``[build].pair`` a pairing rule held this waiter back and
  a younger one started ahead of it: ``id``, ``pid`` and ``argv`` are the
  waiter's, ``why`` the rule ("same repo as slot 0 (lib)"), ``by`` the ``id``
  of the build that started. A waiter is passed at most ``[build].overtake``
  times.
- ``alone``: a running build was found to hold a command that runs alone (an
  image build a script started): from now on nothing starts beside it. Same
  ``id``, ``pid`` and ``slot`` as its ``start``; ``why`` says what was seen.

**gc's turn** is logged with ``cls`` ``"gc"`` (see *gc takes its turn* in
:mod:`buildsem`): ``queued`` when it joins the queue, ``start`` when it holds
the whole gate (``slot`` null: it holds every slot; ``wait_s`` how long it
queued), ``end`` when it lets go (``run_s`` is how long no build could run;
``exit`` 0, 1 if it failed, null for a gc that was killed), or, instead of a
``start``:

- ``left``: its wait ran out and it left the queue, having held nothing;
  ``wait_s`` is how long it waited, ``why`` what was still in the way.

``pid`` is the process gc runs in (the supervisor, for the automatic one),
``swarm`` the swarm whose build output it sweeps, and ``alone`` is true. A gc
is not a build: readers that count or measure builds
skip ``cls`` ``"gc"``.

A ``left`` with ``cls`` ``"heavy"`` is a build the swarm runs itself and can
wait only so long for (:func:`buildsem.slot` with ``wait_s``): no slot came in
that time, it left the queue and nothing ran.

``repo`` is the repository a queued build works in, by the swarm's name for it
(its path in that swarm's project, ``.`` for the project's own; see
:mod:`buildpair`),
on every event of that build; null when it has none, and for a light command.
``alone`` says whether the pairing rules made it run with no build beside it
(always false under ``[build].pair = "any"``), and ``why`` then says why.

A ``queued`` with no ``start`` for the same ``id`` gave up (killed) while
waiting. A ``start`` with no ``end`` whose ``pid`` is gone died unrecorded; the
gate writes the synthetic ``end`` as soon as it notices.

Each line is one ``write(2)`` on an ``O_APPEND`` descriptor, so concurrent writers
never interleave. At 20 MB the file is renamed to ``events.jsonl.1`` (replacing
the previous one) and a new one is started.
"""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import statistics
import time
from pathlib import Path

from . import freezer
from .config import Config

EVENTS = "events.jsonl"
ROTATE_BYTES = 20 * 1024 * 1024
ARGV_MAX = 300
_TAIL_BYTES = 2 * 1024 * 1024
_HISTORY_N = 20
_DEFAULT_RUN_S = 120.0
_DEFAULT_GC_S = 10.0
_WRAPPERS = frozenset({"timeout", "env", "nice", "nohup", "time", "stdbuf", "ionice", "exec"})


def events_path(cfg: Config) -> Path:
    return cfg.buildsem_dir / EVENTS


def swarm_of(cfg: Config) -> dict:
    """Whose build it is, as every ticket, record and event of the gate says it:
    ``swarm`` is the slug (the state dir's name, as ``swarm ls`` lists it) and
    ``swarm_name`` what the owner calls the swarm."""
    return {"swarm": cfg.state_dir.name, "swarm_name": cfg.name}


def whose(rec: dict) -> dict:
    """:func:`swarm_of` as a ticket or a holder's record carries it: for an
    event about a build that may be another swarm's."""
    return {"swarm": rec.get("swarm"), "swarm_name": rec.get("swarm_name")}


def mine(cfg: Config, rec: dict) -> bool:
    """Is this ticket, record or event of ``cfg``'s own swarm?"""
    return rec.get("swarm") == cfg.state_dir.name


def run_of(cfg: Config, rec: dict) -> Config | freezer.Run | None:
    """The run a ticket or record belongs to, for what the gate has to ask of
    it (does it stand frozen, for how long did it): this one's config, or a
    neighbour's state dir, which is beside this one's under the name the record
    gives. None when the record names no swarm."""
    slug = rec.get("swarm")
    if not isinstance(slug, str) or not slug or "/" in slug or slug in (".", ".."):
        return None
    if slug == cfg.state_dir.name:
        return cfg
    return freezer.Run(cfg.state_dir.parent / slug)


def frozen(cfg: Config, rec: dict) -> bool:
    """Does the swarm this ticket or record belongs to stand frozen right now
    (``swarm freeze``)? Its processes are alive and hold what they held, and
    do nothing until its ``swarm thaw``."""
    run = run_of(cfg, rec)
    return run is not None and freezer.stands(run)


def who(entry: dict, unnamed: str = "-") -> str:
    """A build as every view of the gate names it: its phase, with its swarm's
    name in front when the entry says it is another swarm's (``mine`` false):
    ``W3``, ``[glasheim] W3``. ``unnamed`` stands for a build with no phase."""
    phase = str(entry.get("phase") or unnamed)
    if entry.get("mine", True) or not entry.get("swarm"):
        return phase
    return f"[{entry.get('swarm_name') or entry['swarm']}] {phase}"


def who_text(cfg: Config, rec: dict) -> str:
    """:func:`who` for a ticket, record or event, which says whose it is but
    not whether that is the swarm reading it."""
    return who({**rec, "mine": mine(cfg, rec)})


def argv_text(argv: list[str] | str) -> str:
    text = argv if isinstance(argv, str) else shlex.join(argv)
    return text[:ARGV_MAX]


def event(cfg: Config, kind: str, *, id: str, phase: str | None, pid: int,
          slot: int | None, cls: str, argv: list[str] | str, cwd: str,
          wait_s: float | None = None, run_s: float | None = None,
          exit: int | None = None, idle_s: float | None = None,
          hold: bool | None = None, repo: str | None = None, alone: bool | None = None,
          why: str | None = None, by: str | None = None, ts: float | None = None,
          who: dict | None = None) -> None:
    """Append one event line. Never raises: the log must not break a build.
    ``who`` (:func:`whose`) names the build's swarm when the event is about a
    build that is not the writer's own; without it the writer's is named."""
    rec = {
        "ts": round(ts if ts is not None else time.time(), 3),
        **(swarm_of(cfg) if who is None else whose(who)), "event": kind, "id": id,
        "phase": phase, "pid": pid, "slot": slot, "cls": cls, "argv": argv_text(argv),
        "cwd": str(cwd),
        "wait_s": round(wait_s, 3) if wait_s is not None else None,
        "run_s": round(run_s, 3) if run_s is not None else None,
        "exit": exit,
        "idle_s": round(idle_s, 3) if idle_s is not None else None,
        "hold": hold, "repo": repo, "alone": alone, "why": why, "by": by,
    }
    line = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
    path = events_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate(path)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def _maybe_rotate(path: Path) -> None:
    try:
        if path.stat().st_size < ROTATE_BYTES:
            return
    except OSError:
        return
    lock = os.open(path.with_name(EVENTS + ".lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:  # someone else may have rotated while we waited
            if path.stat().st_size >= ROTATE_BYTES:
                os.replace(path, path.with_name(EVENTS + ".1"))
        except OSError:
            pass
    finally:
        os.close(lock)


def _tail_lines(path: Path, limit: int) -> tuple[list[str], int]:
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - limit)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return [], 0
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start and lines:
        lines = lines[1:]  # the first line was cut in the middle
    return lines, len(data)


def read_events(cfg: Config, limit: int = _TAIL_BYTES) -> list[dict]:
    """The most recent events (about ``limit`` bytes' worth), oldest first."""
    path = events_path(cfg)
    lines, got = _tail_lines(path, limit)
    if got < limit:
        older, _ = _tail_lines(path.with_name(EVENTS + ".1"), limit - got)
        lines = older + lines
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


# -- run-time history -------------------------------------------------------
def where(cfg: Config, cwd: str) -> str:
    """Where a command ran, comparable across phases: the path inside a phase's
    worktree (or inside the project), so ``wt/A/lib`` and ``wt/B/lib`` match."""
    p = Path(cwd)
    for root, skip in ((cfg.wt_dir, 1), (cfg.project_dir, 0)):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts[skip:]
        return "/".join(parts) or "."
    return p.name


def family(text: str) -> str:
    """The command's shape: its first three words that are not options,
    assignments or wrapper arguments (``cargo nextest run``)."""
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    out: list[str] = []
    for w in words:
        if not out and (w in _WRAPPERS or "=" in w or w[:1].isdigit()):
            continue
        if w.startswith("-") or "=" in w:
            continue
        out.append(os.path.basename(w) if not out else w)
        if len(out) == 3:
            break
    return " ".join(out)


class History:
    """Past heavy run times, by exact command and by command shape: this
    swarm's own, since a command and where it ran only mean something inside
    one project. What to assume for a command never seen (:attr:`default`) and
    how long a gc holds the gate come from every build on the machine."""

    def __init__(self, cfg: Config, events: list[dict] | None = None):
        self.cfg = cfg
        self.exact: dict[tuple[str, str], list[float]] = {}
        self.shape: dict[tuple[str, str], list[float]] = {}
        self.all: list[float] = []
        self.gc: list[float] = []  # how long each gc held the gate
        for e in events if events is not None else read_events(cfg):
            if e.get("event") != "end" or e.get("exit") is None:
                continue
            run = e.get("run_s")
            if not isinstance(run, (int, float)):
                continue
            if e.get("cls") == "gc":
                self.gc.append(float(run))
            if e.get("cls") != "heavy":
                continue
            self.all.append(float(run))
            if not mine(cfg, e):
                continue
            loc = where(cfg, str(e.get("cwd", "")))
            text = str(e.get("argv", ""))
            self.exact.setdefault((loc, text), []).append(float(run))
            self.shape.setdefault((loc, family(text)), []).append(float(run))

    def predict(self, argv: list[str] | str, cwd: str) -> float | None:
        """The usual run time of this command here, or ``None`` if unknown: the
        median of its own last runs (2+), else of its shape's (3+)."""
        text = argv_text(argv)
        loc = where(self.cfg, cwd)
        runs = self.exact.get((loc, text), [])[-_HISTORY_N:]
        if len(runs) >= 2:
            return statistics.median(runs)
        runs = self.shape.get((loc, family(text)), [])[-_HISTORY_N:]
        if len(runs) >= 3:
            return statistics.median(runs)
        return None

    @property
    def default(self) -> float:
        """What to assume for a command with no history."""
        runs = self.all[-200:]
        return statistics.median(runs) if runs else _DEFAULT_RUN_S


    @property
    def gc_default(self) -> float:
        """How long a gc usually holds the gate."""
        runs = self.gc[-_HISTORY_N:]
        return statistics.median(runs) if runs else _DEFAULT_GC_S


def fmt_s(seconds: float | None) -> str:
    """``42s``, ``3m05s``, ``1h12m``."""
    if seconds is None:
        return "?"
    s = int(max(0.0, seconds) + 0.5)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def short_cmd(text: str, width: int = 60) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"
