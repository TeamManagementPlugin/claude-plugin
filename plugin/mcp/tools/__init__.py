"""
MCP tool modules for team-management.

Each module provides a register_tools(mcp) function that registers
its tools with the FastMCP server instance.

Import directly from submodules:
    from tools import daic, release, git_operations
    from tools.daic import register_tools
"""

# Note: Re-exports removed to support running server.py as script.
# Import modules directly: from tools import daic (works because tools/ is in sys.path)
