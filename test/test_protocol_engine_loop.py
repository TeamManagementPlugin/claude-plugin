#!/usr/bin/env python3
"""Tests for the optimize-protocol engine extensions:
looping_step, restart=true, auto-resume, credential scan.

Run with: python3 -m pytest test/test_protocol_engine_loop.py -v
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


class LoopProtocolTestBase(TestCase):
    """Base fixture for synthetic-loop protocol tests."""

    TASK_NAME = "o-test-loop"
    BRANCH = "optimize/test-loop"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        # Directory layout
        (self.temp_dir / ".claude" / "state").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state" / "protocol-logs").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state" / "tasks").mkdir(parents=True)
        (self.temp_dir / "team-management" / "protocol-configs" / "system").mkdir(parents=True)
        (self.temp_dir / "team-management" / "protocol-configs" / "custom").mkdir(parents=True)
        (self.temp_dir / "plugin" / "protocol-configs").mkdir(parents=True)
        (self.temp_dir / "team-management" / "tasks").mkdir(parents=True)

        # Initial state files
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": self.TASK_NAME,
            "branch": self.BRANCH,
            "services": [],
            "updated": "2026-05-05",
        })
        self._write_json(self.temp_dir / ".claude" / "state" / "daic-mode.json", {
            "mode": "discussion",
        })

        # Synthetic looping protocol: one looping step + one final step
        looptest_config = {
            "name": "looptest",
            "description": "Synthetic loop protocol for engine extension tests",
            "steps": [
                {
                    "name": "loop",
                    "mode": "implementation",
                    "looping_step": True,
                    "pre_funcs": [],
                    "post_funcs": [],
                    "start": "Loop step entry",
                    "end": "Loop step end",
                },
                {
                    "name": "done",
                    "mode": "discussion",
                    "pre_funcs": [],
                    "post_funcs": [],
                    "start": "Done",
                    "end": "Done",
                },
            ],
        }
        self._write_json(
            self.temp_dir / "team-management" / "protocol-configs" / "system" / "looptest.json",
            looptest_config,
        )

        # Task file
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        task_file.write_text(
            "---\n"
            f"task: {self.TASK_NAME}\n"
            f"branch: {self.BRANCH}\n"
            "status: in-progress\n"
            "created: 2026-05-05\n"
            "---\n\n"
            "# Synthetic loop test task\n\n"
            "Body.\n",
            encoding="utf-8",
        )
        # Task subdirectory for results.tsv
        (self.temp_dir / "team-management" / "tasks" / self.TASK_NAME).mkdir(parents=True, exist_ok=True)

        # team-management config (for the protocol_engine.enabled check, if any)
        (self.temp_dir / "team-management").mkdir(exist_ok=True)
        self._write_json(self.temp_dir / "team-management" / "config.json", {
            "protocol_engine": {"enabled": True},
        })

        # Patch shared_state globals
        import shared_state
        self._orig_project_root = shared_state.PROJECT_ROOT
        self._orig_state_dir = shared_state.STATE_DIR
        self._orig_task_state_file = shared_state.TASK_STATE_FILE
        self._orig_daic_state_file = shared_state.DAIC_STATE_FILE
        self._orig_protocol_logs_dir = shared_state.PROTOCOL_LOGS_DIR
        self._orig_optimize_state_file = shared_state.OPTIMIZE_STATE_FILE
        self._orig_task_state_lock_file = shared_state.TASK_STATE_LOCK_FILE

        shared_state.PROJECT_ROOT = self.temp_dir
        shared_state.STATE_DIR = self.temp_dir / ".claude" / "state"
        shared_state.TASK_STATE_FILE = self.temp_dir / ".claude" / "state" / "current_task.json"
        shared_state.DAIC_STATE_FILE = self.temp_dir / ".claude" / "state" / "daic-mode.json"
        shared_state.PROTOCOL_LOGS_DIR = self.temp_dir / ".claude" / "state" / "protocol-logs"
        shared_state.OPTIMIZE_STATE_FILE = self.temp_dir / ".claude" / "state" / "optimize-state.json"
        shared_state.TASK_STATE_LOCK_FILE = self.temp_dir / ".claude" / "state" / "current_task.lock"

        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(self.temp_dir)

    def tearDown(self):
        import shared_state
        shared_state.PROJECT_ROOT = self._orig_project_root
        shared_state.STATE_DIR = self._orig_state_dir
        shared_state.TASK_STATE_FILE = self._orig_task_state_file
        shared_state.DAIC_STATE_FILE = self._orig_daic_state_file
        shared_state.PROTOCOL_LOGS_DIR = self._orig_protocol_logs_dir
        shared_state.OPTIMIZE_STATE_FILE = self._orig_optimize_state_file
        shared_state.TASK_STATE_LOCK_FILE = self._orig_task_state_lock_file
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_json(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _read_json(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _get_protocol_state(self) -> dict:
        return (self._read_json(self.temp_dir / ".claude" / "state" / "current_task.json")
                .get("protocol") or {})

    def _task_dir(self) -> Path:
        return self.temp_dir / "team-management" / "tasks" / self.TASK_NAME


class TestLoopingStep(LoopProtocolTestBase):
    """looping_step: re-execute pre_funcs of same step instead of advancing."""

    def test_loop_runs_until_exit_loop(self):
        # Start protocol — first call enters at index 0, loop_iteration absent
        result = self.engine.start_protocol("looptest")
        self.assertTrue(result["success"])
        self.assertEqual(result["step"]["name"], "loop")

        # Advance without exit_loop — should loop, not advance
        result = self.engine.advance_step("iter 1")
        self.assertTrue(result["success"])
        self.assertTrue(result.get("looped"))
        self.assertEqual(result["loop_iteration"], 1)
        self.assertEqual(result["step"]["name"], "loop")

        # Loop again
        result = self.engine.advance_step("iter 2")
        self.assertEqual(result["loop_iteration"], 2)

        # Exit loop — should advance to "done"
        result = self.engine.advance_step("exit", args={"exit_loop": True})
        self.assertTrue(result["success"])
        self.assertFalse(result.get("looped", False))
        self.assertEqual(result["step"]["name"], "done")

    def test_experimentation_started_at_persists_across_iterations(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")
        first_started = self._get_protocol_state().get("experimentation_started_at")
        self.assertIsNotNone(first_started)

        self.engine.advance_step("iter 2")
        second_started = self._get_protocol_state().get("experimentation_started_at")
        self.assertEqual(first_started, second_started,
                         "experimentation_started_at must not change between iterations")

    def test_terminate_post_func_breaks_loop(self):
        """If a post_func returns terminate=True, the loop ends and advance proceeds."""
        # Inject a custom func via custom-funcs plugin
        custom_funcs_dir = self.temp_dir / "team-management" / "protocol-configs" / "custom" / "funcs"
        custom_funcs_dir.mkdir(parents=True, exist_ok=True)
        (custom_funcs_dir / "term.py").write_text(
            "def terminate_now(args=None):\n"
            "    return {'success': True, 'terminate': True}\n",
            encoding="utf-8",
        )

        # Patch the protocol to use the terminate_now post_func via the
        # `custom(name)` syntax that the engine resolver expects.
        config_path = self.temp_dir / "team-management" / "protocol-configs" / "system" / "looptest.json"
        config = self._read_json(config_path)
        config["steps"][0]["post_funcs"] = ["custom(terminate_now)"]
        self._write_json(config_path, config)

        self.engine.start_protocol("looptest")
        result = self.engine.advance_step("iter 1 with terminate")
        self.assertTrue(result["success"])
        self.assertFalse(result.get("looped", False))
        self.assertEqual(result["step"]["name"], "done")


class TestRestart(LoopProtocolTestBase):
    """restart=true on a looping_step: archive TSV, reset counters, clear best_commit."""

    def test_restart_archives_results_tsv(self):
        self.engine.start_protocol("looptest")
        # Build up some loop state
        self.engine.advance_step("iter 1")
        self.engine.advance_step("iter 2")
        self.assertEqual(self._get_protocol_state().get("loop_iteration"), 2)

        # Write a synthetic results.tsv
        tsv_path = self._task_dir() / "results.tsv"
        tsv_path.write_text(
            "iteration\ttimestamp\tcommit\tmetric\n"
            "0\t2026-05-05T00:00\t-\t10.0\n"
            "1\t2026-05-05T00:01\tabc1234\t8.5\n",
            encoding="utf-8",
        )

        # Restart
        result = self.engine.advance_step("restart", args={"restart": True})
        self.assertTrue(result["success"])
        self.assertTrue(result.get("restarted"))
        self.assertEqual(result["loop_iteration"], 0)
        self.assertTrue(result["archive"]["archived"])
        self.assertEqual(result["archive"]["run_n"], 1)

        # Original TSV gone, archive present
        self.assertFalse(tsv_path.exists())
        self.assertTrue((self._task_dir() / "results.tsv.run-1").exists())

        # State reset
        state = self._get_protocol_state()
        self.assertEqual(state.get("loop_iteration"), 0)

    def test_restart_clears_optimize_best_commit_from_frontmatter(self):
        # Add optimize.best_commit to task frontmatter
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        content = task_file.read_text(encoding="utf-8")
        # Insert optimize.best_commit before closing --- of frontmatter
        new_content = content.replace(
            "created: 2026-05-05\n",
            "created: 2026-05-05\noptimize.best_commit: abc1234\n",
        )
        task_file.write_text(new_content, encoding="utf-8")

        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")

        result = self.engine.advance_step("restart", args={"restart": True})
        self.assertTrue(result["success"])
        self.assertTrue(result.get("cleared_best_commit"))

        # Verify line is gone
        updated = task_file.read_text(encoding="utf-8")
        self.assertNotIn("optimize.best_commit:", updated)

    def test_restart_resets_experimentation_started_at(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")
        first_started = self._get_protocol_state().get("experimentation_started_at")

        # Small artificial delay through state — restart should produce a new
        # timestamp via fresh datetime.now() in _handle_loop_restart.
        result = self.engine.advance_step("restart", args={"restart": True})
        new_started = result.get("experimentation_started_at")
        self.assertIsNotNone(new_started)
        # After restart, persisted experimentation_started_at matches the
        # fresh value (not the pre-restart one).
        self.assertEqual(self._get_protocol_state().get("experimentation_started_at"), new_started)

    def test_restart_rejected_on_non_looping_step(self):
        self.engine.start_protocol("looptest")
        # Advance to "done" (non-looping) step
        self.engine.advance_step("exit", args={"exit_loop": True})
        self.assertEqual(self._get_protocol_state()["step_name"], "done")

        result = self.engine.advance_step("try restart", args={"restart": True})
        self.assertFalse(result["success"])
        self.assertIn("looping_step", result["error"])


class TestAutoResume(LoopProtocolTestBase):
    """Auto-resume: protocol_start on already-active protocol with loop_iteration > 0."""

    def test_resume_returns_resumed_state(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")
        self.engine.advance_step("iter 2")

        # Re-call protocol_start — should resume, not error
        result = self.engine.start_protocol("looptest")
        self.assertTrue(result["success"])
        self.assertTrue(result.get("resumed"))
        self.assertEqual(result["loop_iteration"], 2)
        self.assertEqual(result["step"]["name"], "loop")
        self.assertIn("Resuming protocol", result["start"])
        self.assertIn("loop_iteration=2", result["start"])
        self.assertIn("restart=true", result["start"])

    def test_resume_without_loop_iteration_still_errors(self):
        """A non-loop active protocol (loop_iteration absent or 0) should error
        on re-entry, preserving the existing 'protocol already active' guard."""
        self.engine.start_protocol("looptest")
        # No advance — loop_iteration is still absent
        result = self.engine.start_protocol("looptest")
        self.assertFalse(result["success"])
        self.assertIn("already active", result["error"])

    def test_resume_different_protocol_name_errors(self):
        """If the active protocol is X but caller asks for Y, original error fires."""
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")
        # Caller asks for a different protocol — must NOT auto-resume
        result = self.engine.start_protocol("task")
        self.assertFalse(result["success"])
        self.assertIn("already active", result["error"])


class TestCredentialScan(LoopProtocolTestBase):
    """Credential-scan helper for resume path."""

    def test_jwt_in_results_tsv_blocks_resume(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")

        # Write a results.tsv containing a JWT
        tsv_path = self._task_dir() / "results.tsv"
        tsv_path.write_text(
            "iteration\ttimestamp\tcommit\thypothesis\n"
            "1\t2026-05-05T00:00\tabc1234\tToken: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjMifQ.signature_here_x\n",
            encoding="utf-8",
        )

        result = self.engine.start_protocol("looptest")
        self.assertFalse(result["success"])
        self.assertTrue(result.get("resume_blocked"))
        self.assertTrue(any(m["kind"] == "jwt" for m in result["matches"]))

        # resume-blocked.txt was written
        blocked_path = self._task_dir() / "resume-blocked.txt"
        self.assertTrue(blocked_path.exists())
        blocked_content = blocked_path.read_text(encoding="utf-8")
        self.assertIn("credential pattern detected", blocked_content)
        # Redacted form, not full token
        self.assertNotIn("signature_here_x", blocked_content)

    def test_aws_key_in_results_tsv_blocks_resume(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")

        tsv_path = self._task_dir() / "results.tsv"
        tsv_path.write_text(
            "iteration\thypothesis\n"
            "1\tAccess key AKIAIOSFODNN7EXAMPLE leaked\n",
            encoding="utf-8",
        )

        result = self.engine.start_protocol("looptest")
        self.assertFalse(result["success"])
        self.assertTrue(result.get("resume_blocked"))
        self.assertTrue(any(m["kind"] == "aws_access_key" for m in result["matches"]))

    def test_resume_force_safe_bypasses_scan(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")

        tsv_path = self._task_dir() / "results.tsv"
        tsv_path.write_text(
            "iteration\thypothesis\n"
            "1\teyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdef\n",
            encoding="utf-8",
        )

        # Bypass via flag — should resume despite JWT match
        result = self.engine.start_protocol("looptest", resume_force_safe=True)
        self.assertTrue(result["success"])
        self.assertTrue(result.get("resumed"))
        # Bypass marker visible in resume hint
        self.assertIn("resume_force_safe=true bypass active", result["start"])
        self.assertTrue(result["scan_result"].get("bypassed"))

    def test_github_token_blocks_resume(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")

        tsv_path = self._task_dir() / "results.tsv"
        # ghp_<36 chars> is the canonical GitHub personal-access-token shape
        tsv_path.write_text(
            "iteration\thypothesis\n1\tleaked ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD\n",
            encoding="utf-8",
        )
        result = self.engine.start_protocol("looptest")
        self.assertFalse(result["success"])
        self.assertTrue(result.get("resume_blocked"))
        self.assertTrue(any(m["kind"] == "github_token" for m in result["matches"]))

    def test_gitlab_token_blocks_resume(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")

        tsv_path = self._task_dir() / "results.tsv"
        tsv_path.write_text(
            "iteration\thypothesis\n1\tleaked glpat-aBcDeFgHiJkLmNoPqRsT_X\n",
            encoding="utf-8",
        )
        result = self.engine.start_protocol("looptest")
        self.assertFalse(result["success"])
        self.assertTrue(any(m["kind"] == "gitlab_token" for m in result["matches"]))

    def test_slack_token_blocks_resume(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")

        tsv_path = self._task_dir() / "results.tsv"
        tsv_path.write_text(
            "iteration\thypothesis\n1\tleaked xoxb-1234567890-abcdef\n",
            encoding="utf-8",
        )
        result = self.engine.start_protocol("looptest")
        self.assertFalse(result["success"])
        self.assertTrue(any(m["kind"] == "slack_token" for m in result["matches"]))

    def test_clean_artifacts_allow_resume(self):
        self.engine.start_protocol("looptest")
        self.engine.advance_step("iter 1")

        tsv_path = self._task_dir() / "results.tsv"
        tsv_path.write_text(
            "iteration\thypothesis\n1\tnormal text only 12345\n",
            encoding="utf-8",
        )

        result = self.engine.start_protocol("looptest")
        # The base64_blob regex is a high-FP heuristic; "normal text only 12345"
        # is short and won't match. Should resume cleanly.
        self.assertTrue(result["success"])
        self.assertTrue(result.get("resumed"))


class TestSetProtocolStateExtra(LoopProtocolTestBase):
    """set_protocol_state(extra=...) merges into protocol block."""

    def test_extra_merged_into_protocol_block(self):
        from shared_state import set_protocol_state, get_protocol_state

        set_protocol_state(
            "looptest", 0, "loop", "2026-05-05T00:00:00+00:00",
            extra={"loop_iteration": 7, "experimentation_started_at": "2026-05-05T00:00:00+00:00"},
        )
        state = get_protocol_state()
        self.assertEqual(state["loop_iteration"], 7)
        self.assertEqual(state["experimentation_started_at"], "2026-05-05T00:00:00+00:00")
        # Canonical fields preserved
        self.assertEqual(state["name"], "looptest")
        self.assertEqual(state["current_step"], 0)

    def test_no_extra_uses_canonical_only(self):
        from shared_state import set_protocol_state, get_protocol_state

        set_protocol_state("looptest", 1, "done", "2026-05-05T00:00:00+00:00")
        state = get_protocol_state()
        self.assertEqual(state["name"], "looptest")
        self.assertEqual(state["current_step"], 1)
        self.assertNotIn("loop_iteration", state)


if __name__ == "__main__":
    main()
