#!/usr/bin/env python3
"""Smoke tests for the module-level stdin guards on post-tool-use.py and
user-messages.py (h-fix-daic-enforcement-fail-open).

A bare `json.load(sys.stdin)` at module scope crashes with a traceback on empty
or malformed stdin, and the Claude Code harness treats a hook traceback as a
failure. These hooks now degrade an unparseable / non-dict payload to {} and run
their normal (no-op) path. sessions-enforce.py's stdin guard is covered in
test_sessions_enforce_daic.py; this file covers the two sibling hooks codex
flagged as also unguarded.

Each hook is invoked as a subprocess with raw stdin (mirroring how Claude Code
drives PostToolUse / UserPromptSubmit hooks).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"


def _make_project(tmp_path):
    """Minimal project tree: valid state, no active protocol (so no throttle
    injection), so a well-behaved hook simply exits 0."""
    state = tmp_path / ".claude" / "state"
    state.mkdir(parents=True)
    (tmp_path / "team-management" / "tasks").mkdir(parents=True)
    (state / "daic-mode.json").write_text(json.dumps({"mode": "discussion"}),
                                          encoding="utf-8")
    (state / "current_task.json").write_text(json.dumps(
        {"task": None, "branch": None, "services": [], "updated": None}),
        encoding="utf-8")
    return tmp_path


def _run(hook_name, project_root, raw_stdin):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project_root)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    result = subprocess.run(
        [sys.executable, str(HOOKS_DIR / hook_name)],
        input=raw_stdin, capture_output=True, text=True,
        cwd=str(project_root), env=env, timeout=20)
    return result.returncode, result.stdout, result.stderr


@pytest.mark.parametrize("hook_name", ["post-tool-use.py", "user-messages.py"])
@pytest.mark.parametrize("raw_stdin", ["", "{ not valid json", "[1, 2, 3]", "42"])
def test_hook_survives_bad_stdin(tmp_path, hook_name, raw_stdin):
    """Empty / malformed / valid-but-non-dict stdin must not crash the hook with
    a traceback, and it must exit 0 (non-blocking, no-op)."""
    project = _make_project(tmp_path)
    rc, _, stderr = _run(hook_name, project, raw_stdin)
    assert "Traceback (most recent call last)" not in stderr, \
        f"{hook_name} crashed on stdin={raw_stdin!r}: {stderr!r}"
    assert rc == 0, \
        f"{hook_name} on stdin={raw_stdin!r}: expected exit 0, got rc={rc}; err={stderr!r}"
