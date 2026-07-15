"""
DAIC mode switching MCP tools.

Provides tools for switching between discussion, implementation, and documentation modes
in the DAIC (Discussion-Alignment-Implementation-Check) workflow.
"""

from typing import Dict, Any

from core.project import setup_provider_imports


def register_tools(mcp):
    """Register DAIC tools with the FastMCP server."""

    @mcp.tool()
    def daic_mode_switch_discussion() -> Dict[str, Any]:
        """
        Switch to DAIC discussion mode (blocks Edit/Write tools).

        This tool enters discussion mode where code editing tools are blocked,
        encouraging discussion and alignment before implementation. This is the
        safe default mode and can typically be auto-approved by users.

        Returns:
            Dict with:
            - success: bool - Whether mode switch succeeded
            - previous_mode: str - Mode before switch
            - current_mode: str - Always "discussion" on success
            - message: str - User-facing description of new mode
            - error: str - Error message if switch failed (only present on failure)

        Examples:
            - Returning to discussion after completing implementation
            - Forcing discussion mode before starting a new topic
        """
        try:
            setup_provider_imports()

            from shared_state import set_daic_mode, check_daic_mode_raw, DISCUSSION_MODE_MSG

            previous_mode = check_daic_mode_raw()
            set_daic_mode(True)

            return {
                "success": True,
                "previous_mode": previous_mode,
                "current_mode": "discussion",
                "message": DISCUSSION_MODE_MSG
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to switch to discussion mode: {str(e)}",
                "hint": "Ensure the team-management plugin is installed correctly (shared_state.py must be importable from the plugin's hooks directory)."
            }

    @mcp.tool()
    def daic_mode_switch_implementation() -> Dict[str, Any]:
        """
        Switch to DAIC implementation mode (enables Edit/Write tools).

        This tool enters implementation mode where code editing tools are allowed.
        This is the active mode for making code changes. Users may want to require
        confirmation before approving this tool call as it bypasses the
        discussion-first safeguard.

        IMPORTANT: This tool enables code modification capabilities.

        Returns:
            Dict with:
            - success: bool - Whether mode switch succeeded
            - previous_mode: str - Mode before switch
            - current_mode: str - Always "implementation" on success
            - message: str - User-facing description of new mode
            - reminder: str - Reminder to return to discussion mode after implementation
            - error: str - Error message if switch failed (only present on failure)

        Examples:
            - Starting implementation after discussion and alignment
            - Resuming implementation work on an existing task
        """
        try:
            setup_provider_imports()

            from shared_state import set_daic_mode, check_daic_mode_raw, IMPLEMENTATION_MODE_MSG

            previous_mode = check_daic_mode_raw()
            set_daic_mode(False)

            return {
                "success": True,
                "previous_mode": previous_mode,
                "current_mode": "implementation",
                "message": IMPLEMENTATION_MODE_MSG,
                "reminder": "Remember to return to discussion mode after completing your implementation."
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to switch to implementation mode: {str(e)}",
                "hint": "Ensure the team-management plugin is installed correctly (shared_state.py must be importable from the plugin's hooks directory)."
            }

    @mcp.tool()
    def daic_mode_switch_documentation() -> Dict[str, Any]:
        """
        Switch to DAIC documentation mode (allows docs-only edits).

        This tool enters documentation mode where only documentation files
        (CLAUDE.md, task files, docs/) can be edited. Source code editing
        remains blocked. Useful for updating documentation without entering
        full implementation mode.

        Returns:
            Dict with:
            - success: bool - Whether mode switch succeeded
            - previous_mode: str - Mode before switch
            - current_mode: str - Always "documentation" on success
            - message: str - User-facing description of new mode
            - error: str - Error message if switch failed (only present on failure)

        Examples:
            - Updating CLAUDE.md files after architectural changes
            - Writing task documentation without enabling code edits
        """
        try:
            setup_provider_imports()

            from shared_state import set_daic_mode, check_daic_mode_raw, DOCUMENTATION_MODE_MSG

            previous_mode = check_daic_mode_raw()
            set_daic_mode("documentation")

            return {
                "success": True,
                "previous_mode": previous_mode,
                "current_mode": "documentation",
                "message": DOCUMENTATION_MODE_MSG
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to switch to documentation mode: {str(e)}",
                "hint": "Ensure the team-management plugin is installed correctly (shared_state.py must be importable from the plugin's hooks directory)."
            }
