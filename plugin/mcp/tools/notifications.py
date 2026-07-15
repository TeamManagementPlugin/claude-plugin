"""
Notification MCP tools.

Provides tools for sending notifications and checking notification configuration.
"""

from typing import Dict, Any

from core.project import _import_from_hooks


def register_tools(mcp):
    """Register notification tools with the FastMCP server."""

    @mcp.tool()
    def notify_user(message: str) -> Dict[str, Any]:
        """
        Send a notification to the user through configured channels (e.g. Telegram).

        Use this when you want to alert the user about something important,
        for example when a long-running task finishes or when you need the
        user's input and they may not be watching the terminal.

        The notification is delivered asynchronously and will never block
        the workflow even if delivery fails.

        Args:
            message: The notification text to send.

        Returns:
            Dict with:
            - success: bool - Whether the notification was dispatched
            - message: str - Status description
            - error: str - Error message (only present on failure)
        """
        if not message or not message.strip():
            return {
                "success": False,
                "error": "Message cannot be empty.",
                "hint": "Provide a non-empty message to send.",
            }

        try:
            notification_utils = _import_from_hooks("notification_utils")

            status = notification_utils.get_notification_status()
            if not status["enabled"]:
                return {
                    "success": False,
                    "error": "Notifications are disabled in configuration.",
                    "hint": "Enable notifications in team-management/config.json → notifications.enabled",
                }

            if status["configured_channel_count"] == 0:
                return {
                    "success": False,
                    "error": "No notification channels are configured.",
                    "hint": "Configure at least one channel (e.g. Telegram) in team-management/config.json",
                }

            notification_utils.send_user_notification(message)
            return {
                "success": True,
                "message": f"Notification dispatched to {status['configured_channel_count']} channel(s).",
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to send notification: {str(e)}",
                "hint": "Ensure the team-management plugin is installed correctly (notification_utils.py must be importable from the plugin's hooks directory).",
            }

    @mcp.tool()
    def notification_status() -> Dict[str, Any]:
        """
        Check the notification subsystem configuration and status.

        Returns details about whether notifications are enabled, the message
        prefix, and the state of each configured channel.

        Returns:
            Dict with:
            - success: bool
            - enabled: bool - Whether notifications are enabled
            - prefix: str - Message prefix
            - channels: list - Per-channel status dicts
            - configured_channel_count: int - Number of properly configured channels
        """
        try:
            notification_utils = _import_from_hooks("notification_utils")

            status = notification_utils.get_notification_status()
            return {
                "success": True,
                **status,
            }

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to check notification status: {str(e)}",
                "hint": "Ensure the team-management plugin is installed correctly (notification_utils.py must be importable from the plugin's hooks directory).",
            }

    @mcp.tool()
    def notification_discover_telegram_chats(test_chat_id: str = "") -> Dict[str, Any]:
        """Discover Telegram chats the configured bot can reach — or send a test ping.

        Telegram has NO Bot API endpoint that lists every group a bot belongs to,
        so discovery works off getUpdates: with test_chat_id empty this calls
        getMe (validate the token + read the bot identity) then getUpdates, and
        returns the distinct chats seen in RECENT updates. A chat appears only if
        it recently messaged the bot (in privacy-mode groups: a command / @mention;
        in DMs: the user pressed Start) or the bot was just added to it. If the bot
        has a webhook configured, getUpdates returns 409 and this reports it.

        With test_chat_id set, it instead sends a one-line confirmation message to
        that chat so the user can verify it is the right one BEFORE saving
        notifications.channels.telegram.chat_id via config_update.

        Use during /team-management:config to fill in the Telegram chat id without
        hand-copying it. The bot token is read from the per-project token file
        (.claude/state/provider-tokens.json, key 'telegram') — it is NEVER passed
        to this tool.

        Args:
            test_chat_id: When non-empty, send a confirmation ping to this chat id
                instead of discovering chats.

        Returns (discovery mode):
            success, bot, chats: [{id, type, title}], hint / error.
        Returns (test-ping mode):
            success, message / error.
        """
        try:
            notification_utils = _import_from_hooks("notification_utils")

            if test_chat_id and test_chat_id.strip():
                result = notification_utils.send_telegram_test_message(test_chat_id.strip())
                if result.get("ok"):
                    return {
                        "success": True,
                        "message": f"Test message sent to chat {test_chat_id.strip()}. Ask the user whether it arrived before saving the chat id.",
                    }
                out = {"success": False, "error": result.get("error", "Send failed.")}
                if result.get("hint"):
                    out["hint"] = result["hint"]
                return out

            result = notification_utils.discover_telegram_chats()
            return {"success": bool(result.get("ok")), **result}

        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to run Telegram discovery: {str(e)}",
                "hint": "Ensure the team-management plugin is installed correctly (notification_utils.py must be importable from the plugin's hooks directory).",
            }
