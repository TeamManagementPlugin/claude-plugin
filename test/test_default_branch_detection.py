#!/usr/bin/env python3
"""m-enforcement-and-git-hardening — default-branch detection in the git funcs.

`_func_git_setup_branch` and `_func_git_merge_main` used to inline a
main/master-only probe (`for candidate in ["main","master"]`), which branched
from the wrong base and merged the wrong target in repos whose default is
develop/trunk. Both now route through the shared `_detect_default_branch` helper
(whose own behaviour — origin/HEAD → local-candidate probe → "main" — is covered
in test_completion_workflow.py). These tests pin that the two funcs actually USE
the detected branch, not a hard-coded main/master.

Run with: python3 -m pytest test/test_default_branch_detection.py -v
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import shared_state  # noqa: E402
from protocol_engine import ProtocolEngine  # noqa: E402


def _ok(cmd, stdout=""):
    return subprocess.CompletedProcess(cmd, 0, stdout, "")


class DefaultBranchDetectionTest(TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / ".claude" / "state" / "protocol-logs").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state" / "tasks").mkdir(parents=True)
        (self.temp_dir / "team-management" / "tasks").mkdir(parents=True)
        self._write_json(
            self.temp_dir / ".claude" / "state" / "current_task.json",
            {"task": "m-x", "branch": "fix/x", "services": [], "updated": "2026-07-04"},
        )
        self._write_json(
            self.temp_dir / ".claude" / "state" / "daic-mode.json", {"mode": "discussion"})

        self._orig = (
            shared_state.PROJECT_ROOT, shared_state.STATE_DIR,
            shared_state.TASK_STATE_FILE, shared_state.DAIC_STATE_FILE,
            shared_state.PROTOCOL_LOGS_DIR,
        )
        shared_state.PROJECT_ROOT = self.temp_dir
        shared_state.STATE_DIR = self.temp_dir / ".claude" / "state"
        shared_state.TASK_STATE_FILE = self.temp_dir / ".claude" / "state" / "current_task.json"
        shared_state.DAIC_STATE_FILE = self.temp_dir / ".claude" / "state" / "daic-mode.json"
        shared_state.PROTOCOL_LOGS_DIR = self.temp_dir / ".claude" / "state" / "protocol-logs"
        self.engine = ProtocolEngine(self.temp_dir)

    def tearDown(self):
        (shared_state.PROJECT_ROOT, shared_state.STATE_DIR,
         shared_state.TASK_STATE_FILE, shared_state.DAIC_STATE_FILE,
         shared_state.PROTOCOL_LOGS_DIR) = self._orig
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_setup_branch_bases_off_detected_default(self):
        """Creating the task branch must check out the DETECTED default branch
        (develop) first — not a hard-coded main. Without the fix the func would
        probe main/master and check out `main`, ignoring the detected value."""
        calls = []

        def router(cmd, *a, **kw):
            calls.append(list(cmd))
            return _ok(cmd, "")  # clean tree, no current branch, branch absent

        with patch.object(self.engine, "_detect_default_branch", return_value="develop") as det, \
                patch("protocol_engine.subprocess.run", side_effect=router):
            result = self.engine._func_git_setup_branch({"branch": "fix/x"})

        self.assertTrue(det.called, "must consult _detect_default_branch")
        self.assertTrue(result["success"], result)
        checkouts = [c for c in calls if c[:2] == ["git", "checkout"]]
        self.assertIn(["git", "checkout", "develop"], checkouts,
                      f"did not base off the detected default branch: {checkouts}")
        self.assertNotIn(["git", "checkout", "main"], checkouts,
                         "still checking out a hard-coded main")

    def test_merge_main_targets_detected_default(self):
        """Merge must fetch+merge origin/<detected default>. Without the fix the
        func would fetch origin/main and miss a develop-default repo."""
        calls = []

        def router(cmd, *a, **kw):
            calls.append(list(cmd))
            # `_func_git_merge_main` now probes for an 'origin' remote first
            # (m-fix-completion-strands-without-remote) and skips fetch/merge
            # when absent — report origin present so this test still exercises
            # the fetch+merge default-branch path.
            if list(cmd)[:2] == ["git", "remote"]:
                return _ok(cmd, "origin\n")
            return _ok(cmd, "")

        with patch.object(self.engine, "_detect_default_branch", return_value="develop") as det, \
                patch("protocol_engine.subprocess.run", side_effect=router):
            result = self.engine._func_git_merge_main({})

        self.assertTrue(det.called, "must consult _detect_default_branch")
        self.assertTrue(result["success"], result)
        self.assertIn(["git", "fetch", "origin", "develop"], calls,
                      f"did not fetch the detected default: {calls}")
        self.assertTrue(
            any(c[:2] == ["git", "merge"] and "origin/develop" in c for c in calls),
            f"merge did not target origin/develop: {calls}")


if __name__ == "__main__":
    main()
