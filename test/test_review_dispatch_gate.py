#!/usr/bin/env python3
"""Tests for the review dispatch classifier (CR-2 minimal gate).

m-gstack-agent-roster added `classify_review_dispatch` — a deterministic diff
classifier pre_func on the code-review step that decides which chain
specialists (testing-specialist, code-cleanliness, red-team) the orchestrator
dispatches, and appends per-evaluation telemetry to the project-scoped
.claude/state/review-dispatch-log.jsonl.

Run with: python3 -m pytest test/test_review_dispatch_gate.py -v
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

from protocol_engine import (  # noqa: E402
    ProtocolEngine,
    _REVIEW_DISPATCH_AGENTS,
    _REVIEW_DISPATCH_LOG_NAME,
)

AGENTS = list(_REVIEW_DISPATCH_AGENTS)
TASK_STATE = {"task": "m-test-roster", "branch": "feature/test-roster", "services": []}
PROTOCOL_STATE = {"name": "task", "current_step": 2, "step_name": "code-review"}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


class ClassifierTestBase(TestCase):
    """tmp git repo + engine, with global state readers patched to fixtures."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        _git(self.temp_dir, "init", "-q")
        _git(self.temp_dir, "config", "user.email", "test@test")
        _git(self.temp_dir, "config", "user.name", "Test")
        self.src = self.temp_dir / "src" / "code.py"
        self.src.parent.mkdir()
        self.src.write_text("line0\n", encoding="utf-8")
        _git(self.temp_dir, "add", "-A")
        _git(self.temp_dir, "commit", "-q", "-m", "base")

        self.engine = ProtocolEngine(self.temp_dir)
        self._patches = [
            patch("protocol_engine.get_task_state", return_value=dict(TASK_STATE)),
            patch("protocol_engine.get_protocol_state", return_value=dict(PROTOCOL_STATE)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def grow_tracked(self, n_lines: int, path: Path = None) -> None:
        """Append n_lines to a tracked file → numstat added == n_lines."""
        target = path or self.src
        with target.open("a", encoding="utf-8") as fh:
            for i in range(n_lines):
                fh.write(f"added {i}\n")

    def write_config(self, code_review) -> None:
        cfg_dir = self.temp_dir / "team-management"
        cfg_dir.mkdir(exist_ok=True)
        (cfg_dir / "config.json").write_text(
            json.dumps({"code_review": code_review}), encoding="utf-8")

    def classify(self):
        return self.engine._func_classify_review_dispatch()

    def read_log(self):
        log = self.temp_dir / ".claude" / "state" / _REVIEW_DISPATCH_LOG_NAME
        if not log.exists():
            return []
        return [json.loads(line) for line in
                log.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestThresholdBoundaries(ClassifierTestBase):
    def test_49_lines_below_specialist_threshold(self):
        self.grow_tracked(49)
        result = self.classify()
        self.assertTrue(result["success"])
        self.assertEqual(result["diff_lines"], 49)
        self.assertFalse(result["dispatch"]["testing-specialist"]["fire"])
        self.assertFalse(result["dispatch"]["code-cleanliness"]["fire"])
        self.assertEqual(result["dispatch"]["testing-specialist"]["reason"],
                         "below-threshold (49 < 50)")

    def test_50_lines_fires_specialists_not_red_team(self):
        self.grow_tracked(50)
        result = self.classify()
        self.assertEqual(result["diff_lines"], 50)
        self.assertTrue(result["dispatch"]["testing-specialist"]["fire"])
        self.assertTrue(result["dispatch"]["code-cleanliness"]["fire"])
        self.assertFalse(result["dispatch"]["red-team"]["fire"])

    def test_200_lines_red_team_still_skipped(self):
        self.grow_tracked(200)
        result = self.classify()
        self.assertEqual(result["diff_lines"], 200)
        self.assertFalse(result["dispatch"]["red-team"]["fire"])
        self.assertEqual(result["dispatch"]["red-team"]["reason"],
                         "below-threshold (200 <= 200)")

    def test_201_lines_fires_red_team(self):
        self.grow_tracked(201)
        result = self.classify()
        self.assertTrue(result["dispatch"]["red-team"]["fire"])
        self.assertEqual(result["dispatch"]["red-team"]["reason"], "fired (201 lines)")

    def test_deleted_lines_count(self):
        # 1-line tracked file rewritten to empty → 1 deleted; plus 49 added
        # elsewhere = 50 total → fires.
        other = self.temp_dir / "src" / "other.py"
        other.write_text("x\n", encoding="utf-8")
        _git(self.temp_dir, "add", "-A")
        _git(self.temp_dir, "commit", "-q", "-m", "add other")
        self.src.write_text("", encoding="utf-8")  # uncommitted 1-line deletion
        self.grow_tracked(49, other)
        result = self.classify()
        self.assertEqual(result["diff_lines"], 50)
        self.assertTrue(result["dispatch"]["testing-specialist"]["fire"])


class TestDocsConfigOnly(ClassifierTestBase):
    def test_docs_only_diff_skips_specialists(self):
        readme = self.temp_dir / "README.md"
        readme.write_text("intro\n", encoding="utf-8")
        _git(self.temp_dir, "add", "-A")
        _git(self.temp_dir, "commit", "-q", "-m", "readme")
        self.grow_tracked(60, readme)
        result = self.classify()
        self.assertTrue(result["docs_config_only"])
        self.assertFalse(result["dispatch"]["testing-specialist"]["fire"])
        self.assertEqual(result["dispatch"]["testing-specialist"]["reason"],
                         "docs-config-only")
        self.assertFalse(result["dispatch"]["code-cleanliness"]["fire"])

    def test_red_team_not_docs_gated(self):
        readme = self.temp_dir / "README.md"
        readme.write_text("intro\n", encoding="utf-8")
        _git(self.temp_dir, "add", "-A")
        _git(self.temp_dir, "commit", "-q", "-m", "readme")
        self.grow_tracked(201, readme)
        result = self.classify()
        self.assertTrue(result["docs_config_only"])
        self.assertTrue(result["dispatch"]["red-team"]["fire"])

    def test_docs_dir_counts_as_docs(self):
        doc = self.temp_dir / "docs" / "guide.py"  # under docs/ despite .py
        doc.parent.mkdir()
        doc.write_text("x\n" * 60, encoding="utf-8")
        result = self.classify()
        self.assertTrue(result["docs_config_only"])
        self.assertFalse(result["dispatch"]["testing-specialist"]["fire"])

    def test_mixed_diff_not_docs_only(self):
        readme = self.temp_dir / "README.md"
        readme.write_text("x\n" * 40, encoding="utf-8")
        self.grow_tracked(10)
        result = self.classify()
        self.assertFalse(result["docs_config_only"])
        self.assertTrue(result["dispatch"]["testing-specialist"]["fire"])


class TestConfigGating(ClassifierTestBase):
    def test_disabled_keys_skip_with_reason(self):
        self.write_config({
            "testing_specialist_enabled": False,
            "red_team_enabled": False,
            "cleanliness_enabled": False,
        })
        self.grow_tracked(300)
        result = self.classify()
        for agent in AGENTS:
            self.assertFalse(result["dispatch"][agent]["fire"])
            self.assertEqual(result["dispatch"][agent]["reason"], "disabled-by-config")

    def test_absent_keys_default_enabled(self):
        self.write_config({})
        self.grow_tracked(300)
        result = self.classify()
        for agent in AGENTS:
            self.assertTrue(result["dispatch"][agent]["fire"])

    def test_malformed_config_shapes_default_enabled(self):
        # Root list, code_review: null, code_review: "bad" — flags default
        # true and classification PROCEEDS on diff size (codex plan-review:
        # config trouble must not silently disable the roster).
        self.grow_tracked(300)
        cfg = self.temp_dir / "team-management" / "config.json"
        cfg.parent.mkdir(exist_ok=True)
        for payload in ('["not", "a", "dict"]', '{"code_review": null}',
                        '{"code_review": "bad"}', "{not json"):
            cfg.write_text(payload, encoding="utf-8")
            result = self.classify()
            self.assertTrue(result["success"], payload)
            for agent in AGENTS:
                self.assertTrue(result["dispatch"][agent]["fire"], payload)


class TestEmptyDiffAndErrors(ClassifierTestBase):
    def test_empty_diff_skips_all(self):
        result = self.classify()
        self.assertEqual(result["diff_lines"], 0)
        for agent in AGENTS:
            self.assertFalse(result["dispatch"][agent]["fire"])
            self.assertEqual(result["dispatch"][agent]["reason"], "empty-diff")

    def test_git_failure_degrades_to_classifier_error(self):
        engine = ProtocolEngine(self.temp_dir / "no-such-dir")
        result = engine._func_classify_review_dispatch()
        self.assertTrue(result["success"])
        for agent in AGENTS:
            self.assertFalse(result["dispatch"][agent]["fire"])
            self.assertTrue(
                result["dispatch"][agent]["reason"].startswith("classifier-error:"))

    def test_classifier_error_keeps_disabled_by_config_reason(self):
        # W2 (round-1 review): the enabled map is read before measurement, so
        # a config-disabled agent reads "disabled-by-config" (enabled: false in
        # telemetry) even when git measurement fails.
        plain = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, plain, True)
        cfg_dir = plain / "team-management"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text(
            json.dumps({"code_review": {"red_team_enabled": False}}), encoding="utf-8")
        engine = ProtocolEngine(plain)
        result = engine._func_classify_review_dispatch()
        self.assertTrue(result["success"])
        self.assertEqual(result["dispatch"]["red-team"]["reason"], "disabled-by-config")
        self.assertTrue(result["dispatch"]["testing-specialist"]["reason"]
                        .startswith("classifier-error:"))
        log = plain / ".claude" / "state" / _REVIEW_DISPATCH_LOG_NAME
        by_agent = {r["agent"]: r for r in
                    (json.loads(line) for line in
                     log.read_text(encoding="utf-8").splitlines())}
        self.assertFalse(by_agent["red-team"]["enabled"])
        self.assertTrue(by_agent["testing-specialist"]["enabled"])

    def test_classifier_error_still_appends_telemetry(self):
        # Point at a non-repo dir OUTSIDE the test repo (a nested dir would
        # let git discover the parent .git) that CAN hold the log — the error
        # path must still write 3 records (codex plan-review requirement).
        plain = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, plain, True)
        engine = ProtocolEngine(plain)
        result = engine._func_classify_review_dispatch()
        self.assertTrue(result["success"])
        log = plain / ".claude" / "state" / _REVIEW_DISPATCH_LOG_NAME
        records = [json.loads(line) for line in
                   log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 3)
        for rec in records:
            self.assertFalse(rec["fired"])
            self.assertTrue(rec["reason"].startswith("classifier-error:"))


class TestUntrackedCounting(ClassifierTestBase):
    def test_untracked_lines_counted(self):
        (self.temp_dir / "src" / "new.py").write_text("x\n" * 50, encoding="utf-8")
        result = self.classify()
        self.assertEqual(result["diff_lines"], 50)
        self.assertTrue(result["dispatch"]["testing-specialist"]["fire"])

    def test_untracked_no_trailing_newline_counts_last_line(self):
        # 49 full lines + a final line without trailing newline == 50 lines.
        # An implementation counting b"\n" would undercount to 49 and flip
        # the boundary (codex plan-review pin).
        content = "x\n" * 49 + "last-no-newline"
        (self.temp_dir / "src" / "new.py").write_text(content, encoding="utf-8")
        result = self.classify()
        self.assertEqual(result["diff_lines"], 50)
        self.assertTrue(result["dispatch"]["testing-specialist"]["fire"])

    def test_untracked_binary_counts_zero(self):
        (self.temp_dir / "blob.bin").write_bytes(b"\x00\x01" * 100 + b"\n" * 60)
        result = self.classify()
        self.assertEqual(result["diff_lines"], 0)
        # Binary path still participates in the docs/config-only path set.
        for agent in AGENTS:
            self.assertEqual(result["dispatch"][agent]["reason"], "empty-diff")

    def test_untracked_edge_whitespace_and_non_ascii_names_counted(self):
        # Round-5 codex P2: -z NUL-delimited parsing keeps paths VERBATIM, so
        # names with edge whitespace or non-ASCII (formerly C-quoted) chars
        # are opened and counted instead of silently contributing 0 lines.
        (self.temp_dir / " spaced name .py").write_text("x\n" * 30, encoding="utf-8")
        (self.temp_dir / "файл.py").write_text("x\n" * 20, encoding="utf-8")
        result = self.classify()
        self.assertEqual(result["diff_lines"], 50)
        self.assertTrue(result["dispatch"]["testing-specialist"]["fire"])

    def test_untracked_fifo_skipped_without_blocking(self):
        # Round-3 codex P2: open() on an untracked FIFO blocks until a writer
        # connects — the classifier must skip non-regular files (0 lines) and
        # return promptly, honoring its never-block contract.
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo not available on this platform")
        os.mkfifo(self.temp_dir / "pipe.fifo")
        self.grow_tracked(50)
        result = self.classify()
        self.assertTrue(result["success"])
        self.assertEqual(result["diff_lines"], 50)  # FIFO contributed 0 lines
        self.assertTrue(result["dispatch"]["testing-specialist"]["fire"])

    def test_rename_paths_stay_suffix_checkable(self):
        # --no-renames renders a rename as delete+add with plain paths (no
        # `{old => new}` braces), so the docs/config suffix check works.
        big = self.temp_dir / "src" / "big.py"
        big.write_text("x\n" * 60, encoding="utf-8")
        _git(self.temp_dir, "add", "-A")
        _git(self.temp_dir, "commit", "-q", "-m", "big")
        big.rename(self.temp_dir / "src" / "renamed.py")
        _git(self.temp_dir, "add", "-A")
        result = self.classify()
        self.assertEqual(result["diff_lines"], 120)  # 60 deleted + 60 added
        self.assertFalse(result["docs_config_only"])
        self.assertTrue(result["dispatch"]["testing-specialist"]["fire"])


class TestFrameworkPathExclusion(ClassifierTestBase):
    """The framework's own working tree is bookkeeping ABOUT the change, not the
    change under review — it must not move the dispatch thresholds."""

    def framework_file(self, rel: str, n_lines: int) -> Path:
        """Write an n_lines file at a repo-relative path (parents created)."""
        target = self.temp_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(f"fw {i}\n" for i in range(n_lines)),
                          encoding="utf-8")
        return target

    def test_is_framework_path_classification(self):
        for rel in ("team-management/tasks/x.md", ".claude/state/y.json",
                    ".agents/hooks.json"):
            self.assertTrue(self.engine._is_framework_path(rel), rel)
        # First-component match, so a project file that merely CONTAINS a
        # framework directory name deeper in its path is not caught. The
        # backslash spellings are POSIX FILENAMES containing a backslash, not
        # nested paths — normalizing them would silently drop a project file
        # from the measurement, the one failure direction this gate must avoid.
        for rel in ("wiki/pages/a.md", "docs/b.md", "plugin/hooks/c.py",
                    "src/team-management/d.py", "team-management\\large.py",
                    ".claude\\notes.py"):
            self.assertFalse(self.engine._is_framework_path(rel), rel)

    def test_posix_filename_with_backslash_is_measured(self):
        # End-to-end companion to the predicate case: git -z returns such a name
        # VERBATIM, and it must reach the count instead of being read as a path
        # under team-management/.
        if os.name == "nt":
            self.skipTest("backslash is a path separator on Windows")
        (self.temp_dir / "team-management\\large.py").write_text(
            "x\n" * 300, encoding="utf-8")
        result = self.classify()
        self.assertEqual(result["diff_lines"], 300)
        self.assertEqual(result["framework_lines_excluded"], 0)
        self.assertTrue(result["dispatch"]["red-team"]["fire"])

    def test_tracked_framework_only_diff_measures_empty(self):
        task_md = self.framework_file("team-management/tasks/t.md", 1)
        _git(self.temp_dir, "add", "-A")
        _git(self.temp_dir, "commit", "-q", "-m", "task file")
        self.grow_tracked(300, path=task_md)
        result = self.classify()
        self.assertEqual(result["diff_lines"], 0)
        # The tally is asserted here, not only on the untracked side: this is the
        # loop the observed tm-update-test case flows through (that project tracks
        # team-management/), and dropping the numstat accumulation alone would
        # leave the telemetry claiming 0 while hundreds of lines were dropped.
        self.assertEqual(result["framework_lines_excluded"], 300)
        for agent in AGENTS:
            self.assertFalse(result["dispatch"][agent]["fire"], agent)
            self.assertEqual(result["dispatch"][agent]["reason"], "empty-diff")

    def test_untracked_framework_only_diff_measures_empty(self):
        self.framework_file("team-management/tasks/u.md", 300)
        result = self.classify()
        self.assertEqual(result["diff_lines"], 0)
        for agent in AGENTS:
            self.assertFalse(result["dispatch"][agent]["fire"], agent)
            self.assertEqual(result["dispatch"][agent]["reason"], "empty-diff")

    def test_mixed_diff_counts_project_lines_only(self):
        # 60 project lines + a 300-line task file: the gate must see 60, so the
        # specialists fire and red-team does not. Unfiltered this reads 360 and
        # summons the second echelon on task narrative.
        self.grow_tracked(60)
        self.framework_file("team-management/tasks/u.md", 300)
        result = self.classify()
        self.assertEqual(result["diff_lines"], 60)
        self.assertTrue(result["dispatch"]["testing-specialist"]["fire"])
        self.assertTrue(result["dispatch"]["code-cleanliness"]["fire"])
        self.assertFalse(result["dispatch"]["red-team"]["fire"])
        self.assertEqual(result["dispatch"]["red-team"]["reason"],
                         "below-threshold (60 <= 200)")

    def test_tracked_framework_deletion_is_excluded(self):
        # --numstat contributes DELETED counts too, so removing a committed task
        # file must be excluded as thoroughly as growing one.
        task_md = self.framework_file("team-management/tasks/t.md", 300)
        _git(self.temp_dir, "add", "-A")
        _git(self.temp_dir, "commit", "-q", "-m", "task file")
        task_md.unlink()
        result = self.classify()
        self.assertEqual(result["diff_lines"], 0)
        self.assertEqual(result["framework_lines_excluded"], 300)
        for agent in AGENTS:
            self.assertEqual(result["dispatch"][agent]["reason"], "empty-diff")

    def test_untracked_project_binary_still_enters_the_docs_only_path_set(self):
        # Pins the ORDER the restructuring preserved: a non-framework path is
        # appended BEFORE the FIFO/binary/OSError guards, so a binary counting 0
        # lines still denies the docs/config-only verdict. Moving the append down
        # beside the accumulation would silently flip this.
        notes = self.temp_dir / "NOTES.md"
        notes.write_text("intro\n", encoding="utf-8")
        _git(self.temp_dir, "add", "-A")
        _git(self.temp_dir, "commit", "-q", "-m", "notes")
        self.grow_tracked(60, path=notes)
        (self.temp_dir / "blob.bin").write_bytes(b"\x00\x01" * 100 + b"\n" * 60)
        result = self.classify()
        self.assertEqual(result["diff_lines"], 60)
        self.assertFalse(result["docs_config_only"])

    def test_excluded_fifo_and_binary_contribute_zero(self):
        # The excluded tally shares ONE counting path with the measured one, so
        # a framework FIFO is never opened (never-block contract) and a
        # framework binary counts 0 — neither inflates the excluded total.
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo not available on this platform")
        fw = self.temp_dir / "team-management"
        fw.mkdir(exist_ok=True)
        os.mkfifo(fw / "pipe.fifo")
        (fw / "blob.bin").write_bytes(b"\x00\x01" * 100 + b"\n" * 60)
        self.grow_tracked(50)
        result = self.classify()
        self.assertTrue(result["success"])
        self.assertEqual(result["diff_lines"], 50)
        self.assertEqual(result["framework_lines_excluded"], 0)

    def test_untracked_project_lines_are_not_tallied_as_framework(self):
        # Platform-independent pin for the untracked half of the tally. Without
        # it the only test that catches a project row being double-counted into
        # framework_lines is the backslash case, which skips on Windows.
        (self.temp_dir / "extra.py").write_text("x\n" * 40, encoding="utf-8")
        result = self.classify()
        self.assertEqual(result["diff_lines"], 40)
        self.assertEqual(result["framework_lines_excluded"], 0)

    def test_tracked_framework_non_doc_path_does_not_break_docs_only(self):
        # Symmetric counterpart of the untracked case below: the numstat loop must
        # keep framework rows out of the PATHS set too, not merely out of the line
        # count. Neither tracked test above constrains it — both short-circuit at
        # `empty-diff`, which `decide()` returns before docs_config_only is read.
        notes = self.temp_dir / "NOTES.md"
        notes.write_text("intro\n", encoding="utf-8")
        func = self.framework_file(
            "team-management/protocol-configs/custom/funcs/mine.py", 1)
        _git(self.temp_dir, "add", "-A")
        _git(self.temp_dir, "commit", "-q", "-m", "notes + custom func")
        self.grow_tracked(60, path=notes)
        self.grow_tracked(10, path=func)
        result = self.classify()
        self.assertEqual(result["diff_lines"], 60)
        self.assertEqual(result["framework_lines_excluded"], 10)
        self.assertTrue(result["docs_config_only"])
        for agent in ("testing-specialist", "code-cleanliness"):
            self.assertFalse(result["dispatch"][agent]["fire"], agent)
            self.assertEqual(result["dispatch"][agent]["reason"],
                             "docs-config-only")

    def test_framework_non_doc_path_does_not_break_docs_only(self):
        # An excluded framework .py must not drag a docs-only project change
        # out of the docs/config-only verdict.
        notes = self.temp_dir / "NOTES.md"
        notes.write_text("intro\n", encoding="utf-8")
        _git(self.temp_dir, "add", "-A")
        _git(self.temp_dir, "commit", "-q", "-m", "notes")
        self.grow_tracked(60, path=notes)
        self.framework_file(".claude/hooks/x.py", 40)
        result = self.classify()
        self.assertEqual(result["diff_lines"], 60)
        self.assertTrue(result["docs_config_only"])
        for agent in ("testing-specialist", "code-cleanliness"):
            self.assertFalse(result["dispatch"][agent]["fire"], agent)
            self.assertEqual(result["dispatch"][agent]["reason"],
                             "docs-config-only")


class TestCostLog(ClassifierTestBase):
    def test_three_records_per_evaluation_with_shape(self):
        self.grow_tracked(50)
        self.classify()
        records = self.read_log()
        self.assertEqual(len(records), 3)
        self.assertEqual({r["agent"] for r in records}, set(AGENTS))
        for rec in records:
            for field in ("ts", "task", "protocol", "step", "agent", "diff_lines",
                          "docs_config_only", "enabled", "fired", "reason"):
                self.assertIn(field, rec)
            self.assertEqual(rec["task"], TASK_STATE["task"])
            self.assertEqual(rec["protocol"], PROTOCOL_STATE["name"])
            self.assertEqual(rec["step"], "code-review")
            self.assertEqual(rec["diff_lines"], 50)

    def test_telemetry_records_framework_lines_excluded(self):
        # The log must explain its own numbers: without the excluded count a
        # reader cannot tell a genuinely small change from a large one whose
        # framework bookkeeping was filtered away.
        self.grow_tracked(60)
        fw = self.temp_dir / "team-management" / "tasks"
        fw.mkdir(parents=True, exist_ok=True)
        (fw / "u.md").write_text("fw\n" * 300, encoding="utf-8")
        result = self.classify()
        self.assertEqual(result["diff_lines"], 60)
        self.assertEqual(result["framework_lines_excluded"], 300)
        records = self.read_log()
        self.assertEqual(len(records), 3)
        for rec in records:
            self.assertEqual(rec["diff_lines"], 60)
            self.assertEqual(rec["framework_lines_excluded"], 300)

    def test_classifier_error_records_zero_framework_lines(self):
        # The key is present on the error path too — a missing key and a zero
        # are different claims, and only one of them is true here.
        plain = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, plain, True)
        engine = ProtocolEngine(plain)
        result = engine._func_classify_review_dispatch()
        self.assertEqual(result["framework_lines_excluded"], 0)
        log = plain / ".claude" / "state" / _REVIEW_DISPATCH_LOG_NAME
        records = [json.loads(line) for line in
                   log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 3)
        for rec in records:
            self.assertEqual(rec["framework_lines_excluded"], 0)
            self.assertTrue(rec["reason"].startswith("classifier-error:"))

    def test_append_accumulates_across_evaluations(self):
        self.grow_tracked(50)
        self.classify()
        self.classify()
        self.assertEqual(len(self.read_log()), 6)

    def test_instructions_channel_respects_red_team_opt_out(self):
        # Round-2 W1: the prose instructions must never contradict §2a's
        # opt-out-wins rule — a disabled red-team gets an explicit "never
        # dispatch" line instead of the standing Critical-trigger invitation.
        self.write_config({"red_team_enabled": False})
        self.grow_tracked(300)
        result = self.classify()
        self.assertIn("red-team DISABLED by config", result["instructions"])
        self.assertNotIn("ALSO fire", result["instructions"])

    def test_instructions_channel_keeps_critical_invitation_when_enabled(self):
        self.grow_tracked(50)
        result = self.classify()
        self.assertIn("ALSO fire it if any Critical", result["instructions"])

    def test_disabled_agent_recorded_with_enabled_false(self):
        self.write_config({"red_team_enabled": False})
        self.grow_tracked(300)
        self.classify()
        by_agent = {r["agent"]: r for r in self.read_log()}
        self.assertFalse(by_agent["red-team"]["enabled"])
        self.assertFalse(by_agent["red-team"]["fired"])
        self.assertTrue(by_agent["testing-specialist"]["enabled"])
        self.assertTrue(by_agent["testing-specialist"]["fired"])


class TestRegistrationAndWiring(TestCase):
    def test_handler_and_metadata_both_registered(self):
        engine = ProtocolEngine(PROJECT_ROOT)
        self.assertIn("classify_review_dispatch", engine._build_handlers())
        names = [f["name"] for f in engine.get_available_funcs()["funcs"]]
        self.assertIn("classify_review_dispatch", names)

    def _code_review_pre_funcs(self, config_name: str):
        cfg = json.loads(
            (PROJECT_ROOT / "plugin" / "protocol-configs" / config_name)
            .read_text(encoding="utf-8"))
        steps = {s["name"]: s for s in cfg["steps"]}
        return steps["code-review"]["pre_funcs"]

    def test_wired_into_task_and_refactoring(self):
        for config_name in ("task.json", "refactoring.json"):
            pre = self._code_review_pre_funcs(config_name)
            self.assertIn("classify_review_dispatch", pre, config_name)
            self.assertLess(pre.index("resolve_ai_providers"),
                            pre.index("classify_review_dispatch"), config_name)
            # The spec audit runs last, as a post_func only — never at entry.
            self.assertEqual(pre[-1], "classify_review_dispatch", config_name)
            self.assertNotIn("require_spec_review_passed", pre, config_name)

    def test_not_wired_into_optimize_protocols(self):
        for config_name in ("optimize.json", "optimize-unattended.json"):
            pre = self._code_review_pre_funcs(config_name)
            self.assertNotIn("classify_review_dispatch", pre, config_name)

    def test_contract_docs_name_the_excluded_roots(self):
        # §2 tells the orchestrator to hand-re-measure with the classifier's own
        # commands on re-review rounds. If that instruction does not carry the
        # engine's exclusions, a hand re-measurement over-counts and can escalate
        # on framework lines the entry decision correctly ignored.
        text = (PROJECT_ROOT / "plugin" / "protocol-configs" / "sub-protocols"
                / "code-review.md").read_text(encoding="utf-8")
        marker = "**Re-review rounds after in-step fixes"
        start = text.index(marker)
        paragraph = text[start:text.index("\n\n", start)]
        for root in ("team-management/", ".claude/", ".agents/"):
            self.assertIn(root, paragraph, root)

        # The tool-visible contract must match the implementation too.
        engine = ProtocolEngine(PROJECT_ROOT)
        entry = next(f for f in engine.get_available_funcs()["funcs"]
                     if f["name"] == "classify_review_dispatch")
        # All three roots, matching the §2 loop above — asserting only the first
        # left two thirds of the tool-visible contract unpinned.
        for root in ("team-management/", ".claude/", ".agents/"):
            self.assertIn(root, entry["description"], root)
        self.assertIn("framework_lines_excluded", entry["description"])


if __name__ == "__main__":
    main()
