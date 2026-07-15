#!/usr/bin/env python3
"""Defect 2 (h-fix-discard-clean-and-windows-transcript): a failure in
`task-transcript-link.py` AFTER the subagent-depth increment must not leave the
counter elevated (which would make the main agent be treated as a subagent for
the rest of the turn and silently bypass DAIC).

`task-transcript-link.py` increments the depth counter (line ~38) BEFORE the
transcript chunking + write. The raw `.open('w')` writes (lines ~143,153) lacked
`encoding='utf-8'`, so on Windows cp1252 a non-ASCII transcript char raised
`UnicodeEncodeError` and crashed the hook after the increment.

The fix (a) adds `encoding='utf-8'` to both writes and (b) wraps the whole
post-increment body in `try/except Exception` that decrements (undoing our own
increment) and `sys.exit(2)` to BLOCK the Task — a blocked Task means no
PostToolUse decrement, so the counter cannot be double-decremented.

We inject a deterministic post-increment failure (make `.claude/state/<type>` a
FILE so `BATCH_DIR.mkdir` raises) and assert the depth counter is restored.

Red (pre-fix): the mkdir failure crashes the hook AFTER the increment; depth
stays 1 and exit code is 1 (uncaught traceback), so the assertions fail.

Run:
  python3 -m pytest test/test_task_transcript_link_depth.py -v
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main

TEST_DIR = Path(__file__).parent
REPO_ROOT = TEST_DIR.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
HOOK = HOOKS_DIR / "task-transcript-link.py"
sys.path.insert(0, str(HOOKS_DIR))

import shared_state  # noqa: E402


class _TranscriptLinkHarness(TestCase):
    """Runs the hook as a subprocess against a temp project with CLAUDE_PROJECT_DIR."""

    TOOL_INPUT = {"subagent_type": "code-explorer", "prompt": "trace the auth flow"}

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.state = self.temp_dir / ".claude" / "state"
        self.state.mkdir(parents=True)
        # Baseline depth we expect to be restored to.
        self._write_depth(0)

        # Minimal but valid transcript: an Edit tool_use marks work-start (it is
        # consumed as the boundary), then a couple of chat turns to chunk.
        transcript = [
            {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "tool_use", "name": "Edit", "input": {}}]}},
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "message": {"role": "assistant", "content": "world"}},
        ]
        self.transcript_path = self.temp_dir / "transcript.jsonl"
        self.transcript_path.write_text(
            "".join(json.dumps(e) + "\n" for e in transcript), encoding="utf-8"
        )

        # Path segment the hook derives for the staging dir.
        self.subagent_type = shared_state.subagent_dir_name(self.TOOL_INPUT)  # "code-explorer"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_depth(self, n: int):
        (self.state / "subagent-depth.json").write_text(
            json.dumps({"depth": n}), encoding="utf-8"
        )

    def _read_depth(self) -> int:
        return json.loads((self.state / "subagent-depth.json").read_text(encoding="utf-8"))["depth"]

    def _run_hook(self):
        payload = json.dumps({
            "tool_name": "Task",
            "transcript_path": str(self.transcript_path),
            "tool_input": self.TOOL_INPUT,
        })
        env = {"CLAUDE_PROJECT_DIR": str(self.temp_dir), "PATH": __import__("os").environ["PATH"]}
        return subprocess.run(
            [sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, env=env,
        )

    # ----------------------------------------------------------------- tests

    def test_write_failure_after_increment_restores_depth_and_blocks(self):
        # Inject a deterministic post-increment failure: make the staging-type
        # dir a FILE so BATCH_DIR.mkdir raises.
        (self.state / self.subagent_type).write_text("not a dir", encoding="utf-8")

        result = self._run_hook()

        self.assertEqual(self._read_depth(), 0,
                         "depth must be restored to baseline after a post-increment failure")
        self.assertEqual(result.returncode, 2,
                         f"a staging failure must BLOCK the Task (exit 2); got {result.returncode}, "
                         f"stderr={result.stderr!r}")

    def test_clean_run_increments_and_keeps_depth(self):
        # Positive control: a normal run increments (Task proceeds; PostToolUse
        # will balance the increment later) and exits 0.
        result = self._run_hook()

        self.assertEqual(result.returncode, 0, f"clean run should exit 0; stderr={result.stderr!r}")
        self.assertEqual(self._read_depth(), 1,
                         "a clean run keeps the increment so the subagent is detected")
        # And the chunk file was actually written (the encoded write path ran).
        staged = list((self.state / self.subagent_type).rglob("current_transcript_*.json"))
        self.assertTrue(staged, "clean run should stage at least one transcript chunk")


if __name__ == "__main__":
    main()
