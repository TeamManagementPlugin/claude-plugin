#!/usr/bin/env python3
"""Tests for _func_require_spec_review_passed.

Run with: python3 -m pytest test/test_spec_review_gate.py -v
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


class SpecReviewGateTestBase(TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / ".claude" / "state" / "protocol-logs").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state" / "tasks").mkdir(parents=True)

        self._write_json(
            self.temp_dir / ".claude" / "state" / "current_task.json",
            {"task": None, "branch": None, "services": [], "updated": "2026-04-20"},
        )
        self._write_json(
            self.temp_dir / ".claude" / "state" / "daic-mode.json",
            {"mode": "discussion"},
        )

        import shared_state
        self._orig_project_root = shared_state.PROJECT_ROOT
        self._orig_state_dir = shared_state.STATE_DIR
        self._orig_task_state_file = shared_state.TASK_STATE_FILE
        self._orig_daic_state_file = shared_state.DAIC_STATE_FILE
        self._orig_protocol_logs_dir = shared_state.PROTOCOL_LOGS_DIR

        shared_state.PROJECT_ROOT = self.temp_dir
        shared_state.STATE_DIR = self.temp_dir / ".claude" / "state"
        shared_state.TASK_STATE_FILE = self.temp_dir / ".claude" / "state" / "current_task.json"
        shared_state.DAIC_STATE_FILE = self.temp_dir / ".claude" / "state" / "daic-mode.json"
        shared_state.PROTOCOL_LOGS_DIR = self.temp_dir / ".claude" / "state" / "protocol-logs"

        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(self.temp_dir)

    def tearDown(self):
        import shared_state
        shared_state.PROJECT_ROOT = self._orig_project_root
        shared_state.STATE_DIR = self._orig_state_dir
        shared_state.TASK_STATE_FILE = self._orig_task_state_file
        shared_state.DAIC_STATE_FILE = self._orig_daic_state_file
        shared_state.PROTOCOL_LOGS_DIR = self._orig_protocol_logs_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _set_task(self, task_name):
        self._write_json(
            self.temp_dir / ".claude" / "state" / "current_task.json",
            {
                "task": task_name,
                "branch": f"feature/{task_name}",
                "services": [],
                "updated": "2026-04-20",
            },
        )

    def _write_log(self, task_name, notes, gotos=None):
        log_path = self.temp_dir / ".claude" / "state" / "protocol-logs" / f"{task_name}.json"
        self._write_json(log_path, {
            "protocol": "task",
            "started_at": "2026-04-20T00:00:00+00:00",
            "completed_at": None,
            "steps": [],
            "notes": notes,
            "gotos": gotos or [],
        })


class TestRequireSpecReviewPassed(SpecReviewGateTestBase):
    def test_no_active_task_fails(self):
        result = self.engine._func_require_spec_review_passed()
        self.assertFalse(result["success"])
        self.assertIn("No active task", result["error"])

    def test_missing_log_file_fails(self):
        self._set_task("h-spec-task")
        # No log file written — new fail-closed behaviour (Codex W2 round 4)
        result = self.engine._func_require_spec_review_passed()
        self.assertFalse(result["success"])
        self.assertIn("missing", result["error"].lower())

    def test_empty_notes_fails(self):
        self._set_task("h-spec-task")
        self._write_log("h-spec-task", notes=[])
        result = self.engine._func_require_spec_review_passed()
        self.assertFalse(result["success"])

    def test_notes_without_sentinel_fails(self):
        self._set_task("h-spec-task")
        self._write_log("h-spec-task", notes=[
            {"at": "2026-04-20T00:01:00+00:00", "step": "code-review", "note": "random note"},
            {"at": "2026-04-20T00:02:00+00:00", "step": "code-review", "note": "SPEC_REVIEW: DISPATCHED"},
        ])
        result = self.engine._func_require_spec_review_passed()
        self.assertFalse(result["success"])

    def test_sentinel_present_passes(self):
        self._set_task("h-spec-task")
        self._write_log("h-spec-task", notes=[
            {"at": "2026-04-20T00:01:00+00:00", "step": "code-review", "note": "SPEC_REVIEW: PASSED"},
        ])
        result = self.engine._func_require_spec_review_passed()
        self.assertTrue(result["success"], result)

    def test_sentinel_with_surrounding_whitespace_passes(self):
        self._set_task("h-spec-task")
        self._write_log("h-spec-task", notes=[
            {"at": "2026-04-20T00:01:00+00:00", "step": "code-review", "note": "  SPEC_REVIEW: PASSED  "},
        ])
        result = self.engine._func_require_spec_review_passed()
        self.assertTrue(result["success"], result)

    def test_partial_match_does_not_count(self):
        """Substring match should not trigger pass."""
        self._set_task("h-spec-task")
        self._write_log("h-spec-task", notes=[
            {"at": "2026-04-20T00:01:00+00:00", "step": "code-review",
             "note": "Prefix text SPEC_REVIEW: PASSED suffix text"},
        ])
        result = self.engine._func_require_spec_review_passed()
        self.assertFalse(result["success"])


class TestStaleSentinel(SpecReviewGateTestBase):
    """Codex W1 round 3: sentinel must be newer than the most recent backward goto."""

    def test_sentinel_stale_after_goto_to_implementation(self):
        self._set_task("h-spec-task")
        self._write_log("h-spec-task",
            notes=[
                {"at": "2026-04-20T00:01:00+00:00", "step": "code-review", "note": "SPEC_REVIEW: PASSED"},
            ],
            gotos=[
                {"from_step": "code-review", "to_step": "implementation",
                 "reason": "fix a bug", "at": "2026-04-20T00:02:00+00:00"},
            ],
        )
        result = self.engine._func_require_spec_review_passed()
        self.assertFalse(result["success"], result)
        self.assertIn("STALE", result["error"])

    def test_sentinel_fresh_after_goto_still_blocks_until_new_sentinel(self):
        """After goto back and fix, sentinel from before the goto is stale
        until a NEW SPEC_REVIEW: PASSED is saved."""
        self._set_task("h-spec-task")
        self._write_log("h-spec-task",
            notes=[
                {"at": "2026-04-20T00:01:00+00:00", "step": "code-review", "note": "SPEC_REVIEW: PASSED"},
                {"at": "2026-04-20T00:03:00+00:00", "step": "implementation", "note": "applied fix"},
            ],
            gotos=[
                {"from_step": "code-review", "to_step": "implementation",
                 "reason": "fix a bug", "at": "2026-04-20T00:02:00+00:00"},
            ],
        )
        result = self.engine._func_require_spec_review_passed()
        self.assertFalse(result["success"], result)

    def test_sentinel_fresh_after_new_sentinel_saved_post_goto(self):
        self._set_task("h-spec-task")
        self._write_log("h-spec-task",
            notes=[
                {"at": "2026-04-20T00:01:00+00:00", "step": "code-review", "note": "SPEC_REVIEW: PASSED"},
                {"at": "2026-04-20T00:04:00+00:00", "step": "code-review", "note": "SPEC_REVIEW: PASSED"},
            ],
            gotos=[
                {"from_step": "code-review", "to_step": "implementation",
                 "reason": "fix a bug", "at": "2026-04-20T00:02:00+00:00"},
            ],
        )
        result = self.engine._func_require_spec_review_passed()
        self.assertTrue(result["success"], result)

    def test_goto_to_non_code_changing_step_does_not_invalidate(self):
        """A goto to documentation/completion (non-code-changing) should not invalidate."""
        self._set_task("h-spec-task")
        self._write_log("h-spec-task",
            notes=[
                {"at": "2026-04-20T00:01:00+00:00", "step": "code-review", "note": "SPEC_REVIEW: PASSED"},
            ],
            gotos=[
                {"from_step": "completion", "to_step": "documentation",
                 "reason": "touch-up docs", "at": "2026-04-20T00:02:00+00:00"},
            ],
        )
        result = self.engine._func_require_spec_review_passed()
        self.assertTrue(result["success"], result)


class TestPendingLogDoesNotLeak(SpecReviewGateTestBase):
    """Codex W2 round 4: a stale SPEC_REVIEW: PASSED in _pending.json must NOT
    satisfy the gate for a current task. The func reads the task-specific log
    file directly and fails closed when that file is missing."""

    def test_pending_log_with_sentinel_does_not_unblock(self):
        self._set_task("h-spec-task")
        # Write a _pending.json with the sentinel but NO h-spec-task.json.
        pending_path = self.temp_dir / ".claude" / "state" / "protocol-logs" / "_pending.json"
        self._write_json(pending_path, {
            "protocol": "task",
            "started_at": "2026-04-20T00:00:00+00:00",
            "completed_at": None,
            "steps": [],
            "notes": [
                {"at": "2026-04-20T00:01:00+00:00", "step": "code-review", "note": "SPEC_REVIEW: PASSED"},
            ],
            "gotos": [],
        })
        result = self.engine._func_require_spec_review_passed()
        self.assertFalse(result["success"], result)
        self.assertIn("missing", result["error"].lower())


if __name__ == "__main__":
    main()
