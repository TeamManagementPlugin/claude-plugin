#!/usr/bin/env python3
"""Plugin runtime manifest tests (h-hook-port-boot-detector, commit 2).

Asserts plugin/hooks/hooks.json and plugin/.mcp.json are valid JSON, register
the expected events / MCP server through the _shim.py / bootstrap_mcp.py
launchers, and carry no absolute foreign paths (the stale /Users/halt/... entry
that this task replaced must never come back).

Run with: python3 -m pytest test/test_plugin_manifest_json.py -v
"""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO_ROOT / "plugin" / "hooks" / "hooks.json"
MCP_JSON = REPO_ROOT / "plugin" / "mcp" / "bootstrap_mcp.py"  # existence sanity
MCP_MANIFEST = REPO_ROOT / "plugin" / ".mcp.json"

_ABSOLUTE_PATH = re.compile(r"(/Users/|/home/|[A-Za-z]:\\\\|[A-Za-z]:/)")


def _all_commands(hooks_obj):
    for event_entries in hooks_obj["hooks"].values():
        for entry in event_entries:
            for h in entry["hooks"]:
                yield h["command"]


def test_hooks_json_valid_and_complete():
    obj = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    hooks = obj["hooks"]
    assert set(hooks) == {
        "UserPromptSubmit", "UserPromptExpansion", "PreToolUse", "PostToolUse",
        "SessionStart", "PreCompact"
    }
    # Two PreToolUse registrations (sessions-enforce + task-transcript-link).
    assert len(hooks["PreToolUse"]) == 2
    commands = list(_all_commands(obj))
    # 8 hook commands route through _shim.py (6 base + config_intent_gate on
    # both UserPromptSubmit and UserPromptExpansion).
    assert len(commands) == 8
    assert all("_shim.py" in c for c in commands)
    assert all("${CLAUDE_PLUGIN_ROOT}" in c for c in commands)
    expected_hooks = {
        "user-messages.py", "sessions-enforce.py", "task-transcript-link.py",
        "post-tool-use.py", "session-start.py", "pre-compact.py",
        "config_intent_gate.py",
    }
    assert expected_hooks == {c.split()[-1] for c in commands}


def test_intent_gate_wired_on_both_events():
    """config_intent_gate.py runs on UserPromptExpansion (primary) AND as a 2nd
    UserPromptSubmit hook (backup), per m-config-mcp-flow."""
    obj = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    hooks = obj["hooks"]
    # UserPromptSubmit: user-messages.py + config_intent_gate.py (one entry, 2 hooks).
    submit_cmds = [h["command"] for entry in hooks["UserPromptSubmit"] for h in entry["hooks"]]
    assert any("config_intent_gate.py" in c for c in submit_cmds)
    assert any("user-messages.py" in c for c in submit_cmds)
    # UserPromptExpansion: config_intent_gate.py.
    expansion_cmds = [h["command"] for entry in hooks["UserPromptExpansion"] for h in entry["hooks"]]
    assert any("config_intent_gate.py" in c for c in expansion_cmds)


def test_hooks_json_no_absolute_paths():
    for c in _all_commands(json.loads(HOOKS_JSON.read_text(encoding="utf-8"))):
        assert not _ABSOLUTE_PATH.search(c), f"absolute path leaked into hooks.json: {c!r}"


def test_mcp_json_points_at_bootstrap():
    assert MCP_JSON.exists(), "bootstrap_mcp.py is missing"
    obj = json.loads(MCP_MANIFEST.read_text(encoding="utf-8"))
    # Server key is `tm` (h-mcp-tool-namespace-refs): shortened from
    # `team-management` so the plugin tool prefix is
    # mcp__plugin_team-management_tm__<tool>, not the doubled form. Pinned here so
    # an accidental key change is caught alongside test_mcp_tool_namespace.py.
    assert set(obj["mcpServers"]) == {"tm"}, obj["mcpServers"]
    server = obj["mcpServers"]["tm"]
    args_joined = " ".join(server.get("args", []))
    assert "bootstrap_mcp.py" in args_joined
    assert "${CLAUDE_PLUGIN_ROOT}" in args_joined


def test_mcp_json_no_foreign_absolute_paths():
    raw = MCP_MANIFEST.read_text(encoding="utf-8")
    assert "/Users/halt" not in raw, "stale foreign path resurfaced in .mcp.json"
    obj = json.loads(raw)
    server = obj["mcpServers"]["tm"]
    for val in server.get("args", []) + list(server.get("env", {}).values()):
        assert not _ABSOLUTE_PATH.search(val), f"absolute path in .mcp.json: {val!r}"
