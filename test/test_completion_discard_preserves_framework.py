#!/usr/bin/env python3
"""Defect 1 (h-fix-discard-clean-and-windows-transcript): the completion
`discard` path must NOT delete the framework's own gitignored state.

`_completion_discard` runs `git clean -fdx`, which (with -x) also removes
gitignored files. The framework gitignores `team-management/config.json`
(config flow) and all of `.claude/` (session-start), so a plain `-fdx`
irrecoverably wipes config + every `.claude/state/*-mappings.json` (task<->issue
links) + logs. It also removes any UNTRACKED sibling task under
`team-management/tasks/` or custom protocol under
`team-management/protocol-configs/custom/`.

The fix adds `-e team-management/ -e .claude/` so the whole framework working
tree survives a discard while ordinary untracked/ignored junk is still removed.

Red-green: against the pre-fix `git clean -fdx` the three framework files are
deleted and these `assertTrue(...exists())` checks fail; with the excludes they
pass.

Run:
  python3 -m pytest test/test_completion_discard_preserves_framework.py -v
"""
import subprocess
import sys
from pathlib import Path

# Reuse the temp-engine harness (temp project root + shared_state monkeypatch).
TEST_DIR = Path(__file__).parent
sys.path.insert(0, str(TEST_DIR))
from test_completion_workflow import _TempEngineBase  # noqa: E402


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )


class TestDiscardPreservesFramework(_TempEngineBase):
    """`_completion_discard` in a REAL temp git repo must preserve framework
    state (config, mappings, sibling tasks) while removing ordinary junk.
    """

    def setUp(self):
        super().setUp()
        repo = self.temp_dir
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "t@t")
        _git(repo, "config", "user.name", "t")

        # The framework gitignores its own state. Commit that .gitignore so the
        # `-x` in `git clean -fdx` would otherwise delete the ignored files.
        (repo / ".gitignore").write_text(
            "team-management/config.json\n.claude/\n*.log\n", encoding="utf-8"
        )
        (repo / "src").mkdir()
        (repo / "src" / "app.py").write_text("code\n", encoding="utf-8")
        _git(repo, "add", ".gitignore", "src/app.py")
        _git(repo, "commit", "-qm", "init")
        _git(repo, "branch", "-M", "main")          # canonical default branch
        _git(repo, "checkout", "-q", "-b", "feature/foo")

        # --- framework state that MUST survive a discard ---
        # gitignored config (top-level under team-management/)
        (repo / "team-management" / "config.json").write_text(
            '{"developer_name": "Max"}\n', encoding="utf-8"
        )
        # gitignored task<->issue mapping (under .claude/state/)
        (repo / ".claude" / "state" / "github-mappings.json").write_text(
            '{"m-other": {"issue_id": 7}}\n', encoding="utf-8"
        )
        # UNTRACKED (not ignored) sibling task under team-management/tasks/
        (repo / "team-management" / "tasks" / "m-sibling.md").write_text(
            "# other in-flight task\n", encoding="utf-8"
        )

        # --- ordinary junk that SHOULD be removed by discard ---
        (repo / "scratch.tmp").write_text("junk\n", encoding="utf-8")   # untracked
        (repo / "build").mkdir()
        (repo / "build" / "out.o").write_text("artifact\n", encoding="utf-8")
        # gitignored junk OUTSIDE the preserved dirs — proves -x still removes
        # ignored artifacts (the excludes are scoped, not a blanket -x disable).
        (repo / "debug.log").write_text("ignored junk\n", encoding="utf-8")

        self._write_config(issue_tracking={"provider": "disabled"})
        self._set_task_state("m-foo", "feature/foo")

    def _discard(self):
        return self.engine._completion_discard({
            "completion_option": "discard",
            "discard_confirmation": "discard",
            "discard_confirmed_dry_run": True,
        })

    def test_discard_preserves_config_mappings_and_sibling_task(self):
        result = self._discard()
        self.assertTrue(result.get("success"), f"discard failed: {result}")

        repo = self.temp_dir
        # Framework state survives.
        self.assertTrue((repo / "team-management" / "config.json").exists(),
                        "config.json must survive discard")
        self.assertTrue((repo / ".claude" / "state" / "github-mappings.json").exists(),
                        "issue mappings must survive discard")
        self.assertTrue((repo / "team-management" / "tasks" / "m-sibling.md").exists(),
                        "untracked sibling task must survive discard")

    def test_discard_still_removes_ordinary_junk(self):
        self._discard()
        repo = self.temp_dir
        self.assertFalse((repo / "scratch.tmp").exists(),
                         "discard must still remove ordinary untracked junk")
        self.assertFalse((repo / "build").exists(),
                         "discard must still remove untracked junk dirs")
        self.assertFalse((repo / "debug.log").exists(),
                         "discard must still remove gitignored junk outside the preserved dirs")

    def test_discard_ends_on_default_branch_and_deletes_feature(self):
        result = self._discard()
        self.assertTrue(result.get("success"), f"discard failed: {result}")
        repo = self.temp_dir
        current = _git(repo, "branch", "--show-current").stdout.strip()
        self.assertEqual(current, "main")
        branches = _git(repo, "branch", "--format=%(refname:short)").stdout.split()
        self.assertNotIn("feature/foo", branches)


if __name__ == "__main__":
    import unittest
    unittest.main()
