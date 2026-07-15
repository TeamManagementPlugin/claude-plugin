#!/usr/bin/env python3
"""Tests for the post-tool-use hook's auto-worklog entry.

Covers the m-fix-auto-worklog-bash-corruption fix:
- Multi-line Bash commands collapse to a single well-formed markdown list line
  (the corruption fix) and keep the HEAD on truncation.
- Edit/Write entries are unchanged: file_path, tail-truncated (basename survives).
- Entries are inserted INSIDE the ``## Work Log`` section (before the next
  level<=2 heading), not at EOF; nested ###/#### subheadings (logging-agent
  format) and fenced code blocks are handled correctly.
- The not-in-subagent gate: with subagent depth > 0 no entry is written.
- Throttle: only the 8th significant call writes.

The hook is run as a subprocess with cwd=temp_dir, mirroring
test_post_tool_use_throttle.py (the hook auto-detects PROJECT_ROOT from cwd).

Run with: python3 -m pytest test/test_post_tool_use_worklog.py -v
"""

import datetime
import json
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

TODAY = datetime.date.today().strftime("%Y-%m-%d")


class WorklogHookBase(TestCase):
    STEP_NAME = "implementation"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / ".claude" / "state").mkdir(parents=True)
        (self.temp_dir / "team-management" / "tasks").mkdir(parents=True)
        self.task_name = "test-task"
        self._write_current_task(self.STEP_NAME)
        with open(self.temp_dir / ".claude" / "state" / "daic-mode.json", "w") as f:
            json.dump({"mode": "implementation"}, f)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_current_task(self, step_name):
        with open(self.temp_dir / ".claude" / "state" / "current_task.json", "w") as f:
            json.dump({
                "task": self.task_name,
                "branch": "fix/test",
                "services": [],
                "updated": TODAY,
                "protocol": {
                    "name": "task",
                    "current_step": 1,
                    "step_name": step_name,
                    "started_at": "2026-05-06T00:00:00Z",
                },
            }, f)

    def _task_file(self):
        return self.temp_dir / "team-management" / "tasks" / f"{self.task_name}.md"

    def _write_task(self, content):
        self._task_file().write_text(content, encoding="utf-8")

    def _set_counter(self, n):
        (self.temp_dir / ".claude" / "state" / "worklog-auto-counter.txt").write_text(str(n))

    def _set_depth(self, n):
        (self.temp_dir / ".claude" / "state" / "subagent-depth.json").write_text(
            json.dumps({"depth": n})
        )

    def _run(self, tool_name, tool_input):
        payload = json.dumps({
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
            "cwd": str(self.temp_dir),
        })
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=payload, capture_output=True, text=True,
            cwd=str(self.temp_dir), timeout=30,
        )

    def _fire_bash(self, command, counter=7):
        """Fire one Bash call at the throttle boundary (8th) and return new content."""
        self._set_counter(counter)
        self._run("Bash", {"command": command})
        return self._task_file().read_text(encoding="utf-8")

    def _auto_lines(self, content):
        return [ln for ln in content.split("\n") if "[auto]" in ln]


class TestBashSanitization(WorklogHookBase):
    def test_multiline_bash_collapses_to_single_line(self):
        self._write_task("# T\n\n## Work Log\n- [2026-05-30] start\n")
        content = self._fire_bash("if x\nthen y\nfi")
        autos = self._auto_lines(content)
        self.assertEqual(len(autos), 1, content)
        self.assertEqual(autos[0], f"- [{TODAY}] [auto] Bash: if x then y fi")
        # no continuation line leaked as top-level markdown
        lines = content.split("\n")
        self.assertNotIn("then y", lines)
        self.assertNotIn("fi", lines)

    def test_bash_keeps_head_on_truncation(self):
        self._write_task("# T\n\n## Work Log\n- start\n")
        content = self._fire_bash("echo " + ("a" * 100))
        autos = self._auto_lines(content)
        self.assertEqual(len(autos), 1)
        value = autos[0].split("[auto] Bash: ", 1)[1]
        self.assertTrue(value.startswith("echo "), value)
        self.assertTrue(value.endswith("..."), value)
        self.assertEqual(len(value), 80)

    def test_collapses_crlf_and_tabs_and_runs(self):
        self._write_task("# T\n\n## Work Log\n- start\n")
        content = self._fire_bash("a\r\n\tb   c")
        autos = self._auto_lines(content)
        self.assertEqual(autos[0], f"- [{TODAY}] [auto] Bash: a b c")


class TestEditWriteUnchanged(WorklogHookBase):
    LONG_PATH = (
        "plugin/installer/configurators/ai_providers/"
        "codex_provider_with_a_very_long_name.py"
    )

    def test_write_keeps_tail_on_truncation(self):
        self.assertGreater(len(self.LONG_PATH), 80)
        self._write_task("# T\n\n## Work Log\n- start\n")
        self._set_counter(7)
        self._run("Write", {"file_path": self.LONG_PATH})
        content = self._task_file().read_text(encoding="utf-8")
        autos = self._auto_lines(content)
        self.assertEqual(len(autos), 1)
        value = autos[0].split("[auto] Write: ", 1)[1]
        self.assertTrue(value.startswith("..."), value)
        self.assertTrue(value.endswith(self.LONG_PATH[-77:]), value)
        self.assertEqual(len(value), 80)

    def test_short_path_not_truncated(self):
        self._write_task("# T\n\n## Work Log\n- start\n")
        self._set_counter(7)
        self._run("Edit", {"file_path": "a/b.py"})
        content = self._task_file().read_text(encoding="utf-8")
        autos = self._auto_lines(content)
        self.assertEqual(autos[0], f"- [{TODAY}] [auto] Edit: a/b.py")


class TestSectionAwareInsertion(WorklogHookBase):
    def _idx(self, content, pred):
        lines = content.split("\n")
        return next(i for i, ln in enumerate(lines) if pred(ln))

    def test_insert_before_trailing_code_review(self):
        self._write_task(
            "# T\n\n## Work Log\n- [2026-05-30] start\n\n# Code Review: x\n- review note\n"
        )
        content = self._fire_bash("ls")
        auto_idx = self._idx(content, lambda l: "[auto]" in l)
        cr_idx = self._idx(content, lambda l: l.startswith("# Code Review"))
        self.assertLess(auto_idx, cr_idx)
        self.assertIn("# Code Review: x", content)
        self.assertIn("- review note", content)

    def test_nested_subheadings_append_at_section_end(self):
        # logging-agent consolidated format: ## Work Log holds ### [Date] / #### Completed
        self._write_task(
            "# T\n\n## Work Log\n\n### 2026-05-29\n\n#### Completed\n- did X\n\n"
            "# Code Review: x\n- note\n"
        )
        content = self._fire_bash("ls")
        lines = content.split("\n")
        auto_idx = next(i for i, l in enumerate(lines) if "[auto]" in l)
        did_idx = lines.index("- did X")
        cr_idx = next(i for i, l in enumerate(lines) if l.startswith("# Code Review"))
        # appended at the END of Work Log (after the nested subsection), not the top
        self.assertGreater(auto_idx, did_idx)
        self.assertLess(auto_idx, cr_idx)

    def test_fenced_heading_not_treated_as_boundary(self):
        self._write_task(
            "# T\n\n## Work Log\n- start\n\n```\n# not a heading\n```\n\n"
            "# Code Review: x\n- note\n"
        )
        content = self._fire_bash("ls")
        lines = content.split("\n")
        auto_idx = next(i for i, l in enumerate(lines) if "[auto]" in l)
        fenced_idx = lines.index("# not a heading")
        cr_idx = next(i for i, l in enumerate(lines) if l.startswith("# Code Review"))
        self.assertGreater(auto_idx, fenced_idx)
        self.assertLess(auto_idx, cr_idx)

    def test_fenced_work_log_heading_ignored_in_search(self):
        # A "## Work Log" line inside a fenced markdown example BEFORE the real
        # section must not be mistaken for the section heading.
        self._write_task(
            "# T\n\nExample of the format:\n```\n## Work Log\n- example entry\n```\n\n"
            "## Work Log\n- real start\n\n# Code Review: x\n- note\n"
        )
        content = self._fire_bash("ls")
        lines = content.split("\n")
        auto_idx = next(i for i, l in enumerate(lines) if "[auto]" in l)
        real_idx = lines.index("- real start")
        cr_idx = next(i for i, l in enumerate(lines) if l.startswith("# Code Review"))
        # entry lands in the REAL section (after "- real start", before Code Review),
        # not inside the fenced example
        self.assertGreater(auto_idx, real_idx)
        self.assertLess(auto_idx, cr_idx)
        self.assertEqual(content.count("[auto]"), 1)

    def test_section_created_when_absent(self):
        self._write_task("# T\n\nSome body, no work log here.\n")
        content = self._fire_bash("ls")
        self.assertIn("## Work Log", content)
        self.assertEqual(len(self._auto_lines(content)), 1)

    def test_single_trailing_newline(self):
        self._write_task("# T\n\n## Work Log\n- start\n")
        content = self._fire_bash("ls")
        self.assertTrue(content.endswith("\n"))
        self.assertFalse(content.endswith("\n\n"))


class TestGateAndThrottle(WorklogHookBase):
    def test_subagent_depth_blocks_entry(self):
        # Criterion #3: with a working depth counter, a subagent tool call
        # (depth > 0) must NOT be auto-logged.
        self._write_task("# T\n\n## Work Log\n- start\n")
        self._set_depth(1)
        content = self._fire_bash("if a\nthen b\nfi")
        self.assertEqual(self._auto_lines(content), [])

    def test_counter_below_threshold_no_entry(self):
        self._write_task("# T\n\n## Work Log\n- start\n")
        content = self._fire_bash("ls", counter=3)
        self.assertEqual(self._auto_lines(content), [])
        self.assertEqual(
            (self.temp_dir / ".claude" / "state" / "worklog-auto-counter.txt")
            .read_text().strip(),
            "4",
        )

    def test_non_implementation_step_no_entry(self):
        self._write_current_task("investigation")
        self._write_task("# T\n\n## Work Log\n- start\n")
        content = self._fire_bash("ls")
        self.assertEqual(self._auto_lines(content), [])


if __name__ == "__main__":
    main()
