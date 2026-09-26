"""The ``PermissionRequest`` hook answers Yes to permission boxes, never to a
question put to the owner, and never to an event it cannot name."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "allow-permission.py"


def hook(event, tmp_path) -> str:
    raw = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run([sys.executable, str(HOOK)], input=raw, capture_output=True,
                          text=True, timeout=10,
                          env={"SWARM_STATE_DIR": str(tmp_path)}).stdout


@pytest.mark.parametrize("tool", ["AskUserQuestion", "ExitPlanMode"])
def test_the_permission_hook_never_answers_a_question(tool, tmp_path):
    assert hook({"hook_event_name": "PermissionRequest", "tool_name": tool,
                 "tool_input": {"questions": []}}, tmp_path) == ""
    assert not (tmp_path / "permissions.jsonl").exists()


@pytest.mark.parametrize("raw", ["", "not json", "{}", '{"tool_input": {}}'])
def test_the_permission_hook_answers_nothing_it_cannot_name(raw, tmp_path):
    assert hook(raw, tmp_path) == ""


def test_the_permission_hook_still_answers_a_tool_box(tmp_path):
    out = json.loads(hook({"tool_name": "Bash", "tool_input": {"command": "rm -rf $X/"}},
                          tmp_path))
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
