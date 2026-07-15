#!/usr/bin/env python3
"""Tests for the subagent-context depth counter (m-subagent-context-counter).

Replaces the old single global boolean flag (.claude/state/in_subagent_context.flag)
with a file-locked integer depth counter in shared_state. The counter must:
- increment on Task PreToolUse, decrement (clamped at 0) on Task PostToolUse,
- report in_subagent_context() == (depth > 0),
- survive parallel subagents (first finisher decrements N->N-1, not ->0),
- reset to 0 on UserPromptSubmit (user-messages.py) and SessionStart (session-start.py),
- serialise concurrent cross-process mutations under shared_state._state_lock().

Run with: python3 -m pytest test/test_subagent_depth.py -v
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main

TEST_DIR = Path(__file__).parent
REPO_ROOT = TEST_DIR.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import shared_state  # noqa: E402


class DepthHelperTests(TestCase):
    """In-process unit tests of the depth helpers, isolated to a temp STATE_DIR."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / ".claude" / "state"
        self.state.mkdir(parents=True)
        # Redirect the module-level state paths into the temp dir so we never
        # touch the real .claude/state. The helpers read these as globals at
        # call time, so patching the attributes is sufficient.
        self._orig = (
            shared_state.STATE_DIR,
            shared_state.SUBAGENT_DEPTH_FILE,
            shared_state.TASK_STATE_LOCK_FILE,
        )
        shared_state.STATE_DIR = self.state
        shared_state.SUBAGENT_DEPTH_FILE = self.state / "subagent-depth.json"
        shared_state.TASK_STATE_LOCK_FILE = self.state / "current_task.lock"

    def tearDown(self):
        (
            shared_state.STATE_DIR,
            shared_state.SUBAGENT_DEPTH_FILE,
            shared_state.TASK_STATE_LOCK_FILE,
        ) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_reads_zero(self):
        self.assertEqual(shared_state.read_subagent_depth(), 0)
        self.assertFalse(shared_state.in_subagent_context())

    def test_increment_then_decrement(self):
        self.assertEqual(shared_state.increment_subagent_depth(), 1)
        self.assertTrue(shared_state.in_subagent_context())
        self.assertEqual(shared_state.decrement_subagent_depth(), 0)
        self.assertFalse(shared_state.in_subagent_context())

    def test_parallel_teardown_regression(self):
        # The core bug: two parallel subagents, first one finishes.
        shared_state.increment_subagent_depth()
        shared_state.increment_subagent_depth()
        self.assertEqual(shared_state.read_subagent_depth(), 2)
        # First sibling completes -> decrement once.
        shared_state.decrement_subagent_depth()
        # Old boolean would now be gone (in_subagent == False) -> the bug.
        # With the counter, the still-running sibling keeps us in-subagent.
        self.assertTrue(shared_state.in_subagent_context())
        self.assertEqual(shared_state.read_subagent_depth(), 1)
        # Second sibling completes.
        shared_state.decrement_subagent_depth()
        self.assertFalse(shared_state.in_subagent_context())

    def test_nested_balances_to_zero(self):
        shared_state.increment_subagent_depth()
        shared_state.increment_subagent_depth()
        self.assertEqual(shared_state.read_subagent_depth(), 2)
        shared_state.decrement_subagent_depth()
        shared_state.decrement_subagent_depth()
        self.assertEqual(shared_state.read_subagent_depth(), 0)

    def test_decrement_clamps_at_zero(self):
        self.assertEqual(shared_state.decrement_subagent_depth(), 0)
        self.assertEqual(shared_state.decrement_subagent_depth(), 0)
        self.assertEqual(shared_state.read_subagent_depth(), 0)

    def test_corrupt_file_reads_zero(self):
        shared_state.SUBAGENT_DEPTH_FILE.write_text("not json{", encoding="utf-8")
        self.assertEqual(shared_state.read_subagent_depth(), 0)
        # And a mutation recovers cleanly from the corrupt state.
        self.assertEqual(shared_state.increment_subagent_depth(), 1)

    def test_reset_zeroes_depth(self):
        shared_state.increment_subagent_depth()
        shared_state.increment_subagent_depth()
        shared_state.increment_subagent_depth()
        self.assertTrue(shared_state.in_subagent_context())
        shared_state.reset_subagent_depth()
        self.assertEqual(shared_state.read_subagent_depth(), 0)
        self.assertFalse(shared_state.in_subagent_context())


def _child_program(action):
    """Build a -c program string that runs `action` against shared_state in a
    fresh process. cwd drives get_project_root, so the child resolves its
    STATE_DIR from the temp project root."""
    return (
        "import sys, time;"
        f"sys.path.insert(0, {str(HOOKS_DIR)!r});"
        "import shared_state;"
        f"{action}"
    )


class CrossProcessLockTests(TestCase):
    """Real cross-process contention — the production scenario for parallel
    subagents. fcntl.flock / msvcrt semantics are per open-file-description,
    so only separate OS processes exercise the lock; threads would not."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".claude" / "state").mkdir(parents=True)
        self.depth_file = self.tmp / ".claude" / "state" / "subagent-depth.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spawn(self, action, n):
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _child_program(action)],
                cwd=str(self.tmp),
            )
            for _ in range(n)
        ]
        for p in procs:
            p.wait(timeout=60)

    def _read_depth(self):
        try:
            return int(json.loads(self.depth_file.read_text()).get("depth", 0))
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            return 0

    def test_concurrent_increments_no_lost_updates(self):
        # N processes each increment once. Without a correct lock a lost update
        # would leave depth < N. With the lock, depth == N exactly.
        n = 20
        self._spawn("shared_state.increment_subagent_depth()", n)
        self.assertEqual(self._read_depth(), n)

    def test_concurrent_balanced_ends_at_zero(self):
        n = 20
        self._spawn(
            "shared_state.increment_subagent_depth();"
            "time.sleep(0.01);"
            "shared_state.decrement_subagent_depth()",
            n,
        )
        self.assertEqual(self._read_depth(), 0)


class HookWiringTests(TestCase):
    """The hooks must use the counter, not the legacy boolean flag."""

    def _src(self, name):
        return (HOOKS_DIR / name).read_text(encoding="utf-8")

    def test_task_transcript_link_increments(self):
        src = self._src("task-transcript-link.py")
        self.assertIn("increment_subagent_depth(", src)
        self.assertNotIn("in_subagent_context.flag", src)
        # Increment is skipped under workflow bypass, mirroring post-tool-use's
        # bypass early-return, so the counter stays symmetric (no inc without dec).
        self.assertIn("check_workflow_bypass(", src)

    def test_post_tool_use_uses_counter(self):
        src = self._src("post-tool-use.py")
        self.assertIn("in_subagent_context(", src)
        self.assertIn("decrement_subagent_depth(", src)
        self.assertNotIn("in_subagent_context.flag", src)

    def test_sessions_enforce_uses_counter(self):
        src = self._src("sessions-enforce.py")
        self.assertIn("in_subagent_context(", src)
        self.assertNotIn("in_subagent_context.flag", src)

    def test_user_messages_resets(self):
        src = self._src("user-messages.py")
        self.assertIn("reset_subagent_depth(", src)

    def test_session_start_resets_and_clears_legacy_flag(self):
        src = self._src("session-start.py")
        self.assertIn("reset_subagent_depth(", src)
        self.assertIn("in_subagent_context.flag", src)  # legacy cleanup


class SessionStartRecoveryTests(TestCase):
    """Stale depth left by a crashed session must be cleared on session start,
    and the legacy boolean flag must be removed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / ".claude" / "state"
        self.state.mkdir(parents=True)
        (self.tmp / "team-management" / "protocol-configs" / "system").mkdir(parents=True)
        self.depth_file = self.state / "subagent-depth.json"
        self.legacy_flag = self.state / "in_subagent_context.flag"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_session_start_resets_stale_depth(self):
        self.depth_file.write_text(json.dumps({"depth": 3}), encoding="utf-8")
        self.legacy_flag.touch()
        subprocess.run(
            [sys.executable, str(HOOKS_DIR / "session-start.py")],
            cwd=str(self.tmp),
            input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}),
            text=True,
            capture_output=True,
            timeout=60,
        )
        depth = 0
        if self.depth_file.exists():
            try:
                depth = int(json.loads(self.depth_file.read_text()).get("depth", 0))
            except (json.JSONDecodeError, ValueError, TypeError):
                depth = -1
        self.assertEqual(depth, 0)
        self.assertFalse(self.legacy_flag.exists())


class UserPromptResetTests(TestCase):
    """UserPromptSubmit must reset the depth even when workflow bypass is on —
    bypass is precisely the long-lived mode where a stale counter accumulates."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / ".claude" / "state"
        self.state.mkdir(parents=True)
        (self.tmp / "team-management").mkdir(parents=True)
        self.depth_file = self.state / "subagent-depth.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_user_messages(self):
        return subprocess.run(
            [sys.executable, str(HOOKS_DIR / "user-messages.py")],
            cwd=str(self.tmp),
            input=json.dumps({"prompt": "hello", "hook_event_name": "UserPromptSubmit"}),
            text=True,
            capture_output=True,
            timeout=60,
        )

    def test_reset_runs_normally(self):
        self.depth_file.write_text(json.dumps({"depth": 2}), encoding="utf-8")
        self._run_user_messages()
        self.assertEqual(json.loads(self.depth_file.read_text()).get("depth"), 0)

    def test_reset_runs_under_workflow_bypass(self):
        self.depth_file.write_text(json.dumps({"depth": 2}), encoding="utf-8")
        (self.state / "workflow-bypass.json").write_text(
            json.dumps({"enabled": True}), encoding="utf-8"
        )
        self._run_user_messages()
        self.assertEqual(json.loads(self.depth_file.read_text()).get("depth"), 0)


class AgentDispatchRecognitionTests(TestCase):
    """The subagent-dispatch tool is named 'Task' in some Claude Code harnesses
    and 'Agent' in others. The depth counter must increment on PreToolUse and
    decrement on PostToolUse for BOTH names, else subagent tool calls leak past
    every in_subagent_context() gate (the auto-worklog gate, the DAIC subagent
    bypass, reminder/auto-sync suppression). m-fix-auto-worklog-bash-corruption.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / ".claude" / "state"
        self.state.mkdir(parents=True)
        self.depth_file = self.state / "subagent-depth.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read_depth(self):
        if not self.depth_file.exists():
            return 0
        return int(json.loads(self.depth_file.read_text()).get("depth", 0))

    def _run(self, hook, payload):
        return subprocess.run(
            [sys.executable, str(HOOKS_DIR / hook)],
            cwd=str(self.tmp),
            input=json.dumps(payload),
            text=True, capture_output=True, timeout=60,
        )

    def test_increment_on_agent_dispatch(self):
        self._run("task-transcript-link.py", {"tool_name": "Agent", "tool_input": {}})
        self.assertEqual(self._read_depth(), 1)

    def test_increment_on_task_dispatch(self):
        self._run("task-transcript-link.py", {"tool_name": "Task", "tool_input": {}})
        self.assertEqual(self._read_depth(), 1)

    def test_no_increment_on_other_tool(self):
        self._run("task-transcript-link.py",
                  {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(self._read_depth(), 0)

    def test_decrement_on_agent_completion(self):
        self.depth_file.write_text(json.dumps({"depth": 1}), encoding="utf-8")
        self._run("post-tool-use.py", {
            "tool_name": "Agent", "tool_input": {},
            "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
            "cwd": str(self.tmp),
        })
        self.assertEqual(self._read_depth(), 0)

    def test_roundtrip_agent_dispatch_balances(self):
        self._run("task-transcript-link.py", {"tool_name": "Agent", "tool_input": {}})
        self.assertEqual(self._read_depth(), 1)
        self._run("post-tool-use.py", {
            "tool_name": "Agent", "tool_input": {},
            "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
            "cwd": str(self.tmp),
        })
        self.assertEqual(self._read_depth(), 0)


class HookMatcherTests(TestCase):
    """The PreToolUse matcher for task-transcript-link.py must include 'Agent'
    so the subagent-depth increment fires for an Agent-named dispatch
    (m-fix-auto-worklog-bash-corruption). The matcher lives in the plugin hook
    manifest now that the installer is retired (m-installer-retirement)."""

    def test_transcript_link_matcher_includes_agent(self):
        manifest = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
        matchers = [
            entry.get("matcher")
            for entry in manifest["hooks"]["PreToolUse"]
            if any("task-transcript-link.py" in h.get("command", "")
                   for h in entry.get("hooks", []))
        ]
        self.assertEqual(
            matchers, ["Task|Agent"],
            f"task-transcript-link.py PreToolUse matcher must be exactly "
            f"'Task|Agent', found {matchers}"
        )


if __name__ == "__main__":
    main()
