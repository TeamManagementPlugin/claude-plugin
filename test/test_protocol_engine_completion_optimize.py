#!/usr/bin/env python3
"""Tests for the optimize-protocol completion_dispatch branch (Phase 3).

Run with: python3 -m pytest test/test_protocol_engine_completion_optimize.py -v
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, main
from unittest.mock import patch, MagicMock

TEST_DIR = Path(__file__).parent
PROJECT_ROOT = TEST_DIR.parent
HOOKS_DIR = PROJECT_ROOT / "plugin" / "hooks"
sys.path.insert(0, str(HOOKS_DIR))


class CompletionOptimizeTestBase(TestCase):
    """Fixture for optimize-completion tests."""

    TASK_NAME = "o-test-completion"
    BRANCH = "optimize/test-completion"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        (self.temp_dir / ".claude" / "state").mkdir(parents=True)
        (self.temp_dir / ".claude" / "state" / "protocol-logs").mkdir(parents=True)
        (self.temp_dir / "team-management" / "tasks" / self.TASK_NAME).mkdir(parents=True)
        (self.temp_dir / "team-management" / "protocol-configs" / "system").mkdir(parents=True)
        (self.temp_dir / "plugin" / "protocol-configs").mkdir(parents=True)

        # Set up an active optimize protocol with current_step on completion (4)
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", {
            "task": self.TASK_NAME,
            "branch": self.BRANCH,
            "services": [],
            "updated": "2026-05-05",
            "protocol": {
                "name": "optimize",
                "current_step": 6,
                "step_name": "completion",
                "started_at": "2026-05-05T00:00:00+00:00",
                "loop_iteration": 5,
                "experimentation_started_at": "2026-05-05T00:00:00+00:00",
            },
        })
        self._write_json(self.temp_dir / ".claude" / "state" / "daic-mode.json", {"mode": "implementation"})

        # Task file with optimize.best_commit and optimize.baseline_commit in frontmatter
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        task_file.write_text(
            "---\n"
            f"task: {self.TASK_NAME}\n"
            f"branch: {self.BRANCH}\n"
            "status: in-progress\n"
            "created: 2026-05-05\n"
            "optimize.baseline_commit: base1111\n"
            "optimize.best_commit: best2222\n"
            "optimize.best_metric: 5.0\n"
            "---\n\n"
            "# Test optimization task\n",
            encoding="utf-8",
        )

        # Patch shared_state globals
        import shared_state
        self._orig = {
            "PROJECT_ROOT": shared_state.PROJECT_ROOT,
            "STATE_DIR": shared_state.STATE_DIR,
            "TASK_STATE_FILE": shared_state.TASK_STATE_FILE,
            "DAIC_STATE_FILE": shared_state.DAIC_STATE_FILE,
            "PROTOCOL_LOGS_DIR": shared_state.PROTOCOL_LOGS_DIR,
            "OPTIMIZE_STATE_FILE": shared_state.OPTIMIZE_STATE_FILE,
            "TASK_STATE_LOCK_FILE": shared_state.TASK_STATE_LOCK_FILE,
        }
        shared_state.PROJECT_ROOT = self.temp_dir
        shared_state.STATE_DIR = self.temp_dir / ".claude" / "state"
        shared_state.TASK_STATE_FILE = self.temp_dir / ".claude" / "state" / "current_task.json"
        shared_state.DAIC_STATE_FILE = self.temp_dir / ".claude" / "state" / "daic-mode.json"
        shared_state.PROTOCOL_LOGS_DIR = self.temp_dir / ".claude" / "state" / "protocol-logs"
        shared_state.OPTIMIZE_STATE_FILE = self.temp_dir / ".claude" / "state" / "optimize-state.json"
        shared_state.TASK_STATE_LOCK_FILE = self.temp_dir / ".claude" / "state" / "current_task.lock"

        from protocol_engine import ProtocolEngine
        self.engine = ProtocolEngine(self.temp_dir)

    def tearDown(self):
        import shared_state
        for key, value in self._orig.items():
            setattr(shared_state, key, value)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write_json(self, path: Path, data: dict):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _seed_tsv(self, *iter_value_hyp):
        """Seed results.tsv with rows: (iteration, metric_value, hypothesis)."""
        header = "iteration\ttimestamp\tcommit_sha\tmetric_value\trun_count\taggregator\twall_clock_s\tstatus\thypothesis\n"
        rows = []
        for it, v, h in iter_value_hyp:
            rows.append(f"{it}\t2026-05-05T00:00\t{('c'+str(it))*7}\t{v}\t1\tmedian\t0.1\tok\t{h}")
        tsv_path = self.temp_dir / "team-management" / "tasks" / self.TASK_NAME / "results.tsv"
        tsv_path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")


class TestCompletionOptimizeErrors(CompletionOptimizeTestBase):
    """Error paths: missing best_commit / baseline_commit / branch mismatch."""

    def test_no_best_commit_errors(self):
        # Strip optimize.best_commit
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        content = task_file.read_text(encoding="utf-8").replace("optimize.best_commit: best2222\n", "")
        task_file.write_text(content, encoding="utf-8")

        # Mock the branch check to pass
        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH):
            result = self.engine._func_completion_dispatch()
        self.assertFalse(result["success"])
        self.assertIn("optimize.best_commit", result["error"])

    def test_no_baseline_commit_errors(self):
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        content = task_file.read_text(encoding="utf-8").replace("optimize.baseline_commit: base1111\n", "")
        task_file.write_text(content, encoding="utf-8")

        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH):
            result = self.engine._func_completion_dispatch()
        self.assertFalse(result["success"])
        self.assertIn("optimize.baseline_commit", result["error"])

    def test_best_equals_baseline_errors(self):
        task_file = self.temp_dir / "team-management" / "tasks" / f"{self.TASK_NAME}.md"
        content = task_file.read_text(encoding="utf-8").replace(
            "optimize.best_commit: best2222\n",
            "optimize.best_commit: base1111\n",
        )
        task_file.write_text(content, encoding="utf-8")

        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH):
            result = self.engine._func_completion_dispatch()
        self.assertFalse(result["success"])
        self.assertIn("no improvement", result["error"])

    def test_branch_mismatch_errors(self):
        # Pretend we're on main, not the feature branch
        with patch.object(self.engine, "_current_branch", return_value="main"):
            result = self.engine._func_completion_dispatch()
        self.assertFalse(result["success"])
        self.assertIn("Branch mismatch", result["error"])
        self.assertIn(self.BRANCH, result["error"])

    def test_no_feature_branch_in_task_state_errors(self):
        # Wipe branch from task state
        ts = self._read_json(self.temp_dir / ".claude" / "state" / "current_task.json")
        ts["branch"] = ""
        self._write_json(self.temp_dir / ".claude" / "state" / "current_task.json", ts)

        result = self.engine._func_completion_dispatch()
        self.assertFalse(result["success"])
        self.assertIn("No feature branch", result["error"])

    def _read_json(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


class TestLeaderboardBuilder(CompletionOptimizeTestBase):
    """_build_leaderboard: sorted markdown table from TSV."""

    def test_min_direction_sorts_ascending(self):
        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        self._seed_tsv(
            (0, 10.0, "baseline"),
            (1, 8.5, "first hypothesis"),
            (2, 12.0, "regression"),
            (3, 4.5, "winner"),
        )
        md = self.engine._build_leaderboard(self.TASK_NAME)
        lines = md.split("\n")
        # Header rows
        self.assertIn("| Rank | Iter | Metric | Commit | Hypothesis |", lines[0])
        # Top of leaderboard should be iter 3 with metric 4.5
        self.assertIn("| 1 | 3 | 4.5 |", md)
        # Worst should be at bottom (rank 4 = iter 2 with 12.0)
        self.assertIn("| 4 | 2 | 12.0 |", md)

    def test_max_direction_sorts_descending(self):
        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "max"})
        self._seed_tsv(
            (0, 50.0, "baseline"),
            (1, 75.0, "better"),
            (2, 30.0, "worse"),
        )
        md = self.engine._build_leaderboard(self.TASK_NAME)
        # Max → highest first
        self.assertIn("| 1 | 1 | 75.0 |", md)

    def test_top_10_only(self):
        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        # Iterations start at 100 to avoid collision with rank numbers (1..10)
        self._seed_tsv(*[(100 + i, float(i), f"h{i}") for i in range(20)])
        md = self.engine._build_leaderboard(self.TASK_NAME)
        # Count data rows (lines starting with "|" excluding header + separator)
        body_lines = [l for l in md.split("\n") if l.startswith("|")]
        # 1 header + 1 separator + 10 data rows = 12
        self.assertEqual(len(body_lines), 12)
        # Rank 1 = lowest metric (iter 100, value 0.0)
        self.assertIn("| 1 | 100 | 0.0 |", md)
        # Rank 10 = iter 109 with value 9.0
        self.assertIn("| 10 | 109 | 9.0 |", md)
        # Iter 110 (value 10.0) is rank 11 → cut off
        self.assertNotIn("| 110 |", md)

    def test_no_rows_returns_placeholder(self):
        md = self.engine._build_leaderboard(self.TASK_NAME)
        self.assertIn("no ok rows", md)

    def test_pipe_in_hypothesis_escaped(self):
        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        self._seed_tsv((0, 5.0, "uses | pipe character"))
        md = self.engine._build_leaderboard(self.TASK_NAME)
        # The bare pipe inside hypothesis should be escaped
        self.assertIn(r"\|", md)


class TestSquashFlow(CompletionOptimizeTestBase):
    """End-to-end: _completion_optimize triggers squash + chain."""

    def test_squash_then_chain_invoked(self):
        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        self._seed_tsv((0, 10.0, "baseline"), (1, 5.0, "winner"))

        # Mock the branch check + git subprocess + downstream chain.
        # First subprocess call captures original HEAD; subsequent calls
        # are reset --hard / reset --soft / commit.
        head_capture = MagicMock(returncode=0, stdout="origHEAD\n", stderr="")
        git_ok = MagicMock(returncode=0, stdout="", stderr="")
        chain_ok = {"func": "completion_dispatch", "success": True,
                    "branch_taken": "optimize", "sub_results": []}

        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH), \
             patch("protocol_engine.subprocess.run", side_effect=[head_capture, git_ok, git_ok, git_ok]) as mock_run, \
             patch.object(self.engine, "_run_completion_chain", return_value=chain_ok) as mock_chain:
            result = self.engine._func_completion_dispatch()

        self.assertTrue(result["success"])
        self.assertEqual(result["squashed_from"], "best2222")
        self.assertEqual(result["squashed_baseline"], "base1111")
        # Verify squash commands invoked: rev-parse HEAD (capture), reset --hard,
        # reset --soft, commit. The "checkout" approach was replaced because
        # `git checkout <best> -- .` does not propagate deletions.
        called_commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertTrue(any(cmd[:2] == ["git", "rev-parse"] for cmd in called_commands))
        self.assertTrue(any(cmd[:3] == ["git", "reset", "--hard"] and cmd[3] == "best2222" for cmd in called_commands))
        self.assertTrue(any(cmd[:3] == ["git", "reset", "--soft"] and cmd[3] == "base1111" for cmd in called_commands))
        self.assertTrue(any(cmd[:2] == ["git", "commit"] for cmd in called_commands))
        # Verify provider chain handed off; git_commit IS present (after
        # archive_task) so the archive rename is committed — Codex round-2
        # critical fix.
        self.assertEqual(mock_chain.call_count, 1)
        chain_args = mock_chain.call_args
        chain_funcs = [name for name, _ in chain_args.args[1]]
        self.assertIn("git_commit", chain_funcs)
        # archive_task must come BEFORE git_commit so the rename is staged
        archive_idx = chain_funcs.index("archive_task")
        commit_idx = chain_funcs.index("git_commit")
        self.assertLess(archive_idx, commit_idx)
        self.assertIn("create_merge_request", chain_funcs)

    def test_squash_failure_aborts(self):
        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        self._seed_tsv((0, 10.0, "baseline"), (1, 5.0, "winner"))

        # First call (rev-parse HEAD) succeeds; second (reset --hard) fails.
        head_capture = MagicMock(returncode=0, stdout="origHEAD\n", stderr="")
        git_fail = MagicMock(returncode=128, stdout="", stderr="fatal: bad SHA")
        # The rollback path may also issue a `reset --hard` to original_head;
        # extra responses cover that.
        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH), \
             patch("protocol_engine.subprocess.run",
                   side_effect=[head_capture, git_fail, git_fail]), \
             patch.object(self.engine, "_run_completion_chain") as mock_chain:
            result = self.engine._func_completion_dispatch()
        self.assertFalse(result["success"])
        self.assertIn("git reset --hard", result["error"])
        # Chain must NOT have been invoked
        self.assertEqual(mock_chain.call_count, 0)

    def test_squash_step1_failure_rolls_back_to_original_head(self):
        """R2-O1: a failed step-1 `git reset --hard <best>` must roll back to
        the captured original HEAD, consistent with steps 2/3 (previously it
        returned the error without any rollback, leaving a partially-modified
        working tree)."""
        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        self._seed_tsv((0, 10.0, "baseline"), (1, 5.0, "winner"))

        head_capture = MagicMock(returncode=0, stdout="origHEAD\n", stderr="")
        git_fail = MagicMock(returncode=128, stdout="", stderr="fatal: bad SHA")
        rollback_ok = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH), \
             patch("protocol_engine.subprocess.run",
                   side_effect=[head_capture, git_fail, rollback_ok]) as mock_run, \
             patch.object(self.engine, "_run_completion_chain") as mock_chain:
            result = self.engine._func_completion_dispatch()

        self.assertFalse(result["success"])
        self.assertIn("git reset --hard", result["error"])
        called_commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertIn(["git", "reset", "--hard", "origHEAD"], called_commands)
        self.assertEqual(mock_chain.call_count, 0)

    def test_squash_head_capture_failure_aborts_before_any_reset(self):
        """R2-O1: when `git rev-parse HEAD` fails there is no rollback point —
        the squash must abort with an explicit error BEFORE issuing any reset
        (previously it proceeded with a silently no-op _rollback)."""
        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        self._seed_tsv((0, 10.0, "baseline"), (1, 5.0, "winner"))

        capture_fail = MagicMock(returncode=128, stdout="", stderr="fatal: not a git repo")
        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH), \
             patch("protocol_engine.subprocess.run",
                   side_effect=[capture_fail]) as mock_run, \
             patch.object(self.engine, "_run_completion_chain") as mock_chain:
            result = self.engine._func_completion_dispatch()

        self.assertFalse(result["success"])
        self.assertIn("HEAD", result["error"])
        # Only the rev-parse capture ran — no reset of any kind was attempted.
        called_commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(len(called_commands), 1)
        self.assertEqual(called_commands[0][:2], ["git", "rev-parse"])
        self.assertEqual(mock_chain.call_count, 0)

    def test_disabled_provider_requires_completion_option_BEFORE_squash(self):
        """provider == 'disabled' must validate completion_option BEFORE
        running the destructive squash. Round-2 review (Codex C1 / Claude W1):
        a missing-option call should not mutate the branch.
        """
        config_path = self.temp_dir / "team-management" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"issue_tracking": {"provider": "disabled"}}, f)

        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        self._seed_tsv((0, 10.0, "baseline"), (1, 5.0, "winner"))

        # Track all subprocess calls; with no completion_option, NO git
        # commands should fire because we validate before squashing.
        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH), \
             patch("protocol_engine.subprocess.run") as mock_run:
            result = self.engine._func_completion_dispatch()

        self.assertFalse(result["success"])
        self.assertIn("completion_option is required", result["error"])
        self.assertEqual(mock_run.call_count, 0)
        # The squash payload should NOT be reported (squash didn't run)
        self.assertEqual(result.get("squashed_from"), "best2222")
        # But the squash itself was skipped — verify by absence of subprocess invocations.

    def test_disabled_provider_discard_skips_squash_even_after_confirmation(self):
        """Codex round-3 critical: discard path must NOT squash before
        deletion. Squash mutates branch history irreversibly; on partial
        failure of _completion_discard the user is stranded on a squashed-
        but-not-discarded branch."""
        config_path = self.temp_dir / "team-management" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"issue_tracking": {"provider": "disabled"}}, f)

        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        self._seed_tsv((0, 10.0, "baseline"), (1, 5.0, "winner"))

        def fake_run(*a, **k):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "0\n"
            m.stderr = ""
            return m

        discard_ok = {"func": "completion_dispatch", "success": True,
                      "branch_taken": "discard", "sub_results": []}
        # Confirmed discard: full two-step typed confirmation
        confirmed_args = {
            "completion_option": "discard",
            "discard_confirmation": "discard",
            "discard_confirmed_dry_run": True,
        }
        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH), \
             patch("protocol_engine.subprocess.run", side_effect=fake_run) as mock_run, \
             patch.object(self.engine, "_completion_discard", return_value=discard_ok) as mock_discard:
            result = self.engine._func_completion_dispatch(args=confirmed_args)

        self.assertTrue(result["success"])
        self.assertEqual(mock_discard.call_count, 1)
        # squash-specific commands must NOT have run
        called_commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertFalse(any(cmd[:3] == ["git", "reset", "--hard"] for cmd in called_commands),
                         f"squash reset --hard should NOT have run for confirmed discard: {called_commands!r}")
        self.assertFalse(any(cmd[:3] == ["git", "reset", "--soft"] for cmd in called_commands),
                         f"squash reset --soft should NOT have run for confirmed discard: {called_commands!r}")
        # squashed_from / squashed_baseline should NOT be set on the result
        # because squash didn't run
        self.assertNotIn("squashed_from", result)

    def test_disabled_provider_dispatches_keep(self):
        """provider == 'disabled' + completion_option='keep' should call
        _completion_keep and propagate the squash payload."""
        config_path = self.temp_dir / "team-management" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"issue_tracking": {"provider": "disabled"}}, f)

        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        self._seed_tsv((0, 10.0, "baseline"), (1, 5.0, "winner"))

        head_capture = MagicMock(returncode=0, stdout="origHEAD\n", stderr="")
        git_ok = MagicMock(returncode=0, stdout="", stderr="")
        keep_ok = {"func": "completion_dispatch", "success": True,
                   "branch_taken": "keep", "sub_results": []}
        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH), \
             patch("protocol_engine.subprocess.run",
                   side_effect=[head_capture, git_ok, git_ok, git_ok]), \
             patch.object(self.engine, "_completion_keep", return_value=keep_ok) as mock_keep, \
             patch.object(self.engine, "_completion_merge_local") as mock_merge:
            result = self.engine._func_completion_dispatch(args={"completion_option": "keep"})

        self.assertTrue(result["success"])
        self.assertEqual(mock_keep.call_count, 1)
        self.assertEqual(mock_merge.call_count, 0)
        self.assertEqual(result["squashed_from"], "best2222")
        self.assertEqual(result["leaderboard"][:1], "|")  # leaderboard markdown attached

    def test_disabled_provider_discard_dry_run_does_not_squash(self):
        """provider == 'disabled' + completion_option='discard' must run
        the typed-confirmation gate BEFORE squashing. A dry-run call must
        NOT mutate the branch (Codex round-2 critical: prior ordering let
        the dry-run squash the branch and only then surface the dry-run
        error).
        """
        config_path = self.temp_dir / "team-management" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"issue_tracking": {"provider": "disabled"}}, f)

        from shared_state import write_optimize_state
        write_optimize_state({"metric_direction": "min"})
        self._seed_tsv((0, 10.0, "baseline"), (1, 5.0, "winner"))

        # Use a function side_effect so the discard gate's internal git
        # probes (rev-parse / rev-list for the dry-run message) succeed.
        def fake_run(*a, **k):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "0\n"
            m.stderr = ""
            return m

        with patch.object(self.engine, "_current_branch", return_value=self.BRANCH), \
             patch("protocol_engine.subprocess.run", side_effect=fake_run) as mock_run, \
             patch.object(self.engine, "_completion_discard") as mock_discard:
            # First call: discard with no confirmation → blocked by gate
            result = self.engine._func_completion_dispatch(args={"completion_option": "discard"})
        self.assertFalse(result["success"])
        self.assertEqual(mock_discard.call_count, 0)
        # Critical: NO squash-specific git commands (reset --hard / reset --soft
        # / commit) — squash must not run when discard dry-run blocks.
        called_commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertFalse(any(cmd[:3] == ["git", "reset", "--hard"] for cmd in called_commands),
                         f"squash reset --hard should NOT have run before discard confirmation: {called_commands!r}")
        self.assertFalse(any(cmd[:3] == ["git", "reset", "--soft"] for cmd in called_commands),
                         f"squash reset --soft should NOT have run before discard confirmation: {called_commands!r}")
        self.assertFalse(any(cmd[:2] == ["git", "commit"] for cmd in called_commands),
                         f"squash commit should NOT have run before discard confirmation: {called_commands!r}")


class TestNonOptimizeProtocolUnaffected(CompletionOptimizeTestBase):
    """Backwards-compat: non-optimize protocols hit the existing dispatcher path."""

    def test_task_protocol_does_not_hit_optimize_branch(self):
        # Switch the active protocol to "task"
        ts = json.loads((self.temp_dir / ".claude" / "state" / "current_task.json").read_text())
        ts["protocol"]["name"] = "task"
        with open(self.temp_dir / ".claude" / "state" / "current_task.json", "w") as f:
            json.dump(ts, f)

        # Configure a real provider so the legacy chain triggers
        config_path = self.temp_dir / "team-management" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"issue_tracking": {"provider": "gitlab"}}, f)

        # Mock _run_completion_chain to confirm we hit the legacy chain (with git_commit)
        with patch.object(self.engine, "_run_completion_chain") as mock_chain:
            mock_chain.return_value = {"func": "completion_dispatch", "success": True,
                                        "branch_taken": "provider", "sub_results": []}
            self.engine._func_completion_dispatch()

        chain_funcs = [name for name, _ in mock_chain.call_args.args[1]]
        # Legacy chain has git_commit
        self.assertIn("git_commit", chain_funcs)


if __name__ == "__main__":
    main()
