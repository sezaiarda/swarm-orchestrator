"""Live-config reload policy: the matrix, the gates, and the hold-over.

Every setting declares its reload class where it is declared (see
``test_config_schema.py``). These tests pin the behaviours a naive reload gets wrong:
shrinking ``max_workers`` must retire a busy slot rather than delete its record
(deleting it turns the worker's eventual ``swarm done`` into a DONE-DUPLICATE
no-op that strands the phase), ``park_after`` must move the deadlines already
armed (they are stored absolute, so they do not follow the policy on their own),
and a field pinned by a ``SWARM_*`` variable must be reported rather than
silently doing nothing.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from swarm_orchestrator import reload as reload_mod
from swarm_orchestrator.config import SETTINGS, Config, load
from swarm_orchestrator.reload import ENV, HOT, NEXT, RESTART, Facts

_MIN = '[swarm]\nmax_workers = 4\n'


def _cfg(tmp_path: Path, monkeypatch, text: str = _MIN, **env):
    """A Config loaded from ``text`` written into a throwaway project dir."""
    monkeypatch.setenv("SWARM_STATE_DIR", str(tmp_path / "state"))
    for leak in ("SWARM_SLUG", "SWARM_SESSION", "SWARM_LAYOUT", "SWARM_PARK_AFTER",
                 "SWARM_DRIVER", "SWARM_BUILD_JOBS",
                 "SWARM_GIT_MAIN", "SWARM_GIT_REPOS", "SWARM_DONE_GRACE"):
        monkeypatch.delenv(leak, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, str(val))
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / ".swarm.toml").write_text(text, encoding="utf-8")
    return load(project_dir=str(project))


def _facts(**kw) -> Facts:
    kw.setdefault("now", 1_000.0)
    kw.setdefault("env", {})
    return Facts(**kw)


def _by_name(changes, name):
    return next(c for c in changes if c.name == name)


# -- retired keys ----------------------------------------------------------
def test_a_file_that_still_sets_retired_keys_loads_and_reloads(tmp_path, monkeypatch):
    """`[worker].done_hook` and `[tasks].roadmap` were read by nothing and are
    gone; a `.swarm.toml` that still sets them must load and diff clean."""
    text = (_MIN + "[worker]\ndone_hook = 'swarm done \"$SWARM_PHASE\" ok'\n"
            "[tasks]\nroadmap = \"docs/ROADMAP-MASTER.md\"\n")
    cfg = _cfg(tmp_path, monkeypatch, text)
    names = {f.name for f in fields(Config)}
    assert "done_hook" not in names and "roadmap" not in names
    assert cfg.max_workers == 4
    assert [c for c in reload_mod.diff(cfg, cfg, _facts()) if c.old != c.new] == []


# -- the classes that matter most -----------------------------------------
@pytest.mark.parametrize(
    "name", ["build_jobs", "build_cache"]
)
def test_build_fields_are_next_because_the_env_is_frozen_at_launch(name):
    # launch._worker_env writes SWARM_BUILD_JOBS into each worker's environment and
    # config._int_env gives the environment strict precedence: a running worker
    # holds a copy no reload can reach.
    assert SETTINGS[name].klass == NEXT


def test_the_gates_limits_are_not_a_projects_settings_and_no_reload_is_about_them():
    """They are the machine's (``machine.toml``): every process reads them
    there, so there is nothing for a reload of ``.swarm.toml`` to apply."""
    assert not {name for name in SETTINGS if name in (
        "build_max_concurrent", "build_short_s", "build_overtake", "build_idle_yield_s",
        "build_idle_yield_max", "build_pair", "build_alone")}


@pytest.mark.parametrize(
    "name", ["slug", "project_dir", "driver", "session", "git_isolation"]
)
def test_run_identity_fields_are_restart(name):
    assert SETTINGS[name].klass == RESTART


@pytest.mark.parametrize("name", ["tui_autostart", "tui_cmd"])
def test_boot_only_fields_are_restart(name):
    # session.setup creates and respawns the dash pane exactly once, at `swarm
    # up`. There is no later moment a reload could reach, so "restart the run to
    # change it" is the honest answer rather than a NEXT that never arrives.
    assert SETTINGS[name].klass == RESTART


def test_a_tui_edit_is_refused_and_held_over(tmp_path, monkeypatch):
    old = _cfg(tmp_path, monkeypatch, '[tui]\ncmd = "swarm tui"\n')
    new = _cfg(tmp_path, monkeypatch, '[tui]\ncmd = "htop"\n')

    payload = reload_mod.plan(old, new, _facts())

    assert [c.name for c in payload.refused()] == ["tui_cmd"]
    assert payload.cfg.tui_cmd == "swarm tui"


def test_auto_resolve_says_it_cannot_fix_an_already_held_integration(
    tmp_path, monkeypatch
):
    old = _cfg(tmp_path, monkeypatch, "[git]\n")
    new = _cfg(
        tmp_path, monkeypatch,
        '[git]\n[git.auto_resolve]\n"docs/*.md" = "union"\n',
    )

    change = _by_name(reload_mod.diff(old, new, _facts()), "git_auto_resolve")

    assert change.effective == HOT
    assert "already held" in change.effect and "swarm resolved" in change.effect


# -- max_workers: retire, never delete ------------------------------------
def test_shrink_retires_a_busy_slot_and_drops_only_idle_ones(tmp_path, monkeypatch):
    old = _cfg(tmp_path, monkeypatch, "[swarm]\nmax_workers = 4\n")
    new = _cfg(tmp_path, monkeypatch, "[swarm]\nmax_workers = 1\n")
    facts = _facts(busy={0: "P0", 1: "P1"})

    change = _by_name(reload_mod.diff(old, new, facts), "max_workers")

    assert change.effective == HOT  # a shrink is applied, never refused
    drop, retire = reload_mod.retire_plan(4, 1, facts)
    assert drop == [2, 3]  # idle slots go immediately
    assert retire == [1]  # the surplus BUSY slot is retired, not deleted
    assert 0 not in retire and 0 not in drop  # busy slots are kept first
    assert any("retire slot 1" in a for a in change.actions)
    assert "DONE-DUPLICATE" in change.effect


def test_grow_just_adds_slots(tmp_path, monkeypatch):
    old = _cfg(tmp_path, monkeypatch, "[swarm]\nmax_workers = 2\n")
    new = _cfg(tmp_path, monkeypatch, "[swarm]\nmax_workers = 5\n")

    change = _by_name(reload_mod.diff(old, new, _facts(busy={0: "P0"})), "max_workers")

    assert change.effective == HOT
    assert change.actions == ["add 3 free slot(s)"]
    assert reload_mod.retire_plan(2, 5, _facts(busy={0: "P0"})) == ([], [])


def test_shrink_costs_nothing_when_the_new_cap_still_fits_the_busy_slots():
    facts = _facts(busy={0: "P0", 3: "P3"})
    drop, retire = reload_mod.retire_plan(4, 2, facts)
    assert retire == []  # both live workers survive
    assert drop == [1, 2]


# -- park_after: the armed deadlines are absolute -------------------------
def test_park_after_shifts_already_armed_deadlines_by_the_delta():
    facts = _facts(waiting={"P1": 1_500.0, "P2": 1_400.0})
    assert reload_mod.park_shift(120, 300, facts) == {"P1": 1_680.0, "P2": 1_580.0}


def test_park_after_shrink_is_floored_at_now_plus_one_second():
    # Without the floor every waiting worker parks in the same instant the reload
    # lands -- mid-question, with the owner still typing.
    facts = _facts(waiting={"P1": 1_050.0})
    assert reload_mod.park_shift(600, 1, facts) == {"P1": 1_001.0}


def test_park_after_zero_drops_the_deadlines():
    facts = _facts(waiting={"P1": 1_500.0})
    assert reload_mod.park_shift(120, 0, facts) == {}


def test_park_after_change_reports_the_disarm(tmp_path, monkeypatch):
    old = _cfg(tmp_path, monkeypatch, "[worker]\npark_after = 120\n")
    new = _cfg(tmp_path, monkeypatch, "[worker]\npark_after = 0\n")
    facts = _facts(waiting={"P1": 1_500.0})

    change = _by_name(reload_mod.diff(old, new, facts), "park_after")

    assert change.effective == HOT
    assert change.actions == ["disarm park timer for P1"]


# -- gates ----------------------------------------------------------------
def test_main_branch_is_refused_while_a_phase_is_in_flight(tmp_path, monkeypatch):
    old = _cfg(tmp_path, monkeypatch, '[git]\nmain_branch = "master"\n')
    new = _cfg(tmp_path, monkeypatch, '[git]\nmain_branch = "main"\n')

    busy = _by_name(reload_mod.diff(old, new, _facts(busy={0: "P0"})), "git_main_branch")
    idle = _by_name(reload_mod.diff(old, new, _facts()), "git_main_branch")

    assert busy.effective == RESTART and "P0" in busy.effect
    assert idle.effective == HOT


def test_repo_growth_is_free_but_a_shrink_in_flight_is_refused(tmp_path, monkeypatch):
    one = _cfg(tmp_path, monkeypatch, '[git]\nrepos = ["*"]\n')
    two = _cfg(tmp_path, monkeypatch, '[git]\nrepos = ["*", "packages/*"]\n')
    flying = _facts(integ_queue=["P4"])

    grow = _by_name(reload_mod.diff(one, two, flying), "git_repos")
    shrink = _by_name(reload_mod.diff(two, one, flying), "git_repos")
    shrink_idle = _by_name(reload_mod.diff(two, one, _facts()), "git_repos")

    assert grow.effective == HOT
    assert shrink.effective == RESTART and "packages/*" in shrink.effect
    assert shrink_idle.effective == HOT


def test_layout_edit_is_deferred_when_swarm_layout_pinned_the_run(tmp_path, monkeypatch):
    old = _cfg(tmp_path, monkeypatch, '[tmux]\nlayout = "auto"\n')
    new = _cfg(tmp_path, monkeypatch, '[tmux]\nlayout = "even-vertical"\n')

    free = _by_name(reload_mod.diff(old, new, _facts()), "tmux_layout")
    pinned = _by_name(reload_mod.diff(old, new, _facts(layout="tiled")), "tmux_layout")

    assert free.effective == HOT
    assert pinned.effective == NEXT and "swarm layout" in pinned.effect


def test_parked_and_waiting_phases_count_as_in_flight():
    assert _facts(parked=["P1"]).in_flight() == ["P1"]
    assert _facts(waiting={"P2": 0.0}).in_flight() == ["P2"]
    assert _facts(integ_blocked="P3").in_flight() == ["P3"]
    assert _facts().in_flight() == []


# -- env overrides keep winning after a reload ----------------------------
def test_a_pinned_field_reports_an_env_line_instead_of_silence(tmp_path, monkeypatch):
    # Both sides of the diff went through load(), which applied the same override,
    # so the field CANNOT differ -- reporting nothing would hide the fact that
    # editing [build].jobs is inert until the variable is unset.
    cfg = _cfg(tmp_path, monkeypatch, _MIN, SWARM_BUILD_JOBS=3)
    changes = reload_mod.diff(cfg, cfg, _facts(env={"SWARM_BUILD_JOBS": "3"}))

    pinned = _by_name(changes, "build_jobs")
    assert pinned.effective == ENV
    assert pinned.env == "SWARM_BUILD_JOBS"
    assert "SWARM_BUILD_JOBS" in reload_mod.render(
        reload_mod.ReloadPlan(changes=changes, cfg=cfg)
    )


def test_an_unparseable_numeric_override_is_not_a_shadow(tmp_path, monkeypatch):
    # config._int_env walks PAST a malformed override to the file value, so
    # calling it a shadow would be a lie in the one case that matters.
    old = _cfg(tmp_path, monkeypatch, "[worker]\npark_after = 120\n")
    new = _cfg(tmp_path, monkeypatch, "[worker]\npark_after = 300\n")
    facts = _facts(env={"SWARM_PARK_AFTER": "not-a-number"})

    change = _by_name(reload_mod.diff(old, new, facts), "park_after")

    assert change.effective == HOT  # the file really did win
    assert "unparseable" in change.effect


def test_fields_with_no_env_var_never_report_env(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    changes = reload_mod.diff(cfg, cfg, _facts(env={"SWARM_BUILD_JOBS": "3"}))
    assert all(c.name == "build_jobs" for c in changes)


def test_watchdog_change_says_to_refresh_the_supervisors_cached_copy(
    tmp_path, monkeypatch
):
    # Supervisor.__init__ copies watchdog_s into self.watchdog_s, so swapping
    # self.cfg alone leaves the old sweep interval running. Unlike a worker's
    # frozen env this IS reachable, so it stays HOT -- with an explicit action.
    old = _cfg(tmp_path, monkeypatch, "[swarm]\nwatchdog_s = 300\n")
    new = _cfg(tmp_path, monkeypatch, "[swarm]\nwatchdog_s = 60\n")

    change = _by_name(reload_mod.diff(old, new, _facts()), "watchdog_s")

    assert change.effective == HOT
    assert change.actions == ["set supervisor.watchdog_s = 60"]
    assert "cached copy" in change.effect


def test_a_dict_valued_field_survives_the_diff_and_renders(tmp_path, monkeypatch):
    old = _cfg(tmp_path, monkeypatch, "[git]\n")
    new = _cfg(
        tmp_path, monkeypatch,
        '[git]\n[git.auto_resolve]\n"docs/*.md" = "union"\n',
    )

    payload = reload_mod.plan(old, new, _facts())
    change = _by_name(payload.changes, "git_auto_resolve")

    assert change.effective == HOT
    assert change.new == {"docs/*.md": "union"}
    assert "docs/*.md=union" in reload_mod.render(payload)


# -- hold_over: a reload is all-or-nothing per field ----------------------
def test_hold_over_reverts_every_restart_field_and_nothing_else(tmp_path, monkeypatch):
    old = _cfg(tmp_path, monkeypatch, '[swarm]\nmax_workers = 4\nslug = "before"\n')
    new = _cfg(
        tmp_path, monkeypatch,
        '[swarm]\nmax_workers = 2\nslug = "after"\ndriver = "tmux"\n'
        '[tmux]\nsession = "renamed"\n',
    )
    facts = _facts()

    held = reload_mod.hold_over(new, reload_mod.diff(old, new, facts))

    assert held.slug == "before"  # RESTART: reverted
    assert held.session != "renamed"
    assert held.driver == old.driver
    assert held.max_workers == 2  # HOT: kept
    assert held.project_dir == new.project_dir


def test_hold_over_keeps_state_dir_consistent_with_the_reverted_slug(
    tmp_path, monkeypatch
):
    # dataclasses.replace re-runs __post_init__, so state_dir must be re-derived
    # from the slug we just put back -- not left pointing at the new run's dir.
    monkeypatch.delenv("SWARM_STATE_DIR", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    (project / ".swarm.toml").write_text('[swarm]\nslug = "before"\n', encoding="utf-8")
    old = load(project_dir=str(project))
    (project / ".swarm.toml").write_text('[swarm]\nslug = "after"\n', encoding="utf-8")
    new = load(project_dir=str(project))

    held = reload_mod.hold_over(new, reload_mod.diff(old, new, _facts()))

    assert held.slug == "before"
    assert held.state_dir.name == "before"


def test_hold_over_is_a_no_op_when_nothing_was_refused(tmp_path, monkeypatch):
    old = _cfg(tmp_path, monkeypatch, "[swarm]\nmax_workers = 2\n")
    new = _cfg(tmp_path, monkeypatch, "[swarm]\nmax_workers = 3\n")
    assert reload_mod.hold_over(new, reload_mod.diff(old, new, _facts())) is new


def test_plan_bundles_the_diff_and_the_safe_config(tmp_path, monkeypatch):
    old = _cfg(tmp_path, monkeypatch, '[swarm]\nmax_workers = 4\nslug = "a"\n')
    new = _cfg(tmp_path, monkeypatch, '[swarm]\nmax_workers = 4\nslug = "b"\n')

    payload = reload_mod.plan(old, new, _facts())

    assert [c.name for c in payload.refused()] == ["slug"]
    assert payload.cfg.slug == "a"
    assert payload.to_dict()["refused"] == ["slug"]
    assert "REFUSED" in reload_mod.render(payload)


def test_identical_configs_produce_no_changes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    payload = reload_mod.plan(cfg, cfg, _facts())
    assert payload.changes == []
    assert "no config changes" in reload_mod.render(payload)


def test_facts_from_state_reads_the_live_run():
    from swarm_orchestrator.state import Slot, State

    st = State(
        slots=[Slot(id=0, busy=True, phase="P0"), Slot(id=1)],
        parked=["P9"],
        integ_queue=["P4"],
        layout="tiled",
        paused=True,
    )
    facts = Facts.from_state(st, env={}, now=5.0)

    assert facts.busy == {0: "P0"}
    assert facts.layout == "tiled" and facts.paused is True
    assert facts.in_flight() == ["P0", "P4", "P9"]


def test_a_supervisor_older_than_a_new_setting_can_still_be_reloaded(tmp_path, monkeypatch):
    """The snapshot of a supervisor started before a setting existed lacks that
    field. It runs on the default, so the default is its "before" — not a reason
    to report "no running supervisor" (a new gc_wait_s setting once blocked a cap change)."""
    import json

    from swarm_orchestrator import cli

    cfg = _cfg(tmp_path, monkeypatch)
    cfg.ensure_dirs()
    snap = {f.name: getattr(cfg, f.name) for f in fields(cfg) if f.init}
    snap.pop("gc_wait_s")
    (cfg.state_dir / "config.json").write_text(json.dumps(snap, default=str), encoding="utf-8")
    old = cli._snapshot_cfg(cfg)
    assert old is not None
    assert old.gc_wait_s == SETTINGS["gc_wait_s"].default
    assert old.max_workers == 4
