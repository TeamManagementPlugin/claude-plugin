#!/usr/bin/env python3
"""Tests for the P5 notification mode gate (notifications.mode = per_step | off).

The gate lives at the top of notification_utils.send_protocol_notification:
per-step pings (is_complete=False) are suppressed when mode == "off"; the
completion ping (is_complete=True) is never suppressed by mode; a missing
`mode` key defaults to "per_step" (back-compat for old / silent / disabled
configs).

Run with: python3 -m pytest test/test_notification_mode.py -v
"""

import sys
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import notification_utils  # noqa: E402


class NotificationModeGateTest(TestCase):
    """send_protocol_notification must honour notifications.mode for per-step
    pings while always allowing the completion ping through."""

    def _delivered(self, mode, is_complete):
        """Drive send_protocol_notification with a stubbed config + delivery
        sink; return True iff the underlying send_notification was invoked."""
        cfg = {"enabled": True}
        if mode is not None:
            cfg["mode"] = mode
        with patch.object(notification_utils, "_get_notification_config", return_value=cfg), \
                patch.object(notification_utils, "send_notification") as mock_send:
            notification_utils.send_protocol_notification(
                "task", 1, "implementation", 5,
                is_complete=is_complete, summary="s", task_name="t",
            )
        return mock_send.called

    def test_off_mode_suppresses_per_step(self):
        self.assertFalse(self._delivered("off", is_complete=False))

    def test_off_mode_allows_completion(self):
        self.assertTrue(self._delivered("off", is_complete=True))

    def test_per_step_mode_sends_per_step(self):
        self.assertTrue(self._delivered("per_step", is_complete=False))

    def test_missing_mode_defaults_to_per_step(self):
        self.assertTrue(self._delivered(None, is_complete=False))


if __name__ == "__main__":
    main()
