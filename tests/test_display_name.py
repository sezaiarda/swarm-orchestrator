"""``[swarm].name``: what the swarm is called, apart from where it lives.

Everything the owner reads used to take the swarm's name from the project
folder. A project that is renamed keeps its folder (and with it the slug, the
state dir, the worktrees), so the name is a setting of its own: by default the
folder's, and when set, what the tmux session, the web board, the Telegram
texts and ``swarm status`` show. These tests pin both halves: nothing changes
for a project that does not set it, and setting it moves no state and strands
no board, bot or tmux session started under the old name.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from swarm_orchestrator import cli
from swarm_orchestrator import reload as reload_mod
from swarm_orchestrator import restart as restart_mod
from swarm_orchestrator import session as session_mod
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import tgbot, tmux
from swarm_orchestrator.config import HOT, RESTART, SETTINGS, load
from swarm_orchestrator.logutil import Log
from swarm_orchestrator.web import board as board_mod
from swarm_orchestrator.web import lifecycle
from swarm_orchestrator.web import server as web_server

from test_web_lifecycle import DEMO, _free_port

FOLDER = "Old_Project"
BARE = '[swarm]\ndriver = "bare"\n[web]\nenabled = false\n'
NAMED_BARE = BARE.replace("[swarm]\n", '[swarm]\nname = "New Name"\n')


@pytest.fixture
def project(tmp_path, monkeypatch):
    for setting in SETTINGS.values():
        if setting.env:
            monkeypatch.delenv(setting.env, raising=False)
    monkeypatch.delenv("SWARM_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SWARM_TG_SINK", str(tmp_path / "tg.log"))
    pdir = tmp_path / FOLDER
    pdir.mkdir()
    return pdir


def _load(project, text: str = ""):
    (project / ".swarm.toml").write_text(text, encoding="utf-8")
    return load(project_dir=str(project))


def _sent(tmp_path) -> str:
    return (tmp_path / "tg.log").read_text(encoding="utf-8")


# -- the default is the folder, as it always was --------------------------
def test_unset_the_name_is_the_folder_and_nothing_else_moves(project):
    cfg = _load(project)
    assert cfg.name == FOLDER
    assert cfg.session == "old-project"  # the folder's name, slugified
    assert cfg.slug.startswith("old-project-") and cfg.state_dir.name == cfg.slug
    assert _load(project, '[swarm]\nname = ""\n').name == FOLDER
    assert _load(project, '[swarm]\nname = "  "\n').name == FOLDER


def test_a_name_renames_what_is_shown_and_nothing_on_disk(project):
    before = _load(project)
    cfg = _load(project, '[swarm]\nname = "New Name"\n')
    assert cfg.name == "New Name"
    assert cfg.session == "new-name"
    assert (cfg.slug, cfg.state_dir, cfg.wt_dir, cfg.project_dir) == (
        before.slug, before.state_dir, before.wt_dir, before.project_dir)


def test_an_explicit_session_wins_over_the_name(project, monkeypatch):
    cfg = _load(project, '[swarm]\nname = "New Name"\n[tmux]\nsession = "mine"\n')
    assert (cfg.name, cfg.session, cfg.session_wanted) == ("New Name", "mine", "mine")
    monkeypatch.setenv("SWARM_SESSION", "from-env")
    assert _load(project, '[swarm]\nname = "New Name"\n').session == "from-env"


def test_the_variable_names_the_swarm_over_the_file(project, monkeypatch):
    monkeypatch.setenv("SWARM_NAME", "From Env")
    cfg = _load(project, '[swarm]\nname = "From File"\n')
    assert (cfg.name, cfg.session) == ("From Env", "from-env")


def test_the_name_is_hot_and_the_session_is_restart():
    assert SETTINGS["name"].klass == HOT and SETTINGS["name"].env == "SWARM_NAME"
    assert SETTINGS["session"].klass == RESTART


# -- where the owner reads it ----------------------------------------------
def test_status_and_its_json_carry_the_name(project, capsys):
    cfg = _load(project, NAMED_BARE)
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    assert cli.cmd_status(cfg) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith(f"name=New Name slug={cfg.slug} ")
    assert cli.cmd_status(cfg, as_json=True) == 0
    config = json.loads(capsys.readouterr().out)["config"]
    assert (config["name"], config["slug"]) == ("New Name", cfg.slug)


def test_every_telegram_starts_with_the_swarms_name(project, tmp_path):
    cfg = _load(project, NAMED_BARE)
    cfg.ensure_dirs()
    log = Log(cfg.supervisor_log)
    try:
        restart_mod.fail(cfg, {"id": "r1", "by": "owner terminal"}, "nothing answered", log,
                         left=restart_mod.DOWN)
    finally:
        log.close()
    text = _sent(tmp_path)
    assert text.startswith("[New Name] Asks you: Run `swarm up` to start the swarm: the restart"
                           " owner terminal asked for failed (nothing answered)")
    assert text.count("New Name") == 1  # in front, once, and nowhere in the words
    assert cfg.slug not in text and FOLDER not in text


def test_unnamed_a_telegram_names_the_folder(project, tmp_path):
    cfg = _load(project, BARE)
    cfg.ensure_dirs()
    log = Log(cfg.supervisor_log)
    try:
        restart_mod.fail(cfg, {"id": "r1", "by": "owner terminal"}, "nothing answered", log,
                         left=restart_mod.UNSUPERVISED)
    finally:
        log.close()
    assert _sent(tmp_path).startswith(f"[{FOLDER}] Asks you: Run `swarm restart` to bring the"
                                      " supervisor back")


def test_a_restart_that_changed_nothing_asks_nobody(project, tmp_path):
    cfg = _load(project, BARE)
    cfg.ensure_dirs()
    log = Log(cfg.supervisor_log)
    try:
        restart_mod.fail(cfg, {"id": "r1", "by": "owner terminal"}, "nothing answered", log)
    finally:
        log.close()
    assert not (tmp_path / "tg.log").exists()
    [row] = [json.loads(ln) for ln in
             (cfg.state_dir / "notifications.jsonl").read_text().splitlines()]
    assert row["class"] == "folded" and "it runs on as it was" in row["text"]


def _board(cfg) -> dict:
    cfg.ensure_dirs()
    state_mod.init_state(cfg)
    srv = web_server.make_server(cfg, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        port = srv.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board", timeout=5) as r:
            board = json.loads(r.read())
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            return {"board": board, "health": json.loads(r.read())}
    finally:
        web_server.close(srv)


def test_the_board_is_titled_with_the_name(project):
    named = _board(_load(project, '[swarm]\nname = "New Name"\n'))
    assert named["board"]["project"] == "New Name"  # the page's title and heading
    assert named["health"]["project"] == "New Name"
    plain = _board(_load(project))
    assert plain["board"]["project"] == FOLDER and plain["health"]["project"] == FOLDER


def test_a_config_without_a_name_still_titles_the_board():
    class Bare:
        project_dir = "/somewhere/a-folder"

    assert lifecycle.display_name(Bare()) == "a-folder"
    assert lifecycle.display_name(object()) is None
    assert board_mod.lifecycle is lifecycle


# -- a board started under the old name is still ours ------------------------
def test_a_board_is_recognised_by_slug_whatever_it_is_called(project):
    old = _load(project)
    new = _load(project, '[swarm]\nname = "New Name"\n')
    assert lifecycle.is_ours(new, web_server._health_body(old))
    assert lifecycle.is_ours(old, web_server._health_body(new))
    # A board from before boards said their slug answers the folder's name only.
    assert lifecycle.is_ours(new, {"app": lifecycle.APP_ID, "project": FOLDER})
    # Another project's board is not ours, even one that shows the same name.
    assert not lifecycle.is_ours(
        new, {"app": lifecycle.APP_ID, "project": "New Name", "slug": "other-12345678"})
    assert not lifecycle.is_ours(new, {"app": lifecycle.APP_ID, "project": "elsewhere"})
    assert not lifecycle.is_ours(new, {"app": "something-else", "slug": new.slug})
    assert not lifecycle.is_ours(new, ["not", "a", "board"])


def _serve_json(body: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's name
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _web(project, port: int, name: str = ""):
    head = f'[swarm]\nname = "{name}"\n' if name else ""
    return _load(project, head + f'[web]\nhost = "127.0.0.1"\nport = {port}\n')


def test_a_running_old_name_board_is_found_and_not_doubled(project):
    """The board the run started before the rename answers on the port. The
    renamed swarm must read it as its own: `swarm status` says listening, and
    the dashboard does not start a second board (or call the port taken)."""
    port = _free_port()
    old = _web(project, port)
    old.ensure_dirs()
    state_mod.init_state(old)
    srv = web_server.make_server(old, "127.0.0.1", port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        new = _web(project, port, "New Name")
        assert lifecycle.probe(new) == (lifecycle.OURS, None)
        assert lifecycle.start_detached(new) is None
    finally:
        web_server.close(srv)


def test_a_board_from_before_the_slug_is_found_by_the_folder_name(project):
    legacy = _serve_json({"app": lifecycle.APP_ID, "project": FOLDER})
    try:
        new = _web(project, legacy.server_address[1], "New Name")
        assert lifecycle.probe(new) == (lifecycle.OURS, None)
    finally:
        legacy.shutdown()
        legacy.server_close()


# -- a bot started under the old name is still ours --------------------------
def test_the_bot_is_found_by_its_pid_file_and_lock_not_by_name(project, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    old = _load(project)
    new = _load(project, '[swarm]\nname = "New Name"\n')
    assert tgbot.pidfile(old) == tgbot.pidfile(new)  # `down`/`restart` stop the old one
    held = tgbot.take_lock("123:SECRET", old.name)
    assert held is not None
    try:
        # One poller per bot: a listener under the new name waits for the old
        # one to go; it never polls beside it.
        assert tgbot.take_lock("123:SECRET", new.name) is None
        assert f"project {FOLDER}" in tgbot.lock_holder("123:SECRET")
    finally:
        held.close()
    again = tgbot.take_lock("123:SECRET", new.name)
    assert again is not None and "project New Name" in tgbot.lock_holder("123:SECRET")
    again.close()


# -- a run keeps the tmux session it was started in ---------------------------
def _snapshot(cfg, session: str) -> None:
    """What the run's supervisor recorded when it started."""
    cfg.ensure_dirs()
    (cfg.state_dir / "config.json").write_text(json.dumps({"session": session}))


def test_a_live_run_keeps_its_session_until_it_is_gone(project, monkeypatch):
    old = _load(project)
    _snapshot(old, old.session)
    asked: list[str] = []
    owner = {"value": str(old.state_dir)}

    def session_owner(name):
        asked.append(name)
        return owner["value"]

    monkeypatch.setattr(tmux, "session_owner", session_owner)
    renamed = '[swarm]\nname = "New Name"\n'

    live = _load(project, renamed)
    assert (live.name, live.session, live.session_wanted) == ("New Name", "old-project", "new-name")
    assert asked == ["old-project"]

    owner["value"] = None  # `swarm down` ended it
    assert _load(project, renamed).session == "new-name"
    owner["value"] = ""  # a session of the owner's own that happens to have the name
    assert _load(project, renamed).session == "new-name"
    owner["value"] = "/another/swarm/state"
    assert _load(project, renamed).session == "new-name"


def test_tmux_is_only_asked_when_the_name_moved_the_session(project, monkeypatch):
    def never(name):
        raise AssertionError(f"asked tmux about {name}")

    monkeypatch.setattr(tmux, "session_owner", never)
    plain = _load(project)
    _snapshot(plain, plain.session)
    assert _load(project).session == "old-project"  # unchanged: nothing to ask
    explicit = '[swarm]\nname = "New Name"\n[tmux]\nsession = "mine"\n'
    assert _load(project, explicit).session == "mine"  # set: never held back
    bare = '[swarm]\nname = "New Name"\ndriver = "bare"\n'
    assert _load(project, bare).session == "new-name"  # no tmux, no session to keep


def test_a_missing_tmux_does_not_break_the_load(project, monkeypatch):
    old = _load(project)
    _snapshot(old, old.session)

    def gone(name):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(tmux, "session_owner", gone)
    assert _load(project, '[swarm]\nname = "New Name"\n').session == "new-name"


# -- what `swarm reload` says --------------------------------------------------
def test_reload_applies_the_name_and_refuses_the_session_rename(project, monkeypatch):
    old = _load(project)
    _snapshot(old, old.session)
    monkeypatch.setattr(tmux, "session_owner", lambda name: str(old.state_dir))
    new = _load(project, '[swarm]\nname = "New Name"\n')

    payload = reload_mod.plan(old, new, reload_mod.Facts(env={}, now=1.0))
    by = {c.name: c for c in payload.changes if c.old != c.new}
    assert set(by) == {"name", "session"}
    assert (by["name"].effective, by["name"].old, by["name"].new) == (HOT, FOLDER, "New Name")
    assert (by["session"].effective, by["session"].old, by["session"].new) == (
        RESTART, "old-project", "new-name")
    assert "[swarm].name" in by["session"].effect and "swarm restart --full" in by["session"].effect
    assert (payload.cfg.name, payload.cfg.session) == ("New Name", "old-project")

    text = reload_mod.render(payload)
    applied, refused = text.split("REFUSED (held at the old value")
    assert "[swarm].name: Old_Project -> New Name" in applied
    assert "[tmux].session: old-project -> new-name" in refused

    # Until the restart, every later reload says so again.
    again = reload_mod.plan(payload.cfg, new, reload_mod.Facts(env={}, now=2.0))
    assert [c.name for c in again.changes if c.old != c.new] == ["session"]


def test_reload_says_the_same_when_the_session_could_not_be_kept(project):
    """Under the headless driver (or with tmux not answering) the load does not
    hold the session back; the reload still refuses the rename, and says why."""
    old = _load(project, BARE)
    new = _load(project, NAMED_BARE)
    changes = {c.name: c for c in reload_mod.diff(old, new, reload_mod.Facts(env={}))
               if c.old != c.new}
    assert changes["session"].effective == RESTART
    assert "[swarm].name" in changes["session"].effect
    assert reload_mod.hold_over(new, list(changes.values())).session == "old-project"


def test_reload_is_silent_about_a_session_the_file_sets(project):
    text = '[tmux]\nsession = "mine"\n'
    old = _load(project, text)
    new = _load(project, '[swarm]\nname = "New Name"\n' + text)
    changed = [c.name for c in reload_mod.diff(old, new, reload_mod.Facts(env={}))
               if c.old != c.new]
    assert changed == ["name"]


# -- the real thing: down ends the old session, up makes the new one ----------
@pytest.fixture
def tmux_project(tmp_path, monkeypatch):
    if shutil.which("tmux") is None:
        pytest.skip("tmux not available")
    for setting in SETTINGS.values():
        if setting.env:
            monkeypatch.delenv(setting.env, raising=False)
    for k, v in {"SWARM_STATE_DIR": str(tmp_path / "state"), "SWARM_TUI_AUTOSTART": "0",
                 "SWARM_CONSOLE": "0", "SWARM_WEB": "0", "SWARM_TG_COMMANDS": "0",
                 "SWARM_TG_SINK": str(tmp_path / "tg.log")}.items():
        monkeypatch.setenv(k, v)
    pdir = tmp_path / "project"
    shutil.copytree(DEMO, pdir)
    names = (f"swarm-name-{os.getpid()}-old", f"swarm-name-{os.getpid()}-new")

    def named(name: str):
        (pdir / ".swarm.toml").write_text(
            f'[swarm]\nname = "{name}"\n[tasks]\nledger = "ledger.txt"\n', encoding="utf-8")
        return load(project_dir=str(pdir))

    yield named, names
    for name in names:
        if tmux.session_exists(name):
            tmux.kill_session(name)


def test_down_ends_the_old_named_session_and_the_next_up_gets_the_new_name(tmux_project):
    named, (was, now) = tmux_project
    old = named(was)
    old.ensure_dirs()
    state_mod.init_state(old)
    session_mod.setup(old)
    _snapshot(old, old.session)
    assert tmux.session_owner(was) == str(old.state_dir)

    renamed = named(now)
    assert (renamed.name, renamed.session, renamed.session_wanted) == (now, was, now)
    assert session_mod.owns_session(renamed)

    assert cli.cmd_down(renamed) == 0
    assert not tmux.session_exists(was), "the old-named session was left running"

    after = named(now)
    assert after.session == now
    session_mod.setup(after)
    assert tmux.session_owner(now) == str(after.state_dir)
    assert not tmux.session_exists(was)


def test_a_full_restart_comes_back_in_the_new_session(tmux_project):
    named, (was, now) = tmux_project
    old = named(was)
    old.ensure_dirs()
    state_mod.init_state(old)
    session_mod.setup(old)
    _snapshot(old, old.session)
    renamed = named(now)
    assert renamed.session == was
    seen: dict[str, str] = {}

    def up(cfg) -> int:
        seen["session"] = cfg.session
        session_mod.setup(cfg)
        return 0

    restart_mod.finish_full(renamed, "plan-1", down=cli.cmd_down, up=up)
    assert seen == {"session": now}
    assert tmux.session_exists(now) and not tmux.session_exists(was)
