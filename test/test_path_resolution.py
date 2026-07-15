#!/usr/bin/env python3
"""Path-resolution refactor tests (h-plugin-foundation, commit 2).

Covers the three-root model introduced for the Claude Code plugin conversion:
  - get_project_root() reads CLAUDE_PROJECT_DIR first, cwd/.claude-walk fallback,
    and NO LONGER writes the env var (the old module-level side effect is gone).
  - get_plugin_root() reads CLAUDE_PLUGIN_ROOT, else Path(__file__).parent.parent.
  - get_plugin_data() reads CLAUDE_PLUGIN_DATA only — no __file__ fallback (spike
    F3: PLUGIN_ROOT is ephemeral, unfit for persistent state) — None when unset.

Run with: python3 -m pytest test/test_path_resolution.py -v
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import shared_state  # noqa: E402


def test_get_project_root_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert shared_state.get_project_root() == tmp_path


def test_get_project_root_ignores_nonexistent_env(tmp_path, monkeypatch):
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(missing))
    result = shared_state.get_project_root()
    assert result != missing
    assert result.exists()


def test_get_project_root_cwd_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    (tmp_path / ".claude").mkdir()
    monkeypatch.chdir(tmp_path)
    assert shared_state.get_project_root() == tmp_path


def test_get_project_root_does_not_write_env(tmp_path, monkeypatch):
    """The retired module-level `os.environ['CLAUDE_PROJECT_DIR'] = ...` write
    masked a plugin-injected value; get_project_root() must not mutate the env."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    (tmp_path / ".claude").mkdir()
    monkeypatch.chdir(tmp_path)
    shared_state.get_project_root()
    assert "CLAUDE_PROJECT_DIR" not in os.environ


def test_get_plugin_root_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert shared_state.get_plugin_root() == tmp_path


def test_get_plugin_root_ignores_nonexistent_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "nope"))
    # falls back to the source tree this file lives in: plugin/hooks -> plugin/
    assert shared_state.get_plugin_root() == HOOKS_DIR.parent


def test_get_plugin_root_file_fallback(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    assert shared_state.get_plugin_root() == HOOKS_DIR.parent


def test_get_plugin_data_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    assert shared_state.get_plugin_data() == tmp_path


def test_get_plugin_data_none_without_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    assert shared_state.get_plugin_data() is None


def test_imported_modules_live_under_plugin(monkeypatch):
    """codex plan-review test-gap guard: a stale rename would silently import a
    deployed `.claude/hooks` copy. Assert the imported module is the plugin source."""
    assert Path(shared_state.__file__).resolve() == (HOOKS_DIR / "shared_state.py").resolve()


def test_is_plugin_mode_true_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert shared_state.is_plugin_mode() is True


def test_is_plugin_mode_false_without_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    assert shared_state.is_plugin_mode() is False
