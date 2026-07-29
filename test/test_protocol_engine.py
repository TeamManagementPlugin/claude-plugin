#!/usr/bin/env python3
"""Tests for the Protocol Engine.

Run with: python3 -m pytest test/test_protocol_engine.py -v
"""

import json
import os
import sys
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch, MagicMock

# Setup paths for imports
TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

# We need to mock PROJECT_ROOT in shared_state before importing protocol_engine
# Create a temp project structure for tests


class ProtocolEngineTestBase(TestCase):
    """Base class with temp project directory setup."""

    def setUp(self):
        """Create temporary project directory with protocol configs."""
        self.temp_dir = Path(tempfile.mkdtemp())

        # Create directory structure
        (self.temp_dir / ".claude" / "state").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state" / "protocol-logs").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state" / "tasks").mkdir(parents=True)
        (self.temp_dir / "team-management" / "tasks").mkdir(parents=True)
        (self.temp_dir / "team-management" / "protocol-configs" / "system").mkdir(parents=True)
        (self.temp_dir / "team-management" / "protocol-configs" / "system" / "sub-protocols").mkdir(parents=True)
        (self.temp_dir / "team-management" / "protocol-configs" / "custom").mkdir(parents=True)
        (self.temp_dir / "plugin" / "protocol-configs").mkdir(parents=True)

        # Create initial state files
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": None, "branch": None, "services": [], "updated": "2026-02-16"
        })
        self._write_json(self.temp_dir / ".claude" / "state" / "daic-mode.json", {
            "mode": "discussion"
        })

        # Copy protocol configs from source
        src_configs = PROJECT_ROOT / "plugin" / "protocol-configs"
        for json_file in src_configs.glob("*.json"):
            shutil.copy(json_file, self.temp_dir / "plugin" / "protocol-configs" / json_file.name)
            shutil.copy(json_file, self.temp_dir / "team-management" / "protocol-configs" / "system" / json_file.name)

        # Copy sub-protocols
        src_sub = src_configs / "sub-protocols"
        if src_sub.exists():
            dest_sub = self.temp_dir / "team-management" / "protocol-configs" / "system" / "sub-protocols"
            for md_file in src_sub.glob("*.md"):
                shutil.copy(md_file, dest_sub / md_file.name)

        # Create team-management config
        self._write_json(self.temp_dir / "team-management" / "config.json", {
            "protocol_engine": {"enabled": True},
            "blocked_tools": ["Edit", "Write", "MultiEdit", "NotebookEdit"],
        })

        # Patch shared_state globals
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

        # Import engine after patching
        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(self.temp_dir)

    def tearDown(self):
        """Cleanup temp directory and restore globals."""
        import shared_state
        shared_state.PROJECT_ROOT = self._orig_project_root
        shared_state.STATE_DIR = self._orig_state_dir
        shared_state.TASK_STATE_FILE = self._orig_task_state_file
        shared_state.DAIC_STATE_FILE = self._orig_daic_state_file
        shared_state.PROTOCOL_LOGS_DIR = self._orig_protocol_logs_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_json(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _read_json(self, path: Path) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _get_task_state(self) -> dict:
        return self._read_json(self.temp_dir / ".claude" / "state" / "current_task.json")

    def _get_daic_mode(self) -> str:
        data = self._read_json(self.temp_dir / ".claude" / "state" / "daic-mode.json")
        return data.get("mode", "discussion")


class TestListProtocols(ProtocolEngineTestBase):
    """Test protocol listing."""

    def test_list_protocols(self):
        result = self.engine.list_protocols()
        self.assertTrue(result["success"])
        names = [p["name"] for p in result["protocols"]]
        self.assertIn("task", names)
        self.assertIn("research", names)

    def test_list_protocols_custom_overrides_system(self):
        """Custom protocols should override system protocols of the same name."""
        # Create custom task.json with different description
        custom_config = {
            "name": "task",
            "description": "Custom task protocol",
            "steps": [{"name": "step1", "mode": "discussion"}],
        }
        self._write_json(
            self.temp_dir / "team-management" / "protocol-configs" / "custom" / "task.json",
            custom_config,
        )

        result = self.engine.list_protocols()
        task_protos = [p for p in result["protocols"] if p["name"] == "task"]
        self.assertEqual(len(task_protos), 1)
        self.assertEqual(task_protos[0]["source"], "custom")
        self.assertEqual(task_protos[0]["description"], "Custom task protocol")


class TestStartProtocol(ProtocolEngineTestBase):
    """Test protocol start."""

    def test_start_protocol(self):
        result = self.engine.start_protocol("task")
        self.assertTrue(result["success"])
        self.assertEqual(result["protocol"], "task")
        self.assertEqual(result["step"]["index"], 0)
        self.assertEqual(result["step"]["name"], "investigation")
        self.assertEqual(result["step"]["mode"], "discussion")

        # Check that protocol state was set
        task_state = self._get_task_state()
        self.assertIn("protocol", task_state)
        self.assertEqual(task_state["protocol"]["name"], "task")
        self.assertEqual(task_state["protocol"]["current_step"], 0)

    def test_start_protocol_creates_pending_log(self):
        self.engine.start_protocol("task")
        pending_log = self.temp_dir / ".claude" / "state" / "protocol-logs" / "_pending.json"
        self.assertTrue(pending_log.exists())
        log_data = self._read_json(pending_log)
        self.assertEqual(log_data["protocol"], "task")
        self.assertIsNone(log_data["completed_at"])

    def test_start_protocol_sets_daic_mode(self):
        self.engine.start_protocol("task")
        # First step of task protocol is "investigation" with mode "discussion"
        mode = self._get_daic_mode()
        self.assertEqual(mode, "discussion")

    def test_start_protocol_already_active(self):
        self.engine.start_protocol("task")
        result = self.engine.start_protocol("task")
        self.assertFalse(result["success"])
        self.assertIn("already active", result["error"])

    def test_start_protocol_not_found(self):
        result = self.engine.start_protocol("nonexistent")
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"])

    def test_start_research(self):
        result = self.engine.start_protocol("research")
        self.assertTrue(result["success"])
        self.assertEqual(result["protocol"], "research")
        self.assertEqual(result["step"]["name"], "scoping")


class TestAdvanceStep(ProtocolEngineTestBase):
    """Test protocol step advancement."""

    def test_advance_step(self):
        """Advance from investigation to implementation.

        The task protocol has post_funcs on the investigation step that may fail
        in a test environment (no git, no task files). If post_funcs have
        stop_on_failure=False (default), advance should still succeed.
        If stop_on_failure=True, we expect the chain to stop.
        """
        self.engine.start_protocol("task")

        result = self.engine.advance_step(
            "Investigated the issue thoroughly.",
            args={"task": "test-task", "branch": "fix/test", "task_content": "# Test Task"},
        )

        # The task protocol's investigation step has post_funcs_stop_on_failure=true
        # and post_funcs that will fail in test env (create_task_file, set_task_state, etc.)
        # So either: success (funcs worked) or failure with chain_stopped
        if result["success"]:
            self.assertEqual(result["previous_step"]["name"], "investigation")
            self.assertEqual(result["step"]["name"], "implementation")
            self.assertFalse(result["protocol_complete"])
        else:
            # post_funcs failed and stopped the chain
            self.assertIn("post_funcs_results", result)
            self.assertEqual(result["step"]["name"], "investigation")

    def test_advance_requires_summary(self):
        self.engine.start_protocol("task")
        result = self.engine.advance_step("")
        self.assertFalse(result["success"])
        self.assertIn("Summary is required", result["error"])

    def test_advance_no_protocol(self):
        result = self.engine.advance_step("Some summary")
        self.assertFalse(result["success"])
        self.assertIn("No protocol active", result["error"])

    def test_advance_last_step_completes(self):
        """Advancing the last step should attempt protocol completion.

        Uses a custom minimal protocol to test the advance-to-completion path.
        The custom protocol has no post_funcs so it should complete cleanly.
        """
        # Create a custom 2-step protocol with no post_funcs
        custom_config = {
            "name": "test-minimal",
            "description": "Minimal test protocol",
            "steps": [
                {"name": "step1", "mode": "discussion"},
                {"name": "step2", "mode": "discussion"},
            ],
        }
        self._write_json(
            self.temp_dir / "team-management" / "protocol-configs" / "custom" / "test-minimal.json",
            custom_config,
        )

        self.engine.start_protocol("test-minimal")
        self.engine.advance_step("Completed step 1.")

        # Advance last step — should complete the protocol
        result = self.engine.advance_step("Completed step 2.")

        self.assertTrue(result["success"])
        self.assertTrue(result.get("protocol_complete", False))


class TestGotoStep(ProtocolEngineTestBase):
    """Test goto step (backward navigation)."""

    def test_goto_step(self):
        """Should be able to go back to a previous step."""
        # Use a custom minimal protocol for goto tests (no post_funcs to fail)
        custom_config = {
            "name": "test-goto",
            "description": "Test protocol for goto",
            "steps": [
                {"name": "step1", "mode": "discussion"},
                {"name": "step2", "mode": "discussion"},
                {"name": "step3", "mode": "discussion"},
            ],
        }
        self._write_json(
            self.temp_dir / "team-management" / "protocol-configs" / "custom" / "test-goto.json",
            custom_config,
        )
        self.engine.start_protocol("test-goto")

        # Advance to step 2
        self.engine.advance_step("Completed step 1.")

        # Go back to step1
        result = self.engine.goto_step("step1", "Need to revisit step 1.")
        self.assertTrue(result["success"])
        self.assertEqual(result["to_step"]["name"], "step1")
        self.assertEqual(result["from_step"]["name"], "step2")

    def test_goto_forward_blocked(self):
        """Cannot goto forward — must use advance."""
        custom_config = {
            "name": "test-goto-fwd",
            "description": "Test protocol for goto forward",
            "steps": [
                {"name": "step1", "mode": "discussion"},
                {"name": "step2", "mode": "discussion"},
            ],
        }
        self._write_json(
            self.temp_dir / "team-management" / "protocol-configs" / "custom" / "test-goto-fwd.json",
            custom_config,
        )
        self.engine.start_protocol("test-goto-fwd")

        result = self.engine.goto_step("step2", "Want to skip ahead.")
        self.assertFalse(result["success"])
        self.assertIn("Cannot goto", result["error"])

    def test_goto_requires_reason(self):
        custom_config = {
            "name": "test-goto-reason",
            "description": "Test protocol for goto reason",
            "steps": [
                {"name": "step1", "mode": "discussion"},
                {"name": "step2", "mode": "discussion"},
            ],
        }
        self._write_json(
            self.temp_dir / "team-management" / "protocol-configs" / "custom" / "test-goto-reason.json",
            custom_config,
        )
        self.engine.start_protocol("test-goto-reason")
        self.engine.advance_step("Done.")
        result = self.engine.goto_step("step1", "")
        self.assertFalse(result["success"])
        self.assertIn("Reason is required", result["error"])

    def test_goto_unknown_step(self):
        custom_config = {
            "name": "test-goto-unknown",
            "description": "Test protocol for goto unknown",
            "steps": [
                {"name": "step1", "mode": "discussion"},
                {"name": "step2", "mode": "discussion"},
            ],
        }
        self._write_json(
            self.temp_dir / "team-management" / "protocol-configs" / "custom" / "test-goto-unknown.json",
            custom_config,
        )
        self.engine.start_protocol("test-goto-unknown")
        self.engine.advance_step("Done.")
        result = self.engine.goto_step("nonexistent", "Some reason.")
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"])

    def test_goto_logs_event(self):
        """Goto events should be logged."""
        custom_config = {
            "name": "test-goto-log",
            "description": "Test protocol for goto logging",
            "steps": [
                {"name": "step1", "mode": "discussion"},
                {"name": "step2", "mode": "discussion"},
            ],
        }
        self._write_json(
            self.temp_dir / "team-management" / "protocol-configs" / "custom" / "test-goto-log.json",
            custom_config,
        )
        self.engine.start_protocol("test-goto-log")
        self.engine.advance_step("Completed step 1.")

        self.engine.goto_step("step1", "Need to revisit.")

        # Check pending log for goto entry
        pending_log = self.temp_dir / ".claude" / "state" / "protocol-logs" / "_pending.json"
        log_data = self._read_json(pending_log)
        self.assertTrue(len(log_data.get("gotos", [])) > 0)


class TestAbortProtocol(ProtocolEngineTestBase):
    """Test protocol abort."""

    def test_abort_protocol(self):
        self.engine.start_protocol("task")
        result = self.engine.abort_protocol("Task requirements changed.")
        self.assertTrue(result["success"])
        self.assertEqual(result["reason"], "Task requirements changed.")

        # Protocol state should be cleared
        task_state = self._get_task_state()
        self.assertNotIn("protocol", task_state)

        # DAIC mode should be discussion
        mode = self._get_daic_mode()
        self.assertEqual(mode, "discussion")

    def test_abort_requires_reason(self):
        self.engine.start_protocol("task")
        result = self.engine.abort_protocol("")
        self.assertFalse(result["success"])
        self.assertIn("Reason is required", result["error"])

    def test_abort_no_protocol(self):
        result = self.engine.abort_protocol("Some reason.")
        self.assertFalse(result["success"])
        self.assertIn("No protocol active", result["error"])

    def test_abort_clears_task_identity(self):
        from shared_state import set_task_state
        self.engine.start_protocol("task")
        set_task_state("m-foo", "feature/foo", ["svc-a"])
        result = self.engine.abort_protocol("User cancel.")
        self.assertTrue(result["success"])
        state = self._get_task_state()
        self.assertIsNone(state.get("task"))
        self.assertIsNone(state.get("branch"))
        self.assertEqual(state.get("services"), [])
        self.assertIn("current_task_identity", result.get("cleaned", []))

    def test_abort_removes_per_task_state_dir(self):
        from shared_state import set_task_state
        self.engine.start_protocol("task")
        set_task_state("m-foo", "feature/foo", [])
        task_dir = self.temp_dir / ".claude" / "state" / "tasks" / "m-foo"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "session.json").write_text("{}", encoding="utf-8")
        result = self.engine.abort_protocol("reason.")
        self.assertTrue(result["success"])
        self.assertFalse(task_dir.exists())
        self.assertIn("task_scoped_state", result.get("cleaned", []))

    def test_abort_preserves_optimize_state(self):
        # Per the contract documented in optimize-experimentation-auto.md:75,
        # optimize-state.json stays on disk after abort for forensic salvage.
        # _load_frozen_paths in sessions-enforce.py short-circuits stale state
        # from non-active protocols, so leaving it does not block unrelated work.
        self.engine.start_protocol("task")
        optimize_path = self.temp_dir / ".claude" / "state" / "optimize-state.json"
        optimize_path.write_text('{"frozen_paths": ["src/foo.py"]}', encoding="utf-8")
        result = self.engine.abort_protocol("reason.")
        self.assertTrue(result["success"])
        self.assertTrue(optimize_path.exists())
        self.assertNotIn("optimize_state", result.get("cleaned", []))

    def test_abort_preserves_task_file_and_branch(self):
        from shared_state import set_task_state
        self.engine.start_protocol("task")
        set_task_state("m-foo", "feature/foo", [])
        tasks_dir = self.temp_dir / "team-management" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_file = tasks_dir / "m-foo.md"
        task_file.write_text("# Task", encoding="utf-8")
        result = self.engine.abort_protocol("reason.")
        self.assertTrue(result["success"])
        # Task file MUST survive — work-in-progress is the user's responsibility.
        self.assertTrue(task_file.exists())

    def test_abort_no_task_no_crash(self):
        # current_task.json has only protocol block, no task identity.
        self.engine.start_protocol("task")
        result = self.engine.abort_protocol("reason.")
        self.assertTrue(result["success"])
        self.assertNotIn("task_scoped_state", result.get("cleaned", []))
        self.assertIn("current_task_identity", result.get("cleaned", []))


class TestSaveNote(ProtocolEngineTestBase):
    """Test protocol note saving."""

    def test_save_note(self):
        self.engine.start_protocol("task")
        result = self.engine.save_note("Important finding during investigation.")

        self.assertTrue(result["success"])

        # Check log has the note
        pending_log = self.temp_dir / ".claude" / "state" / "protocol-logs" / "_pending.json"
        log_data = self._read_json(pending_log)
        self.assertEqual(len(log_data.get("notes", [])), 1)
        self.assertEqual(log_data["notes"][0]["note"], "Important finding during investigation.")


class TestProtocolLog(ProtocolEngineTestBase):
    """Test protocol log retrieval."""

    def test_get_log_pending(self):
        """Should find _pending.json log when task name unknown."""
        self.engine.start_protocol("task")

        result = self.engine.get_log("some-task")
        # Should fall back to _pending.json
        self.assertTrue(result["success"])
        self.assertEqual(result["log"]["protocol"], "task")

    def test_get_log_no_task(self):
        result = self.engine.get_log()
        self.assertFalse(result["success"])
        self.assertIn("No task", result["error"])


class TestExecuteFuncs(ProtocolEngineTestBase):
    """Test func handler execution."""

    def test_execute_empty_funcs(self):
        result = self.engine.execute_funcs([])
        self.assertEqual(result, [])

    def test_execute_unknown_func(self):
        result = self.engine.execute_funcs(["unknown_func"])
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["success"])
        self.assertIn("unknown_func", result[0]["error"])

    def test_execute_stop_on_failure(self):
        """Chain should stop when a func fails and stop_on_failure is True."""
        result = self.engine.execute_funcs(
            ["unknown_func", "auto_detect_task"],
            stop_on_failure=True,
        )
        self.assertEqual(len(result), 2)
        # First result: failure
        self.assertFalse(result[0]["success"])
        # Second result: chain stopped marker
        self.assertTrue(result[1].get("chain_stopped"))


class TestDAICModePerStep(ProtocolEngineTestBase):
    """Test that DAIC mode is set correctly per step."""

    def test_investigation_step_discussion(self):
        """Investigation step should set discussion mode."""
        self.engine.start_protocol("task")
        mode = self._get_daic_mode()
        self.assertEqual(mode, "discussion")

    def test_implementation_step(self):
        """Implementation step should set implementation mode."""
        # Use a custom protocol with an implementation step
        custom_config = {
            "name": "test-daic-impl",
            "description": "Test DAIC mode transition",
            "steps": [
                {"name": "discuss", "mode": "discussion"},
                {"name": "implement", "mode": "implementation"},
            ],
        }
        self._write_json(
            self.temp_dir / "team-management" / "protocol-configs" / "custom" / "test-daic-impl.json",
            custom_config,
        )
        self.engine.start_protocol("test-daic-impl")
        # Advance past discuss (discussion) → implement (implementation)
        self.engine.advance_step("Done discussing.")
        mode = self._get_daic_mode()
        self.assertEqual(mode, "implementation")


class TestProtocolConfigSearchOrder(ProtocolEngineTestBase):
    """Test protocol config search order: custom > system > dev."""

    def test_custom_overrides_system(self):
        """Custom protocol should override system protocol."""
        custom_config = {
            "name": "task",
            "description": "Custom override",
            "steps": [{"name": "custom-step", "mode": "discussion"}],
        }
        self._write_json(
            self.temp_dir / "team-management" / "protocol-configs" / "custom" / "task.json",
            custom_config,
        )

        config = self.engine.get_protocol_config("task")
        self.assertEqual(config["description"], "Custom override")
        self.assertEqual(len(config["steps"]), 1)
        self.assertEqual(config["steps"][0]["name"], "custom-step")

    def test_system_used_when_no_custom(self):
        """System protocol used when no custom override exists."""
        config = self.engine.get_protocol_config("task")
        self.assertIsNotNone(config)
        self.assertEqual(len(config.get("steps", [])), 5)


class TestStartTextResolution(ProtocolEngineTestBase):
    """Test @-reference resolution in start text."""

    def test_resolve_at_reference(self):
        """@sub-protocols/task-creation.md should resolve to file contents."""
        from shared_state import resolve_protocol_start_text

        # Create a sub-protocol file
        sub_file = self.temp_dir / "team-management" / "protocol-configs" / "system" / "sub-protocols" / "test-sub.md"
        sub_file.write_text("# Test Sub-Protocol\nThis is test content.", encoding="utf-8")

        result = resolve_protocol_start_text("@sub-protocols/test-sub.md", "task")
        self.assertIn("Test Sub-Protocol", result)
        self.assertIn("test content", result)

    def test_resolve_plain_text(self):
        """Plain text (no @) should pass through unchanged."""
        from shared_state import resolve_protocol_start_text

        result = resolve_protocol_start_text("Just plain text", "task")
        self.assertEqual(result, "Just plain text")

    def test_resolve_missing_reference(self):
        """Missing @-reference should return original text."""
        from shared_state import resolve_protocol_start_text

        result = resolve_protocol_start_text("@nonexistent/file.md", "task")
        self.assertEqual(result, "@nonexistent/file.md")


class TestPendingLogRename(ProtocolEngineTestBase):
    """Test _pending.json log rename behavior."""

    def test_pending_log_exists_after_start(self):
        self.engine.start_protocol("task")
        pending = self.temp_dir / ".claude" / "state" / "protocol-logs" / "_pending.json"
        self.assertTrue(pending.exists())

class TestCreateTaskFileValidation(ProtocolEngineTestBase):
    """Test create_task_file validation."""

    def test_success_criteria_required(self):
        """Task content must include ## Success Criteria section."""
        result = self.engine._func_create_task_file(args={
            "task": "m-test-task",
            "branch": "feature/test",
            "task_content": "---\nstatus: pending\nbranch: feature/test\n---\n# Test Task\nNo criteria here.",
        })
        self.assertFalse(result["success"])
        self.assertIn("Success Criteria", result["error"])

    def test_success_criteria_present(self):
        """Task content with ## Success Criteria should pass."""
        result = self.engine._func_create_task_file(args={
            "task": "m-test-task",
            "branch": "feature/test",
            "task_content": "---\nstatus: pending\nbranch: feature/test\n---\n# Test Task\n\n## Success Criteria\n- [ ] Something works",
        })
        self.assertTrue(result["success"])

    def test_criteria_short_form(self):
        """Task content with ## Criteria should also pass."""
        result = self.engine._func_create_task_file(args={
            "task": "m-test-task",
            "branch": "feature/test",
            "task_content": "---\nstatus: pending\nbranch: feature/test\n---\n# Test Task\n\n## Criteria\n- [ ] Something works",
        })
        self.assertTrue(result["success"])

    def test_o_prefix_accepted(self):
        """Task names with the o- (optimize) priority prefix must be accepted."""
        result = self.engine._func_create_task_file(args={
            "task": "o-some-optimize-task",
            "branch": "optimize/some-optimize-task",
            "task_content": "---\nstatus: pending\nbranch: optimize/some-optimize-task\n---\n# Test\n\n## Success Criteria\n- [ ] x",
        })
        self.assertTrue(result["success"], result.get("error"))

    def test_b_prefix_accepted(self):
        """Task names with the b- (brainstorm) priority prefix must be accepted."""
        result = self.engine._func_create_task_file(args={
            "task": "b-brainstorm-foo",
            "branch": "brainstorm/foo",
            "task_content": "---\nstatus: pending\nbranch: brainstorm/foo\n---\n# Test\n\n## Success Criteria\n- [ ] x",
        })
        self.assertTrue(result["success"], result.get("error"))

    def test_unprefixed_rejected(self):
        """Task names without any priority prefix must be rejected with a clear error."""
        result = self.engine._func_create_task_file(args={
            "task": "untagged-task",
            "branch": "feature/untagged-task",
            "task_content": "---\nstatus: pending\nbranch: feature/untagged-task\n---\n# Test\n\n## Success Criteria\n- [ ] x",
        })
        self.assertFalse(result["success"])
        self.assertIn("priority prefix", result["error"])

    # The literal marker is assembled by concatenation so it never appears verbatim
    # in this repo's own task files / test source (the gate would self-match it).
    CLARIFICATION_MARKER = "[NEEDS " + "CLARIFICATION: which auth method?]"

    def test_needs_clarification_marker_rejected(self):
        """A body containing an unresolved clarification marker blocks delivery."""
        result = self.engine._func_create_task_file(args={
            "task": "m-test-task",
            "branch": "feature/test",
            "task_content": (
                "---\nstatus: pending\nbranch: feature/test\n---\n# Test Task\n\n"
                "## Success Criteria\n- [ ] SC-1: do X " + self.CLARIFICATION_MARKER + "\n"
            ),
        })
        self.assertFalse(result["success"])
        self.assertIn("NEEDS", result["error"])

    def test_needs_clarification_case_insensitive(self):
        """The marker check is case-insensitive."""
        lower_marker = "[needs " + "clarification: which auth method?]"
        result = self.engine._func_create_task_file(args={
            "task": "m-test-task",
            "branch": "feature/test",
            "task_content": (
                "---\nstatus: pending\nbranch: feature/test\n---\n# Test Task\n\n"
                "## Success Criteria\n- [ ] SC-1: do X " + lower_marker + "\n"
            ),
        })
        self.assertFalse(result["success"])
        self.assertIn("NEEDS", result["error"])

    def test_prose_mention_not_false_matched(self):
        """Prose mentioning 'needs clarification' without the bracket marker passes."""
        result = self.engine._func_create_task_file(args={
            "task": "m-test-task",
            "branch": "feature/test",
            "task_content": (
                "---\nstatus: pending\nbranch: feature/test\n---\n# Test Task\n\n"
                "## Success Criteria\n- [ ] SC-1: works\n\n## User Notes\n"
                "One point still needs clarification from the user, recorded here as accepted.\n"
            ),
        })
        self.assertTrue(result["success"], result.get("error"))

    def test_marker_outside_criteria_section_rejected(self):
        """The gate scans the whole body — a marker in ## User Notes also blocks."""
        result = self.engine._func_create_task_file(args={
            "task": "m-test-task",
            "branch": "feature/test",
            "task_content": (
                "---\nstatus: pending\nbranch: feature/test\n---\n# Test Task\n\n"
                "## Success Criteria\n- [ ] SC-1: works\n\n## User Notes\n"
                "Open point: " + self.CLARIFICATION_MARKER + "\n"
            ),
        })
        self.assertFalse(result["success"])
        self.assertIn("NEEDS", result["error"])

    def test_marker_whitespace_variants_rejected(self):
        """\\s+ in the pattern: newline/tab between the marker words still matches."""
        for sep in ("\n", "\t", "  "):
            with self.subTest(sep=repr(sep)):
                marker = "[NEEDS" + sep + "CLARIFICATION: split marker]"
                result = self.engine._func_create_task_file(args={
                    "task": "m-test-task",
                    "branch": "feature/test",
                    "task_content": (
                        "---\nstatus: pending\nbranch: feature/test\n---\n# Test Task\n\n"
                        "## Success Criteria\n- [ ] SC-1: x " + marker + "\n"
                    ),
                })
                self.assertFalse(result["success"])
                self.assertIn("NEEDS", result["error"])

    def test_numbered_criteria_pass(self):
        """SC-numbered criteria are plain convention — the validator accepts them."""
        result = self.engine._func_create_task_file(args={
            "task": "m-test-task",
            "branch": "feature/test",
            "task_content": (
                "---\nstatus: pending\nbranch: feature/test\n---\n# Test Task\n\n"
                "## Success Criteria\n- [ ] SC-1: Something works\n- [ ] SC-2: Another outcome\n"
            ),
        })
        self.assertTrue(result["success"], result.get("error"))


class TestTemplateScNumbering(ProtocolEngineTestBase):
    """Drift guard: the shipped task template documents the SC-numbering convention."""

    def test_template_contains_sc_numbering(self):
        template = Path(__file__).resolve().parent.parent / "plugin" / "templates" / "TEMPLATE.md"
        content = template.read_text(encoding="utf-8")
        self.assertIn("SC-1:", content)
        self.assertIn("SC-2:", content)
        self.assertIn("T1 [SC-1]", content)


class TestCreateTaskFileSkipValidation(ProtocolEngineTestBase):
    """Skip-overwrite path (empty task_content + existing file) re-validates the
    on-disk file with STRICT PARITY instead of trusting existence alone."""

    VALID = (
        "---\ntask: m-skip-valid\nbranch: feature/skip-valid\nstatus: pending\n---\n"
        "# Skip Valid\n\n**Author:** Max\n\n## Success Criteria\n- [ ] works\n"
    )

    def _tasks_dir(self):
        return self.temp_dir / "team-management" / "tasks"

    def _write_task(self, name, content):
        path = self._tasks_dir() / f"{name}.md"
        path.write_text(content, encoding="utf-8")
        return path

    def test_valid_flat_file_skipped(self):
        """An existing valid file + empty content returns action='skipped'."""
        self._write_task("m-skip-valid", self.VALID)
        result = self.engine._func_create_task_file(args={
            "task": "m-skip-valid", "branch": "feature/skip-valid", "task_content": "",
        })
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["action"], "skipped")
        self.assertIn("m-skip-valid.md", result["path"])

    def test_missing_success_criteria_blocks(self):
        """A malformed on-disk file (no ## Success Criteria) blocks the advance."""
        content = (
            "---\ntask: m-skip-nocrit\nbranch: feature/skip-nocrit\nstatus: pending\n---\n"
            "# No Criteria\n\n**Author:** Max\n\nNo criteria section.\n"
        )
        self._write_task("m-skip-nocrit", content)
        result = self.engine._func_create_task_file(args={
            "task": "m-skip-nocrit", "branch": "feature/skip-nocrit", "task_content": "",
        })
        self.assertFalse(result["success"])
        self.assertIn("Success Criteria", result["error"])
        # error names the on-disk file
        self.assertIn("m-skip-nocrit.md", result["error"])

    def test_no_frontmatter_blocks(self):
        """A file that does not start with frontmatter blocks."""
        self._write_task("m-skip-nofm", "# No Frontmatter\n\n## Success Criteria\n- [ ] x\n")
        result = self.engine._func_create_task_file(args={
            "task": "m-skip-nofm", "branch": "feature/skip-nofm", "task_content": "",
        })
        self.assertFalse(result["success"])
        self.assertIn("frontmatter", result["error"])

    def test_branch_mismatch_blocks(self):
        """args.branch disagreeing with frontmatter branch blocks."""
        self._write_task("m-skip-valid", self.VALID)  # frontmatter branch=feature/skip-valid
        result = self.engine._func_create_task_file(args={
            "task": "m-skip-valid", "branch": "feature/DIFFERENT", "task_content": "",
        })
        self.assertFalse(result["success"])
        self.assertIn("Branch mismatch", result["error"])

    def test_missing_author_warns_and_no_mutation(self):
        """A valid-but-author-less file passes with a warning and is NOT rewritten."""
        content = (
            "---\ntask: m-skip-noauthor\nbranch: feature/skip-noauthor\nstatus: pending\n---\n"
            "# No Author\n\n## Success Criteria\n- [ ] x\n"
        )
        path = self._write_task("m-skip-noauthor", content)
        before = path.read_bytes()
        result = self.engine._func_create_task_file(args={
            "task": "m-skip-noauthor", "branch": "feature/skip-noauthor", "task_content": "",
        })
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["action"], "skipped")
        self.assertIn("warnings", result)
        self.assertTrue(any("Author" in w for w in result["warnings"]))
        # File must be byte-identical — the skip path never mutates the file.
        self.assertEqual(path.read_bytes(), before)

    def test_directory_task_readme_validated(self):
        """A directory-style task (README.md) is validated and its README path reported."""
        task_dir = self._tasks_dir() / "m-skip-dir"
        task_dir.mkdir(parents=True)
        (task_dir / "README.md").write_text(
            "---\ntask: m-skip-dir\nbranch: feature/skip-dir\nstatus: pending\n---\n"
            "# Dir Task\n\n**Author:** Max\n\n## Success Criteria\n- [ ] x\n",
            encoding="utf-8",
        )
        result = self.engine._func_create_task_file(args={
            "task": "m-skip-dir", "branch": "feature/skip-dir", "task_content": "",
        })
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["action"], "skipped")
        self.assertIn("README.md", result["path"])

    def test_criteria_short_form_passes(self):
        """The '## Criteria' short form is accepted on the skip path too."""
        content = (
            "---\ntask: m-skip-short\nbranch: feature/skip-short\nstatus: pending\n---\n"
            "# Short\n\n**Author:** Max\n\n## Criteria\n- [ ] x\n"
        )
        self._write_task("m-skip-short", content)
        result = self.engine._func_create_task_file(args={
            "task": "m-skip-short", "branch": "feature/skip-short", "task_content": "",
        })
        self.assertTrue(result["success"], result.get("error"))

    def test_research_branch_none_no_branch_arg_passes(self):
        """A research file (branch: none) with no branch arg passes (branch check skipped)."""
        content = (
            "---\ntask: r-skip-research\nbranch: none\nstatus: pending\n"
            "research_type: exploration\n---\n"
            "# Research\n\n**Author:** Max\n\n## Success Criteria\n- [ ] answered\n"
        )
        self._write_task("r-skip-research", content)
        result = self.engine._func_create_task_file(args={
            "task": "r-skip-research", "task_content": "",
        })
        self.assertTrue(result["success"], result.get("error"))

    def test_parity_inline_and_skip_same_error(self):
        """Identical malformed content fails inline and skip with the same first error."""
        content = (
            "---\ntask: m-parity\nbranch: feature/parity\nstatus: pending\n---\n"
            "# Parity\n\nNo criteria.\n"
        )
        inline = self.engine._func_create_task_file(args={
            "task": "m-parity", "branch": "feature/parity", "task_content": content,
        })
        self._write_task("m-parity", content)
        skip = self.engine._func_create_task_file(args={
            "task": "m-parity", "branch": "feature/parity", "task_content": "",
        })
        self.assertFalse(inline["success"])
        self.assertFalse(skip["success"])
        # Both surface the same underlying validation error.
        self.assertIn("Task file missing required section: ## Success Criteria", inline["error"])
        self.assertIn("Task file missing required section: ## Success Criteria", skip["error"])

    def test_needs_clarification_marker_blocks_skip_path(self):
        """An on-disk file still carrying a clarification marker blocks the advance."""
        marker = "[NEEDS " + "CLARIFICATION: which storage?]"
        content = (
            "---\ntask: m-skip-marker\nbranch: feature/skip-marker\nstatus: pending\n---\n"
            "# Marker\n\n**Author:** Max\n\n## Success Criteria\n- [ ] SC-1: x " + marker + "\n"
        )
        self._write_task("m-skip-marker", content)
        result = self.engine._func_create_task_file(args={
            "task": "m-skip-marker", "branch": "feature/skip-marker", "task_content": "",
        })
        self.assertFalse(result["success"])
        self.assertIn("NEEDS", result["error"])

    def test_parity_marker_same_error(self):
        """The marker rejection surfaces the same first error on both delivery paths."""
        marker = "[NEEDS " + "CLARIFICATION: which storage?]"
        content = (
            "---\ntask: m-parity-marker\nbranch: feature/parity-marker\nstatus: pending\n---\n"
            "# Parity Marker\n\n## Success Criteria\n- [ ] SC-1: x " + marker + "\n"
        )
        inline = self.engine._func_create_task_file(args={
            "task": "m-parity-marker", "branch": "feature/parity-marker", "task_content": content,
        })
        self._write_task("m-parity-marker", content)
        skip = self.engine._func_create_task_file(args={
            "task": "m-parity-marker", "branch": "feature/parity-marker", "task_content": "",
        })
        self.assertFalse(inline["success"])
        self.assertFalse(skip["success"])
        self.assertIn("NEEDS", inline["error"])
        self.assertIn("NEEDS", skip["error"])
        # Same underlying validation message on both paths.
        marker_msg = "unresolved NEEDS"
        self.assertIn(marker_msg, inline["error"])
        self.assertIn(marker_msg, skip["error"])

    def test_advance_args_allows_empty_then_func_validates(self):
        """Two-layer: _validate_advance_args allows empty task_content on existence,
        but _func_create_task_file still validates and blocks a malformed file."""
        content = (
            "---\ntask: m-twolayer\nbranch: feature/twolayer\nstatus: pending\n---\n"
            "# Two Layer\n\nNo criteria.\n"
        )
        self._write_task("m-twolayer", content)
        step = {"name": "investigation", "advance_args": ["task", "branch", "task_content"]}
        args = {"task": "m-twolayer", "branch": "feature/twolayer", "task_content": ""}
        # Layer 1: required-args validation passes (file exists → empty content allowed).
        self.assertIsNone(self.engine._validate_advance_args(step, args))
        # Layer 2: the func still catches the malformed file.
        result = self.engine._func_create_task_file(args=args)
        self.assertFalse(result["success"])
        self.assertIn("Success Criteria", result["error"])

    def test_flat_file_takes_priority_over_readme(self):
        """When both <task>.md and <task>/README.md exist, the flat file is the one
        validated (matches the flat-then-README lookup order)."""
        flat = (
            "---\ntask: m-skip-both\nbranch: feature/skip-both\nstatus: pending\n---\n"
            "# Both\n\n**Author:** Max\n\n## Success Criteria\n- [ ] x\n"
        )
        self._write_task("m-skip-both", flat)
        task_dir = self._tasks_dir() / "m-skip-both"
        task_dir.mkdir(parents=True)
        # Deliberately malformed README (no ## Success Criteria) — must be ignored
        # because the flat file exists and takes priority.
        (task_dir / "README.md").write_text(
            "---\ntask: m-skip-both\nbranch: feature/skip-both\nstatus: pending\n---\n"
            "# Bad README\n\nno criteria here\n",
            encoding="utf-8",
        )
        result = self.engine._func_create_task_file(args={
            "task": "m-skip-both", "branch": "feature/skip-both", "task_content": "",
        })
        self.assertTrue(result["success"], result.get("error"))
        self.assertTrue(result["path"].endswith("m-skip-both.md"))


class TestBrainstormTopicTemplate(ProtocolEngineTestBase):
    """Regression guard for the brainstorm-topic.md composed-file skeleton.

    The brainstorm protocol's topic step ships an inlined task-file skeleton
    (Section 3 of brainstorm-topic.md). It must satisfy `_func_create_task_file`'s
    section validation, otherwise the FIRST `protocol_advance` of every brainstorm
    fails on "Task file missing required section: ## Success Criteria". This guard
    feeds the real shipped skeleton through the validator so the template can never
    silently drift back into the broken state. See task
    h-fix-brainstorm-success-criteria (F1).
    """

    BRAINSTORM_TOPIC = (
        PROJECT_ROOT / "plugin" / "protocol-configs"
        / "sub-protocols" / "brainstorm-topic.md"
    )

    def _extract_skeleton(self) -> str:
        """Pull the single ```markdown ... ``` composed-task-file block out of
        brainstorm-topic.md Section 3 and fill its placeholders so the result can
        be fed to the validator verbatim."""
        text = self.BRAINSTORM_TOPIC.read_text(encoding="utf-8")
        # Anchor to the Section-3 heading text (not the "## 3." number, which
        # would break on harmless renumbering) so a future ```markdown block
        # elsewhere in the file can never make this guard extract the wrong skeleton.
        section = text.find("Compose Brainstorm Task File")
        self.assertNotEqual(section, -1, "Section 3 heading 'Compose Brainstorm Task File' missing in brainstorm-topic.md")
        marker = "```markdown"
        start = text.find(marker, section)
        self.assertNotEqual(start, -1, "no ```markdown fenced block in brainstorm-topic.md Section 3")
        body_start = start + len(marker)
        end = text.find("```", body_start)
        self.assertNotEqual(end, -1, "unterminated ```markdown fenced block")
        skeleton = text[body_start:end].strip("\n")
        return skeleton.replace("<name>", "regression").replace("YYYY-MM-DD", "2026-05-30")

    @staticmethod
    def _strip_section(markdown: str, header: str) -> str:
        """Remove a `## Header` block (header line through the line before the
        next `## ` header) from markdown text."""
        out = []
        skipping = False
        for line in markdown.split("\n"):
            if line.strip() == header:
                skipping = True
                continue
            if skipping and line.startswith("## "):
                skipping = False
            if not skipping:
                out.append(line)
        return "\n".join(out)

    @staticmethod
    def _frontmatter_value(skeleton: str, key: str) -> str:
        """Read a frontmatter `key: value` from the extracted skeleton so the
        validator args always agree with the skeleton (no hardcoded duplication)."""
        for line in skeleton.split("\n"):
            if line.startswith(f"{key}:"):
                return line.split(":", 1)[1].strip()
        return ""

    def test_brainstorm_topic_template_passes_validator(self):
        """The shipped skeleton must satisfy _func_create_task_file — the contract
        the first brainstorm protocol_advance depends on."""
        skeleton = self._extract_skeleton()
        result = self.engine._func_create_task_file(args={
            "task": self._frontmatter_value(skeleton, "task"),
            "branch": self._frontmatter_value(skeleton, "branch"),
            "task_content": skeleton,
        })
        self.assertTrue(result["success"], result.get("error"))

    def test_template_guard_catches_missing_section(self):
        """Mutation guard: stripping ## Success Criteria from the real skeleton
        must make the validator reject it — proving the positive test above
        actually exercises the section requirement."""
        skeleton = self._extract_skeleton()
        self.assertIn("## Success Criteria", skeleton,
                      "brainstorm-topic.md skeleton is missing ## Success Criteria")
        stripped = self._strip_section(skeleton, "## Success Criteria")
        self.assertNotIn("## Success Criteria", stripped)
        result = self.engine._func_create_task_file(args={
            "task": self._frontmatter_value(skeleton, "task"),
            "branch": self._frontmatter_value(skeleton, "branch"),
            "task_content": stripped,
        })
        self.assertFalse(result["success"])
        self.assertIn("Success Criteria", result["error"])


class TestInferTaskFromBranchBrainstorm(ProtocolEngineTestBase):
    """Test infer_task_from_branch special-case for brainstorm/<name> branches.

    Mirrors the existing optimize/ special-case behaviour: branches of the form
    `brainstorm/<name>` resolve to task `b-brainstorm-<name>` when the matching
    task file exists, and return None when the file is missing or the name part
    is empty.
    """

    def _make_task_file(self, task_name: str) -> Path:
        tasks_dir = self.temp_dir / "team-management" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / f"{task_name}.md"
        path.write_text(
            "---\nstatus: in-progress\n---\n# Stub\n\n## Success Criteria\n- [ ] x",
            encoding="utf-8",
        )
        return path

    def test_brainstorm_branch_resolves_to_b_prefixed_task(self):
        import shared_state

        self._make_task_file("b-brainstorm-foo")
        with patch.object(shared_state, "PROJECT_ROOT", self.temp_dir):
            self.assertEqual(
                shared_state.infer_task_from_branch("brainstorm/foo"),
                "b-brainstorm-foo",
            )

    def test_brainstorm_branch_directory_task_resolves(self):
        import shared_state

        tasks_dir = self.temp_dir / "team-management" / "tasks" / "b-brainstorm-bar"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "README.md").write_text(
            "---\nstatus: in-progress\n---\n# Stub\n",
            encoding="utf-8",
        )
        with patch.object(shared_state, "PROJECT_ROOT", self.temp_dir):
            self.assertEqual(
                shared_state.infer_task_from_branch("brainstorm/bar"),
                "b-brainstorm-bar",
            )

    def test_brainstorm_branch_missing_task_returns_none(self):
        import shared_state

        with patch.object(shared_state, "PROJECT_ROOT", self.temp_dir):
            self.assertIsNone(shared_state.infer_task_from_branch("brainstorm/nonexistent"))

    def test_brainstorm_branch_empty_name_returns_none(self):
        import shared_state

        with patch.object(shared_state, "PROJECT_ROOT", self.temp_dir):
            self.assertIsNone(shared_state.infer_task_from_branch("brainstorm/"))


class TestValidateCodeReviewInWorklog(ProtocolEngineTestBase):
    """Test validate_code_review_in_worklog func."""

    def test_no_review_in_worklog(self):
        """Should fail if task file has no code review section."""
        # Create a task and set state
        task_name = "m-test-review"
        tasks_dir = self.temp_dir / "team-management" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_file = tasks_dir / f"{task_name}.md"
        task_file.write_text(
            "---\nstatus: in-progress\n---\n# Test Task\n\n## Work Log\n- Did some work\n",
            encoding="utf-8",
        )
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": task_name, "branch": "feature/test", "services": [], "updated": "2026-02-18",
        })

        result = self.engine._func_validate_code_review_in_worklog()
        self.assertFalse(result["success"])
        self.assertIn("missing code review", result["error"])

    def test_review_present_in_worklog(self):
        """Should pass if task file has code review section."""
        task_name = "m-test-review-ok"
        tasks_dir = self.temp_dir / "team-management" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_file = tasks_dir / f"{task_name}.md"
        task_file.write_text(
            "---\nstatus: in-progress\n---\n# Test Task\n\n## Work Log\n- Did some work\n\n# Code Review: Test Review\n\n## Summary\nAll good.\n",
            encoding="utf-8",
        )
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": task_name, "branch": "feature/test", "services": [], "updated": "2026-02-18",
        })

        result = self.engine._func_validate_code_review_in_worklog()
        self.assertTrue(result["success"])


class TestVerifyBranchAndTask(ProtocolEngineTestBase):
    """Test verify_branch_and_task pre_func."""

    def test_no_active_task(self):
        """Should warn (not fail) when no active task."""
        result = self.engine._func_verify_branch_and_task()
        self.assertTrue(result["success"])  # Non-blocking
        self.assertTrue(len(result["warnings"]) > 0)
        self.assertTrue(any("No active task" in w for w in result["warnings"]))

    def test_task_file_missing(self):
        """Should warn when task file doesn't exist."""
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": "m-nonexistent", "branch": "feature/test", "services": [], "updated": "2026-02-18",
        })

        result = self.engine._func_verify_branch_and_task()
        self.assertTrue(result["success"])  # Non-blocking
        self.assertTrue(any("not found" in w for w in result["warnings"]))

    @patch("subprocess.run")
    def test_branch_mismatch(self, mock_run):
        """Should warn when current git branch doesn't match expected."""
        task_name = "m-test-branch"
        tasks_dir = self.temp_dir / "team-management" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / f"{task_name}.md").write_text("---\nstatus: in-progress\n---\n# Test\n", encoding="utf-8")

        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": task_name, "branch": "feature/test", "services": [], "updated": "2026-02-18",
        })

        mock_run.return_value = MagicMock(returncode=0, stdout="master\n")

        result = self.engine._func_verify_branch_and_task()
        self.assertTrue(result["success"])  # Non-blocking
        self.assertTrue(any("Branch mismatch" in w for w in result["warnings"]))

    @patch("subprocess.run")
    def test_all_checks_pass(self, mock_run):
        """Should pass with no warnings when everything matches."""
        task_name = "m-test-ok"
        tasks_dir = self.temp_dir / "team-management" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / f"{task_name}.md").write_text("---\nstatus: in-progress\n---\n# Test\n", encoding="utf-8")

        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": task_name, "branch": "feature/test", "services": [], "updated": "2026-02-18",
        })

        mock_run.return_value = MagicMock(returncode=0, stdout="feature/test\n")

        result = self.engine._func_verify_branch_and_task()
        self.assertTrue(result["success"])
        self.assertEqual(len(result["warnings"]), 0)
        self.assertEqual(result["message"], "All checks passed.")


class TestProtocolStateReadWrite(ProtocolEngineTestBase):
    """Test protocol state read/write in shared_state."""

    def test_set_and_get_protocol_state(self):
        from shared_state import set_protocol_state, get_protocol_state

        set_protocol_state("task", 2, "code-review", "2026-02-16T00:00:00Z")

        state = get_protocol_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["name"], "task")
        self.assertEqual(state["current_step"], 2)
        self.assertEqual(state["step_name"], "code-review")

    def test_clear_protocol_state(self):
        from shared_state import set_protocol_state, clear_protocol_state, get_protocol_state

        set_protocol_state("task", 0, "investigation", "2026-02-16T00:00:00Z")
        clear_protocol_state()

        state = get_protocol_state()
        self.assertIsNone(state)

    def test_get_protocol_state_none_when_empty(self):
        from shared_state import get_protocol_state

        state = get_protocol_state()
        self.assertIsNone(state)


class TestStartRefactoring(ProtocolEngineTestBase):
    """Test refactoring protocol start."""

    def test_start_refactoring(self):
        """Starting refactoring protocol should begin at test-baseline step in discussion mode."""
        result = self.engine.start_protocol("refactoring")
        self.assertTrue(result["success"])
        self.assertEqual(result["protocol"], "refactoring")
        self.assertEqual(result["step"]["index"], 0)
        self.assertEqual(result["step"]["name"], "test-baseline")
        self.assertEqual(result["step"]["mode"], "discussion")
        self.assertEqual(result["step"]["total_steps"], 6)

        # DAIC mode should be discussion
        mode = self._get_daic_mode()
        self.assertEqual(mode, "discussion")

    def test_refactoring_listed(self):
        """Refactoring protocol should appear in protocol list."""
        result = self.engine.list_protocols()
        self.assertTrue(result["success"])
        names = [p["name"] for p in result["protocols"]]
        self.assertIn("refactoring", names)

        refactoring = [p for p in result["protocols"] if p["name"] == "refactoring"][0]
        self.assertEqual(refactoring["steps_count"], 6)
        self.assertEqual(refactoring["step_names"], [
            "test-baseline", "planning", "refactoring", "test-verify", "code-review", "completion"
        ])


class TestCaptureTestBaseline(ProtocolEngineTestBase):
    """Test capture_test_baseline func handler."""

    def test_capture_test_baseline(self):
        """Should save baseline JSON correctly."""
        result = self.engine._func_capture_test_baseline(args={
            "test_command": "pytest -v",
            "baseline_summary": "47 passed, 0 failed, 2 skipped",
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["baseline"]["test_command"], "pytest -v")
        self.assertEqual(result["baseline"]["baseline_summary"], "47 passed, 0 failed, 2 skipped")
        self.assertIn("captured_at", result["baseline"])
        self.assertIn("captured_on_branch", result["baseline"])

        # Verify file was written
        baseline_file = self.temp_dir / ".claude" / "state" / "test-baseline.json"
        self.assertTrue(baseline_file.exists())
        data = self._read_json(baseline_file)
        self.assertEqual(data["test_command"], "pytest -v")
        self.assertEqual(data["baseline_summary"], "47 passed, 0 failed, 2 skipped")

    def test_capture_requires_test_command(self):
        """Should fail if test_command is missing."""
        result = self.engine._func_capture_test_baseline(args={
            "baseline_summary": "47 passed",
        })
        self.assertFalse(result["success"])
        self.assertIn("test_command", result["error"])

    def test_capture_requires_baseline_summary(self):
        """Should fail if baseline_summary is missing."""
        result = self.engine._func_capture_test_baseline(args={
            "test_command": "pytest -v",
        })
        self.assertFalse(result["success"])
        self.assertIn("baseline_summary", result["error"])

    def test_capture_requires_args(self):
        """Should fail if no args provided."""
        result = self.engine._func_capture_test_baseline()
        self.assertFalse(result["success"])
        self.assertIn("No args", result["error"])


class TestLoadTestBaseline(ProtocolEngineTestBase):
    """Test load_test_baseline func handler."""

    def test_load_test_baseline(self):
        """Should read baseline and return data."""
        # Write a baseline file first
        baseline_data = {
            "test_command": "npm test",
            "baseline_summary": "10 passed",
            "captured_at": "2026-02-21T14:30:00Z",
            "captured_on_branch": "master",
        }
        self._write_json(self.temp_dir / ".claude" / "state" / "test-baseline.json", baseline_data)

        result = self.engine._func_load_test_baseline()
        self.assertTrue(result["success"])
        self.assertEqual(result["baseline"]["test_command"], "npm test")
        self.assertEqual(result["baseline"]["baseline_summary"], "10 passed")
        self.assertEqual(result["baseline"]["captured_on_branch"], "master")

    def test_load_fails_when_missing(self):
        """Should fail if no baseline file exists."""
        result = self.engine._func_load_test_baseline()
        self.assertFalse(result["success"])
        self.assertIn("No test baseline found", result["error"])


class TestCleanupTestBaseline(ProtocolEngineTestBase):
    """Test cleanup_test_baseline func handler."""

    def test_cleanup_test_baseline(self):
        """Should remove baseline file."""
        baseline_file = self.temp_dir / ".claude" / "state" / "test-baseline.json"
        self._write_json(baseline_file, {"test_command": "pytest"})
        self.assertTrue(baseline_file.exists())

        result = self.engine._func_cleanup_test_baseline()
        self.assertTrue(result["success"])
        self.assertFalse(baseline_file.exists())

    def test_cleanup_idempotent(self):
        """Should succeed even if file already gone."""
        baseline_file = self.temp_dir / ".claude" / "state" / "test-baseline.json"
        self.assertFalse(baseline_file.exists())

        result = self.engine._func_cleanup_test_baseline()
        self.assertTrue(result["success"])


class TestCustomFuncsDiscovery(ProtocolEngineTestBase):
    """Test _discover_custom_funcs() method."""

    def _create_custom_funcs_dir(self):
        """Create the custom funcs directory and return its path."""
        d = self.temp_dir / "team-management" / "protocol-configs" / "custom" / "funcs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_discover_empty_when_no_dir(self):
        """Should return empty dict when custom/funcs/ doesn't exist."""
        result = self.engine._discover_custom_funcs()
        self.assertEqual(result, {})

    def test_discover_valid_func(self):
        """Should discover public functions from .py files."""
        funcs_dir = self._create_custom_funcs_dir()
        (funcs_dir / "my_funcs.py").write_text(
            "def greet(args=None):\n"
            "    '''Say hello.'''\n"
            "    return {'success': True, 'message': 'hi'}\n",
            encoding="utf-8",
        )
        result = self.engine._discover_custom_funcs()
        self.assertIn("greet", result)
        self.assertTrue(callable(result["greet"]))

    def test_discover_skips_underscore_functions(self):
        """Functions starting with _ should be ignored."""
        funcs_dir = self._create_custom_funcs_dir()
        (funcs_dir / "helpers.py").write_text(
            "def public_func(args=None):\n    return {'success': True}\n\n"
            "def _private_func():\n    return 'hidden'\n",
            encoding="utf-8",
        )
        result = self.engine._discover_custom_funcs()
        self.assertIn("public_func", result)
        self.assertNotIn("_private_func", result)

    def test_discover_skips_underscore_files(self):
        """Files starting with _ should be ignored entirely."""
        funcs_dir = self._create_custom_funcs_dir()
        (funcs_dir / "_internal.py").write_text(
            "def should_not_load(args=None):\n    return {'success': True}\n",
            encoding="utf-8",
        )
        result = self.engine._discover_custom_funcs()
        self.assertNotIn("should_not_load", result)

    def test_discover_handles_import_error(self):
        """Import errors in one file shouldn't break other files."""
        funcs_dir = self._create_custom_funcs_dir()
        (funcs_dir / "bad.py").write_text("this is not valid python!!!", encoding="utf-8")
        (funcs_dir / "good.py").write_text(
            "def working(args=None):\n    return {'success': True}\n",
            encoding="utf-8",
        )
        result = self.engine._discover_custom_funcs()
        self.assertIn("working", result)


class TestCustomFuncHandler(ProtocolEngineTestBase):
    """Test _get_func_handler() with custom(func_name) syntax."""

    def _create_custom_func(self, name, body):
        """Helper to create a custom func file."""
        funcs_dir = self.temp_dir / "team-management" / "protocol-configs" / "custom" / "funcs"
        funcs_dir.mkdir(parents=True, exist_ok=True)
        (funcs_dir / f"{name}.py").write_text(body, encoding="utf-8")
        # Clear caches so discovery runs fresh
        if hasattr(self.engine, "_custom_funcs_cache"):
            del self.engine._custom_funcs_cache

    def test_custom_handler_found(self):
        """custom(func_name) should resolve to the custom function."""
        self._create_custom_func("myfuncs",
            "def hello(args=None):\n    return {'success': True, 'msg': 'hello'}\n"
        )
        handler = self.engine._get_func_handler("custom(hello)")
        self.assertIsNotNone(handler)
        result = handler(args=None)
        self.assertTrue(result["success"])
        self.assertEqual(result["msg"], "hello")
        self.assertEqual(result["func"], "custom(hello)")

    def test_custom_handler_not_found(self):
        """custom(nonexistent) should return None (unknown function path)."""
        handler = self.engine._get_func_handler("custom(nonexistent)")
        self.assertIsNone(handler)

    def test_custom_handler_invalid_syntax(self):
        """Malformed custom syntax should return None."""
        self.assertIsNone(self.engine._get_func_handler("custom("))
        self.assertIsNone(self.engine._get_func_handler("custom()"))
        self.assertIsNone(self.engine._get_func_handler("custom(a b)"))

    def test_custom_handler_catches_exception(self):
        """Custom function that raises should return error dict."""
        self._create_custom_func("broken",
            "def explode(args=None):\n    raise ValueError('boom')\n"
        )
        handler = self.engine._get_func_handler("custom(explode)")
        self.assertIsNotNone(handler)
        result = handler(args=None)
        self.assertFalse(result["success"])
        self.assertIn("boom", result["error"])

    def test_custom_handler_non_dict_return(self):
        """Custom function returning non-dict should produce error."""
        self._create_custom_func("bad_return",
            "def stringy(args=None):\n    return 'not a dict'\n"
        )
        handler = self.engine._get_func_handler("custom(stringy)")
        result = handler(args=None)
        self.assertFalse(result["success"])
        self.assertIn("must return a dict", result["error"])


class TestCustomFuncExecution(ProtocolEngineTestBase):
    """Test execute_funcs() with custom() syntax."""

    def test_execute_custom_func(self):
        """custom(func) should execute through the normal func chain."""
        funcs_dir = self.temp_dir / "team-management" / "protocol-configs" / "custom" / "funcs"
        funcs_dir.mkdir(parents=True, exist_ok=True)
        (funcs_dir / "example.py").write_text(
            "def my_check(args=None):\n"
            "    task = (args or {}).get('task', '')\n"
            "    return {'success': bool(task), 'task': task}\n",
            encoding="utf-8",
        )
        results = self.engine.execute_funcs(
            ["custom(my_check)"],
            args={"task": "m-test"},
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["task"], "m-test")

    def test_execute_unknown_custom_func(self):
        """Unknown custom(func) should produce an error result."""
        results = self.engine.execute_funcs(["custom(nope)"])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["success"])
        self.assertIn("Unknown function", results[0]["error"])

    def test_execute_mixed_builtin_and_custom(self):
        """Mix of built-in and custom funcs should both execute."""
        funcs_dir = self.temp_dir / "team-management" / "protocol-configs" / "custom" / "funcs"
        funcs_dir.mkdir(parents=True, exist_ok=True)
        (funcs_dir / "mix.py").write_text(
            "def custom_ok(args=None):\n    return {'success': True, 'source': 'custom'}\n",
            encoding="utf-8",
        )
        results = self.engine.execute_funcs(["auto_detect_task", "custom(custom_ok)"])
        self.assertEqual(len(results), 2)
        # First is built-in auto_detect_task
        self.assertEqual(results[0]["func"], "auto_detect_task")
        # Second is custom
        self.assertTrue(results[1]["success"])
        self.assertEqual(results[1]["source"], "custom")


class TestGetAvailableFuncsCustom(ProtocolEngineTestBase):
    """Test get_available_funcs() includes custom funcs."""

    def test_available_funcs_without_custom(self):
        """Should work normally with no custom funcs."""
        result = self.engine.get_available_funcs()
        self.assertTrue(result["success"])
        self.assertNotIn("custom_funcs", result)

    def test_available_funcs_with_custom(self):
        """Should include custom_funcs key when custom funcs exist."""
        funcs_dir = self.temp_dir / "team-management" / "protocol-configs" / "custom" / "funcs"
        funcs_dir.mkdir(parents=True, exist_ok=True)
        (funcs_dir / "avail.py").write_text(
            "def my_validator(args=None):\n"
            "    '''Validate something important.'''\n"
            "    return {'success': True}\n",
            encoding="utf-8",
        )
        # Clear cache
        if hasattr(self.engine, "_custom_funcs_cache"):
            del self.engine._custom_funcs_cache

        result = self.engine.get_available_funcs()
        self.assertTrue(result["success"])
        self.assertIn("custom_funcs", result)
        custom = result["custom_funcs"]
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["name"], "custom(my_validator)")
        self.assertEqual(custom[0]["description"], "Validate something important.")
        # Total should include custom funcs
        builtin_count = len(result["funcs"])
        self.assertEqual(result["total"], builtin_count + 1)


class TestUpdateTaskStatusInProgress(ProtocolEngineTestBase):
    """Regression coverage for _func_update_task_status (l-dead-code-removal).

    The sessions/-fallback loop collapse mistranslated a for/else into an
    if/else, inverting the file-found logic (an EXISTING task .md wrongly
    returned 'Task file not found'). This func had zero test coverage, so the
    green suite did not catch it — these tests close that gap.
    """

    def _set_task(self, name):
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": name, "branch": "feature/x", "services": [], "updated": "2026-07-07"
        })

    def _write_task_file(self, rel, status="pending"):
        p = self.temp_dir / "team-management" / "tasks" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntask: {rel}\ncreated: 2026-07-07\nstatus: {status}\n---\n\n# Title\n", encoding="utf-8")
        return p

    def test_existing_file_task_flips_to_in_progress(self):
        self._set_task("m-sample")
        f = self._write_task_file("m-sample.md", status="pending")
        result = self.engine._func_update_task_status()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["new_status"], "in-progress")
        self.assertIn("status: in-progress", f.read_text(encoding="utf-8"))

    def test_existing_file_not_reported_missing(self):
        """The inverted-logic regression: an existing .md must NOT read 'not found'."""
        self._set_task("m-sample")
        self._write_task_file("m-sample.md")
        result = self.engine._func_update_task_status()
        self.assertNotIn("not found", (result.get("error") or "").lower())

    def test_directory_task_readme_flips(self):
        self._set_task("m-dir")
        self._write_task_file("m-dir/README.md", status="pending")
        result = self.engine._func_update_task_status()
        self.assertTrue(result["success"], result)

    def test_missing_task_file_reports_not_found(self):
        self._set_task("m-absent")
        result = self.engine._func_update_task_status()
        self.assertFalse(result["success"])
        self.assertIn("not found", result["error"].lower())


if __name__ == "__main__":
    main()
