#!/usr/bin/env python3
"""Behavioral tests for the bypass CLI `plugin/hooks/workflow_command.py`.

This 139-line command had zero test references. It is invoked as
`python3 workflow_command.py [bypass [enable|disable]]` and manages the workflow
bypass state in `.claude/state/workflow-bypass.json`. Covered here:
  * no args           -> exit 0, prints the status banner
  * bypass enable      -> exit 0, workflow-bypass.json enabled=True
  * bypass disable     -> exit 0, enabled=False
  * bypass (toggle)    -> exit 0, state flips
  * unknown command    -> exit 1, "Unknown command"

Each case runs the script as a subprocess (mirroring real invocation) over a
temp project, with CLAUDE_PROJECT_DIR pointed at the temp dir and any inherited
CLAUDE_PLUGIN_ROOT scrubbed (mirror test_hook_stdin_guards.py:41-48) so a dogfood
environment cannot pull the subprocess toward the real project state.

Run with: python3 -m pytest test/test_workflow_command.py -v
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CMD_PATH = REPO_ROOT / "plugin" / "hooks" / "workflow_command.py"


def _make_project(tmp_path):
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    return tmp_path


def _env(project_root):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project_root)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return env


def _run(project_root, *args):
    result = subprocess.run(
        [sys.executable, str(CMD_PATH), *args],
        capture_output=True, text=True,
        cwd=str(project_root), env=_env(project_root), timeout=20)
    return result.returncode, result.stdout, result.stderr


def _bypass_file(project_root):
    return project_root / ".claude" / "state" / "workflow-bypass.json"


def _read_enabled(project_root):
    return json.loads(_bypass_file(project_root).read_text(encoding="utf-8"))["enabled"]


def test_no_args_shows_status(tmp_path):
    project = _make_project(tmp_path)
    rc, stdout, stderr = _run(project)
    assert rc == 0, f"rc={rc} stderr={stderr!r}"
    assert "WORKFLOW STATUS" in stdout


def test_bypass_enable_mutates_state(tmp_path):
    project = _make_project(tmp_path)
    rc, stdout, _ = _run(project, "bypass", "enable")
    assert rc == 0
    assert "ENABLED" in stdout
    assert _read_enabled(project) is True


def test_bypass_disable_mutates_state(tmp_path):
    project = _make_project(tmp_path)
    _run(project, "bypass", "enable")
    rc, stdout, _ = _run(project, "bypass", "disable")
    assert rc == 0
    assert "DISABLED" in stdout
    assert _read_enabled(project) is False


def test_bypass_toggle_flips_state(tmp_path):
    project = _make_project(tmp_path)
    # Establish a known ENABLED starting point (enable always writes the file;
    # `disable` on an already-disabled fresh project is a no-op that writes
    # nothing — workflow_command.py:91-92), then toggle and assert it flipped.
    _run(project, "bypass", "enable")
    assert _read_enabled(project) is True
    rc, _, _ = _run(project, "bypass")
    assert rc == 0
    assert _read_enabled(project) is False


def test_unknown_command_exits_1(tmp_path):
    project = _make_project(tmp_path)
    rc, stdout, stderr = _run(project, "frobnicate")
    assert rc == 1
    assert "Unknown command" in (stdout + stderr)


def test_invalid_bypass_action_exits_1(tmp_path):
    """`bypass <garbage>` hits handle_bypass's else branch (distinct from the
    unknown-top-level-command path) -> exit 1, 'Invalid bypass action'."""
    project = _make_project(tmp_path)
    rc, stdout, stderr = _run(project, "bypass", "frobnicate")
    assert rc == 1
    assert "Invalid bypass action" in (stdout + stderr)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
