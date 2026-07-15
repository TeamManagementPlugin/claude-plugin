#!/usr/bin/env python3
"""Tests for the session-start AI-providers deprecation warning.

The hook script reads team-management/config.json at session start; if any of
the legacy AI-provider keys (`include_in_architecture`, `include_in_exploration`,
`gemini.default_model`) are present, it emits a one-time context-line warning
and writes a flag (`.claude/state/ai-providers-migration-warned.flag`).
The flag is cleared on each new session start so the warning re-emits across
sessions until the user edits config.json.

Run with: python3 -m pytest test/test_session_start_deprecation_warning.py -v
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


class SessionStartDeprecationTest(TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".claude" / "state").mkdir(parents=True)
        (self.tmp / "team-management").mkdir(parents=True)
        # Minimal valid config (no legacy keys)
        self.config_path = self.tmp / "team-management" / "config.json"
        self.config_path.write_text(json.dumps({
            "developer_name": "Test",
            "ai_providers": {"enabled_providers": []},
        }), encoding="utf-8")
        self.flag_path = self.tmp / ".claude" / "state" / "ai-providers-migration-warned.flag"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_session_start(self, stdin_payload="{}"):
        """Invoke session-start.py with cwd pointed at the temp tree.

        `shared_state.get_project_root()` walks up from `Path.cwd()` looking for
        a `.claude` directory — it doesn't honour the CLAUDE_PROJECT_DIR env var
        as input. Setting cwd to the temp dir (where we created `.claude/state/`)
        is the canonical way to redirect the hook to our test tree.
        """
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = str(self.tmp)
        result = subprocess.run(
            [sys.executable, str(SESSION_START)],
            input=stdin_payload, env=env, cwd=str(self.tmp),
            capture_output=True, text=True, timeout=30,
        )
        return result

    def _write_legacy_config(self, ai_keys=None, gemini_keys=None):
        cfg = {"developer_name": "Test", "ai_providers": {"enabled_providers": []}}
        if ai_keys:
            cfg["ai_providers"].update(ai_keys)
        if gemini_keys:
            cfg["gemini"] = gemini_keys
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")

    def test_no_warning_when_legacy_absent(self):
        """Clean config with no legacy keys → no warning, no flag written."""
        result = self._run_session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("deprecation notice", result.stdout)
        self.assertFalse(self.flag_path.exists())

    def test_warning_emitted_when_legacy_ai_keys_present(self):
        self._write_legacy_config(ai_keys={"include_in_architecture": True})
        result = self._run_session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deprecation notice", result.stdout)
        self.assertIn("include_in_architecture", result.stdout)
        # Remediation must point at the in-session config flow, not the retired installer
        self.assertNotIn("re-run the installer", result.stdout)
        self.assertIn("/team-management:config", result.stdout)
        # Flag persisted so other code paths can suppress a repeat within the session
        self.assertTrue(self.flag_path.exists())

    def test_warning_emitted_for_legacy_gemini_default_model(self):
        self._write_legacy_config(gemini_keys={"enabled": False, "default_model": "flash"})
        result = self._run_session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("default_model", result.stdout)
        self.assertTrue(self.flag_path.exists())

    def test_flag_cleared_on_each_session_start(self):
        """Even if a stale flag exists from a previous session, it is cleared at
        the start of the new one — so a legacy key triggers the warning anew."""
        self._write_legacy_config(ai_keys={"include_in_exploration": True})
        # Pre-create the stale flag — simulating a prior session that warned
        self.flag_path.write_text("warned\n", encoding="utf-8")
        # First run: clears the stale flag, then re-detects legacy keys, then re-writes flag
        result = self._run_session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("include_in_exploration", result.stdout)
        # Flag is back (the new session re-wrote it after detecting legacy keys)
        self.assertTrue(self.flag_path.exists())

    def test_legacy_values_not_auto_forwarded(self):
        """The deprecation path must NOT silently copy include_in_architecture's
        value into any of the new per-phase flags. The new keys must remain
        absent unless the user (or installer) writes them explicitly."""
        self._write_legacy_config(ai_keys={"include_in_architecture": True})
        # session-start.py is a hook, not a writer — it doesn't modify config.json.
        # The contract is: config.json is unchanged after session-start runs.
        before = self.config_path.read_text(encoding="utf-8")
        self._run_session_start()
        after = self.config_path.read_text(encoding="utf-8")
        self.assertEqual(before, after, "session-start.py modified config.json — must be read-only")


class SessionStartGeminiReplacedTest(SessionStartDeprecationTest):
    """Warning for the retired gemini provider (replaced by agy).

    Independent of the legacy-keys warning above: it has its OWN flag file
    (`ai-providers-gemini-replaced-warned.flag`) because pre-replacement
    installs already carry the migration flag, which would suppress a shared
    notice. Inherits setUp/_run_session_start/_write_legacy_config.
    """

    def setUp(self):
        super().setUp()
        self.replaced_flag = (self.tmp / ".claude" / "state"
                              / "ai-providers-gemini-replaced-warned.flag")

    def test_no_warning_when_gemini_absent(self):
        result = self._run_session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("gemini replaced by agy", result.stdout)
        self.assertFalse(self.replaced_flag.exists())

    def test_warning_when_gemini_enabled_true(self):
        self._write_legacy_config(gemini_keys={"enabled": True})
        result = self._run_session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gemini replaced by agy", result.stdout)
        self.assertIn("gemini.enabled: true", result.stdout)
        # Remediation must point at the in-session config flow, not the retired installer
        self.assertNotIn("re-run the installer", result.stdout)
        self.assertIn("/team-management:config", result.stdout)
        self.assertTrue(self.replaced_flag.exists())

    def test_warning_when_gemini_in_enabled_providers(self):
        self._write_legacy_config(ai_keys={"enabled_providers": ["codex", "gemini"]})
        result = self._run_session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gemini replaced by agy", result.stdout)
        self.assertIn("enabled_providers", result.stdout)
        self.assertTrue(self.replaced_flag.exists())

    def test_no_warning_when_gemini_disabled_block(self):
        """An installer-written `gemini: {enabled: false}` block (stale-state
        clear) is NOT a remnant worth warning about."""
        self._write_legacy_config(gemini_keys={"enabled": False})
        result = self._run_session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("gemini replaced by agy", result.stdout)
        self.assertFalse(self.replaced_flag.exists())

    def test_both_warnings_fire_independently(self):
        """Config holding gemini.default_model AND gemini.enabled: true fires
        BOTH the legacy-keys warning and the replaced warning, each writing
        its own flag file."""
        self._write_legacy_config(gemini_keys={"enabled": True, "default_model": "flash"})
        result = self._run_session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("deprecation notice", result.stdout)
        self.assertIn("gemini replaced by agy", result.stdout)
        self.assertTrue(self.flag_path.exists())
        self.assertTrue(self.replaced_flag.exists())

    def test_replaced_flag_cleared_on_each_session_start(self):
        self._write_legacy_config(gemini_keys={"enabled": True})
        self.replaced_flag.write_text("warned\n", encoding="utf-8")
        result = self._run_session_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("gemini replaced by agy", result.stdout)
        self.assertTrue(self.replaced_flag.exists())


if __name__ == "__main__":
    main()
