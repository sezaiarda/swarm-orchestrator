"""The machine layer: the machine directory, ``machine.toml`` and the registry.

Several swarms run on one machine, and three things belong to none of them: a
directory for what they share, a settings file for what is true of the box, and
the list of the swarms themselves, which is read from the state root (nothing
else records it). ``swarm ls`` prints that list from any folder. The registry
tests run real supervisors on the bare driver, two projects side by side.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from swarm_orchestrator import machine
from swarm_orchestrator import state as state_mod
from swarm_orchestrator import todo as todo_mod
from swarm_orchestrator.cli import main as cli_main
from swarm_orchestrator.config import load
from swarm_orchestrator.machine import SettingsError, setting

DOCS = Path(__file__).resolve().parent.parent / "docs" / "config.md"


# -- where things are ----------------------------------------------------------
def test_the_state_root_holds_every_swarms_state_dir_and_the_machine_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_STATE_DIR", raising=False)
    monkeypatch.delenv("SWARM_SLUG", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "xdg" / "swarm-orchestrator"
    assert machine.state_root() == root
    cfg = load(project_dir=str(tmp_path))
    assert cfg.state_dir == root / cfg.slug
    assert machine.directory() == machine.directory(cfg.state_dir) == root / "machine"
    assert not root.exists()  # asking where it is makes nothing


def test_inside_a_session_the_state_root_is_where_its_own_state_dir_is(tmp_path, monkeypatch):
    """The seam every test uses to move a run moves what the run shares too."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "elsewhere" / "state"))
    assert machine.state_root() == tmp_path / "elsewhere"
    cfg = load(project_dir=str(tmp_path))
    assert machine.directory(cfg.state_dir) == machine.directory() \
        == tmp_path / "elsewhere" / "machine"


def test_without_xdg_the_roots_are_under_the_home_folder(tmp_path, monkeypatch):
    for key in ("SWARM_STATE_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert machine.state_root() == tmp_path / ".local" / "state" / "swarm-orchestrator"
    assert machine.settings_path() == tmp_path / ".config" / "swarm-orchestrator" / "machine.toml"


def test_no_swarm_may_take_the_machine_directorys_name(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_STATE_DIR", raising=False)
    monkeypatch.setenv("SWARM_SLUG", "machine")
    with pytest.raises(ValueError, match="machine"):
        load(project_dir=str(tmp_path))


# -- machine.toml ----------------------------------------------------------------
@dataclass(frozen=True)
class Box:
    """A schema as a later table will declare it."""

    seats: int = setting("build", "seats", 1, "builds at once", env="BOX_SEATS", minimum=0)
    pair: str = setting("build", "pair", "any", "who may build together",
                        choices=("any", "distinct-repo"))
    alone: list[str] = setting("build", "alone", ["cargo clean"], "commands that run alone")
    board: bool = setting("web", "enabled", True, "serve the board")
    host: str = setting("web", "host", "0.0.0.0", "bind address", env="BOX_HOST")


def _file(tmp_path, text: str) -> Path:
    path = tmp_path / "machine.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_settings_file_is_in_the_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = tmp_path / "cfg" / "swarm-orchestrator" / "machine.toml"
    assert machine.settings_path() == path
    assert machine.settings() == machine.Settings()  # no file: every default
    path.parent.mkdir(parents=True)
    path.write_text("# nothing set yet\n")
    assert machine.settings() == machine.Settings()


def test_a_missing_file_is_every_default_and_a_file_sets_what_it_names(tmp_path, monkeypatch):
    monkeypatch.delenv("BOX_SEATS", raising=False)
    monkeypatch.delenv("BOX_HOST", raising=False)
    assert machine.settings(tmp_path / "none.toml", Box) == Box()
    got = machine.settings(_file(tmp_path, (
        '[build]\nseats = 3\npair = "distinct-repo"\nalone = ["a", "b"]\n'
        '[web]\nenabled = false\n')), Box)
    assert got == Box(seats=3, pair="distinct-repo", alone=["a", "b"], board=False)
    assert got.host == "0.0.0.0"


def test_a_default_list_is_never_shared(tmp_path):
    one, two = (machine.settings(tmp_path / "none.toml", Box) for _ in range(2))
    one.alone.append("x")
    assert two.alone == ["cargo clean"] and Box().alone == ["cargo clean"]


def test_the_environment_beats_the_file(tmp_path, monkeypatch):
    path = _file(tmp_path, '[build]\nseats = 3\n[web]\nhost = "127.0.0.1"\n')
    monkeypatch.setenv("BOX_SEATS", "5")
    monkeypatch.setenv("BOX_HOST", "::1")
    got = machine.settings(path, Box)
    assert (got.seats, got.host) == (5, "::1")
    monkeypatch.setenv("BOX_SEATS", "many")
    with pytest.raises(SettingsError, match="BOX_SEATS"):
        machine.settings(path, Box)


@pytest.mark.parametrize("text, says", [
    ("[build]\nseats = \"two\"\n", "[build].seats must be a whole number"),
    ("[build]\nseats = true\n", "[build].seats must be a whole number"),
    ("[build]\nseats = -1\n", "[build].seats must be at least 0"),
    ("[build]\npair = \"some\"\n", "[build].pair must be one of any, distinct-repo"),
    ("[build]\nalone = \"cargo clean\"\n", "[build].alone must be a list of strings"),
    ("[build]\nalone = [1]\n", "[build].alone must be a list of strings"),
    ("[web]\nenabled = \"yes\"\n", "[web].enabled must be true or false"),
    ("[web]\nhost = 80\n", "[web].host must be a string"),
    ("[build]\nseat = 2\n", "[build].seat is not a machine setting ([build] has: alone, pair, seats)"),
    ("[biuld]\nseats = 2\n", "[biuld] is not a table of machine.toml (it has: [build], [web])"),
    ("build = 2\n", "[build] is not a table of machine.toml"),
    ("[build\nseats = 2\n", "machine.toml: "),
])
def test_a_file_that_does_not_read_as_settings_is_an_error_that_names_it(tmp_path, text, says):
    """Never a default in silence: the file holds the limits that protect the box."""
    path = _file(tmp_path, text)
    with pytest.raises(SettingsError) as exc:
        machine.settings(path, Box)
    assert says in str(exc.value) and str(path) in str(exc.value)


def test_the_machine_file_holds_the_build_gates_limits_and_none_has_a_variable(tmp_path):
    """One gate for the machine, so one set of limits: a variable would let a
    process raise the limit for itself."""
    build = {k.key: k for k in machine.keys() if k.table == "build"}
    assert set(build) == {"max_concurrent", "short_s", "overtake", "idle_yield_s",
                          "idle_yield_max", "pair", "alone"}
    assert all(k.env is None for k in build.values())
    got = machine.settings(_file(tmp_path, "[build]\nmax_concurrent = 1\n"))
    assert got.build_max_concurrent == 1 and got.build_pair == "any"
    with pytest.raises(SettingsError, match=r"\[build\].jobs is not a machine setting"):
        machine.settings(_file(tmp_path, "[build]\njobs = 4\n"))  # that one is a project's


def test_the_machine_file_says_which_tables_it_has(tmp_path):
    assert {k.table for k in machine.keys()} >= {"web"}
    with pytest.raises(SettingsError, match=r"\[nope\] is not a table of machine.toml"
                                            r" \(it has: .*\[web\]"):
        machine.settings(_file(tmp_path, "[nope]\nx = 1\n"))


def test_the_board_listens_where_the_machine_file_says_and_no_variable_moves_it(
        tmp_path, monkeypatch):
    """One board for the machine, so one address: a variable would let one shell
    look for it where it is not."""
    web = {k.key: k for k in machine.keys() if k.table == "web"}
    assert set(web) == {"host", "port"} and all(k.env is None for k in web.values())
    monkeypatch.setenv("SWARM_WEB_PORT", "9000")
    monkeypatch.setenv("SWARM_WEB_HOST", "10.0.0.9")
    conf = machine.settings(tmp_path / "none.toml")
    assert (conf.web_host, conf.web_port) == ("0.0.0.0", 8765)
    conf = machine.settings(_file(tmp_path, '[web]\nhost = "127.0.0.1"\nport = 8780\n'))
    assert (conf.web_host, conf.web_port) == ("127.0.0.1", 8780)
    with pytest.raises(SettingsError, match=r"\[web\].port must be at least 1"):
        machine.settings(_file(tmp_path, "[web]\nport = 0\n"))


@pytest.mark.parametrize("key, value", [("port", "8780"), ("host", '"127.0.0.1"')])
def test_a_project_file_that_still_says_where_the_board_listens_is_refused(
        tmp_path, key, value, capsys):
    """The key moved to the machine file. Dropped in silence, the owner would
    believe a port is in force that no board listens on."""
    (tmp_path / ".swarm.toml").write_text(f"[web]\nenabled = true\n{key} = {value}\n")
    with pytest.raises(ValueError) as exc:
        load(project_dir=str(tmp_path))
    said = str(exc.value)
    assert f"[web].{key}" in said and str(tmp_path / ".swarm.toml") in said
    assert str(machine.settings_path()) in said and "machine setting" in said
    # Every command of that project says so and does nothing.
    assert cli_main(["--project-dir", str(tmp_path), "status"]) == 2
    assert f"[web].{key}" in capsys.readouterr().err
    # What stays a project's is whether it is on the board at all.
    (tmp_path / ".swarm.toml").write_text("[web]\nenabled = false\n")
    assert load(project_dir=str(tmp_path)).web_enabled is False


def _doc_rows() -> dict[tuple[str, str], list[str]]:
    """``{(table, key): cells}`` from the machine section of ``docs/config.md``:
    its ``### `[table]` `` headings and the key rows under each."""
    rows, table, inside = {}, None, False
    for line in DOCS.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside, table = "`machine.toml`" in line, None
            continue
        if not inside:
            continue
        heading = re.match(r"^### `\[(\w+)\]`", line)
        if heading:
            table = heading.group(1)
        elif table and line.startswith("| `"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            rows[(table, cells[0].strip("`"))] = cells
    return rows


def test_the_reference_doc_has_a_machine_section_that_lists_exactly_its_keys():
    text = DOCS.read_text(encoding="utf-8")
    assert sum("`machine.toml`" in ln for ln in text.splitlines() if ln.startswith("## ")) == 1
    rows = _doc_rows()
    assert set(rows) == {(k.table, k.key) for k in machine.keys()}
    for k in machine.keys():
        _, default, env = rows[(k.table, k.key)][:3]
        assert (re.findall(r"`(\w+)`", env) or [None])[0] == k.env, k.where
        assert json.dumps(k.default) in default.replace("`", ""), k.where


def test_the_doc_rows_are_read_the_way_a_table_will_be_written(tmp_path, monkeypatch):
    """The parser above, on the shape the section documents for a new table."""
    doc = ("## `[web]`\n| `port` | `1` | `SWARM_WEB_PORT` | restart | x |\n"
           "## The machine file: `machine.toml`\n\n### `[build]`\n\n"
           "| key | default | env | meaning |\n|---|---|---|---|\n"
           "| `seats` | `1` | `BOX_SEATS` | builds at once |\n## Environment\n| `x` | y |\n")
    monkeypatch.setattr(sys.modules[__name__], "DOCS", _file(tmp_path, doc))
    assert _doc_rows() == {("build", "seats"): ["`seats`", "`1`", "`BOX_SEATS`", "builds at once"]}


# -- the registry ------------------------------------------------------------------
def _up(inst) -> None:
    inst.env["FAKE_WORKER_PARK"] = "1"
    inst.up()
    assert inst.wait(lambda: inst.busy_phases() == ["P0"], timeout=25), inst.log_text()


def _ls(inst, cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "swarm_orchestrator", "ls", *args],
                          cwd=str(cwd), env=env or inst.env, capture_output=True, text=True,
                          timeout=60)


def _by_name(found) -> dict:
    return {s.name: s for s in found}


def test_a_machine_with_no_swarm_lists_none_and_ls_leaves_nothing_behind(two_swarms, tmp_path):
    a, _ = two_swarms
    assert machine.swarms() == []
    r = _ls(a, tmp_path)
    assert r.returncode == 0 and "no swarms on this machine" in r.stdout
    assert json.loads(_ls(a, tmp_path, "--json").stdout)["swarms"] == []
    assert not machine.state_root().exists()


def test_the_registry_shows_each_swarm_as_its_state_dir_has_it(two_swarms):
    a, b = two_swarms
    _up(a)
    _up(b)
    b.cli("pause")
    assert b.wait(lambda: b.state()["paused"], timeout=10)

    found = _by_name(machine.swarms())

    assert list(found) == ["alpha", "beta"]
    one, two = found["alpha"], found["beta"]
    assert (one.slug, one.state_dir, one.project_dir) == (a.state_dir.name, a.state_dir, a.project)
    assert (one.session, one.running, one.status) == ("swarm-demo", True, machine.RUNNING)
    assert one.phases == {"done": 0, "running": 1, "open": 5, "total": 5}
    assert (one.asking, one.todos, one.needs_owner, one.stale, one.problem) == (0, 0, 0, False, "")
    assert (two.running, two.paused, two.status) == (True, True, machine.PAUSED)

    a.cli("down")
    found = _by_name(machine.swarms())
    assert (found["alpha"].running, found["alpha"].status) == (False, machine.STOPPED)
    assert found["beta"].status == machine.PAUSED


def test_the_flags_and_counts_come_from_the_state_and_the_ledger(two_swarms):
    a, _ = two_swarms
    _up(a)
    a.cli("down")
    cfg = machine.swarm_config(a.state_dir)
    assert (cfg.state_dir, cfg.project_dir, cfg.name) == (a.state_dir, a.project, "alpha")
    with state_mod.transaction(cfg) as st:
        st.paused = True
        st.usage_hold = {"week": {"at": 60}}
        st.frozen = {"since": 1.0, "cgroups": ["/x"]}
        st.finished = True
        st.waiting["P2"] = 1.0
        st.done.update(P0="ok", P1="ok")
        for slot in st.slots:
            slot.busy, slot.phase = False, None
    got = machine.look(a.state_dir)
    assert (got.paused, got.held, got.frozen, got.finished) == (True, True, True, True)
    assert (got.asking, got.needs_owner - got.todos) == (1, 1)
    assert got.phases == {"done": 2, "running": 0, "open": 3, "total": 5}
    assert got.todos == todo_mod.count(cfg)


def _swarm(**flags) -> machine.Swarm:
    return machine.Swarm(slug="s-1", state_dir=Path("/x/s-1"), name="s",
                         project_dir=Path(flags.pop("project", "/")), session="s", **flags)


def test_one_word_says_how_a_swarm_stands():
    assert _swarm(running=True).status == machine.RUNNING
    assert _swarm(running=True, paused=True).status == machine.PAUSED
    assert _swarm(running=True, paused=True, held=True).status == machine.HELD
    assert _swarm(running=True, held=True, frozen=True).status == machine.FROZEN
    assert _swarm(paused=True).status == machine.STOPPED
    assert _swarm(finished=True).status == machine.FINISHED
    gone = _swarm(running=True, project="/no/such/project/folder")
    assert gone.stale and gone.status == machine.STALE
    assert "stale+running" in machine.render([gone])
    assert _swarm(asking=2, todos=3).needs_owner == 5


def test_a_swarm_whose_project_is_gone_is_shown_as_stale_not_hidden(two_swarms, tmp_path):
    a, b = two_swarms
    _up(a)
    _up(b)
    b.cli("down")
    shutil.rmtree(b.project)
    found = _by_name(machine.swarms())
    assert found["beta"].stale and found["beta"].status == machine.STALE
    assert found["beta"].project_dir == b.project and found["beta"].phases is None
    assert machine.swarm_config(b.state_dir) is None
    out = _ls(a, tmp_path).stdout
    line = next(ln for ln in out.splitlines() if ln.startswith("beta"))
    assert "stale" in line and "(gone)" in line and "alpha" in out


def test_a_state_dir_no_supervisor_ran_in_is_listed_as_empty(two_swarms):
    a, _ = two_swarms
    a.cli("pause")  # changes state, starts nothing: the dir exists, nothing recorded
    assert a.state_dir.is_dir() and not (a.state_dir / "config.json").exists()
    (found,) = machine.swarms()
    assert (found.name, found.project_dir, found.status) == (a.state_dir.name, None, machine.EMPTY)
    assert (found.running, found.paused, found.stale) == (False, True, False)


def test_the_machine_directory_and_stray_folders_are_not_swarms(two_swarms):
    a, _ = two_swarms
    _up(a)
    root = machine.state_root()
    (machine.directory() / "buildsem").mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "a-file").write_text("x")
    assert machine.state_dirs() == [a.state_dir]


def test_one_unreadable_swarm_does_not_hide_the_others(two_swarms):
    a, b = two_swarms
    _up(a)
    _up(b)
    b.cli("down")
    (b.state_dir / "state.json").write_text("{torn")
    found = _by_name(machine.swarms())
    assert found["alpha"].status == machine.RUNNING and not found["alpha"].problem
    assert found["beta"].problem and found["beta"].phases is None
    assert "counts unavailable" in machine.render(list(found.values()))


# -- swarm ls ------------------------------------------------------------------------
def test_ls_lists_every_swarm_from_any_folder(two_swarms, tmp_path):
    a, b = two_swarms
    _up(a)
    _up(b)
    r = _ls(a, tmp_path)
    assert r.returncode == 0 and r.stderr == ""
    head, *rows, board = r.stdout.splitlines()
    assert board.startswith("web board: ")  # the one address, after the swarms
    assert head.split() == ["SWARM", "STATUS", "DONE", "RUNNING", "OPEN", "NEEDS", "YOU",
                            "SESSION", "PROJECT"]
    assert [ln.split() for ln in rows] == [
        ["alpha", "running", "0", "1", "5", "0", "swarm-demo", str(a.project)],
        ["beta", "running", "0", "1", "5", "0", "swarm-demo", str(b.project)],
    ]


def test_ls_json_is_the_registry(two_swarms, tmp_path):
    a, b = two_swarms
    _up(a)
    data = json.loads(_ls(a, tmp_path, "--json").stdout)
    assert data["state_root"] == str(machine.state_root())
    assert data["machine_dir"] == str(machine.directory())
    assert data["settings"] == str(machine.settings_path())
    assert data["web"].startswith("web board: ")
    (one,) = data["swarms"]
    assert one == machine.look(a.state_dir).to_dict()
    assert (one["name"], one["status"], one["running"], one["stale"], one["needs_owner"]) \
        == ("alpha", "running", True, False, 0)
    assert one["project_dir"] == str(a.project) and one["state_dir"] == str(a.state_dir)
    assert not b.state_dir.exists()


def test_ls_from_a_session_of_one_swarm_shows_the_others_too(two_swarms, monkeypatch):
    """The answer to "how is the other swarm doing" that needs no other swarm's
    state dir: a session may list them, and still act only on its own."""
    a, b = two_swarms
    _up(a)
    _up(b)
    env = {**a.env, "SWARM_STATE_DIR": str(a.state_dir), "SWARM_PROJECT": str(a.project)}
    r = _ls(a, b.project, env=env)
    assert [ln.split()[0] for ln in r.stdout.splitlines()[1:-1]] == ["alpha", "beta"]
    monkeypatch.setenv("SWARM_STATE_DIR", str(a.state_dir))
    cfg = machine.swarm_config(b.state_dir)
    assert cfg.state_dir == b.state_dir and cfg.project_dir == b.project
    assert _by_name(machine.swarms()).keys() == {"alpha", "beta"}


def test_ls_needs_no_project_and_survives_a_broken_one(two_swarms, tmp_path, monkeypatch, capsys):
    a, _ = two_swarms
    _up(a)
    (a.project / ".swarm.toml").write_text("[swarm\n")
    monkeypatch.chdir(a.project)
    assert cli_main(["ls"]) == 0
    assert "alpha" in capsys.readouterr().out
    monkeypatch.chdir(tmp_path)
    assert cli_main(["--project-dir", str(tmp_path / "nowhere"), "ls"]) == 0
