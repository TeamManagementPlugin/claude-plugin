#!/usr/bin/env python3
"""SessionStart blanket `.claude/` gitignore tests (h-fix-mcp-token-and-claude-gitignore).

`.claude/` holds per-machine / runtime / system files (state/, logs/,
settings.local.json, plugins/, …) that must never be tracked. The SessionStart
hook now ignores the whole `.claude/` dir (was: only `.claude/settings.local.json`),
migrating the legacy narrow line. Team-share via a committed `.claude/settings.json`
is dropped (per-developer plugin enable).

Run with: python3 -m pytest test/test_session_start_claude_gitignore.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "plugin"
HOOK = PLUGIN_DIR / "hooks" / "session-start.py"


def _project(tmp_path):
    (tmp_path / ".claude" / "state").mkdir(parents=True)
    (tmp_path / "team-management").mkdir()
    return tmp_path


def _run(project, *, plugin_mode=True):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    if plugin_mode:
        env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_DIR)
    else:
        env.pop("CLAUDE_PLUGIN_ROOT", None)
    payload = json.dumps({"hook_event_name": "SessionStart", "source": "startup"})
    return subprocess.run([sys.executable, str(HOOK)], input=payload,
                          capture_output=True, text=True, cwd=str(project),
                          env=env, timeout=20)


def _lines(project):
    gi = project / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.exists() else ""
    return [ln.rstrip() for ln in text.splitlines() if ln.rstrip()]


def test_adds_claude_dir_when_gitignore_absent(tmp_path):
    project = _project(tmp_path)
    r = _run(project)
    assert r.returncode == 0, r.stderr
    assert ".claude/" in _lines(project)


def test_idempotent_when_already_present(tmp_path):
    project = _project(tmp_path)
    (project / ".gitignore").write_text(".claude/\n", encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    assert _lines(project).count(".claude/") == 1


def test_recognizes_blanket_dot_claude_no_slash(tmp_path):
    project = _project(tmp_path)
    (project / ".gitignore").write_text(".claude\n", encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    lines = _lines(project)
    assert ".claude" in lines
    assert ".claude/" not in lines  # already covered → no duplicate


def test_legacy_settings_local_upgraded_to_blanket(tmp_path):
    project = _project(tmp_path)
    (project / ".gitignore").write_text(
        "node_modules/\n.claude/settings.local.json\n", encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    lines = _lines(project)
    assert ".claude/" in lines                          # blanket rule added
    assert ".claude/settings.local.json" not in lines   # legacy narrow line migrated away
    assert "node_modules/" in lines                     # unrelated line preserved


def test_preserves_other_lines(tmp_path):
    project = _project(tmp_path)
    (project / ".gitignore").write_text("*.pyc\nbuild/\n", encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    lines = _lines(project)
    assert "*.pyc" in lines and "build/" in lines and ".claude/" in lines


def test_generic_settings_local_rule_preserved(tmp_path):
    """A GENERIC `settings.local.json` rule matches that filename ANYWHERE in the
    tree — it is the user's own rule, NOT the `.claude/`-scoped legacy line, and
    must be preserved (codex P2: removing it could un-ignore unrelated files)."""
    project = _project(tmp_path)
    (project / ".gitignore").write_text("settings.local.json\n", encoding="utf-8")
    r = _run(project)
    assert r.returncode == 0, r.stderr
    lines = _lines(project)
    assert "settings.local.json" in lines  # generic rule preserved
    assert ".claude/" in lines


def test_not_touched_outside_plugin_mode(tmp_path):
    project = _project(tmp_path)
    r = _run(project, plugin_mode=False)
    assert r.returncode == 0, r.stderr
    assert not (project / ".gitignore").exists()
