"""The big-picture pass: one project doc, kept current, so a worker need not survey.

Every worker used to open with a read-only survey of the whole project — the
ledger, the roadmap, the repo statuses — before its first edit, and then read
most of the same files again itself. This module moves that work to the swarm
and makes it periodic: every ``[big_picture].every`` integrated phases (or once
the doc is ``max_age_h`` old and something has landed since) the supervisor
starts one helper session that rewrites a single bounded document in the
project (``[big_picture].doc``, default ``docs/BIG-PICTURE.md``): where the
project stands, what just landed, what is next and eligible, the cross-repo
contracts and hot spots, and the conventions workers keep rediscovering.

The swarm hands workers nothing. The doc is an ordinary file on the target
branch, so every mirror branched after it lands has it, and a worker reads it
the way it reads any other doc.

The session never touches the project. It runs in its own tmux window
(``big-picture``), in a scratch directory under the state dir, reads the project
by path, writes its draft to ``<state>/bigpic/<id>.md`` and ends with ``swarm
big-picture-done``. The *swarm* then commits the draft to the target branch under
the umbrella's integration lock (:func:`land`); a tree that is mid-merge, on
another branch or holding the owner's own edits to the doc is waited out, never
overwritten. One pass at a time; it takes no worker slot and nothing waits on it.

What the policy remembers lives in ``<state>/bigpic/state.json`` so a restart
neither loses the counter nor forgets a draft still waiting to land.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from . import gitq
from . import launch as launch_mod
from . import ovdigest
from . import pushowed
from . import resolver
from . import session as session_mod
from . import state as state_mod
from . import statuses, tmux
from .config import Config
from .logutil import Log

DIRNAME = "bigpic"
#: The session kind in ``SWARM_SESSION_ID=bigpic:<id>``.
KIND = "bigpic"
#: Its tmux window, and the name its temp dir and meters go under.
WINDOW = "big-picture"
PROMPT = "big_picture.md"
#: The variables naming the pass a session runs and where its draft goes.
PASS_ENV = "SWARM_BIG_PICTURE_PASS"
DRAFT_ENV = "SWARM_BIG_PICTURE_DRAFT"
#: The hard cap on the doc. Every worker may read it, so what it costs is paid
#: per phase; the prompt aims well under this, and a draft over it never lands.
MAX_BYTES = 16_000
#: A pass still running after this is ended; the next trigger starts a fresh one.
TIMEOUT_S = 2400
#: After a pass that did not produce a doc, the counter waits this long before it
#: may start another, so a session that cannot start is not retried in a loop.
RETRY_S = 900
#: The longest summary carried on the FIFO line (well under a pipe's atomic write).
SUMMARY_MAX = 300

# How a pass ended (``Memory.last_status``).
LANDED = "landed"
UNCHANGED = "unchanged"
WAITING = "waiting to land"
TOO_BIG = "too big"
NO_DRAFT = "no draft"
TIMEOUT = "timed out"
DIED = "session ended early"
NO_START = "would not start"
INTERRUPTED = "interrupted"


def bigpic_dir(cfg: Config) -> Path:
    return cfg.state_dir / DIRNAME


def memory_path(cfg: Config) -> Path:
    return bigpic_dir(cfg) / "state.json"


def draft_path(cfg: Config, pid: str) -> Path:
    return bigpic_dir(cfg) / f"{pid}.md"


def brief_path(cfg: Config, pid: str) -> Path:
    return bigpic_dir(cfg) / f"{pid}.brief.md"


def workdir(cfg: Config) -> Path:
    """The session's cwd: a scratch dir, so nothing it does lands in the project."""
    return bigpic_dir(cfg) / "work"


def enabled(cfg: Config) -> bool:
    return bool(cfg.big_picture_every or cfg.big_picture_max_age_h)


@dataclass
class Memory:
    """What the policy carries across wakes and restarts.

    Every field defaults and unknown keys are dropped on load, so a file from
    another version never stops the policy."""

    #: ``done`` as last observed; ``None`` = never observed (baseline, no count).
    seen_done: dict[str, str] | None = None
    #: Phases integrated since the last pass started.
    since: int = 0
    requested: bool = False
    #: The running pass, and when it started.
    live: str = ""
    live_at: float = 0.0
    #: The umbrella's main at the current pass's start; ``last_head`` once it lands.
    live_head: str = ""
    #: The counted phases the current pass took; given back if it produces nothing.
    live_taken: int = 0
    #: When the last pass that produced a doc started, and the umbrella then:
    #: the next pass reads what landed after it.
    last_at: float = 0.0
    last_head: str = ""
    #: How the most recent pass ended, when, and what it said.
    last_status: str = ""
    last_end: float = 0.0
    last_summary: str = ""
    last_commit: str = ""
    #: A pass whose draft waits for the project tree to be ready, and why.
    land_pending: str = ""
    wait_why: str = ""
    #: No automatic pass before this (after one that produced nothing).
    retry_at: float = 0.0
    #: The age clock's start when no pass has ever run (the first wake).
    anchor: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "Memory":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def load(cfg: Config) -> Memory:
    try:
        data = json.loads(memory_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Memory()
    return Memory.from_dict(data) if isinstance(data, dict) else Memory()


def save(cfg: Config, mem: Memory) -> None:
    """Atomic write; a failure costs memory across a restart, never the run."""
    try:
        bigpic_dir(cfg).mkdir(parents=True, exist_ok=True)
        tmp = memory_path(cfg).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(mem), indent=1), encoding="utf-8")
        os.replace(tmp, memory_path(cfg))
    except OSError:
        pass


# -- the policy (pure) -------------------------------------------------------
def observe(mem: Memory, done: dict[str, str]) -> int:
    """Count the phases newly integrated in ``done``; the first look baselines."""
    seen = mem.seen_done
    mem.seen_done = dict(done)
    if seen is None:
        return 0
    new = sum(1 for p, s in done.items() if s in statuses.INTEGRATES and seen.get(p) != s)
    mem.since += new
    return new


def due(cfg: Config, mem: Memory, now: float, *, paused: bool = False,
        doc_exists: bool = True) -> str | None:
    """Why a pass should start now, or ``None``. A manual request beats a pause
    and the back-off; nothing starts while a pass runs or a draft waits to land."""
    if mem.live or mem.land_pending:
        return None
    if mem.requested:
        return "requested"
    if not enabled(cfg) or paused or now < mem.retry_at:
        return None
    if not mem.last_at and not doc_exists:
        return "the doc does not exist yet"
    every = cfg.big_picture_every
    if every and mem.since >= every:
        return f"{mem.since} phase(s) integrated since the last pass"
    age = _age_deadline(cfg, mem)
    if age is not None and now >= age:
        return f"the doc is over {cfg.big_picture_max_age_h}h old"
    return None


def _age_deadline(cfg: Config, mem: Memory) -> float | None:
    """When the doc turns ``max_age_h`` old — only once something has landed since."""
    hours = cfg.big_picture_max_age_h
    start = mem.last_at or mem.anchor
    if not hours or not start or mem.since <= 0:
        return None
    return start + hours * 3600


# -- landing -----------------------------------------------------------------
def check_draft(text: str) -> str | None:
    """Why a draft must not land, or ``None``."""
    if not text.strip():
        return NO_DRAFT
    if len(text.encode("utf-8")) > MAX_BYTES:
        return TOO_BIG
    return None


def land(cfg: Config, text: str, message: str, log: Log) -> tuple[str, str]:
    """Commit ``text`` as the doc on the umbrella's target branch.

    Returns ``(outcome, detail)``: :data:`LANDED` with the commit, :data:`UNCHANGED`,
    or :data:`WAITING` with why the tree is not ready. Committed and pushed the
    way every swarm-authored file is (:func:`gitq.commit_to_target`): only the
    doc's path, never while a merge is under way, and a failed push is owed.
    """
    repo = cfg.project_dir
    rel = cfg.big_picture_doc
    target = (repo / rel).resolve()
    if not target.is_relative_to(repo.resolve()) or target == repo.resolve():
        return WAITING, f"[big_picture].doc {rel!r} is not a file inside the project"

    def write() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")

    result = gitq.commit_to_target(cfg, [rel], write, message, log)
    if result.status == gitq.HELD:
        return WAITING, result.reason
    if result.status == gitq.UNCHANGED:
        return UNCHANGED, ""
    if result.push is not None:
        pushowed.settle(cfg, "big picture", {repo: result.push}, log)
    if result.status == gitq.UNVERSIONED:
        return LANDED, ""
    return LANDED, gitq._git(repo, "rev-parse", "--short", "HEAD").stdout.strip()


def _head(cfg: Config) -> str:
    try:
        return gitq._git(cfg.project_dir, "rev-parse", cfg.git_main_branch,
                         check=False).stdout.strip()
    except gitq.GitError:
        return ""


# -- the session -------------------------------------------------------------
def command(cfg: Config, cwd: Path) -> str:
    """Shell command that runs the session: a worker-built ``claude`` on
    ``[big_picture].model``, or ``[big_picture].cmd`` in its place."""
    if cfg.big_picture_cmd:
        return f"cd {shlex.quote(str(cwd))} && {cfg.big_picture_cmd}"
    model = cfg.big_picture_model
    base = ("claude" + (f" --model {shlex.quote(model)}" if model else "")
            + f" -n {WINDOW}")
    return launch_mod._worker_shell(cfg, WINDOW, cwd, base)


def env(cfg: Config, pid: str) -> dict[str, str]:
    out = launch_mod.session_env(cfg, None, tmp=WINDOW, session=f"{KIND}:{pid}")
    out[PASS_ENV] = pid
    out[DRAFT_ENV] = str(draft_path(cfg, pid))
    out["SWARM_PROJECT"] = str(cfg.project_dir)
    return out


def brief_text(cfg: Config, pid: str, mem: Memory, st: state_mod.State) -> str:
    """The session's brief: where things are, what changed since the last pass."""
    swarm = f"swarm --project-dir {shlex.quote(str(cfg.project_dir))}"
    doc = cfg.project_dir / cfg.big_picture_doc
    if mem.last_at:
        since = (f"The last pass ran {time.strftime('%Y-%m-%d %H:%M', time.localtime(mem.last_at))}"
                 + (f"; the umbrella's {cfg.git_main_branch} was at {mem.last_head} then"
                    f" (`git log {mem.last_head}..{cfg.git_main_branch}` shows what landed since)."
                    if mem.last_head else "."))
    else:
        since = "This is the first pass: there is no earlier doc to start from."
    lines = [
        f"Read {resolver.prompt_path(PROMPT)} and follow it exactly. You are the swarm's"
        f" big-picture pass {pid}, for the project at {cfg.project_dir}.",
        "",
        f"- The doc you rewrite: {doc} ({'exists' if doc.is_file() else 'does not exist yet'})."
        " Read it, never edit it: the swarm commits your draft there.",
        f"- Write your draft to: {draft_path(cfg, pid)}. At most {MAX_BYTES} bytes; a longer"
        " one is not landed.",
        f"- The ledger: {cfg.project_dir / cfg.ledger}. Excluded (owner-run) rows:"
        f" {', '.join(cfg.exclude) or 'none'}.",
        f"- What every worker runs first: {cfg.project_dir / cfg.command_file}.",
        f"- {since}",
        f"- Run swarm commands as `{swarm} <command>`; `{swarm} context` gives the ready set.",
        "",
        "## Finished since the last pass",
        "",
    ]
    finished = ovdigest.finished_since(cfg, st, mem.last_at)
    if not finished:
        lines.append("(none recorded)")
    for row in finished:
        lines.append(f"- {row['phase']} [{row['status']}] {_clip(row['recap'] or row['note'], 300)}")
        for note in row["notes"][:4]:
            lines.append(f"  - {note['kind']}: {_clip(note['text'], 200)}")
    lines += [
        "",
        f'When the draft is written, run `{swarm} big-picture-done "<one-line summary>"`.',
    ]
    return "\n".join(lines) + "\n"


def pane_line(cfg: Config, pid: str) -> str:
    return (f"You are the swarm's big-picture pass {pid}. Read your full brief in"
            f" {brief_path(cfg, pid)} first, then do exactly what it says.")


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# -- what people read --------------------------------------------------------
def status_text(cfg: Config, mem: Memory, now: float | None = None) -> str:
    """One plain line for ``swarm status``."""
    view = web_view(cfg, mem)
    now = time.time() if now is None else now
    age = f" {_ago(now - view['at'])} ago" if view["at"] else ""
    if mem.live:
        age = f" ({_ago(now - mem.live_at)})"
    words, _, rest = view["text"].partition(" · next")
    return f"big picture: {words}{age}" + (f" · next{rest}" if rest else "")


def web_view(cfg: Config, mem: Memory) -> dict:
    """The pass's state in plain words, plus the moment they date from.

    The words carry no age, so a quiet board does not change (and wake a phone)
    just because time passed; the reader ages ``at`` itself."""
    if mem.live:
        return {"text": "refreshing now", "at": mem.live_at}
    if mem.land_pending:
        return {"text": f"a new doc is waiting to land — {mem.wait_why or 'the project is busy'}",
                "at": 0.0}
    off = "" if enabled(cfg) else "off · "
    if not mem.last_status:
        return {"text": f"{off}no pass yet", "at": 0.0}
    if mem.last_status in (LANDED, UNCHANGED):
        text = f"{off}refreshed"
    else:
        text = f"{off}last pass {mem.last_status}"
    if cfg.big_picture_every:
        text += f" · next after {max(0, cfg.big_picture_every - mem.since)} more phase(s)"
    return {"text": text, "at": mem.last_end}


def short_text(mem: Memory, now: float | None = None) -> str:
    """A few words for the dashboard's headline; ``""`` before the first pass."""
    now = time.time() if now is None else now
    if mem.live:
        return "big picture refreshing"
    if mem.land_pending:
        return "big picture waiting to land"
    if not mem.last_end:
        return ""
    text = f"big picture {_ago(now - mem.last_end)} ago"
    return text if mem.last_status in (LANDED, UNCHANGED) else f"{text} ({mem.last_status})"


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


# -- the supervisor's side -----------------------------------------------------
class Runner:
    """Owns the pass for one supervisor: trigger, spawn, end, land.

    Everything but the spawn runs on the supervisor's loop thread. The spawn (a
    ``claude`` boot) runs on a thread that reports back through the FIFO as
    ``big-picture-spawned <id> ok|failed``, like a launch does.
    """

    def __init__(self, cfg: Config, log: Log) -> None:
        self.cfg = cfg
        self.log = log
        self.mem = load(cfg)
        self._spawned = False
        self._proc: subprocess.Popen | None = None  # bare driver only

    def save(self) -> None:
        save(self.cfg, self.mem)

    def recover(self) -> None:
        """At supervisor start: no session outlives the supervisor that started it."""
        if self.mem.live:
            self.log.line(f"BIGPIC-INTERRUPTED {self.mem.live}")
            self._close(self.mem.live)
            self._ended(INTERRUPTED, time.time(), produced=False)
            self.save()

    # -- the wake ----------------------------------------------------------
    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        st = state_mod.read(self.cfg)
        before = asdict(self.mem)
        if not self.mem.anchor:
            self.mem.anchor = now
        observe(self.mem, st.done)
        if self.mem.live:
            if now - self.mem.live_at >= TIMEOUT_S:
                self.log.line(f"BIGPIC-TIMEOUT {self.mem.live} after {TIMEOUT_S}s")
                self._close(self.mem.live)
                self._ended(TIMEOUT, now, produced=False)
            elif self._spawned and not self._alive():
                self.log.line(f"BIGPIC-DIED {self.mem.live}")
                self._close(self.mem.live)
                self._ended(DIED, now, produced=False)
        elif self.mem.land_pending:
            self._land(self.mem.land_pending, now)
        else:
            doc = self.cfg.project_dir / self.cfg.big_picture_doc
            why = due(self.cfg, self.mem, now, paused=st.paused, doc_exists=doc.is_file())
            if why:
                self._start(why, st, now)
        if asdict(self.mem) != before:
            self.save()

    def next_deadline(self, now: float | None = None) -> float | None:
        """The next moment a timer could make something happen (future only)."""
        now = time.time() if now is None else now
        if self.mem.live:
            stamps = [self.mem.live_at + TIMEOUT_S]
        else:
            stamps = [s for s in (_age_deadline(self.cfg, self.mem),) if s is not None]
            if self.mem.retry_at and stamps:
                stamps = [max(s, self.mem.retry_at) for s in stamps]
        future = [s for s in stamps if s > now]
        return min(future) if future else None

    def request(self) -> None:
        if self.mem.live:
            self.log.line(f"BIGPIC-NOW-IGNORED {self.mem.live} is running")
            return
        self.mem.requested = True
        self.log.line("BIGPIC-REQUESTED")
        self.save()

    def handle(self, verb: str, args: list[str]) -> None:
        """``big-picture-now``, ``big-picture-spawned <id> <outcome>``,
        ``big-picture-done <id> <summary...>``."""
        if verb == "big-picture-now":
            self.request()
            self.tick()
        elif verb == "big-picture-spawned":
            self._on_spawned(args[0] if args else "?", args[1] if len(args) > 1 else "failed")
        elif verb == "big-picture-done":
            self._on_done(args[0] if args else "?", " ".join(args[1:]))
        else:
            self.log.line(f"UNKNOWN {verb}")

    def shutdown(self) -> None:
        """The supervisor is stopping: end a live session with it."""
        if self.mem.live:
            self._close(self.mem.live, background=False)
            self._ended(INTERRUPTED, time.time(), produced=False)
            self.save()

    # -- one pass ------------------------------------------------------------
    def _start(self, why: str, st: state_mod.State, now: float) -> None:
        pid = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
        mem = self.mem
        mem.live, mem.live_at, mem.live_head = pid, now, _head(self.cfg)
        mem.requested = False
        mem.live_taken, mem.since = mem.since, 0
        self._spawned = False
        try:
            workdir(self.cfg).mkdir(parents=True, exist_ok=True)
            brief_path(self.cfg, pid).write_text(brief_text(self.cfg, pid, mem, st),
                                                 encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - a brief that cannot be written ends the pass
            self.log.line(f"BIGPIC-BRIEF-FAIL {pid} {exc!r}")
            self._ended(NO_START, now, produced=False)
            return
        self.log.line(f"BIGPIC-START {pid} — {why}")
        self.save()
        threading.Thread(target=self._spawn, args=(pid,), name=f"bigpic:{pid}",
                         daemon=True).start()

    def _spawn(self, pid: str) -> None:
        """Thread body: open the window and start the session; report on the FIFO."""
        cfg = self.cfg
        outcome = "failed"
        try:
            cwd = workdir(cfg)
            launch_mod.pretrust_dir(cwd, self.log)
            if cfg.driver == "tmux":
                ok = self._spawn_tmux(pid, cwd)
            else:
                self._proc = subprocess.Popen(
                    ["/bin/sh", "-c", command(cfg, cwd)], env={**os.environ, **env(cfg, pid)},
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
                ok = True
            outcome = "ok" if ok else "failed"
        except Exception as exc:  # noqa: BLE001 - a thread must report, not vanish
            self.log.line(f"BIGPIC-SPAWN-ERROR {pid} {exc!r}")
        launch_mod._poke_fifo(cfg, f"big-picture-spawned {pid} {outcome}\n")

    def _spawn_tmux(self, pid: str, cwd: Path) -> bool:
        cfg = self.cfg
        stale = tmux.find_window(cfg.session, WINDOW)
        if stale is not None:
            tmux.kill_window(stale)
        win = tmux.new_window(cfg.session, WINDOW)
        pane = tmux.list_panes(win)[0]
        tmux.respawn_pane(pane, command(cfg, cwd), env=env(cfg, pid))
        if cfg.big_picture_cmd:
            return True
        if not resolver.prompt_path(PROMPT).is_file():
            self.log.line(f"BIGPIC-PROMPT-MISSING {resolver.prompt_path(PROMPT)}")
        elif not launch_mod.await_ready(cfg, pane, self.log):
            self.log.line(f"BIGPIC-READY-TIMEOUT {pid}")
        elif not tmux.send_submit(pane, pane_line(cfg, pid)):
            self.log.line(f"BIGPIC-SUBMIT-LOST {pid}")
        else:
            return True
        tmux.kill_window(win)
        return False

    def _on_spawned(self, pid: str, outcome: str) -> None:
        if pid != self.mem.live:
            self.log.line(f"BIGPIC-SPAWNED-STALE {pid} {outcome}")
            return
        if outcome == "ok":
            self._spawned = True
            self.log.line(f"BIGPIC-SPAWNED {pid}")
            return
        self.log.line(f"BIGPIC-SPAWN-FAILED {pid}")
        self._close(pid)
        self._ended(NO_START, time.time(), produced=False)
        self.save()

    def _on_done(self, pid: str, summary: str) -> None:
        if pid != self.mem.live:
            self.log.line(f"BIGPIC-DONE-IGNORED expected={self.mem.live or '-'} got={pid}")
            return
        now = time.time()
        self._close(pid)
        self.mem.last_summary = _clip(summary, SUMMARY_MAX)
        self.mem.live = ""
        self.mem.land_pending = pid
        self._land(pid, now)
        self.save()

    def _land(self, pid: str, now: float) -> None:
        try:
            text = draft_path(self.cfg, pid).read_text(encoding="utf-8")
        except OSError:
            text = ""
        bad = check_draft(text)
        if bad is not None:
            self.log.line(f"BIGPIC-NOT-LANDED {pid} {bad} ({len(text.encode('utf-8'))} bytes)")
            self.mem.land_pending = self.mem.wait_why = ""
            self._ended(bad, now, produced=False)
            return
        message = f"big picture: refresh {self.cfg.big_picture_doc}"
        if self.mem.last_summary:
            message += f" — {self.mem.last_summary}"
        try:
            outcome, detail = land(self.cfg, text, message, self.log)
        except (gitq.GitError, OSError) as exc:
            outcome, detail = WAITING, str(exc)
        if outcome == WAITING:
            if self.mem.wait_why != detail:
                self.log.line(f"BIGPIC-LAND-WAIT {pid} {detail}")
            self.mem.last_status, self.mem.wait_why = WAITING, detail
            return
        self.log.line(f"BIGPIC-{outcome.upper()} {pid} {self.cfg.big_picture_doc} {detail}".rstrip())
        self.mem.land_pending = self.mem.wait_why = ""
        if outcome == LANDED:
            self.mem.last_commit = detail
        self._ended(outcome, now, produced=True)

    def _ended(self, status: str, now: float, *, produced: bool) -> None:
        """Record how the pass ended. One that produced a doc moves the clocks;
        one that did not gives its counted phases back and backs off."""
        mem = self.mem
        if produced:
            mem.last_at = mem.live_at or now
            mem.last_head = mem.live_head
        else:
            mem.since += mem.live_taken
            mem.retry_at = now + RETRY_S
        mem.live, mem.live_taken = "", 0
        mem.last_status = status
        mem.last_end = now
        self._spawned = False
        self._proc = None

    def _alive(self) -> bool:
        if self.cfg.driver == "tmux":
            win = tmux.find_window(self.cfg.session, WINDOW)
            return win is not None and tmux.window_alive(win)
        return self._proc is not None and self._proc.poll() is None

    def _close(self, pid: str, *, background: bool = True) -> None:
        """End the session: its window, then everything carrying ``bigpic:<id>``."""
        if self.cfg.driver == "tmux":
            win = tmux.find_window(self.cfg.session, WINDOW)
            if win is not None:
                tmux.kill_window(win)
        elif self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
        session_mod.reap_session(self.cfg, KIND, pid, self.log, background=background)
        launch_mod.drop_session_tmp(self.cfg, WINDOW)
