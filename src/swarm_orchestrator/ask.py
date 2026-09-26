"""Asks: a waiting window where the owner answers review questions.

A phase that builds something for the owner to look at (mockups, a page, a
recording) finishes, and the owner's decisions live in separate ``owner-run``
ledger rows the swarm never launches. With the worker gone after a Telegram ping
about mockups, the owner has nowhere to type an answer. The fix: a small session
in its own tmux window that waits for the owner,
like a parked worker, where the owner answers.

``swarm ask --name N --rows R1,R2 --why "<one line>" "<brief>"`` writes
``<state>/ask/<N>.json`` and pokes the supervisor, which opens the **ask
session**: a Claude session in window ``ask:<N>`` of the swarm's tmux session,
pointed at ``prompts/ask.md`` and the brief. It shows the owner what to look at,
asks with AskUserQuestion, records the picks in the rows, and ends with
``swarm ask-done N "<outcome>"``, which closes the window, ends everything the
session started (``SWARM_SESSION_ID=ask:<N>``), stops the kept processes named
with ``--stop-keep``, and, under worktree isolation, lands its mirror
``ask-<N>`` through the ordinary merge queue.

An ask takes no worker slot and never times out; several can be open at once.
Its one Telegram ping goes out when its window opens. While one is open the run
does not finish: it waits on the owner, the way a parked phase does. ``swarm
down`` ends the session like every other; ``swarm up`` opens every ask that was
open and not done again, with the same brief, from its record.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import gitq
from . import launch as launch_mod
from . import procs
from . import resolver
from . import session as session_mod
from . import telegram, tmux
from .config import Config
from .logutil import Log

OPEN = "open"
DONE = "done"
#: The session kind in ``SWARM_SESSION_ID=ask:<name>``.
KIND = "ask"
#: The variable naming the ask a session is answering.
ASK_ENV = "SWARM_ASK"
#: The status an ask's mirror rides the integration queue under. Not a phase:
#: the supervisor lands it without recording anything done.
INTEG_STATUS = "ask"
#: The ``--why`` cap: one line on a phone screen and in a table row.
WHY_MAX = 120
#: How many finished asks ``swarm ask --list`` shows beneath the open ones.
RECENT = 10
#: Names become a tmux window (``ask:<name>``), a branch and a directory: no
#: ``.`` (tmux reads it as a pane separator), no ``/``.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
_ROW_SPLIT = re.compile(r"[,\s]+")


class AskError(Exception):
    """An ask refused: a bad name, why, rows or brief, or one already open."""


@dataclass
class Ask:
    name: str
    rows: list[str]
    why: str
    brief: str
    by: str = "owner"
    state: str = OPEN
    opened_at: float = 0.0
    #: The tmux session it opens in (the attach hint names it).
    session: str = ""
    #: ``ask-<name>`` under worktree isolation, once built.
    mirror: str = ""
    #: The one Telegram ping has gone (it never goes twice).
    pinged: bool = False
    #: When its window last opened (``0`` = never opened yet).
    window_at: float = 0.0
    #: Bare driver only: the session process, to tell alive from gone.
    pid: int = 0
    start_ticks: int | None = None
    outcome: str = ""
    attention: bool = False
    done_at: float = 0.0
    stop_keeps: list[str] = field(default_factory=list)

    @property
    def window(self) -> str:
        return window_name(self.name)

    @property
    def is_open(self) -> bool:
        return self.state == OPEN

    def age_s(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.opened_at)

    def attach(self) -> str:
        """How the owner reaches its window from a terminal."""
        return attach_command(self.session, self.name)

    def to_dict(self) -> dict:
        return asdict(self)


def window_name(name: str) -> str:
    return f"ask:{name}"


def attach_command(session: str, name: str) -> str:
    return f"tmux select-window -t {session}:{window_name(name)}"


def mirror_name(name: str) -> str:
    """The mirror (and ``swarm/<name>`` branch) an ask works in."""
    return f"ask-{name}"


# -- the records ------------------------------------------------------------
def ask_dir(cfg: Config) -> Path:
    return Path(cfg.state_dir) / "ask"


def path(cfg: Config, name: str) -> Path:
    return ask_dir(cfg) / f"{name}.json"


def brief_path(cfg: Config, name: str) -> Path:
    return ask_dir(cfg) / f"{name}.brief.md"


@contextmanager
def _locked(cfg: Config):
    """One writer at a time: ``swarm ask``, the opener thread and ``ask-done``."""
    folder = ask_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / ".lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read(p: Path) -> Ask | None:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        known = {f.name for f in fields(Ask)}
        return Ask(**{k: v for k, v in data.items() if k in known})
    except (OSError, ValueError, TypeError):
        return None


def _write(cfg: Config, ask: Ask) -> None:
    ask_dir(cfg).mkdir(parents=True, exist_ok=True)
    tmp = path(cfg, ask.name).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ask.to_dict(), indent=2) + "\n", encoding="utf-8")
    tmp.replace(path(cfg, ask.name))


def load(cfg: Config, name: str) -> Ask | None:
    return _read(path(cfg, name))


def load_all(cfg: Config) -> list[Ask]:
    """Every ask on record, open first, then newest first."""
    try:
        paths = sorted(ask_dir(cfg).glob("*.json"))
    except OSError:
        return []
    asks = [a for a in (_read(p) for p in paths) if a is not None]
    asks.sort(key=lambda a: (not a.is_open, -(a.done_at or a.opened_at)))
    return asks


def open_asks(cfg: Config) -> list[Ask]:
    return [a for a in load_all(cfg) if a.is_open]


def open_names(cfg: Config) -> list[str]:
    """The asks the run waits on (it does not finish while there are any)."""
    return sorted(a.name for a in open_asks(cfg))


def update(cfg: Config, name: str, **changes) -> Ask | None:
    """Read-modify-write one record under the lock; ``None`` if there is none."""
    with _locked(cfg):
        ask = load(cfg, name)
        if ask is None:
            return None
        for key, val in changes.items():
            setattr(ask, key, val)
        _write(cfg, ask)
    return ask


def parse_rows(text: str) -> list[str]:
    """``"coral-W1,coral-W2"`` (commas or spaces) to ``["coral-W1", "coral-W2"]``."""
    seen: list[str] = []
    for row in _ROW_SPLIT.split(text or ""):
        row = row.strip().strip("`")
        if row and row not in seen:
            seen.append(row)
    return seen


def check(name: str, rows: list[str], why: str, brief: str) -> tuple[str, str]:
    """Validate an ask; return ``(why, brief)`` whitespace-collapsed."""
    if not _NAME.match(name or ""):
        raise AskError(f"bad name {name!r}: letters, digits, '_' or '-' (48 at most)")
    if not rows:
        raise AskError("--rows is required: the ledger row(s) the owner's answer settles")
    why = " ".join((why or "").split())
    if not why:
        raise AskError("--why is required: one plain line saying what the owner decides")
    if len(why) > WHY_MAX:
        raise AskError(f"--why is {len(why)} characters; keep it to one line of {WHY_MAX}")
    brief = (brief or "").strip()
    if not brief:
        raise AskError("the brief is required: what the owner looks at, and where")
    return why, brief


def create(cfg: Config, name: str, rows: list[str], why: str, brief: str,
           by: str | None = None, now: float | None = None) -> tuple[Ask, bool]:
    """Record an ask, ``(ask, reopened)``. Raises :class:`AskError`.

    A name that is open with its window alive is refused. One that is open with
    no live window (it would not open, or the owner closed it) takes the new
    brief and opens again; it keeps its ping, so the owner is not told twice. A
    finished name starts a fresh ask.
    """
    why, brief = check(name, rows, why, brief)
    now = time.time() if now is None else now
    with _locked(cfg):
        old = load(cfg, name)
        if old is not None and old.is_open and session_alive(cfg, old):
            raise AskError(f"ask {name} is already open in window {old.window}"
                           f" (`{old.attach()}`); pick another name")
        reopened = old is not None and old.is_open
        # The session name is a default: a worker's `swarm ask` runs in its
        # mirror, whose folder name is not the project's, so the supervisor
        # writes the one it runs under when it opens the window.
        ask = Ask(name=name, rows=list(rows), why=why, brief=brief,
                  by=by or who(), opened_at=old.opened_at if reopened else now,
                  session=(old.session if reopened else "") or cfg.session,
                  mirror=old.mirror if reopened else "",
                  pinged=old.pinged if reopened else False)
        _write(cfg, ask)
    return ask, reopened


def complete(cfg: Config, name: str, outcome: str, attention: bool,
             stop_keeps: list[str], now: float | None = None) -> Ask | None:
    """Mark an open ask done, durably, before anything is poked. ``None`` if the
    name is not an open ask."""
    with _locked(cfg):
        ask = load(cfg, name)
        if ask is None or not ask.is_open:
            return None
        ask.state = DONE
        ask.outcome = " ".join((outcome or "").split())
        ask.attention = attention
        ask.stop_keeps = list(stop_keeps)
        ask.done_at = time.time() if now is None else now
        _write(cfg, ask)
    return ask


def who() -> str:
    """Who opened it: the session's own marker, else the phase, else the owner."""
    return (os.environ.get(procs.SESSION_ENV)
            or (f"worker:{os.environ['SWARM_PHASE']}" if os.environ.get("SWARM_PHASE") else "owner"))


def opened_by_phase(ask: Ask) -> str | None:
    """The phase whose worker opened ``ask``, or ``None`` (anyone else)."""
    kind, _, ident = (ask.by or "").partition(":")
    return ident if kind == "worker" and ident else None


def rows_text(rows: list[str]) -> str:
    return ", ".join(rows) if rows else "(no rows)"


# -- liveness ---------------------------------------------------------------
def session_alive(cfg: Config, ask: Ask) -> bool:
    """Is its window (tmux) or its process (bare) still there?"""
    if cfg.driver == "tmux":
        win = tmux.find_window(ask.session or cfg.session, ask.window)
        return win is not None and tmux.window_alive(win)
    return bool(ask.pid) and procs.same(ask.pid, ask.start_ticks)


# -- the session ------------------------------------------------------------
def ask_command(cfg: Config, name: str, cwd: Path) -> str:
    """Shell command that runs the ask session in ``cwd``.

    Built by the worker's own builder, like the operator's, so it gets what a
    worker gets (in-process teammates, the meters tap, the effort level), with
    ``[ask].model`` or else the master's. ``[ask].cmd`` replaces the session.
    """
    if cfg.ask_cmd:
        return f"cd {shlex.quote(str(cwd))} && {cfg.ask_cmd}"
    model = cfg.ask_model or cfg.master_model
    base = ("claude" + (f" --model {shlex.quote(model)}" if model else "")
            + f" -n {shlex.quote(window_name(name))}")
    return launch_mod._worker_shell(cfg, mirror_name(name), cwd, base)


#: Env overrides the supervisor may have been started with, forwarded so a
#: ``swarm`` run inside the session resolves the same config.
_FORWARD_ENV = ("SWARM_GIT_ISOLATION", "SWARM_GIT_MAIN", "SWARM_GIT_REPOS",
                "SWARM_READY_MARKER")


def ask_env(cfg: Config, name: str, mirror: Path | None) -> dict[str, str]:
    env = launch_mod.session_env(cfg, mirror, tmp=mirror_name(name),
                                 session=f"{KIND}:{name}")
    for key in _FORWARD_ENV:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env[ASK_ENV] = name
    env["SWARM_PROJECT"] = str(cfg.project_dir)
    return env


def swarm_form(cfg: Config) -> str:
    """How the session runs ``swarm``: in a mirror a bare one resolves the mirror."""
    return f"swarm --project-dir {shlex.quote(str(cfg.project_dir))}"


def brief_text(cfg: Config, ask: Ask, cwd: Path | None) -> str:
    """The session's whole brief, written to a file (a long line typed into the
    pane is folded into "[Pasted text]" and never submits)."""
    prompt = resolver.prompt_path("ask.md")
    swarm = swarm_form(cfg)
    where = (
        f"Your cwd {cwd} is your own full-workspace mirror (branch swarm/{mirror_name(ask.name)}):"
        " edit and commit the ledger there; the swarm merges it when you run ask-done."
        if cwd is not None
        else f"Your cwd is the project itself, {cfg.project_dir}: commit there right away."
    )
    command_file = cfg.command_file or "(none configured)"
    return (
        f"Read {prompt} and follow it exactly. You are the swarm's ask session"
        f" `{ask.name}`, in tmux window {ask.window}, for the project at {cfg.project_dir}."
        f" The owner's answer is wanted on these ledger rows: {rows_text(ask.rows)}"
        f" (the ledger is {cfg.ledger}). Why, in one line: {ask.why}. {where}"
        f" The project's worker command file, whose rules for ticking a row and any"
        f" ledger gate you follow: {command_file}. Run swarm commands as `{swarm} <command>`."
        f"\n\nThe brief, in full:\n\n{ask.brief}\n\n"
        f"When every answer is recorded and committed, run"
        f' `{swarm} ask-done {ask.name} "<one-line outcome>"`, adding'
        " `--stop-keep <keep-name>` for each kept process that only existed for this"
        " review, and `--attention` only if the owner still has something to do."
    )


def pane_line(cfg: Config, name: str) -> str:
    return (f"You are the swarm's ask session {name}. Read your full brief in"
            f" {brief_path(cfg, name)} first, then do exactly what it says.")


def prepare_mirror(cfg: Config, ask: Ask, log: Log) -> Path | None:
    """The ask's own mirror (reused when one is left), or ``None`` in place.
    Raises :class:`gitq.GitError` when a fresh one cannot be built."""
    if cfg.git_isolation != "worktree":
        return None
    name = mirror_name(ask.name)
    where = cfg.wt_dir / name
    if where.is_dir() and gitq.branch_exists(cfg.project_dir, f"swarm/{name}"):
        log.line(f"ASK-MIRROR-REUSE {ask.name} {where}")
    else:
        where = gitq.worktree_add(cfg, name, log)
    update(cfg, ask.name, mirror=name)
    launch_mod.pretrust_dir(where, log)
    return where


def _spawn_tmux(cfg: Config, ask: Ask, cwd: Path, env: dict[str, str], log: Log) -> str | None:
    """Open window ``ask:<name>`` and start the session; ``None`` (reason logged)
    when it would not start. A window left from an earlier attempt goes first."""
    stale = tmux.find_window(cfg.session, ask.window)
    if stale is not None:
        tmux.kill_window(stale)
    try:
        win = tmux.new_window(cfg.session, ask.window)
        pane = tmux.list_panes(win)[0]
        tmux.respawn_pane(pane, ask_command(cfg, ask.name, cwd), env=env)
    except (subprocess.CalledProcessError, IndexError, OSError) as exc:
        log.line(f"ASK-SPAWN-FAIL {ask.name} {exc}")
        return None
    if cfg.ask_cmd:
        return win
    prompt = resolver.prompt_path("ask.md")
    if not prompt.is_file():
        log.line(f"ASK-PROMPT-MISSING {prompt}")
    elif not launch_mod.await_ready(cfg, pane, log):
        log.line(f"ASK-READY-TIMEOUT {ask.name}")
    elif not tmux.send_submit(pane, pane_line(cfg, ask.name)):
        log.line(f"ASK-SUBMIT-LOST {ask.name}")
    else:
        return win
    tmux.kill_window(win)
    return None


def _spawn_bare(cfg: Config, ask: Ask, cwd: Path, env: dict[str, str], log: Log) -> bool:
    """The headless driver: a detached process, like a bare worker."""
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", ask_command(cfg, ask.name, cwd)],
            env={**os.environ, **env}, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except OSError as exc:
        log.line(f"ASK-SPAWN-FAIL {ask.name} {exc}")
        return False
    update(cfg, ask.name, pid=proc.pid, start_ticks=procs.start_ticks(proc.pid))
    return True


def open_session(cfg: Config, name: str, log: Log, *, reason: str = "") -> bool:
    """Open the ask's session (mirror, window, prompt), then ping once.

    Idempotent: an ask that is done, or whose window is alive, is left alone.
    Blocks for a ``claude`` boot, so the supervisor runs it on a thread.
    """
    ask = load(cfg, name)
    if ask is None or not ask.is_open:
        log.line(f"ASK-OPEN-SKIP {name} not open")
        return False
    if ask.session != cfg.session:
        ask = update(cfg, name, session=cfg.session) or ask
    if session_alive(cfg, ask):
        log.line(f"ASK-OPEN-SKIP {name} already open")
        return True
    failure = ""
    try:
        mirror = prepare_mirror(cfg, ask, log)
    except gitq.GitError as exc:
        mirror, failure = None, f"its mirror could not be built: {exc}"
    if not failure:
        cwd = mirror or cfg.project_dir
        try:
            brief_path(cfg, name).write_text(brief_text(cfg, ask, mirror) + "\n", encoding="utf-8")
        except OSError as exc:
            failure = f"its brief could not be written: {exc}"
    if not failure:
        env = ask_env(cfg, name, mirror)
        ok = (_spawn_tmux(cfg, ask, cwd, env, log) is not None if cfg.driver == "tmux"
              else _spawn_bare(cfg, ask, cwd, env, log))
        failure = "" if ok else "the session would not start"
    if not failure:
        update(cfg, name, window_at=time.time())
    log.line(f"ASK-OPEN {name} {'ok' if not failure else 'failed: ' + failure}"
             + (f" ({reason})" if reason else ""))
    ping_once(cfg, name, failure)
    return not failure


def ping_once(cfg: Config, name: str, failure: str = "") -> bool:
    """The ask's one Telegram ping (necessary: it waits on the owner). True if
    this call sent it; never a second time, however often the window opens."""
    with _locked(cfg):
        ask = load(cfg, name)
        if ask is None or ask.pinged:
            return False
        ask.pinged = True
        _write(cfg, ask)
    verb = "waits" if len(ask.rows) == 1 else "wait"
    session = ask.session or cfg.session
    if failure:
        head = (f"swarm: {rows_text(ask.rows)} {verb} on you, but the window {ask.window}"
                f" would not open ({failure}); `swarm ask --reopen {name}` tries again")
    else:
        head = (f"swarm: {rows_text(ask.rows)} {verb} on you: answer in tmux window"
                f" {ask.window} (`tmux attach -t {session}`)")
    telegram.notify(cfg.telegram_notify, f"{head}\n{ask.why}", kind="ask", phase=name,
                    source="ask.open_session", state_dir=cfg.state_dir)
    return True


def close_session(cfg: Config, name: str, log: Log) -> None:
    """End the session: its window, then everything carrying ``ask:<name>``."""
    ask = load(cfg, name)
    session = (ask.session if ask else "") or cfg.session
    if cfg.driver == "tmux":
        win = tmux.find_window(session, window_name(name))
        if win is not None:
            tmux.kill_window(win)
    session_mod.reap_session(cfg, KIND, name, log)
    launch_mod.drop_session_tmp(cfg, mirror_name(name))
    log.line(f"ASK-CLOSE {name}")


# -- the mirror ---------------------------------------------------------------
def integration_for(cfg: Config, name: str) -> str | None:
    """The mirror a finished ask's commits land from, or ``None`` (in place, or gone)."""
    if cfg.git_isolation != "worktree":
        return None
    mirror = mirror_name(name)
    return mirror if gitq.branch_exists(cfg.project_dir, f"swarm/{mirror}") else None


def mirror_plan(cfg: Config) -> dict[str, str]:
    """``{mirror: "keep" | "integrate"}`` for ``swarm up``'s reconcile.

    An ask mirror has no completion sentinel, so without this it reads as an
    interrupted phase and is discarded with the owner's recorded picks in it. An
    open ask keeps its mirror for the reopen; a done one whose merge never
    happened is landed."""
    if cfg.git_isolation != "worktree":
        return {}
    plan: dict[str, str] = {}
    for ask in load_all(cfg):
        mirror = mirror_name(ask.name)
        if ask.is_open:
            plan[mirror] = "keep"
        elif gitq.branch_exists(cfg.project_dir, f"swarm/{mirror}"):
            plan[mirror] = "integrate"
    return plan


# -- what people read -----------------------------------------------------------
def age_text(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def line(ask: Ask, now: float | None = None) -> str:
    """One human line per ask, shared by ``ask --list``, ``status`` and doctor."""
    if ask.is_open:
        return (f"{ask.name} [{rows_text(ask.rows)}] waiting on you"
                f" {age_text(ask.age_s(now))} — {ask.why}  (attach: {ask.attach()})")
    when = age_text(max(0.0, (time.time() if now is None else now) - ask.done_at))
    return (f"{ask.name} [{rows_text(ask.rows)}] done {when} ago — "
            f"{ask.outcome or '(no outcome given)'}")


def owner_run_unasked(cfg: Config, graph: dict[str, set[str]], done: dict[str, str],
                      in_flight: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """Owner-run rows (``[tasks].exclude``) whose dependencies have landed, that
    are not done and that no open ask names — the Overseer's to notice."""
    from . import ledger as ledger_mod

    excluded = set(cfg.exclude)
    if not excluded:
        return []
    asked = {row for a in open_asks(cfg) for row in a.rows}
    ready = ledger_mod.ready(graph, done, set(in_flight), set())
    return [p for p in ready if p in excluded and p not in asked]
