"""Pairing rules for the build gate: which builds may run side by side.

``[build].max_concurrent = 2`` lets any two heavy builds run at once. Where the
disk is what a build strains, two limits make that safe, and
``[build].pair = "distinct-repo"`` has the gate keep them (``"any"``, the
default, keeps neither):

1. **Never two builds in one repository.** Two builds of one repo share its
   build output (the per-repo cache ``[build].cache`` links into every
   worktree), so they would only fight over it.
2. **A build that must run alone runs alone**, both ways: it waits until no
   other build is alive, and nothing starts while it runs. That is a command
   ``[build].alone`` names (by default the container clients: an image build
   does its work in a daemon, straight onto the disk), a ``--hold`` build, and
   a build whose repo cannot be told.

**Which repo.** A build belongs to the repository its working directory is in,
under the name the swarm already uses for it: its path inside the project
(``lib``; ``.`` for the project's own repo), which is the lane it is scheduled
by and the directory its shared build cache is kept under. A phase's mirror
(``<state>/wt/<phase>/<path>``) has the project's layout, so the path inside
the mirror is the name; any other checkout is traced through its git common
directory, so every worktree of a repo is that repo. A repo outside the project
is named by its lane if ``[lanes].external`` declares it, else by its path. To that the repos the command itself names are added (a
``cd`` target, a ``--manifest-path``, a script's own place: what the classifier
checks before queueing): ``cargo test --manifest-path lib/Cargo.toml`` run from
the project root builds in ``lib``. Two builds collide when they share any
repo. A working directory in no git checkout has no repo: such a build runs
alone.

**Which builds are alone.** Read from the command before it queues
(:mod:`buildclass`): a heavy step that matches ``[build].alone``, through every
wrapper and script the classifier reads; or a script that could not be read to
the end and names such a program. What the command's text does not show (a
binary, ``make``, a script that calls a script) is caught while it runs: the
build's own ``swarm build`` looks at its process tree every
:data:`SCAN_S`, and whoever is about to start beside a running build looks at
that build's tree first. A process that matches marks the build alone from then
on (``buildsem/pair.json``, an ``alone`` event): nothing new starts beside it.
A build that was already running beside it is not stopped; that one overlap is
what a late discovery costs.

**Who counts.** Every build alive on a seat, including one set aside as idle:
it has stopped counting against ``max_concurrent``, but it may wake up, and
then it works in its repo again. Idle yield frees a slot, not a repo. Only a
build whose command has ended, with a process it left behind still holding the
seat, no longer counts for these rules.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import buildclass, buildidle
from .config import BUILD_PAIR_DISTINCT, Config
from .resources import ptree

STATE = "pair.json"
#: A running build's own ``swarm build`` looks at its process tree this often.
SCAN_S = 2.0
#: A command names at most this many places besides its working directory.
_MAX_PLACES = 12
_GIT_TIMEOUT_S = 10.0
HOLD = "started with --hold"
UNKNOWN_REPO = "its repo is unknown: not in a git checkout"
OLD_HOLDER = "an older swarm build started it"


def enabled(cfg: Config) -> bool:
    return cfg.build_pair == BUILD_PAIR_DISTINCT and cfg.build_max_concurrent >= 1


# -- which repo -------------------------------------------------------------
def _rev_parse(path: Path, *what: str) -> list[str] | None:
    try:
        proc = subprocess.run(["git", "-C", str(path), "rev-parse", *what],
                              capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False,
                              env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    lines = proc.stdout.splitlines()
    return lines if proc.returncode == 0 and len(lines) == len(what) else None


def _checkout(path: Path) -> tuple[Path, Path | None] | None:
    """``(git common dir, top of the working tree)`` for the checkout ``path``
    is in; the top is ``None`` in a bare repo. ``None``: not in a checkout."""
    got = _rev_parse(path, "--git-common-dir", "--show-toplevel")
    if got is None:
        got = _rev_parse(path, "--git-common-dir")
        if got is None:
            return None
    common = (path / got[0]).resolve()
    return common, (Path(got[1]).resolve() if len(got) > 1 else None)


def repo_of(cfg: Config, path: str | Path) -> str | None:
    """The repository ``path`` is in, by the swarm's name for it (see the module
    docstring); ``None`` when it is in no git checkout."""
    p = Path(path)
    while not p.is_dir():
        if p.parent == p:
            return None
        p = p.parent
    got = _checkout(p)
    if got is None:
        return None
    common, top = got
    if top is not None:
        try:
            parts = top.relative_to(cfg.wt_dir.resolve()).parts
        except ValueError:
            parts = ()
        if parts:  # <state>/wt/<phase>/<the repo's path in the project>
            return "/".join(parts[1:]) or "."
    root = common.parent if common.name == ".git" else common
    try:
        return root.relative_to(cfg.project_dir.resolve()).as_posix()
    except ValueError:
        pass
    for name, where in cfg.lanes_external.items():  # a repo the project names as a lane
        if Path(where).expanduser().resolve() == root:
            return name
    return str(root)


def repos(cfg: Config, cwd: str | Path, verdict: buildclass.Verdict | None = None
          ) -> list[str] | None:
    """Every repo a build works in: its working directory's first, then those
    the command names. ``None`` when the working directory has none."""
    first = repo_of(cfg, cwd)
    if first is None:
        return None
    out = [first]
    places: list[Path] = []
    for req in verdict.reqs if verdict is not None else ():
        if req.kind == "script" or (req.kind == "exe" and "/" not in req.paths[0]):
            continue
        full = Path(req.base) / os.path.expanduser(req.paths[0])
        place = full if req.kind == "dir" else full.parent
        if place not in places:
            places.append(place)
    for place in places[:_MAX_PLACES]:
        name = repo_of(cfg, place)
        if name is not None and name not in out:
            out.append(name)
    return out


def alone_why(cfg: Config, verdict: buildclass.Verdict | None, names: list[str] | None,
              hold: bool = False) -> str | None:
    """Why this build runs alone under the pairing rules, or ``None``."""
    if not enabled(cfg):
        return None
    if hold:
        return HOLD
    if verdict is not None and verdict.alone:
        return verdict.alone
    if names is None:
        return UNKNOWN_REPO
    return None


def repo_text(name: str | None) -> str:
    if name is None:
        return "unknown"
    return "the project's own repo" if name == "." else name


# -- the builds found alone while running -------------------------------------
def marks(cfg: Config) -> dict[str, str]:
    """``{build id: why}`` for the builds found, while running, to be alone."""
    try:
        data = json.loads((cfg.buildsem_dir / STATE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found = data.get("alone") if isinstance(data, dict) else None
    if not isinstance(found, dict):
        return {}
    return {bid: str(e.get("why") or "it runs alone") for bid, e in found.items()
            if isinstance(e, dict)}


def mark(cfg: Config, found: dict[str, str], live: list[dict], now: float) -> list[str]:
    """Record ``found`` (``{build id: why}``); returns the ids that were not
    marked yet. Marks of builds no longer alive are dropped. Call under
    ``queue.lock``."""
    path = cfg.buildsem_dir / STATE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        old = data["alone"] if isinstance(data["alone"], dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        old = {}
    alive = {h.get("id") for h in live} | set(found)
    kept = {bid: e for bid, e in old.items() if bid in alive}
    new = [bid for bid in found if bid not in kept]
    for bid in new:
        kept[bid] = {"ts": round(now, 3), "why": found[bid]}
    if new or len(kept) != len(old):
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        try:
            tmp.write_text(json.dumps({"alone": kept}, separators=(",", ":")),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
    return new


def _argv(pid: int, root: Path) -> list[str]:
    """A process's command line, starting at the program it is: a ``#!`` script
    runs as ``/bin/sh /path/docker build .``, and its name is ``docker``."""
    try:
        raw = (root / str(pid) / "cmdline").read_bytes()
    except OSError:
        return []
    argv = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
    name = ptree.comm(pid, root)
    for i, word in enumerate(argv[1:3], 1):
        if name and os.path.basename(word)[:15] == name:
            return argv[i:]
    return argv


def _alone_in(pids: set[int] | list[int], patterns: list[str], root: Path) -> str | None:
    for pid in sorted(pids):
        argv = _argv(pid, root)
        if not argv:
            continue
        name = buildclass.proc_alone(argv, ptree.cwd(pid, root) or None, patterns)
        if name:
            return f"`{name}` seen running in it"
    return None


def tree_alone(cfg: Config, pid: int, root: Path = ptree.PROC) -> str | None:
    """Does the process tree under ``pid`` hold a command that runs alone?"""
    if not cfg.build_alone:
        return None
    table = ptree.scan(root)
    return _alone_in(ptree.tree(ptree.children(table), {pid}), cfg.build_alone, root)


def scan(cfg: Config, holders: list[dict], root: Path = ptree.PROC) -> dict[str, str]:
    """``{build id: why}`` for each of ``holders`` (live seat records, with
    ``seat_path``) that has a command that runs alone among its processes."""
    if not cfg.build_alone or not holders:
        return {}
    table = ptree.scan(root)
    kids = ptree.children(table)
    out: dict[str, str] = {}
    for h in holders:
        if not h.get("id"):
            continue
        why = _alone_in(buildidle.members(h, h.get("seat_path"), table, kids, root),
                        cfg.build_alone, root)
        if why:
            out[h["id"]] = why
    return out


# -- the rules ----------------------------------------------------------------
def counted(holders: list[dict]) -> list[dict]:
    """The live holders the rules are about: all but those whose command has
    ended (a process it left behind is not a build)."""
    return [h for h in holders if not h.get("over")]


def holder_alone(h: dict, found: dict[str, str]) -> str | None:
    """Why nothing may start beside this holder, or ``None``."""
    if not h.get("id") or "repos" not in h:
        return OLD_HOLDER  # nothing is known of it: assume the worst
    if h.get("alone"):
        return str(h["alone"])
    if h.get("hold"):
        return HOLD
    if h["id"] in found:
        return found[h["id"]]
    if not isinstance(h["repos"], list) or not h["repos"]:
        return UNKNOWN_REPO
    return None


def _slot_text(h: dict) -> str:
    return f"slot {h['slot']}" if isinstance(h.get("slot"), int) else "a running build"


def waiters(tickets: list[dict]) -> list[dict]:
    """The tickets as the rules read them: a waiter that says nothing of its
    repos (queued by an older ``swarm build``, or with the rules off) is one
    whose repo is unknown, so it runs alone. Marks the tickets in place."""
    for t in tickets:
        if not t.get("alone") and not (isinstance(t.get("repos"), list) and t["repos"]):
            t["alone"] = UNKNOWN_REPO
    return tickets


def blocked(meta: dict, holders: list[dict], found: dict[str, str]) -> str | None:
    """Why the waiter ``meta`` (one of :func:`waiters`) may not start beside
    ``holders`` (already :func:`counted`), in a few words; ``None`` when the
    rules allow it."""
    if not holders:
        return None
    for h in holders:
        why = holder_alone(h, found)
        if why:
            return f"{_slot_text(h)} runs alone ({why})"
    if meta.get("alone"):
        return f"waits to run alone ({meta['alone']})"
    for h in holders:
        shared = [r for r in meta.get("repos") or () if r in h["repos"]]
        if shared:
            return f"same repo as {_slot_text(h)} ({repo_text(shared[0])})"
    return None


def blocked_all(tickets: list[dict], holders: list[dict], found: dict[str, str]
                ) -> dict[str, str]:
    """``{ticket id: why}`` for every waiter the rules hold back right now."""
    out = {}
    for t in tickets:
        why = blocked(t, holders, found)
        if why:
            out[t["id"]] = why
    return out
