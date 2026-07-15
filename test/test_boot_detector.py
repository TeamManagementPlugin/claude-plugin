#!/usr/bin/env python3
"""boot_detector.detect_legacy_install tests (h-hook-port-boot-detector, commit 3).

Detection fires only when the anchor signal (a deployed legacy .claude/hooks/ dir)
is present AND at least one corroborating signal (settings.json hook entries OR a
duplicate team-management MCP registration) confirms it. Scope is the PROJECT.

Run with: python3 -m pytest test/test_boot_detector.py -v
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "plugin" / "hooks"))

import boot_detector  # noqa: E402


def _legacy_hooks(project: Path):
    hooks = project / ".claude" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "sessions-enforce.py").write_text("# legacy\n")


def _settings_with_hooks(project: Path):
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    (project / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [
            {"type": "command", "command": "python3 .claude/hooks/sessions-enforce.py"}]}]}
    }))


def _mcp_json(project: Path):
    (project / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"team-management": {"command": "python3", "args": []}}
    }))


def test_hooks_dir_plus_settings_fires(tmp_path):
    _legacy_hooks(tmp_path)
    _settings_with_hooks(tmp_path)
    result = boot_detector.detect_legacy_install(tmp_path)
    assert result is not None
    assert any("hooks" in s for s in result["signals"])
    assert any("settings.json" in s for s in result["signals"])


def test_hooks_dir_plus_mcp_fires(tmp_path):
    _legacy_hooks(tmp_path)
    _mcp_json(tmp_path)
    result = boot_detector.detect_legacy_install(tmp_path)
    assert result is not None
    assert any("MCP" in s for s in result["signals"])


def test_hooks_dir_alone_does_not_fire(tmp_path):
    _legacy_hooks(tmp_path)  # anchor only, no corroborating signal
    assert boot_detector.detect_legacy_install(tmp_path) is None


def test_settings_without_hooks_dir_does_not_fire(tmp_path):
    _settings_with_hooks(tmp_path)  # corroborator but no anchor
    assert boot_detector.detect_legacy_install(tmp_path) is None


def test_clean_project_returns_none(tmp_path):
    (tmp_path / ".claude").mkdir()
    assert boot_detector.detect_legacy_install(tmp_path) is None


def test_settings_with_unrelated_hooks_does_not_corroborate(tmp_path):
    _legacy_hooks(tmp_path)
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": "echo unrelated"}]}]}
    }))
    # hooks dir present but the settings entry doesn't reference our scripts and
    # there is no MCP dup → no corroboration → no fire.
    assert boot_detector.detect_legacy_install(tmp_path) is None
