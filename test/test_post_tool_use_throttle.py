#!/usr/bin/env python3
"""Tests for the post-tool-use hook's protocol completion-condition throttle.

Hook behaviour:
- Inject completion condition on first call after a protocol state change
  (counter file absent or 0 — set_protocol_state / clear_protocol_state
  delete the file as a side effect).
- Inject every 10th call thereafter (counter % 10 == 0).
- No-op when no protocol is active.

Run with: python3 -m pytest test/test_post_tool_use_throttle.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
HOOK_PATH = HOOKS_DIR / "post-tool-use.py"
sys.path.insert(0, str(HOOKS_DIR))


def _hook_input(tool_name="Bash"):
    return json.dumps({
        "tool_name": tool_name,
        "tool_input": {"command": "echo hi"},
        "tool_response": {"stdout": "hi", "stderr": "", "interrupted": False},
        "cwd": "",
    })


class HookSubprocessBase(TestCase):
    """Spawn the hook with cwd=temp_dir; the hook auto-detects PROJECT_ROOT
    from cwd by walking up to find .claude/."""

    PROTOCOL_NAME = "task"
    STEP_NAME = "investigation"
    END_TEXT = "TEST_END_CONDITION_TEXT"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / ".claude" / "state").mkdir(parents=True)
        (self.temp_dir / "team-management" / "protocol-configs" / "system").mkdir(parents=True)

        with open(self.temp_dir / ".claude" / "state" / "current_task.json", "w") as f:
            json.dump({
                "task": "test-task",
                "branch": "fix/test",
                "services": [],
                "updated": "2026-05-06",
                "protocol": {
                    "name": self.PROTOCOL_NAME,
                    "current_step": 0,
                    "step_name": self.STEP_NAME,
                    "started_at": "2026-05-06T00:00:00Z",
                },
            }, f)

        with open(self.temp_dir / ".claude" / "state" / "daic-mode.json", "w") as f:
            json.dump({"mode": "discussion"}, f)

        with open(
            self.temp_dir / "team-management" / "protocol-configs" / "system" / f"{self.PROTOCOL_NAME}.json",
            "w",
        ) as f:
            json.dump({
                "name": self.PROTOCOL_NAME,
                "steps": [{"name": self.STEP_NAME, "end": self.END_TEXT}],
            }, f)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _throttle_path(self):
        return self.temp_dir / ".claude" / "state" / "protocol-end-condition-counter.txt"

    def _run_hook(self):
        # Three-root model: isolate PLUGIN_ROOT to an empty temp dir so
        # load_protocol_config does not resolve the REAL repo's protocol configs
        # (get_plugin_root() otherwise falls back to this source tree via __file__).
        # With the PLUGIN_ROOT tier empty, the test's deployed-system config wins.
        plugin_dir = self.temp_dir / "plugin"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=_hook_input(),
            capture_output=True,
            text=True,
            cwd=str(self.temp_dir),
            timeout=30,
            env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(plugin_dir)},
        )

    def _read_throttle(self):
        p = self._throttle_path()
        return p.read_text().strip() if p.exists() else None


class TestHookSubprocess(HookSubprocessBase):
    def test_first_call_injects_when_no_throttle_file(self):
        self.assertFalse(self._throttle_path().exists())
        result = self._run_hook()
        self.assertIn("Completion condition:", result.stderr)
        self.assertIn(self.END_TEXT, result.stderr)
        self.assertEqual(self._read_throttle(), "1")

    def test_calls_2_through_10_do_not_inject(self):
        for n in range(1, 10):
            self._throttle_path().write_text(str(n))
            result = self._run_hook()
            self.assertNotIn(
                "Completion condition:", result.stderr,
                f"unexpected injection at counter={n}",
            )
            self.assertEqual(
                self._read_throttle(), str(n + 1),
                f"unexpected counter increment at {n}",
            )

    def test_call_11_injects(self):
        self._throttle_path().write_text("10")
        result = self._run_hook()
        self.assertIn("Completion condition:", result.stderr)
        self.assertEqual(self._read_throttle(), "11")

    def test_corrupt_throttle_file_treated_as_zero(self):
        self._throttle_path().write_text("garbage")
        result = self._run_hook()
        self.assertIn("Completion condition:", result.stderr)
        self.assertEqual(self._read_throttle(), "1")

    def test_no_active_protocol_does_not_inject(self):
        with open(self.temp_dir / ".claude" / "state" / "current_task.json", "w") as f:
            json.dump({
                "task": "test-task",
                "branch": "fix/test",
                "services": [],
                "updated": "2026-05-06",
            }, f)
        result = self._run_hook()
        self.assertNotIn("Completion condition:", result.stderr)
        self.assertEqual(self._read_throttle(), "1")


class TestSharedStateReset(TestCase):
    """set_protocol_state / clear_protocol_state must delete the throttle
    file as a side effect — covers all engine entry points (start, advance,
    goto, loop iteration, loop restart, resume, abort)."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / ".claude" / "state").mkdir(parents=True)

        with open(self.temp_dir / ".claude" / "state" / "current_task.json", "w") as f:
            json.dump({
                "task": "test-task",
                "branch": "fix/test",
                "services": [],
                "updated": "2026-05-06",
            }, f)

        import shared_state
        self._orig = {
            "PROJECT_ROOT": shared_state.PROJECT_ROOT,
            "STATE_DIR": shared_state.STATE_DIR,
            "TASK_STATE_FILE": shared_state.TASK_STATE_FILE,
            "TASK_STATE_LOCK_FILE": shared_state.TASK_STATE_LOCK_FILE,
        }
        shared_state.PROJECT_ROOT = self.temp_dir
        shared_state.STATE_DIR = self.temp_dir / ".claude" / "state"
        shared_state.TASK_STATE_FILE = self.temp_dir / ".claude" / "state" / "current_task.json"
        shared_state.TASK_STATE_LOCK_FILE = self.temp_dir / ".claude" / "state" / "current_task.lock"

    def tearDown(self):
        import shared_state
        for k, v in self._orig.items():
            setattr(shared_state, k, v)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _throttle_path(self):
        return self.temp_dir / ".claude" / "state" / "protocol-end-condition-counter.txt"

    def test_set_protocol_state_resets_throttle(self):
        self._throttle_path().write_text("5")
        import shared_state
        shared_state.set_protocol_state("task", 0, "investigation", "2026-05-06T00:00:00Z")
        self.assertFalse(self._throttle_path().exists())

    def test_clear_protocol_state_resets_throttle(self):
        self._throttle_path().write_text("5")
        import shared_state
        shared_state.clear_protocol_state()
        self.assertFalse(self._throttle_path().exists())


if __name__ == "__main__":
    main()
