#!/usr/bin/env python3
"""Tests for the review freshness gates: the working-tree hash the engine stamps
on REVIEW_ROUND / SPEC_REVIEW notes, and the two post_funcs that compare it.

Run with: python3 -m pytest test/test_review_freshness_gates.py -v
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT / "plugin" / "hooks"))

TASK = "m-demo-task"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdin=subprocess.DEVNULL, capture_output=True)


class FreshnessTestBase(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        state = self.root / ".claude" / "state"
        (state / "protocol-logs").mkdir(parents=True)
        self._write_json(state / "daic-mode.json", {"mode": "implementation"})
        self._write_json(state / "current_task.json", {
            "task": TASK, "branch": f"fix/{TASK}", "services": [], "updated": "2026-10-13",
            "protocol": {"name": "task", "current_step": 2, "step_name": "code-review"},
        })
        self._write_log([])

        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.email", "t@example.com")
        _git(self.root, "config", "user.name", "t")
        _git(self.root, "config", "commit.gpgsign", "false")
        (self.root / "app.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "team-management" / "tasks").mkdir(parents=True)
        (self.root / "team-management" / "tasks" / f"{TASK}.md").write_text("# task\n", encoding="utf-8")
        _git(self.root, "add", "app.py")
        _git(self.root, "commit", "-q", "-m", "init")

        import shared_state
        self._saved = {name: getattr(shared_state, name) for name in (
            "PROJECT_ROOT", "STATE_DIR", "TASK_STATE_FILE", "DAIC_STATE_FILE", "PROTOCOL_LOGS_DIR")}
        shared_state.PROJECT_ROOT = self.root
        shared_state.STATE_DIR = state
        shared_state.TASK_STATE_FILE = state / "current_task.json"
        shared_state.DAIC_STATE_FILE = state / "daic-mode.json"
        shared_state.PROTOCOL_LOGS_DIR = state / "protocol-logs"

        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(self.root)

    def tearDown(self):
        import shared_state
        for name, value in self._saved.items():
            setattr(shared_state, name, value)
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_json(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @property
    def log_path(self):
        return self.root / ".claude" / "state" / "protocol-logs" / f"{TASK}.json"

    def _write_log(self, notes):
        self._write_json(self.log_path, {
            "protocol": "task", "started_at": "2026-10-13T00:00:00+00:00",
            "steps": [], "gotos": [], "notes": notes,
        })

    def _notes(self):
        return json.loads(self.log_path.read_text(encoding="utf-8"))["notes"]

    def _edit_code(self, text="x = 2\n"):
        (self.root / "app.py").write_text(text, encoding="utf-8")


class TestTreeHash(FreshnessTestBase):
    def test_same_tree_same_hash(self):
        self.assertEqual(self.engine._compute_review_tree_hash(),
                         self.engine._compute_review_tree_hash())

    def test_framework_paths_do_not_change_hash(self):
        before = self.engine._compute_review_tree_hash()
        (self.root / "team-management" / "tasks" / f"{TASK}.md").write_text("# task\nwork log\n", encoding="utf-8")
        (self.root / ".agents").mkdir()
        (self.root / ".agents" / "hooks.json").write_text("{}", encoding="utf-8")
        self.assertEqual(before, self.engine._compute_review_tree_hash())

    def test_tracked_edit_changes_hash(self):
        before = self.engine._compute_review_tree_hash()
        self._edit_code()
        self.assertNotEqual(before, self.engine._compute_review_tree_hash())

    def test_staged_edit_changes_hash(self):
        before = self.engine._compute_review_tree_hash()
        self._edit_code()
        _git(self.root, "add", "app.py")
        self.assertNotEqual(before, self.engine._compute_review_tree_hash())

    def test_new_untracked_file_changes_hash(self):
        before = self.engine._compute_review_tree_hash()
        (self.root / "new.py").write_text("y = 1\n", encoding="utf-8")
        after_add = self.engine._compute_review_tree_hash()
        self.assertNotEqual(before, after_add)
        (self.root / "new.py").write_text("y = 2\n", encoding="utf-8")
        self.assertNotEqual(after_add, self.engine._compute_review_tree_hash())

    def test_untracked_records_are_framed(self):
        # Same bytes split differently across two files must not collide.
        (self.root / "a").write_text("12", encoding="utf-8")
        (self.root / "b").write_text("3", encoding="utf-8")
        first = self.engine._compute_review_tree_hash()
        (self.root / "a").write_text("1", encoding="utf-8")
        (self.root / "b").write_text("23", encoding="utf-8")
        self.assertNotEqual(first, self.engine._compute_review_tree_hash())

    def test_commit_changes_hash_even_with_empty_diff(self):
        self._edit_code()
        dirty = self.engine._compute_review_tree_hash()
        _git(self.root, "commit", "-q", "-am", "edit")
        self.assertNotEqual(dirty, self.engine._compute_review_tree_hash())

    def test_repo_without_head_raises(self):
        shutil.rmtree(self.root / ".git")
        _git(self.root, "init", "-q")
        with self.assertRaises(RuntimeError):
            self.engine._compute_review_tree_hash()


class TestSaveNoteStamping(FreshnessTestBase):
    def test_review_notes_are_stamped(self):
        expected = self.engine._compute_review_tree_hash()
        for note in ("SPEC_REVIEW: PASSED", "  SPEC_REVIEW: PASSED  ", "REVIEW_ROUND: 1 — 0 critical, 0 warnings"):
            entry = self.engine.save_note(note)["note"]
            self.assertEqual(entry["tree_hash"], expected, note)

    def test_other_notes_are_not_stamped(self):
        for note in ("SPEC_UNVERIFIABLE: none", "SPEC_REVIEW: PASSED-ish", "review round 1"):
            entry = self.engine.save_note(note)["note"]
            self.assertNotIn("tree_hash", entry, note)

    def test_hash_failure_is_recorded_not_raised(self):
        def boom():
            raise RuntimeError("git exploded")
        self.engine._compute_review_tree_hash = boom
        result = self.engine.save_note("REVIEW_ROUND: 1")
        self.assertTrue(result["success"])
        self.assertIsNone(result["note"]["tree_hash"])
        self.assertIn("git exploded", result["note"]["tree_hash_error"])


class TestVerifyReviewRound(FreshnessTestBase):
    def test_blocks_without_round_note(self):
        result = self.engine._func_verify_review_round()
        self.assertFalse(result["success"])
        self.assertIn("REVIEW_ROUND", result["error"])

    def test_passes_on_unchanged_tree(self):
        self.engine.save_note("REVIEW_ROUND: 1 — 0 critical, 0 warnings")
        self.assertTrue(self.engine._func_verify_review_round()["success"])

    def test_work_log_edit_keeps_round_valid(self):
        self.engine.save_note("REVIEW_ROUND: 1")
        (self.root / "team-management" / "tasks" / f"{TASK}.md").write_text("# task\n# Code Review: x\n", encoding="utf-8")
        self.assertTrue(self.engine._func_verify_review_round()["success"])

    def test_code_edit_after_round_blocks(self):
        self.engine.save_note("REVIEW_ROUND: 1")
        self._edit_code()
        result = self.engine._func_verify_review_round()
        self.assertFalse(result["success"])
        self.assertIn("changed", result["error"])

    def test_latest_round_wins(self):
        self.engine.save_note("REVIEW_ROUND: 1")
        self._edit_code()
        self.engine.save_note("REVIEW_ROUND: 2")
        self.assertTrue(self.engine._func_verify_review_round()["success"])

    def test_null_hash_blocks(self):
        self._write_log([{"at": "2026-10-13T01:00:00+00:00", "step": "code-review",
                          "note": "REVIEW_ROUND: 1", "tree_hash": None, "tree_hash_error": "x"}])
        self.assertFalse(self.engine._func_verify_review_round()["success"])

    def test_unhashed_round_note_blocks(self):
        self._write_log([{"at": "2026-10-13T01:00:00+00:00", "step": "code-review", "note": "REVIEW_ROUND: 1"}])
        self.assertFalse(self.engine._func_verify_review_round()["success"])

    def test_current_hash_failure_blocks(self):
        self.engine.save_note("REVIEW_ROUND: 1")

        def boom():
            raise RuntimeError("git exploded")
        self.engine._compute_review_tree_hash = boom
        result = self.engine._func_verify_review_round()
        self.assertFalse(result["success"])
        self.assertIn("git exploded", result["error"])

    def test_repo_without_commits_names_the_remedy(self):
        shutil.rmtree(self.root / ".git")
        _git(self.root, "init", "-q")
        self.engine.save_note("REVIEW_ROUND: 1")
        result = self.engine._func_verify_review_round()
        self.assertFalse(result["success"])
        self.assertIn("initial commit", result["error"])

    def test_missing_log_blocks(self):
        self.log_path.unlink()
        self.assertFalse(self.engine._func_verify_review_round()["success"])

    def test_no_active_task_blocks(self):
        self._write_json(self.root / ".claude" / "state" / "current_task.json",
                         {"task": None, "branch": None, "services": [], "updated": "2026-10-13"})
        self.assertFalse(self.engine._func_verify_review_round()["success"])

    def test_registered_with_metadata(self):
        funcs = self.engine.get_available_funcs()
        names = {f["name"] for f in funcs["funcs"]}
        self.assertIn("verify_review_round", names)
        self.assertFalse(funcs.get("discrepancies"))


class TestSpecSentinelHash(FreshnessTestBase):
    def test_fresh_sentinel_passes(self):
        self.engine.save_note("SPEC_REVIEW: PASSED")
        self.engine.save_note("SPEC_DEFERRED: none")
        self.assertTrue(self.engine._func_require_spec_review_passed()["success"])

    def test_code_edit_after_sentinel_blocks(self):
        self.engine.save_note("SPEC_REVIEW: PASSED")
        self._edit_code()
        result = self.engine._func_require_spec_review_passed()
        self.assertFalse(result["success"])
        self.assertIn("STALE", result["error"])

    def test_resaved_sentinel_passes(self):
        self.engine.save_note("SPEC_REVIEW: PASSED")
        self._edit_code()
        self.engine.save_note("SPEC_REVIEW: PASSED")
        self.engine.save_note("SPEC_DEFERRED: none")
        self.assertTrue(self.engine._func_require_spec_review_passed()["success"])

    def test_null_hash_sentinel_blocks(self):
        self._write_log([{"at": "2026-10-13T01:00:00+00:00", "step": "code-review",
                          "note": "SPEC_REVIEW: PASSED", "tree_hash": None, "tree_hash_error": "x"}])
        self.assertFalse(self.engine._func_require_spec_review_passed()["success"])

    def test_legacy_sentinel_without_hash_key_uses_goto_check(self):
        self._write_log([{"at": "2026-10-13T01:00:00+00:00", "step": "code-review",
                          "note": "SPEC_REVIEW: PASSED"},
                         {"at": "2026-10-13T01:00:01+00:00", "step": "code-review",
                          "note": "SPEC_DEFERRED: none"}])
        self._edit_code()  # invisible to the legacy check — only gotos count there
        self.assertTrue(self.engine._func_require_spec_review_passed()["success"])


if __name__ == "__main__":
    main()
