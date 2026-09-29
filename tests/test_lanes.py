"""Lanes: the touch grammar, the overlap rule, and the fair scheduler.

Every refusal gets its own case because each one guards a way a typo would
otherwise widen or narrow a lane silently; the overlap table is checked both
ways round because the scheduler asks the question in either order.
"""

from __future__ import annotations

import pytest

from swarm_orchestrator.lanes import (
    LaneError,
    Touch,
    Wait,
    collide,
    legacy,
    overlaps,
    parse_touch,
    pick,
    repos_of,
)

KNOWN = {"frontend", "billing", "payments", ".", "@live-box", "@nats"}


def t(text: str) -> Touch:
    return parse_touch(text, KNOWN)


def lane(*texts: str) -> frozenset[Touch]:
    return frozenset(t(x) for x in texts)


# --- grammar ---------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("billing/src/http/bridge.rs", Touch("billing", ("src", "http", "bridge.rs"))),
    ("frontend/src/**", Touch("frontend", ("src", "**"))),
    ("frontend/**", Touch("frontend", ("**",))),
    ("./docs/x.md", Touch(".", ("docs", "x.md"))),
    ("./**", Touch(".", ("**",))),
    ("@live-box", Touch("@live-box", ())),
    ("frontend/tests/unit/jade-*.test.ts", Touch("frontend", ("tests", "unit", "jade-*.test.ts"))),
    ("  `frontend/src/**`  ", Touch("frontend", ("src", "**"))),
])
def test_accepted_forms_parse_and_round_trip(text, expected):
    touch = t(text)
    assert touch == expected
    assert t(str(touch)) == touch
    assert str(touch) == text.strip().strip("`")


@pytest.mark.parametrize("text", [
    "uploader/src/x.rs",            # unknown lane
    "@nowhere",                   # unknown resource
    "frontend/src*/x.ts",            # * outside the last segment
    "frontend/src/a*b*c.ts",         # two * in one segment
    "frontend/**/x.ts",              # ** not last
    "frontend/src/a**",              # ** not the whole segment
    "frontend/src//x.ts",            # empty segment
    "frontend/src/",                 # trailing slash
    "frontend",                      # repo with no path
    "./",                         # bare umbrella
    "frontend/../payments/x",           # .. segment
    "frontend/./x",                  # . segment
    "/etc/passwd",                # absolute
    "@live-box/x",                # resource with a path
])
def test_refusals(text):
    with pytest.raises(LaneError):
        t(text)


# --- overlap -----------------------------------------------------------------


@pytest.mark.parametrize("a, b, expected", [
    ("frontend/src/a.ts", "frontend/src/**", True),
    ("frontend/src/a/**", "frontend/src/b/**", False),
    ("frontend/x/a*b", "frontend/x/ac*db", True),
    ("frontend/x/a*.ts", "frontend/x/b*.ts", False),
    ("frontend/t/jade-1.test.ts", "frontend/t/jade-*.test.ts", True),
    ("frontend/t/ruby-1.test.ts", "frontend/t/jade-*.test.ts", False),
    ("frontend/t/ab", "frontend/t/ab*b", False),        # too short for prefix+suffix
    ("frontend/src/a.ts", "payments/src/a.ts", False),
    ("@live-box", "@live-box", True),
    ("@live-box", "@nats", False),
    ("./frontend/x", "frontend/x", False),
    ("frontend/**", "frontend/src/deep/x.ts", True),
    ("frontend/**", "frontend/t/jade-*.test.ts", True),
    ("frontend/src/**", "frontend/src", True),          # ** matches zero segments
    ("frontend/src/a.ts", "frontend/src/a.ts/b", False),
    ("frontend/a", "frontend/a", True),
    ("frontend/a", "frontend/b", False),
])
def test_overlap_truth_table_is_symmetric(a, b, expected):
    assert overlaps(t(a), t(b)) is expected
    assert overlaps(t(b), t(a)) is expected


def test_collide_returns_the_first_pair_or_none():
    left = lane("frontend/docs/x.md", "frontend/src/a.ts")
    assert collide(left, lane("frontend/src/**", "payments/**")) == (t("frontend/src/a.ts"), t("frontend/src/**"))
    assert collide(left, lane("frontend/tests/**", "@live-box")) is None


def test_legacy_is_the_whole_repo_including_the_umbrella():
    assert legacy(["frontend", "."]) == {Touch("frontend", ("**",)), Touch(".", ("**",))}
    assert str(Touch(".", ("**",))) == "./**"
    assert repos_of(lane("frontend/x", "./y", "@live-box")) == {"frontend", "."}


# --- scheduler -----------------------------------------------------------------


def test_two_disjoint_rows_in_one_repo_both_launch():
    lanes = {"A": lane("frontend/src/a.ts"), "B": lane("frontend/src/b.ts")}
    assert pick(["A", "B"], ["A", "B"], {}, lanes, 2) == (["A", "B"], {})


def test_overlapping_rows_launch_one_at_a_time():
    lanes = {"A": lane("frontend/src/**"), "B": lane("frontend/src/b.ts")}
    launch, waits = pick(["A", "B"], ["A", "B"], {}, lanes, 2)
    assert launch == ["A"]
    assert waits == {"B": Wait("A", t("frontend/src/**"), "held")}


def test_a_third_disjoint_row_waits_on_the_per_repo_cap():
    lanes = {p: lane(f"frontend/src/{p}.ts") for p in "ABC"}
    launch, waits = pick("CBA", "ABC", {}, lanes, 2)
    assert launch == ["A", "B"]
    assert waits == {"C": Wait("A", Touch("frontend", ("**",)), "per_repo")}


def test_a_blocked_broad_row_is_not_overtaken_but_a_disjoint_one_goes():
    held = {"run": lane("frontend/src/a.ts")}
    lanes = {"broad": lane("frontend/src/**"), "narrow": lane("frontend/src/b.ts"),
             "docs": lane("frontend/docs/x.md")}
    launch, waits = pick(lanes, ["broad", "narrow", "docs"], held, lanes, 3)
    assert launch == ["docs"]
    assert waits == {
        "broad": Wait("run", t("frontend/src/a.ts"), "held"),
        "narrow": Wait("broad", t("frontend/src/**"), "reserved"),
    }


def test_every_in_flight_state_holds_its_lane():
    held = {"waiting": lane("frontend/a"), "parked": lane("payments/b"), "integrating": lane("./c")}
    lanes = {"X": lane("frontend/**"), "Y": lane("payments/b"), "Z": lane("./**"), "W": lane("billing/d")}
    launch, waits = pick(lanes, "XYZW", held, lanes, 5)
    assert launch == ["W"]
    assert {p: (w.holder, w.why) for p, w in waits.items()} == {
        "X": ("waiting", "held"), "Y": ("parked", "held"), "Z": ("integrating", "held")}


def test_the_first_holder_in_sorted_order_is_reported():
    held = {"b-run": lane("frontend/src/x"), "a-run": lane("frontend/src/y")}
    _, waits = pick(["R"], ["R"], held, {"R": lane("frontend/src/**")}, 5)
    assert waits["R"] == Wait("a-run", t("frontend/src/y"), "held")


def test_legacy_rows_are_a_per_repo_mutex():
    lanes = {"A": legacy(["frontend"]), "B": legacy(["frontend"])}
    launch, waits = pick(["A", "B"], ["A", "B"], {}, lanes, 2)
    assert launch == ["A"]
    assert waits["B"] == Wait("A", Touch("frontend", ("**",)), "held")


def test_a_lane_less_row_neither_launches_nor_waits():
    assert pick(["A", "B"], ["A", "B"], {}, {"B": lane("frontend/x")}, 2) == (["B"], {})


def test_a_resource_serialises_rows_in_different_repos():
    lanes = {"A": lane("billing/x", "@live-box"), "B": lane("payments/y", "@live-box")}
    launch, waits = pick(["A", "B"], ["A", "B"], {}, lanes, 2)
    assert launch == ["A"]
    assert waits["B"] == Wait("A", t("@live-box"), "held")


def test_resources_do_not_count_toward_the_per_repo_cap():
    held = {"R": lane("@nats")}
    lanes = {"A": lane("@live-box")}
    assert pick(["A"], ["A"], held, lanes, 1) == (["A"], {})


def test_the_walk_follows_ledger_order_not_ready_order():
    lanes = {"early": lane("frontend/**"), "late": lane("frontend/x"), "stray": lane("frontend/y")}
    launch, waits = pick(["stray", "late", "early"], ["early", "late"], {}, lanes, 2)
    assert launch == ["early"]
    assert waits["late"].holder == "early"
    assert waits["stray"].holder == "early"
