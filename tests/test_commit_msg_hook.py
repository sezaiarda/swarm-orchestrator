"""The tracked ``commit-msg`` hook (``.githooks/commit-msg``).

This repository is public, and sessions that work on a private project commit
here. The hook refuses a message that carries that project's work-item id. The
table below is the matcher's contract: every shape of id it must catch, and
the ordinary hyphen-and-digit words it must leave alone.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / ".githooks" / "commit-msg"


def _load():
    loader = importlib.machinery.SourceFileLoader("commit_msg_hook", str(HOOK))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


hook = _load()

REJECT = [
    ("demo-F12: fix the gate", ["demo-F12"]),
    ("demo-W3 the web board keeps its title", ["demo-W3"]),
    ("demo-P0. A first cut of the loader", ["demo-P0"]),
    ("demo-W4a: the second half", ["demo-W4a"]),
    ("(demo-F12) fix the gate", ["demo-F12"]),
    ("fix the gate (demo-F12)", ["demo-F12"]),
    ("Demo-F7: capitalised at the start of a sentence", ["Demo-F7"]),
    ("demo-F2 and demo-W4 in one line", ["demo-F2", "demo-W4"]),
    ("two-part-W10 is caught by its last word", ["part-W10"]),
    ("k8s-W3: a word with a digit in it", ["k8s-W3"]),
    ("a subject\n\ndemo-F22. The body starts with the id", ["demo-F22"]),
    ("Merge branch 'swarm/demo-F24'", ["demo-F24"]),
    # Synthetic ids from test descriptions are flagged too: a message never
    # needs one, and no reader can tell a made-up id from a real one.
    ("a worker (worker:navy-W8) kept its window", ["navy-W8"]),
]

ACCEPT = [
    "read the ledger as utf-8",
    "sha-256 of the path, x86-64 only, python-3 syntax, v1-2 of the wire",
    "the forecast prints P50-P85",
    "workers-2 and workers-3 page the slots",
    "pin claude-opus-5 and claude-haiku-4-5",
    "tag bundle-v0.1.0",
    "press Ctrl-U once; ISO-8859-1 and UTF-8 both load",
    "RFC-2119 words, E2E-T1 naming",
    "a one-letter word is no id: a-B1",
    "demo-F12x2 runs on, so it is not that shape",
    "demo-Fix and demo-FW3 have no single capital before digits",
    "later: a phase that waits for a date keeps its work",
    "gc: take the build gate through the queue, never one slot at a time",
    "Merge branch 'master' into display-name",
]


@pytest.mark.parametrize("message,ids", REJECT)
def test_a_message_with_a_work_item_id_is_caught(message, ids):
    assert [ident for _, ident in hook.work_ids(message)] == ids


@pytest.mark.parametrize("message", ACCEPT)
def test_ordinary_hyphenated_words_pass(message):
    assert hook.work_ids(message) == []


def test_the_line_of_each_id_is_reported():
    assert hook.work_ids("subject\n\nbody\ndemo-F1 here") == [(4, "demo-F1")]


def test_comments_and_the_verbose_diff_are_not_the_message():
    text = ("fix the gate\n"
            "# On branch swarm/demo-F12\n"
            f"{hook.SCISSORS}\n"
            "+    phase = 'demo-W3'\n")
    assert hook.work_ids(text) == []


def _run(tmp_path, message, **env):
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(message, encoding="utf-8")
    clean = {k: v for k, v in os.environ.items() if k != hook.ESCAPE}
    return subprocess.run([sys.executable, str(HOOK), str(path)], capture_output=True,
                          text=True, env={**clean, **env})


def test_the_hook_refuses_and_says_how_to_fix_it(tmp_path):
    got = _run(tmp_path, "demo-F12: fix the gate\n")
    assert got.returncode == 1
    assert "demo-F12 (line 1)" in got.stderr
    assert "describe the change; leave the work-item id out" in got.stderr
    assert f"{hook.ESCAPE}=1" in got.stderr


def test_the_hook_passes_a_clean_message(tmp_path):
    got = _run(tmp_path, "fix the gate\n\nIt read utf-8 as latin-1.\n")
    assert (got.returncode, got.stderr) == (0, "")


def test_the_escape_lets_a_deliberate_message_through(tmp_path):
    assert _run(tmp_path, "document the hook: demo-F12 is refused\n",
                **{hook.ESCAPE: "1"}).returncode == 0
    assert _run(tmp_path, "demo-F12\n", **{hook.ESCAPE: "0"}).returncode == 1


def test_the_hook_is_executable_and_git_runs_it(tmp_path):
    assert os.access(HOOK, os.X_OK)
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {k: v for k, v in os.environ.items() if k != hook.ESCAPE and not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True)

    assert git("init", "-q").returncode == 0
    assert git("config", "core.hooksPath", str(HOOK.parent)).returncode == 0
    refused = git("commit", "-q", "--allow-empty", "-m", "demo-F12: fix the gate")
    assert refused.returncode != 0 and "work-item id" in refused.stderr
    assert git("commit", "-q", "--allow-empty", "-m", "fix the gate").returncode == 0
    assert git("log", "--format=%s").stdout.split() == ["fix", "the", "gate"]
