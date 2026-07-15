#!/usr/bin/env python3
"""Namespace drift-guard for plugin MCP tool references (h-mcp-tool-namespace-refs).

A plugin-bundled MCP server's tools are namespaced
`mcp__plugin_<plugin-name>_<server-name>__<tool>` (Claude Code docs, mcp.md
"Plugin MCP tool names"). For this plugin — plugin name `team-management`, and
the `plugin/.mcp.json` server key shortened to `tm` — the canonical tool form is:

    mcp__plugin_team-management_tm__<tool>

The whole `plugin/` tree must therefore be free of:
  - the legacy pip-install form `mcp__team-management__<tool>` — tools that do NOT
    exist by that name in the plugin runtime (the bug this task fixed), and
  - the doubled form `mcp__plugin_team-management_team-management__<tool>` — which
    would reappear if the `plugin/.mcp.json` server key were reverted to
    `team-management` (a regression guard on the key-shortening decision).

Both would break for plugin users: the behavioral guidance would name tools that
the runtime does not expose under that name. This is the automated form of the
task gate `grep -rnE 'mcp__team-management__|_team-management_team-management__' plugin/`.

Scope is `plugin/` ONLY. The dev repo-root `.mcp.json` registers the server at the
top level (legacy `mcp__team-management__*`), and the legacy `team_management/`
package + docs/wiki legitimately keep the bare form — none of those are scanned.

Mirrors `test/test_command_namespace.py` (the m-namespace-rename drift-guard).

Run with: python3 -m pytest test/test_mcp_tool_namespace.py -v
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "plugin"

# Legacy bare form OR the doubled form (server key not shortened to `tm`).
# NB: `mcp__plugin_team-management_tm__<tool>` (the canonical form) matches
# NEITHER alternative, so it passes cleanly.
_LEGACY_REF = re.compile(r"mcp__team-management__|_team-management_team-management__")

# Text-ish files worth scanning (the plugin ships markdown / python / config).
# `.pyc` is deliberately absent → stale __pycache__ bytecode is skipped.
_TEXT_SUFFIXES = {
    ".md", ".py", ".json", ".txt", ".in", ".lock", ".cfg", ".toml",
    ".sh", ".js", ".yml", ".yaml", "",
}


def test_plugin_tree_has_no_legacy_mcp_tool_refs():
    """Walk plugin/ and assert no text file names an MCP tool by the legacy
    (`mcp__team-management__`) or doubled (`_team-management_team-management__`)
    form. Fails loudly with file:line so the offender is obvious."""
    violations = []
    for path in sorted(PLUGIN.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _LEGACY_REF.search(line):
                rel = path.relative_to(REPO)
                violations.append(f"{rel}:{lineno}: {line.strip()[:100]}")
    assert not violations, (
        "legacy/doubled MCP tool refs remain in plugin/ "
        "(expected `mcp__plugin_team-management_tm__<tool>`):\n"
        + "\n".join(violations)
    )
