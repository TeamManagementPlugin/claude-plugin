#!/usr/bin/env python3
"""Subprocess tests for the session-start plugin-update notice
(m-plugin-update-changelog).

session-start.py appends a "[Plugin updated]" block to additionalContext when
the shipped plugin version (plugin.json under CLAUDE_PLUGIN_ROOT) is newer
than the persistent last-seen marker. The marker lives at
CLAUDE_PLUGIN_DATA/tm-last-seen-version.json when that env var is set, else
at .claude/state/tm-last-seen-version.json (the tm- prefix guards against
collisions with other plugins) — and is NEVER cleared by session
start (it fires once per UPDATE, not once per session, unlike the migration
flags). Every case must exit 0 with a single parseable JSON document.

Harness modelled on test_session_start_deprecation_warning.py: subprocess-run
the hook with cwd + CLAUDE_PROJECT_DIR at a temp project, and a FAKE plugin
root via CLAUDE_PLUGIN_ROOT so the tests control plugin.json + CHANGELOG.md.

Run with: python3 -m pytest test/test_session_start_update_notice.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main

PROJECT_ROOT = Path(__file__).parent.parent
SESSION_START = PROJECT_ROOT / "plugin" / "hooks" / "session-start.py"

CHANGELOG = """# Changelog

## [9.9.9] - 2026-08-07

### Added
- Newest change.

## [9.9.8] - 2026-08-01

### Fixed
- Middle change.

## [9.9.7] - 2026-07-20

### Fixed
- Oldest change.
"""


class SessionStartUpdateNoticeTest(TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".claude" / "state").mkdir(parents=True)
        (self.tmp / "team-management").mkdir(parents=True)
        (self.tmp / "team-management" / "config.json").write_text(
            json.dumps({"developer_name": "Test"}), encoding="utf-8")
        # Fake plugin install: hooks/ must exist so nothing else trips, and
        # .claude-plugin/plugin.json + CHANGELOG.md drive the notice.
        self.plugin_root = self.tmp / "fake-plugin"
        (self.plugin_root / ".claude-plugin").mkdir(parents=True)
        self._write_manifest("9.9.9")
        (self.plugin_root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        self.fallback_marker = (
            self.tmp / ".claude" / "state" / "tm-last-seen-version.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_manifest(self, version):
        (self.plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "team-management", "version": version}),
            encoding="utf-8")

    def _write_marker(self, path, version):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": version}), encoding="utf-8")

    def _arm_transition_window(self):
        """Pin the fake plugin to the mechanism release with a matching
        changelog. Combined with an ABSENT marker this is the one-release
        transition window (m-fix-update-notice-first-run-window)."""
        self._write_manifest("0.4.5")
        (self.plugin_root / "CHANGELOG.md").write_text(
            "# Changelog\n\n"
            "## [0.4.5] - 2026-08-07\n\n### Added\n- Update notifications.\n\n"
            "## [0.4.4] - 2026-07-29\n\n### Added\n- An earlier change.\n",
            encoding="utf-8")

    def _run(self, plugin_data=None):
        env = os.environ.copy()
        env.pop("CLAUDE_PLUGIN_DATA", None)
        env["CLAUDE_PROJECT_DIR"] = str(self.tmp)
        env["CLAUDE_PLUGIN_ROOT"] = str(self.plugin_root)
        if plugin_data is not None:
            env["CLAUDE_PLUGIN_DATA"] = str(plugin_data)
        result = subprocess.run(
            [sys.executable, str(SESSION_START)],
            input="{}", env=env, cwd=str(self.tmp),
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        # Single parseable JSON document with the additionalContext contract —
        # the notice must be appended to `context`, never printed separately.
        payload = json.loads(result.stdout)
        self._last_payload = payload
        return payload["hookSpecificOutput"]["additionalContext"]

    def _banner(self):
        """Top-level `systemMessage` from the most recent run, or None.

        Silent paths assert key ABSENCE against `_last_payload` directly (SC-3
        requires the key to be missing entirely, which a null value would also
        satisfy) - this accessor serves the positive case.

        This is the user-visible half of the hybrid delivery — `additionalContext`
        alone never reaches the user (the model cannot speak first and may spend
        its first reply on the user's own request), so the banner is what makes
        the one-shot marker worth spending.
        """
        return self._last_payload.get("systemMessage")

    def test_first_run_writes_marker_silently(self):
        # Manifest is 9.9.9, not the mechanism release, so the one-release
        # transition window stays shut — this doubles as the guard that a
        # missing marker at any OTHER version is still silent.
        context = self._run()
        self.assertNotIn("[Plugin updated]", context)
        self.assertNotIn("systemMessage", self._last_payload)
        data = json.loads(self.fallback_marker.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "9.9.9")

    def test_update_emits_notice_and_syncs_marker(self):
        self._write_marker(self.fallback_marker, "9.9.7")
        context = self._run()
        self.assertIn("[Plugin updated]", context)
        self.assertIn("## [9.9.9]", context)
        self.assertIn("## [9.9.8]", context)
        self.assertNotIn("## [9.9.7]", context)
        data = json.loads(self.fallback_marker.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "9.9.9")

    def test_update_emits_user_facing_banner(self):
        """Hybrid delivery: the banner is a TOP-LEVEL systemMessage (shown to the
        user by Claude Code) and never replaces the full sections in context."""
        self._write_marker(self.fallback_marker, "9.9.7")
        context = self._run()
        banner = self._banner()
        self.assertIsNotNone(banner)
        self.assertIn("9.9.7", banner)
        self.assertIn("9.9.9", banner)
        self.assertIn("2 releases", banner)
        self.assertIn("/team-management:changelog", banner)
        self.assertTrue(banner.isascii(), banner)
        self.assertLessEqual(len(banner.splitlines()), 2, banner)
        # Supplements, never replaces: the model still gets every section.
        self.assertIn("## [9.9.9]", context)
        self.assertIn("## [9.9.8]", context)

    def test_equal_version_silent_and_marker_persists(self):
        self._write_marker(self.fallback_marker, "9.9.9")
        context = self._run()
        self.assertNotIn("[Plugin updated]", context)
        self.assertNotIn("systemMessage", self._last_payload)
        # The marker is persistent — session start must never clear it.
        data = json.loads(self.fallback_marker.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "9.9.9")

    def test_second_session_after_update_is_silent(self):
        self._write_marker(self.fallback_marker, "9.9.7")
        first = self._run()
        self.assertIn("[Plugin updated]", first)
        self.assertIsNotNone(self._banner())
        second = self._run()
        self.assertNotIn("[Plugin updated]", second)
        # The whole point of the one-shot marker: the banner must not repeat.
        self.assertNotIn("systemMessage", self._last_payload)

    def test_malformed_marker_first_run_semantics(self):
        self.fallback_marker.write_text("not json", encoding="utf-8")
        context = self._run()
        self.assertNotIn("[Plugin updated]", context)
        self.assertNotIn("systemMessage", self._last_payload)
        data = json.loads(self.fallback_marker.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "9.9.9")

    def test_plugin_data_takes_priority_over_state_fallback(self):
        plugin_data = self.tmp / "plugin-data"  # deliberately not created yet
        data_marker = plugin_data / "tm-last-seen-version.json"
        context = self._run(plugin_data=plugin_data)
        self.assertNotIn("[Plugin updated]", context)
        # Marker created under CLAUDE_PLUGIN_DATA (parents included), and the
        # per-project fallback stays untouched.
        data = json.loads(data_marker.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "9.9.9")
        self.assertFalse(self.fallback_marker.exists())

    def test_plugin_data_update_cycle(self):
        plugin_data = self.tmp / "plugin-data"
        self._write_marker(plugin_data / "tm-last-seen-version.json", "9.9.8")
        context = self._run(plugin_data=plugin_data)
        self.assertIn("[Plugin updated]", context)
        self.assertIn("## [9.9.9]", context)
        self.assertNotIn("## [9.9.8]", context)

    def test_missing_changelog_fail_open(self):
        (self.plugin_root / "CHANGELOG.md").unlink()
        self._write_marker(self.fallback_marker, "9.9.7")
        context = self._run()
        self.assertNotIn("[Plugin updated]", context)
        self.assertNotIn("systemMessage", self._last_payload)
        data = json.loads(self.fallback_marker.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "9.9.9")

    def test_broken_manifest_fail_open(self):
        (self.plugin_root / ".claude-plugin" / "plugin.json").write_text(
            "broken{", encoding="utf-8")
        context = self._run()
        self.assertNotIn("[Plugin updated]", context)
        self.assertNotIn("systemMessage", self._last_payload)
        self.assertFalse(self.fallback_marker.exists())

    def test_upgrade_without_matching_sections_is_silent(self):
        """Version moved forward but the changelog has nothing newer than the
        marker: sync and stay quiet (no banner, no notice) rather than retry
        every session."""
        self._write_manifest("9.9.10")
        self._write_marker(self.fallback_marker, "9.9.9")
        context = self._run()
        self.assertNotIn("[Plugin updated]", context)
        self.assertNotIn("systemMessage", self._last_payload)
        data = json.loads(self.fallback_marker.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "9.9.10")

    def test_transition_window_first_run_emits_banner_then_is_silent(self):
        """No marker at all + a 0.4.5 manifest — the one release the mechanism
        could not otherwise announce (every pre-0.4.5 install lacks a marker).
        The window fires once end-to-end through the hook, and the marker it
        writes makes every later session silent again."""
        self._arm_transition_window()
        self.assertFalse(self.fallback_marker.exists())

        context = self._run()
        self.assertIn("[Plugin updated]", context)
        self.assertIn("## [0.4.5]", context)
        # The assumed baseline is 0.4.4, so its own section is not repeated.
        self.assertNotIn("## [0.4.4]", context)
        banner = self._banner()
        self.assertIsNotNone(banner)
        self.assertIn("0.4.4", banner)
        self.assertIn("0.4.5", banner)
        self.assertIn("1 release", banner)
        data = json.loads(self.fallback_marker.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "0.4.5")

        second = self._run()
        self.assertNotIn("[Plugin updated]", second)
        self.assertNotIn("systemMessage", self._last_payload)

    def test_transition_window_fires_at_plugin_data_location(self):
        """A REAL plugin install sets CLAUDE_PLUGIN_DATA — the project-local
        fallback is the dev/legacy path — so the window must fire against the
        primary store, whose directory does not exist yet either."""
        self._arm_transition_window()
        plugin_data = self.tmp / "plugin-data"  # deliberately not created
        data_marker = plugin_data / "tm-last-seen-version.json"
        context = self._run(plugin_data=plugin_data)
        self.assertIn("[Plugin updated]", context)
        self.assertIn("## [0.4.5]", context)
        banner = self._banner()
        self.assertIsNotNone(banner)
        self.assertIn("0.4.4", banner)
        self.assertIn("0.4.5", banner)
        data = json.loads(data_marker.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "0.4.5")
        self.assertFalse(self.fallback_marker.exists())

    def test_downgrade_silent_and_no_banner(self):
        (self.plugin_root / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "team-management", "version": "9.9.8"}),
            encoding="utf-8")
        self._write_marker(self.fallback_marker, "9.9.9")
        context = self._run()
        self.assertNotIn("[Plugin updated]", context)
        self.assertNotIn("systemMessage", self._last_payload)
        data = json.loads(self.fallback_marker.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], "9.9.8")


if __name__ == "__main__":
    main()
