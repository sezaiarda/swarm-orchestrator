"""Everything that waits on the owner, and the one way a session says so.

A worker, an operator job or an Overseer pass that needs the owner runs
``swarm waiting <who> "<ask>"`` before it asks. The ask is what the owner reads on
their phone (:func:`telegram.ask`): what the session needs from them and why,
short enough for a notification, and refused when it is not. The supervisor arms
a park deadline. The session asks in full in its own window with AskUserQuestion
and waits. Past
``[worker].park_after`` the supervisor moves it, alive, to a window of its own
(:func:`state.wait_window`) and frees what it held — a worker slot, the operator
window, the master pane — so the swarm goes on; the session carries on there
once the owner answers, and ``swarm resumed <who> "<answer>"`` records it.

``<who>`` is a worker's phase, an operator job's id or ``overseer``; inside an
operator or Overseer session the id alone is enough, because the session's own
environment names the job or pass it is.

Owner-run rows (``[tasks].exclude``) are never sessions, so nothing would ever
ask about them: :func:`ledger.owner_rows` lists the ones that are ready and hold
other rows up, and :func:`ping_owner_rows` tells the owner once per row when it
starts to.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import doctor as doctor_mod
from . import launch as launch_mod
from . import ledger as ledger_mod
from . import notes as notes_mod
from . import opqueue
from . import operator as operator_mod
from . import ovrecord
from . import state as state_mod
from . import telegram, tmux
from .config import Config

#: What ``swarm waiting overseer`` names; any pass id works too.
OVERSEER = "overseer"
#: Where the ``waiting`` session's own window is, by kind (a worker's is its slot's).
HOME_WINDOW = {state_mod.OPERATOR: "operator", state_mod.OVERSEER: "overseer"}


class WaitError(Exception):
    """``swarm waiting`` / ``resumed`` refused: nothing of that name is running."""


def resolve(cfg: Config, who: str) -> str:
    """The ``waiting``/``parked`` key ``who`` means (:func:`state.waiter_key`).

    ``operator:<job>`` and ``overseer:<pass>`` are taken as written. A bare id is
    the operator job or the Overseer pass this session is (its environment says
    which), ``overseer`` is the live pass, and anything else is a worker's phase.
    """
    who = (who or "").strip()
    if ":" in who:
        return who
    pass_id = os.environ.get("SWARM_OVERSEER_PASS", "")
    if who == OVERSEER or (pass_id and who == pass_id):
        pid = pass_id or state_mod.read(cfg).overseer_pass
        if not pid:
            raise WaitError("no Overseer pass is running")
        return state_mod.waiter_key(state_mod.OVERSEER, pid)
    if who and os.environ.get(operator_mod.JOB_ENV) == who:
        return state_mod.waiter_key(state_mod.OPERATOR, who)
    return who


def where(cfg: Config, key: str, st: state_mod.State) -> str:
    """The tmux window the session is in right now ("" when there is none)."""
    if cfg.driver != "tmux":
        return ""
    if key in st.parked:
        return state_mod.wait_window(key)
    kind, ident = state_mod.waiter(key)
    if kind != state_mod.WORKER:
        return HOME_WINDOW[kind]
    pane = next((s.pane_id for s in st.slots if s.busy and s.phase == ident), None)
    try:
        return tmux.window_name_of(pane) if pane else ""
    except OSError:
        return ""


def waiting(cfg: Config, key: str, question: str) -> str:
    """Record that ``key``'s session waits on the owner and ask them.

    ``question`` is the ask as the owner's phone shows it. Raises
    :class:`telegram.TooLong` before anything is recorded when it does not fit,
    and :class:`WaitError` when an operator job or Overseer pass of that name is
    not running. Returns who it is, in words. A worker's ask goes every time
    (as it always did); an operator job's or a pass's only when the question is
    new, so a re-run cannot ring the owner twice.
    """
    question = telegram.short(cfg, question)
    kind, ident = state_mod.waiter(key)
    now = time.time()
    if kind == state_mod.OPERATOR:
        item, fresh = opqueue.wait_on_owner(cfg, ident, question, now)
        if item is None:
            raise WaitError(f"no running operator job {ident}")
        operator_mod.hold_lease(cfg, ident, item.lease_until)
        who = f"operator job {ident}"
    elif kind == state_mod.OVERSEER:
        rec = ovrecord.load_json(cfg, ident)
        st = state_mod.read(cfg)
        if rec is None or (st.overseer_pass != ident and key not in st.parked):
            raise WaitError(f"no Overseer pass {ident} is running")
        fresh = rec.question != question or bool(rec.answer)
        ovrecord.update(cfg, ident, question=question, asked_at=now, answer="")
        with state_mod.transaction(cfg) as s:
            if s.overseer_pass == ident:  # a pass waiting on a person is not hung
                s.overseer_deadline = now + opqueue.WAIT_LEASE_S
        who = "the Overseer"
    else:
        fresh = True
        who = ident
    if fresh:
        telegram.ask(cfg, question, kind="waiting", phase=key, source="cli.waiting")
    launch_mod._poke_fifo(cfg, f"waiting {key}\n")
    return who


def resumed(cfg: Config, key: str, answer: str) -> bool:
    """The owner answered: record it and cancel a pending park.

    Returns whether the answer was recorded as the owner's decision. Raises
    :class:`WaitError` for an operator job or pass that is not waiting.
    """
    answer = " ".join((answer or "").split())
    kind, ident = state_mod.waiter(key)
    parked = key in state_mod.read(cfg).parked
    if kind == state_mod.OPERATOR:
        # A parked job runs on outside the operator window's lease; only its
        # own end lets go of it.
        lease = opqueue.WAIT_LEASE_S if parked else opqueue.LEASE_S
        item = opqueue.resume(cfg, ident, answer, lease_s=lease)
        if item is None:
            raise WaitError(f"operator job {ident} is not waiting on the owner")
        operator_mod.hold_lease(cfg, ident, item.lease_until)
        recorded = notes_mod.owner_answer(cfg, opqueue.owning_phase(ident), item.answer,
                                          item.question)
    elif kind == state_mod.OVERSEER:
        rec = ovrecord.load_json(cfg, ident)
        if rec is None or not rec.question:
            raise WaitError("the Overseer is not waiting on the owner")
        ovrecord.update(cfg, ident, answer=answer or "(answered)")
        with state_mod.transaction(cfg) as s:
            if s.overseer_pass == ident:
                s.overseer_deadline = time.time() + cfg.overseer_timeout_s
        recorded = notes_mod.owner_answer(cfg, notes_mod.OVERSEER, answer, rec.question)
    else:
        recorded = notes_mod.owner_answer(cfg, ident, answer,
                                          doctor_mod.waiting_question(cfg, ident))
    launch_mod._poke_fifo(cfg, f"resumed {key}\n")
    return recorded is not None


# -- owner-run rows ------------------------------------------------------------
def current_owner_rows(cfg: Config, st: state_mod.State) -> list[tuple[str, int]]:
    """:func:`ledger.owner_rows` against the live ledger and ``state.json``."""
    path = cfg.project_dir / cfg.ledger
    graph = ledger_mod.load(path)
    flying = ({s.phase for s in st.busy_slots() if s.phase} | set(st.parked) | set(st.waiting)
              | st.batch_rows())
    done = ledger_mod.with_ticked(st.done, ledger_mod.load_ticked(path), flying)
    return ledger_mod.owner_rows(graph, done, set(cfg.exclude), flying)


def waits(cfg: Config, st: state_mod.State) -> list[str]:
    """What is stopped on the owner right now, in a few words each: sessions
    asking, a merge held with no resolver on it, rows only they can do."""
    out = []
    asking = len(st.on_owner())
    if asking:
        out.append(f"{asking} question{'' if asking == 1 else 's'}")
    if st.integ_blocked and f"resolve:{st.integ_blocked}" not in st.windows:
        out.append(f"a held merge ({st.integ_blocked})")
    try:
        mine = len(current_owner_rows(cfg, st))
    except (OSError, ValueError):
        mine = 0
    if mine:
        out.append(f"{mine} row{'' if mine == 1 else 's'} only you can do")
    return out


def _record_path(cfg: Config) -> Path:
    return Path(cfg.state_dir) / "owner_rows.json"


def ping_owner_rows(cfg: Config, st: state_mod.State) -> list[str]:
    """Ask the owner once for each owner-run row that has started holding rows up.

    Once per row for the life of the state dir (``owner_rows.json``), however
    often it is checked and across restarts; the rows found together share one
    ask. Returns the rows just asked about.
    """
    rows = current_owner_rows(cfg, st)
    path = _record_path(cfg)
    try:
        told = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        told = {}
    if not isinstance(told, dict):
        told = {}
    new = [(r, n) for r, n in rows if r not in told]
    if not new:
        return []
    if len(new) == 1:
        (row, n), = new
        title = launch_mod.row_title(cfg, row)
        tail = (f", then tick it in the ledger or run `swarm skip {row}`: only you can do"
                f" it, and {n} row{'' if n == 1 else 's'} wait{'s' if n == 1 else ''} on it.")
        text = (telegram.fitted(cfg, f"Do {row} (", title, ")" + tail)
                if title and title != row else f"Do {row}{tail}")
    else:
        text = telegram.fitted(
            cfg, f"Do the {len(new)} rows only you can do (",
            telegram.names([r for r, _ in new]),
            "), then tick each in the ledger or `swarm skip` it: other rows wait on"
            " them. `swarm todo` lists them.")
    telegram.ask(
        cfg,
        text,
        kind="owner-row",
        phase=new[0][0],
        source="owner.ping_owner_rows",
    )
    now = time.time()
    told.update({r: now for r, _ in new})
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(told, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass
    return [r for r, _ in new]
