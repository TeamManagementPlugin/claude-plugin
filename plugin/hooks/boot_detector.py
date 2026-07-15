#!/usr/bin/env python3
"""Boot detector — find a legacy team-management install living alongside the plugin.

When the plugin is enabled in a project that STILL carries the old installer-
deployed layout — `.claude/hooks/*.py` scripts, hook entries in
`.claude/settings.json`, and/or a `team-management` MCP server registered — BOTH
hook sets fire on every event (spike F1 = MERGE), producing double DAIC
enforcement / double MCP and silently corrupting the workflow.

``detect_legacy_install(project_root)`` returns a dict naming the signals that
fired (for the advisory / block message) or ``None`` when the project is clean.
Detection is scoped to the PROJECT (`.claude/settings.json` + `.mcp.json`); it
does NOT inspect user-level `~/.claude`. Pure stdlib + hook_utils.
"""
import json
from pathlib import Path

from hook_utils import normalize_command

# Hook scripts the legacy installer deploys into .claude/hooks/.
_LEGACY_HOOK_FILES = (
    "sessions-enforce.py", "session-start.py", "post-tool-use.py",
    "user-messages.py", "task-transcript-link.py", "pre-compact.py",
)
_MCP_SERVER_NAME = "team-management"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _has_legacy_hooks_dir(project_root: Path) -> bool:
    """A deployed legacy hooks dir = .claude/hooks/ holding our hook scripts."""
    hooks = project_root / ".claude" / "hooks"
    return any((hooks / name).exists() for name in _LEGACY_HOOK_FILES)


def _has_settings_hook_entries(project_root: Path) -> bool:
    """settings.json hook commands referencing the legacy .claude/hooks/ scripts."""
    settings = _read_json(project_root / ".claude" / "settings.json")
    if not isinstance(settings, dict):
        return False
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            for h in (entry or {}).get("hooks", []) or []:
                cmd = normalize_command((h or {}).get("command", ""))
                if ".claude/hooks/" in cmd and any(name in cmd for name in _LEGACY_HOOK_FILES):
                    return True
    return False


def _has_duplicate_mcp(project_root: Path) -> bool:
    """The team-management MCP server registered in project .mcp.json or settings.json."""
    for rel in (".mcp.json", ".claude/settings.json"):
        data = _read_json(project_root / rel)
        if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
            if _MCP_SERVER_NAME in data["mcpServers"]:
                return True
    return False


def detect_legacy_install(project_root):
    """Return ``{'signals': [...]}`` when a legacy install coexists with the plugin,
    else ``None``.

    Fires only when the anchor signal (a deployed legacy hooks dir) is present AND
    at least one corroborating signal (settings.json hook entries or a duplicate
    MCP registration) confirms it — so a single leftover file does not trip the
    block on its own.
    """
    project_root = Path(project_root)
    has_hooks_dir = _has_legacy_hooks_dir(project_root)
    has_settings = _has_settings_hook_entries(project_root)
    has_mcp = _has_duplicate_mcp(project_root)

    signals = []
    if has_hooks_dir:
        signals.append("legacy .claude/hooks/ scripts")
    if has_settings:
        signals.append("team-management hook entries in .claude/settings.json")
    if has_mcp:
        signals.append("duplicate 'team-management' MCP server registration")

    if has_hooks_dir and (has_settings or has_mcp):
        return {"signals": signals}
    return None
