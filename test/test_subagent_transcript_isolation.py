#!/usr/bin/env python3
"""Tests for per-Task-invocation subagent transcript isolation
(m-subagent-transcript-isolation).

The subagent transcript pipeline stages chunks of the parent transcript in
.claude/state/{subagent_type}/ (task-transcript-link.py, Task PreToolUse) and
post-tool-use.py archives them into the task record (Task PostToolUse). The fix:
- task-transcript-link resolves subagent_type from its OWN tool_input (not by
  scanning clean_transcript[-1], which misattributes under parallel Tasks and
  raises IndexError on an empty deque),
- both hooks key the staging dir by shared_state.subagent_transcript_key(tool_input)
  so parallel same-type subagents don't clobber one shared dir,
- post-tool-use removes the keyed staging dir after archiving.

Run with: python3 -m pytest test/test_subagent_transcript_isolation.py -v
"""

import json
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path
from unittest import TestCase, main

TEST_DIR = Path(__file__).parent
REPO_ROOT = TEST_DIR.parent
HOOKS_DIR = REPO_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import shared_state  # noqa: E402

TT_LINK = HOOKS_DIR / "task-transcript-link.py"
POST = HOOKS_DIR / "post-tool-use.py"


def _write_fake_transcript(path, with_edit=True):
    """A minimal parent transcript. task-transcript-link strips entries until it
    sees an Edit/Write/MultiEdit tool_use, then chunks what remains. List-shaped
    content avoids the legacy str-iteration crash so pre-fix runs to completion
    and the clobber/IndexError are the observable failures."""
    lines = []
    if with_edit:
        lines.append({"type": "assistant", "message": {"role": "assistant",
                     "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "x"}}]}})
    lines.append({"type": "user", "message": {"role": "user",
                 "content": [{"type": "text", "text": "context"}]}})
    lines.append({"type": "assistant", "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "reply"}]}})
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


class KeyHelperTests(TestCase):
    def test_deterministic(self):
        ti = {"subagent_type": "codex-cli", "prompt": "do X"}
        self.assertEqual(
            shared_state.subagent_transcript_key(ti),
            shared_state.subagent_transcript_key(dict(ti)),
        )

    def test_key_ordering_insensitive(self):
        a = shared_state.subagent_transcript_key({"a": 1, "b": 2})
        b = shared_state.subagent_transcript_key({"b": 2, "a": 1})
        self.assertEqual(a, b)

    def test_differs_on_prompt(self):
        k1 = shared_state.subagent_transcript_key({"subagent_type": "codex-cli", "prompt": "P1"})
        k2 = shared_state.subagent_transcript_key({"subagent_type": "codex-cli", "prompt": "P2"})
        self.assertNotEqual(k1, k2)

    def test_shape(self):
        k = shared_state.subagent_transcript_key({"prompt": "x"})
        self.assertEqual(len(k), 16)
        int(k, 16)  # raises if not hex

    def test_non_serializable_does_not_raise(self):
        # A set is not JSON-serializable; the helper must fall back, not crash.
        k = shared_state.subagent_transcript_key({"weird": {1, 2, 3}})
        self.assertEqual(len(k), 16)


class DirNameTests(TestCase):
    def test_normal_passthrough(self):
        self.assertEqual(shared_state.subagent_dir_name({"subagent_type": "codex-cli"}), "codex-cli")

    def test_traversal_stripped(self):
        # No path separators or dots survive → cannot escape state/<type>/.
        out = shared_state.subagent_dir_name({"subagent_type": "../../etc/passwd"})
        for bad in ("/", "\\", ".", ":"):
            self.assertNotIn(bad, out)
        self.assertTrue(out)

    def test_null_falls_back_to_shared(self):
        self.assertEqual(shared_state.subagent_dir_name({"subagent_type": None}), "shared")

    def test_missing_falls_back_to_shared(self):
        self.assertEqual(shared_state.subagent_dir_name({}), "shared")
        self.assertEqual(shared_state.subagent_dir_name(None), "shared")


class HookWiringTests(TestCase):
    def _src(self, p):
        return p.read_text(encoding="utf-8")

    def test_task_transcript_link_uses_own_payload_and_key(self):
        src = self._src(TT_LINK)
        self.assertIn("subagent_transcript_key(", src)
        # subagent_type resolved from the hook's own payload (via the sanitising
        # helper), not by scanning the parent transcript.
        self.assertIn("subagent_dir_name(tool_input)", src)
        self.assertIn('input_data.get("tool_input"', src)
        # The fragile parent-transcript scan must be gone.
        self.assertNotIn("clean_transcript[-1]", src)

    def test_post_tool_use_uses_key_and_cleans_up(self):
        src = self._src(POST)
        self.assertIn("subagent_transcript_key(", src)
        self.assertIn("rmtree(", src)


def _run_hook(hook_path, payload, cwd):
    return subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload), text=True, capture_output=True,
        cwd=str(cwd), timeout=60,
    )


class IsolationTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".claude" / "state").mkdir(parents=True)
        (self.tmp / "team-management" / "tasks").mkdir(parents=True)
        self.transcript = self.tmp / "transcript.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self, *parts):
        return self.tmp / ".claude" / "state" / Path(*parts)

    def test_parallel_same_type_do_not_clobber(self):
        _write_fake_transcript(self.transcript)
        for prompt in ("PROMPT-ONE", "PROMPT-TWO"):
            r = _run_hook(TT_LINK, {
                "tool_name": "Task",
                "tool_input": {"subagent_type": "codex-cli", "prompt": prompt},
                "transcript_path": str(self.transcript),
            }, self.tmp)
            self.assertEqual(r.returncode, 0, r.stderr)
        type_dir = self._state("codex-cli")
        keyed = [d for d in type_dir.iterdir() if d.is_dir()]
        # Two same-type invocations with different prompts → two keyed dirs,
        # each holding its own chunk. Pre-fix: one shared dir, second clobbers.
        self.assertEqual(len(keyed), 2, f"expected 2 keyed dirs, got {[d.name for d in keyed]}")
        for d in keyed:
            self.assertTrue(list(d.glob("current_transcript_*.json")))

    def test_traversal_subagent_type_stays_under_state(self):
        # End-to-end safety: a crafted subagent_type must not let the hook (and
        # post-tool-use's rmtree) escape .claude/state/<type>/.
        _write_fake_transcript(self.transcript)
        r = _run_hook(TT_LINK, {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "../../pwned", "prompt": "x"},
            "transcript_path": str(self.transcript),
        }, self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self._state("pwned").exists())          # sanitised, under state/
        self.assertFalse((self.tmp / "pwned").exists())          # no escape one level up
        self.assertFalse((self.tmp.parent / "pwned").exists())   # no escape outside the project

    def test_null_subagent_type_through_hook(self):
        _write_fake_transcript(self.transcript)
        r = _run_hook(TT_LINK, {
            "tool_name": "Task",
            "tool_input": {"subagent_type": None, "prompt": "x"},
            "transcript_path": str(self.transcript),
        }, self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self._state("shared").exists())

    def test_empty_transcript_no_indexerror(self):
        # No Edit/Write/MultiEdit → the strip loop empties the deque. Must not
        # raise IndexError (pre-fix: clean_transcript[-1] crashes).
        _write_fake_transcript(self.transcript, with_edit=False)
        r = _run_hook(TT_LINK, {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "codex-cli", "prompt": "x"},
            "transcript_path": str(self.transcript),
        }, self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("IndexError", r.stderr)

    def test_corrupt_transcript_lines_do_not_crash(self):
        # C3 (m-hooks-hygiene-sweep): a blank or partially-written transcript
        # line must not crash the PreToolUse hook — the crash happened AFTER
        # the subagent_depth increment, desyncing the counter. Valid lines
        # around the corrupt ones must still be staged.
        valid_edit = {"type": "assistant", "message": {"role": "assistant",
                      "content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "x"}}]}}
        valid_user = {"type": "user", "message": {"role": "user",
                      "content": [{"type": "text", "text": "context"}]}}
        self.transcript.write_text(
            json.dumps(valid_edit) + "\n"
            + "\n"                                  # blank line
            + '{"type": "user", "mess' + "\n"       # truncated/partial write
            + json.dumps(valid_user) + "\n",
            encoding="utf-8",
        )
        r = _run_hook(TT_LINK, {
            "tool_name": "Task",
            "tool_input": {"subagent_type": "codex-cli", "prompt": "x"},
            "transcript_path": str(self.transcript),
        }, self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("JSONDecodeError", r.stderr)
        type_dir = self._state("codex-cli")
        keyed = [d for d in type_dir.iterdir() if d.is_dir()]
        self.assertEqual(len(keyed), 1)
        self.assertTrue(list(keyed[0].glob("current_transcript_*.json")),
                        "valid entries around corrupt lines must still be staged")

    def test_pre_then_post_archives_and_cleans_source(self):
        _write_fake_transcript(self.transcript)
        tool_input = {"subagent_type": "codex-cli", "prompt": "archive me"}
        key = shared_state.subagent_transcript_key(tool_input)

        # Active task so post-tool-use performs the archival copy.
        (self._state("current_task.json")).write_text(json.dumps({
            "task": "t-iso", "branch": "fix/iso", "services": [],
        }), encoding="utf-8")

        # PreToolUse: stage chunks under state/codex-cli/{key}/.
        r1 = _run_hook(TT_LINK, {
            "tool_name": "Task", "tool_input": tool_input,
            "transcript_path": str(self.transcript),
        }, self.tmp)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        source = self._state("codex-cli", key)
        self.assertTrue(list(source.glob("current_transcript_*.json")))

        # PostToolUse on the completing Task archives + removes the source.
        r2 = _run_hook(POST, {
            "tool_name": "Task", "tool_input": tool_input,
            "tool_response": {"success": True}, "cwd": str(self.tmp),
        }, self.tmp)
        self.assertEqual(r2.returncode, 0, r2.stderr)

        dest = self._state("tasks", "t-iso", "transcripts", "codex-cli", key)
        self.assertTrue(list(dest.glob("current_transcript_*.json")),
                        "chunks should be archived under the keyed dest")
        self.assertFalse(source.exists(), "keyed staging dir should be removed after archiving")


if __name__ == "__main__":
    main()
