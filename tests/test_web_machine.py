"""One board for every swarm on the machine: the overview, and each swarm's own
routes under ``/s/<slug>/`` that serve that swarm and never another.

The swarms here are state dirs written by hand under one state root, as two
real swarms keep theirs; ``tests/test_web_lifecycle.py`` runs real ones.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import machine_toml

from swarm_orchestrator import config as config_mod
from swarm_orchestrator import machine
from swarm_orchestrator.web import hub as hub_mod
from swarm_orchestrator.web import server as web_server

from test_web_board import record, serve

LEDGERS = {
    "alpha": "# Alpha\n\n- [ ] `al-W0` · **the alpha root**\n- [ ] `al-W1` · needs:`al-W0` · **alpha one**\n"
             "- [ ] `al-W2` · needs:`al-W0` · **alpha two**\n- [ ] `al-W3` · needs:`al-W2` · **alpha three**\n",
    "beta": "# Beta\n\n- [ ] `be-W0` · **the beta root**\n- [ ] `be-W1` · needs:`be-W0` · **beta one**\n"
            "- [ ] `be-W2` · needs:`be-W1` · **beta two**\n",
}


def make_swarm(base: Path, name: str, state: dict, enabled: bool = True):
    """A project called ``name`` and its state dir in this test's state root,
    recorded as a swarm that last ran on ``state``."""
    project = base / name
    (project / "docs").mkdir(parents=True)
    (project / "docs" / "LEDGER.md").write_text(LEDGERS.get(name, LEDGERS["beta"]), encoding="utf-8")
    (project / ".swarm.toml").write_text('[tasks]\nledger = "docs/LEDGER.md"\n', encoding="utf-8")
    cfg = config_mod.load(project_dir=str(project))
    cfg.web_enabled = enabled  # as `[web] enabled` in its file; the suite's env says off
    cfg.ensure_dirs()
    record(cfg)
    cfg.state_path.write_text(json.dumps(state), encoding="utf-8")
    return cfg


def ask(cfg, phase: str, text: str, ts: float) -> None:
    with (cfg.state_dir / "notifications.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": ts, "kind": "waiting", "phase": phase, "text": text,
                             "delivered": True, "error": None}) + "\n")


@pytest.fixture
def pair(tmp_path, monkeypatch):
    """Two swarms in one state root. Alpha builds one phase and asks about
    another; beta is finished with one phase landed."""
    machine_toml(build={"max_concurrent": 0})  # no gate to ask: these swarms build nothing
    now = time.time()
    alpha = make_swarm(tmp_path, "alpha", {
        "slots": [{"id": 0, "busy": True, "phase": "al-W1"}, {"id": 1, "busy": True, "phase": "al-W2"},
                  {"id": 2, "busy": False, "phase": None}],
        "done": {"al-W0": "ok"}, "waiting": {"al-W2": now + 60}, "parked": [],
        "last_event_at": now - 30})
    ask(alpha, "al-W2", "alpha asks: left or right?", now - 60)
    beta = make_swarm(tmp_path, "beta", {
        "slots": [{"id": 0, "busy": False, "phase": None}], "parked": ["be-W1"],
        "asked": {"be-W1": now - 3600}, "done": {"be-W0": "ok"}, "last_event_at": now - 7200})
    ask(beta, "be-W1", "beta asks: now or tonight?", now - 3600)
    return alpha, beta


@pytest.fixture
def srv(pair):
    s = serve(pair[0])
    try:
        yield s
    finally:
        web_server.close(s)


def _get(srv, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}{path}",
                                    timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _json(srv, path: str) -> dict:
    code, body = _get(srv, path)
    assert code == 200, (path, code, body[:200])
    return json.loads(body)


def _slug(cfg) -> str:
    return cfg.state_dir.name


def _reader(state_dir: Path) -> int:
    """Stand in for a supervisor: hold the control FIFO open for reading."""
    fifo = state_dir / "control.fifo"
    if not fifo.exists():
        os.mkfifo(fifo)
    return os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)


# -- the overview ----------------------------------------------------------------------
def test_the_overview_has_every_swarm_and_what_waits_on_the_owner(srv, pair):
    alpha, beta = pair
    m = _json(srv, "/api/machine")
    by = {s["name"]: s for s in m["swarms"]}
    assert list(by) == ["alpha", "beta"]
    assert (by["alpha"]["slug"], by["beta"]["slug"]) == (_slug(alpha), _slug(beta))
    assert by["alpha"]["phases"] == {"done": 1, "running": 1, "open": 3, "total": 4}
    assert by["beta"]["phases"] == {"done": 1, "running": 0, "open": 2, "total": 3}
    assert [b["id"] for b in by["alpha"]["building"]] == ["al-W1"] and by["beta"]["building"] == []
    # First on the page: what waits, newest first, each naming its swarm.
    asked = m["waiting"]["questions"]
    assert [(a["swarm"], a["slug"], a["id"]) for a in asked] == [
        ("alpha", _slug(alpha), "al-W2"), ("beta", _slug(beta), "be-W1")]
    assert "left or right" in asked[0]["q"] and "now or tonight" in asked[1]["q"]
    # A worker still on its park timer asked that long before it parks.
    assert asked[0]["since"] == pytest.approx(time.time() - 60, abs=30)
    assert asked[0]["since"] > asked[1]["since"]
    assert m["waiting"]["n"] == by["alpha"]["needs_you"] + by["beta"]["needs_you"] >= 2
    # The totals are the swarms added up, as `swarm ls` counts each.
    assert m["totals"]["phases"] == {"done": 2, "running": 1, "open": 5, "total": 7}
    assert m["totals"]["swarms"] == 2
    assert m["totals"]["builds"] == {"running": 0, "waiting": 0, "by": []}
    assert m["version"] >= 1 and m["host"]


def test_each_swarms_routes_serve_that_swarm_and_never_the_other(srv, pair):
    alpha, beta = pair
    mine = {_slug(alpha): ("al-", "be-", "alpha"), _slug(beta): ("be-", "al-", "beta")}
    for slug, (own, other, name) in mine.items():
        board = _json(srv, f"/s/{slug}/api/board")
        ids = [c["id"] for col in board["columns"] for c in col["cards"]]
        assert ids and all(i.startswith(own) for i in ids), (slug, ids)
        assert board["project"] == name
        # Its own rows open; the other swarm's do not exist here.
        assert _json(srv, f"/s/{slug}/api/phase/{own}W0")["id"] == own + "W0"
        assert _get(srv, f"/s/{slug}/api/phase/{other}W0")[0] == 404
        assert _json(srv, f"/s/{slug}/api/search?q=root")["ids"] == [own + "W0"]
        nodes = {n["id"] for n in _json(srv, f"/s/{slug}/api/graph?mode=all")["nodes"]}
        assert nodes and all(n.startswith(own) for n in nodes)
        for path in ("api/board", "api/graph?mode=all", "api/usage", "api/resources",
                     f"api/phase/{own}W0"):
            body = _get(srv, f"/s/{slug}/{path}")[1].decode()
            assert other + "W" not in body, (slug, path)
    # One page for every swarm: which one it shows is where it was opened.
    assert _get(srv, f"/s/{_slug(alpha)}/")[1] == _get(srv, f"/s/{_slug(beta)}/")[1]


def test_a_swarm_that_is_down_shows_its_last_state_marked_as_not_running(srv, pair):
    alpha, beta = pair
    held = _reader(alpha.state_dir)
    try:
        srv.hub.refresh(time.time() + 2 * hub_mod.LOOK_EVERY_S)
        by = {s["name"]: s for s in _json(srv, "/api/machine")["swarms"]}
        assert (by["alpha"]["status"], by["alpha"]["word"], by["alpha"]["up"]) == (
            "running", "running", True)
        assert (by["beta"]["status"], by["beta"]["word"], by["beta"]["up"]) == (
            "stopped", "down", False)
        assert _json(srv, "/api/machine")["totals"]["up"] == 1
        # Down, and still read: its page has how it stood.
        board = _json(srv, f"/s/{_slug(beta)}/api/board")
        assert board["header"]["running"] is False and board["header"]["progress"]["done"] == 1
    finally:
        os.close(held)


def test_every_standing_has_a_plain_word():
    for status in (machine.RUNNING, machine.PAUSED, machine.HELD, machine.FROZEN,
                   machine.FINISHED, machine.STOPPED):
        assert hub_mod.WORDS[status]
    assert hub_mod.WORDS[machine.HELD] == "held on a usage limit"
    assert hub_mod.WORDS[machine.STOPPED] == "down"


def test_a_state_dir_that_is_no_swarms_is_counted_and_not_shown(srv, pair, tmp_path, monkeypatch):
    root = pair[0].state_dir.parent
    (root / "never-ran" / "logs").mkdir(parents=True)  # no supervisor ever ran there
    off = make_swarm(tmp_path, "quiet", {"slots": [], "done": {}}, enabled=False)
    gone = make_swarm(tmp_path, "moved", {"slots": [], "done": {}})
    shutil.rmtree(gone.project_dir)
    (root / machine.DIR_NAME).mkdir(exist_ok=True)  # the machine's own folder is nobody's
    srv.hub.refresh()
    m = _json(srv, "/api/machine")
    assert [s["name"] for s in m["swarms"]] == ["alpha", "beta"]
    assert m["hidden"] == {"empty": 1, "stale": 1, "off": 1}
    for slug in ("never-ran", _slug(off), _slug(gone), machine.DIR_NAME):
        assert _get(srv, f"/s/{slug}/api/board")[0] == 404, slug


def test_one_unreadable_swarm_does_not_take_the_board_down(srv, pair, monkeypatch, capsys):
    alpha, beta = pair
    real = hub_mod._entry

    def entry(swarm, *rest):
        if swarm.name == "beta":
            raise RuntimeError("torn")
        return real(swarm, *rest)

    monkeypatch.setattr(hub_mod, "_entry", entry)
    srv.hub.refresh(time.time() + 2 * hub_mod.LOOK_EVERY_S)
    m = _json(srv, "/api/machine")
    assert [s["name"] for s in m["swarms"]] == ["alpha"] and m["hidden"] == {"unreadable": 1}
    assert _json(srv, f"/s/{_slug(alpha)}/api/board")["project"] == "alpha"
    assert "could not be read" in capsys.readouterr().out


def test_a_swarm_that_comes_or_goes_shows_without_a_restart(srv, pair, tmp_path):
    late = make_swarm(tmp_path, "gamma", {"slots": [], "done": {"be-W0": "ok"}})
    assert _get(srv, f"/s/{_slug(late)}/api/board")[0] == 404  # not looked at yet
    srv.hub.refresh()
    assert [s["name"] for s in _json(srv, "/api/machine")["swarms"]] == ["alpha", "beta", "gamma"]
    assert _json(srv, f"/s/{_slug(late)}/api/board")["project"] == "gamma"
    shutil.rmtree(late.state_dir)
    srv.hub.refresh()
    assert _get(srv, f"/s/{_slug(late)}/api/board")[0] == 404
    assert len(_json(srv, "/api/machine")["swarms"]) == 2


def test_the_board_is_no_swarms_whatever_the_environment_names(pair, monkeypatch):
    """A feed is bound to its swarm's state dir by the swarm's own record, so a
    ``SWARM_STATE_DIR`` or ``SWARM_SLUG`` in the server's environment (it is
    started with neither) cannot point two swarms at one run."""
    alpha, beta = pair
    monkeypatch.setenv("SWARM_STATE_DIR", str(alpha.state_dir))
    monkeypatch.setenv("SWARM_SLUG", "alpha-by-hand")
    hub = hub_mod.Hub(alpha.state_dir.parent)
    try:
        hub.refresh()
        assert hub.feed(_slug(beta)).cfg.state_dir == beta.state_dir
        assert hub.feed(_slug(beta)).dash.cfg.project_dir == beta.project_dir
        assert hub.feed(_slug(alpha)).cfg.state_dir == alpha.state_dir
    finally:
        hub.stop()


def test_the_machines_builds_say_whose_they_are():
    gate = {"slots": [
        {"busy": True, "id": "b1", "swarm": "alpha-1", "swarm_name": "alpha"},
        {"busy": True, "id": "b2", "swarm": "beta-2", "swarm_name": "beta"},
        {"busy": True, "id": "b3", "swarm": "beta-2", "swarm_name": "beta"},
        {"busy": True, "gc": True, "id": "gc-1", "swarm": "alpha-1"},  # gc holds a slot: no build
        {"busy": True, "unknown": True, "id": None},
        {"busy": False, "id": None}],
        "queue": [{"id": "q1", "swarm": "alpha-1", "swarm_name": "alpha"},
                  {"id": "q2"}]}  # a record from before builds said whose they are
    assert hub_mod._builds(gate) == {"running": 3, "waiting": 2, "by": [
        {"slug": "beta-2", "name": "beta", "running": 2, "waiting": 0},
        {"slug": "alpha-1", "name": "alpha", "running": 1, "waiting": 1},
        {"slug": "", "name": "", "running": 0, "waiting": 1}]}
    assert hub_mod._builds({}) == {"running": 0, "waiting": 0, "by": []}
    assert hub_mod._builds({"on": False}) == {"running": 0, "waiting": 0, "by": []}


def test_the_overview_says_whose_builds_run_and_wait(pair, monkeypatch):
    """The gate is the machine's, asked once: the tile's totals are every
    swarm's builds, each swarm's card has its own."""
    alpha, beta = pair
    a, b = _slug(alpha), _slug(beta)
    asked = []

    def gate(cfg):
        asked.append(cfg.state_dir.name)
        return {"on": True, "slots": [
            {"busy": True, "id": "b1", "swarm": a, "swarm_name": "alpha", "mine": True},
            {"busy": True, "id": "b2", "swarm": b, "swarm_name": "beta", "mine": False}],
            "queue": [{"id": "q1", "swarm": b, "swarm_name": "beta", "mine": False}]}

    monkeypatch.setattr(hub_mod.resview, "gate", gate)
    hub = hub_mod.Hub(alpha.state_dir.parent)
    try:
        hub.refresh()
        m = hub.overview
    finally:
        hub.stop()
    assert len(asked) == 1  # one gate, whichever swarm asks
    assert m["totals"]["builds"] == {"running": 2, "waiting": 1, "by": [
        {"slug": b, "name": "beta", "running": 1, "waiting": 1},
        {"slug": a, "name": "alpha", "running": 1, "waiting": 0}]}
    by = {s["name"]: s["builds"] for s in m["swarms"]}
    assert by == {"alpha": {"running": 1, "waiting": 0}, "beta": {"running": 1, "waiting": 1}}


def test_the_owners_to_dos_wait_on_the_owner_too(pair, monkeypatch):
    """A to-do is not a question, and it waits on the owner all the same: the
    count of a swarm is both, each thing once, as ``swarm ls`` has it."""
    from swarm_orchestrator import todo as todo_mod

    alpha, beta = pair
    real = todo_mod.collect

    def collect(cfg, st=None):
        got = real(cfg, st)
        if cfg.state_dir == alpha.state_dir:
            got.items = [
                todo_mod.Todo("al-W3", "try it by hand", todo_mod.OWNER_ROW, rows_behind=2,
                              since=50.0, paths=["/home/x/secret.md"], close=["swarm record"]),
                # A question's row is listed once, as the question.
                todo_mod.Todo("al-W2", "the same thing", todo_mod.OWNER_ROW),
                todo_mod.Todo("overseer-1", "look at the logs", todo_mod.OVERSEER, since=90.0),
            ]
        return got

    monkeypatch.setattr(todo_mod, "collect", collect)
    hub = hub_mod.Hub(alpha.state_dir.parent)
    try:
        hub.refresh()
        m = hub.overview
    finally:
        hub.stop()
    entry = next(s for s in m["swarms"] if s["name"] == "alpha")
    assert [t["id"] for t in m["waiting"]["todos"]] == ["overseer-1", "al-W3"]  # newest first
    assert entry["needs_you"] == len(entry["questions"]) + 2
    todo = m["waiting"]["todos"][1]
    assert (todo["swarm"], todo["sub"], todo["behind"], todo["row"]) == (
        "alpha", "yours to do", 2, True)
    assert m["waiting"]["todos"][0]["row"] is False  # no ledger row: no sheet to open
    # What to do and since when, never a path or a command of the box.
    assert "secret.md" not in json.dumps(m) and "swarm record" not in json.dumps(m)
    assert m["waiting"]["n"] == m["waiting"]["questions_n"] + m["waiting"]["todos_n"]


def test_the_overview_moves_only_when_something_did(srv):
    v = srv.hub.version
    for _ in range(3):
        assert srv.hub.refresh() is False
    assert srv.hub.version == v
    body, _, etag = srv.hub.current()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}/api/machine",
                                 headers={"If-None-Match": etag})
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 304
