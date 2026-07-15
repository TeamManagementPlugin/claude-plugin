#!/usr/bin/env python3
"""Bounded staged-transcript cap for task-transcript-link.py
(m-fix-unbounded-transcript-reads, SC#2).

`task-transcript-link.py` runs on EVERY Task/Agent PreToolUse dispatch. It
previously read the WHOLE parent transcript into memory and tiktoken-encoded
every entry while chunking — unbounded CPU + RSS in a blocking hook as the
session JSONL grows to tens of MB.

The fix tail-reads only the last `TRANSCRIPT_STAGE_CAP_BYTES` (1 MB) via
`shared_state._read_file_tail` before parsing/chunking, and — because the
Edit/Write/MultiEdit work-start marker may fall BEFORE that window — skips the
pre-work-removal strip loop when the read was capped, so the bounded tail is
retained instead of the strip loop emptying it and starving the subagent.

This test builds a >1 MB transcript with the Edit boundary + an EARLY sentinel
at the very start (before the 1 MB tail window) and a RECENT sentinel at EOF,
then asserts the hook (a) still stages chunks (no starvation despite the marker
being outside the window) and (b) stages ONLY the recent tail (recent sentinel
present, early sentinel dropped). The "early sentinel absent" assertion is the
deterministic proof that the cap is applied — it does not depend on wall-clock.

Run: python3 -m pytest test/test_transcript_stage_cap.py -v
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

EARLY_SENTINEL = "EARLY_SENTINEL_aaaaaaaa"
RECENT_SENTINEL = "RECENT_SENTINEL_zzzzzzzz"


class StageCapTests(TestCase):
    TOOL_INPUT = {"subagent_type": "codex-cli", "prompt": "review the diff"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / ".claude" / "state"
        self.state.mkdir(parents=True)
        (self.state / "subagent-depth.json").write_text(
            json.dumps({"depth": 0}), encoding="utf-8")
        self.transcript = self.tmp / "transcript.jsonl"
        self.subagent_type = shared_state.subagent_dir_name(self.TOOL_INPUT)  # "codex-cli"

    def _read_depth(self):
        return json.loads((self.state / "subagent-depth.json").read_text(encoding="utf-8"))["depth"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_big_transcript(self):
        """~1.5 MB transcript: Edit boundary + EARLY sentinel at the start, then
        ~1.5 MB of filler user turns, then the RECENT sentinel at EOF."""
        lines = [
            {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "x"}}]}},
            {"type": "user", "message": {"role": "user",
             "content": [{"type": "text", "text": EARLY_SENTINEL}]}},
        ]
        filler = {"type": "user", "message": {"role": "user",
                  "content": [{"type": "text", "text": "F" * 900}]}}
        lines += [filler for _ in range(1600)]  # ~1.5 MB of filler
        lines.append({"type": "assistant", "message": {"role": "assistant",
                      "content": [{"type": "text", "text": RECENT_SENTINEL}]}})
        self.transcript.write_text(
            "\n".join(json.dumps(x) for x in lines), encoding="utf-8"
        )

    def _run_hook(self):
        payload = json.dumps({
            "tool_name": "Task",
            "transcript_path": str(self.transcript),
            "tool_input": self.TOOL_INPUT,
        })
        env = {"CLAUDE_PROJECT_DIR": str(self.tmp),
               "PATH": __import__("os").environ["PATH"]}
        return subprocess.run(
            [sys.executable, str(HOOK)], input=payload,
            capture_output=True, text=True, env=env, timeout=60,
        )

    def _staged_chunks(self):
        type_dir = self.tmp / ".claude" / "state" / self.subagent_type
        return list(type_dir.rglob("current_transcript_*.json"))

    def test_cap_respected_recent_staged_early_dropped(self):
        self._write_big_transcript()
        self.assertGreater(self.transcript.stat().st_size, 1_048_576,
                           "fixture must exceed the 1 MB cap to exercise the tail-read")

        r = self._run_hook()
        self.assertEqual(r.returncode, 0, f"hook must exit 0; stderr={r.stderr!r}")

        chunks = self._staged_chunks()
        # No starvation: the Edit boundary sits BEFORE the window, but a capped
        # read retains the tail (strip loop skipped) so chunks ARE staged.
        self.assertTrue(chunks, "capped read must still stage the recent tail (no starvation)")

        staged_text = "".join(c.read_text(encoding="utf-8") for c in chunks)
        # Cap applied: the recent tail is staged, the pre-window content is not.
        self.assertIn(RECENT_SENTINEL, staged_text,
                      "the recent (in-window) content must be staged")
        self.assertNotIn(EARLY_SENTINEL, staged_text,
                         "content before the 1 MB tail window must be dropped by the cap")

    def test_staged_bytes_bounded(self):
        # Secondary, deterministic boundedness check: staged output stays within
        # a small multiple of the 1 MB cap, not the full ~1.5 MB+ transcript
        # (pretty-printed chunks inflate the slice, hence the generous ceiling).
        self._write_big_transcript()
        r = self._run_hook()
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        total = sum(c.stat().st_size for c in self._staged_chunks())
        self.assertLess(total, 4 * 1_048_576,
                        f"staged bytes should be bounded by the ~1 MB cap, got {total}")

    def test_small_transcript_strips_prework_as_before(self):
        # (codex) Non-capped (small) path must stay identical to today: the
        # pre-work strip loop runs, dropping everything up to+including the first
        # Edit boundary. PRE sentinel dropped, POST sentinel staged.
        lines = [
            {"type": "user", "message": {"role": "user",
             "content": [{"type": "text", "text": EARLY_SENTINEL}]}},   # pre-work
            {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "x"}}]}},
            {"type": "assistant", "message": {"role": "assistant",
             "content": [{"type": "text", "text": RECENT_SENTINEL}]}},  # post-work
        ]
        self.transcript.write_text(
            "\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        self.assertLess(self.transcript.stat().st_size, 1_048_576)

        r = self._run_hook()
        self.assertEqual(r.returncode, 0, f"stderr={r.stderr!r}")
        staged_text = "".join(c.read_text(encoding="utf-8") for c in self._staged_chunks())
        self.assertIn(RECENT_SENTINEL, staged_text, "post-boundary content must be staged")
        self.assertNotIn(EARLY_SENTINEL, staged_text, "pre-work content must be stripped")

    def test_unreadable_transcript_degrades_gracefully(self):
        # (codex) SC#3: a set-but-unreadable transcript_path must NOT block the
        # Task. _read_file_tail swallows the read error to ([], False) -> empty
        # staging -> exit 0, and the depth increment is kept (PostToolUse balances
        # it) since the Task proceeds.
        missing = self.tmp / "does-not-exist.jsonl"
        payload = json.dumps({
            "tool_name": "Task",
            "transcript_path": str(missing),
            "tool_input": self.TOOL_INPUT,
        })
        env = {"CLAUDE_PROJECT_DIR": str(self.tmp),
               "PATH": __import__("os").environ["PATH"]}
        r = subprocess.run([sys.executable, str(HOOK)], input=payload,
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0,
                         f"unreadable transcript must degrade gracefully, not block; stderr={r.stderr!r}")
        self.assertNotIn("blocking Task", r.stderr)
        self.assertEqual(self._read_depth(), 1,
                         "increment kept (Task proceeds -> PostToolUse balances it)")
        self.assertEqual(self._staged_chunks(), [], "no chunks staged for an unreadable transcript")


if __name__ == "__main__":
    main()
