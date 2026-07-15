#!/usr/bin/env python3
"""
MCP server for team-management: issue tracking, git/MR, code review,
DAIC mode, protocol engine, notifications, release, and config tools.

Exposes 42 MCP tools across 8 tool modules, with automatic provider
detection and routing based on config.json:

- tools/issue_tracking.py  - 14 issue tracking tools (status/read/create/update/
                             sync/push/link/unlink/comment/set_status/api/
                             dependency, plus 2 config-status helpers)
- tools/protocol.py        - 11 protocol engine tools
- tools/code_review.py     -  4 code review tools (code_review, fetch_mr_review,
                             merge_request_comment, pull_request_comment)
- tools/git_operations.py  -  4 git/MR tools (git_commit, git_push,
                             merge_request_create, merge_request_update)
- tools/daic.py            -  3 DAIC mode switching tools
- tools/notifications.py   -  3 notification tools (notify_user,
                             notification_status,
                             notification_discover_telegram_chats)
- tools/release.py         -  1 release creation tool
- tools/config.py          -  2 config tools (config_get, config_update)

Canonical, drift-guarded inventory: plugin/mcp/CLAUDE.md ("MCP Tools
(42 total)") and test/test_mcp_tool_inventory.py.
Note: issue_dependency supports Gitea (full) and GitHub (same-repo only).
"""

import os
import sys

# Bootstrap for running as script (MCP server invocation)
# When run directly by MCP, there's no package context for relative imports.
# This adds the mcp directory to sys.path so absolute imports work.
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.insert(0, _script_dir)

from mcp.server.fastmcp import FastMCP

# Initialize core infrastructure (sets up project root and provider imports)
from core.project import setup_provider_imports
setup_provider_imports()

# Initialize FastMCP server
mcp = FastMCP("team-management")

# Register all tool modules
from tools import daic
from tools import release
from tools import git_operations
from tools import issue_tracking
from tools import code_review
from tools import protocol
from tools import notifications
from tools import config

daic.register_tools(mcp)
release.register_tools(mcp)
git_operations.register_tools(mcp)
issue_tracking.register_tools(mcp)
code_review.register_tools(mcp)
protocol.register_tools(mcp)       # 11 protocol engine tools
notifications.register_tools(mcp)  # 3 notification tools
config.register_tools(mcp)         # 2 config tools (config_get, config_update)

# ============================================================================
# SECURITY NOTE: workflow_toggle MCP tool INTENTIONALLY REMOVED
# ============================================================================
# The workflow_toggle tool was removed for security reasons to prevent Claude
# from autonomously disabling DAIC enforcement mechanisms. Allowing Claude to
# bypass its own behavioral constraints creates a backdoor that violates the
# core principle of enforced Discussion-Alignment-Implementation workflow.
#
# Workflow control remains available to users via:
# - Terminal: python3 $CLAUDE_PLUGIN_ROOT/hooks/workflow_command.py bypass [enable|disable]
#
# This ensures only the user can disable enforcement, maintaining protocol integrity.
# ============================================================================


def main():
    """Entry point for MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
