#!/usr/bin/env python3
"""Tests for _func_check_completion_evidence.

Run with: python3 -m pytest test/test_completion_evidence.py -v
"""

import sys
from pathlib import Path
from unittest import TestCase, main

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

from protocol_engine import ProtocolEngine


class CompletionEvidenceTestBase(TestCase):
    def setUp(self):
        self.engine = ProtocolEngine(PROJECT_ROOT)

    def _run(self, summary):
        return self.engine._func_check_completion_evidence(
            args={"advance_summary": summary}
        )


class TestPositiveAnchors(CompletionEvidenceTestBase):
    def test_fenced_block_with_signal_passes(self):
        summary = "Ran the tests:\n```\n5 passed in 0.3s\n```"
        result = self._run(summary)
        self.assertTrue(result["success"], result)

    def test_fenced_block_with_counted_check_marks_passes(self):
        """Check-marks require count-adjacency (Codex W1 round 7)."""
        summary = "Results:\n```\n2 ✓ 0 ✗\n```"
        result = self._run(summary)
        self.assertTrue(result["success"], result)

    def test_fenced_block_with_exit_code_passes(self):
        summary = "```\n$ ./build.sh\nexit code: 0\n```"
        result = self._run(summary)
        self.assertTrue(result["success"], result)

    def test_exit_code_passes(self):
        result = self._run("Build succeeded, exit 0.")
        self.assertTrue(result["success"], result)

    def test_exit_code_with_colon_passes(self):
        result = self._run("Build finished. exit code: 0")
        self.assertTrue(result["success"], result)

    def test_numeric_ratio_passes(self):
        result = self._run("All done: 5/5 tests passed.")
        self.assertTrue(result["success"], result)

    def test_test_keyword_passes(self):
        result = self._run("All tests passing after the fix.")
        self.assertTrue(result["success"], result)

    def test_no_errors_keyword_passes(self):
        result = self._run("Linter reports no errors.")
        self.assertTrue(result["success"], result)

    def test_canonical_review_summary_passes(self):
        """The Good example from code-review.md sub-protocol must pass."""
        summary = (
            "code-review agent + codex-cli + agy-cli ran in parallel. "
            "Final pass: 0 critical, 0 warnings. "
            "Findings appended to work log under `# Code Review: Spec Compliance Reviewer`."
        )
        result = self._run(summary)
        self.assertTrue(result["success"], result)

    def test_zero_critical_passes(self):
        result = self._run("Review done: 0 critical issues.")
        self.assertTrue(result["success"], result)

    def test_zero_warnings_passes(self):
        result = self._run("Ship it: 0 warnings after the fix.")
        self.assertTrue(result["success"], result)

    def test_no_critical_passes(self):
        result = self._run("Final review: no critical issues.")
        self.assertTrue(result["success"], result)

    def test_user_confirmed_prose_blocks(self):
        """Codex W1 round 5: 'user confirmed' removed from evidence patterns
        after completion-step gate was dropped. Prose like 'user confirmed the
        intended behavior' must NOT pass as evidence on code-review."""
        result = self._run("User confirmed the intended behavior.")
        self.assertFalse(result["success"], result)


class TestEscapeHatch(CompletionEvidenceTestBase):
    def test_escape_hatch_docs_only_passes(self):
        result = self._run("no-verification-applicable: documentation-only")
        self.assertTrue(result["success"], result)
        self.assertTrue(result.get("escape_hatch"))
        self.assertEqual(result.get("reason"), "documentation-only")

    def test_escape_hatch_case_insensitive(self):
        result = self._run("No-Verification-Applicable: Planning-Step")
        self.assertTrue(result["success"], result)
        self.assertTrue(result.get("escape_hatch"))

    def test_escape_hatch_reason_captured(self):
        summary = (
            "We discussed scope with the user; no code changed.\n"
            "no-verification-applicable: discussion-step\n"
        )
        result = self._run(summary)
        self.assertTrue(result["success"], result)
        self.assertEqual(result.get("reason"), "discussion-step")


class TestNegativeCases(CompletionEvidenceTestBase):
    def test_prose_only_blocks(self):
        result = self._run("Looks good, tests should pass.")
        self.assertFalse(result["success"], result)
        self.assertIn("BLOCKED", result["error"])
        self.assertIn("no-verification-applicable", result["error"])

    def test_empty_summary_blocks(self):
        result = self._run("")
        self.assertFalse(result["success"], result)

    def test_lgtm_blocks(self):
        result = self._run("LGTM, shipping.")
        self.assertFalse(result["success"], result)

    def test_user_tested_negative_outcome_blocks(self):
        """Codex W1: 'user tested and found a regression' must NOT pass."""
        result = self._run("User tested and found a regression.")
        self.assertFalse(result["success"], result)

    def test_user_verified_bug_present_blocks(self):
        """Codex W1: 'user verified the bug is still present' must NOT pass."""
        result = self._run("User verified the bug is still present.")
        self.assertFalse(result["success"], result)

    def test_user_approved_without_confirmed_blocks(self):
        """Only 'user confirmed' is the narrowest accepted token."""
        result = self._run("User approved the design direction.")
        self.assertFalse(result["success"], result)

    def test_docs_only_zero_issues_prose_blocks(self):
        """Codex W2: 'docs-only change, 0 issues' must NOT pass — use escape hatch."""
        result = self._run("Docs-only change, 0 issues.")
        self.assertFalse(result["success"], result)

    def test_no_issues_reported_prose_blocks(self):
        """Codex W2 (round 2): 'no issues reported' in a disclaimer must NOT pass."""
        result = self._run("User hasn't tested yet, so no issues reported.")
        self.assertFalse(result["success"], result)

    def test_bare_fenced_block_without_signal_blocks(self):
        """Codex W2 (round 3): fence containing only example code must NOT pass."""
        summary = "Here's how to call it:\n```bash\ncurl https://example.com/api\n```"
        result = self._run(summary)
        self.assertFalse(result["success"], result)

    def test_fenced_block_with_prose_only_blocks(self):
        """Codex W2 (round 3): fence with no verification signal must NOT pass."""
        summary = "```\nThis is just a commentary\nabout the change\n```"
        result = self._run(summary)
        self.assertFalse(result["success"], result)

    def test_fenced_json_with_ok_blocks(self):
        """Codex W1 (round 6): JSON example with bare 'ok' must NOT pass."""
        summary = '```json\n{"ok": true}\n```'
        result = self._run(summary)
        self.assertFalse(result["success"], result)

    def test_fenced_log_with_warning_keyword_blocks(self):
        """Codex W1 (round 6): log snippet with bare 'warning:' must NOT pass."""
        summary = "```\nwarning: deprecated API\n```"
        result = self._run(summary)
        self.assertFalse(result["success"], result)

    def test_fenced_bare_PASS_word_blocks(self):
        """Codex W1 (round 6): fence containing only 'PASS' without a count must NOT pass."""
        summary = "```\nPASS\n```"
        result = self._run(summary)
        self.assertFalse(result["success"], result)

    def test_fenced_critical_path_example_blocks(self):
        """Codex W1 (round 6): 'critical' in a code sample must NOT pass."""
        summary = "```python\n# handle critical path\nprocess()\n```"
        result = self._run(summary)
        self.assertFalse(result["success"], result)

    def test_fenced_bare_checkmark_checklist_blocks(self):
        """Codex W1 (round 7): uncounted check-mark bullets must NOT pass."""
        summary = "```\n✓ renamed endpoint\n✓ updated docs\n```"
        result = self._run(summary)
        self.assertFalse(result["success"], result)


if __name__ == "__main__":
    main()
