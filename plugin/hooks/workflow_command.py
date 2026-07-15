#!/usr/bin/env python3
"""
Workflow Toggle Command

This script provides a command-line interface for managing the team-management
workflow state via Bypass Mode (instant, no restart):
   - Hooks run but skip all DAIC enforcement
   - Toggle instantly without Claude restart

Plugin mode has no console script — invoke it directly with the plugin python:
    python3 $CLAUDE_PLUGIN_ROOT/hooks/workflow_command.py            # Show status
    python3 $CLAUDE_PLUGIN_ROOT/hooks/workflow_command.py bypass     # Toggle bypass
    python3 $CLAUDE_PLUGIN_ROOT/hooks/workflow_command.py bypass enable
    python3 $CLAUDE_PLUGIN_ROOT/hooks/workflow_command.py bypass disable

Exit Codes:
    0 - Success
    1 - Error
"""

import sys
from pathlib import Path

# Add hooks directory to path for shared_state import
hooks_path = Path(__file__).parent
sys.path.insert(0, str(hooks_path))

try:
    from shared_state import (
        check_workflow_bypass,
        set_workflow_bypass,
        toggle_workflow_bypass,
        get_workflow_bypass_state,
        BYPASS_ENABLED_MSG,
        BYPASS_DISABLED_MSG
    )
except ImportError as e:
    print(f"ERROR: Failed to import shared_state module: {e}")
    print("TIP: Ensure the team-management plugin is installed correctly (shared_state.py must be importable from the plugin's hooks directory)")
    sys.exit(1)


# How this script is invoked (no console script in plugin mode).
_INVOKE = "python3 $CLAUDE_PLUGIN_ROOT/hooks/workflow_command.py"


def show_status():
    """Show current workflow status."""
    # Check bypass mode
    bypass_state = get_workflow_bypass_state()
    bypass_enabled = bypass_state.get("enabled", False)

    print("=" * 50)
    print("WORKFLOW STATUS")
    print("=" * 50)
    print()
    print(f"Bypass Mode:  {'ENABLED' if bypass_enabled else 'DISABLED'}")
    if bypass_enabled and bypass_state.get("reason"):
        print(f"  Reason: {bypass_state['reason']}")
    if bypass_enabled and bypass_state.get("updated"):
        print(f"  Since: {bypass_state['updated']}")
    print()
    print("-" * 50)
    print("Available commands:")
    print(f"  {_INVOKE} bypass          - Toggle bypass mode (instant)")
    print(f"  {_INVOKE} bypass enable   - Enable bypass")
    print(f"  {_INVOKE} bypass disable  - Disable bypass")
    print("-" * 50)


def handle_bypass(action: str = None):
    """Handle bypass mode commands."""
    try:
        if action is None or action == "":
            # Toggle
            message = toggle_workflow_bypass()
            new_state = check_workflow_bypass()
            print(f"SUCCESS: Bypass mode {'ENABLED' if new_state else 'DISABLED'}")
            print(f">> {message}")
        elif action in ["enable", "on", "1", "true"]:
            if check_workflow_bypass():
                print("INFO: Bypass mode is already enabled")
            else:
                set_workflow_bypass(True, reason="Manual enable via workflow_command")
                if not check_workflow_bypass():
                    print("ERROR: Failed to enable bypass mode - state file not updated")
                    sys.exit(1)
                print("SUCCESS: Bypass mode ENABLED")
                print(f">> {BYPASS_ENABLED_MSG}")
        elif action in ["disable", "off", "0", "false"]:
            if not check_workflow_bypass():
                print("INFO: Bypass mode is already disabled")
            else:
                set_workflow_bypass(False)
                if check_workflow_bypass():
                    print("ERROR: Failed to disable bypass mode - state file not updated")
                    sys.exit(1)
                print("SUCCESS: Bypass mode DISABLED")
                print(f">> {BYPASS_DISABLED_MSG}")
        else:
            print(f"ERROR: Invalid bypass action: '{action}'")
            print("Valid actions: enable, disable, or omit for toggle")
            sys.exit(1)
    except Exception as e:
        print(f"ERROR: Exception in bypass mode handling: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        sys.exit(1)


def main():
    """Main entry point for workflow command."""
    args = sys.argv[1:]

    if len(args) == 0:
        # No arguments - show status
        show_status()
        return

    level = args[0].lower().strip()
    action = args[1].lower().strip() if len(args) > 1 else None

    if level == "bypass":
        handle_bypass(action)
    elif level == "status":
        show_status()
    else:
        print(f"ERROR: Unknown command: '{level}'")
        print()
        print("Usage:")
        print(f"  {_INVOKE}                 - Show status")
        print(f"  {_INVOKE} bypass [action] - Bypass mode (instant)")
        print()
        print("Actions: enable, disable, or omit for toggle")
        sys.exit(1)


if __name__ == "__main__":
    main()
