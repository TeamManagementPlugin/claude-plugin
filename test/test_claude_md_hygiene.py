#!/usr/bin/env python3
"""Tests for the CLAUDE.md hygiene gate.

CLAUDE.md files are re-read into every session, so they hold only current,
load-bearing facts. The gate blocks the documentation step when a task ADDS
history, task names, or review chatter to one, or grows one past its budget.

Run with: python3 -m pytest test/test_claude_md_hygiene.py -v
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main

TEST_DIR = Path(__file__).parent
REPO = TEST_DIR.parent
sys.path.insert(0, str(REPO / "plugin" / "hooks"))

from claude_md_hygiene import collect_task_names, count_content_lines, find_hygiene_hits, parse_added_lines  # noqa: E402

TASK = "m-demo-task"
REPO_CLAUDE_MDS = [
    REPO / "CLAUDE.md",
    REPO / "plugin" / "CLAUDE.md",
    REPO / "plugin" / "hooks" / "CLAUDE.md",
    REPO / "plugin" / "mcp" / "CLAUDE.md",
    REPO / "plugin" / "agents" / "CLAUDE.md",
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdin=subprocess.DEVNULL, capture_output=True)


def _rules(lines, task_names=()):
    return [hit.rule for hit in find_hygiene_hits(list(enumerate(lines, 1)), set(task_names))]


class TestRules(TestCase):
    def test_task_name_rule(self):
        self.assertEqual(_rules(["Fixed by m-fix-foo-bar."], ["m-fix-foo-bar"]), ["task-name"])

    def test_task_name_needs_a_boundary(self):
        self.assertEqual(_rules(["see m-fix-foo-bar-baz"], ["m-fix-foo-bar"]), [])
        self.assertEqual(_rules(["xm-fix-foo-bar"], ["m-fix-foo-bar"]), [])

    def test_history_wording(self):
        for line in (
            "The flag was removed.",
            "Previously the hook wrote state.",
            "It is no longer read.",
            "Renamed from sessions-config.json.",
            "Retired in the plugin conversion.",
            "Fixed in the last release.",
            "Formerly a boolean.",
            "It used to be a boolean.",
        ):
            self.assertEqual(_rules([line]), ["history"], line)

    def test_present_tense_prose_passes(self):
        for line in (
            "The key used to sign requests lives in the token file.",
            "Uses a fixed-size buffer.",
            "Added lines are inspected; removed lines never block.",
            "Rounds the value to two places.",
        ):
            self.assertEqual(_rules([line]), [], line)

    def test_review_lexicon(self):
        for line in (
            "Found in review round 3.",
            "Codex W2 flagged the race.",
            "Verified on codex-cli 0.145.0 and 0.147.0.",
        ):
            self.assertEqual(_rules([line]), ["review"], line)

    def test_parse_added_lines(self):
        patch = (
            "diff --git a/CLAUDE.md b/CLAUDE.md\n--- a/CLAUDE.md\n+++ b/CLAUDE.md\n"
            "@@ -1 +1 @@\n-old\n+new\n"
            "@@ -10,0 +11,2 @@\n++++ looks like a header\n+b\n"
        )
        self.assertEqual(parse_added_lines(patch),
                         [(1, "new"), (11, "+++ looks like a header"), (12, "b")])

    def test_count_content_lines_skips_managed_block(self):
        text = (
            "# Project\nline\n"
            "<!-- team-management:begin (managed by /team-management:init; do not edit inside) -->\n"
            "@CLAUDE.tm.md\n@CLAUDE.tm.custom.md\n"
            "<!-- team-management:end -->\n"
        )
        self.assertEqual(count_content_lines(text), 2)
        self.assertEqual(count_content_lines("a\nb\n"), 2)

    def test_marker_ambiguity_never_hides_content(self):
        # Only marker lines and @-imports inside a block are excluded; every
        # other non-blank line counts, whatever the markers around it look like.
        begin = "<!-- team-management:begin (managed by /team-management:init; do not edit inside) -->"
        end = "<!-- team-management:end -->"
        # Unterminated block: its content still counts.
        self.assertEqual(count_content_lines(f"a\n{begin}\nb\n\nc\n"), 3)
        # Stray begin before the real block: the lines between them count.
        stray = "<!-- team-management:begin note -->"
        body = "".join(f"- line {i}\n" for i in range(5))
        self.assertEqual(count_content_lines(f"# P\n{stray}\n{body}\n{begin}\n@CLAUDE.tm.md\n{end}\n"), 6)
        # Nested begin inside an open block.
        self.assertEqual(count_content_lines(f"{begin}\nx\ny\n{begin}\n@a\n{end}\n"), 2)
        # An @-line outside any block is ordinary content.
        self.assertEqual(count_content_lines("@foo\n"), 1)

    def test_collect_task_names(self):
        root = Path(tempfile.mkdtemp())
        try:
            tasks = root / "team-management" / "tasks"
            (tasks / "done").mkdir(parents=True)
            (tasks / "h-dir-task").mkdir()
            (tasks / "TEMPLATE.md").write_text("x", encoding="utf-8")
            (tasks / "m-open.md").write_text("x", encoding="utf-8")
            (tasks / "done" / "l-closed.md").write_text("x", encoding="utf-8")
            (tasks / "h-dir-task" / "README.md").write_text("x", encoding="utf-8")
            self.assertEqual(collect_task_names(root), {"m-open", "l-closed", "h-dir-task"})
            shutil.rmtree(tasks)
            self.assertEqual(collect_task_names(root), set())
        finally:
            shutil.rmtree(root, ignore_errors=True)


class GateTestBase(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        state = self.root / ".claude" / "state"
        (state / "protocol-logs").mkdir(parents=True)
        (state / "current_task.json").write_text(json.dumps({
            "task": TASK, "branch": f"fix/{TASK}", "services": [], "updated": "2026-10-13",
        }), encoding="utf-8")
        tasks = self.root / "team-management" / "tasks"
        (tasks / "done").mkdir(parents=True)
        (tasks / f"{TASK}.md").write_text("# task\n", encoding="utf-8")
        (tasks / "done" / "m-old-task.md").write_text("# old\n", encoding="utf-8")

        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@example.com")
        _git(self.root, "config", "user.name", "t")
        _git(self.root, "config", "commit.gpgsign", "false")
        (self.root / "pkg").mkdir()
        self.doc = self.root / "pkg" / "CLAUDE.md"
        self.doc.write_text("# pkg\n\nPreviously this was legacy (m-old-task).\n", encoding="utf-8")
        _git(self.root, "add", "pkg/CLAUDE.md")
        _git(self.root, "commit", "-q", "-m", "init")

        import shared_state
        self._saved = {n: getattr(shared_state, n) for n in (
            "PROJECT_ROOT", "STATE_DIR", "TASK_STATE_FILE", "PROTOCOL_LOGS_DIR")}
        shared_state.PROJECT_ROOT = self.root
        shared_state.STATE_DIR = state
        shared_state.TASK_STATE_FILE = state / "current_task.json"
        shared_state.PROTOCOL_LOGS_DIR = state / "protocol-logs"
        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(self.root)

    def tearDown(self):
        import shared_state
        for name, value in self._saved.items():
            setattr(shared_state, name, value)
        shutil.rmtree(self.root, ignore_errors=True)

    def _config(self, documentation):
        path = self.root / "team-management" / "config.json"
        path.write_text(json.dumps({"documentation": documentation}), encoding="utf-8")

    def _append(self, text):
        with self.doc.open("a", encoding="utf-8") as f:
            f.write(text)

    def _check(self):
        return self.engine._func_check_claude_md_hygiene()


class TestGate(GateTestBase):
    def test_clean_addition_passes(self):
        self._append("- `pkg/app.py` - entry point\n")
        self.assertTrue(self._check()["success"])

    def test_pre_existing_lines_never_block(self):
        self.assertTrue(self._check()["success"])

    def test_added_history_blocks_with_location(self):
        self._append("The cache was removed.\n")
        result = self._check()
        self.assertFalse(result["success"])
        self.assertIn("pkg/CLAUDE.md:4", result["error"])
        self.assertIn("history", result["error"])

    def test_added_task_name_blocks(self):
        self._append(f"- detail ({TASK})\n")
        result = self._check()
        self.assertFalse(result["success"])
        self.assertIn("task-name", result["error"])

    def test_untracked_claude_md_counts_as_added(self):
        (self.root / "new").mkdir()
        (self.root / "new" / "CLAUDE.md").write_text("Codex W2 found this.\n", encoding="utf-8")
        result = self._check()
        self.assertFalse(result["success"])
        self.assertIn("new/CLAUDE.md:1", result["error"])

    def test_staged_new_claude_md_is_judged(self):
        # A new CLAUDE.md that is `git add`-ed shows up in `git diff HEAD`, not in
        # the untracked list, and has no HEAD version to compare against.
        self._config({"claude_md_max_lines": 2})
        (self.root / "new").mkdir()
        (self.root / "new" / "CLAUDE.md").write_text("The cache was removed.\n- b\n- c\n", encoding="utf-8")
        _git(self.root, "add", "new/CLAUDE.md")
        result = self._check()
        self.assertFalse(result["success"])
        self.assertNotIn("warning", result)
        self.assertIn("new/CLAUDE.md:1 — history", result["error"])
        self.assertIn("new/CLAUDE.md — budget", result["error"])

    def test_other_files_are_ignored(self):
        (self.root / "pkg" / "notes.md").write_text("This was removed.\n", encoding="utf-8")
        (self.root / "CLAUDE.tm.md").write_text("This was removed.\n", encoding="utf-8")
        self.assertTrue(self._check()["success"])

    def test_growth_past_budget_blocks(self):
        self._config({"claude_md_max_lines": 5})
        self._append("- a\n- b\n- c\n- d\n")  # 2 + 4 = 6 non-blank lines
        result = self._check()
        self.assertFalse(result["success"])
        self.assertIn("budget", result["error"])

    def test_shrinking_an_oversized_file_passes(self):
        self.doc.write_text("".join(f"- line {i}\n" for i in range(10)), encoding="utf-8")
        _git(self.root, "commit", "-q", "-am", "big")
        self._config({"claude_md_max_lines": 5})
        self.doc.write_text("".join(f"- line {i}\n" for i in range(8)), encoding="utf-8")
        self.assertTrue(self._check()["success"])

    def test_framework_managed_block_is_not_growth(self):
        # Session start wires the managed @-import block into an existing
        # CLAUDE.md. Those lines are the framework's, not the task's.
        self.doc.write_text("".join(f"- line {i}\n" for i in range(10)), encoding="utf-8")
        _git(self.root, "commit", "-q", "-am", "big")
        self._config({"claude_md_max_lines": 5})
        self._append(
            "\n<!-- team-management:begin (managed by /team-management:init; do not edit inside) -->\n"
            "@CLAUDE.tm.md\n@CLAUDE.tm.custom.md\n<!-- team-management:end -->\n"
        )
        self.assertTrue(self._check()["success"])
        self._append("- a task line\n")
        result = self._check()
        self.assertFalse(result["success"])
        self.assertIn("budget", result["error"])

    def test_toggle_disables_gate(self):
        self._config({"claude_md_hygiene": False})
        self._append("The cache was removed.\n")
        self.assertTrue(self._check()["success"])

    def test_missing_tasks_dir_skips_task_name_rule(self):
        shutil.rmtree(self.root / "team-management" / "tasks")
        self._append(f"- detail ({TASK})\n")
        self.assertTrue(self._check()["success"])

    def test_git_failure_passes_with_warning(self):
        # A quality gate must never strand the documentation step.
        shutil.rmtree(self.root / ".git")
        result = self._check()
        self.assertTrue(result["success"])
        self.assertIn("warning", result)

    def test_non_utf8_diff_does_not_crash(self):
        with self.doc.open("ab") as f:
            f.write(b"caf\xe9 was removed\n")
        result = self._check()
        self.assertFalse(result["success"])
        self.assertIn("history", result["error"])

    def test_registered_with_metadata(self):
        funcs = self.engine.get_available_funcs()
        self.assertIn("check_claude_md_hygiene", {f["name"] for f in funcs["funcs"]})
        self.assertFalse(funcs.get("discrepancies"))


class TestWiringAndConfig(TestCase):
    def _step(self, protocol, name):
        data = json.loads((REPO / "plugin" / "protocol-configs" / f"{protocol}.json").read_text(encoding="utf-8"))
        return next(s for s in data["steps"] if s["name"] == name)

    def test_documentation_step_blocks_on_hygiene(self):
        step = self._step("task", "documentation")
        self.assertIn("check_claude_md_hygiene", step["post_funcs"])
        self.assertIs(step["post_funcs_stop_on_failure"], True)

    def test_optimize_synthesis_blocks_on_hygiene(self):
        for protocol in ("optimize", "optimize-unattended"):
            step = self._step(protocol, "synthesis")
            self.assertEqual(step["post_funcs"], ["policy_compliance_audit", "check_claude_md_hygiene"])
            self.assertIs(step["post_funcs_stop_on_failure"], True)
            self.assertNotIn("This step never blocks advance", step["end"])

    def test_config_keys(self):
        sys.path.insert(0, str(REPO / "plugin" / "mcp"))
        from tools.config import _CONFIG_SCHEMA
        self.assertIn("documentation.claude_md_hygiene", _CONFIG_SCHEMA)
        self.assertIn("documentation.claude_md_max_lines", _CONFIG_SCHEMA)
        template = json.loads((REPO / "plugin" / "templates" / "config.template.json").read_text(encoding="utf-8"))
        self.assertEqual(template["documentation"], {"claude_md_hygiene": True, "claude_md_max_lines": 300})


class TestContentRules(TestCase):
    """SC-11: both CLAUDE.md authors carry the content test."""

    CONTENT_TEST = "would a developer starting a new task tomorrow need this"

    def test_documentation_step_states_content_test(self):
        data = json.loads((REPO / "plugin" / "protocol-configs" / "task.json").read_text(encoding="utf-8"))
        step = next(s for s in data["steps"] if s["name"] == "documentation")
        self.assertIn(self.CONTENT_TEST, step["start"].lower())

    def test_service_documentation_agent(self):
        text = (REPO / "plugin" / "agents" / "service-documentation.md").read_text(encoding="utf-8")
        self.assertIn(self.CONTENT_TEST, text.lower())
        self.assertNotIn("Recent changes made", text)
        self.assertNotIn("line numbers", text.lower())
        self.assertNotIn("line ranges", text.lower())


class TestRepoClaudeMdFiles(TestCase):
    """SC-13: the repository's own CLAUDE.md files obey the rules in full."""

    def test_each_file_within_budget(self):
        for path in REPO_CLAUDE_MDS:
            count = len(path.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(count, 300, f"{path.relative_to(REPO)}: {count} lines")

    def test_full_content_passes_rules(self):
        task_names = collect_task_names(REPO)
        for path in REPO_CLAUDE_MDS:
            lines = list(enumerate(path.read_text(encoding="utf-8").splitlines(), 1))
            hits = find_hygiene_hits(lines, task_names)
            self.assertEqual(
                [], [f"{h.line}: {h.rule}: {h.text[:80]}" for h in hits],
                f"{path.relative_to(REPO)}",
            )

    def test_managed_block_preserved(self):
        text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("<!-- team-management:begin", text)
        self.assertTrue(text.rstrip().endswith("<!-- team-management:end -->"))


if __name__ == "__main__":
    main()
