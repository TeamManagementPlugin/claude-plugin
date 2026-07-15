#!/usr/bin/env python3
"""Drift guard for the refactoring protocol's code-review gates
(m-hooks-hygiene-sweep, audit finding M2).

The code-review step must carry the same spec-compliance and completion-
evidence gates as task.json. verify_tests_pass is DELIBERATELY OMITTED:
the refactoring protocol tolerates pre-existing baseline failures by design
(the test-verify step diffs against the captured baseline — see
sub-protocols/refactoring-verify.md), while verify_tests_pass requires a
clean exit 0 and would permanently block advance on dirty-baseline
codebases. If a future change adds it back, this test fails on purpose so
the trade-off is re-litigated, not reintroduced by accident.

Run with: python3 -m pytest test/test_refactoring_gates_drift.py -v
"""

import json
from pathlib import Path
from unittest import TestCase, main

REPO_ROOT = Path(__file__).parent.parent
REFACTORING_JSON = REPO_ROOT / "plugin" / "protocol-configs" / "refactoring.json"


class RefactoringCodeReviewGatesTest(TestCase):
    @classmethod
    def setUpClass(cls):
        config = json.loads(REFACTORING_JSON.read_text(encoding="utf-8"))
        steps = {s["name"]: s for s in config["steps"]}
        cls.step = steps["code-review"]

    def test_pre_funcs_exact(self):
        self.assertEqual(
            self.step["pre_funcs"],
            ["resolve_ai_providers", "require_spec_review_passed"],
        )

    def test_post_funcs_exact(self):
        self.assertEqual(
            self.step["post_funcs"],
            [
                "require_spec_review_passed",
                "check_completion_evidence",
                "validate_code_review_in_worklog",
                "validate_no_critical_issues_in_worklog",
            ],
        )

    def test_verify_tests_pass_deliberately_omitted(self):
        # See module docstring: baseline-diff design vs exit-0 gate.
        self.assertNotIn("verify_tests_pass", self.step["post_funcs"])
        self.assertNotIn("verify_tests_pass", self.step["pre_funcs"])

    def test_stop_on_failure_enabled(self):
        self.assertTrue(self.step["post_funcs_stop_on_failure"])


if __name__ == "__main__":
    main()
